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


def _resolve_path(doc: dict[str, Any], key: str) -> Any:
    """Resolve a (possibly dotted) field path against a nested document.

    MongoDB treats ``"metadata.always_included"`` as a walk into the embedded
    ``metadata`` sub-document; the fake mirrors that so queries on nested fields
    behave like the real driver. A non-dotted key is a plain ``dict.get``.
    """
    if "." not in key:
        return doc.get(key)
    current: Any = doc
    for part in key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    """Minimal query matcher: equality plus the few operators the gateway uses."""
    for key, expected in query.items():
        actual = _resolve_path(doc, key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
                if op == "$in":
                    actual_values = actual if isinstance(actual, list) else [actual]
                    if not set(actual_values).intersection(set(operand)):
                        return False
                elif op == "$nin":
                    actual_values = actual if isinstance(actual, list) else [actual]
                    if set(actual_values).intersection(set(operand)):
                        return False
                elif op == "$eq":
                    if actual != operand:
                        return False
                elif op == "$ne":
                    if actual == operand:
                        return False
                elif op == "$gt":
                    if actual is None or actual <= operand:
                        return False
                elif op == "$gte":
                    if actual is None or actual < operand:
                        return False
                elif op == "$lt":
                    if actual is None or actual >= operand:
                        return False
                elif op == "$lte":
                    if actual is None or actual > operand:
                        return False
                elif op == "$exists":
                    present = _resolve_path(doc, key) is not None
                    if bool(operand) != present:
                        return False
                else:  # pragma: no cover - unsupported operator guard
                    raise NotImplementedError(f"FakeCollection: unsupported operator {op}")
        else:
            if actual != expected:
                return False
    return True


def _date_trunc(value: Any, unit: str, bin_size: int) -> Any:
    """Truncate a datetime to a unit/binSize boundary (UTC), like ``$dateTrunc``."""
    if not isinstance(value, datetime):
        return None
    bin_size = max(1, int(bin_size or 1))
    if unit == "minute":
        floored = (value.minute // bin_size) * bin_size
        return value.replace(minute=floored, second=0, microsecond=0)
    if unit == "hour":
        floored = (value.hour // bin_size) * bin_size
        return value.replace(hour=floored, minute=0, second=0, microsecond=0)
    if unit == "day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    return value.replace(second=0, microsecond=0)


def _eval_expr(doc: dict[str, Any], expr: Any) -> Any:
    """Evaluate the aggregation-expression subset the analytics pipelines use."""
    if isinstance(expr, str):
        if expr.startswith("$"):
            return _resolve_path(doc, expr[1:])
        return expr
    if isinstance(expr, dict):
        if "$dateTrunc" in expr:
            spec = expr["$dateTrunc"]
            return _date_trunc(
                _eval_expr(doc, spec.get("date")),
                str(spec.get("unit", "hour")),
                int(spec.get("binSize", 1) or 1),
            )
        if "$ifNull" in expr:
            primary, fallback = expr["$ifNull"]
            resolved = _eval_expr(doc, primary)
            return resolved if resolved is not None else _eval_expr(doc, fallback)
        if "$cond" in expr:
            branches = expr["$cond"]
            if isinstance(branches, list):
                condition, then_expr, else_expr = branches
            else:
                condition = branches["if"]
                then_expr = branches["then"]
                else_expr = branches["else"]
            return (
                _eval_expr(doc, then_expr)
                if _truthy(_eval_expr(doc, condition))
                else _eval_expr(doc, else_expr)
            )
        if "$regexMatch" in expr:
            spec = expr["$regexMatch"]
            text = _eval_expr(doc, spec.get("input"))
            if not isinstance(text, str):
                return False
            flags = re.IGNORECASE if "i" in str(spec.get("options", "")) else 0
            return re.search(str(spec.get("regex", "")), text, flags) is not None
        # Otherwise treat as a composite group-key mapping: evaluate each value.
        return {key: _eval_expr(doc, value) for key, value in expr.items()}
    return expr


def _truthy(value: Any) -> bool:
    """MongoDB falsiness: false, null, 0, and missing are false; all else true."""
    return value not in (False, None, 0, 0.0)


def _hashable(value: Any) -> Any:
    """Map a group-key value to something usable as a dict key."""
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    return value


def _reduce_accumulator(op: str, values: list[Any], operand: Any) -> Any:
    numeric = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    if op == "$sum":
        return sum(numeric)
    if op == "$avg":
        return sum(numeric) / len(numeric) if numeric else None
    if op == "$min":
        return min(values) if values else None
    if op == "$max":
        return max(values) if values else None
    if op == "$push":
        return list(values)
    if op == "$percentile":
        if not numeric:
            return [None for _ in (operand.get("p", []) if isinstance(operand, dict) else [])]
        ordered = sorted(numeric)
        ps = operand.get("p", []) if isinstance(operand, dict) else []
        result = []
        for p in ps:
            rank = max(0, min(len(ordered) - 1, int(round(float(p) * (len(ordered) - 1)))))
            result.append(ordered[rank])
        return result
    return None


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self._skip = 0
        self._limit: int | None = None

    def skip(self, count: int) -> _FakeCursor:
        self._skip = max(0, int(count or 0))
        return self

    def limit(self, count: int | None) -> _FakeCursor:
        self._limit = int(count) if count and int(count) > 0 else None
        return self

    def batch_size(self, _count: int) -> _FakeCursor:
        # Server-side streaming hint; a no-op for the in-memory fake.
        return self

    def sort(self, key_or_list: Any, direction: int | None = None) -> _FakeCursor:
        if isinstance(key_or_list, str):
            keys = [(key_or_list, 1 if direction is None else direction)]
        else:
            keys = [(field, dirn) for field, dirn in key_or_list]
        # Apply least-significant key first so the primary key wins (stable sort).
        for field, dirn in reversed(keys):
            self._docs = sorted(
                self._docs,
                key=lambda d, f=field: (d.get(f) is None, d.get(f)),
                reverse=dirn < 0,
            )
        return self

    def _window(self) -> list[dict[str, Any]]:
        docs = self._docs[self._skip :]
        if self._limit is not None:
            docs = docs[: self._limit]
        return docs

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        docs = self._window()
        if length is None:
            return list(docs)
        return list(docs[:length])

    def __aiter__(self):
        docs = self._window()

        async def _gen():
            for doc in docs:
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

    def find(
        self, query: dict[str, Any] | None = None, projection: dict[str, Any] | None = None
    ) -> _FakeCursor:
        query = query or {}
        docs = [dict(d) for d in self.docs if _matches(d, query)]
        if isinstance(projection, dict) and projection:
            keys = {k for k, v in projection.items() if v}
            if keys:
                docs = [{k: d.get(k) for k in keys if k in d} for d in docs]
        return _FakeCursor(docs)

    async def count_documents(self, query: dict[str, Any] | None = None, **_kwargs: Any) -> int:
        query = query or {}
        return len([d for d in self.docs if _matches(d, query)])

    async def distinct(
        self, field: str, query: dict[str, Any] | None = None, **_kwargs: Any
    ) -> list[Any]:
        query = query or {}
        values = []
        seen = set()
        for doc in self.docs:
            if not _matches(doc, query):
                continue
            value = doc.get(field)
            marker = repr(value)
            if marker in seen:
                continue
            seen.add(marker)
            values.append(value)
        return values

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

    async def delete_one(self, query: dict[str, Any]) -> Any:
        index = next((i for i, d in enumerate(self.docs) if _matches(d, query)), None)
        if index is not None:
            self.docs.pop(index)

        class _Result:
            deleted_count = 0 if index is None else 1

        return _Result()

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

    async def drop_search_index(self, name: str) -> None:
        self._search_indexes.pop(name, None)

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
            elif "$group" in stage:
                docs = self._emulate_group(docs, stage["$group"])
            elif "$sort" in stage:
                for field, direction in reversed(list(stage["$sort"].items())):
                    docs.sort(
                        key=lambda d, f=field: (d.get(f) is None, d.get(f)),
                        reverse=direction < 0,
                    )
            elif "$skip" in stage:
                docs = docs[stage["$skip"] :]
            elif "$limit" in stage:
                docs = docs[: stage["$limit"]]
            elif "$project" in stage:
                fields = [k for k, v in stage["$project"].items() if v and k != "_id"]
                docs = [{k: d.get(k) for k in fields if k in d} for d in docs]
        return docs

    def _emulate_group(
        self, docs: list[dict[str, Any]], spec: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Emulate a ``$group`` stage for the accumulators the gateway uses.

        Supports ``_id`` as null, a field path (``"$period"``), a composite mapping
        (``{"server": "$metadata.server"}``), or a ``$dateTrunc`` expression, and
        the ``$sum`` / ``$avg`` / ``$min`` / ``$max`` / ``$push`` / ``$percentile``
        accumulators (whose inputs may themselves be ``$cond`` / ``$regexMatch`` /
        ``$ifNull`` expressions). Enough to make the analytics aggregations compute
        real results in-process; not a general MongoDB engine.
        """
        id_expr = spec.get("_id")
        accumulators = {k: v for k, v in spec.items() if k != "_id"}
        # Insertion-ordered buckets keyed by a hashable form of the group key.
        buckets: dict[Any, dict[str, Any]] = {}
        for doc in docs:
            key_value = _eval_expr(doc, id_expr)
            key_hash = _hashable(key_value)
            bucket = buckets.get(key_hash)
            if bucket is None:
                bucket = {"_key": key_value, "_acc": {field: [] for field in accumulators}}
                buckets[key_hash] = bucket
            for field, acc_spec in accumulators.items():
                op, operand = next(iter(acc_spec.items()))
                if op == "$sum":
                    value = _eval_expr(doc, operand)
                    if isinstance(value, bool):
                        value = int(value)
                    if isinstance(value, int | float):
                        bucket["_acc"][field].append(float(value))
                elif op in ("$avg", "$min", "$max", "$push"):
                    bucket["_acc"][field].append(_eval_expr(doc, operand))
                elif op == "$percentile":
                    value = _eval_expr(doc, operand.get("input"))
                    if isinstance(value, int | float):
                        bucket["_acc"][field].append(float(value))
                # Unknown accumulators collapse to None below.

        results: list[dict[str, Any]] = []
        for bucket in buckets.values():
            row: dict[str, Any] = {"_id": bucket["_key"]}
            for field, acc_spec in accumulators.items():
                op, operand = next(iter(acc_spec.items()))
                values = bucket["_acc"][field]
                row[field] = _reduce_accumulator(op, values, operand)
            results.append(row)
        return results

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
        if "$addToSet" in update:
            for key, value in update["$addToSet"].items():
                existing = doc.get(key)
                existing = list(existing) if isinstance(existing, list) else []
                if value not in existing:
                    existing.append(value)
                doc[key] = existing
        if "$pull" in update:
            for key, value in update["$pull"].items():
                existing = doc.get(key)
                if isinstance(existing, list):
                    doc[key] = [item for item in existing if item != value]

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

    async def drop_database(self, name: str) -> None:
        self._databases.pop(name, None)


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

    async def detect_dimensions(self) -> int:
        vector = await self.embed_text("dimension probe")
        return len(vector)


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
