"""Tests for the one-click demo-user admin flow.

`POST /admin/users/demo` must create an account that can BOTH discover and invoke
tools out of the box (the whole point of "easy demo"): `tool:invoke` plus
catalog-derived scopes. `GET /admin/users/demo-scopes` backs the console's Demo
preset, and both honor the same tenant-scoping RBAC as the rest of the surface.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import gateway.routers.admin.users as users_router
from models.admin import DemoUserCreateRequest
from services import users as users_service


class _State:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Req:
    def __init__(
        self, *, tenant_id="local-dev", roles=None, user_id="admin@example.com", headers=None
    ):
        self.state = _State(tenant_id=tenant_id, roles=roles or ["platform-admin"], user_id=user_id)
        self.headers = headers or {}


async def _seed_catalog(db) -> None:
    cat = db["tool_catalog"]
    await cat.insert_one({"server": "weather", "name": "get", "scopes": ["weather", "readonly"]})
    await cat.insert_one({"server": "orders", "name": "find", "scopes": ["orders"]})
    # A tool that already carries a server: scope must not leak into tool scopes.
    await cat.insert_one({"server": "orders", "name": "list", "scopes": ["server:orders"]})


# --------------------------------------------------------------------------- #
# derive_demo_scopes: catalog-aware, server:* + distinct tool scopes
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_derive_demo_scopes_from_catalog(reset_settings, patch_mongo):
    await _seed_catalog(patch_mongo)
    scopes = await users_service.derive_demo_scopes("local-dev")
    assert scopes[0] == "server:*"
    # server:* plus every distinct *tool* scope; the server:orders scope is excluded.
    assert set(scopes) == {"server:*", "weather", "readonly", "orders"}


@pytest.mark.asyncio
async def test_derive_demo_scopes_empty_catalog(reset_settings, patch_mongo):
    scopes = await users_service.derive_demo_scopes("local-dev")
    assert scopes == ["server:*"]


# --------------------------------------------------------------------------- #
# POST /admin/users/demo: a working, tool-invoking account in one call
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_create_demo_user_makes_working_account(reset_settings, patch_mongo):
    await _seed_catalog(patch_mongo)
    req = _Req(roles=["platform-admin"], tenant_id="local-dev")

    res = await users_router.create_demo_user(req, None)

    assert res.created is True
    assert res.password  # returned once
    assert res.user.email.startswith("demo-") and res.user.email.endswith("@demo.local")
    assert set(res.user.roles) == {"user", "tool:invoke"}
    assert "server:*" in res.user.scopes and "weather" in res.user.scopes

    # The credential actually authenticates.
    principal = await users_service.authenticate(res.user.email, res.password)
    assert principal is not None

    # And a token minted for it clears the data-plane gate.
    token_res = await users_router.mint_user_token(
        _Req(roles=["platform-admin"], tenant_id="local-dev"), res.user.id, None
    )
    assert token_res.data_plane_ok is True
    assert "server:*" in token_res.scopes


@pytest.mark.asyncio
async def test_create_demo_user_writes_audit_log(reset_settings, patch_mongo, caplog):
    import logging

    req = _Req(roles=["platform-admin"], tenant_id="local-dev", user_id="boss@example.com")
    with caplog.at_level(logging.INFO, logger=users_router.logger.name):
        res = await users_router.create_demo_user(req, None)

    audit = [r for r in caplog.records if "Demo user created" in r.getMessage()]
    assert len(audit) == 1
    message = audit[0].getMessage()
    assert "actor=boss@example.com" in message
    assert f"target={res.user.email}" in message
    # The generated password must never be logged.
    assert res.password not in message


@pytest.mark.asyncio
async def test_create_demo_user_honors_supplied_email(reset_settings, patch_mongo):
    req = _Req(roles=["platform-admin"], tenant_id="local-dev")
    res = await users_router.create_demo_user(req, DemoUserCreateRequest(email="vip@demo.local"))
    assert res.user.email == "vip@demo.local"


@pytest.mark.asyncio
async def test_create_demo_user_carries_label_and_client(reset_settings, patch_mongo):
    # The cosmetic name/client an operator picks in the console must round-trip
    # through the one-click endpoint and onto the stored record.
    req = _Req(roles=["platform-admin"], tenant_id="local-dev")
    res = await users_router.create_demo_user(
        req, DemoUserCreateRequest(label="Cursor — Laptop", client="cursor")
    )
    assert res.user.label == "Cursor — Laptop"
    assert res.user.client == "cursor"
    # They are recognition-only and never affect the granted capability.
    assert set(res.user.roles) == {"user", "tool:invoke"}


@pytest.mark.asyncio
async def test_create_team_and_viewer_users_carry_label_and_client(reset_settings, patch_mongo):
    req = _Req(roles=["platform-admin"], tenant_id="local-dev")

    team = await users_router.create_team_user(
        req, DemoUserCreateRequest(label="Read-only bot", client="vscode")
    )
    assert team.user.label == "Read-only bot"
    assert team.user.client == "vscode"

    viewer = await users_router.create_viewer_user(
        req, DemoUserCreateRequest(label="Showcase", client="claude")
    )
    assert viewer.user.label == "Showcase"
    assert viewer.user.client == "claude"


@pytest.mark.asyncio
async def test_create_user_endpoint_passes_label_and_client(reset_settings, patch_mongo):
    from models.admin import UserCreateRequest

    req = _Req(roles=["platform-admin"], tenant_id="local-dev")
    res = await users_router.create_user(
        req,
        UserCreateRequest(
            email="custom@demo.local",
            password="pw123456",
            roles=["user", "tool:invoke"],
            scopes=["server:*"],
            label="Custom cred",
            client="cursor",
        ),
    )
    assert res.label == "Custom cred"
    assert res.client == "cursor"


@pytest.mark.asyncio
async def test_create_demo_user_existing_email_conflict(reset_settings, patch_mongo):
    await users_service.create_user(
        email="taken@demo.local", password="pw", tenant_id="local-dev", roles=["user"]
    )
    req = _Req(roles=["platform-admin"], tenant_id="local-dev")
    with pytest.raises(HTTPException) as exc:
        await users_router.create_demo_user(req, DemoUserCreateRequest(email="taken@demo.local"))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_create_demo_user_cross_tenant_forbidden(reset_settings, patch_mongo):
    # A tenant-admin (not platform-admin) may not target another tenant.
    req = _Req(roles=["admin"], tenant_id="tenant-a")
    with pytest.raises(HTTPException) as exc:
        await users_router.create_demo_user(req, DemoUserCreateRequest(tenant_id="tenant-b"))
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# GET /admin/users/demo-scopes
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_demo_scopes_endpoint(reset_settings, patch_mongo):
    await _seed_catalog(patch_mongo)
    req = _Req(roles=["platform-admin"], tenant_id="local-dev")
    res = await users_router.get_demo_scopes(req, None)
    assert res.tenant_id == "local-dev"
    assert "server:*" in res.scopes and "weather" in res.scopes
    assert set(res.roles) == {"user", "tool:invoke"}


# --------------------------------------------------------------------------- #
# POST /admin/users/viewer: a discover-only (tool:read) account in one call
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_create_viewer_user_is_discover_only(reset_settings, patch_mongo):
    await _seed_catalog(patch_mongo)
    req = _Req(roles=["platform-admin"], tenant_id="local-dev")

    res = await users_router.create_viewer_user(req, None)

    assert res.created is True
    assert res.password
    assert res.user.email.startswith("viewer-") and res.user.email.endswith("@demo.local")
    # The one-click viewer is the complete read-only identity: `viewer` (read-only
    # console login) + `tool:read` (discover-only MCP), but never `tool:invoke`.
    assert set(res.user.roles) == {"user", "viewer", "tool:read"}
    assert "server:*" in res.user.scopes

    principal = await users_service.authenticate(res.user.email, res.password)
    assert principal is not None

    # A token minted for it does NOT clear the data-plane invoke gate, and the
    # caveat explains it can discover but not call.
    token_res = await users_router.mint_user_token(
        _Req(roles=["platform-admin"], tenant_id="local-dev"), res.user.id, None
    )
    assert token_res.data_plane_ok is False
    assert "tool:read" in token_res.caveat
    assert "tools/call is rejected" in token_res.caveat


@pytest.mark.asyncio
async def test_create_viewer_user_honors_supplied_email(reset_settings, patch_mongo):
    req = _Req(roles=["platform-admin"], tenant_id="local-dev")
    res = await users_router.create_viewer_user(req, DemoUserCreateRequest(email="look@demo.local"))
    assert res.user.email == "look@demo.local"
    assert set(res.user.roles) == {"user", "viewer", "tool:read"}


@pytest.mark.asyncio
async def test_create_viewer_user_cross_tenant_forbidden(reset_settings, patch_mongo):
    req = _Req(roles=["admin"], tenant_id="tenant-a")
    with pytest.raises(HTTPException) as exc:
        await users_router.create_viewer_user(req, DemoUserCreateRequest(tenant_id="tenant-b"))
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# derive_safe_scopes / POST /admin/users/team: non-destructive (read-only invoke)
# --------------------------------------------------------------------------- #
async def _seed_mixed_catalog(db) -> None:
    """A catalog exercising every _is_readonly_tool branch.

    - read tools (explicit action_type=read) carrying `readonly`
    - a destructive tool and a write tool (the writer SHARES `analytics` with a
      read tool, so that shared scope must be dropped from the safe set)
    - a proxied read tool with NO action_type but the `readonly` marker
    - an unknown tool (no action_type, no `readonly`) -> treated as unsafe
    """
    cat = db["tool_catalog"]
    await cat.insert_one(
        {
            "server": "weather",
            "name": "get",
            "scopes": ["weather", "readonly"],
            "metadata": {"action_type": "read"},
        }
    )
    await cat.insert_one(
        {
            "server": "orders",
            "name": "find",
            "scopes": ["orders", "readonly"],
            "metadata": {"action_type": "read"},
        }
    )
    await cat.insert_one(
        {
            "server": "orders",
            "name": "update",
            "scopes": ["orders:write"],
            "metadata": {"action_type": "destructive"},
        }
    )
    await cat.insert_one(
        {
            "server": "analytics",
            "name": "stats",
            "scopes": ["analytics", "server:analytics", "readonly"],
            "metadata": {"action_type": "read"},
        }
    )
    await cat.insert_one(
        {
            "server": "analytics",
            "name": "track",
            "scopes": ["analytics", "server:analytics"],
            "metadata": {"action_type": "write"},
        }
    )
    # Proxied (non-code) read tool: no action_type, but the readonly marker.
    await cat.insert_one({"server": "deepwiki", "name": "ask", "scopes": ["deepwiki", "readonly"]})
    # Unknown tool: neither action_type nor readonly -> fail-closed (excluded).
    await cat.insert_one({"server": "mystery", "name": "x", "scopes": ["mystery"]})


@pytest.mark.asyncio
async def test_derive_safe_scopes_excludes_writers(reset_settings, patch_mongo):
    await _seed_mixed_catalog(patch_mongo)
    scopes = await users_service.derive_safe_scopes("local-dev")
    assert scopes[0] == "server:*"
    # Only read-only scopes survive; writer/destructive/unknown/shared scopes drop.
    assert set(scopes) == {"server:*", "weather", "orders", "readonly", "deepwiki"}
    assert "orders:write" not in scopes  # destructive tool's scope
    assert "analytics" not in scopes  # shared with a writer -> dropped
    assert "mystery" not in scopes  # unclassifiable -> fail-closed


@pytest.mark.asyncio
async def test_derive_safe_scopes_empty_catalog(reset_settings, patch_mongo):
    scopes = await users_service.derive_safe_scopes("local-dev")
    assert scopes == ["server:*"]


@pytest.mark.asyncio
async def test_create_team_user_is_readonly_invoke(reset_settings, patch_mongo):
    await _seed_mixed_catalog(patch_mongo)
    req = _Req(roles=["platform-admin"], tenant_id="local-dev")

    res = await users_router.create_team_user(req, None)

    assert res.created is True
    assert res.password
    assert res.user.email.startswith("team-") and res.user.email.endswith("@demo.local")
    # Same role axis as a demo (it CAN invoke) — safety is in the scopes.
    assert set(res.user.roles) == {"user", "tool:invoke"}
    assert "server:*" in res.user.scopes and "readonly" in res.user.scopes
    assert "orders:write" not in res.user.scopes
    assert "analytics" not in res.user.scopes

    principal = await users_service.authenticate(res.user.email, res.password)
    assert principal is not None

    # A team token clears the data-plane invoke gate (unlike a viewer).
    token_res = await users_router.mint_user_token(
        _Req(roles=["platform-admin"], tenant_id="local-dev"), res.user.id, None
    )
    assert token_res.data_plane_ok is True


@pytest.mark.asyncio
async def test_create_team_user_cross_tenant_forbidden(reset_settings, patch_mongo):
    req = _Req(roles=["admin"], tenant_id="tenant-a")
    with pytest.raises(HTTPException) as exc:
        await users_router.create_team_user(req, DemoUserCreateRequest(tenant_id="tenant-b"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_safe_scopes_endpoint(reset_settings, patch_mongo):
    await _seed_mixed_catalog(patch_mongo)
    req = _Req(roles=["platform-admin"], tenant_id="local-dev")
    res = await users_router.get_safe_scopes(req, None)
    assert res.tenant_id == "local-dev"
    assert set(res.roles) == {"user", "tool:invoke"}
    assert "readonly" in res.scopes and "orders:write" not in res.scopes


# --------------------------------------------------------------------------- #
# GET /admin/whoami: surface read-only principal + tenant read-only state
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_whoami_surfaces_read_only_principal(reset_settings, patch_mongo):
    req = _Req(roles=["viewer"], tenant_id="local-dev")
    req.state.is_read_only_principal = True
    res = await users_router.who_am_i(req)
    assert res.is_read_only is True
    assert res.tenant_read_only is False
    assert res.is_platform_admin is False


@pytest.mark.asyncio
async def test_whoami_surfaces_tenant_read_only(reset_settings, patch_mongo):
    from services.tenant_status import set_tenant_read_only

    await patch_mongo._control_db["tenants"].insert_one(
        {"tenant_id": "local-dev", "db_name": "tenant_local-dev", "status": "active"}
    )
    await set_tenant_read_only("local-dev", True, updated_by="ops", reason="frozen")

    # Even a full platform-admin sees tenant_read_only=True (writes are frozen),
    # but is not itself a read-only principal.
    req = _Req(roles=["platform-admin"], tenant_id="local-dev")
    req.state.is_read_only_principal = False
    res = await users_router.who_am_i(req)
    assert res.tenant_read_only is True
    assert res.is_read_only is False
