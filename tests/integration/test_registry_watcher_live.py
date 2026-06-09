from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from config.settings import get_settings
from database.mongo import get_control_database, tenant_db_name

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_WATCHER_INSTANCE_ID = "itest-watcher"
_TEST_SERVER_PREFIX = "itest_watcher_"


def _server_doc(
    server_name: str, *, enabled: bool = True, include_endpoint: bool = True
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": server_name,
        "server": server_name,
        "enabled": enabled,
        "metadata": {"scopes": ["itest"]},
        "tools": [
            {
                "name": "echo_tool",
                "description": "Integration watcher test tool.",
                "input_schema": {"type": "object"},
            }
        ],
    }
    if include_endpoint:
        doc["endpoint"] = "http://unused.invalid/mcp"
        doc["transport"] = "streamable_http"
    return doc


async def _until(
    predicate: Callable[[], bool | Awaitable[bool]],
    *,
    timeout: float = 30.0,
    interval: float = 0.5,
    message: str = "Condition timed out",
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        await asyncio.sleep(interval)
    raise AssertionError(message)


async def _mount_via_watcher(
    live_db, registry, server_doc: dict[str, Any], *, timeout: float = 25.0
) -> None:
    tenant_id = server_doc.get("tenant_id") or "local-dev"
    rev = 0
    while True:
        rev += 1
        doc = dict(server_doc)
        doc["rev"] = rev
        await live_db["routing_registry"].replace_one({"_id": server_doc["_id"]}, doc, upsert=True)
        if registry.get_server(server_doc["server"], tenant_id=tenant_id) is not None:
            return
        if rev * 0.5 >= timeout:
            break
        await asyncio.sleep(0.5)
    raise AssertionError(f"Watcher never mounted server '{server_doc['server']}'")


@pytest.fixture
async def watcher_registry(live_db, live_embeddings, monkeypatch):
    import services.registry_watcher as rw
    from services.proxy_registry import InMemoryFastMCPRegistry

    settings = get_settings()
    object.__setattr__(settings, "gateway_instance_id", _WATCHER_INSTANCE_ID)
    watcher_state_id = rw._resume_doc_id(_WATCHER_INSTANCE_ID)
    control_db = get_control_database()
    await rw.stop_registry_watcher()
    await control_db["watcher_state"].delete_many({"_id": watcher_state_id})
    await control_db["tenants"].update_one(
        {"tenant_id": "local-dev"},
        {"$set": {"tenant_id": "local-dev", "db_name": tenant_db_name("local-dev")}},
        upsert=True,
    )

    registry = InMemoryFastMCPRegistry(embedding_service=live_embeddings)
    monkeypatch.setattr(rw, "get_proxy_registry", lambda: registry)

    await rw.start_registry_watcher()
    await _until(
        lambda: registry.get_server("weather", tenant_id="local-dev") is not None,
        message="Watcher never reached steady state after startup",
    )
    await asyncio.sleep(0.25)

    try:
        yield registry
    finally:
        await rw.stop_registry_watcher()
        await control_db["watcher_state"].delete_many({"_id": watcher_state_id})
        await live_db["routing_registry"].delete_many(
            {"_id": {"$regex": f"^{_TEST_SERVER_PREFIX}"}}
        )
        await live_db["tool_catalog"].delete_many({"server": {"$regex": f"^{_TEST_SERVER_PREFIX}"}})


@pytest.fixture
async def temp_server(live_db):
    name = f"{_TEST_SERVER_PREFIX}{uuid.uuid4().hex[:8]}"
    yield name
    await live_db["routing_registry"].delete_many({"_id": name})
    await live_db["tool_catalog"].delete_many({"server": name})


async def test_watcher_mounts_server_on_insert(live_db, watcher_registry, temp_server):
    import services.registry_watcher as rw

    before = rw.get_catalog_version()
    doc = _server_doc(temp_server, enabled=True)
    await _mount_via_watcher(live_db, watcher_registry, doc)

    await _until(
        lambda: rw.get_catalog_version() > before,
        message="catalog_version did not advance after mount event",
    )
    catalog_doc = await live_db["tool_catalog"].find_one(
        {"server": temp_server, "name": "echo_tool"}
    )
    assert catalog_doc is not None
    assert watcher_registry.get_server(temp_server) is not None


async def test_watcher_initial_sync_includes_seeded_secure_stdio_server(watcher_registry):
    secure_stdio = watcher_registry.get_server("secure-stdio", tenant_id="local-dev")
    assert secure_stdio is not None
    assert secure_stdio.transport == "stdio"
    assert secure_stdio.command == "python"
    assert secure_stdio.env is not None
    assert secure_stdio.env.get("DOWNSTREAM_API_TOKEN") == "demo-secret-token"


async def test_watcher_unmounts_server_on_disable(live_db, watcher_registry, temp_server):
    doc = _server_doc(temp_server, enabled=True)
    await _mount_via_watcher(live_db, watcher_registry, doc)
    await live_db["routing_registry"].update_one({"_id": temp_server}, {"$set": {"enabled": False}})

    await _until(
        lambda: watcher_registry.get_server(temp_server) is None,
        message="Server remained mounted after disable event",
    )
    assert await live_db["tool_catalog"].count_documents({"server": temp_server}) == 0


async def test_watcher_unmounts_server_on_delete(live_db, watcher_registry, temp_server):
    doc = _server_doc(temp_server, enabled=True)
    await _mount_via_watcher(live_db, watcher_registry, doc)
    await live_db["routing_registry"].delete_one({"_id": temp_server})

    await _until(
        lambda: watcher_registry.get_server(temp_server) is None,
        message="Server remained mounted after delete event",
    )
    assert await live_db["tool_catalog"].count_documents({"server": temp_server}) == 0


async def test_watcher_isolates_poisoned_event(live_db, watcher_registry, temp_server):
    bad_server = f"{_TEST_SERVER_PREFIX}{uuid.uuid4().hex[:8]}_bad"
    bad_doc = _server_doc(bad_server, enabled=True, include_endpoint=False)
    await live_db["routing_registry"].replace_one({"_id": bad_server}, bad_doc, upsert=True)

    good_doc = _server_doc(temp_server, enabled=True)
    await _mount_via_watcher(live_db, watcher_registry, good_doc)

    assert watcher_registry.get_server(bad_server) is None
    assert watcher_registry.get_server(temp_server) is not None


async def test_watcher_resumes_missed_events_after_restart(
    live_db, watcher_registry, temp_server, monkeypatch
):
    import services.registry_watcher as rw

    watcher_state_id = rw._resume_doc_id(_WATCHER_INSTANCE_ID)
    first_doc = _server_doc(temp_server, enabled=True)
    await _mount_via_watcher(live_db, watcher_registry, first_doc)
    await _until(
        lambda: get_control_database()["watcher_state"].find_one({"_id": watcher_state_id}),
        message="resume token never persisted before restart",
    )

    await rw.stop_registry_watcher()

    initial_sync_calls = 0
    real_initial_sync = rw._initial_sync_all_tenants

    async def counting_initial_sync(registry):
        nonlocal initial_sync_calls
        initial_sync_calls += 1
        await real_initial_sync(registry)

    monkeypatch.setattr(rw, "_initial_sync_all_tenants", counting_initial_sync)

    second_server = f"{_TEST_SERVER_PREFIX}{uuid.uuid4().hex[:8]}_resume"
    second_doc = _server_doc(second_server, enabled=True)
    await live_db["routing_registry"].replace_one({"_id": second_server}, second_doc, upsert=True)

    await rw.start_registry_watcher()
    await _until(
        lambda: watcher_registry.get_server(second_server) is not None,
        message="Watcher did not replay missed event after restart",
    )
    assert initial_sync_calls == 0
