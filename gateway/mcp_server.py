from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from fastmcp import FastMCP

from config.settings import get_settings
from services.authorization import get_authorization_service
from services.credential_broker import CallerIdentity
from services.data_plane import record_billable_call
from services.hybrid_search import HybridSearchService
from services.metrics import observe_quota_block, observe_quota_preflight_block
from services.proxy_registry import get_proxy_registry
from services.registry_watcher import get_catalog_version
from services.telemetry_logger import get_telemetry_logger
from services.tenant_status import TenantInactiveError, assert_tenant_active
from services.usage_metering import check_quota, check_sandbox_preflight

try:
    from fastmcp.server.auth.providers.jwt import JWTVerifier
except ImportError:  # pragma: no cover - defensive for FastMCP API drift
    JWTVerifier = None  # type: ignore[assignment,misc]

try:
    from fastmcp.server.dependencies import get_http_request
except ImportError:  # pragma: no cover - defensive for FastMCP API drift
    get_http_request = None  # type: ignore[assignment]

try:
    from fastmcp.exceptions import ToolError
except ImportError:  # pragma: no cover - defensive for FastMCP API drift

    class ToolError(Exception):  # type: ignore[no-redef]
        """Fallback when FastMCP does not export ToolError."""


hybrid_search_service = HybridSearchService()
_mcp_server: FastMCP | None = None


@dataclass
class _CallerIdentity:
    """Identity for an `/mcp` meta-tool call, resolved from the verified request.

    The gateway's ``AuthMiddleware`` runs for the mounted ``/mcp`` app and stamps
    the verified tenant/roles/scopes onto ``request.state``. These are the same
    values the ``/rpc`` data plane authorizes against, so reading them here keeps
    both surfaces bound to one source of truth instead of trusting tool arguments.

    ``user_id``/``request_id`` are carried so audit rows written for `/mcp` match
    the `/rpc` shape and can be correlated across both surfaces.
    """

    tenant_id: str
    roles: list[str]
    scopes: list[str]
    user_id: str
    request_id: str | None


def _audit(
    identity: _CallerIdentity,
    *,
    status: str,
    started: float,
    metadata: dict[str, Any],
) -> None:
    """Write an ``audit_telemetry`` row for an `/mcp` tool call.

    Uses the same ``method="tools/call"`` label as the `/rpc` data plane so one
    audit query spans both invocation surfaces. Fire-and-forget, exactly like
    `/rpc`: a telemetry failure must never break the tool call.
    """
    get_telemetry_logger().log_background(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        request_id=identity.request_id,
        method="tools/call",
        status=status,
        latency_ms=(perf_counter() - started) * 1000,
        metadata=metadata,
    )


def _build_auth_verifier():
    settings = get_settings()
    if settings.auth_mode != "jwks" or JWTVerifier is None:
        return None
    if not settings.jwks_uri:
        # Local-JWKS mode is enforced by the gateway middleware; FastMCP's JWT
        # verifier currently expects a URI endpoint.
        return None
    kwargs: dict[str, Any] = {"jwks_uri": settings.jwks_uri}
    if settings.jwt_issuer:
        kwargs["issuer"] = settings.jwt_issuer
    if settings.jwt_audience:
        kwargs["audience"] = settings.jwt_audience
    return JWTVerifier(**kwargs)


def _register_tools(mcp: FastMCP) -> None:
    settings = get_settings()

    def _resolve_identity(override_tenant_id: str | None = None) -> _CallerIdentity:
        """Resolve the caller's tenant/roles/scopes from the verified request.

        The tenant is bound to the authenticated claim: when auth is enabled, a
        ``tenant_id`` argument that does not match the verified tenant is refused
        rather than silently honored, so the meta-tool surface cannot be used to
        reach across tenant boundaries. In ``disabled`` mode there is no verified
        claim and the caller is already trusted, so an explicit override is kept.
        """
        request = None
        if get_http_request is not None:
            try:
                request = get_http_request()
            except Exception:
                # No active HTTP request (e.g. direct invocation). Fall through to
                # the default-tenant path below rather than failing the call.
                request = None

        state = getattr(request, "state", None) if request is not None else None
        if state is not None:
            verified_tenant = getattr(state, "tenant_id", None) or settings.default_tenant_id
            roles = [r for r in (getattr(state, "roles", None) or []) if isinstance(r, str)]
            scopes = [s for s in (getattr(state, "scopes", None) or []) if isinstance(s, str)]
            user_id = getattr(state, "user_id", None) or "unknown-user"
            request_id = getattr(state, "request_id", None)
        else:
            verified_tenant = settings.default_tenant_id
            roles = ["admin"] if settings.auth_mode == "disabled" else []
            scopes = []
            user_id = "local-dev" if settings.auth_mode == "disabled" else "unknown-user"
            request_id = None

        if (
            override_tenant_id
            and settings.auth_mode != "disabled"
            and override_tenant_id != verified_tenant
        ):
            raise ToolError(
                "cross_tenant_forbidden: the tenant_id argument does not match the "
                "authenticated tenant; cross-tenant access is not allowed on /mcp."
            )

        return _CallerIdentity(
            tenant_id=override_tenant_id or verified_tenant,
            roles=roles,
            scopes=scopes,
            user_id=user_id,
            request_id=request_id,
        )

    @mcp.tool
    async def search_tools(
        query: str,
        limit: int = 10,
        vector_weight: float | None = None,
        text_weight: float | None = None,
        allowed_scopes: list[str] | None = None,
        mode: str = "hybrid",
        tenant_id: str | None = None,
    ) -> dict:
        identity = _resolve_identity(tenant_id)
        await assert_tenant_active(identity.tenant_id)
        items = await hybrid_search_service.search_tools(
            tenant_id=identity.tenant_id,
            query=query,
            limit=limit,
            vector_weight=vector_weight,
            text_weight=text_weight,
            allowed_scopes=allowed_scopes,
            mode=mode,
        )
        return {
            "query": query,
            "limit": limit,
            "mode": mode,
            "items": items,
        }

    @mcp.tool
    async def list_catalog_tools(
        limit: int = 20,
        cursor: int = 0,
        query: str | None = None,
        allowed_scopes: list[str] | None = None,
        tenant_id: str | None = None,
    ) -> dict:
        identity = _resolve_identity(tenant_id)
        await assert_tenant_active(identity.tenant_id)
        if query:
            items = await hybrid_search_service.search_tools(
                tenant_id=identity.tenant_id,
                query=query,
                limit=limit,
                allowed_scopes=allowed_scopes,
            )
            return {
                "tools": items,
                "next_cursor": None,
                "catalog_version": get_catalog_version(),
                "routed": True,
            }

        page_size = min(max(1, limit), settings.catalog_list_limit)
        offset = max(0, cursor)
        items = await hybrid_search_service.list_tools(
            tenant_id=identity.tenant_id,
            allowed_scopes=allowed_scopes,
            limit=page_size + 1,
            offset=offset,
        )
        has_more = len(items) > page_size
        if has_more:
            items = items[:page_size]
        return {
            "tools": items,
            "next_cursor": offset + page_size if has_more else None,
            "catalog_version": get_catalog_version(),
            "routed": False,
        }

    @mcp.tool
    async def call_downstream_tool(
        server: str,
        name: str,
        arguments: dict,
        tenant_id: str | None = None,
    ) -> dict:
        # Full parity with the /rpc data plane (gateway/routers/rpc.py
        # _handle_tools_call): tenant-active -> per-call authorization ->
        # quota -> execute -> meter -> audit. Each gate emits an audit row so
        # every /mcp call lands in audit_telemetry just like /rpc.
        started = perf_counter()
        identity = _resolve_identity(tenant_id)
        try:
            await assert_tenant_active(identity.tenant_id)
        except TenantInactiveError as exc:
            _audit(
                identity,
                status=exc.status_code,
                started=started,
                metadata={"server": server, "tool": name, "reason": exc.reason},
            )
            raise

        # Per-call authorization: the tool must exist in the tenant catalog and
        # the caller must satisfy its scope/role requirements. Without this,
        # /mcp would proxy any (server, name) pair.
        authz = await get_authorization_service().authorize_tool_call(
            tenant_id=identity.tenant_id,
            server=server,
            name=name,
            caller_scopes=identity.scopes,
            caller_roles=identity.roles,
        )
        if not authz.allowed:
            _audit(
                identity,
                status="forbidden",
                started=started,
                metadata={
                    "server": server,
                    "tool": name,
                    "reason": authz.reason,
                    "scopes": identity.scopes,
                },
            )
            raise ToolError(f"forbidden: {authz.reason}")

        # Usage quota: enforced here so /mcp cannot be used to bypass the
        # tenant ceilings the /rpc surface enforces.
        quota_allowed, quota_reason, usage, quota = await check_quota(
            identity.tenant_id, settings=settings
        )
        if not quota_allowed:
            observe_quota_block()
            _audit(
                identity,
                status="quota_exceeded",
                started=started,
                metadata={
                    "server": server,
                    "tool": name,
                    "reason": quota_reason,
                    "period": usage.get("period"),
                    "usage": usage,
                    "quota": quota,
                },
            )
            raise ToolError(f"quota_exceeded: {quota_reason}")

        # Sandbox quota preflight: identical gate to the /rpc data plane so a code
        # tool whose worst-case sandbox cost cannot fit the remaining quota is
        # rejected before any work starts, rather than killed mid-flight.
        tool_metadata = (authz.tool or {}).get("metadata", {})
        is_code_tool = tool_metadata.get("transport") == "code"
        if is_code_tool and settings.quota_preflight_enabled:
            projected_ms = int(
                tool_metadata.get("wall_timeout_ms") or settings.sandbox_wall_timeout_ms
            )
            preflight_ok, preflight_reason = check_sandbox_preflight(
                usage=usage, quota=quota, projected_ms=projected_ms
            )
            if not preflight_ok:
                observe_quota_preflight_block()
                _audit(
                    identity,
                    status="sandbox_quota_preflight",
                    started=started,
                    metadata={
                        "server": server,
                        "tool": name,
                        "reason": preflight_reason,
                        "projected_ms": projected_ms,
                        "usage": usage,
                        "quota": quota,
                    },
                )
                raise ToolError(
                    "sandbox_quota_preflight: projected sandbox cost exceeds remaining quota"
                )

        caller = CallerIdentity(
            user_id=identity.user_id,
            scopes=identity.scopes,
            roles=identity.roles,
        )
        result = await get_proxy_registry().call_tool(
            server_name=server,
            tool_name=name,
            arguments=arguments,
            tenant_id=identity.tenant_id,
            caller=caller,
        )
        await record_billable_call(
            identity.tenant_id, server=server, tool=name, source="live_execution"
        )
        _audit(
            identity,
            status="live_execution_success",
            started=started,
            metadata={"server": server, "tool": name},
        )
        return {"server": server, "name": name, "result": result}


def _create_server() -> FastMCP:
    server = FastMCP("mdb-mcp-gateway", auth=_build_auth_verifier())
    _register_tools(server)
    return server


def get_mcp_server() -> FastMCP:
    global _mcp_server
    if _mcp_server is None:
        _mcp_server = _create_server()
    return _mcp_server
