from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from database.mongo import get_tenant_database
from services.cache_migration import SemanticCacheMigrationService


def _seed_cache_doc(*, tenant_id: str, tool_name: str, version: str, suffix: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "tool_name": tool_name,
        "arguments": {"id": suffix},
        "arguments_hash": f"hash-{suffix}",
        "embedding_version": version,
        "embedding": [0.1, 0.2, 0.3],
        "result": {"ok": suffix},
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }


@pytest.mark.asyncio
async def test_cache_migration_status_reports_version_counts(patch_mongo, fake_embeddings):
    service = SemanticCacheMigrationService(embedding_service=fake_embeddings)
    tenant = "local-dev"
    coll = get_tenant_database(tenant)["semantic_cache"]
    coll.docs.extend(
        [
            _seed_cache_doc(
                tenant_id=tenant,
                tool_name="find_order",
                version=service.active_embedding_version,
                suffix="active",
            ),
            _seed_cache_doc(
                tenant_id=tenant,
                tool_name="find_order",
                version="prev-model:8",
                suffix="stale",
            ),
        ]
    )

    out = await service.migrate(tenant_ids=[tenant], mode="status")
    summary = out["tenants"][0]
    assert summary["active_entries"] == 1
    assert summary["stale_entries"] == 1
    assert summary["counts_by_version"][service.active_embedding_version] == 1
    assert summary["counts_by_version"]["prev-model:8"] == 1


@pytest.mark.asyncio
async def test_cache_migration_purge_removes_stale_versions(patch_mongo, fake_embeddings):
    service = SemanticCacheMigrationService(embedding_service=fake_embeddings)
    tenant = "local-dev"
    coll = get_tenant_database(tenant)["semantic_cache"]
    coll.docs.extend(
        [
            _seed_cache_doc(
                tenant_id=tenant,
                tool_name="find_order",
                version=service.active_embedding_version,
                suffix="active",
            ),
            _seed_cache_doc(
                tenant_id=tenant,
                tool_name="find_order",
                version="prev-model:8",
                suffix="stale",
            ),
        ]
    )

    out = await service.migrate(tenant_ids=[tenant], mode="purge")
    summary = out["tenants"][0]
    assert summary["purged_entries"] == 1
    assert all(doc["embedding_version"] == service.active_embedding_version for doc in coll.docs)


@pytest.mark.asyncio
async def test_cache_migration_reembed_updates_stale_entries(patch_mongo, fake_embeddings):
    service = SemanticCacheMigrationService(embedding_service=fake_embeddings)
    tenant = "local-dev"
    coll = get_tenant_database(tenant)["semantic_cache"]
    coll.docs.append(
        _seed_cache_doc(
            tenant_id=tenant,
            tool_name="find_order",
            version="prev-model:8",
            suffix="stale",
        )
    )

    out = await service.migrate(tenant_ids=[tenant], mode="reembed", batch_size=10)
    summary = out["tenants"][0]
    assert summary["reembedded_entries"] == 1
    versions = {doc.get("embedding_version") for doc in coll.docs}
    assert service.active_embedding_version in versions
    assert "prev-model:8" not in versions
