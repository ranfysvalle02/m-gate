from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pymongo.errors import OperationFailure

from database.mongo import get_control_database, get_tenant_database, tenant_db_name


class FakeChangeStream:
    def __init__(self, events: list[dict[str, Any]], *, cancel_after_events: bool = False) -> None:
        self._events = events
        self._index = 0
        self._cancel_after_events = cancel_after_events
        self.resume_token: Any | None = None

    async def __aenter__(self) -> FakeChangeStream:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def __aiter__(self) -> FakeChangeStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._index >= len(self._events):
            if self._cancel_after_events:
                raise asyncio.CancelledError()
            raise StopAsyncIteration
        event = self._events[self._index]
        self._index += 1
        self.resume_token = event.get("_id")
        return event


@pytest.mark.asyncio
async def test_watch_loop_isolates_bad_event_and_persists_resume_token(patch_mongo, monkeypatch):
    import services.registry_watcher as rw

    mounted: list[str] = []

    class _Reg:
        async def mount_or_update(self, doc):
            if doc["server"] == "bad":
                raise RuntimeError("broken doc")
            mounted.append(doc["server"])

        async def unmount(self, server_name, tenant_id=None):
            return None

    monkeypatch.setattr(rw, "get_proxy_registry", lambda: _Reg())

    control_db = get_control_database()
    tenant_id = "local-dev"
    await control_db["tenants"].insert_one({"tenant_id": tenant_id})
    ns_db = tenant_db_name(tenant_id)

    events = [
        {
            "_id": {"_data": "token-1"},
            "operationType": "insert",
            "ns": {"db": ns_db, "coll": "routing_registry"},
            "fullDocument": {"server": "bad", "enabled": True},
        },
        {
            "_id": {"_data": "token-2"},
            "operationType": "insert",
            "ns": {"db": ns_db, "coll": "routing_registry"},
            "fullDocument": {"server": "good", "endpoint": "http://good/mcp", "enabled": True},
        },
    ]

    class _Client:
        async def watch(self, **kwargs):
            return FakeChangeStream(events, cancel_after_events=True)

    monkeypatch.setattr(rw, "get_client", lambda: _Client())

    with pytest.raises(asyncio.CancelledError):
        await rw._watch_loop()

    assert mounted == ["good"]
    state_doc = await control_db["watcher_state"].find_one({"_id": "routing_registry"})
    assert state_doc is not None
    assert state_doc["resume_token"] == {"_data": "token-2"}


@pytest.mark.asyncio
async def test_watch_loop_uses_saved_token_and_skips_initial_sync(patch_mongo, monkeypatch):
    import services.registry_watcher as rw

    resume_token = {"_data": "resume-me"}
    control_db = get_control_database()
    tenant_id = "local-dev"
    await control_db["tenants"].insert_one({"tenant_id": tenant_id})
    await control_db["watcher_state"].insert_one(
        {"_id": "routing_registry", "resume_token": resume_token}
    )
    await get_tenant_database(tenant_id)["routing_registry"].insert_one(
        {"server": "would-be-initial-sync", "endpoint": "http://x/mcp", "enabled": True}
    )

    mounted: list[str] = []

    class _Reg:
        async def mount_or_update(self, doc):
            mounted.append(doc["server"])

        async def unmount(self, server_name, tenant_id=None):
            return None

    monkeypatch.setattr(rw, "get_proxy_registry", lambda: _Reg())
    watch_kwargs: dict[str, Any] = {}

    class _Client:
        async def watch(self, **kwargs):
            watch_kwargs.update(kwargs)
            raise asyncio.CancelledError()

    monkeypatch.setattr(rw, "get_client", lambda: _Client())

    with pytest.raises(asyncio.CancelledError):
        await rw._watch_loop()

    assert watch_kwargs.get("resume_after") == resume_token
    assert mounted == []


@pytest.mark.asyncio
async def test_non_resumable_error_clears_token_and_resyncs(patch_mongo, monkeypatch):
    import services.registry_watcher as rw

    old_token = {"_data": "old-token"}
    control_db = get_control_database()
    tenant_id = "local-dev"
    await control_db["tenants"].insert_one({"tenant_id": tenant_id})
    await control_db["watcher_state"].insert_one(
        {"_id": "routing_registry", "resume_token": old_token}
    )
    await get_tenant_database(tenant_id)["routing_registry"].insert_one(
        {"server": "resync", "endpoint": "http://resync/mcp", "enabled": True}
    )

    mounted: list[str] = []

    class _Reg:
        async def mount_or_update(self, doc):
            mounted.append(doc["server"])

        async def unmount(self, server_name, tenant_id=None):
            return None

    monkeypatch.setattr(rw, "get_proxy_registry", lambda: _Reg())
    seen_watch_kwargs: list[dict[str, Any]] = []

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def watch(self, **kwargs):
            self.calls += 1
            seen_watch_kwargs.append(dict(kwargs))
            if self.calls == 1:
                raise OperationFailure("history lost", code=286)
            return FakeChangeStream([], cancel_after_events=True)

    monkeypatch.setattr(rw, "get_client", lambda: _Client())

    with pytest.raises(asyncio.CancelledError):
        await rw._watch_loop()

    assert seen_watch_kwargs[0].get("resume_after") == old_token
    assert "resume_after" not in seen_watch_kwargs[1]
    assert "resync" in mounted
    assert await control_db["watcher_state"].find_one({"_id": "routing_registry"}) is None
