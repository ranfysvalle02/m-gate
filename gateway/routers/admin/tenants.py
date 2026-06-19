"""Tenant lifecycle, egress allowlist, per-server secrets, usage, and quota."""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from models.admin import (
    EgressAllowlistResponse,
    EgressAllowlistUpdateRequest,
    PipPolicyEntry,
    PipPolicyResponse,
    PipPolicyUpdateRequest,
    QuotaResponse,
    QuotaUpdateRequest,
    ServerEnvResponse,
    ServerEnvUpdateRequest,
    TenantCreateRequest,
    TenantDeleteResponse,
    TenantResponse,
    TenantRestoreResponse,
    TenantStatusUpdateRequest,
    ToolPolicyResponse,
    ToolPolicyToolEntry,
    ToolPolicyUpdateRequest,
    UsageEventsResponse,
    UsageRemaining,
    UsageResponse,
    UsageTotals,
)
from services.account_tier import (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_UNCONFIRMED,
    set_tenant_confirmation,
    tier_caps,
)
from services.code_tools import encrypt_raw_code
from services.egress_policy import EgressNotAllowed, parse_allowlist
from services.tenant_egress import set_tenant_egress_allowlist
from services.tenant_pip_policy import (
    PipPolicyError,
    effective_allowlist,
    global_ceiling_names,
    set_tenant_pip_allowlist,
)
from services.tenant_provisioner import (
    deprovision_tenant,
    soft_delete_tenant,
    tenant_db_name,
)
from services.tenant_provisioner import restore_tenant as restore_tenant_record
from services.tenant_status import (
    STATUS_ACTIVE,
    STATUS_SUSPENDED,
    set_tenant_read_only,
    set_tenant_status,
)
from services.tenant_tool_policy import (
    get_tool_policy,
    matches_allowlist,
    set_tool_policy,
)
from services.usage_metering import (
    USAGE_EVENTS_COLLECTION,
    get_effective_quota,
    get_usage,
    set_quota,
    summarize_billing_events,
)

from . import _common as c
from ._common import (
    _build_time_range,
    _is_platform_admin,
    _require_platform_admin,
    _require_tenant_admin,
    _require_tenant_writable,
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
    read_only = bool(doc.get("read_only", False))
    read_only_reason = str(doc.get("read_only_reason", "")) or None
    confirmation = str(doc.get("confirmation") or CONFIRMATION_CONFIRMED)
    if confirmation not in {CONFIRMATION_CONFIRMED, CONFIRMATION_UNCONFIRMED}:
        confirmation = CONFIRMATION_CONFIRMED
    return TenantResponse(
        tenant_id=str(doc.get("tenant_id")),
        db_name=str(doc.get("db_name") or db_name or ""),
        status=status_value,
        suspended_reason=reason if status_value == STATUS_SUSPENDED else None,
        deleted_at=doc.get("deleted_at"),
        purge_at=doc.get("purge_at"),
        read_only=read_only,
        read_only_reason=read_only_reason if read_only else None,
        confirmation=confirmation,
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
async def delete_tenant(
    request: Request,
    tenant_id: str,
    hard: bool = False,
) -> TenantDeleteResponse:
    # ``hard=true`` permanently drops the tenant database immediately
    # (irreversible). The default is a reversible soft-delete with retention.
    _require_platform_admin(request)
    actor = str(getattr(request.state, "user_id", "admin"))

    if hard:
        deleted = await deprovision_tenant(tenant_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
        c.get_telemetry_logger().log_background(
            tenant_id=tenant_id,
            user_id=actor,
            method="admin/tenants/delete",
            status="tenant_deprovisioned",
            metadata={"tenant_id": tenant_id, "actor": actor, "hard": True},
        )
        return TenantDeleteResponse(
            tenant_id=tenant_id,
            db_name=tenant_db_name(tenant_id),
            status="purged",
            deleted=True,
            purge_at=None,
        )

    doc = await soft_delete_tenant(tenant_id, actor=actor)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    c.get_telemetry_logger().log_background(
        tenant_id=tenant_id,
        user_id=actor,
        method="admin/tenants/delete",
        status="tenant_soft_deleted",
        metadata={"tenant_id": tenant_id, "actor": actor, "purge_at": doc.get("purge_at")},
    )
    return TenantDeleteResponse(
        tenant_id=tenant_id,
        db_name=str(doc.get("db_name") or tenant_db_name(tenant_id)),
        status="deleted",
        deleted=True,
        purge_at=doc.get("purge_at"),
    )


@router.post("/tenants/{tenant_id}/restore", response_model=TenantRestoreResponse)
async def restore_tenant(request: Request, tenant_id: str) -> TenantRestoreResponse:
    _require_platform_admin(request)
    actor = str(getattr(request.state, "user_id", "admin"))
    doc = await restore_tenant_record(tenant_id, actor=actor)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found or not soft-deleted.",
        )
    c.get_telemetry_logger().log_background(
        tenant_id=tenant_id,
        user_id=actor,
        method="admin/tenants/restore",
        status="tenant_restored",
        metadata={"tenant_id": tenant_id, "actor": actor},
    )
    return TenantRestoreResponse(
        tenant_id=tenant_id,
        db_name=str(doc.get("db_name") or tenant_db_name(tenant_id)),
        status="active",
        restored=True,
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


@router.post("/tenants/{tenant_id}/confirm", response_model=TenantResponse)
async def confirm_tenant(request: Request, tenant_id: str) -> TenantResponse:
    """Promote a self-registered tenant from ``unconfirmed`` to ``confirmed``.

    Lifts the unconfirmed tier caps (transport allowlist, server/tool ceilings) and
    re-stamps the tenant's quota to the confirmed tier. Platform-admin only, since
    confirmation is the trust decision that unlocks registering external servers.
    """
    return await _set_tenant_confirmation_endpoint(
        request=request,
        tenant_id=tenant_id,
        confirmation=CONFIRMATION_CONFIRMED,
    )


@router.post("/tenants/{tenant_id}/unconfirm", response_model=TenantResponse)
async def unconfirm_tenant(request: Request, tenant_id: str) -> TenantResponse:
    """Revoke trust: move a tenant back to ``unconfirmed`` and re-apply its caps."""
    return await _set_tenant_confirmation_endpoint(
        request=request,
        tenant_id=tenant_id,
        confirmation=CONFIRMATION_UNCONFIRMED,
    )


async def _set_tenant_confirmation_endpoint(
    *,
    request: Request,
    tenant_id: str,
    confirmation: str,
) -> TenantResponse:
    # Confirmation gates the high-risk capability (registering external servers),
    # so it is platform-admin only — a tenant-admin cannot confirm its own tenant.
    _require_platform_admin(request)
    actor = str(getattr(request.state, "user_id", "admin"))
    doc = await set_tenant_confirmation(tenant_id, confirmation, updated_by=actor)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    # Re-stamp the per-tenant quota to match the new tier so usage limits track the
    # tier in lockstep with the cap changes.
    caps = tier_caps(confirmation, settings=settings)
    await set_quota(
        tenant_id,
        calls_limit=caps.quota_calls_per_period,
        sandbox_seconds_limit=caps.quota_sandbox_seconds_per_period,
        updated_by=actor,
    )
    c.get_telemetry_logger().log_background(
        tenant_id=tenant_id,
        user_id=actor,
        method=f"admin/tenants/{confirmation}",
        status=f"tenant_{confirmation}",
        metadata={"tenant_id": tenant_id, "actor": actor, "confirmation": confirmation},
    )
    return _tenant_response(doc)


@router.post("/tenants/{tenant_id}/read-only", response_model=TenantResponse)
async def make_tenant_read_only(
    request: Request,
    tenant_id: str,
    payload: TenantStatusUpdateRequest | None = None,
) -> TenantResponse:
    return await _set_tenant_read_only_endpoint(
        request=request,
        tenant_id=tenant_id,
        enabled=True,
        reason=payload.reason if payload else None,
    )


@router.post("/tenants/{tenant_id}/read-write", response_model=TenantResponse)
async def make_tenant_read_write(request: Request, tenant_id: str) -> TenantResponse:
    return await _set_tenant_read_only_endpoint(
        request=request,
        tenant_id=tenant_id,
        enabled=False,
        reason=None,
    )


async def _set_tenant_read_only_endpoint(
    *,
    request: Request,
    tenant_id: str,
    enabled: bool,
    reason: str | None,
) -> TenantResponse:
    # Read-only is a platform control (it freezes a whole tenant for a showcase),
    # so only a platform-admin may toggle it. A tenant-admin cannot lift their own
    # tenant's freeze.
    _require_platform_admin(request)
    actor = str(getattr(request.state, "user_id", "admin"))
    doc = await set_tenant_read_only(
        tenant_id,
        enabled,
        updated_by=actor,
        reason=reason,
    )
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    c.get_telemetry_logger().log_background(
        tenant_id=tenant_id,
        user_id=actor,
        method="admin/tenants/read-only" if enabled else "admin/tenants/read-write",
        status="tenant_read_only" if enabled else "tenant_read_write",
        metadata={"tenant_id": tenant_id, "actor": actor, "reason": reason},
    )
    return _tenant_response(doc)


@router.get("/tenants/{tenant_id}/tool-policy", response_model=ToolPolicyResponse)
async def get_tenant_tool_policy(request: Request, tenant_id: str) -> ToolPolicyResponse:
    # Read-only-safe (GET): tenant-admins may view their own curation; cross-tenant
    # still needs platform-admin via _resolve_target_tenant.
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    policy = await get_tool_policy(target_tenant)
    catalog = (
        await c.get_tenant_database(target_tenant)["tool_catalog"].find({}).to_list(length=10_000)
    )
    catalog.sort(key=lambda item: (str(item.get("server", "")), str(item.get("name", ""))))
    available = [
        ToolPolicyToolEntry(
            server=str(doc.get("server", "")),
            name=str(doc.get("name", "")),
            description=str(doc.get("description", "")),
            allowlisted=matches_allowlist(
                str(doc.get("server", "")), str(doc.get("name", "")), policy["allowlist"]
            ),
            disabled=f"{doc.get('server', '')}/{doc.get('name', '')}" in policy["disabled_tools"],
        )
        for doc in catalog
    ]
    return ToolPolicyResponse(
        tenant_id=target_tenant,
        allowlist=policy["allowlist"],
        max_tools=policy["max_tools"],
        disabled_tools=policy["disabled_tools"],
        available_tools=available,
    )


@router.put("/tenants/{tenant_id}/tool-policy", response_model=ToolPolicyResponse)
async def put_tenant_tool_policy(
    request: Request,
    tenant_id: str,
    payload: ToolPolicyUpdateRequest,
) -> ToolPolicyResponse:
    # Curating the allowlist / cap is a tenant-admin operation, but it is refused
    # while the tenant is read-only (platform-admin bypasses, since they own the
    # freeze and may need to re-curate it).
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    await _require_tenant_writable(request, target_tenant)
    actor = str(getattr(request.state, "user_id", "admin"))
    doc = await set_tool_policy(
        target_tenant,
        allowlist=payload.allowlist,
        max_tools=payload.max_tools,
        updated_by=actor,
    )
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    c.get_telemetry_logger().log_background(
        tenant_id=target_tenant,
        user_id=actor,
        method="admin/tenants/tool-policy",
        status="tool_policy_updated",
        metadata={
            "tenant_id": target_tenant,
            "actor": actor,
            "allowlist_size": len(payload.allowlist),
            "max_tools": payload.max_tools,
        },
    )
    return await get_tenant_tool_policy(request, target_tenant)


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
    await _require_tenant_writable(request, target_tenant)
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


def _pip_policy_response(tenant_id: str, doc: dict[str, Any] | None) -> PipPolicyResponse:
    stored = (doc or {}).get("code_requirements_allowlist")
    allowlist = (
        sorted(
            {str(item).strip() for item in stored if isinstance(item, str) and str(item).strip()}
        )
        if isinstance(stored, list)
        else []
    )
    ceiling = sorted(global_ceiling_names(settings))
    ceiling_set = set(ceiling)
    effective = effective_allowlist(allowlist, settings=settings)
    return PipPolicyResponse(
        tenant_id=tenant_id,
        allowlist=allowlist,
        global_ceiling=ceiling,
        effective=effective,
        entries=[
            PipPolicyEntry(name=name, in_global_ceiling=name in ceiling_set) for name in allowlist
        ],
        global_restricted=bool(ceiling),
        execution_enabled=bool(settings.code_tool_execution_enabled),
        updated_at=(doc or {}).get("code_requirements_updated_at"),
        updated_by=(doc or {}).get("code_requirements_updated_by"),
    )


@router.get(
    "/tenants/{tenant_id}/code-requirements",
    response_model=PipPolicyResponse,
)
async def get_code_requirements_policy(request: Request, tenant_id: str) -> PipPolicyResponse:
    # Read-only-safe (GET): tenant-admins may view their own code-package policy;
    # cross-tenant still needs platform-admin via _resolve_target_tenant.
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    doc = await c.get_control_database()["tenants"].find_one({"tenant_id": target_tenant})
    return _pip_policy_response(target_tenant, doc)


@router.put(
    "/tenants/{tenant_id}/code-requirements",
    response_model=PipPolicyResponse,
)
async def put_code_requirements_policy(
    request: Request,
    tenant_id: str,
    payload: PipPolicyUpdateRequest,
) -> PipPolicyResponse:
    # Curating the tenant's allowed packages is a tenant-admin operation, refused
    # while the tenant is read-only (platform-admin bypasses, since they own the
    # freeze and may need to re-curate it).
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    await _require_tenant_writable(request, target_tenant)
    actor = str(getattr(request.state, "user_id", "admin"))
    try:
        doc = await set_tenant_pip_allowlist(
            target_tenant,
            payload.allowlist,
            updated_by=actor,
        )
    except PipPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    c.get_telemetry_logger().log_background(
        tenant_id=target_tenant,
        user_id=actor,
        method="admin/tenants/code-requirements",
        status="code_requirements_updated",
        metadata={
            "tenant_id": target_tenant,
            "actor": actor,
            "entries": len(payload.allowlist),
        },
    )
    return _pip_policy_response(target_tenant, doc)


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
    await _require_tenant_writable(request, target_tenant)
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


_USAGE_EXPORT_COLUMNS = ["ts", "tenant_id", "period", "kind", "amount", "source"]


@router.get("/tenants/{tenant_id}/usage/export")
async def export_tenant_usage(
    request: Request,
    tenant_id: str,
    export_format: str = Query(default="csv", alias="format"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream a tenant's raw ``usage_events`` as CSV over a date range.

    The events are iterated straight off a ``find()`` cursor (``async for``) and
    written one CSV row at a time, so an arbitrarily large billing history never
    materializes in memory. The ``(tenant_id, ts)`` index backs the range scan.
    """
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    if export_format != "csv":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only format=csv is supported for usage export.",
        )

    query: dict[str, Any] = {"tenant_id": target_tenant}
    ts_range = _build_time_range(from_, to)
    if ts_range is not None:
        query["ts"] = ts_range

    cursor = c.get_control_database()[USAGE_EVENTS_COLLECTION].find(query).sort("ts", 1)

    async def _stream() -> AsyncIterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def _drain() -> str:
            chunk = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return chunk

        writer.writerow(_USAGE_EXPORT_COLUMNS)
        yield _drain()
        async for doc in cursor:
            ts = doc.get("ts")
            metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            writer.writerow(
                [
                    ts.isoformat() if isinstance(ts, datetime) else "",
                    target_tenant,
                    str(doc.get("period", "")),
                    str(doc.get("kind", "")),
                    int(doc.get("amount", 0) or 0),
                    str(metadata.get("source", "")),
                ]
            )
            yield _drain()

    filename = f"usage-{target_tenant}.csv"
    return StreamingResponse(
        _stream(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


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
