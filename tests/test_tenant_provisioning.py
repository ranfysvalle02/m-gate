"""Tests for lazy tenant auto-provisioning (services/tenant_provisioner.py).

These verify the "provision-on-first-use, or fail loudly" contract that replaces
the previous silent empty-result behavior for unknown tenants.
"""

from __future__ import annotations

import pytest

import database.mongo as mongo_module
import services.tenant_provisioner as tp
from config.settings import get_settings
from services.tenant_provisioner import (
    UnknownTenantError,
    ensure_tenant_ready,
)


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
