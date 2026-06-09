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
