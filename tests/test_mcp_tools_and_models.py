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
        endpoint="http://weather:8101/mcp",
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
        endpoint="http://weather:8101/mcp",
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
        endpoint="http://weather:8101/mcp",
    )

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def list_tools(self):
            return [{"name": "get_forecast", "description": "forecast", "inputSchema": {}}]

    reg._build_client = lambda server: _Client()  # type: ignore[method-assign]
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


@pytest.mark.asyncio
async def test_call_downstream_tool_closure(patch_mongo, monkeypatch, reset_settings):
    import gateway.mcp_server as mcp_mod

    class _Reg:
        async def call_tool(self, *, server_name, tool_name, arguments, tenant_id=None):
            return {"ok": True}

    monkeypatch.setattr(mcp_mod, "get_proxy_registry", lambda: _Reg())

    captured = {}

    class _Recorder:
        def tool(self, fn):
            captured[fn.__name__] = fn
            return fn

    mcp_mod._register_tools(_Recorder())
    out = await captured["call_downstream_tool"](
        server="weather", name="get_forecast", arguments={}
    )
    assert out["result"] == {"ok": True}
    assert out["server"] == "weather"
