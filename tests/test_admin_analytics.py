"""Tests for the admin analytics endpoints and the scalable admin_stats rewrite.

Mirrors tests/test_readonly_admin.py: handlers are called directly with a fake
Request, against the in-memory Mongo fake (which now emulates the ``$group`` /
``$dateTrunc`` / ``$percentile`` pipeline subset the analytics router uses).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from database.mongo import get_control_database, get_tenant_database
from services.usage_metering import current_period


class _State:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Req:
    def __init__(
        self,
        *,
        tenant_id: str = "local-dev",
        roles: list[str] | None = None,
        user_id: str = "admin@example.com",
        headers=None,
    ):
        self.state = _State(tenant_id=tenant_id, roles=roles or [], user_id=user_id)
        self.headers = headers or {}


def _platform_admin(admin, **kwargs) -> _Req:
    return _Req(roles=[admin.settings.platform_admin_role], **kwargs)


def _tenant_admin(**kwargs) -> _Req:
    return _Req(roles=["admin"], **kwargs)


async def _seed_tenant(tenant_id="local-dev", *, confirmation: str | None = None):
    doc = {"tenant_id": tenant_id, "db_name": f"tenant_{tenant_id}", "status": "active"}
    if confirmation is not None:
        doc["confirmation"] = confirmation
    await get_control_database()["tenants"].insert_one(doc)


async def _seed_usage(tenant_id, period, *, calls=0, sandbox_ms=0):
    await get_control_database()["usage_counters"].insert_one(
        {"tenant_id": tenant_id, "period": period, "calls": calls, "sandbox_ms": sandbox_ms}
    )


async def _seed_event(tenant_id, period, *, kind="calls", amount=1, server="s", tool="t"):
    await get_control_database()["usage_events"].insert_one(
        {
            "tenant_id": tenant_id,
            "period": period,
            "kind": kind,
            "amount": amount,
            "ts": datetime.now(UTC),
            "metadata": {"server": server, "tool": tool},
        }
    )


async def _seed_telemetry(tenant_id, *, status, latency_ms, when=None):
    await get_tenant_database(tenant_id)["audit_telemetry"].insert_one(
        {
            "tenant_id": tenant_id,
            "timestamp": when or datetime.now(UTC),
            "status": status,
            "latency_ms": latency_ms,
            "method": "tools/call",
            "user_id": "u@example.com",
        }
    )


# --------------------------------------------------------------------------- #
#  overview                                                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_overview_platform_rolls_up_all_tenants(patch_mongo):
    import gateway.routers.admin as admin

    period = current_period()
    await _seed_tenant("local-dev")
    await _seed_tenant("beta-1", confirmation="unconfirmed")
    await _seed_usage("local-dev", period, calls=10, sandbox_ms=1000)
    await _seed_usage("beta-1", period, calls=5, sandbox_ms=500)
    await get_control_database()["users"].insert_one(
        {"email": "x@example.com", "tenant_id": "beta-1", "self_registered": True}
    )

    res = await admin.analytics_overview(_platform_admin(admin), tenant_id=None)
    assert res.scope == "platform"
    assert res.period == period
    assert res.tenant_count == 2
    assert res.calls == 15
    assert res.sandbox_ms == 1500
    assert res.confirmed_count == 1
    assert res.unconfirmed_count == 1
    assert res.self_registered_count == 1
    assert res.self_registration_max_tenants == admin.settings.self_registration_max_tenants


@pytest.mark.asyncio
async def test_overview_tenant_scope_is_confined(patch_mongo):
    import gateway.routers.admin as admin

    period = current_period()
    await _seed_tenant("local-dev")
    await _seed_tenant("other")
    await _seed_usage("local-dev", period, calls=10, sandbox_ms=1000)
    await _seed_usage("other", period, calls=99, sandbox_ms=9999)

    res = await admin.analytics_overview(_tenant_admin(), tenant_id=None)
    assert res.scope == "tenant"
    assert res.tenant_count == 1
    assert res.calls == 10
    assert res.sandbox_ms == 1000
    # Beta-headroom fields are platform-only.
    assert res.confirmed_count is None
    assert res.self_registered_count is None


@pytest.mark.asyncio
async def test_overview_empty_tenant_is_zero(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant("local-dev")
    res = await admin.analytics_overview(_tenant_admin(), tenant_id=None)
    assert res.calls == 0
    assert res.sandbox_ms == 0


@pytest.mark.asyncio
async def test_overview_requires_admin(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant("local-dev")
    with pytest.raises(HTTPException) as exc:
        await admin.analytics_overview(_Req(roles=["user"]), tenant_id=None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_tenant_admin_cannot_cross_tenant(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant("local-dev")
    await _seed_tenant("other")
    with pytest.raises(HTTPException) as exc:
        await admin.analytics_overview(_tenant_admin(), tenant_id="other")
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
#  usage-trend                                                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_usage_trend_fills_all_periods(patch_mongo):
    import gateway.routers.admin as admin
    from gateway.routers.admin.analytics import _recent_periods

    periods = _recent_periods(3)
    await _seed_tenant("local-dev")
    await _seed_usage("local-dev", periods[-1], calls=7, sandbox_ms=70)  # current
    await _seed_usage("local-dev", periods[0], calls=3, sandbox_ms=30)  # oldest

    res = await admin.analytics_usage_trend(_tenant_admin(), months=3, tenant_id=None)
    assert res.scope == "tenant"
    assert [p.period for p in res.points] == periods
    assert res.points[-1].calls == 7
    assert res.points[0].calls == 3
    assert res.points[1].calls == 0  # gap filled


# --------------------------------------------------------------------------- #
#  top-tools                                                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_top_tools_groups_and_sorts(patch_mongo):
    import gateway.routers.admin as admin

    period = current_period()
    await _seed_tenant("local-dev")
    await _seed_event("local-dev", period, server="s1", tool="t1", amount=3)
    await _seed_event("local-dev", period, server="s1", tool="t1", amount=2)
    await _seed_event("local-dev", period, server="s1", tool="t2", amount=4)
    await _seed_event("local-dev", period, server="s2", tool="t3", amount=1)
    # A non-calls event must be excluded from the rollup.
    await _seed_event("local-dev", period, kind="sandbox_ms", server="s1", tool="t1", amount=999)

    res = await admin.analytics_top_tools(_tenant_admin(), period=None, limit=10, tenant_id=None)
    top = [(t.server, t.tool, t.calls) for t in res.tools]
    assert top[0] == ("s1", "t1", 5)
    assert ("s1", "t2", 4) in top
    assert ("s2", "t3", 1) in top
    servers = {s.server: s.calls for s in res.servers}
    assert servers == {"s1": 9, "s2": 1}


# --------------------------------------------------------------------------- #
#  telemetry-trend                                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_telemetry_trend_counts_and_latency(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant("local-dev")
    await _seed_telemetry("local-dev", status="live_execution_success", latency_ms=10)
    await _seed_telemetry("local-dev", status="live_execution_success", latency_ms=20)
    await _seed_telemetry("local-dev", status="quota_exceeded", latency_ms=30)

    res = await admin.analytics_telemetry_trend(_tenant_admin(), hours=24, tenant_id=None)
    assert res.scope == "tenant"
    assert len(res.points) == 1
    point = res.points[0]
    assert point.total == 3
    assert point.errors == 1
    assert point.latency_avg_ms == pytest.approx(20.0)
    assert point.latency_p95_ms is not None


@pytest.mark.asyncio
async def test_telemetry_trend_excludes_old_events(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant("local-dev")
    await _seed_telemetry(
        "local-dev",
        status="live_execution_success",
        latency_ms=10,
        when=datetime.now(UTC) - timedelta(hours=48),
    )
    res = await admin.analytics_telemetry_trend(_tenant_admin(), hours=24, tenant_id=None)
    assert res.points == []


@pytest.mark.asyncio
async def test_telemetry_trend_platform_merges_tenants(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant("local-dev")
    await _seed_tenant("beta-1")
    await _seed_telemetry("local-dev", status="ok_success", latency_ms=10)
    await _seed_telemetry("beta-1", status="error", latency_ms=40)

    res = await admin.analytics_telemetry_trend(_platform_admin(admin), hours=24, tenant_id=None)
    assert res.scope == "platform"
    total = sum(p.total for p in res.points)
    errors = sum(p.errors for p in res.points)
    assert total == 2
    assert errors == 1


# --------------------------------------------------------------------------- #
#  quota-utilization                                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_quota_utilization_computes_pct(patch_mongo):
    import gateway.routers.admin as admin

    period = current_period()
    await _seed_tenant("local-dev")
    await _seed_usage("local-dev", period, calls=50, sandbox_ms=30_000)
    await get_control_database()["tenant_quotas"].insert_one(
        {
            "_id": "local-dev",
            "tenant_id": "local-dev",
            "calls_limit": 100,
            "sandbox_seconds_limit": 60,
        }
    )

    res = await admin.analytics_quota_utilization(_tenant_admin(), tenant_id=None)
    assert res.scope == "tenant"
    assert len(res.tenants) == 1
    entry = res.tenants[0]
    assert entry.calls == 50
    assert entry.calls_limit == 100
    assert entry.calls_utilization_pct == pytest.approx(50.0)
    assert entry.sandbox_utilization_pct == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_quota_utilization_unlimited_is_none(patch_mongo):
    import gateway.routers.admin as admin

    period = current_period()
    await _seed_tenant("local-dev")
    await _seed_usage("local-dev", period, calls=5, sandbox_ms=5)
    await get_control_database()["tenant_quotas"].insert_one(
        {
            "_id": "local-dev",
            "tenant_id": "local-dev",
            "calls_limit": 0,
            "sandbox_seconds_limit": 0,
        }
    )

    res = await admin.analytics_quota_utilization(_tenant_admin(), tenant_id=None)
    entry = res.tenants[0]
    assert entry.calls_utilization_pct is None
    assert entry.sandbox_utilization_pct is None


# --------------------------------------------------------------------------- #
#  admin_stats (scalable rewrite)                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_admin_stats_counts_without_loading_collections(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant("local-dev")
    registry = get_tenant_database("local-dev")["routing_registry"]
    await registry.insert_one({"server": "s1", "enabled": True})
    await registry.insert_one({"server": "s2", "enabled": False})
    await registry.insert_one({"server": "s3"})  # missing flag == enabled
    catalog = get_tenant_database("local-dev")["tool_catalog"]
    await catalog.insert_one({"server": "s1", "name": "t1"})
    await catalog.insert_one({"server": "s1", "name": "t2"})
    await _seed_telemetry("local-dev", status="success", latency_ms=1)
    await _seed_telemetry("local-dev", status="success", latency_ms=1)
    await _seed_telemetry("local-dev", status="error", latency_ms=1)

    res = await admin.admin_stats(_platform_admin(admin))
    row = next(r for r in res.tenants if r.tenant_id == "local-dev")
    assert row.server_count == 3
    assert row.enabled_server_count == 2
    assert row.tool_count == 2
    assert res.telemetry_status_counts["success"] == 2
    assert res.telemetry_status_counts["error"] == 1
