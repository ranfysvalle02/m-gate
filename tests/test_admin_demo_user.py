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
