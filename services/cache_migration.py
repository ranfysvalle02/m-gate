from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from config.settings import Settings, get_settings
from database.mongo import get_tenant_database
from services.cache_manager import SemanticCacheManager, semantic_cache_index_name
from services.embeddings import EmbeddingService, embedding_version_for, get_embedding_service
from services.metrics import observe_cache_event

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
        docs = await collection.find({}).to_list(length=100_000)
        counts_by_version: dict[str, int] = {}
        stale_docs: list[dict[str, Any]] = []
        for doc in docs:
            version = str(doc.get("embedding_version") or "unversioned")
            counts_by_version[version] = counts_by_version.get(version, 0) + 1
            if version != self.active_embedding_version:
                stale_docs.append(doc)

        index_names = await self._semantic_cache_index_names(collection)
        summary: dict[str, Any] = {
            "tenant_id": tenant_id,
            "total_entries": len(docs),
            "active_entries": counts_by_version.get(self.active_embedding_version, 0),
            "stale_entries": len(stale_docs),
            "counts_by_version": counts_by_version,
            "active_index_present": self.active_index_name in index_names,
            "index_names": sorted(index_names),
            "purged_entries": 0,
            "reembedded_entries": 0,
            "skipped_entries": 0,
            "remaining_entries": len(stale_docs),
        }
        if mode == "status":
            return summary

        if mode == "purge":
            summary["purged_entries"] = await self._purge_stale_entries(
                collection, stale_docs=stale_docs
            )
            summary["remaining_entries"] = max(
                0, summary["stale_entries"] - summary["purged_entries"]
            )
            return summary

        reembedded, skipped = await self._reembed_stale_entries(
            tenant_id=tenant_id,
            collection=collection,
            stale_docs=stale_docs,
            batch_size=batch_size,
        )
        summary["reembedded_entries"] = reembedded
        summary["skipped_entries"] = skipped
        summary["remaining_entries"] = max(0, summary["stale_entries"] - reembedded - skipped)
        return summary

    async def _semantic_cache_index_names(self, collection: Any) -> set[str]:
        try:
            cursor = await collection.list_search_indexes()
            rows = await cursor.to_list(length=200)
        except Exception:
            return set()
        return {str(row.get("name")) for row in rows if row.get("name")}

    async def _purge_stale_entries(
        self,
        collection: Any,
        *,
        stale_docs: list[dict[str, Any]],
    ) -> int:
        deleted = 0
        for doc in stale_docs:
            query = self._stale_doc_query(doc)
            result = await collection.delete_many(query)
            deleted += int(result.deleted_count)
        return deleted

    async def _reembed_stale_entries(
        self,
        *,
        tenant_id: str,
        collection: Any,
        stale_docs: list[dict[str, Any]],
        batch_size: int,
    ) -> tuple[int, int]:
        reembedded = 0
        skipped = 0
        chunk_size = max(1, batch_size)
        for idx in range(0, len(stale_docs), chunk_size):
            chunk = stale_docs[idx : idx + chunk_size]
            for doc in chunk:
                tool_name = doc.get("tool_name")
                arguments = doc.get("arguments")
                if not isinstance(tool_name, str) or not isinstance(arguments, dict):
                    skipped += 1
                    continue

                ttl_seconds = self._remaining_ttl_seconds(doc.get("expires_at"))
                await self.cache_manager.store(
                    tool_name,
                    arguments,
                    doc.get("result"),
                    tenant_id=tenant_id,
                    ttl_seconds=ttl_seconds,
                )
                await collection.delete_many(self._stale_doc_query(doc))
                reembedded += 1

        return reembedded, skipped

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
