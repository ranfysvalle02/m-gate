"""Human-in-the-loop approval queue for confirmation-gated tool calls."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query, Request, status

from models.admin import PendingActionListResponse, PendingActionResponse
from services.pending_actions import approve_action, list_pending_actions, reject_action

from . import _common as c
from ._common import _require_tenant_admin, _resolve_target_tenant, router


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
    c.get_telemetry_logger().log_background(
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
