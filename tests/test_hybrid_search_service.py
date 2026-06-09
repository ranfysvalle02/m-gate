"""Service-level tests for HybridSearchService.search_tools / list_tools.

These exercise the runtime branching (mode selection, embedding-failure
fallback to lexical, $rankFusion OperationFailure -> app-side RRF) using the
fake DB + a lexical-overlap aggregate handler, complementing the existing
pure pipeline-shape tests.
"""

from __future__ import annotations

import pytest
from pymongo.errors import OperationFailure

from config.settings import Settings
from services.hybrid_search import HybridSearchService

CATALOG = [
    {
        "server": "weather",
        "name": "get_forecast",
        "description": "weather forecast for a city",
        "scopes": ["weather"],
        "input_schema": {},
        "embedding": [0.0],
    },
    {
        "server": "orders",
        "name": "find_order",
        "description": "look up an order by id",
        "scopes": ["orders"],
        "input_schema": {},
        "embedding": [0.0],
    },
]


@pytest.fixture
def service(patch_mongo, fake_embeddings):
    from fakes import lexical_overlap_handler

    catalog = patch_mongo["tool_catalog"]
    catalog.docs.extend(CATALOG)
    catalog._aggregate_handler = lexical_overlap_handler(lambda: catalog.docs)
    return HybridSearchService(settings=Settings(), embedding_service=fake_embeddings)


@pytest.mark.asyncio
async def test_text_mode_skips_embedding(service, fake_embeddings):
    results = await service.search_tools(query="forecast", mode="text", limit=5)
    assert any(r["name"] == "get_forecast" for r in results)
    # Lexical-only path must not call the embedding service.
    assert fake_embeddings.calls == []


@pytest.mark.asyncio
async def test_vector_mode_uses_embedding(service, fake_embeddings):
    await service.search_tools(query="forecast", mode="vector", limit=5)
    assert fake_embeddings.calls  # embedding was requested


@pytest.mark.asyncio
async def test_hybrid_mode_returns_ranked_results(service):
    results = await service.search_tools(query="order by id", mode="hybrid", limit=5)
    # "find_order" should rank first for an order-centric query.
    assert results[0]["name"] == "find_order"


@pytest.mark.asyncio
async def test_embedding_failure_falls_back_to_text(patch_mongo):
    from fakes import FakeEmbeddingService, lexical_overlap_handler

    catalog = patch_mongo["tool_catalog"]
    catalog.docs.extend(CATALOG)
    catalog._aggregate_handler = lexical_overlap_handler(lambda: catalog.docs)
    failing = FakeEmbeddingService(fail=True)
    service = HybridSearchService(settings=Settings(), embedding_service=failing)

    # Hybrid mode requires an embedding; when it fails we should still get
    # lexical results instead of an exception.
    results = await service.search_tools(query="forecast", mode="hybrid", limit=5)
    assert any(r["name"] == "get_forecast" for r in results)


@pytest.mark.asyncio
async def test_rankfusion_operationfailure_falls_back_to_app_side(patch_mongo, fake_embeddings):
    from fakes import lexical_overlap_handler

    catalog = patch_mongo["tool_catalog"]
    catalog.docs.extend(CATALOG)
    lexical = lexical_overlap_handler(lambda: catalog.docs)

    def handler(pipeline):
        # Simulate a cluster without native $rankFusion support.
        if any("$rankFusion" in stage for stage in pipeline):
            raise OperationFailure("$rankFusion is not supported")
        return lexical(pipeline)

    catalog._aggregate_handler = handler
    service = HybridSearchService(settings=Settings(), embedding_service=fake_embeddings)

    results = await service.search_tools(query="forecast", mode="hybrid", limit=5)
    assert any(r["name"] == "get_forecast" for r in results)


@pytest.mark.asyncio
async def test_list_tools_scope_filter(service):
    results = await service.list_tools(allowed_scopes=["weather"], limit=10)
    names = {r["name"] for r in results}
    assert "get_forecast" in names
    assert "find_order" not in names
