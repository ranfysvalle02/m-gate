import asyncio

import pytest

import services.tenant_provisioner as tenant_provisioner
from config.settings import get_settings
from services.cache_manager import SemanticCacheManager
from services.embeddings import TtlLruCache
from services.hybrid_search import HybridSearchService
from services.tenant_provisioner import ensure_tenant_ready


@pytest.mark.load
@pytest.mark.asyncio
async def test_rrf_fusion_smoke_under_concurrency():
    vector_docs = [{"server": "orders", "name": f"find_order_{i}"} for i in range(10)]
    text_docs = list(reversed(vector_docs))

    async def _run_once():
        fused = HybridSearchService._fuse_rrf(
            vector_docs=vector_docs,
            text_docs=text_docs,
            vector_weight=0.5,
            text_weight=0.5,
            output_limit=5,
            include_score_details=False,
        )
        assert len(fused) == 5

    await asyncio.gather(*[_run_once() for _ in range(100)])


@pytest.mark.load
@pytest.mark.asyncio
async def test_semantic_cache_ensure_index_is_single_flight(
    patch_mongo, fake_embeddings, monkeypatch
):
    manager = SemanticCacheManager(embedding_service=fake_embeddings)
    collection = patch_mongo["semantic_cache"]
    calls = {"create": 0}
    original_create = collection.create_search_index

    async def _counting_create(*args, **kwargs):
        calls["create"] += 1
        await asyncio.sleep(0.01)
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(collection, "create_search_index", _counting_create)
    await asyncio.gather(
        *[
            manager._ensure_index(tenant_id="local-dev", collection=collection)  # noqa: SLF001
            for _ in range(30)
        ]
    )
    assert calls["create"] == 1


@pytest.mark.load
@pytest.mark.asyncio
async def test_ensure_tenant_ready_single_flight_under_concurrency(patch_mongo, monkeypatch):
    settings = get_settings()
    object.__setattr__(settings, "auto_provision_tenants", True)
    tenant_provisioner.reset_ready_tenant_cache()
    calls = {"provision": 0}

    async def _fake_provision(tenant_id, **kwargs):
        calls["provision"] += 1
        await asyncio.sleep(0.01)
        return tenant_id

    monkeypatch.setattr(tenant_provisioner, "provision_tenant", _fake_provision)
    await asyncio.gather(
        *[ensure_tenant_ready("burst-tenant", settings=settings) for _ in range(30)]
    )
    assert calls["provision"] == 1


@pytest.mark.load
@pytest.mark.asyncio
async def test_ttl_lru_cache_thread_safe_and_respects_ttl():
    cache = TtlLruCache(max_entries=2, ttl_seconds=0.05)
    cache.set("a", [1.0])
    cache.set("b", [2.0])
    assert cache.get("a") == [1.0]
    cache.set("c", [3.0])
    assert cache.get("b") is None
    assert cache.get("c") == [3.0]

    def _churn():
        for index in range(500):
            key = f"k-{index % 5}"
            cache.set(key, [float(index)])
            cache.get(key)

    await asyncio.gather(*[asyncio.to_thread(_churn) for _ in range(4)])
    await asyncio.sleep(0.06)
    assert cache.get("a") is None
