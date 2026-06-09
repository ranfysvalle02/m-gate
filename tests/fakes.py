"""In-memory async fakes for MongoDB and the embedding service.

These let the gateway's DB-touching code and full middleware chain run under
pytest without a live MongoDB or Ollama. They implement only the subset of the
driver surface the gateway actually uses (catalogued from the codebase):

    find_one, find, find_one_and_update, update_one, delete_many, insert_one,
    aggregate (+ cursor.to_list), and admin.command("ping").

The Atlas-only search stages ($vectorSearch / $rankFusion / $search) cannot be
emulated in process, so aggregate() accepts an optional per-collection handler
that inspects the pipeline and returns documents. Tests that exercise pure
Mongo CRUD (rate limiting, telemetry, RBAC session lookup) need no handler;
tests that exercise search inject one.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    """Minimal query matcher: equality plus the few operators the gateway uses."""
    for key, expected in query.items():
        actual = doc.get(key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
                if op == "$in":
                    actual_values = actual if isinstance(actual, list) else [actual]
                    if not set(actual_values).intersection(set(operand)):
                        return False
                elif op == "$eq":
                    if actual != operand:
                        return False
                else:  # pragma: no cover - unsupported operator guard
                    raise NotImplementedError(f"FakeCollection: unsupported operator {op}")
        else:
            if actual != expected:
                return False
    return True


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        if length is None:
            return list(self._docs)
        return list(self._docs[:length])

    def __aiter__(self):  # pragma: no cover - change-stream style iteration unused here
        async def _gen():
            for doc in self._docs:
                yield doc

        return _gen()


# Sentinel for "key did not exist" so update merges can tell absent from None.
_MISSING = object()


class FakeCollection:
    """An in-memory collection. Documents are plain dicts."""

    def __init__(
        self,
        name: str,
        aggregate_handler: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.name = name
        self.docs: list[dict[str, Any]] = []
        self._aggregate_handler = aggregate_handler
        self._search_indexes: dict[str, dict[str, Any]] = {}

    async def find_one(
        self, query: dict[str, Any], projection: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        for doc in self.docs:
            if _matches(doc, query):
                return dict(doc)
        return None

    def find(self, query: dict[str, Any] | None = None) -> _FakeCursor:
        query = query or {}
        return _FakeCursor([dict(d) for d in self.docs if _matches(d, query)])

    async def insert_one(self, doc: dict[str, Any]) -> Any:
        self.docs.append(dict(doc))

        class _Result:
            inserted_id = doc.get("_id")

        return _Result()

    async def update_one(
        self, query: dict[str, Any], update: dict[str, Any], *, upsert: bool = False
    ) -> Any:
        matched = next((d for d in self.docs if _matches(d, query)), None)
        if matched is None and upsert:
            matched = {k: v for k, v in query.items() if not isinstance(v, dict)}
            self.docs.append(matched)
        if matched is not None:
            self._apply_update(matched, update)

        class _Result:
            matched_count = 0 if matched is None else 1
            modified_count = 0 if matched is None else 1

        return _Result()

    async def replace_one(
        self,
        query: dict[str, Any],
        replacement: dict[str, Any],
        *,
        upsert: bool = False,
    ) -> Any:
        matched_index = next((i for i, d in enumerate(self.docs) if _matches(d, query)), None)
        if matched_index is None:
            if upsert:
                self.docs.append(dict(replacement))
        else:
            self.docs[matched_index] = dict(replacement)

        class _Result:
            matched_count = 0 if matched_index is None else 1
            modified_count = 0 if matched_index is None and not upsert else 1

        return _Result()

    async def find_one_and_update(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool = False,
        return_document: Any = None,
    ) -> dict[str, Any] | None:
        matched = next((d for d in self.docs if _matches(d, query)), None)
        if matched is None:
            if not upsert:
                return None
            matched = {k: v for k, v in query.items() if not isinstance(v, dict)}
            self.docs.append(matched)
        self._apply_update(matched, update)
        return dict(matched)

    async def delete_many(self, query: dict[str, Any]) -> Any:
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, query)]
        deleted = before - len(self.docs)

        class _Result:
            deleted_count = deleted

        return _Result()

    async def create_index(self, keys: Any, **kwargs: Any) -> str:
        # The gateway only relies on btree indexes for correctness via uniqueness
        # at the application layer in tests, so this is a structural no-op that
        # simply returns a deterministic name like the real driver does.
        if isinstance(keys, str):
            field_part = keys
        elif isinstance(keys, list | tuple):
            field_part = "_".join(str(field) for field, *_ in keys)
        else:
            field_part = str(keys)
        return f"{field_part}_idx"

    async def create_search_index(self, model: Any) -> str:
        name, definition = self._extract_search_index_model(model)
        self._search_indexes[name] = {"name": name, "definition": definition, "queryable": True}
        return name

    async def update_search_index(self, name: str, definition: dict[str, Any]) -> None:
        self._search_indexes[name] = {"name": name, "definition": definition, "queryable": True}

    async def list_search_indexes(self, name: str | None = None) -> _FakeCursor:
        rows = list(self._search_indexes.values())
        if name is not None:
            rows = [row for row in rows if row.get("name") == name]
        return _FakeCursor(rows)

    async def aggregate(self, pipeline: list[dict[str, Any]]) -> _FakeCursor:
        if self._aggregate_handler is not None:
            return _FakeCursor(self._aggregate_handler(pipeline))
        # Best-effort emulation of the non-Atlas list_tools pipeline
        # ($match/$project/$sort/$skip/$limit) so catalog listing works without
        # an injected handler.
        return _FakeCursor(self._emulate_basic_pipeline(pipeline))

    def _emulate_basic_pipeline(self, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        docs = [dict(d) for d in self.docs]
        for stage in pipeline:
            if "$match" in stage:
                docs = [d for d in docs if _matches(d, stage["$match"])]
            elif "$sort" in stage:
                for field, direction in reversed(list(stage["$sort"].items())):
                    docs.sort(key=lambda d: d.get(field), reverse=direction < 0)
            elif "$skip" in stage:
                docs = docs[stage["$skip"] :]
            elif "$limit" in stage:
                docs = docs[: stage["$limit"]]
            elif "$project" in stage:
                fields = [k for k, v in stage["$project"].items() if v and k != "_id"]
                docs = [{k: d.get(k) for k in fields if k in d} for d in docs]
        return docs

    @staticmethod
    def _apply_update(doc: dict[str, Any], update: dict[str, Any]) -> None:
        if "$set" in update:
            doc.update(update["$set"])
        if "$setOnInsert" in update:
            for key, value in update["$setOnInsert"].items():
                if doc.get(key, _MISSING) is _MISSING:
                    doc[key] = value
        if "$inc" in update:
            for key, amount in update["$inc"].items():
                doc[key] = doc.get(key, 0) + amount

    @staticmethod
    def _extract_search_index_model(model: Any) -> tuple[str, dict[str, Any]]:
        doc = getattr(model, "document", None)
        if isinstance(doc, dict):
            name = doc.get("name")
            definition = doc.get("definition")
            if isinstance(name, str) and isinstance(definition, dict):
                return name, definition

        name = getattr(model, "name", None)
        definition = getattr(model, "definition", None)
        if isinstance(name, str) and isinstance(definition, dict):
            return name, definition

        raise TypeError("FakeCollection could not read search index model fields.")


class FakeDatabase:
    def __init__(
        self,
        aggregate_handlers: dict[str, Callable[[list[dict[str, Any]]], list[dict[str, Any]]]]
        | None = None,
    ) -> None:
        self._collections: dict[str, FakeCollection] = {}
        self._handlers = aggregate_handlers or {}

    def __getitem__(self, name: str) -> FakeCollection:
        if name not in self._collections:
            self._collections[name] = FakeCollection(name, self._handlers.get(name))
        return self._collections[name]

    async def list_collection_names(self) -> list[str]:
        return list(self._collections.keys())

    async def command(self, command: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # Support the timeseries `create` used during tenant provisioning by
        # materializing the collection; everything else is a benign ack.
        if isinstance(command, dict) and "create" in command:
            _ = self[str(command["create"])]
        return {"ok": 1}


class FakeAdmin:
    async def command(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if args and args[0] == "hostInfo":
            return {"ok": 1, "system": {"currentTime": datetime.now(UTC)}}
        return {"ok": 1}


class FakeMongoClient:
    def __init__(
        self,
        database: FakeDatabase | None = None,
        *,
        default_db_name: str = "mcp_gateway",
    ) -> None:
        self._databases: dict[str, FakeDatabase] = {}
        self._default_db_name = default_db_name
        self._databases[default_db_name] = database or FakeDatabase()
        self.admin = FakeAdmin()

    def __getitem__(self, name: str) -> FakeDatabase:
        if name not in self._databases:
            self._databases[name] = FakeDatabase()
        return self._databases[name]


class FakeEmbeddingService:
    """Deterministic embedding stub: hashes text into a fixed-width vector.

    Same text -> same vector, different text -> (almost surely) different vector,
    which is enough for cache-key and pipeline-shape assertions.
    """

    def __init__(
        self,
        dimensions: int = 8,
        fail: bool = False,
        *,
        model_id: str = "fake-embeddings",
    ) -> None:
        self.dimensions = dimensions
        self.model_id = model_id
        self.fail = fail
        self.calls: list[str] = []

    async def embed_text(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.fail:
            from services.embeddings import EmbeddingUnavailableError

            raise EmbeddingUnavailableError("stubbed failure")
        seed = sum(ord(c) for c in text) or 1
        return [((seed * (i + 1)) % 97) / 97.0 for i in range(self.dimensions)]

    async def embed_texts(self, texts):
        return [await self.embed_text(t) for t in texts]


_WORD_RE = re.compile(r"[a-z0-9]+")


def lexical_overlap_handler(
    get_docs: Callable[[], list[dict[str, Any]]],
) -> Callable[[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Build an aggregate handler that fakes search by lexical token overlap.

    It reads the query string out of a $search / $vectorSearch-ish pipeline,
    applies any $match scope filter, ranks catalog docs by word overlap against
    name+description+server, and projects a `score`. Good enough to assert
    routing, scope filtering, and result shaping without Atlas.
    """

    def _scan(stages: list[dict[str, Any]], state: dict[str, Any]) -> None:
        """Walk a (possibly $rankFusion-nested) pipeline collecting query+filter."""
        for stage in stages:
            if "$search" in stage:
                state["query_text"] = stage["$search"]["text"]["query"]
            if "$vectorSearch" in stage:
                vs = stage["$vectorSearch"]
                if "filter" in vs:
                    state["scope_filter"] = vs["filter"]
            if "$match" in stage:
                state["scope_filter"] = stage["$match"]
            if "$limit" in stage:
                state["limit"] = stage["$limit"]
            if "$rankFusion" in stage:
                for nested in stage["$rankFusion"]["input"]["pipelines"].values():
                    _scan(nested, state)

    def handler(pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        docs = [dict(d) for d in get_docs()]
        state: dict[str, Any] = {"scope_filter": None, "query_text": "", "limit": len(docs)}
        _scan(pipeline, state)
        scope_filter = state["scope_filter"]
        query_text = state["query_text"]
        limit = state["limit"]
        if scope_filter is not None:
            docs = [d for d in docs if _matches(d, scope_filter)]
        terms = set(_WORD_RE.findall(query_text.lower()))
        scored = []
        for d in docs:
            blob = f"{d.get('name', '')} {d.get('description', '')} {d.get('server', '')}".lower()
            overlap = len(terms.intersection(_WORD_RE.findall(blob)))
            scored.append((overlap, d))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        result = []
        for overlap, d in scored[:limit]:
            d = {k: v for k, v in d.items() if k not in {"_id", "embedding"}}
            d["score"] = float(overlap)
            result.append(d)
        return result

    return handler
