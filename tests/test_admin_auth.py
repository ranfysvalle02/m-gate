from __future__ import annotations

import pytest

from gateway.middleware.auth import AuthMiddleware
from gateway.middleware.rbac import RbacMiddleware
from services.admin_session import (
    ADMIN_CSRF_COOKIE,
    ADMIN_SESSION_COOKIE,
    mint_session,
    verify_credentials,
    verify_session,
)


def _scope(path="/admin/servers", method="POST", headers=None):
    return {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80),
    }


class _Sink:
    def __init__(self):
        self.sent = []

    async def receive(self):
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(self, message):
        self.sent.append(message)

    @property
    def status(self):
        return next(m["status"] for m in self.sent if m["type"] == "http.response.start")


def test_verify_credentials(reset_settings, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "demo@demo.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "demo-password")
    assert verify_credentials("demo@demo.com", "demo-password") is True
    assert verify_credentials("demo@demo.com", "wrong") is False


def test_mint_and_verify_session(reset_settings, monkeypatch):
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "a-very-long-admin-secret")
    token = mint_session("demo@demo.com")
    claims = verify_session(token)
    assert claims is not None
    assert claims["sub"] == "demo@demo.com"


def test_mint_session_embeds_tenant_and_roles(reset_settings, monkeypatch):
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "a-very-long-admin-secret")
    token = mint_session("ta@x.com", tenant_id="t1", roles=["admin"])
    claims = verify_session(token)
    assert claims is not None
    assert claims["tenant_id"] == "t1"
    assert claims["roles"] == ["admin"]


@pytest.mark.asyncio
async def test_auth_middleware_accepts_admin_session_cookie(reset_settings, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "hs256")
    monkeypatch.setenv("JWT_SECRET", "super-secret-for-tests")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "a-very-long-admin-secret")

    captured = {}

    async def ok_app(scope, receive, send):
        captured["state"] = scope.get("state", {})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = AuthMiddleware(ok_app)
    token = mint_session("demo@demo.com")
    scope = _scope(
        path="/admin/whoami",
        method="GET",
        headers=[(b"cookie", f"{ADMIN_SESSION_COOKIE}={token}".encode())],
    )
    sink = _Sink()
    await middleware(scope, sink.receive, sink.send)
    assert sink.status == 200
    assert captured["state"]["is_admin_principal"] is True
    assert captured["state"]["admin_auth_via_cookie"] is True


@pytest.mark.asyncio
async def test_auth_middleware_tenant_admin_session_is_admin_principal(reset_settings, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "hs256")
    monkeypatch.setenv("JWT_SECRET", "super-secret-for-tests")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "a-very-long-admin-secret")

    captured = {}

    async def ok_app(scope, receive, send):
        captured["state"] = scope.get("state", {})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = AuthMiddleware(ok_app)
    token = mint_session("ta@x.com", tenant_id="tenant-b", roles=["admin"])
    scope = _scope(
        path="/admin/whoami",
        method="GET",
        headers=[(b"cookie", f"{ADMIN_SESSION_COOKIE}={token}".encode())],
    )
    sink = _Sink()
    await middleware(scope, sink.receive, sink.send)
    assert sink.status == 200
    assert captured["state"]["is_admin_principal"] is True
    assert captured["state"]["roles"] == ["admin"]
    assert captured["state"]["tenant_id"] == "tenant-b"


@pytest.mark.asyncio
async def test_auth_middleware_plain_user_session_is_not_admin_principal(
    reset_settings, monkeypatch
):
    """A non-admin user session authenticates but must not become an admin principal.

    Use a UI path (exempt from the bearer-token requirement) so we can observe the
    hydrated state rather than the 401 a plain user would get on /admin.
    """
    monkeypatch.setenv("AUTH_MODE", "hs256")
    monkeypatch.setenv("JWT_SECRET", "super-secret-for-tests")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "a-very-long-admin-secret")

    captured = {}

    async def ok_app(scope, receive, send):
        captured["state"] = scope.get("state", {})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = AuthMiddleware(ok_app)
    token = mint_session("user@x.com", tenant_id="t1", roles=["user"])
    scope = _scope(
        path="/ui/",
        method="GET",
        headers=[(b"cookie", f"{ADMIN_SESSION_COOKIE}={token}".encode())],
    )
    sink = _Sink()
    await middleware(scope, sink.receive, sink.send)
    assert sink.status == 200
    assert captured["state"]["is_admin_principal"] is False
    assert captured["state"]["roles"] == ["user"]
    assert captured["state"]["tenant_id"] == "t1"


@pytest.mark.asyncio
async def test_rbac_blocks_admin_without_admin_principal(reset_settings):
    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = RbacMiddleware(ok_app)
    scope = _scope(path="/admin/servers", method="GET")
    scope["state"] = {"roles": ["admin"], "is_admin_principal": False}
    sink = _Sink()
    await middleware(scope, sink.receive, sink.send)
    assert sink.status == 401


@pytest.mark.asyncio
async def test_rbac_enforces_csrf_for_cookie_admin(reset_settings):
    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = RbacMiddleware(ok_app)
    scope = _scope(
        path="/admin/servers",
        method="POST",
        headers=[(b"cookie", f"{ADMIN_CSRF_COOKIE}=abc".encode()), (b"x-csrf-token", b"bad")],
    )
    scope["state"] = {
        "roles": ["admin"],
        "is_admin_principal": True,
        "admin_auth_via_cookie": True,
    }
    sink = _Sink()
    await middleware(scope, sink.receive, sink.send)
    assert sink.status == 403


@pytest.mark.asyncio
async def test_rbac_allows_cookie_admin_when_csrf_matches(reset_settings):
    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = RbacMiddleware(ok_app)
    scope = _scope(
        path="/admin/servers",
        method="POST",
        headers=[(b"cookie", f"{ADMIN_CSRF_COOKIE}=good".encode()), (b"x-csrf-token", b"good")],
    )
    scope["state"] = {
        "roles": ["admin"],
        "is_admin_principal": True,
        "admin_auth_via_cookie": True,
    }
    sink = _Sink()
    await middleware(scope, sink.receive, sink.send)
    assert sink.status == 200
