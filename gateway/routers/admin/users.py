"""Managed-user CRUD, the caller's identity (whoami), and password self-service."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import HTTPException, Query, Request, status

from models.admin import (
    CodeRequirementsPolicySummary,
    DemoScopesResponse,
    DemoUserCreateRequest,
    DemoUserCreateResponse,
    HttpEgressPolicySummary,
    PasswordChangeRequest,
    SandboxBridgesSummary,
    UserCreateRequest,
    UserListResponse,
    UserResponse,
    UserTokenRequest,
    UserTokenResponse,
    UserUpdateRequest,
    WhoAmIResponse,
)
from services import users as users_service
from services.account_tier import get_tenant_confirmation
from services.admin_session import mint_bearer_jwt, mint_session
from services.egress_policy import effective_code_egress_hosts, global_egress_ceiling
from services.passwords import verify_password
from services.tenant_egress import get_tenant_egress_allowlist
from services.tenant_pip_policy import (
    effective_allowlist,
    get_tenant_pip_allowlist,
    global_ceiling_names,
)
from services.tenant_status import get_tenant_read_only
from services.users import (
    DEMO_USER_ROLES,
    TEAM_USER_ROLES,
    VIEWER_USER_ROLES,
    derive_demo_scopes,
    derive_safe_scopes,
)

from ._common import (
    _assert_can_assign_roles,
    _is_platform_admin,
    _load_managed_user,
    _require_tenant_writable,
    _resolve_target_tenant,
    router,
    settings,
)

logger = logging.getLogger(__name__)

# Bound the operator-supplied token lifetime so a typo can't mint a decade-long
# credential, while still allowing a comfortably long demo token.
_MIN_TOKEN_TTL_SECONDS = 60
_MAX_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


@router.get("/whoami", response_model=WhoAmIResponse)
async def who_am_i(request: Request) -> WhoAmIResponse:
    roles = list(getattr(request.state, "roles", []))
    scopes = list(getattr(request.state, "scopes", []))
    tenant_id = str(getattr(request.state, "tenant_id", settings.default_tenant_id))
    user_id = str(getattr(request.state, "user_id", "anonymous"))
    tenant_allowlist = await get_tenant_pip_allowlist(tenant_id, settings=settings)
    ceiling = sorted(global_ceiling_names(settings))
    code_requirements = CodeRequirementsPolicySummary(
        effective=effective_allowlist(tenant_allowlist, settings=settings),
        allowlist=tenant_allowlist,
        global_ceiling=ceiling,
        global_restricted=bool(ceiling),
        execution_enabled=bool(settings.code_tool_execution_enabled),
    )
    egress_allowlist = await get_tenant_egress_allowlist(tenant_id, settings=settings)
    egress_ceiling = global_egress_ceiling(settings)
    http_egress = HttpEgressPolicySummary(
        enabled=bool(settings.sandbox_http_bridge_enabled),
        effective=effective_code_egress_hosts(egress_allowlist, settings=settings),
        allowlist=sorted(egress_allowlist),
        global_ceiling=egress_ceiling,
        global_restricted=bool(egress_ceiling),
    )
    sandbox = SandboxBridgesSummary(
        db_bridge_enabled=bool(settings.sandbox_db_bridge_enabled),
        tool_bridge_enabled=bool(settings.sandbox_tool_bridge_enabled),
        http_bridge_enabled=bool(settings.sandbox_http_bridge_enabled),
    )
    return WhoAmIResponse(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=roles,
        scopes=scopes,
        is_platform_admin=settings.platform_admin_role in set(roles),
        # The viewer principal drives the console's read-only UX; tenant_read_only
        # tells even a full admin that tenant-scoped writes are frozen.
        is_read_only=bool(getattr(request.state, "is_read_only_principal", False)),
        tenant_read_only=await get_tenant_read_only(tenant_id),
        confirmation=await get_tenant_confirmation(tenant_id),
        code_requirements=code_requirements,
        http_egress=http_egress,
        sandbox=sandbox,
        auth_mode=settings.auth_mode,
    )


@router.post("/users", response_model=UserResponse)
async def create_user(request: Request, payload: UserCreateRequest) -> UserResponse:
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    await _require_tenant_writable(request, target_tenant)
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
            label=payload.label,
            client=payload.client,
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


@router.get("/users/demo-scopes", response_model=DemoScopesResponse)
async def get_demo_scopes(
    request: Request,
    tenant_id: str | None = Query(default=None),
) -> DemoScopesResponse:
    """Recommended demo roles/scopes for a tenant, derived from its live catalog.

    Powers the console's "Demo" role preset so a manually-created demo user gets a
    scope set that actually clears discovery + invocation — declared before
    ``GET /users/{user_id}`` so the literal path wins over the wildcard.
    """
    target_tenant = _resolve_target_tenant(request, tenant_id)
    scopes = await derive_demo_scopes(target_tenant)
    return DemoScopesResponse(tenant_id=target_tenant, roles=list(DEMO_USER_ROLES), scopes=scopes)


@router.get("/users/safe-scopes", response_model=DemoScopesResponse)
async def get_safe_scopes(
    request: Request,
    tenant_id: str | None = Query(default=None),
) -> DemoScopesResponse:
    """Recommended roles/scopes for a non-destructive "team" user.

    Powers the console's "Team member" role preset: a tool-invoking account
    scoped to read-only tools only (see :func:`derive_safe_scopes`). Declared
    before ``GET /users/{user_id}`` so the literal path wins over the wildcard.
    """
    target_tenant = _resolve_target_tenant(request, tenant_id)
    scopes = await derive_safe_scopes(target_tenant)
    return DemoScopesResponse(tenant_id=target_tenant, roles=list(TEAM_USER_ROLES), scopes=scopes)


@router.post(
    "/users/demo", response_model=DemoUserCreateResponse, status_code=status.HTTP_201_CREATED
)
async def create_demo_user(
    request: Request,
    payload: DemoUserCreateRequest | None = None,
) -> DemoUserCreateResponse:
    """One-click: create a ready-to-use, tool-invoking demo account.

    Generates a password and (unless supplied) a unique email, grants
    ``tool:invoke`` plus catalog-derived scopes, and returns the credential once.
    The result is immediately usable with ``POST /users/{id}/token`` to hand the
    demo consumer a bearer + ``mcp.json``. Honors the same tenant-scoping RBAC as
    the rest of the user surface (a tenant-admin can only target their own tenant).
    """
    payload = payload or DemoUserCreateRequest()
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    await _require_tenant_writable(request, target_tenant)
    scopes = await derive_demo_scopes(target_tenant)
    password = secrets.token_urlsafe(12)
    created_by = str(getattr(request.state, "user_id", "")) or None

    user = await _create_preset_user_record(
        email=payload.email,
        tenant_id=target_tenant,
        scopes=scopes,
        password=password,
        created_by=created_by,
        roles=list(DEMO_USER_ROLES),
        email_prefix="demo",
        label=payload.label,
        client=payload.client,
    )
    await users_service.sync_session_context(user)

    logger.info(
        "Demo user created: actor=%s target=%s tenant=%s scopes=%s",
        created_by or "unknown",
        user["email"],
        target_tenant,
        scopes,
    )
    return DemoUserCreateResponse(user=UserResponse(**user), password=password, created=True)


@router.post(
    "/users/viewer", response_model=DemoUserCreateResponse, status_code=status.HTTP_201_CREATED
)
async def create_viewer_user(
    request: Request,
    payload: DemoUserCreateRequest | None = None,
) -> DemoUserCreateResponse:
    """One-click: create a discover-only (``tool:read``) viewer account.

    The data-plane twin of the demo button: a viewer can ``tools/list`` /
    ``tools/search`` the curated catalog (catalog-derived scopes so discovery is
    complete) but per-call authorization refuses ``tools/call`` because it lacks
    ``tool:invoke``. Hand the returned credential + ``mcp.json`` to someone who
    should explore, not run, the platform. Same tenant-scoping RBAC as the rest of
    the user surface.
    """
    payload = payload or DemoUserCreateRequest()
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    await _require_tenant_writable(request, target_tenant)
    scopes = await derive_demo_scopes(target_tenant)
    password = secrets.token_urlsafe(12)
    created_by = str(getattr(request.state, "user_id", "")) or None

    user = await _create_preset_user_record(
        email=payload.email,
        tenant_id=target_tenant,
        scopes=scopes,
        password=password,
        created_by=created_by,
        roles=list(VIEWER_USER_ROLES),
        email_prefix="viewer",
        label=payload.label,
        client=payload.client,
    )
    await users_service.sync_session_context(user)

    logger.info(
        "Viewer user created: actor=%s target=%s tenant=%s scopes=%s",
        created_by or "unknown",
        user["email"],
        target_tenant,
        scopes,
    )
    return DemoUserCreateResponse(user=UserResponse(**user), password=password, created=True)


@router.post(
    "/users/team", response_model=DemoUserCreateResponse, status_code=status.HTTP_201_CREATED
)
async def create_team_user(
    request: Request,
    payload: DemoUserCreateRequest | None = None,
) -> DemoUserCreateResponse:
    """One-click: create a safe-to-share, read-only-invoking "team" account.

    The non-destructive middle ground between the demo and viewer buttons: it
    carries ``tool:invoke`` so it can actually *run* tools (a real "hello world"
    over MCP), but its scopes are limited to read-only tools (see
    :func:`derive_safe_scopes`) so per-call authorization refuses anything that
    writes or deletes. Returns a credential + ``mcp.json`` to hand a teammate.
    Same tenant-scoping RBAC as the rest of the user surface.
    """
    payload = payload or DemoUserCreateRequest()
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    await _require_tenant_writable(request, target_tenant)
    scopes = await derive_safe_scopes(target_tenant)
    password = secrets.token_urlsafe(12)
    created_by = str(getattr(request.state, "user_id", "")) or None

    user = await _create_preset_user_record(
        email=payload.email,
        tenant_id=target_tenant,
        scopes=scopes,
        password=password,
        created_by=created_by,
        roles=list(TEAM_USER_ROLES),
        email_prefix="team",
        label=payload.label,
        client=payload.client,
    )
    await users_service.sync_session_context(user)

    logger.info(
        "Team user created: actor=%s target=%s tenant=%s scopes=%s",
        created_by or "unknown",
        user["email"],
        target_tenant,
        scopes,
    )
    return DemoUserCreateResponse(user=UserResponse(**user), password=password, created=True)


async def _create_preset_user_record(
    *,
    email: str | None,
    tenant_id: str,
    scopes: list[str],
    password: str,
    created_by: str | None,
    roles: list[str],
    email_prefix: str,
    label: str | None = None,
    client: str | None = None,
) -> dict[str, Any]:
    """Create a preset (demo/viewer) user, retrying generated emails on collision.

    An operator-supplied email that already exists is a hard 409 (the caller chose
    it); an auto-generated ``<prefix>-<rand>@demo.local`` address is simply
    re-rolled so one-click never fails on an astronomically unlikely clash.
    """
    operator_supplied = bool(email and email.strip())
    attempts = 1 if operator_supplied else 5
    last_exc: users_service.UserAlreadyExists | None = None
    for _ in range(attempts):
        candidate = (
            email if operator_supplied else f"{email_prefix}-{secrets.token_hex(3)}@demo.local"
        )
        try:
            return await users_service.create_user(
                email=candidate or "",
                password=password,
                tenant_id=tenant_id,
                roles=roles,
                scopes=scopes,
                status="active",
                created_by=created_by,
                label=label,
                client=client,
            )
        except users_service.UserAlreadyExists as exc:
            last_exc = exc
        except users_service.UserError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(last_exc))


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


@router.post("/users/{user_id}/token", response_model=UserTokenResponse)
async def mint_user_token(
    request: Request,
    user_id: str,
    payload: UserTokenRequest | None = None,
) -> UserTokenResponse:
    """Mint a ready-to-use bearer token carrying a managed user's identity.

    Authorization reuses :func:`_load_managed_user`: a tenant-admin may only mint
    for users inside their own tenant and never for a platform-admin account.
    This grants no privilege the caller lacks -- it is no more powerful than the
    admin-initiated password reset already exposed on this surface.

    The token's shape is auth-mode aware:

    * ``hs256`` -- a real scoped bearer (roles + groups/scopes), which the
      gateway verifies against ``jwt_secret``.
    * ``jwks`` -- the gateway cannot forge a token its IdP-backed verifier
      trusts, so it falls back to a roles-only admin-session token (accepted on
      ``/rpc`` + ``/mcp``) and surfaces a caveat.
    """
    doc = await _load_managed_user(request, user_id)
    user = users_service.public_user(doc)
    email = str(user.get("email", ""))
    tenant_id = str(user.get("tenant_id", "")) or settings.default_tenant_id
    roles = list(user.get("roles", []))
    scopes = list(user.get("scopes", []))
    role_set = set(roles)
    data_plane_ok = "admin" in role_set or "tool:invoke" in role_set

    caveat: str | None = None

    if settings.auth_mode == "jwks":
        token = mint_session(email, tenant_id=tenant_id, roles=roles)
        expires_in = settings.admin_session_ttl_seconds
        caveat = (
            "auth_mode is 'jwks': this is a roles-only admin-session token "
            "(no fine-grained scopes). For scoped tokens, issue them from your IdP."
        )
    else:  # hs256
        expires_in = _resolve_token_ttl(payload)
        token = mint_bearer_jwt(
            email,
            tenant_id=tenant_id,
            roles=roles,
            scopes=scopes,
            ttl_seconds=expires_in,
        )

    if not data_plane_ok:
        if "tool:read" in role_set:
            gate_note = (
                "This account carries 'tool:read' only, so the token can discover "
                "tools (tools/list, tools/search) but tools/call is rejected."
            )
        else:
            gate_note = (
                "This account lacks the 'admin' or 'tool:invoke' role, so the token "
                "authenticates but is rejected at the /rpc and /mcp gate."
            )
        caveat = f"{caveat} {gate_note}".strip() if caveat else gate_note

    # A minted token is a credential; record who issued one for whom so the act
    # is auditable (the token value itself is never logged).
    logger.info(
        "Access token minted: actor=%s target=%s tenant=%s roles=%s ttl_seconds=%s",
        getattr(request.state, "user_id", "unknown"),
        email,
        tenant_id,
        roles,
        expires_in,
    )

    return UserTokenResponse(
        auth_mode=settings.auth_mode,
        token=token,
        expires_in=expires_in,
        tenant_id=tenant_id,
        roles=roles,
        scopes=scopes,
        data_plane_ok=data_plane_ok,
        caveat=caveat,
    )


def _resolve_token_ttl(payload: UserTokenRequest | None) -> int:
    """Clamp the requested TTL (minutes) into a sane window of seconds."""
    if payload is None or payload.ttl_minutes is None:
        return settings.admin_session_ttl_seconds
    requested = payload.ttl_minutes * 60
    return max(_MIN_TOKEN_TTL_SECONDS, min(_MAX_TOKEN_TTL_SECONDS, requested))


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    request: Request,
    user_id: str,
    payload: UserUpdateRequest,
) -> UserResponse:
    doc = await _load_managed_user(request, user_id)
    await _require_tenant_writable(request, str(doc.get("tenant_id")))
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
    await _require_tenant_writable(request, str(doc.get("tenant_id")))
    caller_email = str(getattr(request.state, "user_id", ""))
    if str(doc.get("email", "")) == caller_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account.",
        )
    await users_service.delete_user(user_id)
    return {"deleted": True, "id": user_id}
