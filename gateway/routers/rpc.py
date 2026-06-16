from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError

from config.settings import Settings, get_settings
from models.domain import ToolCallParams, ToolListParams, ToolSearchParams
from models.jsonrpc import (
    JsonRpcErrorCode,
    JsonRpcRequest,
    JsonRpcResponse,
    make_error_response,
)
from services.authorization import AuthorizationService, get_authorization_service
from services.cache_manager import SemanticCacheManager
from services.credential_broker import CallerIdentity
from services.data_plane import record_billable_call
from services.hybrid_search import HybridSearchService, get_last_fusion_path
from services.metrics import (
    observe_cache_event,
    observe_downstream_error,
    observe_quota_block,
    observe_quota_preflight_block,
)
from services.pending_actions import consume_approved_action, create_pending_action
from services.proxy_registry import DownstreamTimeout, get_proxy_registry
from services.registry_watcher import get_catalog_version
from services.telemetry_logger import TelemetryLogger, get_telemetry_logger
from services.tenant_provisioner import UnknownTenantError, ensure_tenant_ready
from services.tenant_status import TenantInactiveError, assert_tenant_active
from services.tracing import set_span_attribute, start_span
from services.usage_metering import check_quota, check_sandbox_preflight

router = APIRouter(tags=["rpc"])

# Methods that touch a tenant database; these require the tenant to be provisioned
# before we run any query against it.
_TENANT_SCOPED_METHODS = frozenset({"tools/call", "tools/list", "tools/search"})

_hybrid_search_service: HybridSearchService | None = None
_cache_manager: SemanticCacheManager | None = None


def get_hybrid_search_service() -> HybridSearchService:
    global _hybrid_search_service
    if _hybrid_search_service is None:
        _hybrid_search_service = HybridSearchService()
    return _hybrid_search_service


def get_cache_manager() -> SemanticCacheManager:
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = SemanticCacheManager()
    return _cache_manager


@dataclass
class RpcContext:
    """Per-request state and resolved dependencies for a single JSON-RPC call.

    Bundles the request envelope, identity, the active tracing span, and the
    services a handler needs so handlers take one argument instead of a long
    positional signature, and so dependencies are injected (and easily faked)
    rather than reached for as module globals.
    """

    http_request: Request
    request: JsonRpcRequest
    started: float
    tenant_id: str
    user_id: str
    request_log_id: str | None
    span: Any
    settings: Settings
    hybrid_search_service: HybridSearchService
    cache_manager: SemanticCacheManager
    telemetry_logger: TelemetryLogger
    authorization_service: AuthorizationService


RpcHandler = Callable[[RpcContext], Awaitable[JsonRpcResponse]]


def _to_mcp_tool(item: dict[str, Any]) -> dict[str, Any]:
    """Shape a catalog document into an MCP tool descriptor.

    Keeps the gateway's discovery responses spec-shaped (`inputSchema`) while
    carrying the routing hints (`server`, `scopes`) the agent needs to call it.
    """
    return {
        "name": item.get("name"),
        "description": item.get("description", ""),
        "inputSchema": item.get("input_schema", {}),
        "server": item.get("server"),
        "scopes": item.get("scopes", []),
    }


def _caller_scopes(http_request: Request, explicit: list[str] | None) -> list[str] | None:
    if explicit:
        return explicit
    state_scopes = getattr(http_request.state, "scopes", None)
    return state_scopes or None


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        parsed = int(cursor)
        return max(0, parsed)
    except ValueError:
        return 0


@router.post("/rpc", response_model=JsonRpcResponse)
async def jsonrpc_handler(http_request: Request, request: JsonRpcRequest) -> JsonRpcResponse:
    started = perf_counter()
    settings = get_settings()
    tenant_id = getattr(http_request.state, "tenant_id", "unknown")
    user_id = getattr(http_request.state, "user_id", "unknown")
    request_log_id = getattr(http_request.state, "request_id", None)
    with start_span(
        f"rpc {request.method}",
        {
            "rpc.method": request.method,
            "mcp.tenant_id": tenant_id,
            "mcp.user_id": user_id,
            "mcp.request_id": request_log_id,
        },
    ) as span:
        context = RpcContext(
            http_request=http_request,
            request=request,
            started=started,
            tenant_id=tenant_id,
            user_id=user_id,
            request_log_id=request_log_id,
            span=span,
            settings=settings,
            hybrid_search_service=get_hybrid_search_service(),
            cache_manager=get_cache_manager(),
            telemetry_logger=get_telemetry_logger(),
            authorization_service=get_authorization_service(),
        )
        return await _dispatch(context)


async def _dispatch(context: RpcContext) -> JsonRpcResponse:
    """Route a request to its handler, applying preconditions and error mapping.

    This is the single switchboard: it looks the method up in ``_HANDLERS``,
    enforces tenant readiness for tenant-scoped methods, then delegates. All
    handler exceptions funnel through one mapper so every failure leaves as a
    protocol-safe JSON-RPC error frame.
    """
    request = context.request
    try:
        handler = _HANDLERS.get(request.method)
        if handler is None:
            return make_error_response(
                request_id=request.id,
                code=JsonRpcErrorCode.METHOD_NOT_FOUND,
                message=f"Unsupported method: {request.method}",
            )

        if request.method in _TENANT_SCOPED_METHODS:
            # Make the tenant boundary explicit: provision-on-first-use (or fail
            # loudly) so tenant-scoped queries never run against a missing database.
            await ensure_tenant_ready(context.tenant_id, settings=context.settings)
            # Abuse kill-switch: a suspended tenant cannot run tools or consume
            # resources until an operator resumes it.
            await assert_tenant_active(context.tenant_id, settings=context.settings)
        return await handler(context)
    except ValidationError as exc:
        return make_error_response(
            request_id=request.id,
            code=JsonRpcErrorCode.INVALID_PARAMS,
            message="Invalid JSON-RPC parameters.",
            data={"errors": exc.errors()},
        )
    except KeyError as exc:
        return make_error_response(
            request_id=request.id,
            code=JsonRpcErrorCode.METHOD_NOT_FOUND,
            message=str(exc),
        )
    except UnknownTenantError as exc:
        observe_downstream_error("unknown_tenant")
        _telemetry(
            context,
            status="unknown_tenant",
            metadata={"tenant_id": exc.tenant_id},
        )
        return make_error_response(
            request_id=request.id,
            code=JsonRpcErrorCode.INVALID_REQUEST,
            message=str(exc),
            data={"tenant_id": exc.tenant_id, "reason": "tenant_not_provisioned"},
        )
    except TenantInactiveError as exc:
        observe_downstream_error(exc.status_code)
        _telemetry(
            context,
            status=exc.status_code,
            metadata={"tenant_id": exc.tenant_id, "reason": exc.reason},
        )
        return make_error_response(
            request_id=request.id,
            code=JsonRpcErrorCode.FORBIDDEN,
            message=exc.message,
            data={
                "tenant_id": exc.tenant_id,
                "reason": exc.status_code,
                "detail": exc.reason,
            },
        )
    except Exception as exc:
        observe_downstream_error("gateway_error")
        _telemetry(
            context,
            status="error",
            metadata={"error": str(exc)},
        )
        return make_error_response(
            request_id=request.id,
            code=JsonRpcErrorCode.INTERNAL_ERROR,
            message="Gateway execution failed.",
            data={"error": str(exc)},
        )


async def _handle_initialize(context: RpcContext) -> JsonRpcResponse:
    return JsonRpcResponse(
        id=context.request.id,
        result={
            "protocolVersion": "2025-06-18",
            "capabilities": {
                "tools": {
                    "listChanged": True,
                    "pagination": True,
                }
            },
            "serverInfo": {
                "name": context.settings.app_name,
                "version": context.settings.app_version,
            },
            "catalog_version": get_catalog_version(),
        },
    )


async def _handle_list_changed(context: RpcContext) -> JsonRpcResponse:
    return JsonRpcResponse(
        id=context.request.id,
        result={"catalog_version": get_catalog_version(), "changed": True},
    )


async def _handle_tools_call(context: RpcContext) -> JsonRpcResponse:
    request = context.request
    call_params = ToolCallParams(**request.params)
    set_span_attribute(context.span, "mcp.server", call_params.server)
    set_span_attribute(context.span, "mcp.tool", call_params.name)

    caller_scopes = _caller_scopes(context.http_request, None)
    caller_roles = getattr(context.http_request.state, "roles", [])
    authz = await context.authorization_service.authorize_tool_call(
        tenant_id=context.tenant_id,
        server=call_params.server,
        name=call_params.name,
        caller_scopes=caller_scopes,
        caller_roles=caller_roles,
    )
    set_span_attribute(context.span, "mcp.authz", authz.reason)
    if not authz.allowed:
        _telemetry(
            context,
            status="forbidden",
            metadata={
                "server": call_params.server,
                "tool": call_params.name,
                "reason": authz.reason,
                "scopes": caller_scopes,
            },
        )
        return make_error_response(
            request_id=request.id,
            code=JsonRpcErrorCode.FORBIDDEN,
            message="Insufficient scope for tool call.",
            data={
                "server": call_params.server,
                "tool": call_params.name,
                "reason": authz.reason,
            },
        )

    quota_allowed, quota_reason, usage, quota = await check_quota(
        context.tenant_id,
        settings=context.settings,
    )
    if not quota_allowed:
        observe_quota_block()
        _telemetry(
            context,
            status="quota_exceeded",
            metadata={
                "server": call_params.server,
                "tool": call_params.name,
                "reason": quota_reason,
                "period": usage.get("period"),
                "usage": usage,
                "quota": quota,
            },
        )
        return make_error_response(
            request_id=request.id,
            code=JsonRpcErrorCode.RATE_LIMITED,
            message="Tenant usage quota exceeded.",
            data={
                "server": call_params.server,
                "tool": call_params.name,
                "reason": "quota_exceeded",
                "quota_reason": quota_reason,
                "period": usage.get("period"),
                "usage": usage,
                "quota": quota,
            },
        )

    tool_metadata = (authz.tool or {}).get("metadata", {})

    # Code-backed tools execute in the local sandbox path. When the feature flag
    # is off, refuse the call with a clear, protocol-safe error instead of
    # attempting any downstream proxy hop.
    is_code_tool = tool_metadata.get("transport") == "code"
    if is_code_tool and not context.settings.code_tool_execution_enabled:
        _telemetry(
            context,
            status="code_execution_disabled",
            metadata={"server": call_params.server, "tool": call_params.name},
        )
        return make_error_response(
            request_id=request.id,
            code=JsonRpcErrorCode.SERVER_ERROR,
            message="Code tool execution is not yet enabled.",
            data={
                "server": call_params.server,
                "tool": call_params.name,
                "reason": "code_execution_not_enabled",
            },
        )

    # Sandbox quota preflight: for code tools, reject up front when the tool's
    # worst-case wall-clock cost cannot fit the tenant's remaining sandbox-seconds
    # budget, instead of starting work that gets killed mid-flight. Shared with
    # /mcp via check_sandbox_preflight so both surfaces enforce it identically.
    if is_code_tool and context.settings.quota_preflight_enabled:
        projected_ms = int(
            tool_metadata.get("wall_timeout_ms") or context.settings.sandbox_wall_timeout_ms
        )
        preflight_ok, preflight_reason = check_sandbox_preflight(
            usage=usage, quota=quota, projected_ms=projected_ms
        )
        if not preflight_ok:
            observe_quota_preflight_block()
            _telemetry(
                context,
                status="sandbox_quota_preflight",
                metadata={
                    "server": call_params.server,
                    "tool": call_params.name,
                    "reason": preflight_reason,
                    "projected_ms": projected_ms,
                    "usage": usage,
                    "quota": quota,
                },
            )
            return make_error_response(
                request_id=request.id,
                code=JsonRpcErrorCode.RATE_LIMITED,
                message="Projected sandbox cost exceeds the tenant's remaining quota.",
                data={
                    "server": call_params.server,
                    "tool": call_params.name,
                    "reason": "sandbox_quota_preflight",
                    "projected_ms": projected_ms,
                    "usage": usage,
                    "quota": quota,
                },
            )

    requires_confirmation = bool(tool_metadata.get("requires_confirmation"))
    action_type = str(tool_metadata.get("action_type", "destructive"))
    if requires_confirmation and call_params.confirmation_id:
        consume_status, action_doc = await consume_approved_action(
            tenant_id=context.tenant_id,
            action_id=call_params.confirmation_id,
            user_id=context.user_id,
            server=call_params.server,
            tool=call_params.name,
            arguments=call_params.arguments,
        )
        if consume_status == "ok":
            set_span_attribute(context.span, "mcp.confirmation", "consumed")
            _telemetry(
                context,
                status="confirmation_consumed",
                metadata={
                    "server": call_params.server,
                    "tool": call_params.name,
                    "action_id": call_params.confirmation_id,
                },
            )
        elif consume_status == "mismatch":
            set_span_attribute(context.span, "mcp.confirmation", "invalid")
            _telemetry(
                context,
                status="confirmation_invalid",
                metadata={
                    "server": call_params.server,
                    "tool": call_params.name,
                    "action_id": call_params.confirmation_id,
                    "reason": consume_status,
                },
            )
            return make_error_response(
                request_id=request.id,
                code=JsonRpcErrorCode.FORBIDDEN,
                message="Confirmation token does not match this request.",
                data={
                    "server": call_params.server,
                    "tool": call_params.name,
                    "reason": consume_status,
                },
            )
        else:
            set_span_attribute(context.span, "mcp.confirmation", "required")
            _telemetry(
                context,
                status="confirmation_invalid",
                metadata={
                    "server": call_params.server,
                    "tool": call_params.name,
                    "action_id": call_params.confirmation_id,
                    "reason": consume_status,
                },
            )
            return _confirmation_required_response(
                request_id=request.id,
                server=call_params.server,
                tool=call_params.name,
                action_type=action_type,
                action_id=call_params.confirmation_id,
                expires_at=action_doc.get("expires_at") if action_doc else None,
                reason=consume_status,
            )
    elif requires_confirmation:
        pending_action = await create_pending_action(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            server=call_params.server,
            tool=call_params.name,
            arguments=call_params.arguments,
            action_type=action_type,
            ttl_seconds=context.settings.confirmation_ttl_seconds,
        )
        action_id = str(pending_action.get("_id", ""))
        set_span_attribute(context.span, "mcp.confirmation", "required")
        _telemetry(
            context,
            status="confirmation_pending",
            metadata={
                "server": call_params.server,
                "tool": call_params.name,
                "action_id": action_id,
                "action_type": action_type,
            },
        )
        return _confirmation_required_response(
            request_id=request.id,
            server=call_params.server,
            tool=call_params.name,
            action_type=action_type,
            action_id=action_id,
            expires_at=pending_action.get("expires_at"),
            reason="pending",
        )

    cacheable = bool(tool_metadata.get("cacheable", False))
    cache_ttl_seconds = int(tool_metadata.get("cache_ttl_seconds", 24 * 3600) or 0)
    invalidates = [name for name in tool_metadata.get("invalidates", []) if isinstance(name, str)]

    cache_response = await _try_cache_lookup(
        context=context,
        call_params=call_params,
        cacheable=cacheable,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    if cache_response is not None:
        return cache_response

    try:
        result = await _execute_downstream(context, call_params)
    except DownstreamTimeout as exc:
        observe_downstream_error("timeout")
        set_span_attribute(context.span, "mcp.downstream", "timeout")
        _telemetry(
            context,
            status="timeout_failure",
            metadata={
                "server": call_params.server,
                "tool": call_params.name,
                "error": str(exc),
            },
        )
        return make_error_response(
            request_id=request.id,
            code=JsonRpcErrorCode.UPSTREAM_TIMEOUT,
            message=str(exc),
            data={"server": call_params.server, "tool": call_params.name},
        )

    if cacheable and cache_ttl_seconds > 0:
        await context.cache_manager.store(
            call_params.name,
            call_params.arguments,
            result,
            tenant_id=context.tenant_id,
            ttl_seconds=cache_ttl_seconds,
        )
    if invalidates:
        await context.cache_manager.invalidate(tenant_id=context.tenant_id, tool_names=invalidates)

    await _record_billable_call(context, call_params, source="live_execution")
    _telemetry(
        context,
        status="live_execution_success",
        metadata={
            "server": call_params.server,
            "tool": call_params.name,
            "cacheable": cacheable,
            "invalidated": invalidates,
        },
    )
    return JsonRpcResponse(id=request.id, result=result)


async def _try_cache_lookup(
    *,
    context: RpcContext,
    call_params: ToolCallParams,
    cacheable: bool,
    cache_ttl_seconds: int,
) -> JsonRpcResponse | None:
    if not (cacheable and cache_ttl_seconds > 0):
        return None

    cached = await context.cache_manager.lookup(
        call_params.name,
        call_params.arguments,
        tenant_id=context.tenant_id,
    )
    if cached is None:
        observe_cache_event("miss")
        set_span_attribute(context.span, "mcp.cache", "miss")
        return None

    observe_cache_event("hit")
    set_span_attribute(context.span, "mcp.cache", "hit")
    _telemetry(
        context,
        status="cache_hit",
        metadata={"server": call_params.server, "tool": call_params.name},
    )
    await _record_billable_call(context, call_params, source="cache_hit")
    return JsonRpcResponse(id=context.request.id, result=cached)


async def _record_billable_call(
    context: RpcContext,
    call_params: ToolCallParams,
    *,
    source: str,
) -> None:
    # Delegates to the shared data-plane helper so /rpc and /mcp meter calls
    # identically (single source of truth for "a billable tool call").
    await record_billable_call(
        context.tenant_id,
        server=call_params.server,
        tool=call_params.name,
        source=source,
    )


async def _execute_downstream(context: RpcContext, call_params: ToolCallParams) -> dict[str, Any]:
    caller = CallerIdentity(
        user_id=context.user_id,
        scopes=[scope for scope in getattr(context.http_request.state, "scopes", []) if scope],
        roles=[role for role in getattr(context.http_request.state, "roles", []) if role],
    )
    with start_span(
        "downstream.tools/call",
        {"mcp.server": call_params.server, "mcp.tool": call_params.name},
    ):
        return await get_proxy_registry().call_tool(
            server_name=call_params.server,
            tool_name=call_params.name,
            arguments=call_params.arguments,
            tenant_id=context.tenant_id,
            caller=caller,
        )


async def _handle_tools_list(context: RpcContext) -> JsonRpcResponse:
    request = context.request
    list_params = ToolListParams(**request.params)
    query = list_params.query or context.http_request.headers.get(context.settings.query_header)
    scopes = _caller_scopes(context.http_request, list_params.scopes)
    catalog_version = get_catalog_version()
    client_catalog_version = list_params.client_catalog_version or 0

    if query:
        items = await context.hybrid_search_service.search_tools(
            tenant_id=context.tenant_id,
            query=query,
            limit=list_params.limit or context.settings.route_top_k,
            allowed_scopes=scopes,
        )
        status = "tools_list_routed"
        next_cursor = None
    else:
        limit = min(
            max(1, list_params.limit or context.settings.route_top_k),
            context.settings.catalog_list_limit,
        )
        offset = _decode_cursor(list_params.cursor)
        items = await context.hybrid_search_service.list_tools(
            tenant_id=context.tenant_id,
            allowed_scopes=scopes,
            limit=limit + 1,
            offset=offset,
        )
        status = "tools_list_full"
        has_more = len(items) > limit
        if has_more:
            items = items[:limit]
        next_cursor = str(offset + limit) if has_more else None

    _telemetry(
        context,
        status=status,
        metadata={
            "query": query,
            "scopes": scopes,
            "returned": len(items),
            "next_cursor": next_cursor,
            "catalog_version": catalog_version,
            # Only the routed (query) branch runs ranked search; the full-list page
            # does no fusion, so report None there rather than a stale value.
            "fusion_path": get_last_fusion_path() if query else None,
        },
    )
    return JsonRpcResponse(
        id=request.id,
        result={
            "tools": [_to_mcp_tool(item) for item in items],
            "routed": bool(query),
            "next_cursor": next_cursor,
            "catalog_version": catalog_version,
            "list_changed": client_catalog_version != 0
            and client_catalog_version != catalog_version,
        },
    )


async def _handle_tools_search(context: RpcContext) -> JsonRpcResponse:
    request = context.request
    search_params = ToolSearchParams(**request.params)
    scopes = _caller_scopes(context.http_request, search_params.scopes)
    items = await context.hybrid_search_service.search_tools(
        tenant_id=context.tenant_id,
        query=search_params.query,
        limit=search_params.limit,
        vector_weight=search_params.vector_weight,
        text_weight=search_params.text_weight,
        allowed_scopes=scopes,
        mode=search_params.mode,
    )
    _telemetry(
        context,
        status="hybrid_search_success",
        metadata={
            "query": search_params.query,
            "limit": search_params.limit,
            "scopes": scopes,
            "mode": search_params.mode,
            # Which retrieval/fusion path actually served this query (native
            # $rankFusion vs app-side RRF vs single-arm). Observability only.
            "fusion_path": get_last_fusion_path(),
        },
    )
    return JsonRpcResponse(id=request.id, result={"mode": search_params.mode, "items": items})


def _telemetry(context: RpcContext, *, status: str, metadata: dict[str, Any]) -> None:
    context.telemetry_logger.log_background(
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        request_id=context.request_log_id,
        method=context.request.method,
        status=status,
        latency_ms=(perf_counter() - context.started) * 1000,
        metadata=metadata,
    )


def _confirmation_required_response(
    *,
    request_id: str | int | None,
    server: str,
    tool: str,
    action_type: str,
    action_id: str,
    expires_at: Any,
    reason: str,
) -> JsonRpcResponse:
    return JsonRpcResponse(
        id=request_id,
        result={
            "status": "confirmation_required",
            "reason": reason,
            "confirmation": {
                "action_id": action_id,
                "server": server,
                "tool": tool,
                "action_type": action_type,
                "expires_at": expires_at,
                "message": "This action requires approval before it can run.",
            },
        },
    )


_HANDLERS: dict[str, RpcHandler] = {
    "initialize": _handle_initialize,
    "notifications/tools/list_changed": _handle_list_changed,
    "tools/call": _handle_tools_call,
    "tools/list": _handle_tools_list,
    "tools/search": _handle_tools_search,
}
