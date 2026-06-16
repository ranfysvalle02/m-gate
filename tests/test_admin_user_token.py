from __future__ import annotations

import logging

import jwt
import pytest
from fastapi import HTTPException

import gateway.routers.admin.users as users_router
from config.settings import get_settings
from services import users as users_service
from services.admin_session import verify_session

JWT_SECRET = "super-secret-for-tests-1234"


class _State:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Req:
    def __init__(
        self,
        *,
        tenant_id: str = "local-dev",
        roles: list[str] | None = None,
        user_id: str = "admin@example.com",
        headers=None,
    ):
        self.state = _State(tenant_id=tenant_id, roles=roles or [], user_id=user_id)
        self.headers = headers or {}


def _user_doc(
    *,
    user_id: str = "u-1",
    tenant_id: str = "tenant-b",
    email: str = "demo@demo.com",
    roles: list[str] | None = None,
    scopes: list[str] | None = None,
):
    return {
        "_id": user_id,
        "tenant_id": tenant_id,
        "email": email,
        "roles": roles if roles is not None else ["user", "tool:invoke"],
        "scopes": scopes if scopes is not None else ["weather", "orders:read"],
        "status": "active",
    }


def _use_auth_mode(monkeypatch, mode: str, **env) -> None:
    """Make both the router branch and the minting helpers see ``mode``.

    The router binds ``settings`` at import time while the minting helpers call
    ``get_settings()`` fresh; aligning them on one rebuilt, cache-warmed instance
    keeps the two consistent within a test.
    """
    monkeypatch.setenv("AUTH_MODE", mode)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    fresh = get_settings()
    monkeypatch.setattr(users_router, "settings", fresh)


def _patch_user(monkeypatch, doc) -> None:
    async def fake_get_user_raw(user_id):
        return doc

    monkeypatch.setattr(users_service, "get_user_raw", fake_get_user_raw)


# --------------------------------------------------------------------------- #
# hs256: a real scoped bearer the gateway accepts
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hs256_mints_scoped_bearer(reset_settings, monkeypatch):
    _use_auth_mode(monkeypatch, "hs256", JWT_SECRET=JWT_SECRET)
    doc = _user_doc(roles=["user", "tool:invoke"], scopes=["weather", "orders:read"])
    _patch_user(monkeypatch, doc)

    req = _Req(roles=["platform-admin"], tenant_id="tenant-a")
    result = await users_router.mint_user_token(req, "u-1", None)

    assert result.auth_mode == "hs256"
    assert result.token
    assert result.data_plane_ok is True
    assert result.tenant_id == "tenant-b"
    assert result.scopes == ["weather", "orders:read"]
    assert result.expires_in and result.expires_in > 0

    claims = jwt.decode(result.token, JWT_SECRET, algorithms=["HS256"])
    assert claims["sub"] == "demo@demo.com"
    assert claims["tenant_id"] == "tenant-b"
    assert claims["roles"] == ["user", "tool:invoke"]
    # Both claims carry the scopes; the middleware reads ``groups`` first.
    assert claims["groups"] == ["weather", "orders:read"]
    assert claims["scopes"] == ["weather", "orders:read"]


@pytest.mark.asyncio
async def test_hs256_token_accepted_by_middleware_with_scopes(reset_settings, monkeypatch):
    from gateway.middleware.auth import AuthMiddleware

    _use_auth_mode(monkeypatch, "hs256", JWT_SECRET=JWT_SECRET)
    doc = _user_doc(roles=["user", "tool:invoke"], scopes=["weather", "orders:read"])
    _patch_user(monkeypatch, doc)

    req = _Req(roles=["platform-admin"], tenant_id="tenant-a")
    result = await users_router.mint_user_token(req, "u-1", None)

    captured: dict = {}

    async def ok_app(scope, receive, send):
        captured["state"] = scope.get("state", {})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = AuthMiddleware(ok_app)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/rpc",
        "raw_path": b"/rpc",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {result.token}".encode())],
        "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80),
    }

    sent: list = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)

    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 200
    assert captured["state"]["tenant_id"] == "tenant-b"
    assert captured["state"]["scopes"] == ["weather", "orders:read"]


# --------------------------------------------------------------------------- #
# A minted token is a credential: every issuance must be auditable.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mint_writes_audit_log(reset_settings, monkeypatch, caplog):
    _use_auth_mode(monkeypatch, "hs256", JWT_SECRET=JWT_SECRET)
    doc = _user_doc(email="demo@demo.com", tenant_id="tenant-b", roles=["user", "tool:invoke"])
    _patch_user(monkeypatch, doc)

    req = _Req(roles=["platform-admin"], tenant_id="tenant-a", user_id="admin@example.com")
    with caplog.at_level(logging.INFO, logger=users_router.logger.name):
        await users_router.mint_user_token(req, "u-1", None)

    audit = [r for r in caplog.records if "Access token minted" in r.getMessage()]
    assert len(audit) == 1
    message = audit[0].getMessage()
    assert "actor=admin@example.com" in message
    assert "target=demo@demo.com" in message
    assert "tenant=tenant-b" in message
    # The token value itself must never be logged.
    assert "Bearer" not in message and "eyJ" not in message


# --------------------------------------------------------------------------- #
# jwks: roles-only admin-session token + caveat
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_jwks_returns_admin_session_token_with_caveat(reset_settings, monkeypatch):
    _use_auth_mode(
        monkeypatch,
        "jwks",
        JWT_ISSUER="https://idp.example.com",
        JWT_AUDIENCE="mcp-gateway",
        ADMIN_SESSION_SECRET="a-very-long-admin-session-secret",
    )
    doc = _user_doc(roles=["user", "tool:invoke"])
    _patch_user(monkeypatch, doc)

    req = _Req(roles=["platform-admin"], tenant_id="tenant-a")
    result = await users_router.mint_user_token(req, "u-1", None)

    assert result.auth_mode == "jwks"
    assert result.token
    assert result.caveat and "jwks" in result.caveat.lower()
    claims = verify_session(result.token)
    assert claims is not None
    assert claims["tenant_id"] == "tenant-b"
    assert claims["roles"] == ["user", "tool:invoke"]


# --------------------------------------------------------------------------- #
# data_plane_ok reflects whether roles can reach /rpc + /mcp
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_data_plane_ok_false_for_plain_user(reset_settings, monkeypatch):
    _use_auth_mode(monkeypatch, "hs256", JWT_SECRET=JWT_SECRET)
    doc = _user_doc(roles=["user"], scopes=[])
    _patch_user(monkeypatch, doc)

    req = _Req(roles=["platform-admin"], tenant_id="tenant-a")
    result = await users_router.mint_user_token(req, "u-1", None)

    assert result.data_plane_ok is False
    assert result.token  # still minted; it just won't pass the gate
    assert result.caveat and "tool:invoke" in result.caveat


# --------------------------------------------------------------------------- #
# RBAC: tenant-admins are scoped; no cross-tenant or platform-admin minting
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tenant_admin_cross_tenant_forbidden(reset_settings, monkeypatch):
    _use_auth_mode(monkeypatch, "hs256", JWT_SECRET=JWT_SECRET)
    doc = _user_doc(tenant_id="tenant-b")
    _patch_user(monkeypatch, doc)

    req = _Req(roles=["admin"], tenant_id="tenant-a")
    with pytest.raises(HTTPException) as exc:
        await users_router.mint_user_token(req, "u-1", None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_tenant_admin_cannot_mint_for_platform_admin_user(reset_settings, monkeypatch):
    _use_auth_mode(monkeypatch, "hs256", JWT_SECRET=JWT_SECRET)
    doc = _user_doc(tenant_id="tenant-a", roles=["platform-admin"])
    _patch_user(monkeypatch, doc)

    req = _Req(roles=["admin"], tenant_id="tenant-a")
    with pytest.raises(HTTPException) as exc:
        await users_router.mint_user_token(req, "u-1", None)
    assert exc.value.status_code == 403
