"""Tests for database.mongo.mongo_server_now, the authoritative clock the
distributed rate limiter anchors to.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

import database.mongo as mongo_module


class _FakeAdmin:
    def __init__(self, response):
        self._response = response

    async def command(self, *args, **kwargs):
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.admin = _FakeAdmin(response)


@pytest.mark.asyncio
async def test_mongo_server_now_normalizes_aware_time(monkeypatch):
    plus_two = timezone(timedelta(hours=2))
    server_time = datetime(2026, 1, 1, 12, 0, tzinfo=plus_two)
    monkeypatch.setattr(
        mongo_module, "get_client", lambda: _FakeClient({"system": {"currentTime": server_time}})
    )
    result = await mongo_module.mongo_server_now()
    assert result.tzinfo is UTC
    assert result == server_time


@pytest.mark.asyncio
async def test_mongo_server_now_assumes_utc_for_naive_time(monkeypatch):
    naive = datetime(2026, 1, 1, 12, 0)
    monkeypatch.setattr(
        mongo_module, "get_client", lambda: _FakeClient({"system": {"currentTime": naive}})
    )
    result = await mongo_module.mongo_server_now()
    assert result == naive.replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_mongo_server_now_falls_back_to_local_time(monkeypatch):
    local = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(mongo_module, "get_client", lambda: _FakeClient({"localTime": local}))
    result = await mongo_module.mongo_server_now()
    assert result == local


@pytest.mark.asyncio
async def test_mongo_server_now_raises_when_no_clock_value(monkeypatch):
    monkeypatch.setattr(mongo_module, "get_client", lambda: _FakeClient({"system": {}}))
    with pytest.raises(RuntimeError):
        await mongo_module.mongo_server_now()
