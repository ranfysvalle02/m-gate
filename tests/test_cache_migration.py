from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from database.mongo import get_tenant_database
from services.cache_migration import SemanticCacheMigrationService


def _seed_cache_doc(*, tenant_id: str, tool_name: str, version: str, suffix: str) -> dict:
    return {
        "_id": f"oid-{suffix}",
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
    assert summary["remaining_entries"] == 0
    versions = {doc.get("embedding_version") for doc in coll.docs}
    assert service.active_embedding_version in versions
    assert "prev-model:8" not in versions


@pytest.mark.asyncio
async def test_cache_migration_reembed_processes_all_stale_docs_over_batch_size(
    patch_mongo, fake_embeddings
):
    service = SemanticCacheMigrationService(embedding_service=fake_embeddings)
    tenant = "local-dev"
    coll = get_tenant_database(tenant)["semantic_cache"]
    for idx in range(5):
        coll.docs.append(
            _seed_cache_doc(
                tenant_id=tenant,
                tool_name="find_order",
                version="prev-model:8",
                suffix=f"stale-{idx}",
            )
        )

    out = await service.migrate(tenant_ids=[tenant], mode="reembed", batch_size=2)
    summary = out["tenants"][0]
    assert summary["stale_entries"] == 5
    assert summary["reembedded_entries"] == 5
    assert summary["skipped_entries"] == 0
    assert summary["remaining_entries"] == 0
    assert out["totals"]["reembedded_entries"] == 5
    assert out["totals"]["remaining_entries"] == 0


@pytest.mark.asyncio
async def test_cache_migration_purge_pages_with_batched_delete(patch_mongo, fake_embeddings):
    service = SemanticCacheMigrationService(embedding_service=fake_embeddings)
    tenant = "local-dev"
    coll = get_tenant_database(tenant)["semantic_cache"]
    for idx in range(5):
        coll.docs.append(
            _seed_cache_doc(
                tenant_id=tenant,
                tool_name="find_order",
                version="prev-model:8",
                suffix=f"stale-{idx}",
            )
        )
    coll.docs.append(
        _seed_cache_doc(
            tenant_id=tenant,
            tool_name="find_order",
            version=service.active_embedding_version,
            suffix="active",
        )
    )

    delete_calls: list[dict] = []
    original_delete_many = coll.delete_many

    async def _spy_delete_many(query):
        delete_calls.append(query)
        return await original_delete_many(query)

    coll.delete_many = _spy_delete_many  # type: ignore[method-assign]

    # page_size 2 over 5 stale docs => 3 delete pages (2 + 2 + 1).
    out = await service.migrate(tenant_ids=[tenant], mode="purge", batch_size=2)
    summary = out["tenants"][0]
    assert summary["stale_entries"] == 5
    assert summary["purged_entries"] == 5
    assert summary["remaining_entries"] == 0
    # Only the active doc survives.
    assert [d["embedding_version"] for d in coll.docs] == [service.active_embedding_version]
    # Deletes were batched by _id (one delete_many per page), not one per doc.
    assert len(delete_calls) == 3
    assert all("_id" in q and "$in" in q["_id"] for q in delete_calls)


@pytest.mark.asyncio
async def test_cache_migration_reembed_uses_batched_embed_calls(patch_mongo, fake_embeddings):
    service = SemanticCacheMigrationService(embedding_service=fake_embeddings)
    tenant = "local-dev"
    coll = get_tenant_database(tenant)["semantic_cache"]
    for idx in range(5):
        coll.docs.append(
            _seed_cache_doc(
                tenant_id=tenant,
                tool_name="find_order",
                version="prev-model:8",
                suffix=f"stale-{idx}",
            )
        )

    embed_texts_batches: list[int] = []
    original_embed_texts = fake_embeddings.embed_texts

    async def _spy_embed_texts(texts):
        items = list(texts)
        embed_texts_batches.append(len(items))
        return await original_embed_texts(items)

    fake_embeddings.embed_texts = _spy_embed_texts  # type: ignore[method-assign]
    embed_text_before = len(fake_embeddings.calls)

    out = await service.migrate(tenant_ids=[tenant], mode="reembed", batch_size=2)
    summary = out["tenants"][0]
    assert summary["reembedded_entries"] == 5
    assert summary["remaining_entries"] == 0

    # The batch embedding API was used once per page (2 + 2 + 1 docs), rather
    # than a single embed_text call per document.
    assert embed_texts_batches == [2, 2, 1]
    # Each text was embedded exactly once (store reused the precomputed vector
    # instead of re-embedding), so the total embed_text count equals doc count.
    assert len(fake_embeddings.calls) - embed_text_before == 5


@pytest.mark.asyncio
async def test_cache_migration_reembed_purges_malformed_stale_docs(patch_mongo, fake_embeddings):
    service = SemanticCacheMigrationService(embedding_service=fake_embeddings)
    tenant = "local-dev"
    coll = get_tenant_database(tenant)["semantic_cache"]
    # A re-embeddable stale doc plus a malformed one (missing arguments) that can
    # never be re-embedded. The malformed doc must still be deleted, otherwise a
    # paged find(stale).limit() loop would re-fetch it forever.
    coll.docs.append(
        _seed_cache_doc(
            tenant_id=tenant,
            tool_name="find_order",
            version="prev-model:8",
            suffix="good",
        )
    )
    coll.docs.append(
        {
            "_id": "oid-bad",
            "tenant_id": tenant,
            "tool_name": "find_order",
            "arguments": "not-a-dict",
            "arguments_hash": "hash-bad",
            "embedding_version": "prev-model:8",
            "result": {"ok": "bad"},
        }
    )

    out = await service.migrate(tenant_ids=[tenant], mode="reembed", batch_size=1)
    summary = out["tenants"][0]
    assert summary["stale_entries"] == 2
    assert summary["reembedded_entries"] == 1
    assert summary["skipped_entries"] == 1
    assert summary["remaining_entries"] == 0
    # The loop terminated: no stale docs remain (malformed one was purged).
    versions = {doc.get("embedding_version") for doc in coll.docs}
    assert "prev-model:8" not in versions
    assert service.active_embedding_version in versions
