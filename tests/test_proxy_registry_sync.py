"""Tests for InMemoryFastMCPRegistry catalog sync, mount/unmount, and
JSON-RPC discovery — the parts that talk to Mongo and downstream servers.
"""

from __future__ import annotations

import pytest

from services.proxy_registry import InMemoryFastMCPRegistry


@pytest.fixture
def registry(patch_mongo, fake_embeddings):
    return InMemoryFastMCPRegistry(embedding_service=fake_embeddings)


@pytest.mark.asyncio
async def test_mount_or_update_registers_server_and_syncs_catalog(registry, patch_mongo):
    await registry.mount_or_update(
        {
            "server": "weather",
            "endpoint": "http://weather:8101/mcp",
            "transport": "streamable_http",
            "enabled": True,
            "tools": [
                {"name": "get_forecast", "description": "forecast", "input_schema": {}},
            ],
            "metadata": {"scopes": ["weather"]},
        }
    )
    assert "weather" in registry.list_servers()
    docs = patch_mongo["tool_catalog"].docs
    assert len(docs) == 1
    assert docs[0]["name"] == "get_forecast"
    assert docs[0]["scopes"] == ["weather"]
    assert docs[0]["embedding"]  # embedding was generated


@pytest.mark.asyncio
async def test_sync_reuses_embedding_when_schema_unchanged(registry, patch_mongo, fake_embeddings):
    server_doc = {
        "server": "weather",
        "endpoint": "http://weather:8101/mcp",
        "transport": "streamable_http",
        "tools": [{"name": "get_forecast", "description": "forecast", "input_schema": {}}],
        "metadata": {"scopes": ["weather"]},
    }
    await registry.mount_or_update(server_doc)
    calls_after_first = len(fake_embeddings.calls)

    # Re-sync identical doc: schema_hash matches -> no new embedding call.
    await registry.sync_tool_catalog(server_doc)
    assert len(fake_embeddings.calls) == calls_after_first


@pytest.mark.asyncio
async def test_sync_reembeds_when_description_changes(registry, patch_mongo, fake_embeddings):
    base = {
        "server": "weather",
        "endpoint": "http://weather:8101/mcp",
        "transport": "streamable_http",
        "tools": [{"name": "get_forecast", "description": "v1", "input_schema": {}}],
        "metadata": {"scopes": ["weather"]},
    }
    await registry.mount_or_update(base)
    calls_after_first = len(fake_embeddings.calls)

    changed = dict(base)
    changed["tools"] = [{"name": "get_forecast", "description": "v2 changed", "input_schema": {}}]
    await registry.sync_tool_catalog(changed)
    assert len(fake_embeddings.calls) > calls_after_first


@pytest.mark.asyncio
async def test_unmount_removes_server_and_catalog(registry, patch_mongo):
    await registry.mount_or_update(
        {
            "server": "weather",
            "endpoint": "http://weather:8101/mcp",
            "transport": "streamable_http",
            "tools": [{"name": "get_forecast", "description": "f", "input_schema": {}}],
            "metadata": {"scopes": ["weather"]},
        }
    )
    await registry.unmount("weather")
    assert "weather" not in registry.list_servers()
    assert patch_mongo["tool_catalog"].docs == []


@pytest.mark.asyncio
async def test_call_tool_unknown_server_raises_keyerror(registry):
    with pytest.raises(KeyError):
        await registry.call_tool("ghost", "noop", {})


@pytest.mark.asyncio
async def test_discover_tools_returns_empty_on_unreachable(registry):
    await registry.mount_or_update(
        {
            "server": "ghost",
            "endpoint": "http://nonexistent.invalid/mcp",
            "transport": "streamable_http",
            "enabled": True,
        }
    )
    tools = await registry.discover_tools("ghost")
    assert tools == []
