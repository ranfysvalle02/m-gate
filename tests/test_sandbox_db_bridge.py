from __future__ import annotations

from datetime import UTC, datetime

import pytest
from bson import ObjectId

from config.settings import Settings
from database.mongo import get_tenant_database
from services.sandbox_db_bridge import SandboxDbBridge


@pytest.mark.asyncio
async def test_bridge_read_find_returns_tenant_docs(patch_mongo):
    tenant = "tenant-a"
    coll = get_tenant_database(tenant)["users"]
    await coll.insert_one({"_id": "a1", "tenant": tenant})

    bridge = SandboxDbBridge(tenant_id=tenant, action_type="read", settings=Settings())
    frame = await bridge.handle(
        {"id": 1, "op": "find", "collection": "users", "args": [{"tenant": tenant}], "kwargs": {}}
    )

    assert frame["ok"] is True
    assert frame["result"] == [{"_id": "a1", "tenant": tenant}]


@pytest.mark.asyncio
async def test_bridge_rejects_write_for_read_action_type(patch_mongo):
    bridge = SandboxDbBridge(tenant_id="tenant-a", action_type="read", settings=Settings())
    frame = await bridge.handle(
        {
            "id": 1,
            "op": "insert_one",
            "collection": "users",
            "args": [{"_id": "x"}],
            "kwargs": {},
        }
    )
    assert frame["ok"] is False
    assert "not allowed" in frame["error"]["message"]


@pytest.mark.asyncio
async def test_bridge_scopes_to_current_tenant(patch_mongo):
    await get_tenant_database("tenant-a")["users"].insert_one(
        {"_id": "a-only", "tenant": "tenant-a"}
    )
    await get_tenant_database("tenant-b")["users"].insert_one(
        {"_id": "b-only", "tenant": "tenant-b"}
    )

    bridge = SandboxDbBridge(tenant_id="tenant-a", action_type="read", settings=Settings())
    frame = await bridge.handle(
        {"id": 1, "op": "find", "collection": "users", "args": [{}], "kwargs": {"limit": 50}}
    )
    assert frame["ok"] is True
    assert frame["result"] == [{"_id": "a-only", "tenant": "tenant-a"}]


@pytest.mark.asyncio
async def test_bridge_rejects_invalid_collection_name(patch_mongo):
    bridge = SandboxDbBridge(tenant_id="tenant-a", action_type="read", settings=Settings())
    frame = await bridge.handle(
        {"id": 1, "op": "find", "collection": "system.profile", "args": [{}], "kwargs": {}}
    )
    assert frame["ok"] is False
    assert "not allowed" in frame["error"]["message"]


@pytest.mark.asyncio
async def test_bridge_rejects_banned_aggregation_stages(patch_mongo):
    bridge = SandboxDbBridge(tenant_id="tenant-a", action_type="read", settings=Settings())
    frame = await bridge.handle(
        {
            "id": 1,
            "op": "aggregate",
            "collection": "users",
            "args": [[{"$match": {}}, {"$out": "archive"}]],
            "kwargs": {},
        }
    )
    assert frame["ok"] is False
    assert "$out" in frame["error"]["message"]


@pytest.mark.asyncio
async def test_bridge_enforces_call_limit_per_invocation(patch_mongo):
    bridge = SandboxDbBridge(
        tenant_id="tenant-a",
        action_type="read",
        settings=Settings(),
        max_calls_override=1,
    )
    first = await bridge.handle(
        {"id": 1, "op": "find", "collection": "users", "args": [{}], "kwargs": {}}
    )
    second = await bridge.handle(
        {"id": 2, "op": "find", "collection": "users", "args": [{}], "kwargs": {}}
    )
    assert first["ok"] is True
    assert second["ok"] is False
    assert "call limit" in second["error"]["message"]


@pytest.mark.asyncio
async def test_bridge_extended_json_round_trip_for_oid_and_date(patch_mongo):
    now = datetime.now(UTC).replace(microsecond=0)
    oid = ObjectId()
    await get_tenant_database("tenant-a")["events"].insert_one({"_id": oid, "created_at": now})
    bridge = SandboxDbBridge(tenant_id="tenant-a", action_type="read", settings=Settings())
    frame = await bridge.handle(
        {"id": 1, "op": "find_one", "collection": "events", "args": [{"_id": {"$oid": str(oid)}}]}
    )
    assert frame["ok"] is True
    assert frame["result"]["_id"] == {"$oid": str(oid)}
    assert "$date" in frame["result"]["created_at"]
