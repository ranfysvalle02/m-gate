import jwt
import pytest

from config.settings import get_settings
from gateway.middleware.auth import AuthMiddleware


@pytest.mark.asyncio
async def test_auth_middleware_rejects_missing_token_when_required(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "hs256")
    monkeypatch.setenv("JWT_SECRET", "super-secret-for-tests")
    get_settings.cache_clear()

    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = AuthMiddleware(ok_app)

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/rpc",
        "raw_path": b"/rpc",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80),
    }

    await middleware(scope, receive, send)

    status = next(msg["status"] for msg in sent if msg["type"] == "http.response.start")
    assert status == 401

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_auth_middleware_uses_verified_claim_scopes(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "hs256")
    monkeypatch.setenv("JWT_SECRET", "super-secret-for-tests")
    monkeypatch.setenv("JWT_AUDIENCE", "")
    monkeypatch.setenv("JWT_ISSUER", "")
    get_settings.cache_clear()

    captured = {}

    async def ok_app(scope, receive, send):
        captured["state"] = scope.get("state", {})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = AuthMiddleware(ok_app)
    token = jwt.encode(
        {
            "sub": "u1",
            "tenant_id": "t1",
            "groups": ["orders", "readonly"],
            "roles": ["tool:invoke"],
        },
        "super-secret-for-tests",
        algorithm="HS256",
    )

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/rpc",
        "raw_path": b"/rpc",
        "query_string": b"",
        "headers": [
            (b"authorization", f"Bearer {token}".encode()),
            (b"x-mcp-scopes", b"weather"),
        ],
        "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80),
    }

    await middleware(scope, receive, send)
    status = next(msg["status"] for msg in sent if msg["type"] == "http.response.start")
    assert status == 200
    assert captured["state"]["tenant_id"] == "t1"
    assert captured["state"]["user_id"] == "u1"
    # Header scopes are ignored when auth is enabled; verified token claims win.
    assert captured["state"]["scopes"] == ["orders", "readonly"]

    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    get_settings.cache_clear()
