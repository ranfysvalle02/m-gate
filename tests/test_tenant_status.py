from __future__ import annotations

import pytest

from config.settings import get_settings
from services.tenant_status import (
    STATUS_ACTIVE,
    STATUS_DELETED,
    STATUS_SUSPENDED,
    TenantDeletedError,
    TenantInactiveError,
    TenantReadOnlyError,
    TenantSuspendedError,
    assert_tenant_active,
    assert_tenant_writable,
    get_tenant_read_only,
    get_tenant_status,
    reset_tenant_status_cache,
    set_tenant_read_only,
    set_tenant_status,
)


@pytest.mark.asyncio
async def test_unknown_tenant_defaults_to_active(patch_mongo):
    # An unprovisioned/unknown tenant must not be treated as suspended, otherwise
    # the kill-switch would fail closed and break first-use provisioning.
    assert await get_tenant_status("never-seen") == STATUS_ACTIVE
    await assert_tenant_active("never-seen")


@pytest.mark.asyncio
async def test_set_suspended_blocks_and_records_reason(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1", "status": "active"})

    doc = await set_tenant_status("t1", STATUS_SUSPENDED, updated_by="ops@x", reason="abuse")
    assert doc is not None
    assert doc["status"] == STATUS_SUSPENDED
    assert doc["suspended_reason"] == "abuse"
    assert doc["status_updated_by"] == "ops@x"

    assert await get_tenant_status("t1") == STATUS_SUSPENDED
    with pytest.raises(TenantSuspendedError) as exc:
        await assert_tenant_active("t1")
    assert exc.value.tenant_id == "t1"
    assert "abuse" in exc.value.reason


@pytest.mark.asyncio
async def test_resume_clears_reason_and_unblocks(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1"})

    await set_tenant_status("t1", STATUS_SUSPENDED, reason="abuse")
    resumed = await set_tenant_status("t1", STATUS_ACTIVE)
    assert resumed is not None
    assert resumed["status"] == STATUS_ACTIVE
    assert resumed["suspended_reason"] == ""

    assert await get_tenant_status("t1") == STATUS_ACTIVE
    await assert_tenant_active("t1")  # does not raise


@pytest.mark.asyncio
async def test_set_status_on_missing_tenant_returns_none(patch_mongo):
    assert await set_tenant_status("ghost", STATUS_SUSPENDED) is None


@pytest.mark.asyncio
async def test_set_deleted_blocks_hot_path_with_deleted_error(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1", "status": "active"})

    doc = await set_tenant_status("t1", STATUS_DELETED, updated_by="ops@x")
    assert doc is not None and doc["status"] == STATUS_DELETED

    assert await get_tenant_status("t1") == STATUS_DELETED
    # A deleted tenant raises the deleted-specific error (a TenantInactiveError),
    # not the suspended one, so each surface can render the right reason.
    with pytest.raises(TenantDeletedError) as exc:
        await assert_tenant_active("t1")
    assert exc.value.tenant_id == "t1"
    assert exc.value.status_code == "tenant_deleted"
    assert isinstance(exc.value, TenantInactiveError)
    assert not isinstance(exc.value, TenantSuspendedError)


@pytest.mark.asyncio
async def test_invalid_status_rejected():
    with pytest.raises(ValueError):
        await set_tenant_status("t1", "paused")


@pytest.mark.asyncio
async def test_unrecognized_stored_status_is_treated_as_active(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1", "status": "weird"})
    assert await get_tenant_status("t1") == STATUS_ACTIVE


@pytest.mark.asyncio
async def test_status_is_cached_within_ttl(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1", "status": "active"})
    # Prime the per-process cache with the current (active) value.
    assert await get_tenant_status("t1") == STATUS_ACTIVE
    # Mutate the doc directly so the in-process cache is intentionally stale.
    await control["tenants"].update_one({"tenant_id": "t1"}, {"$set": {"status": "suspended"}})
    # Within the TTL the cached value is still served (bounded propagation delay).
    assert await get_tenant_status("t1") == STATUS_ACTIVE
    # After a reset the fresh value is read from the control plane.
    reset_tenant_status_cache()
    assert await get_tenant_status("t1") == STATUS_SUSPENDED


@pytest.mark.asyncio
async def test_ttl_zero_disables_cache(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1", "status": "active"})
    no_cache = get_settings().model_copy(update={"tenant_status_cache_ttl_seconds": 0})
    assert await get_tenant_status("t1", settings=no_cache) == STATUS_ACTIVE
    await control["tenants"].update_one({"tenant_id": "t1"}, {"$set": {"status": "suspended"}})
    # With caching disabled the change is observed immediately.
    assert await get_tenant_status("t1", settings=no_cache) == STATUS_SUSPENDED


@pytest.mark.asyncio
async def test_read_only_defaults_false_and_writable(patch_mongo):
    # An unknown/unflagged tenant is writable: read-only must fail open so the
    # data plane is not accidentally frozen for every tenant.
    assert await get_tenant_read_only("never-seen") is False
    await assert_tenant_writable("never-seen")  # does not raise


@pytest.mark.asyncio
async def test_set_read_only_blocks_writes_and_records_reason(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1", "status": "active"})

    doc = await set_tenant_read_only("t1", True, updated_by="ops@x", reason="showcase")
    assert doc is not None
    assert doc["read_only"] is True
    assert doc["read_only_reason"] == "showcase"
    assert doc["read_only_updated_by"] == "ops@x"

    assert await get_tenant_read_only("t1") is True
    with pytest.raises(TenantReadOnlyError) as exc:
        await assert_tenant_writable("t1")
    assert exc.value.tenant_id == "t1"
    assert exc.value.status_code == "tenant_read_only"
    assert "showcase" in exc.value.reason
    # Read-only is orthogonal to status: the tenant is still "active".
    assert isinstance(exc.value, TenantInactiveError)
    assert await get_tenant_status("t1") == STATUS_ACTIVE
    await assert_tenant_active("t1")  # read-only does not block discovery


@pytest.mark.asyncio
async def test_clear_read_only_unblocks_writes(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1"})

    await set_tenant_read_only("t1", True, reason="freeze")
    cleared = await set_tenant_read_only("t1", False)
    assert cleared is not None
    assert cleared["read_only"] is False
    assert cleared["read_only_reason"] == ""

    assert await get_tenant_read_only("t1") is False
    await assert_tenant_writable("t1")  # does not raise


@pytest.mark.asyncio
async def test_set_read_only_on_missing_tenant_returns_none(patch_mongo):
    assert await set_tenant_read_only("ghost", True) is None


@pytest.mark.asyncio
async def test_read_only_is_cached_within_ttl(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1", "read_only": False})
    assert await get_tenant_read_only("t1") is False
    # Mutate the doc directly so the in-process cache is intentionally stale.
    await control["tenants"].update_one({"tenant_id": "t1"}, {"$set": {"read_only": True}})
    assert await get_tenant_read_only("t1") is False
    reset_tenant_status_cache()
    assert await get_tenant_read_only("t1") is True


@pytest.mark.asyncio
async def test_set_status_with_caching_disabled(patch_mongo, monkeypatch, reset_settings):
    # set_tenant_status reads the live settings for its own cache write; with the
    # TTL at 0 it must take the no-cache branch and still persist correctly.
    from config.settings import get_settings as _get_settings

    monkeypatch.setenv("TENANT_STATUS_CACHE_TTL_SECONDS", "0")
    _get_settings.cache_clear()

    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1", "status": "active"})
    assert await get_tenant_status("t1") == STATUS_ACTIVE
    suspended = await set_tenant_status("t1", STATUS_SUSPENDED, reason="x")
    assert suspended is not None and suspended["status"] == STATUS_SUSPENDED
    # No stale cache entry is served; the fresh value is read back.
    assert await get_tenant_status("t1") == STATUS_SUSPENDED
