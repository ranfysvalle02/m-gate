from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

from config.settings import Settings, get_settings
from database.errors import (
    is_index_already_exists,
    is_index_not_queryable_yet,
    is_namespace_not_found,
)
from database.mongo import get_tenant_database
from services.embeddings import EmbeddingService, embedding_version_for, get_embedding_service
from services.metrics import observe_cache_event

SEMANTIC_CACHE_INDEX_PREFIX = "semantic-cache-v"
SEMANTIC_CACHE_VECTOR_PATH = "embedding"
SEMANTIC_CACHE_FILTER_FIELDS = ("tenant_id", "embedding_version")


def semantic_cache_lookup_filter(*, tenant_id: str, embedding_version: str) -> dict[str, str]:
    return {"tenant_id": tenant_id, "embedding_version": embedding_version}


def semantic_cache_index_name(embedding_version: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", embedding_version.lower()).strip("-")
    if not slug:
        slug = "default"
    name = f"{SEMANTIC_CACHE_INDEX_PREFIX}-{slug}"
    if len(name) <= 120:
        return name

    digest = hashlib.sha1(embedding_version.encode("utf-8")).hexdigest()[:12]
    max_slug_len = max(8, 120 - len(SEMANTIC_CACHE_INDEX_PREFIX) - len(digest) - 2)
    return f"{SEMANTIC_CACHE_INDEX_PREFIX}-{slug[:max_slug_len]}-{digest}"


def semantic_cache_index_spec(*, embedding_version: str, dimensions: int) -> dict[str, Any]:
    return {
        "name": semantic_cache_index_name(embedding_version),
        "definition": {
            "fields": [
                {
                    "type": "vector",
                    "path": SEMANTIC_CACHE_VECTOR_PATH,
                    "numDimensions": dimensions,
                    "similarity": "cosine",
                },
                *({"type": "filter", "path": path} for path in SEMANTIC_CACHE_FILTER_FIELDS),
            ]
        },
    }


class SemanticCacheManager:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        embedding_service: EmbeddingService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.embedding_service = embedding_service or get_embedding_service(self.settings)
        self._ensured_indexes: set[tuple[str, str]] = set()
        self._ensure_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._ensure_locks_guard = asyncio.Lock()

    @property
    def embedding_version(self) -> str:
        return embedding_version_for(self.embedding_service)

    @property
    def embedding_model(self) -> str:
        return self.embedding_service.model_id

    @property
    def embedding_dim(self) -> int:
        return self.embedding_service.dimensions

    @property
    def index_spec(self) -> dict[str, Any]:
        return semantic_cache_index_spec(
            embedding_version=self.embedding_version,
            dimensions=self.embedding_dim,
        )

    async def lookup(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        collection = get_tenant_database(tenant_id)["semantic_cache"]
        await self._ensure_index(tenant_id=tenant_id, collection=collection)
        serialized = self._serialize(tool_name, arguments)
        query_vector = await self.embedding_service.embed_text(serialized)
        query_filter = semantic_cache_lookup_filter(
            tenant_id=tenant_id,
            embedding_version=self.embedding_version,
        )
        pipeline = [
            {
                "$vectorSearch": {
                    "index": self.index_spec["name"],
                    "path": SEMANTIC_CACHE_VECTOR_PATH,
                    "queryVector": query_vector,
                    "filter": query_filter,
                    "numCandidates": 50,
                    "limit": 1,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "tenant_id": 1,
                    "tool_name": 1,
                    "arguments_hash": 1,
                    "embedding_version": 1,
                    "result": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ]
        try:
            cursor = await collection.aggregate(pipeline)
            docs = await cursor.to_list(length=1)
        except OperationFailure as exc:
            # Freshly-created Atlas vector indexes may take a brief moment before
            # they become queryable. Treat this as a miss so callers can retry.
            if is_index_not_queryable_yet(exc):
                return None
            raise
        if not docs:
            return None
        top = docs[0]
        if top.get("tenant_id") != tenant_id:
            return None
        if top.get("embedding_version") != self.embedding_version:
            observe_cache_event("version_skip")
            return None
        if top.get("tool_name") != tool_name:
            return None
        if top.get("score", 0) < self.settings.semantic_cache_threshold:
            return None
        return top.get("result")

    async def store(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        *,
        tenant_id: str,
        ttl_seconds: int = 24 * 3600,
    ) -> None:
        collection = get_tenant_database(tenant_id)["semantic_cache"]
        serialized = self._serialize(tool_name, arguments)
        embedding = await self.embedding_service.embed_text(serialized)
        arguments_hash = self._hash(serialized)
        expires_at = datetime.now(UTC) + timedelta(seconds=max(1, ttl_seconds))
        await collection.update_one(
            {
                "tenant_id": tenant_id,
                "tool_name": tool_name,
                "arguments_hash": arguments_hash,
                "embedding_version": self.embedding_version,
            },
            {
                "$set": {
                    "tenant_id": tenant_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "arguments_hash": arguments_hash,
                    "embedding": embedding,
                    "embedding_model": self.embedding_model,
                    "embedding_dim": self.embedding_dim,
                    "embedding_version": self.embedding_version,
                    "result": result,
                    "expires_at": expires_at,
                    "updated_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )
        await self._ensure_index(tenant_id=tenant_id, collection=collection)

    async def invalidate(self, *, tenant_id: str, tool_names: list[str]) -> int:
        names = [name for name in tool_names if name]
        if not names:
            return 0
        result = await get_tenant_database(tenant_id)["semantic_cache"].delete_many(
            {"tenant_id": tenant_id, "tool_name": {"$in": names}}
        )
        return int(result.deleted_count)

    @staticmethod
    def _serialize(tool_name: str, arguments: dict[str, Any]) -> str:
        return f"{tool_name}:{json.dumps(arguments, sort_keys=True, separators=(',', ':'))}"

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    async def _ensure_index(self, *, tenant_id: str, collection: Any) -> None:
        key = (tenant_id, self.embedding_version)
        if key in self._ensured_indexes:
            return

        lock = await self._ensure_lock_for(key)
        async with lock:
            if key in self._ensured_indexes:
                return

            spec = self.index_spec
            model = SearchIndexModel(
                name=spec["name"],
                type="vectorSearch",
                definition=spec["definition"],
            )
            try:
                await collection.create_search_index(model=model)
            except OperationFailure as exc:
                if is_index_already_exists(exc):
                    await collection.update_search_index(
                        name=spec["name"],
                        definition=spec["definition"],
                    )
                elif is_namespace_not_found(exc):
                    return
                else:
                    raise

            self._ensured_indexes.add(key)

    async def _ensure_lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        async with self._ensure_locks_guard:
            lock = self._ensure_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._ensure_locks[key] = lock
            return lock
