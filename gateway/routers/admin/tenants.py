"""Tenant lifecycle, egress allowlist, per-server secrets, usage, and quota."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Query, Request, status

from models.admin import (
    EgressAllowlistResponse,
    EgressAllowlistUpdateRequest,
    QuotaResponse,
    QuotaUpdateRequest,
    ServerEnvResponse,
    ServerEnvUpdateRequest,
    TenantCreateRequest,
    TenantDeleteResponse,
    TenantResponse,
    TenantStatusUpdateRequest,
    UsageEventsResponse,
    UsageRemaining,
    UsageResponse,
    UsageTotals,
)
from services.code_tools import encrypt_raw_code
from services.egress_policy import EgressNotAllowed, parse_allowlist
from services.tenant_egress import set_tenant_egress_allowlist
from services.tenant_provisioner import deprovision_tenant, tenant_db_name
from services.tenant_status import STATUS_ACTIVE, STATUS_SUSPENDED, set_tenant_status
from services.usage_metering import (
    get_effective_quota,
    get_usage,
    set_quota,
    summarize_billing_events,
)

from . import _common as c
from ._common import (
    _is_platform_admin,
    _require_platform_admin,
    _require_tenant_admin,
    _resolve_target_tenant,
    router,
    settings,
)


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
    doc = await c.get_tenant_database(tenant_id)["routing_registry"].find_one({"_id": server_name})
    if not isinstance(doc, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found.")


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
    doc = await c.get_tenant_database(tenant_id)["server_secrets"].find_one({"_id": server_name})
    values = doc.get("values", {}) if isinstance(doc, dict) else {}
    keys = sorted(str(key) for key in values.keys()) if isinstance(values, dict) else []
    return ServerEnvResponse(
        tenant_id=tenant_id,
        server=server_name,
        keys=keys,
        updated_at=doc.get("updated_at") if isinstance(doc, dict) else None,
        updated_by=doc.get("updated_by") if isinstance(doc, dict) else None,
    )


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
    db_name = await c.provision_tenant(tenant_id, wait_for_queryable_indexes=False)
    doc = await c.get_control_database()["tenants"].find_one({"tenant_id": tenant_id})
    if not doc:
        return TenantResponse(tenant_id=tenant_id, db_name=db_name)
    return _tenant_response(doc, db_name=db_name)


@router.get("/tenants", response_model=list[TenantResponse])
async def list_tenants(request: Request) -> list[TenantResponse]:
    control_db = c.get_control_database()
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
    c.get_telemetry_logger().log_background(
        tenant_id=tenant_id,
        user_id=actor,
        method="admin/tenants/delete",
        status="tenant_deprovisioned",
        metadata={"tenant_id": tenant_id, "actor": actor},
    )
    return TenantDeleteResponse(
        tenant_id=tenant_id, db_name=tenant_db_name(tenant_id), deleted=True
    )


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
    c.get_telemetry_logger().log_background(
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
    doc = await c.get_control_database()["tenants"].find_one({"tenant_id": target_tenant})
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
    c.get_telemetry_logger().log_background(
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
    collection = c.get_tenant_database(target_tenant)["server_secrets"]
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
    registry = c.get_proxy_registry()
    await registry.refresh_server_credentials(server_name, tenant_id=target_tenant)
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
