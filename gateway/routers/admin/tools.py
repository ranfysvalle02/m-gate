"""Per-tenant tool enable/disable overlay.

A tool-level kill-switch that is distinct from both server enable/disable
(``servers.py``) and the allowlist (``tenants.py`` tool-policy): an operator can
take a *single* tool out of service for a tenant without touching its server or
rebuilding the allowlist. The overlay lives on the ``tenants`` control doc
(``disabled_tools``) so it survives the catalog sync that rebuilds
``tool_catalog`` from downstream servers.

Both a tenant-admin (within their own tenant) and a platform-admin may toggle a
tool; the action is a mutation, so it is refused while the tenant is read-only
(platform-admin bypasses).
"""

from __future__ import annotations

from fastapi import HTTPException, Query, Request, status

from models.admin import ToolEnableResponse
from services.tenant_tool_policy import set_tool_enabled

from . import _common as c
from ._common import (
    _require_tenant_admin,
    _require_tenant_writable,
    _resolve_target_tenant,
    router,
)


async def _require_tool_exists(tenant_id: str, server: str, name: str) -> None:
    doc = await c.get_tenant_database(tenant_id)["tool_catalog"].find_one(
        {"server": server, "name": name}
    )
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tool not found in tenant catalog.",
        )


@router.post("/tools/{server}/{name}/enable", response_model=ToolEnableResponse)
async def enable_tool(
    request: Request,
    server: str,
    name: str,
    tenant_id: str | None = Query(default=None),
) -> ToolEnableResponse:
    return await _set_tool_enabled_endpoint(request, server, name, True, tenant_id)


@router.post("/tools/{server}/{name}/disable", response_model=ToolEnableResponse)
async def disable_tool(
    request: Request,
    server: str,
    name: str,
    tenant_id: str | None = Query(default=None),
) -> ToolEnableResponse:
    return await _set_tool_enabled_endpoint(request, server, name, False, tenant_id)


async def _set_tool_enabled_endpoint(
    request: Request,
    server: str,
    name: str,
    enabled: bool,
    tenant_id: str | None,
) -> ToolEnableResponse:
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    await _require_tenant_writable(request, target_tenant)
    await _require_tool_exists(target_tenant, server, name)
    doc = await set_tool_enabled(
        target_tenant,
        server,
        name,
        enabled,
        updated_by=str(getattr(request.state, "user_id", "admin")),
    )
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    c.get_telemetry_logger().log_background(
        tenant_id=target_tenant,
        user_id=str(getattr(request.state, "user_id", "admin")),
        method="admin/tools/enable" if enabled else "admin/tools/disable",
        status="tool_enabled" if enabled else "tool_disabled",
        metadata={"tenant_id": target_tenant, "server": server, "tool": name},
    )
    return ToolEnableResponse(
        tenant_id=target_tenant,
        server=server,
        name=name,
        enabled=enabled,
    )
