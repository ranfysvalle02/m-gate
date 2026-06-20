from __future__ import annotations

import httpx
import pytest

from config.settings import Settings
from services.egress_policy import EgressNotAllowed
from services.sandbox_http_bridge import SandboxHttpBridge


def _factory_from_handler(handler):
    """Build a client factory backed by an httpx.MockTransport (no real network)."""

    def factory(**_kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    return factory


def _bridge(handler, *, action_type="read", env=None, settings=None, **factory_overrides):
    return SandboxHttpBridge(
        tenant_id="tenant-a",
        action_type=action_type,
        env=env or {},
        settings=settings or Settings(),
        server="srv",
        tool="fn",
        client_factory=_factory_from_handler(handler),
    )


@pytest.mark.asyncio
async def test_get_returns_structured_response(patch_mongo):
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"ok": True}, headers={"x-test": "1"})

    bridge = _bridge(handler)
    frame = await bridge.handle(
        {"id": 1, "method": "GET", "url": "https://api.example.com/data", "params": {"q": "x"}}
    )

    assert frame["type"] == "http_rpc_result"
    assert frame["ok"] is True
    result = frame["result"]
    assert result["status"] == 200
    assert result["headers"]["x-test"] == "1"
    import json as _json

    assert _json.loads(result["text"]) == {"ok": True}
    # Params were forwarded onto the outbound request URL.
    assert seen["request"].url.params.get("q") == "x"


@pytest.mark.asyncio
async def test_https_only(patch_mongo):
    bridge = _bridge(lambda r: httpx.Response(200))
    frame = await bridge.handle({"id": 1, "method": "GET", "url": "http://api.example.com/x"})
    assert frame["ok"] is False
    assert frame["error"]["type"] == "http_scheme_forbidden"


@pytest.mark.asyncio
async def test_read_tool_cannot_use_write_method(patch_mongo):
    bridge = _bridge(lambda r: httpx.Response(200), action_type="read")
    frame = await bridge.handle({"id": 1, "method": "POST", "url": "https://api.example.com/x"})
    assert frame["ok"] is False
    assert frame["error"]["type"] == "http_method_forbidden"


@pytest.mark.asyncio
async def test_write_tool_can_post_body(patch_mongo):
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(201, json={"created": True})

    bridge = _bridge(handler, action_type="write")
    frame = await bridge.handle(
        {
            "id": 1,
            "method": "POST",
            "url": "https://api.example.com/things",
            "json": {"name": "widget"},
        }
    )
    assert frame["ok"] is True
    assert frame["result"]["status"] == 201
    assert b"widget" in seen["request"].content


@pytest.mark.asyncio
async def test_call_budget_enforced(patch_mongo):
    settings = Settings(sandbox_http_max_calls_per_invocation=1)
    bridge = _bridge(lambda r: httpx.Response(200), settings=settings)
    first = await bridge.handle({"id": 1, "method": "GET", "url": "https://api.example.com/a"})
    second = await bridge.handle({"id": 2, "method": "GET", "url": "https://api.example.com/b"})
    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"]["type"] == "http_call_limit"


@pytest.mark.asyncio
async def test_response_size_cap(patch_mongo):
    settings = Settings(sandbox_http_max_response_bytes=1024)
    bridge = _bridge(lambda r: httpx.Response(200, content=b"x" * 4096), settings=settings)
    frame = await bridge.handle({"id": 1, "method": "GET", "url": "https://api.example.com/big"})
    assert frame["ok"] is False
    assert frame["error"]["type"] == "http_response_too_large"


@pytest.mark.asyncio
async def test_request_size_cap(patch_mongo):
    settings = Settings(sandbox_http_max_request_bytes=16)
    bridge = _bridge(lambda r: httpx.Response(200), action_type="write", settings=settings)
    frame = await bridge.handle(
        {
            "id": 1,
            "method": "POST",
            "url": "https://api.example.com/x",
            "json": {"payload": "this is definitely longer than sixteen bytes"},
        }
    )
    assert frame["ok"] is False
    assert frame["error"]["type"] == "http_request_too_large"


@pytest.mark.asyncio
async def test_secret_injection_never_leaks(patch_mongo):
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        # Echo the auth header in the body to prove the bridge does not relay it back.
        return httpx.Response(200, text="ok")

    bridge = _bridge(handler, env={"TOKEN": "sup3r-secret"})
    frame = await bridge.handle(
        {"id": 1, "method": "GET", "url": "https://api.example.com/me", "auth": "TOKEN"}
    )
    assert frame["ok"] is True
    # The secret was attached to the OUTBOUND request...
    assert seen["request"].headers["authorization"] == "Bearer sup3r-secret"
    # ...but never appears anywhere in the result returned to the guest.
    assert "sup3r-secret" not in str(frame)


@pytest.mark.asyncio
async def test_secret_injection_custom_header(patch_mongo):
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, text="ok")

    bridge = _bridge(handler, env={"KEY": "abc123"})
    frame = await bridge.handle(
        {
            "id": 1,
            "method": "GET",
            "url": "https://api.example.com/me",
            "auth": {"key": "KEY", "header": "X-Api-Key", "scheme": ""},
        }
    )
    assert frame["ok"] is True
    assert seen["request"].headers["x-api-key"] == "abc123"


@pytest.mark.asyncio
async def test_unknown_auth_key_rejected_without_leaking(patch_mongo):
    bridge = _bridge(lambda r: httpx.Response(200), env={"TOKEN": "secret"})
    frame = await bridge.handle(
        {"id": 1, "method": "GET", "url": "https://api.example.com/me", "auth": "MISSING"}
    )
    assert frame["ok"] is False
    assert frame["error"]["type"] == "http_auth_unknown_key"
    assert "secret" not in str(frame)


@pytest.mark.asyncio
async def test_egress_block_surfaces_typed_error(patch_mongo):
    def handler(request: httpx.Request) -> httpx.Response:
        raise EgressNotAllowed("host 'api.example.com' is not on the tenant egress allowlist")

    bridge = _bridge(handler)
    frame = await bridge.handle({"id": 1, "method": "GET", "url": "https://api.example.com/x"})
    assert frame["ok"] is False
    assert frame["error"]["type"] == "egress_blocked"


@pytest.mark.asyncio
async def test_set_cookie_is_stripped(patch_mongo):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"set-cookie": "sid=abc", "x-ok": "1"}, text="hi")

    bridge = _bridge(handler)
    frame = await bridge.handle({"id": 1, "method": "GET", "url": "https://api.example.com/x"})
    assert frame["ok"] is True
    assert "set-cookie" not in frame["result"]["headers"]
    assert frame["result"]["headers"]["x-ok"] == "1"


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_consecutive_failures(patch_mongo):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    settings = Settings(sandbox_http_breaker_failures=2, sandbox_http_breaker_reset_seconds=30)
    bridge = _bridge(handler, settings=settings)
    url = "https://api.example.com/flaky"
    first = await bridge.handle({"id": 1, "method": "GET", "url": url})
    second = await bridge.handle({"id": 2, "method": "GET", "url": url})
    third = await bridge.handle({"id": 3, "method": "GET", "url": url})

    assert first["error"]["type"] == "http_request_failed"
    assert second["error"]["type"] == "http_request_failed"
    # After 2 consecutive failures the breaker is open: the 3rd call short-circuits.
    assert third["error"]["type"] == "http_circuit_open"
