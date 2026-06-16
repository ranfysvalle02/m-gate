from __future__ import annotations

import contextvars
import logging
from collections import defaultdict
from typing import Any

from pymongo.errors import EncryptionError, OperationFailure

from config.settings import Settings, get_settings
from database.indexes import TEXT_INDEX_NAME, VECTOR_INDEX_NAME
from database.mongo import get_tenant_database, get_tenant_database_for_search
from services.embedding_config import get_embedding_service_for
from services.embeddings import EmbeddingService, EmbeddingUnavailableError

logger = logging.getLogger(__name__)

# Native $rankFusion falling back to app-side RRF is a degradation worth surfacing,
# but in a misconfigured deployment it would fire on *every* request. Warn once per
# distinct reason (per process) and DEBUG thereafter, so the signal survives without
# flooding the logs.
_warned_fallback_reasons: set[str] = set()

# Which retrieval/fusion path actually served the most recent search *in this
# request's context*. HybridSearchService is a process-wide singleton, so an
# instance attribute would be clobbered under concurrency; a ContextVar is
# per-task (per-request) and is visible to the caller once `search_tools` returns,
# so the /rpc layer can record it in audit telemetry. Values are a closed set:
# "native_rankfusion", "app_side_rrf", "vector", "text", "lexical_fallback".
_fusion_path: contextvars.ContextVar[str] = contextvars.ContextVar("hybrid_fusion_path", default="")


def get_last_fusion_path() -> str:
    """Return the fusion path taken by the most recent search in this context.

    Empty string if no ranked search has run in the current request context (e.g.
    a non-routed ``tools/list`` page). Concurrency-safe: each request task has its
    own ContextVar value.
    """
    return _fusion_path.get()


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


def _scope_filter(scopes: list[str], *, server: str | None = None) -> dict[str, Any]:
    match: dict[str, Any] = {}
    tool_scopes = [scope for scope in scopes if not scope.startswith("server:")]
    if tool_scopes:
        match["scopes"] = {"$in": tool_scopes}

    if server:
        match["server"] = server
        return match

    if "server:*" in scopes:
        return match

    allowed_servers = sorted(
        {
            scope.split(":", 1)[1]
            for scope in scopes
            if scope.startswith("server:") and scope != "server:*" and scope.split(":", 1)[1]
        }
    )
    match["server"] = {"$in": allowed_servers} if allowed_servers else {"$in": []}
    return match


def build_vector_pipeline(
    *,
    query_vector: list[float],
    num_candidates: int,
    output_limit: int,
    allowed_scopes: list[str] | None = None,
    server: str | None = None,
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
        vector_stage["filter"] = _scope_filter(scopes, server=server)
    elif server:
        vector_stage["filter"] = {"server": server}
    return [
        {"$vectorSearch": vector_stage},
        {"$project": {**_BASE_PROJECTION, "score": {"$meta": "vectorSearchScore"}}},
    ]


def build_text_pipeline(
    *,
    query: str,
    output_limit: int,
    allowed_scopes: list[str] | None = None,
    server: str | None = None,
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
        pipeline.append({"$match": _scope_filter(scopes, server=server)})
    elif server:
        pipeline.append({"$match": {"server": server}})
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
    server: str | None = None,
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
        vector_stage["filter"] = _scope_filter(scopes, server=server)
    elif server:
        vector_stage["filter"] = {"server": server}

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
        full_text_pipeline.append({"$match": _scope_filter(scopes, server=server)})
    elif server:
        full_text_pipeline.append({"$match": {"server": server}})
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
        self._embedding_service_override = embedding_service

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
        server: str | None = None,
    ) -> list[dict[str, Any]]:
        """Route a query to the most relevant tools, then pin always-included ones.

        Ranking and pinning are deliberately separate responsibilities:
        ``_search_ranked`` produces the relevance-ordered shortlist, and pinned
        (``metadata.always_included``) tools are layered on top here so an admin
        can guarantee a tool's presence without it ever bypassing identity-bound
        scope filtering or silently inflating the caller's ``limit`` budget.
        """
        effective_limit = limit or self.settings.hybrid_output_limit
        mode = (mode or SEARCH_MODE_HYBRID).lower()
        resolved_tenant_id = tenant_id or self.settings.default_tenant_id
        collection = get_tenant_database(resolved_tenant_id)["tool_catalog"]

        ranked = await self._search_ranked(
            collection=collection,
            resolved_tenant_id=resolved_tenant_id,
            query=query,
            mode=mode,
            effective_limit=effective_limit,
            vector_weight=vector_weight,
            text_weight=text_weight,
            allowed_scopes=allowed_scopes,
            server=server,
        )

        if not self.settings.hybrid_pin_always_included:
            return ranked

        pinned = await self._fetch_always_included(
            collection=collection,
            allowed_scopes=allowed_scopes,
            server=server,
        )
        if not pinned:
            return ranked
        return self._merge_pinned(pinned=pinned, ranked=ranked, output_limit=effective_limit)

    async def _search_ranked(
        self,
        *,
        collection: Any,
        resolved_tenant_id: str,
        query: str,
        mode: str,
        effective_limit: int,
        vector_weight: float | None,
        text_weight: float | None,
        allowed_scopes: list[str] | None,
        server: str | None,
    ) -> list[dict[str, Any]]:
        """Relevance-ranked retrieval only -- the three-mode engine.

        Lexical-only, vector-only, or hybrid $rankFusion, each with the same
        graceful fallbacks (embedding-unavailable -> lexical; $rankFusion
        unsupported -> app-side RRF). Knows nothing about always-included
        pinning, which ``search_tools`` layers on top.
        """
        if mode == SEARCH_MODE_TEXT:
            # Lexical-only needs no embedding -- skip the hot-path embed call.
            _fusion_path.set("text")
            pipeline = build_text_pipeline(
                query=query,
                output_limit=effective_limit,
                allowed_scopes=allowed_scopes,
                server=server,
            )
            cursor = await collection.aggregate(pipeline)
            return await cursor.to_list(length=effective_limit)

        embedding_service = self._embedding_service_override or await get_embedding_service_for(
            resolved_tenant_id, self.settings
        )
        try:
            query_vector = await embedding_service.embed_text(query)
        except EmbeddingUnavailableError:
            # Resiliency fallback: keep routing alive with lexical-only retrieval.
            _fusion_path.set("lexical_fallback")
            pipeline = build_text_pipeline(
                query=query,
                output_limit=effective_limit,
                allowed_scopes=allowed_scopes,
                server=server,
            )
            cursor = await collection.aggregate(pipeline)
            return await cursor.to_list(length=effective_limit)

        if mode == SEARCH_MODE_VECTOR:
            _fusion_path.set("vector")
            pipeline = build_vector_pipeline(
                query_vector=query_vector,
                num_candidates=self.settings.hybrid_num_candidates,
                output_limit=effective_limit,
                allowed_scopes=allowed_scopes,
                server=server,
            )
            cursor = await collection.aggregate(pipeline)
            return await cursor.to_list(length=effective_limit)

        if self.settings.fusion_strategy == "app_side":
            _fusion_path.set("app_side_rrf")
            return await self._search_hybrid_app_side(
                collection=collection,
                query=query,
                query_vector=query_vector,
                effective_limit=effective_limit,
                vector_weight=vector_weight or self.settings.hybrid_vector_weight,
                text_weight=text_weight or self.settings.hybrid_text_weight,
                allowed_scopes=allowed_scopes,
                server=server,
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
            server=server,
        )
        # Native server-side $rankFusion (the differentiator). Under QE this MUST run
        # through the bypass-auto-encryption client: the shared client's crypt_shared
        # query analysis can't resolve $rankFusion's sub-pipeline namespaces. tool_catalog
        # has no encrypted fields, so the bypass is safe and decryption still works.
        # See QUERYABLE_ENCRYPTION_CAVEATS.md.
        search_collection = get_tenant_database_for_search(resolved_tenant_id)["tool_catalog"]
        try:
            cursor = await search_collection.aggregate(pipeline)
            results = await cursor.to_list(length=effective_limit)
            _fusion_path.set("native_rankfusion")
            return results
        except (OperationFailure, EncryptionError) as exc:
            # GA-safe fallback: run both retrievers and fuse with app-side RRF.
            # OperationFailure covers servers without native $rankFusion; EncryptionError
            # covers the residual QE case (e.g. a non-bypass client, or the known
            # empty-sub-pipeline analysis bug). The single-stage $vectorSearch/$search
            # arms used by the app-side path analyze fine under QE, so hybrid still works.
            _fusion_path.set("app_side_rrf")
            reason = exc.__class__.__name__
            log = logger.warning if reason not in _warned_fallback_reasons else logger.debug
            _warned_fallback_reasons.add(reason)
            log(
                "Native $rankFusion unavailable (%s); using app-side RRF fallback "
                "[tenant=%s mode=%s]. See QUERYABLE_ENCRYPTION_CAVEATS.md.",
                reason,
                resolved_tenant_id,
                mode,
            )
            return await self._search_hybrid_app_side(
                collection=collection,
                query=query,
                query_vector=query_vector,
                effective_limit=effective_limit,
                vector_weight=vector_weight or self.settings.hybrid_vector_weight,
                text_weight=text_weight or self.settings.hybrid_text_weight,
                allowed_scopes=allowed_scopes,
                server=server,
            )

    async def _fetch_always_included(
        self,
        *,
        collection: Any,
        allowed_scopes: list[str] | None,
        server: str | None,
    ) -> list[dict[str, Any]]:
        """Fetch tools flagged ``metadata.always_included`` for this caller.

        This is a plain metadata filter (not an Atlas search), so a ``find`` is
        the cheapest, most honest choice. The same identity-bound scope filter
        the ranked arms use is applied here too -- pinning never surfaces a tool
        the caller could not otherwise discover.
        """
        scopes = _normalize_scopes(allowed_scopes)
        match: dict[str, Any] = {"metadata.always_included": True}
        if scopes is not None:
            match.update(_scope_filter(scopes, server=server))
        elif server:
            match["server"] = server
        cursor = collection.find(match, dict(_BASE_PROJECTION))
        docs = await cursor.to_list(length=None)
        # Deterministic order; pinned sets are tiny so an in-process sort is fine.
        docs.sort(key=lambda doc: (doc.get("server") or "", doc.get("name") or ""))
        return docs

    @staticmethod
    def _merge_pinned(
        *,
        pinned: list[dict[str, Any]],
        ranked: list[dict[str, Any]],
        output_limit: int,
    ) -> list[dict[str, Any]]:
        """Place pinned tools first, then fill remaining budget with relevance.

        Pinned tools take "reserved seats" inside the caller's ``limit`` so the
        prompt cost stays bounded in the common case. If an admin pins more tools
        than ``limit``, all pinned tools are still returned -- the override is
        explicit and the UI already warns about the recommended cap -- but the
        relevance tail is then empty. Tools that are both pinned and relevant are
        de-duplicated by ``(server, name)`` and appear once, at the top.
        """
        merged: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any]] = set()
        for doc in pinned:
            key = (doc.get("server"), doc.get("name"))
            if key in seen:
                continue
            seen.add(key)
            tagged = dict(doc)
            tagged["pinned"] = True
            merged.append(tagged)

        remaining = max(0, output_limit - len(merged))
        if remaining:
            for doc in ranked:
                key = (doc.get("server"), doc.get("name"))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(doc)
                remaining -= 1
                if remaining == 0:
                    break
        return merged

    async def list_tools(
        self,
        *,
        tenant_id: str | None = None,
        allowed_scopes: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
        server: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the catalog (optionally scope-filtered) with no ranking.

        Used by tools/list when the caller does not supply a routing query, so
        discovery still respects identity-bound scope even without a search.
        """
        scopes = _normalize_scopes(allowed_scopes)
        match: dict[str, Any]
        if scopes is not None:
            match = _scope_filter(scopes, server=server)
        elif server:
            match = {"server": server}
        else:
            match = {}
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
        server: str | None,
    ) -> list[dict[str, Any]]:
        vector_pipeline = build_vector_pipeline(
            query_vector=query_vector,
            num_candidates=self.settings.hybrid_num_candidates,
            output_limit=self.settings.hybrid_pipeline_limit,
            allowed_scopes=allowed_scopes,
            server=server,
        )
        text_pipeline = build_text_pipeline(
            query=query,
            output_limit=self.settings.hybrid_pipeline_limit,
            allowed_scopes=allowed_scopes,
            server=server,
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
