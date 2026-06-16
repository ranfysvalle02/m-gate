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


@pytest.mark.parametrize("path", ["/health/live", "/health/ready", "/health", "/metrics"])
@pytest.mark.asyncio
async def test_auth_middleware_exempts_probes_and_metrics(monkeypatch, path):
    """Liveness/readiness probes and the metrics scrape must succeed without a token
    even when auth is required, so k8s probes and Prometheus work in prod auth modes.
    """
    monkeypatch.setenv("AUTH_MODE", "jwks")
    monkeypatch.setenv("JWT_ISSUER", "iss")
    monkeypatch.setenv("JWT_AUDIENCE", "aud")
    monkeypatch.setenv("JWKS_URI", "https://issuer/jwks")
    get_settings.cache_clear()

    reached = {"app": False}

    async def ok_app(scope, receive, send):
        reached["app"] = True
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
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80),
    }

    await middleware(scope, receive, send)
    status = next(msg["status"] for msg in sent if msg["type"] == "http.response.start")
    assert status == 200
    assert reached["app"] is True

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
        ],
        "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80),
    }

    await middleware(scope, receive, send)
    status = next(msg["status"] for msg in sent if msg["type"] == "http.response.start")
    assert status == 200
    assert captured["state"]["tenant_id"] == "t1"
    assert captured["state"]["user_id"] == "u1"
    # Scopes always come from the verified token claims.
    assert captured["state"]["scopes"] == ["orders", "readonly"]

    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_auth_middleware_viewer_bearer_is_read_only_principal(monkeypatch):
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
        {"sub": "viewer@x", "tenant_id": "t1", "roles": ["viewer"]},
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
        "method": "GET",
        "scheme": "http",
        "path": "/admin/tenants",
        "raw_path": b"/admin/tenants",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80),
    }

    await middleware(scope, receive, send)
    state = captured["state"]
    # A viewer reaches the console (admin principal) but is flagged read-only so
    # RbacMiddleware refuses its mutations.
    assert state["is_admin_principal"] is True
    assert state["is_read_only_principal"] is True

    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_auth_middleware_admin_keeps_write_even_with_viewer_role(monkeypatch):
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
        {"sub": "admin@x", "tenant_id": "t1", "roles": ["admin", "viewer"]},
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
        "method": "GET",
        "scheme": "http",
        "path": "/admin/tenants",
        "raw_path": b"/admin/tenants",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 5000),
        "server": ("testserver", 80),
    }

    await middleware(scope, receive, send)
    state = captured["state"]
    # A full admin that also carries the viewer role must NOT be downgraded to
    # read-only — an accidental viewer grant can never lock an admin out of writes.
    assert state["is_admin_principal"] is True
    assert state["is_read_only_principal"] is False

    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("JWT_AUDIENCE", raising=False)
    monkeypatch.delenv("JWT_ISSUER", raising=False)
    get_settings.cache_clear()
