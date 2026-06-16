"""Cover the MCP tool closures (search_tools / list_catalog_tools /
call_downstream_tool), the proxy registry's FastMCP client transport behavior,
and the Pydantic registry models.
"""

from __future__ import annotations

import pytest

from models.registry import RoutingRegistryDocument, ToolDocument

# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


def test_tool_document_defaults():
    doc = ToolDocument(server="weather", name="get_forecast")
    assert doc.description == ""
    assert doc.scopes == []
    assert doc.embedding == []


def test_routing_registry_document_nests_tools():
    doc = RoutingRegistryDocument(
        server="weather",
        endpoint="http://weather/mcp",
        transport="streamable_http",
        tools=[{"server": "weather", "name": "get_forecast"}],
    )
    assert doc.enabled is True
    assert doc.tools[0].name == "get_forecast"


# --------------------------------------------------------------------------
# proxy_registry FastMCP transport
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_downstream_call_tool_success(patch_mongo, fake_embeddings):
    from services.proxy_registry import DownstreamServer, InMemoryFastMCPRegistry

    reg = InMemoryFastMCPRegistry(embedding_service=fake_embeddings)
    reg._servers[("local-dev", "weather")] = DownstreamServer(
        tenant_id="local-dev",
        server="weather",
        transport="streamable_http",
        endpoint="https://weather:8101/mcp",
    )

    async def _ok(**kwargs):
        return {"temp": 70}

    reg._call_via_client = _ok  # type: ignore[method-assign]

    result = await reg.call_tool("weather", "get_forecast", {"city": "NYC"})
    assert result == {"temp": 70}


@pytest.mark.asyncio
async def test_downstream_error_raises_downstream_error(patch_mongo, fake_embeddings):
    from services.proxy_registry import DownstreamError, DownstreamServer, InMemoryFastMCPRegistry

    reg = InMemoryFastMCPRegistry(embedding_service=fake_embeddings)
    reg._servers[("local-dev", "weather")] = DownstreamServer(
        tenant_id="local-dev",
        server="weather",
        transport="streamable_http",
        endpoint="https://weather:8101/mcp",
    )

    async def _raise(*args, **kwargs):
        raise DownstreamError("boom")

    reg._call_via_client = _raise  # type: ignore[method-assign]

    with pytest.raises(DownstreamError):
        await reg.call_tool("weather", "get_forecast", {})


@pytest.mark.asyncio
async def test_discover_tools_parses_tools_list(patch_mongo, fake_embeddings):
    from services.proxy_registry import DownstreamServer, InMemoryFastMCPRegistry

    reg = InMemoryFastMCPRegistry(embedding_service=fake_embeddings)
    reg._servers[("local-dev", "weather")] = DownstreamServer(
        tenant_id="local-dev",
        server="weather",
        transport="streamable_http",
        endpoint="https://weather:8101/mcp",
    )

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def list_tools(self):
            return [{"name": "get_forecast", "description": "forecast", "inputSchema": {}}]

    reg._build_client = lambda server, **_kwargs: _Client()  # type: ignore[method-assign]
    tools = await reg.discover_tools("weather")
    assert tools == [{"name": "get_forecast", "description": "forecast", "input_schema": {}}]


# --------------------------------------------------------------------------
# MCP tool closures
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tools_closure_shapes_response(patch_mongo, monkeypatch, reset_settings):
    import gateway.mcp_server as mcp_mod

    async def fake_search(**kwargs):
        return [{"name": "get_forecast", "server": "weather"}]

    monkeypatch.setattr(mcp_mod.hybrid_search_service, "search_tools", fake_search)

    # Build a fresh server and pull the registered tool functions out.
    captured = {}

    class _Recorder:
        def tool(self, fn):
            captured[fn.__name__] = fn
            return fn

    mcp_mod._register_tools(_Recorder())
    out = await captured["search_tools"](query="rain", limit=3, mode="vector")
    assert out["query"] == "rain"
    assert out["mode"] == "vector"
    assert out["items"][0]["name"] == "get_forecast"


def _capture_tools(mcp_mod):
    captured = {}

    class _Recorder:
        def tool(self, fn):
            captured[fn.__name__] = fn
            return fn

    mcp_mod._register_tools(_Recorder())
    return captured


class _FakeRequest:
    """Stand-in for the Starlette request the gateway middleware stamps."""

    def __init__(
        self, *, tenant_id, roles=None, scopes=None, user_id="user@tenant", request_id="req-1"
    ):
        class _State:
            pass

        self.state = _State()
        self.state.tenant_id = tenant_id
        self.state.roles = roles or []
        self.state.scopes = scopes or []
        self.state.user_id = user_id
        self.state.request_id = request_id


class _RecordingTelemetry:
    """Captures audit rows synchronously so tests can assert what was logged.

    The real logger fires telemetry as a background task; recording inline keeps
    assertions deterministic without racing the event loop.
    """

    def __init__(self):
        self.events = []

    def log_background(self, **kwargs):
        self.events.append(kwargs)


@pytest.mark.asyncio
async def test_call_downstream_tool_closure(patch_mongo, monkeypatch, reset_settings):
    import gateway.mcp_server as mcp_mod

    # The /mcp surface now authorizes per call at parity with /rpc, so the tool
    # must exist in the tenant catalog. In disabled mode the caller is admin, so
    # the admin override applies once the tool is found.
    patch_mongo["tool_catalog"].docs.append({"server": "weather", "name": "get_forecast"})

    class _Reg:
        async def call_tool(
            self, *, server_name, tool_name, arguments, tenant_id=None, caller=None
        ):
            return {"ok": True}

    monkeypatch.setattr(mcp_mod, "get_proxy_registry", lambda: _Reg())

    captured = _capture_tools(mcp_mod)
    out = await captured["call_downstream_tool"](
        server="weather", name="get_forecast", arguments={}
    )
    assert out["result"] == {"ok": True}
    assert out["server"] == "weather"


@pytest.mark.asyncio
async def test_call_downstream_tool_rejects_cross_tenant(patch_mongo, monkeypatch, reset_settings):
    from fastmcp.exceptions import ToolError

    import gateway.mcp_server as mcp_mod
    from config.settings import get_settings

    monkeypatch.setenv("AUTH_MODE", "hs256")
    get_settings.cache_clear()

    # Verified identity is tenant-a, but the caller passes tenant-b: the override
    # must be refused before any execution.
    monkeypatch.setattr(
        mcp_mod, "get_http_request", lambda: _FakeRequest(tenant_id="tenant-a", roles=["admin"])
    )

    class _Reg:
        called = False

        async def call_tool(
            self, *, server_name, tool_name, arguments, tenant_id=None, caller=None
        ):
            _Reg.called = True
            return {"ok": True}

    monkeypatch.setattr(mcp_mod, "get_proxy_registry", lambda: _Reg())

    captured = _capture_tools(mcp_mod)
    with pytest.raises(ToolError):
        await captured["call_downstream_tool"](
            server="weather", name="get_forecast", arguments={}, tenant_id="tenant-b"
        )
    assert _Reg.called is False


@pytest.mark.asyncio
async def test_call_downstream_tool_authz_denied_without_scope(
    patch_mongo, monkeypatch, reset_settings
):
    from fastmcp.exceptions import ToolError

    import gateway.mcp_server as mcp_mod
    from config.settings import get_settings
    from database.mongo import get_tenant_database

    monkeypatch.setenv("AUTH_MODE", "hs256")
    get_settings.cache_clear()

    get_tenant_database("tenant-a")["tool_catalog"].docs.append(
        {"server": "weather", "name": "get_forecast", "scopes": ["weather:read"]}
    )
    # tool:invoke clears the coarse RBAC gate but carries no server scope.
    monkeypatch.setattr(
        mcp_mod,
        "get_http_request",
        lambda: _FakeRequest(tenant_id="tenant-a", roles=["tool:invoke"], scopes=[]),
    )

    class _Reg:
        called = False

        async def call_tool(
            self, *, server_name, tool_name, arguments, tenant_id=None, caller=None
        ):
            _Reg.called = True
            return {"ok": True}

    monkeypatch.setattr(mcp_mod, "get_proxy_registry", lambda: _Reg())

    captured = _capture_tools(mcp_mod)
    with pytest.raises(ToolError):
        await captured["call_downstream_tool"](server="weather", name="get_forecast", arguments={})
    assert _Reg.called is False


@pytest.mark.asyncio
async def test_call_downstream_tool_authz_allowed_with_scope(
    patch_mongo, monkeypatch, reset_settings
):
    import gateway.mcp_server as mcp_mod
    from config.settings import get_settings
    from database.mongo import get_tenant_database

    monkeypatch.setenv("AUTH_MODE", "hs256")
    get_settings.cache_clear()

    get_tenant_database("tenant-a")["tool_catalog"].docs.append(
        {"server": "weather", "name": "get_forecast"}
    )
    # The tenant comes from the verified claim (tenant-a); the caller carries the
    # server scope, so authorization passes and the call executes against tenant-a.
    monkeypatch.setattr(
        mcp_mod,
        "get_http_request",
        lambda: _FakeRequest(
            tenant_id="tenant-a", roles=["tool:invoke"], scopes=["server:weather"]
        ),
    )

    seen = {}

    class _Reg:
        async def call_tool(
            self, *, server_name, tool_name, arguments, tenant_id=None, caller=None
        ):
            seen["tenant_id"] = tenant_id
            return {"ok": True}

    monkeypatch.setattr(mcp_mod, "get_proxy_registry", lambda: _Reg())

    captured = _capture_tools(mcp_mod)
    out = await captured["call_downstream_tool"](
        server="weather", name="get_forecast", arguments={}
    )
    assert out["result"] == {"ok": True}
    assert seen["tenant_id"] == "tenant-a"


@pytest.mark.asyncio
async def test_call_downstream_tool_blocked_when_over_quota(
    patch_mongo, monkeypatch, reset_settings
):
    """Quota is enforced on /mcp at parity with /rpc: an over-quota tenant is
    refused before the downstream hop, and the block is audited."""
    from fastmcp.exceptions import ToolError

    import gateway.mcp_server as mcp_mod
    from config.settings import get_settings
    from database.mongo import get_tenant_database
    from services.usage_metering import record_usage

    monkeypatch.setenv("AUTH_MODE", "hs256")
    get_settings.cache_clear()
    settings = get_settings()
    object.__setattr__(settings, "default_quota_calls_per_period", 1)

    get_tenant_database("tenant-a")["tool_catalog"].docs.append(
        {"server": "weather", "name": "get_forecast"}
    )
    await record_usage("tenant-a", calls=1)  # already at the limit

    monkeypatch.setattr(
        mcp_mod,
        "get_http_request",
        lambda: _FakeRequest(tenant_id="tenant-a", roles=["admin"]),
    )
    recording = _RecordingTelemetry()
    monkeypatch.setattr(mcp_mod, "get_telemetry_logger", lambda: recording)

    class _Reg:
        called = False

        async def call_tool(
            self, *, server_name, tool_name, arguments, tenant_id=None, caller=None
        ):
            _Reg.called = True
            return {"ok": True}

    monkeypatch.setattr(mcp_mod, "get_proxy_registry", lambda: _Reg())

    captured = _capture_tools(mcp_mod)
    with pytest.raises(ToolError, match="quota_exceeded"):
        await captured["call_downstream_tool"](server="weather", name="get_forecast", arguments={})

    assert _Reg.called is False
    statuses = [event["status"] for event in recording.events]
    assert "quota_exceeded" in statuses


@pytest.mark.asyncio
async def test_call_downstream_tool_meters_and_audits_on_success(
    patch_mongo, monkeypatch, reset_settings
):
    """A successful /mcp call records billable usage and writes an audit row,
    closing the gap where /mcp calls were neither metered nor audited."""
    import gateway.mcp_server as mcp_mod
    from config.settings import get_settings
    from database.mongo import get_tenant_database
    from services.usage_metering import get_usage

    monkeypatch.setenv("AUTH_MODE", "hs256")
    get_settings.cache_clear()

    get_tenant_database("tenant-a")["tool_catalog"].docs.append(
        {"server": "weather", "name": "get_forecast"}
    )
    monkeypatch.setattr(
        mcp_mod,
        "get_http_request",
        lambda: _FakeRequest(
            tenant_id="tenant-a",
            roles=["tool:invoke"],
            scopes=["server:weather"],
            user_id="alice@tenant-a",
            request_id="req-42",
        ),
    )
    recording = _RecordingTelemetry()
    monkeypatch.setattr(mcp_mod, "get_telemetry_logger", lambda: recording)

    captured_caller = {}

    class _Reg:
        async def call_tool(
            self, *, server_name, tool_name, arguments, tenant_id=None, caller=None
        ):
            captured_caller["caller"] = caller
            return {"ok": True}

    monkeypatch.setattr(mcp_mod, "get_proxy_registry", lambda: _Reg())

    captured = _capture_tools(mcp_mod)
    out = await captured["call_downstream_tool"](
        server="weather", name="get_forecast", arguments={}
    )
    assert out["result"] == {"ok": True}

    # Billable usage was recorded for the verified tenant.
    usage = await get_usage("tenant-a")
    assert usage["calls"] == 1

    # The downstream hop received the caller identity (for credential brokering).
    assert captured_caller["caller"] is not None
    assert captured_caller["caller"].user_id == "alice@tenant-a"

    # A success row was audited with the same method label /rpc uses.
    success = [e for e in recording.events if e["status"] == "live_execution_success"]
    assert len(success) == 1
    assert success[0]["method"] == "tools/call"
    assert success[0]["tenant_id"] == "tenant-a"
    assert success[0]["user_id"] == "alice@tenant-a"
    assert success[0]["request_id"] == "req-42"


@pytest.mark.asyncio
async def test_call_downstream_tool_blocked_when_tenant_suspended(
    patch_mongo, monkeypatch, reset_settings
):
    import gateway.mcp_server as mcp_mod
    from services.tenant_status import TenantSuspendedError

    control = patch_mongo._control_db
    await control["tenants"].insert_one(
        {"tenant_id": "local-dev", "db_name": "db", "status": "suspended", "suspended_reason": "x"}
    )

    class _Reg:
        called = False

        async def call_tool(
            self, *, server_name, tool_name, arguments, tenant_id=None, caller=None
        ):
            _Reg.called = True
            return {"ok": True}

    monkeypatch.setattr(mcp_mod, "get_proxy_registry", lambda: _Reg())
    captured = {}

    class _Recorder:
        def tool(self, fn):
            captured[fn.__name__] = fn
            return fn

    mcp_mod._register_tools(_Recorder())
    with pytest.raises(TenantSuspendedError):
        await captured["call_downstream_tool"](
            server="weather", name="get_forecast", arguments={}, tenant_id="local-dev"
        )
    # The suspended tenant never reaches the execution layer.
    assert _Reg.called is False


@pytest.mark.asyncio
async def test_call_downstream_tool_rejected_by_sandbox_preflight(
    patch_mongo, monkeypatch, reset_settings
):
    from fastmcp.exceptions import ToolError

    import gateway.mcp_server as mcp_mod
    from config.settings import get_settings

    # 1s sandbox quota = 1000ms remaining; the code tool's 5000ms wall budget
    # cannot fit, so the shared preflight must reject before execution -- exactly
    # as the /rpc data plane does (DESIGN.md parity guarantee).
    settings = get_settings()
    object.__setattr__(settings, "default_quota_sandbox_seconds_per_period", 1)
    object.__setattr__(settings, "quota_preflight_enabled", True)

    patch_mongo["tool_catalog"].docs.append(
        {
            "server": "my-funcs",
            "name": "add",
            "metadata": {"transport": "code", "wall_timeout_ms": 5000},
        }
    )

    class _Reg:
        called = False

        async def call_tool(
            self, *, server_name, tool_name, arguments, tenant_id=None, caller=None
        ):
            _Reg.called = True
            return {"ok": True}

    monkeypatch.setattr(mcp_mod, "get_proxy_registry", lambda: _Reg())

    captured = _capture_tools(mcp_mod)
    with pytest.raises(ToolError, match="sandbox_quota_preflight"):
        await captured["call_downstream_tool"](server="my-funcs", name="add", arguments={})
    # The sandbox/downstream is never reached.
    assert _Reg.called is False


@pytest.mark.asyncio
async def test_mcp_discovery_blocked_when_tenant_suspended(
    patch_mongo, monkeypatch, reset_settings
):
    import gateway.mcp_server as mcp_mod
    from services.tenant_status import TenantSuspendedError

    control = patch_mongo._control_db
    await control["tenants"].insert_one(
        {"tenant_id": "local-dev", "db_name": "db", "status": "suspended"}
    )

    searched = {"called": False}

    async def fake_search(**kwargs):
        searched["called"] = True
        return []

    monkeypatch.setattr(mcp_mod.hybrid_search_service, "search_tools", fake_search)
    captured = {}

    class _Recorder:
        def tool(self, fn):
            captured[fn.__name__] = fn
            return fn

    mcp_mod._register_tools(_Recorder())
    # Discovery surfaces are blocked too, so a suspended tenant sees nothing.
    with pytest.raises(TenantSuspendedError):
        await captured["search_tools"](query="x", tenant_id="local-dev")
    with pytest.raises(TenantSuspendedError):
        await captured["list_catalog_tools"](tenant_id="local-dev")
    assert searched["called"] is False
