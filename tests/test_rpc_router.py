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

    async def call_tool(self, *, server_name, tool_name, arguments, tenant_id=None, caller=None):
        _FakeRegistry.last_call = (server_name, tool_name, arguments, tenant_id, caller)
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
    request = _FakeRequest(scopes=["orders:read", "server:orders"], roles=[])
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
async def test_tools_call_code_tool_execution_disabled(rpc_module, patch_mongo):
    # Code-backed tools remain discoverable even when execution is feature-flagged
    # off; tools/call must return a clear, protocol-safe disabled error.
    patch_mongo["tool_catalog"].docs.append(
        {
            "server": "my-funcs",
            "name": "add",
            "scopes": ["math:run"],
            "metadata": {"transport": "code"},
        }
    )
    request = _FakeRequest(scopes=["math:run", "server:my-funcs"], roles=[])
    resp = await _handle(
        rpc_module,
        "tools/call",
        {"server": "my-funcs", "name": "add", "arguments": {"a": 1, "b": 2}},
        request,
    )
    assert resp.error is not None
    assert resp.error.code == int(JsonRpcErrorCode.SERVER_ERROR)
    assert resp.error.data["reason"] == "code_execution_not_enabled"
    # The downstream proxy must never have been invoked.
    assert _FakeRegistry.last_call is None or _FakeRegistry.last_call[1] != "add"


@pytest.mark.asyncio
async def test_tools_call_code_tool_execution_enabled_calls_registry(rpc_module, patch_mongo):
    patch_mongo["tool_catalog"].docs.append(
        {
            "server": "my-funcs",
            "name": "add",
            "scopes": ["math:run"],
            "metadata": {"transport": "code"},
        }
    )
    settings = rpc_module.get_settings()
    original = settings.code_tool_execution_enabled
    object.__setattr__(settings, "code_tool_execution_enabled", True)
    try:
        request = _FakeRequest(scopes=["math:run", "server:my-funcs"], roles=[])
        resp = await _handle(
            rpc_module,
            "tools/call",
            {"server": "my-funcs", "name": "add", "arguments": {"a": 1, "b": 2}},
            request,
        )
    finally:
        object.__setattr__(settings, "code_tool_execution_enabled", original)

    assert resp.error is None
    assert _FakeRegistry.last_call[0:4] == ("my-funcs", "add", {"a": 1, "b": 2}, "local-dev")


@pytest.mark.asyncio
async def test_tools_call_code_tool_rejected_by_sandbox_preflight(rpc_module, patch_mongo):
    # A code tool whose per-tool wall budget cannot fit the remaining sandbox
    # quota is rejected up front, before the sandbox/downstream is ever invoked.
    _FakeRegistry.last_call = None
    patch_mongo["tool_catalog"].docs.append(
        {
            "server": "my-funcs",
            "name": "add",
            "scopes": ["math:run"],
            "metadata": {"transport": "code", "wall_timeout_ms": 5000},
        }
    )
    settings = rpc_module.get_settings()
    original_exec = settings.code_tool_execution_enabled
    original_sandbox = settings.default_quota_sandbox_seconds_per_period
    object.__setattr__(settings, "code_tool_execution_enabled", True)
    # 1s sandbox quota = 1000ms remaining; the tool's 5000ms budget cannot fit.
    object.__setattr__(settings, "default_quota_sandbox_seconds_per_period", 1)
    try:
        request = _FakeRequest(scopes=["math:run", "server:my-funcs"], roles=[])
        resp = await _handle(
            rpc_module,
            "tools/call",
            {"server": "my-funcs", "name": "add", "arguments": {"a": 1, "b": 2}},
            request,
        )
    finally:
        object.__setattr__(settings, "code_tool_execution_enabled", original_exec)
        object.__setattr__(settings, "default_quota_sandbox_seconds_per_period", original_sandbox)

    assert resp.error is not None
    assert resp.error.code == int(JsonRpcErrorCode.RATE_LIMITED)
    assert resp.error.data["reason"] == "sandbox_quota_preflight"
    assert resp.error.data["projected_ms"] == 5000
    # The sandbox/downstream is never reached.
    assert _FakeRegistry.last_call is None


@pytest.mark.asyncio
async def test_tools_call_code_tool_admitted_when_projection_fits(rpc_module, patch_mongo):
    _FakeRegistry.last_call = None
    patch_mongo["tool_catalog"].docs.append(
        {
            "server": "my-funcs",
            "name": "add",
            "scopes": ["math:run"],
            "metadata": {"transport": "code", "wall_timeout_ms": 500},
        }
    )
    settings = rpc_module.get_settings()
    original_exec = settings.code_tool_execution_enabled
    original_sandbox = settings.default_quota_sandbox_seconds_per_period
    object.__setattr__(settings, "code_tool_execution_enabled", True)
    # 1s = 1000ms remaining; the tool's 500ms budget fits, so it proceeds.
    object.__setattr__(settings, "default_quota_sandbox_seconds_per_period", 1)
    try:
        request = _FakeRequest(scopes=["math:run", "server:my-funcs"], roles=[])
        resp = await _handle(
            rpc_module,
            "tools/call",
            {"server": "my-funcs", "name": "add", "arguments": {"a": 1, "b": 2}},
            request,
        )
    finally:
        object.__setattr__(settings, "code_tool_execution_enabled", original_exec)
        object.__setattr__(settings, "default_quota_sandbox_seconds_per_period", original_sandbox)

    assert resp.error is None
    assert _FakeRegistry.last_call[0:2] == ("my-funcs", "add")


@pytest.mark.asyncio
async def test_tools_call_quota_exceeded_returns_rate_limited(rpc_module, patch_mongo):
    import services.usage_metering as usage_metering

    _FakeRegistry.last_call = None
    patch_mongo["tool_catalog"].docs.append(
        {"server": "orders", "name": "find_order", "scopes": ["orders:read"]}
    )
    await usage_metering.record_usage("local-dev", calls=1)
    settings = rpc_module.get_settings()
    original_calls = settings.default_quota_calls_per_period
    object.__setattr__(settings, "default_quota_calls_per_period", 1)
    try:
        request = _FakeRequest(scopes=["orders:read", "server:orders"], roles=[])
        resp = await _handle(
            rpc_module,
            "tools/call",
            {"server": "orders", "name": "find_order", "arguments": {"id": 7}},
            request,
        )
    finally:
        object.__setattr__(settings, "default_quota_calls_per_period", original_calls)

    assert resp.error is not None
    assert resp.error.code == int(JsonRpcErrorCode.RATE_LIMITED)
    assert resp.error.data["reason"] == "quota_exceeded"
    assert _FakeRegistry.last_call is None


@pytest.mark.asyncio
async def test_tools_call_allowed_executes_downstream(rpc_module, patch_mongo):
    import services.usage_metering as usage_metering

    _FakeRegistry.last_call = None
    patch_mongo["tool_catalog"].docs.append(
        {"server": "orders", "name": "find_order", "scopes": ["orders:read"]}
    )
    request = _FakeRequest(scopes=["orders:read", "server:orders"], roles=[])
    resp = await _handle(
        rpc_module,
        "tools/call",
        {"server": "orders", "name": "find_order", "arguments": {"id": 7}},
        request,
    )
    assert resp.error is None
    assert resp.result["echo"] == {"id": 7}
    assert _FakeRegistry.last_call[0:4] == ("orders", "find_order", {"id": 7}, "local-dev")
    assert _FakeRegistry.last_call[4] is not None
    usage = await usage_metering.get_usage("local-dev")
    assert usage["calls"] == 1


@pytest.mark.asyncio
async def test_tools_call_requires_confirmation_creates_pending_action(rpc_module, patch_mongo):
    _FakeRegistry.last_call = None
    patch_mongo["tool_catalog"].docs.append(
        {
            "server": "orders",
            "name": "delete_order",
            "scopes": ["orders:write"],
            "metadata": {"requires_confirmation": True, "action_type": "destructive"},
        }
    )
    request = _FakeRequest(
        scopes=["orders:write", "server:orders"],
        roles=[],
        user_id="requester",
    )
    resp = await _handle(
        rpc_module,
        "tools/call",
        {"server": "orders", "name": "delete_order", "arguments": {"id": 42}},
        request,
    )
    assert resp.error is None
    assert resp.result["status"] == "confirmation_required"
    assert resp.result["confirmation"]["action_id"]
    assert _FakeRegistry.last_call is None
    docs = patch_mongo["pending_actions"].docs
    assert len(docs) == 1
    assert docs[0]["status"] == "pending"
    assert docs[0]["tool"] == "delete_order"


@pytest.mark.asyncio
async def test_tools_call_with_approved_confirmation_executes_downstream(rpc_module, patch_mongo):
    _FakeRegistry.last_call = None
    patch_mongo["tool_catalog"].docs.append(
        {
            "server": "orders",
            "name": "delete_order",
            "scopes": ["orders:write"],
            "metadata": {"requires_confirmation": True, "action_type": "destructive"},
        }
    )
    request = _FakeRequest(
        scopes=["orders:write", "server:orders"],
        roles=[],
        user_id="requester",
    )
    first = await _handle(
        rpc_module,
        "tools/call",
        {"server": "orders", "name": "delete_order", "arguments": {"id": 42}},
        request,
    )
    action_id = first.result["confirmation"]["action_id"]
    patch_mongo["pending_actions"].docs[0]["status"] = "approved"

    second = await _handle(
        rpc_module,
        "tools/call",
        {
            "server": "orders",
            "name": "delete_order",
            "arguments": {"id": 42},
            "confirmation_id": action_id,
        },
        request,
    )
    assert second.error is None
    assert second.result["echo"] == {"id": 42}
    assert _FakeRegistry.last_call is not None
    assert patch_mongo["pending_actions"].docs[0]["status"] == "consumed"


@pytest.mark.asyncio
async def test_tools_call_confirmation_mismatch_returns_forbidden(rpc_module, patch_mongo):
    _FakeRegistry.last_call = None
    patch_mongo["tool_catalog"].docs.append(
        {
            "server": "orders",
            "name": "delete_order",
            "scopes": ["orders:write"],
            "metadata": {"requires_confirmation": True, "action_type": "destructive"},
        }
    )
    request = _FakeRequest(
        scopes=["orders:write", "server:orders"],
        roles=[],
        user_id="requester",
    )
    first = await _handle(
        rpc_module,
        "tools/call",
        {"server": "orders", "name": "delete_order", "arguments": {"id": 42}},
        request,
    )
    action_id = first.result["confirmation"]["action_id"]
    patch_mongo["pending_actions"].docs[0]["status"] = "approved"

    second = await _handle(
        rpc_module,
        "tools/call",
        {
            "server": "orders",
            "name": "delete_order",
            "arguments": {"id": 99},
            "confirmation_id": action_id,
        },
        request,
    )
    assert second.error is not None
    assert second.error.code == int(JsonRpcErrorCode.FORBIDDEN)
    assert second.error.data["reason"] == "mismatch"
    assert _FakeRegistry.last_call is None


@pytest.mark.asyncio
async def test_tools_call_confirmation_not_approved_returns_confirmation_required(
    rpc_module, patch_mongo
):
    _FakeRegistry.last_call = None
    patch_mongo["tool_catalog"].docs.append(
        {
            "server": "orders",
            "name": "delete_order",
            "scopes": ["orders:write"],
            "metadata": {"requires_confirmation": True, "action_type": "destructive"},
        }
    )
    request = _FakeRequest(
        scopes=["orders:write", "server:orders"],
        roles=[],
        user_id="requester",
    )
    first = await _handle(
        rpc_module,
        "tools/call",
        {"server": "orders", "name": "delete_order", "arguments": {"id": 42}},
        request,
    )
    action_id = first.result["confirmation"]["action_id"]
    patch_mongo["pending_actions"].docs[0]["status"] = "rejected"

    second = await _handle(
        rpc_module,
        "tools/call",
        {
            "server": "orders",
            "name": "delete_order",
            "arguments": {"id": 42},
            "confirmation_id": action_id,
        },
        request,
    )
    assert second.error is None
    assert second.result["status"] == "confirmation_required"
    assert second.result["reason"] == "not_approved"
    assert _FakeRegistry.last_call is None


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
        async def call_tool(
            self, *, server_name, tool_name, arguments, tenant_id=None, caller=None
        ):
            raise DownstreamTimeout("downstream slow")

    rpc_module.get_proxy_registry = lambda: _TimingOutRegistry()
    resp = await _handle(
        rpc_module,
        "tools/call",
        {"server": "orders", "name": "find_order", "arguments": {}},
        _FakeRequest(scopes=["server:orders"]),
    )
    assert resp.error is not None
    assert resp.error.code == int(JsonRpcErrorCode.UPSTREAM_TIMEOUT)


@pytest.mark.asyncio
async def test_tools_call_cache_hit_skips_downstream(rpc_module, patch_mongo, monkeypatch):
    import services.usage_metering as usage_metering

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
        _FakeRequest(scopes=["server:weather"]),
    )
    assert resp.error is None
    assert resp.result == {"cached": True, "city": "NYC"}
    assert called["downstream"] is False
    usage = await usage_metering.get_usage("local-dev")
    assert usage["calls"] == 1


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
        _FakeRequest(scopes=["server:weather"]),
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
        _FakeRequest(scopes=["weather", "server:weather"]),
    )
    assert resp.error is None
    assert resp.result["mode"] == "vector"
    assert captured["mode"] == "vector"
    assert captured["allowed_scopes"] == ["weather", "server:weather"]


@pytest.mark.asyncio
async def test_tools_call_blocked_when_tenant_suspended(rpc_module, patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one(
        {
            "tenant_id": "local-dev",
            "db_name": "db",
            "status": "suspended",
            "suspended_reason": "abuse",
        }
    )
    resp = await _handle(
        rpc_module,
        "tools/call",
        {"server": "orders", "name": "do", "arguments": {}},
        _FakeRequest(tenant_id="local-dev", scopes=["anything"]),
    )
    assert resp.error is not None
    assert resp.error.code == int(JsonRpcErrorCode.FORBIDDEN)
    assert resp.error.data["reason"] == "tenant_suspended"
    assert resp.error.data["detail"] == "abuse"
    # The kill-switch fires before authz/quota, so no downstream call happens.
    assert _FakeRegistry.last_call is None or _FakeRegistry.last_call[1] != "do"


@pytest.mark.asyncio
async def test_tools_list_blocked_when_tenant_suspended(rpc_module, patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one(
        {"tenant_id": "local-dev", "db_name": "db", "status": "suspended"}
    )
    resp = await _handle(rpc_module, "tools/list", {}, _FakeRequest(tenant_id="local-dev"))
    assert resp.error is not None
    assert resp.error.code == int(JsonRpcErrorCode.FORBIDDEN)
    assert resp.error.data["reason"] == "tenant_suspended"


@pytest.mark.asyncio
async def test_non_tenant_scoped_method_ignores_suspension(rpc_module, patch_mongo):
    # `initialize` is not tenant-scoped, so a suspended tenant can still handshake.
    control = patch_mongo._control_db
    await control["tenants"].insert_one(
        {"tenant_id": "local-dev", "db_name": "db", "status": "suspended"}
    )
    resp = await _handle(rpc_module, "initialize", {}, _FakeRequest(tenant_id="local-dev"))
    assert resp.error is None


@pytest.mark.asyncio
async def test_tools_call_blocked_when_tenant_deleted(rpc_module, patch_mongo):
    # A soft-deleted tenant is locked out on the hot path with a deleted-specific
    # reason, distinct from suspension, before any authz/quota/downstream work.
    control = patch_mongo._control_db
    await control["tenants"].insert_one(
        {"tenant_id": "local-dev", "db_name": "db", "status": "deleted"}
    )
    resp = await _handle(
        rpc_module,
        "tools/call",
        {"server": "orders", "name": "do", "arguments": {}},
        _FakeRequest(tenant_id="local-dev", scopes=["anything"]),
    )
    assert resp.error is not None
    assert resp.error.code == int(JsonRpcErrorCode.FORBIDDEN)
    assert resp.error.data["reason"] == "tenant_deleted"


@pytest.mark.asyncio
async def test_admin_suspend_then_rpc_blocked_then_resume(rpc_module, patch_mongo, monkeypatch):
    # End-to-end wiring: the admin endpoint and the /rpc enforcement share the same
    # control plane + status cache, so a suspend takes effect on the next call and a
    # resume restores access.
    import gateway.routers.admin as admin

    class _Telemetry:
        def log_background(self, **kwargs):
            return None

    monkeypatch.setattr(admin._common, "get_telemetry_logger", lambda: _Telemetry())

    control = patch_mongo._control_db
    await control["tenants"].insert_one(
        {"tenant_id": "local-dev", "db_name": "db", "status": "active"}
    )
    platform_admin = _FakeRequest(tenant_id="local-dev", roles=[admin.settings.platform_admin_role])

    await admin.suspend_tenant(platform_admin, "local-dev", None)
    blocked = await _handle(rpc_module, "tools/list", {}, _FakeRequest(tenant_id="local-dev"))
    assert blocked.error is not None
    assert blocked.error.data["reason"] == "tenant_suspended"

    await admin.resume_tenant(platform_admin, "local-dev")
    restored = await _handle(rpc_module, "tools/list", {}, _FakeRequest(tenant_id="local-dev"))
    assert restored.error is None
