"""Tests for the embedding reprovision orchestrator: catalog/guardrail re-embed,
vector index recreate, and status reporting. Mongo + embeddings are faked.
"""

from __future__ import annotations

import pytest
from fakes import FakeEmbeddingService

from database.mongo import get_control_database, get_tenant_database
from services import embedding_config
from services.embedding_reprovision import (
    ReprovisionInProgressError,
    get_reprovision_status,
    get_tenant_reprovision_status,
    run_reprovision,
    trigger_reprovision,
    trigger_tenant_reprovision,
)


def _activate_fake(dimensions: int = 8) -> FakeEmbeddingService:
    fake = FakeEmbeddingService(dimensions=dimensions, model_id="probe-model")
    embedding_config._set_active(fake)
    return fake


@pytest.mark.asyncio
async def test_run_reprovision_reembeds_catalog_and_guardrails(patch_mongo):
    _activate_fake(8)
    control = get_control_database()
    await control["tenants"].insert_one({"tenant_id": "local-dev"})
    await control["tenants"].insert_one({"tenant_id": "tenant-b"})

    catalog = get_tenant_database("local-dev")["tool_catalog"]
    catalog.docs.append(
        {"server": "weather", "name": "get_forecast", "description": "Weather", "embedding": [0.0]}
    )
    catalog.docs.append(
        {"server": "orders", "name": "find_order", "description": "Orders", "embedding": [0.0]}
    )

    status = await run_reprovision(started_by="admin@example.com")

    assert status["state"] == "completed"
    assert status["totals"]["catalog_reembedded"] == 2
    assert status["totals"]["guardrail_signatures"] == 12
    assert status["totals"]["tenants"] >= 2
    assert status["target_version"] == "probe-model:8"
    assert status["progress"]["completed"] == status["progress"]["total"]

    # Catalog embeddings were rewritten to the active provider's width.
    assert all(len(doc["embedding"]) == 8 for doc in catalog.docs)

    signature = await control["guardrail_signatures"].find_one(
        {"_id": "inj-ignore-previous-instructions"}
    )
    assert signature is not None
    assert len(signature["embedding"]) == 8
    assert signature["embedding_version"] == "probe-model:8"


@pytest.mark.asyncio
async def test_run_reprovision_recreates_vector_index(patch_mongo):
    _activate_fake(16)
    catalog = get_tenant_database("local-dev")["tool_catalog"]
    # Pre-existing vector index with the wrong width.
    catalog._search_indexes["hybrid-vector-search"] = {
        "name": "hybrid-vector-search",
        "definition": {"fields": [{"type": "vector", "numDimensions": 3}]},
        "queryable": True,
    }

    await run_reprovision()

    rebuilt = catalog._search_indexes["hybrid-vector-search"]
    dims = rebuilt["definition"]["fields"][0]["numDimensions"]
    assert dims == 16


@pytest.mark.asyncio
async def test_run_reprovision_emits_incremental_progress(patch_mongo, monkeypatch):
    from services import embedding_reprovision as er

    _activate_fake(8)
    control = get_control_database()
    await control["tenants"].insert_one({"tenant_id": "local-dev"})
    await control["tenants"].insert_one({"tenant_id": "tenant-b"})

    progress_states: list[tuple[int, int]] = []
    original_write_status = er._write_status

    async def _spy_write_status(**fields):
        progress = fields.get("progress")
        if isinstance(progress, dict):
            progress_states.append((int(progress.get("completed", -1)), int(progress.get("total", -1))))
        await original_write_status(**fields)

    monkeypatch.setattr(er, "_write_status", _spy_write_status)
    status = await er.run_reprovision(started_by="admin@example.com")
    assert status["state"] == "completed"
    assert (0, 2) in progress_states
    assert (1, 2) in progress_states
    assert (2, 2) in progress_states


@pytest.mark.asyncio
async def test_status_idle_when_never_run(patch_mongo):
    assert (await get_reprovision_status())["state"] == "idle"


@pytest.mark.asyncio
async def test_trigger_rejects_when_already_running(patch_mongo):
    from datetime import UTC, datetime

    _activate_fake(8)
    await get_control_database()["embedding_status"].update_one(
        {"_id": "reprovision"},
        {"$set": {"_id": "reprovision", "state": "running", "started_at": datetime.now(UTC)}},
        upsert=True,
    )
    with pytest.raises(ReprovisionInProgressError):
        await trigger_reprovision(started_by="admin")


@pytest.mark.asyncio
async def test_trigger_allowed_when_running_status_is_stale(patch_mongo):
    import asyncio
    from datetime import UTC, datetime, timedelta

    from services import embedding_reprovision as er

    _activate_fake(8)
    stale = datetime.now(UTC) - timedelta(hours=3)
    await get_control_database()["embedding_status"].update_one(
        {"_id": "reprovision"},
        {"$set": {"_id": "reprovision", "state": "running", "started_at": stale}},
        upsert=True,
    )
    # A crashed/stale run must not lock out future reprovisions forever.
    status = await trigger_reprovision(started_by="admin")
    assert status["state"] == "running"

    # Drain the scheduled background job so it doesn't outlive the patched DB.
    await asyncio.gather(*list(er._background_tasks), return_exceptions=True)
    assert (await get_reprovision_status())["state"] == "completed"


@pytest.mark.asyncio
async def test_trigger_tenant_reprovision_runs_and_records_tenant_status(patch_mongo, monkeypatch):
    import asyncio

    from services import embedding_reprovision as er

    fake = FakeEmbeddingService(dimensions=11, model_id="tenant-model")

    async def _service_for_tenant(tenant_id: str, settings=None):
        return fake

    async def _tenant_identity(tenant_id: str, settings=None):
        return ("tenant-model", 11, "tenant-model:11")

    monkeypatch.setattr(er, "get_embedding_service_for", _service_for_tenant)
    monkeypatch.setattr(er, "tenant_embedding_identity", _tenant_identity)

    catalog = get_tenant_database("tenant-a")["tool_catalog"]
    catalog.docs.append(
        {
            "server": "weather",
            "name": "get_forecast",
            "description": "Weather",
            "embedding": [0.0],
        }
    )

    status = await trigger_tenant_reprovision("tenant-a", started_by="admin@example.com")
    assert status["state"] == "running"
    assert status["progress"] == {"completed": 0, "total": 1}

    await asyncio.gather(*list(er._background_tasks), return_exceptions=True)
    done = await get_tenant_reprovision_status("tenant-a")
    assert done["state"] == "completed"
    assert done["tenant_id"] == "tenant-a"
    assert done["totals"]["tenants"] == 1
    assert done["totals"]["catalog_reembedded"] == 1
    assert done["progress"] == {"completed": 1, "total": 1}


@pytest.mark.asyncio
async def test_trigger_tenant_reprovision_rejects_when_already_running(patch_mongo):
    from datetime import UTC, datetime

    await get_control_database()["embedding_status"].update_one(
        {"_id": "reprovision:tenant-a"},
        {
            "$set": {
                "_id": "reprovision:tenant-a",
                "state": "running",
                "tenant_id": "tenant-a",
                "started_at": datetime.now(UTC),
            }
        },
        upsert=True,
    )
    with pytest.raises(ReprovisionInProgressError):
        await trigger_tenant_reprovision("tenant-a", started_by="admin")
