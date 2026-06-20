from __future__ import annotations

import pytest
from pymongo.errors import OperationFailure

from database import indexes


def _search_mgmt_unavailable() -> OperationFailure:
    return OperationFailure(
        "Error connecting to Search Index Management service.",
        code=125,
        details={"codeName": "CommandFailed"},
    )


class _FakeCollection:
    """Minimal async stand-in for a Motor collection's search-index DDL."""

    def __init__(self, create_side_effects: list[Exception | None]):
        self._create_side_effects = list(create_side_effects)
        self.create_calls = 0
        self.update_calls = 0
        self.last_update: dict | None = None

    async def create_search_index(self, *, model):  # noqa: ANN001
        self.create_calls += 1
        effect = self._create_side_effects.pop(0)
        if effect is not None:
            raise effect

    async def update_search_index(self, *, name, definition):  # noqa: ANN001
        self.update_calls += 1
        self.last_update = {"name": name, "definition": definition}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Keep the bounded-retry loop instantaneous in tests.
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(indexes.asyncio, "sleep", _instant)


@pytest.mark.asyncio
async def test_upsert_retries_transient_search_mgmt_then_succeeds():
    coll = _FakeCollection([_search_mgmt_unavailable(), _search_mgmt_unavailable(), None])
    await indexes.upsert_search_index(
        coll, name="x", definition={"mappings": {"dynamic": True}}, index_type="search"
    )
    assert coll.create_calls == 3
    assert coll.update_calls == 0


@pytest.mark.asyncio
async def test_upsert_gives_up_after_exhausting_retries():
    attempts = indexes._SEARCH_MGMT_RETRY_ATTEMPTS
    coll = _FakeCollection([_search_mgmt_unavailable()] * attempts)
    with pytest.raises(OperationFailure):
        await indexes.upsert_search_index(
            coll, name="x", definition={"mappings": {"dynamic": True}}, index_type="search"
        )
    # Tried exactly the budget, no more.
    assert coll.create_calls == attempts


@pytest.mark.asyncio
async def test_upsert_does_not_retry_non_transient_errors():
    fatal = OperationFailure("bad definition", code=14, details={"codeName": "TypeMismatch"})
    coll = _FakeCollection([fatal])
    with pytest.raises(OperationFailure):
        await indexes.upsert_search_index(
            coll, name="x", definition={"mappings": {"dynamic": True}}, index_type="search"
        )
    assert coll.create_calls == 1


@pytest.mark.asyncio
async def test_upsert_existing_index_updates_in_place():
    exists = OperationFailure("exists", code=68, details={"codeName": "IndexAlreadyExists"})
    coll = _FakeCollection([exists])
    definition = {"mappings": {"dynamic": True}}
    await indexes.upsert_search_index(coll, name="x", definition=definition, index_type="search")
    assert coll.create_calls == 1
    assert coll.update_calls == 1
    assert coll.last_update == {"name": "x", "definition": definition}
