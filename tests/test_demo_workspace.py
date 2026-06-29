"""Tests for one-click demo workspaces (services/demo_workspace + demo_seed).

Covers the orchestration that makes a demo "bulletproof":

* provisioning tags an isolated, confirmed tenant + a shareable tenant-admin login,
* the tenant-axis cap (``max_demo_tenants``) and TTL clamp,
* expiry reaping (idempotent, frees the cap, purges users) on create/list,
* rollback when a later step fails (no half-built demo lingers),
* the ``origin="demo"`` delete guard (cannot nuke a real tenant), and
* the capability-aware seeder gating (only tools that can run are seeded).

The seeder's catalog mount needs an embedding backend, so orchestration tests
stub ``seed_demo_pack`` to isolate the lifecycle logic, and the gating tests stub
``_seed_server`` to assert the *pack composition* without a real mount.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from config.settings import get_settings
from database.mongo import get_control_database
from services import demo_seed
from services import demo_workspace as dw
from services import users as users_service
from services.account_tier import CONFIRMATION_CONFIRMED, get_tenant_confirmation
from services.demo_seed import DemoSeedResult
from services.usage_metering import get_effective_quota


# --------------------------------------------------------------------------- #
#  Helpers                                                                    #
# --------------------------------------------------------------------------- #
def _enable_demos(**overrides) -> None:
    settings = get_settings()
    object.__setattr__(settings, "demo_workspaces_enabled", True)
    for key, value in overrides.items():
        object.__setattr__(settings, key, value)


def _stub_seed(monkeypatch, *, servers=None, tools=2, bridges=None) -> None:
    """Replace seed_demo_pack so lifecycle tests don't need an embedding backend."""

    async def _fake_seed(tenant_id, *, settings=None):
        return DemoSeedResult(
            servers=list(servers if servers is not None else ["utilities", "analytics"]),
            tools=tools,
            bridges=dict(bridges or {"db": True, "tools": True, "http": False}),
        )

    monkeypatch.setattr(dw, "seed_demo_pack", _fake_seed)


async def _tenant_doc(tenant_id: str):
    return await get_control_database()["tenants"].find_one({"tenant_id": tenant_id})


async def _expire(tenant_id: str) -> None:
    await get_control_database()["tenants"].update_one(
        {"tenant_id": tenant_id},
        {"$set": {"expires_at": datetime.now(UTC) - timedelta(hours=1)}},
    )


# --------------------------------------------------------------------------- #
#  Provisioning: happy path                                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_provision_creates_isolated_tagged_workspace(reset_settings, patch_mongo, monkeypatch):
    _enable_demos()
    _stub_seed(monkeypatch, servers=["utilities", "analytics"], tools=4)

    ws = await dw.provision_demo_workspace(
        label="Acme Corp", client="cursor", created_by="boss@example.com"
    )

    assert ws.tenant_id.startswith(get_settings().demo_tenant_prefix)
    assert ws.password  # returned exactly once
    assert ws.user_email.endswith("@demo.local")
    assert ws.expired is False
    assert ws.servers == ["utilities", "analytics"] and ws.tools == 4
    # Lifetime tracks the configured default exactly (created_at == now == base).
    assert ws.expires_at - ws.created_at == timedelta(hours=get_settings().demo_ttl_hours)

    # Tenant is tagged + confirmed (so the multi-tool pack + authoring work).
    doc = await _tenant_doc(ws.tenant_id)
    assert doc["origin"] == dw.DEMO_ORIGIN
    assert doc["demo_label"] == "Acme Corp"
    assert doc["demo_user_email"] == ws.user_email
    assert await get_tenant_confirmation(ws.tenant_id) == CONFIRMATION_CONFIRMED

    # The login is a real, usable tenant-admin of ONLY this tenant — never platform.
    principal = await users_service.authenticate(ws.user_email, ws.password)
    assert principal is not None
    assert set(principal["roles"]) == {"user", "admin"}
    assert get_settings().platform_admin_role not in principal["roles"]
    assert principal["tenant_id"] == ws.tenant_id

    # Quota stamped to the confirmed tier (unlimited == 0 by default).
    quota = await get_effective_quota(ws.tenant_id)
    assert quota["calls_limit"] == get_settings().confirmed_quota_calls_per_period


@pytest.mark.asyncio
async def test_two_demos_are_isolated_tenants(reset_settings, patch_mongo, monkeypatch):
    _enable_demos()
    _stub_seed(monkeypatch)

    a = await dw.provision_demo_workspace(label="Customer A")
    b = await dw.provision_demo_workspace(label="Customer B")

    assert a.tenant_id != b.tenant_id
    assert a.user_email != b.user_email
    workspaces = await dw.list_demo_workspaces()
    assert {w.tenant_id for w in workspaces} == {a.tenant_id, b.tenant_id}


# --------------------------------------------------------------------------- #
#  Guards: disabled + cap                                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_provision_disabled_raises(reset_settings, patch_mongo, monkeypatch):
    _enable_demos()
    object.__setattr__(get_settings(), "demo_workspaces_enabled", False)
    _stub_seed(monkeypatch)
    with pytest.raises(dw.DemoWorkspacesDisabled):
        await dw.provision_demo_workspace()


@pytest.mark.asyncio
async def test_cap_enforced(reset_settings, patch_mongo, monkeypatch):
    _enable_demos(max_demo_tenants=1)
    _stub_seed(monkeypatch)

    await dw.provision_demo_workspace(label="first")
    with pytest.raises(dw.DemoCapReached):
        await dw.provision_demo_workspace(label="second")


@pytest.mark.asyncio
async def test_cap_zero_is_unlimited(reset_settings, patch_mongo, monkeypatch):
    _enable_demos(max_demo_tenants=0)
    _stub_seed(monkeypatch)
    for _ in range(3):
        await dw.provision_demo_workspace()
    assert await dw.count_active_demos() == 3


# --------------------------------------------------------------------------- #
#  TTL clamp                                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ttl_override_clamped_to_ceiling(reset_settings, patch_mongo, monkeypatch):
    _enable_demos(demo_ttl_max_hours=48)
    _stub_seed(monkeypatch)
    ws = await dw.provision_demo_workspace(ttl_hours=99999)
    assert ws.expires_at - ws.created_at == timedelta(hours=48)


@pytest.mark.asyncio
async def test_ttl_override_floor(reset_settings, patch_mongo, monkeypatch):
    _enable_demos()
    _stub_seed(monkeypatch)
    ws = await dw.provision_demo_workspace(ttl_hours=0)
    assert ws.expires_at - ws.created_at == timedelta(hours=1)


# --------------------------------------------------------------------------- #
#  Expiry reaping                                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_expired_demo_reaped_and_frees_cap(reset_settings, patch_mongo, monkeypatch):
    _enable_demos(max_demo_tenants=1)
    _stub_seed(monkeypatch)

    first = await dw.provision_demo_workspace(label="old")
    await _expire(first.tenant_id)

    # The cap is full, but provisioning reaps the expired one first and succeeds.
    second = await dw.provision_demo_workspace(label="new")

    assert second.tenant_id != first.tenant_id
    assert await _tenant_doc(first.tenant_id) is None  # reaped (hard-dropped)
    # The expired demo's users were purged too.
    assert (
        await get_control_database()["users"].count_documents({"tenant_id": first.tenant_id}) == 0
    )


@pytest.mark.asyncio
async def test_list_reaps_expired(reset_settings, patch_mongo, monkeypatch):
    _enable_demos()
    _stub_seed(monkeypatch)
    keep = await dw.provision_demo_workspace(label="keep")
    drop = await dw.provision_demo_workspace(label="drop")
    await _expire(drop.tenant_id)

    workspaces = await dw.list_demo_workspaces()
    assert [w.tenant_id for w in workspaces] == [keep.tenant_id]
    assert await _tenant_doc(drop.tenant_id) is None


@pytest.mark.asyncio
async def test_reap_is_idempotent(reset_settings, patch_mongo, monkeypatch):
    _enable_demos()
    _stub_seed(monkeypatch)
    ws = await dw.provision_demo_workspace()
    await _expire(ws.tenant_id)

    assert await dw.reap_expired_demo_workspaces() == 1
    assert await dw.reap_expired_demo_workspaces() == 0


# --------------------------------------------------------------------------- #
#  Rollback                                                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_provision_rolls_back_on_failure(reset_settings, patch_mongo, monkeypatch):
    _enable_demos()
    _stub_seed(monkeypatch)

    async def _boom(*args, **kwargs):
        raise RuntimeError("user store exploded")

    monkeypatch.setattr(users_service, "create_user", _boom)

    with pytest.raises(RuntimeError):
        await dw.provision_demo_workspace(label="doomed")

    # No half-built demo lingers: the tenant was torn down entirely.
    assert await get_control_database()["tenants"].count_documents({}) == 0


# --------------------------------------------------------------------------- #
#  Deletion guard + teardown                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_delete_demo_tears_down_and_purges_users(reset_settings, patch_mongo, monkeypatch):
    _enable_demos()
    _stub_seed(monkeypatch)
    ws = await dw.provision_demo_workspace()

    assert await dw.delete_demo_workspace(ws.tenant_id) is True
    assert await _tenant_doc(ws.tenant_id) is None
    assert (
        await get_control_database()["users"].count_documents({"tenant_id": ws.tenant_id}) == 0
    )


@pytest.mark.asyncio
async def test_delete_refuses_non_demo_tenant(reset_settings, patch_mongo):
    _enable_demos()
    # A real (non-demo) tenant must never be deletable through the demo surface.
    await get_control_database()["tenants"].insert_one(
        {"tenant_id": "real-customer", "db_name": "tenant_real", "status": "active"}
    )
    assert await dw.delete_demo_workspace("real-customer") is False
    assert await _tenant_doc("real-customer") is not None


@pytest.mark.asyncio
async def test_delete_unknown_tenant_returns_false(reset_settings, patch_mongo):
    _enable_demos()
    assert await dw.delete_demo_workspace("does-not-exist") is False


# --------------------------------------------------------------------------- #
#  Capability-aware seeding (pack composition)                                #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_seed_pack_db_off_is_stdlib_only(reset_settings, patch_mongo, monkeypatch):
    _enable_demos()
    object.__setattr__(get_settings(), "sandbox_db_bridge_enabled", False)

    seeded: list[tuple[str, int]] = []

    async def _record(tenant_id, *, server, tools, metadata, settings):
        seeded.append((server, len(tools)))
        return True

    monkeypatch.setattr(demo_seed, "_seed_server", _record)

    result = await demo_seed.seed_demo_pack("demo-x")
    assert result.servers == ["utilities"]
    assert result.tools == 1
    assert result.bridges["db"] is False


@pytest.mark.asyncio
async def test_seed_pack_db_and_tools_on_is_full(reset_settings, patch_mongo, monkeypatch):
    _enable_demos()
    object.__setattr__(get_settings(), "sandbox_db_bridge_enabled", True)
    object.__setattr__(get_settings(), "sandbox_tool_bridge_enabled", True)

    async def _ok(tenant_id, *, server, tools, metadata, settings):
        return True

    monkeypatch.setattr(demo_seed, "_seed_server", _ok)

    result = await demo_seed.seed_demo_pack("demo-y")
    # utilities(1) + analytics(get_stats, track_click, track_and_report = 3) + directory(1)
    assert set(result.servers) == {"utilities", "analytics", "directory"}
    assert result.tools == 5
    assert result.bridges == {"db": True, "tools": True, "http": False}


@pytest.mark.asyncio
async def test_seed_pack_db_on_tools_off_omits_compose(reset_settings, patch_mongo, monkeypatch):
    _enable_demos()
    object.__setattr__(get_settings(), "sandbox_db_bridge_enabled", True)
    object.__setattr__(get_settings(), "sandbox_tool_bridge_enabled", False)

    captured: dict[str, int] = {}

    async def _capture(tenant_id, *, server, tools, metadata, settings):
        captured[server] = len(tools)
        return True

    monkeypatch.setattr(demo_seed, "_seed_server", _capture)

    result = await demo_seed.seed_demo_pack("demo-z")
    # analytics drops track_and_report (needs the tool bridge): get_stats + track_click.
    assert captured["analytics"] == 2
    assert result.tools == 4
