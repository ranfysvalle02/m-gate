from services.hybrid_search import (
    HybridSearchService,
    build_rank_fusion_pipeline,
    build_text_pipeline,
    build_vector_pipeline,
)


def test_rank_fusion_pipeline_shape():
    pipeline = build_rank_fusion_pipeline(
        query="weather in nyc",
        query_vector=[0.1, 0.2, 0.3],
        vector_weight=0.5,
        text_weight=0.5,
        num_candidates=100,
        pipeline_limit=20,
        output_limit=10,
        include_score_details=True,
    )

    assert (
        pipeline[0]["$rankFusion"]["input"]["pipelines"]["vectorPipeline"][0]["$vectorSearch"][
            "path"
        ]
        == "embedding"
    )
    assert pipeline[0]["$rankFusion"]["input"]["pipelines"]["fullTextPipeline"][0]["$search"][
        "text"
    ]["path"] == [
        "name",
        "description",
        "server",
    ]
    assert pipeline[0]["$rankFusion"]["scoreDetails"] is True
    assert pipeline[-1]["$limit"] == 10


def test_rank_fusion_projects_fused_score_and_sorts():
    pipeline = build_rank_fusion_pipeline(
        query="weather in nyc",
        query_vector=[0.1, 0.2, 0.3],
        vector_weight=0.6,
        text_weight=0.4,
        num_candidates=100,
        pipeline_limit=20,
        output_limit=5,
        include_score_details=True,
    )
    project = next(stage["$project"] for stage in pipeline if "$project" in stage)
    assert project["score"] == {"$meta": "score"}
    assert project["scoreDetails"] == {"$meta": "scoreDetails"}
    # Explicit sort by fused score precedes the final limit.
    sort_idx = next(i for i, s in enumerate(pipeline) if "$sort" in s)
    limit_idx = next(i for i, s in enumerate(pipeline) if "$limit" in s)
    assert pipeline[sort_idx]["$sort"] == {"score": -1}
    assert sort_idx < limit_idx
    assert pipeline[0]["$rankFusion"]["combination"]["weights"] == {
        "vectorPipeline": 0.6,
        "fullTextPipeline": 0.4,
    }


def test_vector_pipeline_shape_and_scope_filter():
    pipeline = build_vector_pipeline(
        query_vector=[0.1, 0.2, 0.3],
        num_candidates=100,
        output_limit=5,
        allowed_scopes=["weather", "server:weather"],
    )
    vs = pipeline[0]["$vectorSearch"]
    assert vs["path"] == "embedding"
    assert vs["filter"] == {"scopes": {"$in": ["weather"]}, "server": {"$in": ["weather"]}}
    assert pipeline[1]["$project"]["score"] == {"$meta": "vectorSearchScore"}


def test_text_pipeline_shape_and_scope_filter():
    pipeline = build_text_pipeline(
        query="forecast",
        output_limit=5,
        allowed_scopes=["weather", "server:weather"],
    )
    assert pipeline[0]["$search"]["text"]["path"] == ["name", "description", "server"]
    match_stages = [s["$match"] for s in pipeline if "$match" in s]
    assert match_stages == [{"scopes": {"$in": ["weather"]}, "server": {"$in": ["weather"]}}]
    project = next(s["$project"] for s in pipeline if "$project" in s)
    assert project["score"] == {"$meta": "searchScore"}


def test_rank_fusion_pipeline_without_scopes_has_no_filter():
    pipeline = build_rank_fusion_pipeline(
        query="weather in nyc",
        query_vector=[0.1, 0.2, 0.3],
        vector_weight=0.5,
        text_weight=0.5,
        num_candidates=100,
        pipeline_limit=20,
        output_limit=10,
        include_score_details=True,
    )
    pipelines = pipeline[0]["$rankFusion"]["input"]["pipelines"]
    assert "filter" not in pipelines["vectorPipeline"][0]["$vectorSearch"]
    assert all("$match" not in stage for stage in pipelines["fullTextPipeline"])


def test_rank_fusion_pipeline_applies_scope_filter():
    pipeline = build_rank_fusion_pipeline(
        query="weather in nyc",
        query_vector=[0.1, 0.2, 0.3],
        vector_weight=0.5,
        text_weight=0.5,
        num_candidates=100,
        pipeline_limit=20,
        output_limit=10,
        include_score_details=True,
        allowed_scopes=["weather", "readonly", "server:weather"],
    )
    pipelines = pipeline[0]["$rankFusion"]["input"]["pipelines"]

    vector_filter = pipelines["vectorPipeline"][0]["$vectorSearch"]["filter"]
    assert vector_filter == {
        "scopes": {"$in": ["weather", "readonly"]},
        "server": {"$in": ["weather"]},
    }

    match_stages = [stage["$match"] for stage in pipelines["fullTextPipeline"] if "$match" in stage]
    assert match_stages == [
        {"scopes": {"$in": ["weather", "readonly"]}, "server": {"$in": ["weather"]}}
    ]


def test_rank_fusion_ignores_empty_scope_list():
    pipeline = build_rank_fusion_pipeline(
        query="weather in nyc",
        query_vector=[0.1, 0.2, 0.3],
        vector_weight=0.5,
        text_weight=0.5,
        num_candidates=100,
        pipeline_limit=20,
        output_limit=10,
        include_score_details=True,
        allowed_scopes=[],
    )
    pipelines = pipeline[0]["$rankFusion"]["input"]["pipelines"]
    assert "filter" not in pipelines["vectorPipeline"][0]["$vectorSearch"]


def test_server_scope_wildcard_does_not_restrict_server():
    pipeline = build_vector_pipeline(
        query_vector=[0.1, 0.2, 0.3],
        num_candidates=50,
        output_limit=5,
        allowed_scopes=["readonly", "server:*"],
    )
    assert pipeline[0]["$vectorSearch"]["filter"] == {"scopes": {"$in": ["readonly"]}}


def test_app_side_rrf_fusion_orders_by_combined_rank():
    vector_docs = [
        {"server": "orders", "name": "find_order", "description": "a"},
        {"server": "orders", "name": "list_customer_orders", "description": "b"},
    ]
    text_docs = [
        {"server": "orders", "name": "list_customer_orders", "description": "b"},
        {"server": "orders", "name": "find_order", "description": "a"},
    ]

    fused = HybridSearchService._fuse_rrf(
        vector_docs=vector_docs,
        text_docs=text_docs,
        vector_weight=0.5,
        text_weight=0.5,
        output_limit=2,
        include_score_details=True,
    )

    assert len(fused) == 2
    assert {item["name"] for item in fused} == {"find_order", "list_customer_orders"}
    assert all("score" in item for item in fused)
    assert all("scoreDetails" in item for item in fused)


def test_merge_pinned_dedups_and_pins_first():
    pinned = [{"server": "demo", "name": "hello", "metadata": {"always_included": True}}]
    ranked = [
        {"server": "demo", "name": "hello", "score": 0.1},
        {"server": "weather", "name": "get_forecast", "score": 0.9},
    ]

    merged = HybridSearchService._merge_pinned(pinned=pinned, ranked=ranked, output_limit=5)

    # Pinned tool leads, is tagged, and is de-duplicated against the ranked arm.
    assert (merged[0]["server"], merged[0]["name"]) == ("demo", "hello")
    assert merged[0]["pinned"] is True
    keys = [(m["server"], m["name"]) for m in merged]
    assert keys.count(("demo", "hello")) == 1
    assert ("weather", "get_forecast") in keys


def test_merge_pinned_counts_against_budget():
    pinned = [{"server": "demo", "name": "hello"}]
    ranked = [
        {"server": "weather", "name": "get_forecast"},
        {"server": "orders", "name": "find_order"},
    ]

    merged = HybridSearchService._merge_pinned(pinned=pinned, ranked=ranked, output_limit=2)

    # One reserved seat for the pin leaves room for exactly one relevance hit.
    assert [m["name"] for m in merged] == ["hello", "get_forecast"]


def test_merge_pinned_over_limit_returns_all_pinned():
    pinned = [
        {"server": "a", "name": "one"},
        {"server": "b", "name": "two"},
        {"server": "c", "name": "three"},
    ]
    ranked = [{"server": "weather", "name": "get_forecast"}]

    merged = HybridSearchService._merge_pinned(pinned=pinned, ranked=ranked, output_limit=2)

    # Admin intent wins past the limit; the relevance tail is then empty.
    assert [m["name"] for m in merged] == ["one", "two", "three"]
    assert all(m.get("pinned") for m in merged)
