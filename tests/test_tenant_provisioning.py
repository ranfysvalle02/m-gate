"""Tests for lazy tenant auto-provisioning (services/tenant_provisioner.py).

These verify the "provision-on-first-use, or fail loudly" contract that replaces
the previous silent empty-result behavior for unknown tenants.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import database.mongo as mongo_module
import services.tenant_provisioner as tp
from config.settings import get_settings
from services.tenant_provisioner import (
    UnknownTenantError,
    deprovision_tenant,
    ensure_tenant_ready,
    purge_expired_tenants,
    restore_tenant,
    soft_delete_tenant,
)
from services.tenant_status import STATUS_ACTIVE, STATUS_DELETED, get_tenant_status


@pytest.mark.asyncio
async def test_ensure_tenant_ready_provisions_unknown_tenant(patch_mongo):
    settings = get_settings()
    object.__setattr__(settings, "auto_provision_tenants", True)

    result = await ensure_tenant_ready("brand-new", settings=settings)

    assert result is True
    control = mongo_module.get_control_database()
    doc = await control["tenants"].find_one({"tenant_id": "brand-new"})
    assert doc is not None
    assert doc["tenant_id"] == "brand-new"
    # Cached as ready for the rest of the process.
    assert "brand-new" in tp._ready_tenants  # noqa: SLF001 - whitebox cache check


@pytest.mark.asyncio
async def test_ensure_tenant_ready_short_circuits_existing_tenant(patch_mongo, monkeypatch):
    settings = get_settings()
    object.__setattr__(settings, "auto_provision_tenants", True)
    control = mongo_module.get_control_database()
    await control["tenants"].insert_one({"tenant_id": "known"})

    calls = {"provision": 0}

    async def _spy_provision(tenant_id, **kwargs):  # pragma: no cover - should not run
        calls["provision"] += 1
        return tenant_id

    monkeypatch.setattr(tp, "provision_tenant", _spy_provision)

    assert await ensure_tenant_ready("known", settings=settings) is True
    assert calls["provision"] == 0


@pytest.mark.asyncio
async def test_ensure_tenant_ready_raises_when_auto_provision_disabled(patch_mongo):
    settings = get_settings()
    object.__setattr__(settings, "auto_provision_tenants", False)

    with pytest.raises(UnknownTenantError) as excinfo:
        await ensure_tenant_ready("ghost-tenant", settings=settings)

    assert excinfo.value.tenant_id == "ghost-tenant"
    assert "not provisioned" in str(excinfo.value)


@pytest.mark.asyncio
async def test_ensure_tenant_ready_is_cached_after_first_provision(patch_mongo, monkeypatch):
    settings = get_settings()
    object.__setattr__(settings, "auto_provision_tenants", True)

    calls = {"provision": 0}

    async def _counting_provision(tenant_id, **kwargs):
        calls["provision"] += 1
        control = mongo_module.get_control_database()
        await control["tenants"].insert_one({"tenant_id": tenant_id})
        return tenant_id

    monkeypatch.setattr(tp, "provision_tenant", _counting_provision)

    await ensure_tenant_ready("repeat", settings=settings)
    await ensure_tenant_ready("repeat", settings=settings)

    assert calls["provision"] == 1


@pytest.mark.asyncio
async def test_watcher_state_index_is_recreated_when_ttl_changes():
    class _Cursor:
        async def to_list(self, length: int):
            return [{"name": "updated_at_1", "expireAfterSeconds": 300}]

    class _Collection:
        def __init__(self) -> None:
            self.dropped: list[str] = []
            self.created: list[tuple[str, int]] = []

        def list_indexes(self):
            return _Cursor()

        async def drop_index(self, name: str):
            self.dropped.append(name)

        async def create_index(self, key: str, **kwargs):
            self.created.append((key, int(kwargs["expireAfterSeconds"])))

    collection = _Collection()
    control_db = {"watcher_state": collection}

    await tp._ensure_watcher_state_ttl_index(control_db=control_db, ttl_seconds=86400)  # noqa: SLF001

    assert collection.dropped == ["updated_at_1"]
    assert collection.created == [("updated_at", 86400)]


@pytest.mark.asyncio
async def test_provision_tenant_uses_tenant_embedding_identity_dimensions(patch_mongo, monkeypatch):
    async def _tenant_identity(tenant_id: str, settings=None):
        return ("tenant-model", 42, "tenant-model:42")

    monkeypatch.setattr(tp, "tenant_embedding_identity", _tenant_identity)
    db_name = await tp.provision_tenant("tenant-dims", wait_for_queryable_indexes=False)
    assert db_name.startswith(get_settings().tenant_db_prefix)

    tenant_db = mongo_module.get_tenant_database("tenant-dims")
    cache_indexes = tenant_db["semantic_cache"]._search_indexes
    assert cache_indexes
    cache_vector_dims = next(
        field["numDimensions"]
        for index in cache_indexes.values()
        for field in index["definition"]["fields"]
        if field.get("type") == "vector"
    )
    assert cache_vector_dims == 42

    catalog_indexes = tenant_db["tool_catalog"]._search_indexes
    vector_index = catalog_indexes["hybrid-vector-search"]
    catalog_vector_dims = next(
        field["numDimensions"]
        for field in vector_index["definition"]["fields"]
        if field.get("type") == "vector"
    )
    assert catalog_vector_dims == 42


@pytest.mark.asyncio
async def test_provision_tenant_qe_branch_ensures_tenant_data_key(patch_mongo, monkeypatch):
    settings = get_settings()
    object.__setattr__(settings, "qe_enabled", True)
    calls = {"vault": 0, "registry": 0, "tenant_key": 0}

    async def _ensure_key_vault(settings=None):
        calls["vault"] += 1

    async def _create_encrypted_routing_registry(tenant_db, settings=None):
        calls["registry"] += 1
        return tenant_db["routing_registry"]

    async def _ensure_tenant_data_key(tenant_id: str, settings=None):
        calls["tenant_key"] += 1
        assert tenant_id == "tenant-qe"

    monkeypatch.setattr(tp, "ensure_key_vault", _ensure_key_vault)
    monkeypatch.setattr(tp, "create_encrypted_routing_registry", _create_encrypted_routing_registry)
    monkeypatch.setattr(tp, "ensure_tenant_data_key", _ensure_tenant_data_key)

    await tp.provision_tenant("tenant-qe", wait_for_queryable_indexes=False)
    assert calls == {"vault": 1, "registry": 1, "tenant_key": 1}


@pytest.mark.asyncio
async def test_deprovision_tenant_drops_db_and_evicts_ready_cache(patch_mongo):
    settings = get_settings()
    original_qe = settings.qe_enabled
    object.__setattr__(settings, "qe_enabled", False)
    await tp.provision_tenant("tenant-remove", wait_for_queryable_indexes=False)
    assert "tenant-remove" in tp._ready_tenants  # noqa: SLF001
    assert "tenant-remove" in tp._provision_locks  # noqa: SLF001

    try:
        deleted = await deprovision_tenant("tenant-remove")
        assert deleted is True

        control = mongo_module.get_control_database()
        assert await control["tenants"].find_one({"tenant_id": "tenant-remove"}) is None
        assert "tenant-remove" not in tp._ready_tenants  # noqa: SLF001
        assert "tenant-remove" not in tp._ready_locks  # noqa: SLF001
        assert "tenant-remove" not in tp._provision_locks  # noqa: SLF001

        db_name = mongo_module.tenant_db_name("tenant-remove")
        client = mongo_module.get_client()
        assert db_name not in client._databases  # noqa: SLF001
    finally:
        object.__setattr__(settings, "qe_enabled", original_qe)


@pytest.mark.asyncio
async def test_deprovision_unknown_tenant_returns_false_and_clears_cache(patch_mongo):
    tp._ready_tenants.add("ghost-tenant")  # noqa: SLF001
    deleted = await deprovision_tenant("ghost-tenant")
    assert deleted is False
    assert "ghost-tenant" not in tp._ready_tenants  # noqa: SLF001


@pytest.mark.asyncio
async def test_soft_delete_marks_deleted_and_keeps_db(patch_mongo):
    settings = get_settings()
    object.__setattr__(settings, "qe_enabled", False)
    await tp.provision_tenant("soft-1", wait_for_queryable_indexes=False)
    db_name = mongo_module.tenant_db_name("soft-1")
    client = mongo_module.get_client()
    assert db_name in client._databases  # noqa: SLF001

    doc = await soft_delete_tenant("soft-1", retention_days=30, actor="ops@x")

    assert doc is not None
    assert doc["status"] == STATUS_DELETED
    assert doc["deleted_at"] is not None
    assert doc["purge_at"] is not None
    # The database is RETAINED on soft-delete (reversible) and the hot-path ready
    # cache is evicted so the tenant is re-checked (and rejected).
    assert db_name in client._databases  # noqa: SLF001
    assert "soft-1" not in tp._ready_tenants  # noqa: SLF001
    assert await get_tenant_status("soft-1") == STATUS_DELETED


@pytest.mark.asyncio
async def test_soft_delete_unknown_tenant_returns_none(patch_mongo):
    assert await soft_delete_tenant("ghost", retention_days=1) is None


@pytest.mark.asyncio
async def test_restore_reverses_soft_delete(patch_mongo):
    settings = get_settings()
    object.__setattr__(settings, "qe_enabled", False)
    await tp.provision_tenant("restore-1", wait_for_queryable_indexes=False)
    await soft_delete_tenant("restore-1", retention_days=30)

    restored = await restore_tenant("restore-1", actor="ops@x")

    assert restored is not None
    assert restored["status"] == STATUS_ACTIVE
    assert restored["deleted_at"] is None
    assert restored["purge_at"] is None
    assert await get_tenant_status("restore-1") == STATUS_ACTIVE


@pytest.mark.asyncio
async def test_restore_returns_none_when_not_soft_deleted(patch_mongo):
    settings = get_settings()
    object.__setattr__(settings, "qe_enabled", False)
    await tp.provision_tenant("active-1", wait_for_queryable_indexes=False)
    # An active (never-deleted) tenant has nothing to restore.
    assert await restore_tenant("active-1") is None


@pytest.mark.asyncio
async def test_purge_reaper_drops_only_due_tenants(patch_mongo):
    settings = get_settings()
    object.__setattr__(settings, "qe_enabled", False)
    await tp.provision_tenant("due", wait_for_queryable_indexes=False)
    await tp.provision_tenant("not-due", wait_for_queryable_indexes=False)
    # "due" is past its retention window; "not-due" is far in the future.
    await soft_delete_tenant("due", retention_days=0)
    await soft_delete_tenant("not-due", retention_days=30)

    client = mongo_module.get_client()
    due_db = mongo_module.tenant_db_name("due")
    not_due_db = mongo_module.tenant_db_name("not-due")

    purged = await purge_expired_tenants(now=datetime.now(UTC) + timedelta(minutes=1))

    assert purged == 1
    control = mongo_module.get_control_database()
    assert await control["tenants"].find_one({"tenant_id": "due"}) is None
    assert due_db not in client._databases  # noqa: SLF001
    # The not-yet-expired tenant survives untouched.
    assert await control["tenants"].find_one({"tenant_id": "not-due"}) is not None
    assert not_due_db in client._databases  # noqa: SLF001


@pytest.mark.asyncio
async def test_purge_reaper_skips_already_claimed_tenants(patch_mongo):
    settings = get_settings()
    object.__setattr__(settings, "qe_enabled", False)
    await tp.provision_tenant("claimed", wait_for_queryable_indexes=False)
    await soft_delete_tenant("claimed", retention_days=0)
    control = mongo_module.get_control_database()
    # Simulate another replica having already claimed this tenant for purge.
    await control["tenants"].update_one(
        {"tenant_id": "claimed"},
        {"$set": {"purge_started_at": datetime.now(UTC)}},
    )

    purged = await purge_expired_tenants(now=datetime.now(UTC) + timedelta(minutes=1))

    assert purged == 0
    # The claimed doc is left intact for the claiming replica to finish.
    assert await control["tenants"].find_one({"tenant_id": "claimed"}) is not None


@pytest.mark.asyncio
async def test_purge_reaper_releases_claim_on_failure_for_retry(patch_mongo, monkeypatch):
    settings = get_settings()
    object.__setattr__(settings, "qe_enabled", False)
    await tp.provision_tenant("flaky", wait_for_queryable_indexes=False)
    await soft_delete_tenant("flaky", retention_days=0)
    control = mongo_module.get_control_database()

    # A transient failure in the hard-drop must not strand the tenant: the claim
    # is released so the next sweep retries (deprovision is idempotent).
    calls = {"n": 0}
    original_deprovision = tp.deprovision_tenant

    async def _flaky_deprovision(tenant_id: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient dropDatabase failure")
        return await original_deprovision(tenant_id)

    monkeypatch.setattr(tp, "deprovision_tenant", _flaky_deprovision)

    when = datetime.now(UTC) + timedelta(minutes=1)
    first = await purge_expired_tenants(now=when)
    assert first == 0
    # Claim was released (not stranded), so the doc is still present and unclaimed.
    doc = await control["tenants"].find_one({"tenant_id": "flaky"})
    assert doc is not None
    assert doc.get("purge_started_at") is None

    # The next sweep succeeds and the tenant is finally purged.
    second = await purge_expired_tenants(now=when)
    assert second == 1
    assert await control["tenants"].find_one({"tenant_id": "flaky"}) is None
