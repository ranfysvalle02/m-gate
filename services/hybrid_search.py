from __future__ import annotations

from collections import defaultdict
from typing import Any

from pymongo.errors import OperationFailure

from config.settings import Settings, get_settings
from database.indexes import TEXT_INDEX_NAME, VECTOR_INDEX_NAME
from database.mongo import get_tenant_database
from services.embeddings import EmbeddingService, EmbeddingUnavailableError, get_embedding_service

# The three retrieval strategies this service can run over the SAME collection
# and the SAME two indexes. The whole point of the gateway is that switching
# between them is a pipeline shape, not a different database.
SEARCH_MODE_HYBRID = "hybrid"
SEARCH_MODE_VECTOR = "vector"
SEARCH_MODE_TEXT = "text"

# Fields every result carries so the three modes are directly comparable.
_BASE_PROJECTION: dict[str, Any] = {
    "_id": 0,
    "server": 1,
    "name": 1,
    "description": 1,
    "input_schema": 1,
    "scopes": 1,
    "metadata": 1,
}


def _normalize_scopes(allowed_scopes: list[str] | None) -> list[str] | None:
    if not allowed_scopes:
        return None
    cleaned = [s for s in allowed_scopes if s]
    return cleaned or None


def _scope_filter(scopes: list[str]) -> dict[str, Any]:
    return {"scopes": {"$in": scopes}}


def build_vector_pipeline(
    *,
    query_vector: list[float],
    num_candidates: int,
    output_limit: int,
    allowed_scopes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Semantic-only retrieval ($vectorSearch). Great at intent, blind to exact tokens."""
    scopes = _normalize_scopes(allowed_scopes)
    vector_stage: dict[str, Any] = {
        "index": VECTOR_INDEX_NAME,
        "path": "embedding",
        "queryVector": query_vector,
        "numCandidates": num_candidates,
        "limit": output_limit,
    }
    if scopes is not None:
        vector_stage["filter"] = _scope_filter(scopes)
    return [
        {"$vectorSearch": vector_stage},
        {"$project": {**_BASE_PROJECTION, "score": {"$meta": "vectorSearchScore"}}},
    ]


def build_text_pipeline(
    *,
    query: str,
    output_limit: int,
    allowed_scopes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Lexical-only retrieval ($search / BM25). Great at exact tokens, blind to intent."""
    scopes = _normalize_scopes(allowed_scopes)
    pipeline: list[dict[str, Any]] = [
        {
            "$search": {
                "index": TEXT_INDEX_NAME,
                "text": {
                    "query": query,
                    "path": ["name", "description", "server"],
                },
            }
        },
    ]
    if scopes is not None:
        pipeline.append({"$match": _scope_filter(scopes)})
    pipeline.append({"$project": {**_BASE_PROJECTION, "score": {"$meta": "searchScore"}}})
    pipeline.append({"$limit": output_limit})
    return pipeline


def build_rank_fusion_pipeline(
    *,
    query: str,
    query_vector: list[float],
    vector_weight: float,
    text_weight: float,
    num_candidates: int,
    pipeline_limit: int,
    output_limit: int,
    include_score_details: bool,
    allowed_scopes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Hybrid retrieval: fuse the vector and lexical arms with Reciprocal Rank Fusion.

    This is the gateway's differentiator. Two retrievers with incompatible score
    scales (cosine similarity in [0,1] vs. unbounded BM25) are merged by *rank*
    position, so no score normalization is required. Done natively by MongoDB in
    a single query over one collection -- no second store, no client-side merge.
    """
    scopes = _normalize_scopes(allowed_scopes)

    vector_stage: dict[str, Any] = {
        "index": VECTOR_INDEX_NAME,
        "path": "embedding",
        "queryVector": query_vector,
        "numCandidates": num_candidates,
        "limit": pipeline_limit,
    }
    if scopes is not None:
        # Identity narrows the candidate set before meaning ranks it.
        vector_stage["filter"] = _scope_filter(scopes)

    full_text_pipeline: list[dict[str, Any]] = [
        {
            "$search": {
                "index": TEXT_INDEX_NAME,
                "text": {
                    "query": query,
                    "path": ["name", "description", "server"],
                },
            }
        },
    ]
    if scopes is not None:
        full_text_pipeline.append({"$match": _scope_filter(scopes)})
    full_text_pipeline.append({"$limit": pipeline_limit})

    projection: dict[str, Any] = {**_BASE_PROJECTION, "score": {"$meta": "score"}}
    if include_score_details:
        # Per-pipeline rank contributions: the receipts that prove BOTH arms ran.
        projection["scoreDetails"] = {"$meta": "scoreDetails"}

    return [
        {
            "$rankFusion": {
                "input": {
                    "pipelines": {
                        "vectorPipeline": [{"$vectorSearch": vector_stage}],
                        "fullTextPipeline": full_text_pipeline,
                    }
                },
                "combination": {
                    "weights": {
                        "vectorPipeline": vector_weight,
                        "fullTextPipeline": text_weight,
                    }
                },
                "scoreDetails": include_score_details,
            }
        },
        {"$project": projection},
        {"$sort": {"score": -1}},
        {"$limit": output_limit},
    ]


class HybridSearchService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        embedding_service: EmbeddingService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.embedding_service = embedding_service or get_embedding_service(self.settings)

    async def search_tools(
        self,
        *,
        tenant_id: str | None = None,
        query: str,
        limit: int | None = None,
        vector_weight: float | None = None,
        text_weight: float | None = None,
        allowed_scopes: list[str] | None = None,
        mode: str | None = None,
    ) -> list[dict[str, Any]]:
        effective_limit = limit or self.settings.hybrid_output_limit
        mode = (mode or SEARCH_MODE_HYBRID).lower()
        resolved_tenant_id = tenant_id or self.settings.default_tenant_id
        collection = get_tenant_database(resolved_tenant_id)["tool_catalog"]

        if mode == SEARCH_MODE_TEXT:
            # Lexical-only needs no embedding -- skip the hot-path embed call.
            pipeline = build_text_pipeline(
                query=query,
                output_limit=effective_limit,
                allowed_scopes=allowed_scopes,
            )
            cursor = await collection.aggregate(pipeline)
            return await cursor.to_list(length=effective_limit)

        try:
            query_vector = await self.embedding_service.embed_text(query)
        except EmbeddingUnavailableError:
            # Resiliency fallback: keep routing alive with lexical-only retrieval.
            pipeline = build_text_pipeline(
                query=query,
                output_limit=effective_limit,
                allowed_scopes=allowed_scopes,
            )
            cursor = await collection.aggregate(pipeline)
            return await cursor.to_list(length=effective_limit)

        if mode == SEARCH_MODE_VECTOR:
            pipeline = build_vector_pipeline(
                query_vector=query_vector,
                num_candidates=self.settings.hybrid_num_candidates,
                output_limit=effective_limit,
                allowed_scopes=allowed_scopes,
            )
            cursor = await collection.aggregate(pipeline)
            return await cursor.to_list(length=effective_limit)

        if self.settings.fusion_strategy == "app_side":
            return await self._search_hybrid_app_side(
                collection=collection,
                query=query,
                query_vector=query_vector,
                effective_limit=effective_limit,
                vector_weight=vector_weight or self.settings.hybrid_vector_weight,
                text_weight=text_weight or self.settings.hybrid_text_weight,
                allowed_scopes=allowed_scopes,
            )

        pipeline = build_rank_fusion_pipeline(
            query=query,
            query_vector=query_vector,
            vector_weight=vector_weight or self.settings.hybrid_vector_weight,
            text_weight=text_weight or self.settings.hybrid_text_weight,
            num_candidates=self.settings.hybrid_num_candidates,
            pipeline_limit=self.settings.hybrid_pipeline_limit,
            output_limit=effective_limit,
            include_score_details=self.settings.include_score_details,
            allowed_scopes=allowed_scopes,
        )
        try:
            cursor = await collection.aggregate(pipeline)
            return await cursor.to_list(length=effective_limit)
        except OperationFailure:
            # GA-safe fallback: run both retrievers and fuse with app-side RRF.
            return await self._search_hybrid_app_side(
                collection=collection,
                query=query,
                query_vector=query_vector,
                effective_limit=effective_limit,
                vector_weight=vector_weight or self.settings.hybrid_vector_weight,
                text_weight=text_weight or self.settings.hybrid_text_weight,
                allowed_scopes=allowed_scopes,
            )

    async def list_tools(
        self,
        *,
        tenant_id: str | None = None,
        allowed_scopes: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return the catalog (optionally scope-filtered) with no ranking.

        Used by tools/list when the caller does not supply a routing query, so
        discovery still respects identity-bound scope even without a search.
        """
        scopes = _normalize_scopes(allowed_scopes)
        match: dict[str, Any] = _scope_filter(scopes) if scopes is not None else {}
        pipeline = [
            {"$match": match},
            {"$project": dict(_BASE_PROJECTION)},
            {"$sort": {"server": 1, "name": 1}},
            {"$skip": max(0, offset)},
            {"$limit": limit or self.settings.catalog_list_limit},
        ]
        resolved_tenant_id = tenant_id or self.settings.default_tenant_id
        collection = get_tenant_database(resolved_tenant_id)["tool_catalog"]
        cursor = await collection.aggregate(pipeline)
        return await cursor.to_list(length=limit or self.settings.catalog_list_limit)

    async def _search_hybrid_app_side(
        self,
        *,
        collection,
        query: str,
        query_vector: list[float],
        effective_limit: int,
        vector_weight: float,
        text_weight: float,
        allowed_scopes: list[str] | None,
    ) -> list[dict[str, Any]]:
        vector_pipeline = build_vector_pipeline(
            query_vector=query_vector,
            num_candidates=self.settings.hybrid_num_candidates,
            output_limit=self.settings.hybrid_pipeline_limit,
            allowed_scopes=allowed_scopes,
        )
        text_pipeline = build_text_pipeline(
            query=query,
            output_limit=self.settings.hybrid_pipeline_limit,
            allowed_scopes=allowed_scopes,
        )
        vector_cursor = await collection.aggregate(vector_pipeline)
        text_cursor = await collection.aggregate(text_pipeline)
        vector_docs = await vector_cursor.to_list(length=self.settings.hybrid_pipeline_limit)
        text_docs = await text_cursor.to_list(length=self.settings.hybrid_pipeline_limit)
        return self._fuse_rrf(
            vector_docs=vector_docs,
            text_docs=text_docs,
            vector_weight=vector_weight,
            text_weight=text_weight,
            output_limit=effective_limit,
            include_score_details=self.settings.include_score_details,
        )

    @staticmethod
    def _fuse_rrf(
        *,
        vector_docs: list[dict[str, Any]],
        text_docs: list[dict[str, Any]],
        vector_weight: float,
        text_weight: float,
        output_limit: int,
        include_score_details: bool,
    ) -> list[dict[str, Any]]:
        by_key: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        scores: dict[tuple[str | None, str | None], float] = defaultdict(float)
        details: dict[tuple[str | None, str | None], list[dict[str, Any]]] = defaultdict(list)

        def key_of(doc: dict[str, Any]) -> tuple[str | None, str | None]:
            return doc.get("server"), doc.get("name")

        for rank, doc in enumerate(vector_docs, start=1):
            key = key_of(doc)
            by_key.setdefault(key, dict(doc))
            contribution = vector_weight * (1 / (60 + rank))
            scores[key] += contribution
            if include_score_details:
                details[key].append(
                    {
                        "inputPipelineName": "vectorPipeline",
                        "rank": rank,
                        "weight": vector_weight,
                        "value": contribution,
                    }
                )

        for rank, doc in enumerate(text_docs, start=1):
            key = key_of(doc)
            by_key.setdefault(key, dict(doc))
            contribution = text_weight * (1 / (60 + rank))
            scores[key] += contribution
            if include_score_details:
                details[key].append(
                    {
                        "inputPipelineName": "fullTextPipeline",
                        "rank": rank,
                        "weight": text_weight,
                        "value": contribution,
                    }
                )

        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:output_limit]
        fused: list[dict[str, Any]] = []
        for key, score in ordered:
            doc = by_key[key]
            doc["score"] = score
            if include_score_details:
                doc["scoreDetails"] = details[key]
            fused.append(doc)
        return fused
