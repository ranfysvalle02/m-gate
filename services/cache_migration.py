from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from config.settings import Settings, get_settings
from database.mongo import get_tenant_database
from services.cache_manager import SemanticCacheManager, semantic_cache_index_name
from services.embeddings import EmbeddingService, embedding_version_for, get_embedding_service
from services.metrics import observe_cache_event

logger = logging.getLogger(__name__)

MigrationMode = Literal["status", "purge", "reembed"]


class SemanticCacheMigrationService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        embedding_service: EmbeddingService | None = None,
        cache_manager: SemanticCacheManager | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.embedding_service = embedding_service or get_embedding_service(self.settings)
        self.cache_manager = cache_manager or SemanticCacheManager(
            settings=self.settings,
            embedding_service=self.embedding_service,
        )

    @property
    def active_embedding_version(self) -> str:
        return embedding_version_for(self.embedding_service)

    @property
    def active_index_name(self) -> str:
        return semantic_cache_index_name(self.active_embedding_version)

    async def migrate(
        self,
        *,
        tenant_ids: list[str],
        mode: MigrationMode,
        batch_size: int = 200,
    ) -> dict[str, Any]:
        summaries: list[dict[str, Any]] = []
        totals = {
            "total_entries": 0,
            "active_entries": 0,
            "stale_entries": 0,
            "purged_entries": 0,
            "reembedded_entries": 0,
            "skipped_entries": 0,
            "remaining_entries": 0,
        }
        for tenant_id in tenant_ids:
            summary = await self._migrate_tenant(
                tenant_id=tenant_id,
                mode=mode,
                batch_size=batch_size,
            )
            observe_cache_event(f"migrate_{mode}")
            summaries.append(summary)
            for key in totals:
                totals[key] += int(summary.get(key, 0))

        return {
            "mode": mode,
            "active_embedding_version": self.active_embedding_version,
            "active_index_name": self.active_index_name,
            "batch_size": batch_size,
            "tenants": summaries,
            "totals": totals,
        }

    async def _migrate_tenant(
        self,
        *,
        tenant_id: str,
        mode: MigrationMode,
        batch_size: int,
    ) -> dict[str, Any]:
        collection = get_tenant_database(tenant_id)["semantic_cache"]
        page_size = self._page_size(batch_size)

        # Memory is O(distinct versions), not O(documents): a projected stream of
        # just ``embedding_version`` avoids pulling every cached vector into RAM.
        counts_by_version = await self._count_by_version(collection)
        total_entries = sum(counts_by_version.values())
        active_entries = counts_by_version.get(self.active_embedding_version, 0)
        stale_entries = total_entries - active_entries

        index_names = await self._semantic_cache_index_names(collection)
        summary: dict[str, Any] = {
            "tenant_id": tenant_id,
            "total_entries": total_entries,
            "active_entries": active_entries,
            "stale_entries": stale_entries,
            "counts_by_version": counts_by_version,
            "active_index_present": self.active_index_name in index_names,
            "index_names": sorted(index_names),
            "purged_entries": 0,
            "reembedded_entries": 0,
            "skipped_entries": 0,
            "remaining_entries": stale_entries,
        }
        if mode == "status":
            return summary

        if mode == "purge":
            purged = await self._purge_stale_entries(collection, page_size=page_size)
            summary["purged_entries"] = purged
            summary["remaining_entries"] = max(0, stale_entries - purged)
            return summary

        reembedded, skipped = await self._reembed_stale_entries(
            tenant_id=tenant_id,
            collection=collection,
            page_size=page_size,
        )
        summary["reembedded_entries"] = reembedded
        summary["skipped_entries"] = skipped
        summary["remaining_entries"] = max(0, stale_entries - reembedded - skipped)
        return summary

    def _page_size(self, batch_size: int) -> int:
        """Streaming window size: caller-supplied ``batch_size`` wins, otherwise
        fall back to the configured default."""
        if batch_size and batch_size > 0:
            return batch_size
        return max(1, self.settings.cache_migration_fetch_page_size)

    def _stale_query(self) -> dict[str, Any]:
        # ``$ne`` also matches docs missing ``embedding_version`` (legacy /
        # unversioned rows), so they are swept as stale too.
        return {"embedding_version": {"$ne": self.active_embedding_version}}

    async def _count_by_version(self, collection: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        cursor = collection.find({}, {"embedding_version": 1})
        async for doc in cursor:
            version = str(doc.get("embedding_version") or "unversioned")
            counts[version] = counts.get(version, 0) + 1
        return counts

    async def _semantic_cache_index_names(self, collection: Any) -> set[str]:
        try:
            cursor = await collection.list_search_indexes()
            rows = await cursor.to_list(length=200)
        except Exception:
            # Treat an unreadable index list as "none found", but log it: a broken
            # connection here can otherwise drive an incorrect migration decision.
            logger.warning(
                "Could not list semantic-cache search indexes; assuming none.", exc_info=True
            )
            return set()
        return {str(row.get("name")) for row in rows if row.get("name")}

    async def _purge_stale_entries(
        self,
        collection: Any,
        *,
        page_size: int,
    ) -> int:
        """Page through stale docs, deleting one batch per page.

        Each page is removed with a single ``delete_many`` keyed on ``_id`` (vs.
        the old delete-per-doc loop), and the next ``find`` advances because the
        just-deleted docs no longer match the stale filter -- guaranteeing
        termination without ever loading the whole collection.
        """
        stale_query = self._stale_query()
        deleted = 0
        while True:
            page = await collection.find(stale_query).limit(page_size).to_list(page_size)
            if not page:
                break
            removed = await self._delete_page(collection, page)
            if removed == 0:
                # Nothing in this page could be identified for deletion; stop
                # rather than re-fetch the same docs forever.
                break
            deleted += removed
        return deleted

    async def _reembed_stale_entries(
        self,
        *,
        tenant_id: str,
        collection: Any,
        page_size: int,
    ) -> tuple[int, int]:
        """Re-embed stale docs page by page with a single batched embed call per
        window and a bounded write fan-out.

        Malformed/un-re-embeddable docs are deleted alongside the re-embedded
        ones: leaving them in place would re-surface them on every paged
        ``find`` and spin the loop forever.
        """
        stale_query = self._stale_query()
        semaphore = asyncio.Semaphore(max(1, self.settings.cache_migration_embed_concurrency))
        reembedded = 0
        skipped = 0
        while True:
            page = await collection.find(stale_query).limit(page_size).to_list(page_size)
            if not page:
                break

            usable: list[dict[str, Any]] = []
            for doc in page:
                tool_name = doc.get("tool_name")
                arguments = doc.get("arguments")
                if isinstance(tool_name, str) and isinstance(arguments, dict):
                    usable.append(doc)
                else:
                    skipped += 1

            if usable:
                # One provider round-trip for the whole window, then bounded
                # concurrent writes reusing the precomputed vectors.
                texts = [self._serialize_doc(doc) for doc in usable]
                vectors = await self.embedding_service.embed_texts(texts)
                await asyncio.gather(
                    *(
                        self._store_one(semaphore, tenant_id, doc, vector)
                        for doc, vector in zip(usable, vectors, strict=True)
                    )
                )
                reembedded += len(usable)

            removed = await self._delete_page(collection, page)
            if removed == 0 and not usable:
                # No progress possible on this page; bail to avoid an unbounded loop.
                break

        return reembedded, skipped

    async def _store_one(
        self,
        semaphore: asyncio.Semaphore,
        tenant_id: str,
        doc: dict[str, Any],
        embedding: list[float],
    ) -> None:
        async with semaphore:
            ttl_seconds = self._remaining_ttl_seconds(doc.get("expires_at"))
            await self.cache_manager.store(
                doc["tool_name"],
                doc["arguments"],
                doc.get("result"),
                tenant_id=tenant_id,
                ttl_seconds=ttl_seconds,
                embedding=embedding,
            )

    def _serialize_doc(self, doc: dict[str, Any]) -> str:
        return self.cache_manager._serialize(doc["tool_name"], doc["arguments"])

    async def _delete_page(self, collection: Any, page: list[dict[str, Any]]) -> int:
        ids = [doc["_id"] for doc in page if doc.get("_id") is not None]
        if ids:
            result = await collection.delete_many({"_id": {"$in": ids}})
            return int(result.deleted_count)
        # Fallback for docs without an ``_id`` (real Mongo always has one; the
        # in-memory test fake may not): delete each by its stale-doc identity so
        # the paged loop still terminates.
        removed = 0
        for doc in page:
            result = await collection.delete_many(self._stale_doc_query(doc))
            removed += int(result.deleted_count)
        return removed

    @staticmethod
    def _remaining_ttl_seconds(expires_at: Any) -> int:
        if isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            return max(1, int((expires_at - datetime.now(UTC)).total_seconds()))
        return 24 * 3600

    @staticmethod
    def _stale_doc_query(doc: dict[str, Any]) -> dict[str, Any]:
        query: dict[str, Any] = {
            "tool_name": doc.get("tool_name"),
            "arguments_hash": doc.get("arguments_hash"),
            "embedding_version": doc.get("embedding_version"),
        }
        if doc.get("tenant_id"):
            query["tenant_id"] = doc.get("tenant_id")
        return query
