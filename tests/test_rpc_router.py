"""End-to-end tests for the JSON-RPC router (gateway/routers/rpc.py).

These drive the handler directly with a fake Request and patched Mongo/search,
covering every supported method plus the authorization, cache, timeout, and
pagination paths that previously had zero coverage.
"""

from __future__ import annotations

import pytest

from models.jsonrpc import JsonRpcErrorCode, JsonRpcRequest


class _FakeState:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeRequest:
    """Minimal stand-in for starlette Request as rpc.py uses it."""

    def __init__(
        self,
        *,
        scopes=None,
        roles=None,
        tenant_id="local-dev",
        user_id="u1",
        headers=None,
    ):
        self.state = _FakeState(
            scopes=scopes,
            roles=roles or [],
            tenant_id=tenant_id,
            user_id=user_id,
            request_id="req-123",
        )
        self.headers = headers or {}


@pytest.fixture
def rpc_module(patch_mongo, fake_embeddings, monkeypatch):
    """Import the rpc router with Mongo + embeddings + search faked out."""
    import importlib

    import gateway.routers.rpc as rpc

    rpc = importlib.reload(rpc)

    fake_hybrid = rpc.HybridSearchService(embedding_service=fake_embeddings)
    fake_cache = rpc.SemanticCacheManager(embedding_service=fake_embeddings)
    monkeypatch.setattr(rpc, "get_hybrid_search_service", lambda: fake_hybrid)
    monkeypatch.setattr(rpc, "get_cache_manager", lambda: fake_cache)
    monkeypatch.setattr(rpc, "get_proxy_registry", lambda: _FakeRegistry())
    return rpc


class _FakeRegistry:
    last_call = None

    async def call_tool(self, *, server_name, tool_name, arguments, tenant_id=None):
        _FakeRegistry.last_call = (server_name, tool_name, arguments, tenant_id)
        return {"echo": arguments, "server": server_name, "tool": tool_name}


async def _handle(rpc, method, params, request):
    req = JsonRpcRequest(method=method, params=params, id=1)
    return await rpc.jsonrpc_handler(request, req)


@pytest.mark.asyncio
async def test_initialize_reports_capabilities(rpc_module):
    resp = await _handle(rpc_module, "initialize", {}, _FakeRequest())
    assert resp.error is None
    assert resp.result["protocolVersion"] == "2025-06-18"
    assert resp.result["capabilities"]["tools"]["listChanged"] is True
    assert "catalog_version" in resp.result


@pytest.mark.asyncio
async def test_unsupported_method_returns_method_not_found(rpc_module):
    resp = await _handle(rpc_module, "does/not/exist", {}, _FakeRequest())
    assert resp.error is not None
    assert resp.error.code == int(JsonRpcErrorCode.METHOD_NOT_FOUND)


@pytest.mark.asyncio
async def test_invalid_params_returns_invalid_params(rpc_module):
    # tools/call requires server + name; omit them to trigger ValidationError.
    resp = await _handle(rpc_module, "tools/call", {"arguments": {}}, _FakeRequest())
    assert resp.error is not None
    assert resp.error.code == int(JsonRpcErrorCode.INVALID_PARAMS)


@pytest.mark.asyncio
async def test_tools_call_denied_when_scope_missing(rpc_module, patch_mongo):
    patch_mongo["tool_catalog"].docs.append(
        {"server": "orders", "name": "update_order_status", "scopes": ["orders:write"]}
    )
    request = _FakeRequest(scopes=["orders:read"], roles=[])
    resp = await _handle(
        rpc_module,
        "tools/call",
        {"server": "orders", "name": "update_order_status", "arguments": {}},
        request,
    )
    assert resp.error is not None
    assert resp.error.code == int(JsonRpcErrorCode.FORBIDDEN)
    assert resp.error.data["reason"] == "scope_mismatch"


@pytest.mark.asyncio
async def test_tools_call_allowed_executes_downstream(rpc_module, patch_mongo):
    patch_mongo["tool_catalog"].docs.append(
        {"server": "orders", "name": "find_order", "scopes": ["orders:read"]}
    )
    request = _FakeRequest(scopes=["orders:read"], roles=[])
    resp = await _handle(
        rpc_module,
        "tools/call",
        {"server": "orders", "name": "find_order", "arguments": {"id": 7}},
        request,
    )
    assert resp.error is None
    assert resp.result["echo"] == {"id": 7}
    assert _FakeRegistry.last_call == ("orders", "find_order", {"id": 7}, "local-dev")


@pytest.mark.asyncio
async def test_tools_call_admin_override_bypasses_scope(rpc_module, patch_mongo):
    patch_mongo["tool_catalog"].docs.append(
        {"server": "orders", "name": "find_order", "scopes": ["orders:write"]}
    )
    request = _FakeRequest(scopes=[], roles=["admin"])
    resp = await _handle(
        rpc_module,
        "tools/call",
        {"server": "orders", "name": "find_order", "arguments": {}},
        request,
    )
    assert resp.error is None


@pytest.mark.asyncio
async def test_tools_call_unknown_tenant_returns_clear_error_when_provisioning_disabled(
    rpc_module, patch_mongo
):
    settings = rpc_module.get_settings()
    original = settings.auto_provision_tenants
    object.__setattr__(settings, "auto_provision_tenants", False)
    try:
        resp = await _handle(
            rpc_module,
            "tools/call",
            {"server": "orders", "name": "find_order", "arguments": {}},
            _FakeRequest(scopes=[], tenant_id="never-seen"),
        )
    finally:
        object.__setattr__(settings, "auto_provision_tenants", original)

    assert resp.error is not None
    assert resp.error.code == int(JsonRpcErrorCode.INVALID_REQUEST)
    assert resp.error.data["reason"] == "tenant_not_provisioned"
    assert resp.error.data["tenant_id"] == "never-seen"


@pytest.mark.asyncio
async def test_tools_call_downstream_timeout_returns_upstream_timeout(rpc_module, patch_mongo):
    from services.proxy_registry import DownstreamTimeout

    patch_mongo["tool_catalog"].docs.append(
        {"server": "orders", "name": "find_order", "scopes": []}
    )

    class _TimingOutRegistry:
        async def call_tool(self, *, server_name, tool_name, arguments, tenant_id=None):
            raise DownstreamTimeout("downstream slow")

    rpc_module.get_proxy_registry = lambda: _TimingOutRegistry()
    resp = await _handle(
        rpc_module,
        "tools/call",
        {"server": "orders", "name": "find_order", "arguments": {}},
        _FakeRequest(scopes=[]),
    )
    assert resp.error is not None
    assert resp.error.code == int(JsonRpcErrorCode.UPSTREAM_TIMEOUT)


@pytest.mark.asyncio
async def test_tools_call_cache_hit_skips_downstream(rpc_module, patch_mongo, monkeypatch):
    patch_mongo["tool_catalog"].docs.append(
        {
            "server": "weather",
            "name": "get_forecast",
            "scopes": [],
            "metadata": {"cacheable": True, "cache_ttl_seconds": 3600},
        }
    )

    async def fake_lookup(name, arguments, *, tenant_id):
        return {"cached": True, "city": arguments.get("city")}

    monkeypatch.setattr(rpc_module.get_cache_manager(), "lookup", fake_lookup)

    called = {"downstream": False}

    class _Reg:
        async def call_tool(self, **kwargs):
            called["downstream"] = True
            return {}

    rpc_module.get_proxy_registry = lambda: _Reg()

    resp = await _handle(
        rpc_module,
        "tools/call",
        {"server": "weather", "name": "get_forecast", "arguments": {"city": "NYC"}},
        _FakeRequest(scopes=[]),
    )
    assert resp.error is None
    assert resp.result == {"cached": True, "city": "NYC"}
    assert called["downstream"] is False


@pytest.mark.asyncio
async def test_tools_call_cache_miss_stores_result(rpc_module, patch_mongo, monkeypatch):
    patch_mongo["tool_catalog"].docs.append(
        {
            "server": "weather",
            "name": "get_forecast",
            "scopes": [],
            "metadata": {"cacheable": True, "cache_ttl_seconds": 3600},
        }
    )
    stored = {}

    async def fake_lookup(name, arguments, *, tenant_id):
        return None

    async def fake_store(name, arguments, result, *, tenant_id, ttl_seconds):
        stored["name"] = name
        stored["result"] = result
        stored["ttl"] = ttl_seconds

    monkeypatch.setattr(rpc_module.get_cache_manager(), "lookup", fake_lookup)
    monkeypatch.setattr(rpc_module.get_cache_manager(), "store", fake_store)

    resp = await _handle(
        rpc_module,
        "tools/call",
        {"server": "weather", "name": "get_forecast", "arguments": {"city": "NYC"}},
        _FakeRequest(scopes=[]),
    )
    assert resp.error is None
    assert stored["name"] == "get_forecast"
    assert stored["ttl"] == 3600


@pytest.mark.asyncio
async def test_tools_list_full_catalog_paginates(rpc_module, patch_mongo):
    for i in range(5):
        patch_mongo["tool_catalog"].docs.append(
            {
                "server": "s",
                "name": f"tool_{i}",
                "description": "",
                "scopes": [],
                "input_schema": {},
            }
        )
    # First page of 2 -> next_cursor points past it.
    resp = await _handle(rpc_module, "tools/list", {"limit": 2}, _FakeRequest(scopes=None))
    assert resp.error is None
    assert resp.result["routed"] is False
    assert len(resp.result["tools"]) == 2
    assert resp.result["next_cursor"] == "2"
    # Tools are shaped to MCP descriptors.
    assert "inputSchema" in resp.result["tools"][0]


@pytest.mark.asyncio
async def test_tools_list_routed_when_query_present(rpc_module, patch_mongo, monkeypatch):
    async def fake_search(**kwargs):
        return [{"server": "weather", "name": "get_forecast", "score": 1.0}]

    monkeypatch.setattr(rpc_module.get_hybrid_search_service(), "search_tools", fake_search)
    resp = await _handle(rpc_module, "tools/list", {"query": "forecast"}, _FakeRequest(scopes=None))
    assert resp.error is None
    assert resp.result["routed"] is True
    assert resp.result["tools"][0]["name"] == "get_forecast"


@pytest.mark.asyncio
async def test_tools_search_passes_mode_through(rpc_module, monkeypatch):
    captured = {}

    async def fake_search(**kwargs):
        captured.update(kwargs)
        return [{"server": "weather", "name": "get_forecast"}]

    monkeypatch.setattr(rpc_module.get_hybrid_search_service(), "search_tools", fake_search)
    resp = await _handle(
        rpc_module,
        "tools/search",
        {"query": "rain", "mode": "vector", "limit": 3},
        _FakeRequest(scopes=["weather"]),
    )
    assert resp.error is None
    assert resp.result["mode"] == "vector"
    assert captured["mode"] == "vector"
    assert captured["allowed_scopes"] == ["weather"]
