"""Managed-user CRUD, the caller's identity (whoami), and password self-service."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query, Request, status

from models.admin import (
    PasswordChangeRequest,
    UserCreateRequest,
    UserListResponse,
    UserResponse,
    UserUpdateRequest,
    WhoAmIResponse,
)
from services import users as users_service
from services.passwords import verify_password

from ._common import (
    _assert_can_assign_roles,
    _is_platform_admin,
    _load_managed_user,
    _resolve_target_tenant,
    router,
    settings,
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
