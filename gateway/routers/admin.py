from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from config.settings import get_settings
from database.mongo import get_control_database, get_tenant_database, tenant_db_name
from models.admin import (
    AdminSearchRequest,
    CacheMigrateRequest,
    CatalogItemResponse,
    CatalogListRequest,
    CatalogListResponse,
    CodeToolTestRequest,
    CodeToolTestResponse,
    CodeToolValidateRequest,
    CodeToolValidateResponse,
    CodeToolValidationIssue,
    EgressAllowlistResponse,
    EgressAllowlistUpdateRequest,
    EmbeddingConfigResponse,
    EmbeddingConfigUpdateRequest,
    EmbeddingTestRequest,
    EmbeddingTestResponse,
    ExploreCollectionsResponse,
    ExploreQueryRequest,
    ExploreQueryResponse,
    ExploreSampleRequest,
    ExploreSampleResponse,
    PasswordChangeRequest,
    PendingActionListResponse,
    PendingActionResponse,
    QuotaResponse,
    QuotaUpdateRequest,
    ServerEnvResponse,
    ServerEnvUpdateRequest,
    ServerPatchRequest,
    ServerUpsertRequest,
    StatsResponse,
    TelemetryEventResponse,
    TelemetryListRequest,
    TelemetryListResponse,
    TenantCreateRequest,
    TenantDeleteResponse,
    TenantResponse,
    TenantStats,
    TenantStatusUpdateRequest,
    UsageEventsResponse,
    UsageRemaining,
    UsageResponse,
    UsageTotals,
    UserCreateRequest,
    UserListResponse,
    UserResponse,
    UserUpdateRequest,
    WhoAmIResponse,
)
from services import users as users_service
from services.cache_migration import SemanticCacheMigrationService
from services.code_tools import (
    CODE_TRANSPORT,
    CodeToolValidationError,
    decrypt_raw_code,
    encrypt_raw_code,
    is_encrypted_token,
    lint_code_tool,
    suggest_input_schema,
    validate_code_tool,
)
from services.credential_broker import CallerIdentity
from services.egress_policy import EgressNotAllowed, check_endpoint_allowed, parse_allowlist
from services.embedding_config import (
    EmbeddingConfig,
    default_model_for,
    delete_tenant_config,
    load_persisted_config,
    load_tenant_config,
    refresh_active_embedding_config,
    save_persisted_config,
    save_tenant_config,
    validate_config,
)
from services.embedding_reprovision import (
    ReprovisionInProgressError,
    get_reprovision_status,
    get_tenant_reprovision_status,
    is_reprovision_running,
    is_tenant_reprovision_running,
    trigger_reprovision,
    trigger_tenant_reprovision,
)
from services.embeddings import (
    SUPPORTED_PROVIDERS,
    build_provider_service,
    embedding_version_for,
)
from services.hybrid_search import HybridSearchService
from services.metrics import observe_egress_block
from services.passwords import verify_password
from services.pending_actions import (
    approve_action,
    list_pending_actions,
    reject_action,
)
from services.proxy_registry import get_proxy_registry
from services.registry_watcher import get_catalog_version
from services.sandbox_db_bridge import SandboxDbBridge
from services.sandbox_executor import (
    ExecRequest,
    SandboxError,
    SandboxProtocolError,
    SandboxTimeoutError,
    get_executor,
)
from services.server_exporter import (
    ServerExportError,
    ServerExportNotFound,
    build_server_export,
)
from services.server_guard import EndpointNotAllowed, StdioNotAllowed, enforce_server_policy
from services.telemetry_logger import get_telemetry_logger
from services.tenant_egress import get_tenant_egress_allowlist, set_tenant_egress_allowlist
from services.tenant_provisioner import deprovision_tenant, provision_tenant
from services.tenant_status import STATUS_ACTIVE, STATUS_SUSPENDED, set_tenant_status
from services.usage_metering import (
    get_effective_quota,
    get_usage,
    set_quota,
    summarize_billing_events,
)

router = APIRouter(prefix="/admin", tags=["admin"])
settings = get_settings()
cache_migration_service = SemanticCacheMigrationService()
hybrid_search_service = HybridSearchService()


def _is_platform_admin(request: Request) -> bool:
    roles = set(getattr(request.state, "roles", []))
    return settings.platform_admin_role in roles


def _require_platform_admin(request: Request) -> None:
    if not _is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Embedding configuration requires the platform-admin role.",
        )


def _require_tenant_admin(request: Request) -> None:
    roles = set(getattr(request.state, "roles", []))
    if _is_platform_admin(request) or "admin" in roles or "tenant-admin" in roles:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Approvals require tenant-admin or platform-admin role.",
    )


def _resolve_target_tenant(request: Request, requested_tenant: str | None = None) -> str:
    caller_tenant = getattr(request.state, "tenant_id", settings.default_tenant_id)
    header_tenant = request.headers.get("x-tenant-id")
    target = requested_tenant or header_tenant or caller_tenant
    if target != caller_tenant and not _is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-tenant admin access requires platform-admin role.",
        )
    return target


def _validate_secret_key(key: str) -> str:
    normalized = key.strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Secret keys must not be empty.",
        )
    if "." in normalized or normalized.startswith("$"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Secret keys may not contain '.' or start with '$'.",
        )
    return normalized


async def _require_server_exists(tenant_id: str, server_name: str) -> None:
    doc = await get_tenant_database(tenant_id)["routing_registry"].find_one({"_id": server_name})
    if not isinstance(doc, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found.")


def _pending_action_response(doc: dict[str, Any]) -> PendingActionResponse:
    return PendingActionResponse(
        action_id=str(doc.get("_id", "")),
        tenant_id=str(doc.get("tenant_id", "")),
        user_id=str(doc.get("user_id", "")),
        server=str(doc.get("server", "")),
        tool=str(doc.get("tool", "")),
        arguments=doc.get("arguments", {}) if isinstance(doc.get("arguments"), dict) else {},
        action_type=str(doc.get("action_type", "destructive")),
        status=str(doc.get("status", "pending")),
        created_at=doc.get("created_at"),
        expires_at=doc.get("expires_at"),
        decided_by=doc.get("decided_by"),
        decided_at=doc.get("decided_at"),
    )


def _usage_response(
    *,
    tenant_id: str,
    period: str,
    calls: int,
    sandbox_ms: int,
    calls_limit: int,
    sandbox_seconds_limit: int,
) -> UsageResponse:
    calls_remaining = None if calls_limit <= 0 else max(0, calls_limit - calls)
    used_sandbox_seconds = max(0, int(sandbox_ms) // 1000)
    sandbox_seconds_remaining = (
        None if sandbox_seconds_limit <= 0 else max(0, sandbox_seconds_limit - used_sandbox_seconds)
    )
    return UsageResponse(
        tenant_id=tenant_id,
        period=period,
        usage=UsageTotals(calls=max(0, int(calls)), sandbox_ms=max(0, int(sandbox_ms))),
        quota=QuotaResponse(
            tenant_id=tenant_id,
            calls_limit=max(0, int(calls_limit)),
            sandbox_seconds_limit=max(0, int(sandbox_seconds_limit)),
        ),
        remaining=UsageRemaining(
            calls_remaining=calls_remaining,
            sandbox_seconds_remaining=sandbox_seconds_remaining,
        ),
    )


async def _server_env_response(tenant_id: str, server_name: str) -> ServerEnvResponse:
    doc = await get_tenant_database(tenant_id)["server_secrets"].find_one({"_id": server_name})
    values = doc.get("values", {}) if isinstance(doc, dict) else {}
    keys = sorted(str(key) for key in values.keys()) if isinstance(values, dict) else []
    return ServerEnvResponse(
        tenant_id=tenant_id,
        server=server_name,
        keys=keys,
        updated_at=doc.get("updated_at") if isinstance(doc, dict) else None,
        updated_by=doc.get("updated_by") if isinstance(doc, dict) else None,
    )


def _field_type_summary(sample_docs: list[dict[str, Any]]) -> dict[str, str]:
    summary: dict[str, str] = {}
    for doc in sample_docs:
        if not isinstance(doc, dict):
            continue
        for key, value in doc.items():
            if key in summary:
                continue
            summary[key] = type(value).__name__
    return summary


def _find_snippet(collection: str, filter_doc: dict[str, Any], limit: int) -> str:
    return (
        f"context.db[{json.dumps(collection)}].find("
        f"{json.dumps(filter_doc, indent=2)}, limit={int(limit)})"
    )


def _aggregate_snippet(collection: str, pipeline: list[dict[str, Any]]) -> str:
    return (
        f"context.db[{json.dumps(collection)}].aggregate("
        f"{json.dumps(pipeline, indent=2)})"
    )


async def _bridge_read(
    *,
    tenant_id: str,
    op: str,
    collection: str,
    args: list[Any],
    kwargs: dict[str, Any] | None = None,
) -> Any:
    bridge = SandboxDbBridge(
        tenant_id=tenant_id,
        action_type="read",
        settings=settings,
        max_calls_override=5,
    )
    frame = await bridge.handle(
        {
            "id": f"admin-explore-{op}",
            "op": op,
            "collection": collection,
            "args": args,
            "kwargs": kwargs or {},
        }
    )
    if not frame.get("ok"):
        error = frame.get("error") if isinstance(frame.get("error"), dict) else {}
        message = str(error.get("message") or "Explore query failed.")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)
    return frame.get("result")


def _require_db_bridge_enabled() -> None:
    if settings.sandbox_db_bridge_enabled:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Virtual DB bridge is disabled. Enable SANDBOX_DB_BRIDGE_ENABLED.",
    )


def _assert_can_assign_roles(request: Request, roles: list[str]) -> None:
    """A non-platform-admin may never grant (or keep granting) platform-admin."""
    if settings.platform_admin_role in set(roles) and not _is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform-admin may grant the platform-admin role.",
        )


async def _load_managed_user(request: Request, user_id: str) -> dict[str, Any]:
    """Fetch a user the caller is allowed to read/modify, or raise 403/404.

    Platform-admins span all tenants. A tenant-admin may only touch users inside
    their own tenant, and never a platform-admin account.
    """
    doc = await users_service.get_user_raw(user_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if not _is_platform_admin(request):
        caller_tenant = getattr(request.state, "tenant_id", settings.default_tenant_id)
        if str(doc.get("tenant_id")) != caller_tenant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cross-tenant user management requires platform-admin role.",
            )
        if settings.platform_admin_role in set(doc.get("roles", [])):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only platform-admin may manage a platform-admin user.",
            )
    return doc


def _validate_server_doc(server_doc: dict[str, Any]) -> None:
    transport = server_doc.get("transport")
    endpoint = server_doc.get("endpoint")
    command = server_doc.get("command")
    if transport in {"streamable_http", "sse"} and not endpoint:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"transport={transport} requires endpoint.",
        )
    if transport == "stdio" and not command:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="transport=stdio requires command.",
        )
    if transport == CODE_TRANSPORT and not (server_doc.get("tools") or []):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="transport=code requires at least one authored function in 'tools'.",
        )


async def _prepare_code_server(doc: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    """Lint and encrypt authored functions before a code server is persisted.

    Each tool's ``raw_code`` is statically linted, then encrypted at rest (unless
    it is already an encrypted token carried over from a prior save). Connection
    fields are cleared since a code tool has no downstream endpoint/command.
    """
    if doc.get("transport") != CODE_TRANSPORT:
        return doc
    tools = doc.get("tools") or []
    prepared: list[dict[str, Any]] = []
    for tool in tools:
        tool = dict(tool)
        tool.setdefault("server", doc["server"])
        raw_code = tool.get("raw_code")
        # Already-encrypted source carried over from a prior save was validated at
        # authoring time; re-linting the ciphertext would always fail. Only newly
        # submitted plaintext is linted and then encrypted.
        if isinstance(raw_code, str) and not is_encrypted_token(raw_code):
            try:
                lint_code_tool(tool)
            except CodeToolValidationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(exc),
                ) from exc
            tool["raw_code"] = await encrypt_raw_code(tenant_id, raw_code)
        # An authored function is not a downstream embedding target.
        tool["embedding"] = []
        prepared.append(tool)
    doc["tools"] = prepared
    # Code servers have no network/process target; keep these unset so the proxy
    # never tries to connect to one. Under Queryable Encryption the encrypted
    # routing fields (env/command/args) cannot store a null, so omit them rather
    # than writing null/empty placeholders that libmongocrypt rejects.
    doc["endpoint"] = None
    doc["cwd"] = None
    for connection_field in ("command", "args", "env"):
        doc.pop(connection_field, None)
    return doc


def _to_server_doc(payload: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    server = payload["server"]
    doc = dict(payload)
    doc["tenant_id"] = tenant_id
    doc["_id"] = server
    doc["args"] = [str(arg) for arg in (doc.get("args") or [])]
    doc["env"] = {str(key): str(value) for key, value in (doc.get("env") or {}).items()}
    return doc


async def _apply_server_policy(doc: dict[str, Any], *, is_platform_admin: bool) -> dict[str, Any]:
    try:
        doc = await enforce_server_policy(
            doc,
            is_platform_admin=is_platform_admin,
            settings=settings,
        )
    except (StdioNotAllowed, EndpointNotAllowed) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # Egress allowlist gate (friendly, fail-fast on save). The connect-time
    # transport is the authoritative gate, but rejecting here gives operators a
    # clear 422 instead of a downstream error at first call.
    transport = str(doc.get("transport") or "")
    endpoint = doc.get("endpoint")
    if transport in {"streamable_http", "sse"} and isinstance(endpoint, str) and endpoint:
        tenant_id = str(doc.get("tenant_id") or settings.default_tenant_id)
        tenant_allowlist = await get_tenant_egress_allowlist(tenant_id, settings=settings)
        try:
            await check_endpoint_allowed(
                endpoint,
                tenant_allowlist=tenant_allowlist,
                settings=settings,
            )
        except EgressNotAllowed as exc:
            observe_egress_block("register")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
    return doc


def _redact_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Strip encrypted source from a tool, leaving a presence flag.

    Authored ``raw_code`` is stored encrypted; never echo the ciphertext (or
    plaintext) in list/collection responses. The single-server fetch decrypts it
    explicitly for the author's editor instead.
    """
    public = {key: value for key, value in tool.items() if key not in {"raw_code", "embedding"}}
    if "raw_code" in tool:
        public["has_raw_code"] = bool(tool.get("raw_code"))
    return public


def _public_server_doc(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "tenant_id": doc.get("tenant_id"),
        "origin": doc.get("origin", "platform"),
        "server": doc.get("server"),
        "transport": doc.get("transport"),
        "endpoint": doc.get("endpoint"),
        "command": doc.get("command"),
        "args": doc.get("args", []),
        "env": doc.get("env", {}),
        "cwd": doc.get("cwd"),
        "enabled": bool(doc.get("enabled", True)),
        "metadata": doc.get("metadata", {}),
        "tools": [_redact_tool(tool) for tool in (doc.get("tools") or [])],
    }


async def _public_server_doc_with_code(doc: dict[str, Any]) -> dict[str, Any]:
    """Like :func:`_public_server_doc` but decrypts ``raw_code`` for the editor.

    Used only by the single-server fetch so an author can load their function
    back into the GUI. Decryption is best-effort: an undecryptable token yields
    an empty body rather than an error.
    """
    public = _public_server_doc(doc)
    if doc.get("transport") != CODE_TRANSPORT:
        return public
    tenant_id = str(doc.get("tenant_id") or settings.default_tenant_id)
    decrypted: list[dict[str, Any]] = []
    for stored, redacted in zip(doc.get("tools") or [], public["tools"], strict=False):
        redacted = dict(redacted)
        redacted["raw_code"] = await decrypt_raw_code(tenant_id, stored.get("raw_code"))
        decrypted.append(redacted)
    public["tools"] = decrypted
    return public


async def _resolve_target_tenants_for_cache_migration(
    request: Request,
    *,
    requested_tenant: str | None,
) -> list[str]:
    if requested_tenant:
        return [_resolve_target_tenant(request, requested_tenant)]

    if _is_platform_admin(request):
        docs = await get_control_database()["tenants"].find({}).to_list(length=10_000)
        tenant_ids = sorted(
            {str(doc.get("tenant_id")) for doc in docs if isinstance(doc.get("tenant_id"), str)}
        )
        return tenant_ids or [settings.default_tenant_id]

    return [getattr(request.state, "tenant_id", settings.default_tenant_id)]


def _tenant_response(doc: dict[str, Any], *, db_name: str | None = None) -> TenantResponse:
    status_value = str(doc.get("status", STATUS_ACTIVE)) or STATUS_ACTIVE
    reason = str(doc.get("suspended_reason", "")) or None
    return TenantResponse(
        tenant_id=str(doc.get("tenant_id")),
        db_name=str(doc.get("db_name") or db_name or ""),
        status=status_value,
        suspended_reason=reason if status_value == STATUS_SUSPENDED else None,
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


@router.post("/tenants", response_model=TenantResponse)
async def create_tenant(request: Request, payload: TenantCreateRequest) -> TenantResponse:
    tenant_id = _resolve_target_tenant(request, payload.tenant_id)
    db_name = await provision_tenant(tenant_id, wait_for_queryable_indexes=False)
    doc = await get_control_database()["tenants"].find_one({"tenant_id": tenant_id})
    if not doc:
        return TenantResponse(tenant_id=tenant_id, db_name=db_name)
    return _tenant_response(doc, db_name=db_name)


@router.get("/tenants", response_model=list[TenantResponse])
async def list_tenants(request: Request) -> list[TenantResponse]:
    control_db = get_control_database()
    if _is_platform_admin(request):
        docs = await control_db["tenants"].find({}).to_list(length=10_000)
    else:
        tenant_id = getattr(request.state, "tenant_id", settings.default_tenant_id)
        doc = await control_db["tenants"].find_one({"tenant_id": tenant_id})
        docs = [doc] if doc else []
    return [_tenant_response(doc) for doc in docs if doc]


@router.delete("/tenants/{tenant_id}", response_model=TenantDeleteResponse)
async def delete_tenant(request: Request, tenant_id: str) -> TenantDeleteResponse:
    _require_platform_admin(request)
    deleted = await deprovision_tenant(tenant_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    actor = str(getattr(request.state, "user_id", "admin"))
    get_telemetry_logger().log_background(
        tenant_id=tenant_id,
        user_id=actor,
        method="admin/tenants/delete",
        status="tenant_deprovisioned",
        metadata={"tenant_id": tenant_id, "actor": actor},
    )
    return TenantDeleteResponse(tenant_id=tenant_id, db_name=tenant_db_name(tenant_id), deleted=True)


@router.post("/tenants/{tenant_id}/suspend", response_model=TenantResponse)
async def suspend_tenant(
    request: Request,
    tenant_id: str,
    payload: TenantStatusUpdateRequest | None = None,
) -> TenantResponse:
    return await _set_tenant_status_endpoint(
        request=request,
        tenant_id=tenant_id,
        target_status=STATUS_SUSPENDED,
        reason=payload.reason if payload else None,
    )


@router.post("/tenants/{tenant_id}/resume", response_model=TenantResponse)
async def resume_tenant(request: Request, tenant_id: str) -> TenantResponse:
    return await _set_tenant_status_endpoint(
        request=request,
        tenant_id=tenant_id,
        target_status=STATUS_ACTIVE,
        reason=None,
    )


async def _set_tenant_status_endpoint(
    *,
    request: Request,
    tenant_id: str,
    target_status: str,
    reason: str | None,
) -> TenantResponse:
    # Suspension is a platform abuse-control lever, so it is platform-admin only
    # (a tenant-admin cannot suspend or un-suspend their own tenant).
    _require_platform_admin(request)
    actor = str(getattr(request.state, "user_id", "admin"))
    doc = await set_tenant_status(
        tenant_id,
        target_status,
        updated_by=actor,
        reason=reason,
    )
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    get_telemetry_logger().log_background(
        tenant_id=tenant_id,
        user_id=actor,
        method=f"admin/tenants/{target_status}",
        status="tenant_suspended" if target_status == STATUS_SUSPENDED else "tenant_resumed",
        metadata={"tenant_id": tenant_id, "actor": actor, "reason": reason},
    )
    return _tenant_response(doc)


def _egress_allowlist_response(
    tenant_id: str, doc: dict[str, Any] | None
) -> EgressAllowlistResponse:
    entries = (doc or {}).get("egress_allowlist")
    allowlist = (
        [str(item) for item in entries if isinstance(item, str)]
        if isinstance(entries, list)
        else []
    )
    return EgressAllowlistResponse(
        tenant_id=tenant_id,
        allowlist=allowlist,
        global_allowlist=parse_allowlist(settings.egress_global_allowlist),
        enforced=bool(settings.egress_allowlist_enabled),
        default_deny=bool(settings.egress_default_deny),
        updated_at=(doc or {}).get("egress_allowlist_updated_at"),
        updated_by=(doc or {}).get("egress_allowlist_updated_by"),
    )


@router.get(
    "/tenants/{tenant_id}/egress-allowlist",
    response_model=EgressAllowlistResponse,
)
async def get_egress_allowlist(request: Request, tenant_id: str) -> EgressAllowlistResponse:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    doc = await get_control_database()["tenants"].find_one({"tenant_id": target_tenant})
    return _egress_allowlist_response(target_tenant, doc)


@router.put(
    "/tenants/{tenant_id}/egress-allowlist",
    response_model=EgressAllowlistResponse,
)
async def put_egress_allowlist(
    request: Request,
    tenant_id: str,
    payload: EgressAllowlistUpdateRequest,
) -> EgressAllowlistResponse:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    actor = str(getattr(request.state, "user_id", "admin"))
    try:
        doc = await set_tenant_egress_allowlist(
            target_tenant,
            payload.allowlist,
            updated_by=actor,
        )
    except EgressNotAllowed as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    get_telemetry_logger().log_background(
        tenant_id=target_tenant,
        user_id=actor,
        method="admin/tenants/egress-allowlist",
        status="egress_allowlist_updated",
        metadata={
            "tenant_id": target_tenant,
            "actor": actor,
            "entries": len(payload.allowlist),
        },
    )
    return _egress_allowlist_response(target_tenant, doc)


@router.get(
    "/servers/{server_name}/env",
    response_model=ServerEnvResponse,
)
async def get_server_env(
    request: Request,
    server_name: str,
    tenant_id: str | None = Query(default=None),
) -> ServerEnvResponse:
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(
        request, tenant_id if isinstance(tenant_id, str) else None
    )
    await _require_server_exists(target_tenant, server_name)
    return await _server_env_response(target_tenant, server_name)


@router.put(
    "/servers/{server_name}/env",
    response_model=ServerEnvResponse,
)
async def put_server_env(
    request: Request,
    server_name: str,
    payload: ServerEnvUpdateRequest,
    tenant_id: str | None = Query(default=None),
) -> ServerEnvResponse:
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(
        request, tenant_id if isinstance(tenant_id, str) else None
    )
    await _require_server_exists(target_tenant, server_name)
    collection = get_tenant_database(target_tenant)["server_secrets"]
    existing = await collection.find_one({"_id": server_name})
    current = existing.get("values", {}) if isinstance(existing, dict) else {}
    values = dict(current) if isinstance(current, dict) else {}

    for key, raw_value in payload.values.items():
        secret_key = _validate_secret_key(str(key))
        if not isinstance(raw_value, str):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Secret '{secret_key}' must be a string.",
            )
        if raw_value == "":
            values.pop(secret_key, None)
            continue
        encrypted = await encrypt_raw_code(target_tenant, raw_value)
        if not encrypted:
            values.pop(secret_key, None)
            continue
        values[secret_key] = encrypted

    if values:
        now = datetime.now(UTC)
        updated_by = getattr(request.state, "user_id", "admin")
        await collection.replace_one(
            {"_id": server_name},
            {
                "_id": server_name,
                "tenant_id": target_tenant,
                "server": server_name,
                "values": values,
                "updated_at": now,
                "updated_by": str(updated_by),
            },
            upsert=True,
        )
    else:
        await collection.delete_one({"_id": server_name})
    return await _server_env_response(target_tenant, server_name)


@router.get("/tenants/{tenant_id}/usage", response_model=UsageResponse)
async def get_tenant_usage(request: Request, tenant_id: str) -> UsageResponse:
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    usage = await get_usage(target_tenant)
    quota = await get_effective_quota(target_tenant)
    return _usage_response(
        tenant_id=target_tenant,
        period=str(usage.get("period", "")),
        calls=int(usage.get("calls", 0)),
        sandbox_ms=int(usage.get("sandbox_ms", 0)),
        calls_limit=int(quota.get("calls_limit", 0)),
        sandbox_seconds_limit=int(quota.get("sandbox_seconds_limit", 0)),
    )


@router.get("/tenants/{tenant_id}/usage/events", response_model=UsageEventsResponse)
async def get_tenant_usage_events(
    request: Request,
    tenant_id: str,
    period: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> UsageEventsResponse:
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    summary = await summarize_billing_events(target_tenant, period=period, limit=limit)
    return UsageEventsResponse(**summary)


@router.put("/tenants/{tenant_id}/quota", response_model=QuotaResponse)
async def update_tenant_quota(
    request: Request,
    tenant_id: str,
    payload: QuotaUpdateRequest,
) -> QuotaResponse:
    _require_platform_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    updated = await set_quota(
        target_tenant,
        calls_limit=payload.calls_limit,
        sandbox_seconds_limit=payload.sandbox_seconds_limit,
        updated_by=str(getattr(request.state, "user_id", "admin")),
    )
    return QuotaResponse(
        tenant_id=target_tenant,
        calls_limit=int(updated.get("calls_limit", 0)),
        sandbox_seconds_limit=int(updated.get("sandbox_seconds_limit", 0)),
    )


@router.get("/actions", response_model=PendingActionListResponse)
async def list_actions(
    request: Request,
    tenant_id: str | None = Query(default=None),
    action_status: str = Query(default="pending", alias="status"),
) -> PendingActionListResponse:
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    docs = await list_pending_actions(tenant_id=target_tenant, status=action_status)
    return PendingActionListResponse(
        tenant_id=target_tenant,
        items=[_pending_action_response(doc) for doc in docs],
    )


async def _decide_action(
    *,
    request: Request,
    action_id: str,
    tenant_id: str | None,
    decision: str,
) -> PendingActionResponse:
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    approver_id = str(getattr(request.state, "user_id", "admin"))
    approver_roles = [str(role) for role in getattr(request.state, "roles", [])]
    decide_fn = approve_action if decision == "approve" else reject_action
    outcome, action_doc = await decide_fn(
        tenant_id=target_tenant,
        action_id=action_id,
        approver_id=approver_id,
        approver_roles=approver_roles,
    )
    if action_doc is None and outcome == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Pending action not found."
        )
    if outcome == "self_approval_forbidden":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requesters may not approve or reject their own actions.",
        )
    if outcome in {"not_pending", "expired"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pending action can no longer be decided.",
        )
    if action_doc is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update pending action.",
        )
    get_telemetry_logger().log_background(
        tenant_id=target_tenant,
        user_id=approver_id,
        method=f"admin/actions/{decision}",
        status="action_approved" if decision == "approve" else "action_rejected",
        metadata={
            "action_id": action_id,
            "server": action_doc.get("server"),
            "tool": action_doc.get("tool"),
            "requester": action_doc.get("user_id"),
            "approver": approver_id,
        },
    )
    return _pending_action_response(action_doc)


@router.post("/actions/{action_id}/approve", response_model=PendingActionResponse)
async def approve_pending_action(
    request: Request,
    action_id: str,
    tenant_id: str | None = Query(default=None),
) -> PendingActionResponse:
    return await _decide_action(
        request=request,
        action_id=action_id,
        tenant_id=tenant_id,
        decision="approve",
    )


@router.post("/actions/{action_id}/reject", response_model=PendingActionResponse)
async def reject_pending_action(
    request: Request,
    action_id: str,
    tenant_id: str | None = Query(default=None),
) -> PendingActionResponse:
    return await _decide_action(
        request=request,
        action_id=action_id,
        tenant_id=tenant_id,
        decision="reject",
    )


@router.post("/servers")
async def create_or_update_server(request: Request, payload: ServerUpsertRequest) -> dict[str, Any]:
    tenant_id = _resolve_target_tenant(request, payload.tenant_id)
    await provision_tenant(tenant_id, wait_for_queryable_indexes=False)
    doc = _to_server_doc(payload.model_dump(), tenant_id)
    _validate_server_doc(doc)
    doc = await _apply_server_policy(doc, is_platform_admin=_is_platform_admin(request))
    doc = await _prepare_code_server(doc, tenant_id)
    collection = get_tenant_database(tenant_id)["routing_registry"]
    await collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    if doc.get("enabled", True):
        await get_proxy_registry().mount_or_update(doc)
    else:
        await get_proxy_registry().unmount(doc["server"], tenant_id=tenant_id)
    return _public_server_doc(doc)


@router.get("/servers")
async def list_servers(
    request: Request,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    docs = (
        await get_tenant_database(target_tenant)["routing_registry"].find({}).to_list(length=10_000)
    )
    docs.sort(key=lambda item: str(item.get("server", "")))
    return {
        "tenant_id": target_tenant,
        "items": [_public_server_doc(doc) for doc in docs],
    }


@router.get("/servers/{server_name}")
async def get_server(
    request: Request,
    server_name: str,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    doc = await get_tenant_database(target_tenant)["routing_registry"].find_one(
        {"_id": server_name}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Server not found.")
    return await _public_server_doc_with_code(doc)


@router.get("/servers/{server_name}/export")
async def export_server(
    request: Request,
    server_name: str,
    tenant_id: str | None = Query(default=None),
) -> Response:
    """Download a code server as a runnable, self-contained FastMCP project.

    Bundles every tool on the server plus the transitive closure of sibling
    code tools they call via ``context.tools``/``context.call`` so cross-tool
    composition keeps working in-process. Secrets are never included — only the
    names of ``context.env`` keys are emitted into ``.env.example``.
    """
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    try:
        export = await build_server_export(tenant_id=target_tenant, server_name=server_name)
    except ServerExportNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ServerExportError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return Response(
        content=export.content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "X-Export-Tool-Count": str(export.tool_count),
            "X-Export-Servers": ",".join(export.bundled_servers),
        },
    )


@router.get("/explore/collections", response_model=ExploreCollectionsResponse)
async def explore_collections(
    request: Request,
    tenant_id: str | None = Query(default=None),
) -> ExploreCollectionsResponse:
    _require_tenant_admin(request)
    _require_db_bridge_enabled()
    target_tenant = _resolve_target_tenant(request, tenant_id if isinstance(tenant_id, str) else None)
    names = await get_tenant_database(target_tenant).list_collection_names()
    collections = sorted(
        name
        for name in names
        if isinstance(name, str) and name and not name.startswith("system.")
    )
    return ExploreCollectionsResponse(tenant_id=target_tenant, collections=collections)


@router.post("/explore/sample", response_model=ExploreSampleResponse)
async def explore_sample(
    request: Request,
    payload: ExploreSampleRequest,
) -> ExploreSampleResponse:
    _require_tenant_admin(request)
    _require_db_bridge_enabled()
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    limit = max(1, min(int(payload.limit), int(settings.sandbox_db_max_docs)))
    raw = await _bridge_read(
        tenant_id=target_tenant,
        op="find",
        collection=payload.collection,
        args=[{}],
        kwargs={"limit": limit},
    )
    sample_docs = [doc for doc in (raw or []) if isinstance(doc, dict)]
    return ExploreSampleResponse(
        tenant_id=target_tenant,
        collection=payload.collection,
        limit=limit,
        field_types=_field_type_summary(sample_docs),
        sample_docs=sample_docs,
        snippet=_find_snippet(payload.collection, {}, limit),
    )


@router.post("/explore/query", response_model=ExploreQueryResponse)
async def explore_query(
    request: Request,
    payload: ExploreQueryRequest,
) -> ExploreQueryResponse:
    _require_tenant_admin(request)
    _require_db_bridge_enabled()
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    limit = max(1, min(int(payload.limit), int(settings.sandbox_db_max_docs)))
    if payload.mode == "aggregate":
        pipeline = [stage for stage in payload.pipeline if isinstance(stage, dict)]
        raw = await _bridge_read(
            tenant_id=target_tenant,
            op="aggregate",
            collection=payload.collection,
            args=[pipeline],
        )
        snippet = _aggregate_snippet(payload.collection, pipeline or [{"$limit": limit}])
    else:
        filter_doc = payload.filter if isinstance(payload.filter, dict) else {}
        raw = await _bridge_read(
            tenant_id=target_tenant,
            op="find",
            collection=payload.collection,
            args=[filter_doc],
            kwargs={"limit": limit},
        )
        snippet = _find_snippet(payload.collection, filter_doc, limit)

    results = [doc for doc in (raw or []) if isinstance(doc, dict)]
    return ExploreQueryResponse(
        tenant_id=target_tenant,
        collection=payload.collection,
        mode=payload.mode,
        limit=limit,
        results=results,
        snippet=snippet,
    )


@router.post("/code-tools/validate", response_model=CodeToolValidateResponse)
async def validate_code_tool_endpoint(
    request: Request, payload: CodeToolValidateRequest
) -> CodeToolValidateResponse:
    """Lint authored Python without executing it.

    Returns the exact set of issues the save path enforces (same
    :func:`validate_code_tool`), so the admin UI can block a broken save before it
    is attempted and surface line-accurate problems while the author types. Pure
    and cheap: no DB access, no sandbox spawn.
    """
    _require_tenant_admin(request)
    issues = validate_code_tool(
        {
            "name": payload.name,
            "raw_code": payload.raw_code,
            "requirements": [str(req).strip() for req in (payload.requirements or []) if str(req).strip()],
            "metadata": {"action_type": payload.action_type},
            "input_schema": payload.input_schema,
        }
    )
    ok = not any(issue["severity"] == "error" for issue in issues)
    typed_issues = [CodeToolValidationIssue(**issue) for issue in issues]
    return CodeToolValidateResponse(
        ok=ok,
        issues=typed_issues,
        suggested_schema=suggest_input_schema(payload.raw_code, payload.name),
    )


@router.post(
    "/servers/{server_name}/tools/{tool_name}/test",
    response_model=CodeToolTestResponse,
)
async def test_code_tool(
    request: Request,
    server_name: str,
    tool_name: str,
    payload: CodeToolTestRequest,
) -> CodeToolTestResponse:
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    candidate_tool = {
        "server": server_name,
        "name": tool_name,
        "description": "sandbox test run",
        "raw_code": payload.raw_code,
        "requirements": [str(req).strip() for req in (payload.requirements or []) if str(req).strip()],
        "metadata": {
            "action_type": payload.action_type,
            "requires_confirmation": bool(payload.requires_confirmation),
        },
        "input_schema": {},
        "scopes": [],
    }
    try:
        lint_code_tool(candidate_tool)
    except CodeToolValidationError as exc:
        return CodeToolTestResponse(ok=False, error=str(exc))

    timeout_seconds = settings.sandbox_wall_timeout_ms / 1000
    executor = get_executor()
    # Let the workbench "Run" exercise context.tools just like production: an
    # admin caller can reach sibling code tools, still re-authorized + restricted
    # to code servers + no confirmation-gated tools by the shared invoker.
    test_caller = CallerIdentity(
        user_id=str(getattr(request.state, "user_id", "") or "admin-test"),
        scopes=["server:*"],
        roles=["admin"],
    )
    tool_invoker = get_proxy_registry().make_tool_invoker(
        tenant_id=target_tenant,
        caller=test_caller,
        call_depth=0,
    )
    try:
        result = await asyncio.wait_for(
            executor.run(
                ExecRequest(
                    tenant_id=target_tenant,
                    server=server_name,
                    tool=tool_name,
                    raw_code=payload.raw_code,
                    requirements=list(candidate_tool["requirements"]),
                    arguments=payload.arguments if isinstance(payload.arguments, dict) else {},
                    env={},
                    action_type=payload.action_type,
                    tool_invoker=tool_invoker,
                )
            ),
            timeout=timeout_seconds,
        )
        return CodeToolTestResponse(ok=True, result=result.payload, elapsed_ms=result.elapsed_ms)
    except TimeoutError:
        return CodeToolTestResponse(
            ok=False,
            error=(
                f"Sandbox test exceeded {settings.sandbox_wall_timeout_ms}ms timeout. "
                "Optimize your function or inputs."
            ),
        )
    except SandboxTimeoutError as exc:
        return CodeToolTestResponse(ok=False, error=str(exc))
    except (SandboxProtocolError, SandboxError, ValueError) as exc:
        return CodeToolTestResponse(ok=False, error=str(exc))


@router.patch("/servers/{server_name}")
async def patch_server(
    request: Request,
    server_name: str,
    payload: ServerPatchRequest,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    requested_tenant = payload.tenant_id or tenant_id
    target_tenant = _resolve_target_tenant(request, requested_tenant)
    collection = get_tenant_database(target_tenant)["routing_registry"]
    existing = await collection.find_one({"_id": server_name})
    if not existing:
        raise HTTPException(status_code=404, detail="Server not found.")

    updates = payload.model_dump(exclude_none=True)
    updates.pop("tenant_id", None)
    merged = dict(existing)
    merged.update(updates)
    merged["server"] = server_name
    merged["_id"] = server_name
    merged["tenant_id"] = target_tenant
    merged["args"] = [str(arg) for arg in (merged.get("args") or [])]
    merged["env"] = {str(key): str(value) for key, value in (merged.get("env") or {}).items()}
    merged["origin"] = existing.get("origin", "platform")

    _validate_server_doc(merged)
    is_platform_admin = _is_platform_admin(request)
    if merged["origin"] == "platform" and not is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform-admin may modify platform-origin servers.",
        )
    merged = await _apply_server_policy(merged, is_platform_admin=is_platform_admin)
    merged = await _prepare_code_server(merged, target_tenant)
    await collection.replace_one({"_id": server_name}, merged, upsert=True)
    if merged.get("enabled", True):
        await get_proxy_registry().mount_or_update(merged)
    else:
        await get_proxy_registry().unmount(server_name, tenant_id=target_tenant)
    return _public_server_doc(merged)


@router.delete("/servers/{server_name}")
async def delete_server(
    request: Request,
    server_name: str,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    collection = get_tenant_database(target_tenant)["routing_registry"]
    result = await collection.delete_many({"_id": server_name})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Server not found.")
    await get_proxy_registry().unmount(server_name, tenant_id=target_tenant)
    return {"deleted": True, "tenant_id": target_tenant, "server": server_name}


@router.post("/cache/migrate")
async def migrate_cache(request: Request, payload: CacheMigrateRequest) -> dict[str, Any]:
    tenant_ids = await _resolve_target_tenants_for_cache_migration(
        request,
        requested_tenant=payload.tenant_id,
    )
    for tenant_id in tenant_ids:
        await provision_tenant(tenant_id, wait_for_queryable_indexes=False)
    return await cache_migration_service.migrate(
        tenant_ids=tenant_ids,
        mode=payload.mode,
        batch_size=payload.batch_size,
    )


@router.get("/whoami", response_model=WhoAmIResponse)
async def who_am_i(request: Request) -> WhoAmIResponse:
    roles = list(getattr(request.state, "roles", []))
    scopes = list(getattr(request.state, "scopes", []))
    tenant_id = str(getattr(request.state, "tenant_id", settings.default_tenant_id))
    user_id = str(getattr(request.state, "user_id", "anonymous"))
    return WhoAmIResponse(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=roles,
        scopes=scopes,
        is_platform_admin=settings.platform_admin_role in set(roles),
        auth_mode=settings.auth_mode,
    )


@router.post("/users", response_model=UserResponse)
async def create_user(request: Request, payload: UserCreateRequest) -> UserResponse:
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    _assert_can_assign_roles(request, payload.roles)
    try:
        user = await users_service.create_user(
            email=payload.email,
            password=payload.password,
            tenant_id=target_tenant,
            roles=payload.roles,
            scopes=payload.scopes,
            status=payload.status,
            created_by=str(getattr(request.state, "user_id", "")) or None,
        )
    except users_service.UserAlreadyExists as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except users_service.UserError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    await users_service.sync_session_context(user)
    return UserResponse(**user)


@router.get("/users", response_model=UserListResponse)
async def list_users(
    request: Request,
    tenant_id: str | None = Query(default=None),
) -> UserListResponse:
    if _is_platform_admin(request) and not tenant_id:
        users = await users_service.list_users()
        return UserListResponse(tenant_id=None, items=[UserResponse(**u) for u in users])
    target_tenant = _resolve_target_tenant(request, tenant_id)
    users = await users_service.list_users(tenant_id=target_tenant)
    return UserListResponse(tenant_id=target_tenant, items=[UserResponse(**u) for u in users])


@router.post("/users/me/password")
async def change_my_password(request: Request, payload: PasswordChangeRequest) -> dict[str, Any]:
    caller_email = str(getattr(request.state, "user_id", ""))
    doc = await users_service.find_user_by_email(caller_email)
    if doc is None:
        # The env bootstrap admin has no DB record; its password is environment-managed.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password self-service is unavailable for the bootstrap admin.",
        )
    if not verify_password(payload.current_password, str(doc.get("password_hash", ""))):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current password is incorrect.",
        )
    user = await users_service.update_user(str(doc["_id"]), password=payload.new_password)
    await users_service.sync_session_context(user)
    return {"updated": True}


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(request: Request, user_id: str) -> UserResponse:
    doc = await _load_managed_user(request, user_id)
    return UserResponse(**users_service.public_user(doc))


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    request: Request,
    user_id: str,
    payload: UserUpdateRequest,
) -> UserResponse:
    await _load_managed_user(request, user_id)
    if payload.roles is not None:
        _assert_can_assign_roles(request, payload.roles)
    try:
        user = await users_service.update_user(
            user_id,
            password=payload.password,
            roles=payload.roles,
            scopes=payload.scopes,
            status=payload.status,
        )
    except users_service.UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await users_service.sync_session_context(user)
    return UserResponse(**user)


@router.delete("/users/{user_id}")
async def delete_user(request: Request, user_id: str) -> dict[str, Any]:
    doc = await _load_managed_user(request, user_id)
    caller_email = str(getattr(request.state, "user_id", ""))
    if str(doc.get("email", "")) == caller_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account.",
        )
    await users_service.delete_user(user_id)
    return {"deleted": True, "id": user_id}


@router.get("/catalog", response_model=CatalogListResponse)
async def list_catalog(
    request: Request,
    tenant_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> CatalogListResponse:
    params = CatalogListRequest(tenant_id=tenant_id, limit=limit, offset=offset)
    target_tenant = _resolve_target_tenant(request, params.tenant_id)
    docs = await get_tenant_database(target_tenant)["tool_catalog"].find({}).to_list(length=10_000)
    docs.sort(key=lambda item: (str(item.get("server", "")), str(item.get("name", ""))))
    window = docs[offset : offset + params.limit]
    items = [
        CatalogItemResponse(
            server=str(doc.get("server", "")),
            name=str(doc.get("name", "")),
            description=str(doc.get("description", "")),
            scopes=[scope for scope in doc.get("scopes", []) if isinstance(scope, str)],
            input_schema=doc.get("input_schema")
            if isinstance(doc.get("input_schema"), dict)
            else {},
            metadata=doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {},
            transport=str((doc.get("metadata") or {}).get("transport", "")) or None,
            action_type=str((doc.get("metadata") or {}).get("action_type", "")) or None,
            updated_at=doc.get("updated_at"),
        )
        for doc in window
    ]
    return CatalogListResponse(
        tenant_id=target_tenant,
        items=items,
        total=len(docs),
        limit=params.limit,
        offset=params.offset,
    )


@router.get("/telemetry", response_model=TelemetryListResponse)
async def list_telemetry(
    request: Request,
    tenant_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> TelemetryListResponse:
    params = TelemetryListRequest(tenant_id=tenant_id, limit=limit)
    target_tenant = _resolve_target_tenant(request, params.tenant_id)
    docs = (
        await get_tenant_database(target_tenant)["audit_telemetry"].find({}).to_list(length=10_000)
    )
    docs.sort(
        key=lambda item: item.get("timestamp")
        if isinstance(item.get("timestamp"), datetime)
        else datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    items = [
        TelemetryEventResponse(
            timestamp=doc.get("timestamp"),
            tenant_id=str(doc.get("tenant_id", target_tenant)),
            user_id=str(doc.get("user_id", "unknown-user")),
            request_id=doc.get("request_id"),
            method=str(doc.get("method", "")),
            status=str(doc.get("status", "")),
            latency_ms=doc.get("latency_ms"),
            metadata=doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {},
        )
        for doc in docs[: params.limit]
    ]
    return TelemetryListResponse(tenant_id=target_tenant, items=items)


@router.get("/stats", response_model=StatsResponse)
async def admin_stats(request: Request) -> StatsResponse:
    roles = set(getattr(request.state, "roles", []))
    is_platform_admin = settings.platform_admin_role in roles
    if is_platform_admin:
        tenant_docs = await get_control_database()["tenants"].find({}).to_list(length=10_000)
        tenant_ids = sorted(
            {
                str(doc.get("tenant_id"))
                for doc in tenant_docs
                if isinstance(doc.get("tenant_id"), str)
            }
        )
    else:
        tenant_ids = [str(getattr(request.state, "tenant_id", settings.default_tenant_id))]
    if not tenant_ids:
        tenant_ids = [settings.default_tenant_id]

    tenant_rows: list[TenantStats] = []
    status_counts: dict[str, int] = {}
    for tenant_id in tenant_ids:
        tenant_db = get_tenant_database(tenant_id)
        server_docs = await tenant_db["routing_registry"].find({}).to_list(length=10_000)
        tool_docs = await tenant_db["tool_catalog"].find({}).to_list(length=10_000)
        telemetry_docs = await tenant_db["audit_telemetry"].find({}).to_list(length=10_000)
        for doc in telemetry_docs:
            status = str(doc.get("status", "unknown"))
            status_counts[status] = status_counts.get(status, 0) + 1
        tenant_rows.append(
            TenantStats(
                tenant_id=tenant_id,
                server_count=len(server_docs),
                enabled_server_count=sum(
                    1 for doc in server_docs if bool(doc.get("enabled", True))
                ),
                tool_count=len(tool_docs),
            )
        )
    tenant_rows.sort(key=lambda row: row.tenant_id)
    return StatsResponse(
        tenant_count=len(tenant_ids) if is_platform_admin else None,
        catalog_version=get_catalog_version(),
        telemetry_status_counts=status_counts,
        tenants=tenant_rows,
    )


@router.post("/search")
async def admin_search(request: Request, payload: AdminSearchRequest) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    items = await hybrid_search_service.search_tools(
        tenant_id=target_tenant,
        query=payload.query,
        limit=payload.limit,
        vector_weight=payload.vector_weight,
        text_weight=payload.text_weight,
        mode=payload.mode,
        server=payload.server,
    )
    return {"tenant_id": target_tenant, "mode": payload.mode, "server": payload.server, "items": items}


def _merge_embedding_config(
    current: EmbeddingConfig,
    payload: EmbeddingConfigUpdateRequest | EmbeddingTestRequest,
) -> EmbeddingConfig:
    """Overlay a request onto the current config without losing the stored key.

    - A ``None`` model on a provider switch resolves the new provider's default.
    - ``api_key=None`` keeps the existing key; ``""`` clears it; otherwise replace.
    """
    if payload.model is not None:
        model = payload.model
    elif payload.provider != current.provider:
        model = default_model_for(payload.provider, settings)
    else:
        model = current.model

    api_key = current.api_key if payload.api_key is None else payload.api_key

    base_url: str | None
    if payload.base_url is not None:
        base_url = payload.base_url
    elif payload.provider == current.provider:
        base_url = current.base_url
    else:
        base_url = None

    return EmbeddingConfig(
        provider=payload.provider,
        model=model,
        base_url=base_url,
        dimensions=0,
        api_key=api_key,
        azure_endpoint=(
            payload.azure_endpoint if payload.azure_endpoint is not None else current.azure_endpoint
        ),
        azure_api_version=payload.azure_api_version or current.azure_api_version,
        azure_deployment=(
            payload.azure_deployment
            if payload.azure_deployment is not None
            else current.azure_deployment
        ),
    )


async def _validate_and_detect_dimensions(candidate: EmbeddingConfig) -> EmbeddingConfig:
    try:
        validate_config(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Validate the provider for real and detect the actual vector width by probing
    # it. This both rejects an unusable config before anything is persisted and
    # guarantees the stored dimension always equals the provider's real output
    # length (so Atlas vector indexes can never drift out of sync).
    service = build_provider_service(candidate, settings)
    try:
        detected = await service.detect_dimensions()
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Embedding provider validation failed: {exc}",
        ) from exc
    return replace(candidate, dimensions=detected)


def _secret_encryption_label(config: EmbeddingConfig) -> str | None:
    """Describe how this config's API key is protected at rest.

    Tenant overrides use a per-tenant Queryable Encryption DEK when QE is on;
    everything else (platform default, QE disabled) uses the shared Fernet seam.
    """
    if not config.has_api_key:
        return None
    if config.source == "tenant-db" and settings.qe_enabled:
        return "per-tenant-dek"
    return "shared-fernet"


def _embedding_config_response(
    config: EmbeddingConfig,
    reprovision: dict[str, Any] | None = None,
) -> EmbeddingConfigResponse:
    service = build_provider_service(config, settings)
    return EmbeddingConfigResponse(
        provider=config.provider,  # type: ignore[arg-type]
        model=config.model or (config.azure_deployment or ""),
        base_url=config.base_url,
        dimensions=config.dimensions,
        embedding_version=embedding_version_for(service),
        api_key_set=config.has_api_key,
        api_key_hint=config.api_key_hint,
        azure_endpoint=config.azure_endpoint,
        azure_api_version=config.azure_api_version,
        azure_deployment=config.azure_deployment,
        supported_providers=list(SUPPORTED_PROVIDERS),
        source=config.source,
        secret_encryption=_secret_encryption_label(config),
        updated_at=config.updated_at,
        updated_by=config.updated_by,
        reprovision=reprovision or {},
    )


@router.get("/embedding", response_model=EmbeddingConfigResponse)
async def get_embedding_config(request: Request) -> EmbeddingConfigResponse:
    _require_platform_admin(request)
    config = await load_persisted_config(settings)
    reprovision = await get_reprovision_status()
    return _embedding_config_response(config, reprovision)


@router.put("/embedding", response_model=EmbeddingConfigResponse)
async def update_embedding_config(
    request: Request,
    payload: EmbeddingConfigUpdateRequest,
) -> EmbeddingConfigResponse:
    _require_platform_admin(request)
    user_id = str(getattr(request.state, "user_id", "admin"))

    # Refuse to mutate the active embedding config while a reprovision is running:
    # the in-flight job reads the active provider, so swapping it mid-run would
    # produce an inconsistent vector space across tenants.
    if await is_reprovision_running():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An embedding reprovision is in progress; retry once it completes.",
        )

    current = await load_persisted_config(settings)
    candidate = _merge_embedding_config(current, payload)
    candidate = await _validate_and_detect_dimensions(candidate)

    await save_persisted_config(candidate, settings, updated_by=user_id)
    refreshed = await refresh_active_embedding_config(settings)

    reprovision: dict[str, Any] = {}
    if payload.reprovision:
        try:
            reprovision = await trigger_reprovision(started_by=user_id)
        except ReprovisionInProgressError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return _embedding_config_response(refreshed, reprovision)


@router.post("/embedding/test", response_model=EmbeddingTestResponse)
async def test_embedding_config(
    request: Request,
    payload: EmbeddingTestRequest,
) -> EmbeddingTestResponse:
    _require_platform_admin(request)
    current = await load_persisted_config(settings)
    candidate = _merge_embedding_config(current, payload)
    try:
        validate_config(candidate)
        service = build_provider_service(candidate, settings)
        dims = await service.detect_dimensions()
        version = f"{service.model_id}:{dims}"
    except Exception as exc:
        return EmbeddingTestResponse(
            ok=False,
            provider=candidate.provider,
            model=candidate.model or (candidate.azure_deployment or ""),
            message=str(exc),
        )
    return EmbeddingTestResponse(
        ok=True,
        provider=candidate.provider,
        model=service.model_id,
        dimensions=dims,
        embedding_version=version,
        message="Embedding provider reachable.",
    )


@router.get("/tenants/{tenant_id}/embedding", response_model=EmbeddingConfigResponse)
async def get_tenant_embedding_config(
    request: Request,
    tenant_id: str,
) -> EmbeddingConfigResponse:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    config = await load_tenant_config(target_tenant, settings)
    reprovision = await get_tenant_reprovision_status(target_tenant)
    return _embedding_config_response(config, reprovision)


@router.put("/tenants/{tenant_id}/embedding", response_model=EmbeddingConfigResponse)
async def update_tenant_embedding_config(
    request: Request,
    tenant_id: str,
    payload: EmbeddingConfigUpdateRequest,
) -> EmbeddingConfigResponse:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    user_id = str(getattr(request.state, "user_id", "admin"))
    if await is_tenant_reprovision_running(target_tenant):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An embedding reprovision is in progress for tenant '{target_tenant}'.",
        )

    await provision_tenant(target_tenant, wait_for_queryable_indexes=False)
    current = await load_tenant_config(target_tenant, settings)
    candidate = await _validate_and_detect_dimensions(_merge_embedding_config(current, payload))

    await save_tenant_config(target_tenant, candidate, settings, updated_by=user_id)
    refreshed = await load_tenant_config(target_tenant, settings)
    reprovision: dict[str, Any] = {}
    if payload.reprovision:
        try:
            reprovision = await trigger_tenant_reprovision(
                tenant_id=target_tenant,
                started_by=user_id,
            )
        except ReprovisionInProgressError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _embedding_config_response(refreshed, reprovision)


@router.delete("/tenants/{tenant_id}/embedding", response_model=EmbeddingConfigResponse)
async def reset_tenant_embedding_config(
    request: Request,
    tenant_id: str,
    reprovision: bool = False,
) -> EmbeddingConfigResponse:
    """Remove a tenant's override so it inherits the platform default again.

    Optionally re-embeds the tenant (``?reprovision=true``) so its stored vectors
    realign with the inherited platform provider.
    """
    target_tenant = _resolve_target_tenant(request, tenant_id)
    user_id = str(getattr(request.state, "user_id", "admin"))
    if await is_tenant_reprovision_running(target_tenant):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An embedding reprovision is in progress for tenant '{target_tenant}'.",
        )

    deleted = await delete_tenant_config(target_tenant, settings)
    refreshed = await load_tenant_config(target_tenant, settings)
    repro: dict[str, Any] = {}
    if reprovision and deleted:
        try:
            repro = await trigger_tenant_reprovision(
                tenant_id=target_tenant,
                started_by=user_id,
            )
        except ReprovisionInProgressError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _embedding_config_response(refreshed, repro)


@router.post("/tenants/{tenant_id}/embedding/test", response_model=EmbeddingTestResponse)
async def test_tenant_embedding_config(
    request: Request,
    tenant_id: str,
    payload: EmbeddingTestRequest,
) -> EmbeddingTestResponse:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    current = await load_tenant_config(target_tenant, settings)
    candidate = _merge_embedding_config(current, payload)
    try:
        validate_config(candidate)
        service = build_provider_service(candidate, settings)
        dims = await service.detect_dimensions()
        version = f"{service.model_id}:{dims}"
    except Exception as exc:
        return EmbeddingTestResponse(
            ok=False,
            provider=candidate.provider,
            model=candidate.model or (candidate.azure_deployment or ""),
            message=str(exc),
        )
    return EmbeddingTestResponse(
        ok=True,
        provider=candidate.provider,
        model=service.model_id,
        dimensions=dims,
        embedding_version=version,
        message="Embedding provider reachable.",
    )


@router.get("/tenants/{tenant_id}/embedding/status")
async def get_tenant_embedding_status(
    request: Request,
    tenant_id: str,
) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    return await get_tenant_reprovision_status(target_tenant)


@router.get("/embedding/status")
async def get_embedding_status(request: Request) -> dict[str, Any]:
    _require_platform_admin(request)
    return await get_reprovision_status()
