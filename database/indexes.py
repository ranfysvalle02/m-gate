from __future__ import annotations

import asyncio
import time
from typing import Any

from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

from config.settings import get_settings
from database.errors import is_index_already_exists
from database.mongo import get_tenant_database

VECTOR_INDEX_NAME = "hybrid-vector-search"
TEXT_INDEX_NAME = "hybrid-full-text-search"


async def upsert_search_index(
    collection,
    *,
    name: str,
    definition: dict[str, Any],
    index_type: str,
) -> None:
    model = SearchIndexModel(name=name, type=index_type, definition=definition)
    try:
        await collection.create_search_index(model=model)
        return
    except OperationFailure as exc:
        if not is_index_already_exists(exc):
            raise

    # Index exists. Update in place.
    await collection.update_search_index(name=name, definition=definition)


async def _wait_for_queryable_index(
    collection,
    index_name: str,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        cursor = await collection.list_search_indexes(index_name)
        indexes = await cursor.to_list(length=5)
        if indexes and indexes[0].get("queryable") is True:
            return indexes[0]
        await asyncio.sleep(3)
    raise TimeoutError(f"Timed out waiting for search index '{index_name}' to become queryable.")


async def ensure_tool_catalog_indexes(
    *,
    collection: Any | None = None,
    wait_for_queryable: bool = True,
    dimensions: int | None = None,
) -> None:
    settings = get_settings()
    target_collection = (
        collection
        if collection is not None
        else get_tenant_database(settings.default_tenant_id)["tool_catalog"]
    )
    vector_dimensions = dimensions or settings.ollama_dimensions

    vector_definition = {
        "fields": [
            {
                "type": "vector",
                "path": "embedding",
                "numDimensions": vector_dimensions,
                "similarity": "cosine",
            },
            # Identity-bound scope: indexed as a filter field so the same
            # $vectorSearch can narrow candidates by the caller's groups before
            # ranking by meaning (Section 1 of the blog).
            {"type": "filter", "path": "scopes"},
        ]
    }
    text_definition = {"mappings": {"dynamic": True}}

    await upsert_search_index(
        target_collection,
        name=VECTOR_INDEX_NAME,
        definition=vector_definition,
        index_type="vectorSearch",
    )
    await upsert_search_index(
        target_collection,
        name=TEXT_INDEX_NAME,
        definition=text_definition,
        index_type="search",
    )

    if wait_for_queryable:
        await _wait_for_queryable_index(target_collection, VECTOR_INDEX_NAME)
        await _wait_for_queryable_index(target_collection, TEXT_INDEX_NAME)
