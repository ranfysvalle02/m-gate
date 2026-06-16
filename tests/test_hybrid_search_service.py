"""Service-level tests for HybridSearchService.search_tools / list_tools.

These exercise the runtime branching (mode selection, embedding-failure
fallback to lexical, $rankFusion OperationFailure -> app-side RRF) using the
fake DB + a lexical-overlap aggregate handler, complementing the existing
pure pipeline-shape tests.
"""

from __future__ import annotations

import logging

import pytest
from pymongo.errors import EncryptionError, OperationFailure

from config.settings import Settings
from services.hybrid_search import HybridSearchService, get_last_fusion_path

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
async def test_rankfusion_encryptionerror_falls_back_to_app_side(patch_mongo, fake_embeddings):
    from fakes import lexical_overlap_handler

    catalog = patch_mongo["tool_catalog"]
    catalog.docs.extend(CATALOG)
    lexical = lexical_overlap_handler(lambda: catalog.docs)

    def handler(pipeline):
        # Simulate Queryable Encryption blocking native $rankFusion analysis.
        if any("$rankFusion" in stage for stage in pipeline):
            raise EncryptionError(
                Exception('[crypt_shared] "analyze_query" failed: No resolved namespace provided')
            )
        return lexical(pipeline)

    catalog._aggregate_handler = handler
    service = HybridSearchService(settings=Settings(), embedding_service=fake_embeddings)

    # Hybrid must still return fused results via the app-side RRF safety net.
    results = await service.search_tools(query="forecast", mode="hybrid", limit=5)
    assert any(r["name"] == "get_forecast" for r in results)


class _RecordingHandler(logging.Handler):
    """Captures records straight off a logger, immune to global logging config.

    The app configures logging via dictConfig (disable_existing_loggers / custom
    handlers), which can defeat pytest's propagation-based ``caplog``. Attaching
    directly to the module logger keeps these assertions deterministic.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.mark.asyncio
async def test_rankfusion_fallback_logs_once_then_debug(patch_mongo, fake_embeddings):
    import services.hybrid_search as hs
    from fakes import lexical_overlap_handler

    catalog = patch_mongo["tool_catalog"]
    catalog.docs.extend(CATALOG)
    lexical = lexical_overlap_handler(lambda: catalog.docs)

    def handler(pipeline):
        if any("$rankFusion" in stage for stage in pipeline):
            raise OperationFailure("$rankFusion is not supported")
        return lexical(pipeline)

    catalog._aggregate_handler = handler
    # The "warn once per reason" guard is process-global; clear it so this test sees
    # the first-time WARNING regardless of test ordering.
    hs._warned_fallback_reasons.clear()

    cap = _RecordingHandler()
    prev_level, prev_disabled = hs.logger.level, hs.logger.disabled
    hs.logger.addHandler(cap)
    hs.logger.setLevel(logging.DEBUG)
    hs.logger.disabled = False
    try:
        service = HybridSearchService(settings=Settings(), embedding_service=fake_embeddings)
        first = await service.search_tools(query="forecast", mode="hybrid", limit=5)
        await service.search_tools(query="forecast", mode="hybrid", limit=5)
    finally:
        hs.logger.removeHandler(cap)
        hs.logger.setLevel(prev_level)
        hs.logger.disabled = prev_disabled

    # Fallback still returns fused results...
    assert any(r["name"] == "get_forecast" for r in first)
    fallbacks = [r for r in cap.records if "app-side RRF" in r.getMessage()]
    warnings = [r for r in fallbacks if r.levelno == logging.WARNING]
    debugs = [r for r in fallbacks if r.levelno == logging.DEBUG]
    # ...the first degradation WARNs (with the cause), the repeat is demoted to DEBUG.
    assert len(warnings) == 1
    assert "OperationFailure" in warnings[0].getMessage()
    assert len(debugs) == 1


@pytest.mark.asyncio
async def test_fusion_path_native_for_hybrid(service):
    await service.search_tools(query="order by id", mode="hybrid", limit=5)
    assert get_last_fusion_path() == "native_rankfusion"


@pytest.mark.asyncio
async def test_fusion_path_text_and_vector(service):
    await service.search_tools(query="forecast", mode="text", limit=5)
    assert get_last_fusion_path() == "text"
    await service.search_tools(query="forecast", mode="vector", limit=5)
    assert get_last_fusion_path() == "vector"


@pytest.mark.asyncio
async def test_fusion_path_lexical_fallback_on_embedding_failure(patch_mongo):
    from fakes import FakeEmbeddingService, lexical_overlap_handler

    catalog = patch_mongo["tool_catalog"]
    catalog.docs.extend(CATALOG)
    catalog._aggregate_handler = lexical_overlap_handler(lambda: catalog.docs)
    service = HybridSearchService(
        settings=Settings(), embedding_service=FakeEmbeddingService(fail=True)
    )

    await service.search_tools(query="forecast", mode="hybrid", limit=5)
    assert get_last_fusion_path() == "lexical_fallback"


@pytest.mark.asyncio
async def test_fusion_path_app_side_on_operationfailure(patch_mongo, fake_embeddings):
    from fakes import lexical_overlap_handler

    catalog = patch_mongo["tool_catalog"]
    catalog.docs.extend(CATALOG)
    lexical = lexical_overlap_handler(lambda: catalog.docs)

    def handler(pipeline):
        if any("$rankFusion" in stage for stage in pipeline):
            raise OperationFailure("$rankFusion is not supported")
        return lexical(pipeline)

    catalog._aggregate_handler = handler
    service = HybridSearchService(settings=Settings(), embedding_service=fake_embeddings)

    await service.search_tools(query="forecast", mode="hybrid", limit=5)
    assert get_last_fusion_path() == "app_side_rrf"


@pytest.mark.asyncio
async def test_fusion_path_app_side_on_encryptionerror(patch_mongo, fake_embeddings):
    from fakes import lexical_overlap_handler

    catalog = patch_mongo["tool_catalog"]
    catalog.docs.extend(CATALOG)
    lexical = lexical_overlap_handler(lambda: catalog.docs)

    def handler(pipeline):
        if any("$rankFusion" in stage for stage in pipeline):
            raise EncryptionError(Exception("analyze_query failed"))
        return lexical(pipeline)

    catalog._aggregate_handler = handler
    service = HybridSearchService(settings=Settings(), embedding_service=fake_embeddings)

    await service.search_tools(query="forecast", mode="hybrid", limit=5)
    assert get_last_fusion_path() == "app_side_rrf"


@pytest.mark.asyncio
async def test_list_tools_scope_filter(service):
    results = await service.list_tools(
        allowed_scopes=["weather", "server:weather"],
        limit=10,
    )
    names = {r["name"] for r in results}
    assert "get_forecast" in names
    assert "find_order" not in names


# A pinned tool on its own (demo) server, lexically unrelated to weather/orders.
PINNED_TOOL = {
    "server": "gateway_demo",
    "name": "gateway_hello",
    "description": "hello health smoke test",
    "scopes": ["demo"],
    "input_schema": {},
    "embedding": [0.0],
    "metadata": {"always_included": True},
}


@pytest.mark.asyncio
async def test_always_included_surfaces_when_irrelevant(service, patch_mongo):
    patch_mongo["tool_catalog"].docs.append(dict(PINNED_TOOL))

    results = await service.search_tools(query="forecast", mode="hybrid", limit=5)

    # Pinned to the top despite zero lexical/semantic overlap with the query...
    assert results[0]["name"] == "gateway_hello"
    assert results[0]["pinned"] is True
    # ...while genuine relevance still flows in behind the pin.
    assert any(r["name"] == "get_forecast" for r in results)


@pytest.mark.asyncio
async def test_always_included_respects_scope(service, patch_mongo):
    patch_mongo["tool_catalog"].docs.append(dict(PINNED_TOOL))

    results = await service.search_tools(
        query="forecast",
        mode="hybrid",
        limit=5,
        allowed_scopes=["weather", "server:weather"],
    )

    names = {r["name"] for r in results}
    # Pinning never bypasses identity-bound discovery: a tool on a server the
    # caller cannot see stays hidden.
    assert "gateway_hello" not in names
    assert "get_forecast" in names


@pytest.mark.asyncio
async def test_always_included_deduplicates_with_relevance(service, patch_mongo):
    pinned_and_relevant = {
        "server": "orders",
        "name": "cancel_order",
        "description": "cancel an order by id",
        "scopes": ["orders"],
        "input_schema": {},
        "embedding": [0.0],
        "metadata": {"always_included": True},
    }
    patch_mongo["tool_catalog"].docs.append(pinned_and_relevant)

    results = await service.search_tools(query="order by id", mode="hybrid", limit=5)

    keys = [(r["server"], r["name"]) for r in results]
    # Both pinned and relevant -> appears exactly once, at the top.
    assert keys.count(("orders", "cancel_order")) == 1
    assert results[0]["name"] == "cancel_order"
    assert results[0]["pinned"] is True


@pytest.mark.asyncio
async def test_always_included_counts_against_limit(service, patch_mongo):
    patch_mongo["tool_catalog"].docs.append(dict(PINNED_TOOL))

    results = await service.search_tools(query="forecast", mode="hybrid", limit=1)

    # The single seat is spent on the pin; relevance is squeezed out.
    assert len(results) == 1
    assert results[0]["name"] == "gateway_hello"


@pytest.mark.asyncio
async def test_always_included_over_limit_returns_all_pinned(service, patch_mongo):
    catalog = patch_mongo["tool_catalog"]
    catalog.docs.append(dict(PINNED_TOOL))
    catalog.docs.append(
        {
            "server": "gateway_demo",
            "name": "gateway_status",
            "description": "status check",
            "scopes": ["demo"],
            "input_schema": {},
            "embedding": [0.0],
            "metadata": {"always_included": True},
        }
    )

    results = await service.search_tools(query="forecast", mode="hybrid", limit=1)

    # Two pins, limit of one: admin intent wins and both survive.
    assert len(results) == 2
    assert {r["name"] for r in results} == {"gateway_hello", "gateway_status"}
    assert all(r["pinned"] for r in results)


@pytest.mark.asyncio
async def test_pinning_disabled_by_setting(patch_mongo, fake_embeddings):
    from fakes import lexical_overlap_handler

    catalog = patch_mongo["tool_catalog"]
    catalog.docs.extend(CATALOG)
    catalog.docs.append(dict(PINNED_TOOL))
    catalog._aggregate_handler = lexical_overlap_handler(lambda: catalog.docs)
    service = HybridSearchService(
        settings=Settings(hybrid_pin_always_included=False),
        embedding_service=fake_embeddings,
    )

    results = await service.search_tools(query="forecast", mode="hybrid", limit=5)

    # With the escape hatch off, nothing is promoted or tagged.
    assert all("pinned" not in r for r in results)
    assert results[0]["name"] == "get_forecast"
