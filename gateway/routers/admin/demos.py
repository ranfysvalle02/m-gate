"""Demo workspaces: one-click, isolated, self-expiring demo tenants.

Platform-admin-only surface that spins up a fully-seeded demo tenant per
prospect (``POST /admin/demos``), lists active demos (``GET /admin/demos``), and
hard-deletes one (``DELETE /admin/demos/{tenant_id}``). The heavy lifting —
provision, confirm, seed a capability-aware pack + sample data, mint a tenant-
admin login, cap, and reap — lives in :mod:`services.demo_workspace`; these
handlers are thin adapters that enforce RBAC and map errors to HTTP.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

from models.admin import (
    DemoWorkspaceCreateRequest,
    DemoWorkspaceListResponse,
    DemoWorkspaceResponse,
    DemoWorkspaceSummary,
)
from services.demo_workspace import (
    DemoCapReached,
    DemoWorkspace,
    DemoWorkspaceError,
    DemoWorkspacesDisabled,
    delete_demo_workspace,
    list_demo_workspaces,
    provision_demo_workspace,
)

from . import _common as c
from ._common import _require_platform_admin, router, settings

logger = logging.getLogger(__name__)


def _login_url() -> str:
    return f"{settings.admin_ui_path.rstrip('/')}/login"


def _summary(workspace: DemoWorkspace) -> DemoWorkspaceSummary:
    return DemoWorkspaceSummary(
        tenant_id=workspace.tenant_id,
        db_name=workspace.db_name,
        label=workspace.label,
        client=workspace.client,
        status=workspace.status,
        created_at=workspace.created_at,
        created_by=workspace.created_by,
        expires_at=workspace.expires_at,
        expired=workspace.expired,
        user_id=workspace.user_id,
        user_email=workspace.user_email,
        servers=workspace.servers,
        tools=workspace.tools,
        bridges=workspace.bridges,
    )


@router.post(
    "/demos", response_model=DemoWorkspaceResponse, status_code=status.HTTP_201_CREATED
)
async def create_demo_workspace(
    request: Request,
    payload: DemoWorkspaceCreateRequest | None = None,
) -> DemoWorkspaceResponse:
    """Provision a fully-seeded, isolated, self-expiring demo workspace.

    Platform-admin only (creating tenants + databases is a platform operation).
    Returns the demo's tenant-admin credential exactly once, plus the relative
    console login path to hand the prospect.
    """
    _require_platform_admin(request)
    payload = payload or DemoWorkspaceCreateRequest()
    actor = str(getattr(request.state, "user_id", "")) or None
    try:
        workspace = await provision_demo_workspace(
            label=payload.label,
            client=payload.client,
            ttl_hours=payload.ttl_hours,
            created_by=actor,
        )
    except DemoWorkspacesDisabled as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DemoCapReached as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except DemoWorkspaceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    c.get_telemetry_logger().log_background(
        tenant_id=workspace.tenant_id,
        user_id=actor or "admin",
        method="admin/demos/create",
        status="demo_workspace_created",
        metadata={
            "tenant_id": workspace.tenant_id,
            "actor": actor,
            "servers": workspace.servers,
            "tools": workspace.tools,
            "expires_at": workspace.expires_at.isoformat() if workspace.expires_at else None,
        },
    )
    summary = _summary(workspace)
    return DemoWorkspaceResponse(
        **summary.model_dump(),
        password=workspace.password,
        login_url=_login_url(),
    )


@router.get("/demos", response_model=DemoWorkspaceListResponse)
async def list_demos(request: Request) -> DemoWorkspaceListResponse:
    """List active demo workspaces (platform-admin only). Reaps expired ones first."""
    _require_platform_admin(request)
    workspaces = await list_demo_workspaces()
    return DemoWorkspaceListResponse(
        items=[_summary(w) for w in workspaces],
        max_demo_tenants=max(0, int(settings.max_demo_tenants)),
        enabled=bool(settings.demo_workspaces_enabled),
    )


@router.delete("/demos/{tenant_id}")
async def delete_demo(request: Request, tenant_id: str) -> dict[str, object]:
    """Hard-delete a demo workspace (platform-admin only).

    Refuses any tenant that is not ``origin="demo"`` (404), so this surface can
    never drop a real customer tenant.
    """
    _require_platform_admin(request)
    actor = str(getattr(request.state, "user_id", "")) or "admin"
    deleted = await delete_demo_workspace(tenant_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Demo workspace not found.",
        )
    c.get_telemetry_logger().log_background(
        tenant_id=tenant_id,
        user_id=actor,
        method="admin/demos/delete",
        status="demo_workspace_deleted",
        metadata={"tenant_id": tenant_id, "actor": actor},
    )
    return {"deleted": True, "tenant_id": tenant_id}
