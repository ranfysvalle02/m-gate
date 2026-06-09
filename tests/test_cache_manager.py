"""Tests for the semantic cache manager: similarity threshold gating, tenant
and tool isolation, store round-trip, and invalidation.
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from services.cache_manager import (
    SEMANTIC_CACHE_FILTER_FIELDS,
    SemanticCacheManager,
    semantic_cache_index_spec,
    semantic_cache_lookup_filter,
)


@pytest.fixture
def manager(patch_mongo, fake_embeddings):
    settings = Settings(semantic_cache_threshold=0.95)
    return SemanticCacheManager(settings=settings, embedding_service=fake_embeddings)


@pytest.mark.asyncio
async def test_lookup_returns_none_when_below_threshold(manager, patch_mongo, monkeypatch):
    # Force the vector-search handler to return a hit below threshold.
    def handler(pipeline):
        return [
            {
                "tenant_id": "local-dev",
                "embedding_version": manager.embedding_version,
                "tool_name": "get_forecast",
                "result": {"x": 1},
                "score": 0.5,
            }
        ]

    patch_mongo["semantic_cache"]._aggregate_handler = handler
    out = await manager.lookup("get_forecast", {"city": "NYC"}, tenant_id="local-dev")
    assert out is None


@pytest.mark.asyncio
async def test_lookup_returns_result_above_threshold(manager, patch_mongo):
    def handler(pipeline):
        return [
            {
                "tenant_id": "local-dev",
                "embedding_version": manager.embedding_version,
                "tool_name": "get_forecast",
                "result": {"temp": 72},
                "score": 0.99,
            }
        ]

    patch_mongo["semantic_cache"]._aggregate_handler = handler
    out = await manager.lookup("get_forecast", {"city": "NYC"}, tenant_id="local-dev")
    assert out == {"temp": 72}


@pytest.mark.asyncio
async def test_lookup_enforces_tenant_isolation(manager, patch_mongo):
    def handler(pipeline):
        # A high-score hit belonging to a *different* tenant must be rejected.
        return [
            {
                "tenant_id": "other",
                "embedding_version": manager.embedding_version,
                "tool_name": "get_forecast",
                "result": {"leak": True},
                "score": 0.99,
            }
        ]

    patch_mongo["semantic_cache"]._aggregate_handler = handler
    out = await manager.lookup("get_forecast", {"city": "NYC"}, tenant_id="local-dev")
    assert out is None


@pytest.mark.asyncio
async def test_lookup_enforces_tool_isolation(manager, patch_mongo):
    def handler(pipeline):
        return [
            {
                "tenant_id": "local-dev",
                "embedding_version": manager.embedding_version,
                "tool_name": "a_different_tool",
                "result": {"wrong": True},
                "score": 0.99,
            }
        ]

    patch_mongo["semantic_cache"]._aggregate_handler = handler
    out = await manager.lookup("get_forecast", {"city": "NYC"}, tenant_id="local-dev")
    assert out is None


@pytest.mark.asyncio
async def test_lookup_enforces_embedding_version_isolation(manager, patch_mongo):
    def handler(pipeline):
        return [
            {
                "tenant_id": "local-dev",
                "embedding_version": "stale-model:8",
                "tool_name": "get_forecast",
                "result": {"wrong": True},
                "score": 0.99,
            }
        ]

    patch_mongo["semantic_cache"]._aggregate_handler = handler
    out = await manager.lookup("get_forecast", {"city": "NYC"}, tenant_id="local-dev")
    assert out is None


@pytest.mark.asyncio
async def test_store_then_invalidate(manager, patch_mongo):
    await manager.store(
        "get_forecast", {"city": "NYC"}, {"temp": 72}, tenant_id="local-dev", ttl_seconds=3600
    )
    assert len(patch_mongo["semantic_cache"].docs) == 1

    deleted = await manager.invalidate(tenant_id="local-dev", tool_names=["get_forecast"])
    assert deleted == 1
    assert patch_mongo["semantic_cache"].docs == []


@pytest.mark.asyncio
async def test_store_stamps_embedding_provenance(manager, patch_mongo):
    await manager.store(
        "get_forecast",
        {"city": "NYC"},
        {"temp": 72},
        tenant_id="local-dev",
        ttl_seconds=3600,
    )
    [doc] = patch_mongo["semantic_cache"].docs
    assert doc["embedding_model"] == manager.embedding_model
    assert doc["embedding_dim"] == manager.embedding_dim
    assert doc["embedding_version"] == manager.embedding_version


@pytest.mark.asyncio
async def test_lookup_pipeline_filters_tenant_and_embedding_version(manager, patch_mongo):
    captured = {}

    def handler(pipeline):
        captured["pipeline"] = pipeline
        return []

    patch_mongo["semantic_cache"]._aggregate_handler = handler
    out = await manager.lookup("get_forecast", {"city": "NYC"}, tenant_id="local-dev")
    assert out is None
    filter_doc = captured["pipeline"][0]["$vectorSearch"]["filter"]
    assert filter_doc == semantic_cache_lookup_filter(
        tenant_id="local-dev",
        embedding_version=manager.embedding_version,
    )


def test_semantic_cache_index_spec_declares_all_filter_fields():
    spec = semantic_cache_index_spec(embedding_version="foo:8", dimensions=8)
    filter_paths = {
        field["path"] for field in spec["definition"]["fields"] if field["type"] == "filter"
    }
    assert set(SEMANTIC_CACHE_FILTER_FIELDS).issubset(filter_paths)


@pytest.mark.asyncio
async def test_invalidate_empty_list_is_noop(manager):
    assert await manager.invalidate(tenant_id="local-dev", tool_names=[]) == 0
