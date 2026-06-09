"""Tests for the embedding reprovision orchestrator: catalog/guardrail re-embed,
vector index recreate, and status reporting. Mongo + embeddings are faked.
"""

from __future__ import annotations

import pytest
from fakes import FakeEmbeddingService

from database.mongo import get_control_database, get_tenant_database
from services import embedding_config
from services.embedding_config import EmbeddingConfig
from services.embedding_reprovision import (
    ReprovisionInProgressError,
    get_reprovision_status,
    run_reprovision,
    trigger_reprovision,
)


def _activate_fake(dimensions: int = 8) -> FakeEmbeddingService:
    fake = FakeEmbeddingService(dimensions=dimensions, model_id="probe-model")
    embedding_config._set_active(
        EmbeddingConfig(provider="ollama", model="probe-model", dimensions=dimensions),
        fake,
    )
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
