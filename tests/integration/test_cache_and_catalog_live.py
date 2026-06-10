"""Integration tests for the semantic cache, catalog sync, and index DDL against
a real MongoDB Atlas Local instance.

These exercise the Atlas-only machinery the unit suite can only emulate:
  * semantic cache ``$vectorSearch`` round-trip + tenant isolation on the engine
  * registry-driven catalog sync writing real documents + embeddings
  * ``ensure_tool_catalog_indexes`` DDL being idempotent against a live cluster
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# --------------------------------------------------------------------------
# Semantic cache (real $vectorSearch)
# --------------------------------------------------------------------------


async def _poll_lookup(manager, tool_name, arguments, *, tenant_id, attempts=40):
    """Atlas vector indexes are eventually consistent; poll a freshly stored doc.

    A freshly-created vector index passes through transient states (NOT_STARTED,
    UNKNOWN, …) before it is queryable; ``lookup`` treats those as a miss, so we
    poll long enough (~20s) for mongot to materialize the index.
    """
    for _ in range(attempts):
        hit = await manager.lookup(tool_name, arguments, tenant_id=tenant_id)
        if hit is not None:
            return hit
        await asyncio.sleep(0.5)
    return None


@pytest.fixture
async def cache_manager(live_db, live_embeddings, settings):
    from services.cache_manager import SemanticCacheManager

    mgr = SemanticCacheManager(settings=settings, embedding_service=live_embeddings)
    tenant = f"itest-{uuid.uuid4().hex[:8]}"
    yield mgr, tenant
    # Cleanup: drop everything this test wrote for its isolated tenant.
    await live_db["semantic_cache"].delete_many({"tenant_id": tenant})


async def test_semantic_cache_store_and_vector_lookup(cache_manager, settings):
    mgr, tenant = cache_manager
    tool = "get_forecast"
    args = {"city": "Seattle", "days": 3}
    payload = {"forecast": "rainy", "high": 60}

    await mgr.store(tool, args, payload, tenant_id=tenant, ttl_seconds=3600)

    # Exact same args -> the engine's $vectorSearch should clear the threshold.
    hit = await _poll_lookup(mgr, tool, args, tenant_id=tenant)
    assert hit == payload, "stored result was not recoverable via real $vectorSearch"


async def test_semantic_cache_tenant_isolation_on_engine(cache_manager, live_db):
    mgr, tenant = cache_manager
    tool = "get_forecast"
    args = {"city": "Boston"}
    await mgr.store(tool, args, {"secret": "tenant-a-data"}, tenant_id=tenant, ttl_seconds=3600)
    # Confirm it is retrievable for its own tenant (warms the index).
    assert await _poll_lookup(mgr, tool, args, tenant_id=tenant) is not None

    # A *different* tenant must never see it, even with identical args.
    other = f"itest-{uuid.uuid4().hex[:8]}"
    leaked = await mgr.lookup(tool, args, tenant_id=other)
    assert leaked is None


async def test_semantic_cache_invalidate_removes_entries(cache_manager):
    mgr, tenant = cache_manager
    await mgr.store("find_order", {"order_id": "A1"}, {"status": "shipped"}, tenant_id=tenant)
    assert await _poll_lookup(mgr, "find_order", {"order_id": "A1"}, tenant_id=tenant) is not None

    deleted = await mgr.invalidate(tenant_id=tenant, tool_names=["find_order"])
    assert deleted >= 1
    # After invalidation the entry is gone (no polling — deletion is immediate).
    assert await mgr.lookup("find_order", {"order_id": "A1"}, tenant_id=tenant) is None


# --------------------------------------------------------------------------
# Catalog sync (real upserts + embeddings)
# --------------------------------------------------------------------------


@pytest.fixture
async def temp_server(live_db):
    """A throwaway server name so catalog-sync tests don't disturb the seed."""
    name = f"itest_srv_{uuid.uuid4().hex[:8]}"
    yield name
    await live_db["tool_catalog"].delete_many({"server": name})


async def test_catalog_sync_writes_real_documents_and_embeddings(
    live_db, live_embeddings, temp_server, settings
):
    from services.proxy_registry import InMemoryFastMCPRegistry

    registry = InMemoryFastMCPRegistry(embedding_service=live_embeddings)
    await registry.mount_or_update(
        {
            "server": temp_server,
            "endpoint": "http://unused.invalid/mcp",
            "transport": "streamable_http",
            "enabled": True,
            "metadata": {"scopes": ["itest"]},
            "tools": [
                {
                    "name": "echo_tool",
                    "description": "An integration-test tool that echoes input.",
                    "input_schema": {"type": "object"},
                }
            ],
        }
    )

    doc = await live_db["tool_catalog"].find_one({"server": temp_server, "name": "echo_tool"})
    assert doc is not None
    assert doc["scopes"] == ["itest"]
    # A real embedding of the configured dimensionality was generated and stored.
    assert len(doc["embedding"]) == settings.ollama_dimensions
    assert "schema_hash" in doc


async def test_catalog_sync_is_idempotent_and_reuses_embedding(
    live_db, live_embeddings, temp_server
):
    from services.proxy_registry import InMemoryFastMCPRegistry

    registry = InMemoryFastMCPRegistry(embedding_service=live_embeddings)
    server_doc = {
        "server": temp_server,
        "endpoint": "http://unused.invalid/mcp",
        "transport": "streamable_http",
        "metadata": {"scopes": ["itest"]},
        "tools": [{"name": "stable_tool", "description": "unchanged", "input_schema": {}}],
    }
    await registry.sync_tool_catalog(server_doc)
    first = await live_db["tool_catalog"].find_one({"server": temp_server, "name": "stable_tool"})

    # Re-sync identical doc: schema_hash matches, embedding must be reused verbatim.
    await registry.sync_tool_catalog(server_doc)
    second = await live_db["tool_catalog"].find_one({"server": temp_server, "name": "stable_tool"})
    assert first["embedding"] == second["embedding"]
    assert first["schema_hash"] == second["schema_hash"]


# --------------------------------------------------------------------------
# Index DDL idempotency
# --------------------------------------------------------------------------


async def test_ensure_indexes_is_idempotent(live_db):
    """Re-running index creation against an already-indexed cluster is a no-op."""
    from database.indexes import (
        TEXT_INDEX_NAME,
        VECTOR_INDEX_NAME,
        ensure_tool_catalog_indexes,
    )

    # wait_for_queryable=False keeps this fast; bootstrap already made them ready.
    await ensure_tool_catalog_indexes(wait_for_queryable=False)

    cursor = await live_db["tool_catalog"].list_search_indexes()
    names = {idx["name"] for idx in await cursor.to_list(length=20)}
    assert VECTOR_INDEX_NAME in names
    assert TEXT_INDEX_NAME in names
