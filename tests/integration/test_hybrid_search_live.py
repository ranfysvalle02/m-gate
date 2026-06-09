"""Real hybrid-search integration tests against MongoDB Atlas Local.

This is the suite that proves the gateway's differentiator actually works on a
real engine — native ``$rankFusion`` fusing ``$vectorSearch`` + ``$search`` over
one collection — rather than just asserting pipeline JSON shape. It also serves
as the contract check that the in-memory fake used by the unit suite behaves
like the real thing for the cases the unit tests rely on.

Requires Atlas Local + Ollama + a bootstrapped catalog (see conftest docstring).
"""

from __future__ import annotations

import pytest

from services.hybrid_search import (
    SEARCH_MODE_HYBRID,
    SEARCH_MODE_TEXT,
    SEARCH_MODE_VECTOR,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _catalog_count(db) -> int:
    return await db["tool_catalog"].count_documents({})


async def test_catalog_is_bootstrapped(live_db):
    """Guard: the session bootstrap seeded the catalog into the isolated DB."""
    count = await _catalog_count(live_db)
    assert count >= 6, "bootstrap fixture did not seed the tool catalog"


async def test_hybrid_rankfusion_runs_natively_and_ranks(live_search):
    """$rankFusion executes on Atlas and returns fused, score-sorted results."""
    results = await live_search.search_tools(query="weather forecast", mode=SEARCH_MODE_HYBRID)
    assert results, "hybrid search returned nothing"
    names = [r["name"] for r in results]
    # A weather query should surface the weather tools near the top.
    assert any("weather" in n or "forecast" in n for n in names[:3])
    # Scores are present and monotonically non-increasing (the engine sorted).
    scores = [r.get("score", 0) for r in results]
    assert scores == sorted(scores, reverse=True)


async def test_hybrid_emits_scoredetails_receipts(live_search, settings):
    """With include_score_details on, both arms leave per-pipeline receipts."""
    if not settings.include_score_details:
        pytest.skip("INCLUDE_SCORE_DETAILS is off in this environment")
    results = await live_search.search_tools(query="order status", mode=SEARCH_MODE_HYBRID)
    assert results
    # At least the top hit should carry scoreDetails proving the fusion ran.
    top = results[0]
    assert "scoreDetails" in top


async def test_vector_mode_finds_semantically_related_tool(live_search):
    """$vectorSearch should match on meaning, not exact tokens.

    'check the climate somewhere' shares no keywords with the tool names but is
    semantically a weather query.
    """
    results = await live_search.search_tools(
        query="what is the climate like somewhere", mode=SEARCH_MODE_VECTOR, limit=5
    )
    assert results
    names = [r["name"] for r in results]
    assert any("weather" in n or "forecast" in n for n in names)


async def test_text_mode_matches_exact_tokens(live_search):
    """$search (BM25) should match the literal token 'order'."""
    results = await live_search.search_tools(query="order", mode=SEARCH_MODE_TEXT, limit=5)
    assert results
    assert all("score" in r for r in results)
    assert any("order" in r["name"] for r in results)


async def test_scope_filter_excludes_unauthorized_tools(live_search):
    """Identity-bound scope must narrow $vectorSearch candidates on the engine."""
    # A caller scoped only to weather must never see orders tools, even for an
    # orders-flavored query.
    results = await live_search.search_tools(
        query="find my order", mode=SEARCH_MODE_HYBRID, allowed_scopes=["weather"]
    )
    for r in results:
        assert "orders" not in (r.get("scopes") or []) or "weather" in (r.get("scopes") or [])
    # And the orders-only tool (update_order_status, scope orders:write) is absent.
    assert all(r["name"] != "update_order_status" for r in results)


async def test_list_tools_respects_scope_without_query(live_search):
    weather_only = await live_search.list_tools(allowed_scopes=["weather"], limit=50)
    names = {r["name"] for r in weather_only}
    assert "get_forecast" in names
    assert "update_order_status" not in names


async def test_readme_hybrid_corrects_lexical_noise(live_search):
    """Pin the README's headline claim against regression.

    For "look up a purchase by its id", lexical-only ($search) drags an unrelated
    *weather* tool into the top results on common words, while hybrid keeps the
    real order tools above that noise. We assert the *behavior* the README sells —
    hybrid ranks order tools at least as high as text mode does, and does not let a
    weather tool outrank both order tools — rather than a brittle exact ordering
    (which shifts with the embedding model).
    """
    query = "look up a purchase by its id"
    order_tools = {"find_order", "list_customer_orders"}
    weather_tools = {"severe_weather_alerts", "get_forecast", "get_current_weather"}

    text = [
        r["name"]
        for r in await live_search.search_tools(query=query, mode=SEARCH_MODE_TEXT, limit=5)
    ]
    hybrid = [
        r["name"]
        for r in await live_search.search_tools(query=query, mode=SEARCH_MODE_HYBRID, limit=5)
    ]

    # The query is about orders: the top hit under hybrid is a real order tool.
    assert hybrid[0] in order_tools, f"hybrid top hit was not an order tool: {hybrid}"

    # Hybrid keeps both order tools ahead of any weather tool — the correction the
    # semantic arm provides over lexical-only retrieval.
    top3_hybrid = hybrid[:3]
    assert order_tools <= set(top3_hybrid), f"hybrid dropped an order tool: {hybrid}"
    first_weather_rank = next((i for i, n in enumerate(hybrid) if n in weather_tools), len(hybrid))
    last_order_rank = max((i for i, n in enumerate(hybrid) if n in order_tools), default=-1)
    assert (
        last_order_rank < first_weather_rank
    ), f"a weather tool outranked an order tool under hybrid: {hybrid}"

    # Sanity: the text/hybrid arms genuinely disagree on this query (otherwise the
    # README's "watch the arms disagree" demo — and this test — would be vacuous).
    assert (
        text[:3] != hybrid[:3]
    ), f"text and hybrid agreed on top-3 ({hybrid}); the lexical-noise demo is moot"


async def test_all_three_modes_return_comparable_shape(live_search):
    """The three retrieval strategies share one result contract."""
    query = "weather"
    out = {}
    for mode in (SEARCH_MODE_HYBRID, SEARCH_MODE_VECTOR, SEARCH_MODE_TEXT):
        res = await live_search.search_tools(query=query, mode=mode, limit=5)
        assert res, f"mode {mode} returned nothing"
        for item in res:
            assert {"server", "name", "score"} <= set(item)
        out[mode] = {r["name"] for r in res}
    # The weather tools should appear across all three strategies.
    assert out[SEARCH_MODE_VECTOR] & out[SEARCH_MODE_TEXT]
