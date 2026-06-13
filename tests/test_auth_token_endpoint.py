from __future__ import annotations

import base64
import json

import pytest
from starlette.requests import Request

import gateway.routers.auth as auth_router
from gateway.middleware.auth import AuthMiddleware
from gateway.routers.auth import issue_token, protected_resource_metadata
from services.admin_session import mint_session, verify_session


def _request(method: str, path: str, *, body: bytes = b"", headers=None) -> Request:
    scope = {
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

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _body(response) -> dict:
    return json.loads(bytes(response.body).decode("utf-8"))


def _scope(path: str, method: str = "POST", headers=None) -> dict:
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

    @property
    def headers(self) -> dict[str, str]:
        start = next(m for m in self.sent if m["type"] == "http.response.start")
        return {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}


# --------------------------------------------------------------------------- #
# POST /auth/token (OAuth2 password grant)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_token_endpoint_returns_bearer_for_valid_credentials(reset_settings, monkeypatch):
    async def fake_resolve(email, password):
        assert (email, password) == ("svc@x.com", "pw")
        return {"email": "svc@x.com", "tenant_id": "tenant-b", "roles": ["tool:invoke"]}

    monkeypatch.setattr(auth_router, "resolve_login_principal", fake_resolve)
    request = _request(
        "POST",
        "/auth/token",
        body=b"grant_type=password&username=svc@x.com&password=pw",
        headers=[(b"content-type", b"application/x-www-form-urlencoded")],
    )
    response = await issue_token(request)
    assert response.status_code == 200
    data = _body(response)
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0
    claims = verify_session(data["access_token"])
    assert claims is not None
    assert claims["sub"] == "svc@x.com"
    assert claims["tenant_id"] == "tenant-b"
    assert claims["roles"] == ["tool:invoke"]


@pytest.mark.asyncio
async def test_token_endpoint_accepts_json_body(reset_settings, monkeypatch):
    async def fake_resolve(email, password):
        return {"email": email, "tenant_id": "local-dev", "roles": ["admin"]}

    monkeypatch.setattr(auth_router, "resolve_login_principal", fake_resolve)
    request = _request(
        "POST",
        "/auth/token",
        body=json.dumps({"username": "a@b.com", "password": "pw"}).encode(),
        headers=[(b"content-type", b"application/json")],
    )
    response = await issue_token(request)
    assert response.status_code == 200
    assert _body(response)["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_token_endpoint_rejects_bad_credentials(reset_settings, monkeypatch):
    async def fake_resolve(email, password):
        return None

    monkeypatch.setattr(auth_router, "resolve_login_principal", fake_resolve)
    request = _request(
        "POST",
        "/auth/token",
        body=b"grant_type=password&username=a@b.com&password=wrong",
        headers=[(b"content-type", b"application/x-www-form-urlencoded")],
    )
    response = await issue_token(request)
    assert response.status_code == 401
    assert _body(response)["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_token_endpoint_rejects_unsupported_grant(reset_settings):
    request = _request(
        "POST",
        "/auth/token",
        body=b"grant_type=client_credentials&username=a&password=b",
        headers=[(b"content-type", b"application/x-www-form-urlencoded")],
    )
    response = await issue_token(request)
    assert response.status_code == 400
    assert _body(response)["error"] == "unsupported_grant_type"


@pytest.mark.asyncio
async def test_token_endpoint_requires_username_and_password(reset_settings):
    request = _request(
        "POST",
        "/auth/token",
        body=b"grant_type=password&username=&password=",
        headers=[(b"content-type", b"application/x-www-form-urlencoded")],
    )
    response = await issue_token(request)
    assert response.status_code == 400
    assert _body(response)["error"] == "invalid_request"


# --------------------------------------------------------------------------- #
# Issued bearer is accepted by AuthMiddleware on the MCP surface
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_issued_bearer_authorizes_rpc(reset_settings, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "hs256")
    monkeypatch.setenv("JWT_SECRET", "super-secret-for-tests")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "a-very-long-admin-secret")

    captured = {}

    async def ok_app(scope, receive, send):
        captured["state"] = scope.get("state", {})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = AuthMiddleware(ok_app)
    token = mint_session("svc@x.com", tenant_id="tenant-b", roles=["admin"])
    scope = _scope(
        path="/rpc",
        method="POST",
        headers=[(b"authorization", f"Bearer {token}".encode())],
    )
    sink = _Sink()
    await middleware(scope, sink.receive, sink.send)
    assert sink.status == 200
    assert captured["state"]["is_admin_principal"] is True
    assert captured["state"]["tenant_id"] == "tenant-b"


@pytest.mark.asyncio
async def test_auth_token_path_is_public_under_hs256(reset_settings, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "hs256")
    monkeypatch.setenv("JWT_SECRET", "super-secret-for-tests")

    reached = {}

    async def ok_app(scope, receive, send):
        reached["ok"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = AuthMiddleware(ok_app)
    sink = _Sink()
    await middleware(_scope("/auth/token", "POST"), sink.receive, sink.send)
    assert reached.get("ok") is True
    assert sink.status == 200


# --------------------------------------------------------------------------- #
# OAuth discovery (RFC 9728 Protected Resource Metadata)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_protected_resource_metadata_present_under_jwks(reset_settings, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "jwks")
    monkeypatch.setenv("JWT_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("JWT_AUDIENCE", "mcp-gateway")
    response = await protected_resource_metadata(
        _request("GET", "/.well-known/oauth-protected-resource")
    )
    assert response.status_code == 200
    data = _body(response)
    assert data["authorization_servers"] == ["https://idp.example.com"]
    assert data["audience"] == "mcp-gateway"
    assert data["resource"].startswith("http://")


@pytest.mark.asyncio
async def test_protected_resource_metadata_404_when_disabled(reset_settings, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setenv("OAUTH_METADATA_ENABLED", "false")
    response = await protected_resource_metadata(
        _request("GET", "/.well-known/oauth-protected-resource")
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Optional HTTP Basic on the MCP surface
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_basic_auth_on_mcp_authenticates_principal(reset_settings, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "hs256")
    monkeypatch.setenv("JWT_SECRET", "super-secret-for-tests")
    monkeypatch.setenv("MCP_BASIC_AUTH_ENABLED", "true")

    async def fake_resolve(email, password):
        if (email, password) == ("svc@x.com", "pw"):
            return {"email": "svc@x.com", "tenant_id": "tenant-b", "roles": ["tool:invoke"]}
        return None

    monkeypatch.setattr("gateway.middleware.auth.resolve_login_principal", fake_resolve)

    captured = {}

    async def ok_app(scope, receive, send):
        captured["state"] = scope.get("state", {})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = AuthMiddleware(ok_app)
    creds = base64.b64encode(b"svc@x.com:pw").decode()
    scope = _scope("/rpc", "POST", headers=[(b"authorization", f"Basic {creds}".encode())])
    sink = _Sink()
    await middleware(scope, sink.receive, sink.send)
    assert sink.status == 200
    assert captured["state"]["authenticated_via_basic"] is True
    assert captured["state"]["tenant_id"] == "tenant-b"
    assert captured["state"]["roles"] == ["tool:invoke"]


@pytest.mark.asyncio
async def test_basic_auth_on_mcp_rejects_bad_credentials(reset_settings, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "hs256")
    monkeypatch.setenv("JWT_SECRET", "super-secret-for-tests")
    monkeypatch.setenv("MCP_BASIC_AUTH_ENABLED", "true")

    async def fake_resolve(email, password):
        return None

    monkeypatch.setattr("gateway.middleware.auth.resolve_login_principal", fake_resolve)

    async def ok_app(scope, receive, send):  # pragma: no cover - must not be reached
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = AuthMiddleware(ok_app)
    creds = base64.b64encode(b"svc@x.com:wrong").decode()
    scope = _scope("/rpc", "POST", headers=[(b"authorization", f"Basic {creds}".encode())])
    sink = _Sink()
    await middleware(scope, sink.receive, sink.send)
    assert sink.status == 401
    assert "basic" in sink.headers.get("www-authenticate", "").lower()


@pytest.mark.asyncio
async def test_basic_auth_on_mcp_ignored_when_auth_disabled(reset_settings, monkeypatch):
    # In disabled mode the gateway trusts every caller, so a stray (even invalid)
    # Basic header must not turn an open deployment into a 401 machine.
    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setenv("MCP_BASIC_AUTH_ENABLED", "true")

    async def fail_resolve(email, password):  # pragma: no cover - must not be called
        raise AssertionError("Basic auth must not run in disabled mode")

    monkeypatch.setattr("gateway.middleware.auth.resolve_login_principal", fail_resolve)

    reached = {}

    async def ok_app(scope, receive, send):
        reached["ok"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = AuthMiddleware(ok_app)
    creds = base64.b64encode(b"svc@x.com:wrong").decode()
    scope = _scope("/rpc", "POST", headers=[(b"authorization", f"Basic {creds}".encode())])
    sink = _Sink()
    await middleware(scope, sink.receive, sink.send)
    assert reached.get("ok") is True
    assert sink.status == 200
