"""Tests for the telemetry logger (fire-and-forget audit writes) and the
registry watcher's initial-sync resilience.
"""

from __future__ import annotations

import asyncio

import pytest

from database.mongo import get_control_database, get_tenant_database

# --------------------------------------------------------------------------
# TelemetryLogger
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_writes_audit_document(patch_mongo):
    from services.telemetry_logger import TelemetryLogger

    logger = TelemetryLogger()
    await logger.log(
        tenant_id="local-dev",
        user_id="u1",
        method="tools/call",
        status="ok",
        latency_ms=12.5,
        metadata={"tool": "x"},
    )
    docs = patch_mongo["audit_telemetry"].docs
    assert len(docs) == 1
    assert docs[0]["status"] == "ok"
    assert docs[0]["metadata"] == {"tool": "x"}


@pytest.mark.asyncio
async def test_log_background_schedules_and_completes(patch_mongo):
    from services.telemetry_logger import TelemetryLogger

    logger = TelemetryLogger()
    logger.log_background(tenant_id="local-dev", user_id="u1", method="tools/list", status="ok")
    # Let the scheduled task run.
    await asyncio.sleep(0.05)
    assert len(patch_mongo["audit_telemetry"].docs) == 1


@pytest.mark.asyncio
async def test_safe_log_swallows_errors(monkeypatch):
    from services.telemetry_logger import TelemetryLogger

    logger = TelemetryLogger()

    async def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(logger, "log", boom)
    # Should not raise despite the underlying failure.
    await logger._safe_log(tenant_id="t", user_id="u", method="m", status="s")


def test_log_background_without_event_loop_is_noop():
    from services.telemetry_logger import TelemetryLogger

    logger = TelemetryLogger()
    # No running loop in a sync context -> silently returns.
    logger.log_background(tenant_id="t", user_id="u", method="m", status="s")
    assert logger._tasks == set()


# --------------------------------------------------------------------------
# Registry watcher initial sync
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_catalog_version_increments(monkeypatch):
    import services.registry_watcher as rw

    before = rw.get_catalog_version()
    rw._bump_catalog_version()
    assert rw.get_catalog_version() == before + 1


@pytest.mark.asyncio
async def test_initial_sync_isolates_failing_server(patch_mongo, monkeypatch):
    """One bad downstream must not abort syncing of the others."""
    import services.registry_watcher as rw

    tenant_id = "local-dev"
    await get_control_database()["tenants"].insert_one({"tenant_id": tenant_id})
    tenant_routing = get_tenant_database(tenant_id)["routing_registry"]
    tenant_routing.docs.extend(
        [
            {"server": "good", "endpoint": "http://good/mcp", "enabled": True},
            {"server": "bad", "endpoint": "http://bad/mcp", "enabled": True},
        ]
    )

    mounted = []

    class _Reg:
        async def mount_or_update(self, doc):
            if doc["server"] == "bad":
                raise RuntimeError("sync failed")
            mounted.append(doc["server"])

    await rw._initial_sync_all_tenants(_Reg())

    assert "good" in mounted
    assert "bad" not in mounted
