from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from bson import json_util

from config.settings import Settings, get_settings
from database.mongo import get_tenant_database

ALLOWED_ACTION_TYPES = frozenset({"read", "write", "destructive"})

_READ_OPS = frozenset({"find_one", "find", "aggregate", "count_documents", "distinct"})
_WRITE_OPS = _READ_OPS | frozenset({"insert_one", "insert_many", "update_one", "update_many"})
_DESTRUCTIVE_OPS = _WRITE_OPS | frozenset({"delete_one", "delete_many"})
_OPS_BY_ACTION = {
    "read": _READ_OPS,
    "write": _WRITE_OPS,
    "destructive": _DESTRUCTIVE_OPS,
}

_COLLECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BANNED_AGG_STAGES = frozenset(
    {
        "$out",
        "$merge",
        "$function",
        "$where",
        "$accumulator",
        "$documents",
        "$unionWith",
        "$currentOp",
        "$listLocalSessions",
        "$listSessions",
    }
)
_MAX_PIPELINE_STAGES = 25


def _to_extjson(value: Any) -> Any:
    """Normalize to strict JSON-compatible Extended JSON."""
    return json.loads(json_util.dumps(value))


def _from_extjson(value: Any) -> Any:
    """Decode Extended JSON payloads into native Python/BSON types."""
    return json_util.loads(json.dumps(value))


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, int):
        return value != 0
    return False


async def _maybe_await(value: Any) -> Any:
    if isinstance(value, Awaitable):
        return await value
    return value


class SandboxDbBridge:
    """Tenant-scoped, host-side DB RPC dispatcher for sandboxed code tools."""

    def __init__(
        self,
        *,
        tenant_id: str,
        action_type: str,
        settings: Settings | None = None,
        max_calls_override: int | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.tenant_id = tenant_id
        self.action_type = action_type if action_type in ALLOWED_ACTION_TYPES else "read"
        self.calls = 0
        configured_max_calls = max(0, int(self.settings.sandbox_db_max_calls_per_invocation))
        if max_calls_override is not None:
            configured_max_calls = max(0, int(max_calls_override))
        self.max_calls = configured_max_calls
        self.max_docs = max(1, int(self.settings.sandbox_db_max_docs))
        self.query_timeout_ms = max(1, int(self.settings.sandbox_db_query_timeout_ms))
        self.max_result_bytes = max(1024, int(self.settings.sandbox_db_max_result_bytes))

    async def handle(self, rpc: dict[str, Any]) -> dict[str, Any]:
        rpc_id = rpc.get("id")
        try:
            payload = await self._dispatch(rpc)
            response = {"ok": True, "result": _to_extjson(payload)}
        except Exception as exc:  # noqa: BLE001 - always return structured failure
            response = {"ok": False, "error": {"type": "db_rpc_error", "message": str(exc)}}

        encoded = json.dumps(response).encode("utf-8")
        if len(encoded) > self.max_result_bytes:
            response = {
                "ok": False,
                "error": {
                    "type": "db_rpc_error",
                    "message": "DB RPC response exceeded size limit.",
                },
            }
        return {"type": "db_rpc_result", "id": rpc_id, **response}

    async def _dispatch(self, rpc: dict[str, Any]) -> Any:
        if self.max_calls > 0 and self.calls >= self.max_calls:
            raise RuntimeError("DB RPC call limit exceeded for this invocation.")
        self.calls += 1

        op = str(rpc.get("op") or "").strip()
        collection_name = str(rpc.get("collection") or "").strip()
        raw_args = rpc.get("args")
        args = raw_args if isinstance(raw_args, list) else []
        raw_kwargs = rpc.get("kwargs")
        kwargs = raw_kwargs if isinstance(raw_kwargs, dict) else {}

        self._validate_op(op)
        collection = self._collection(collection_name)

        decoded_args = [_from_extjson(arg) for arg in args]
        decoded_kwargs = {str(k): _from_extjson(v) for k, v in kwargs.items()}

        if op == "find_one":
            return await self._find_one(collection, decoded_args, decoded_kwargs)
        if op == "find":
            return await self._find(collection, decoded_args, decoded_kwargs)
        if op == "aggregate":
            return await self._aggregate(collection, decoded_args, decoded_kwargs)
        if op == "count_documents":
            return await self._count_documents(collection, decoded_args, decoded_kwargs)
        if op == "distinct":
            return await self._distinct(collection, decoded_args, decoded_kwargs)
        if op == "insert_one":
            return await self._insert_one(collection, decoded_args, decoded_kwargs)
        if op == "insert_many":
            return await self._insert_many(collection, decoded_args, decoded_kwargs)
        if op == "update_one":
            return await self._update_one(collection, decoded_args, decoded_kwargs)
        if op == "update_many":
            return await self._update_many(collection, decoded_args, decoded_kwargs)
        if op == "delete_one":
            return await self._delete_one(collection, decoded_args, decoded_kwargs)
        if op == "delete_many":
            return await self._delete_many(collection, decoded_args, decoded_kwargs)
        raise RuntimeError(f"Unsupported DB operation '{op}'.")

    def _collection(self, collection_name: str):
        if not _COLLECTION_RE.match(collection_name):
            raise RuntimeError("Invalid collection name.")
        if collection_name.startswith("system.") or "$" in collection_name:
            raise RuntimeError("Collection is not allowed.")
        return get_tenant_database(self.tenant_id)[collection_name]

    def _validate_op(self, op: str) -> None:
        allowed = _OPS_BY_ACTION[self.action_type]
        if op not in allowed:
            raise RuntimeError(
                f"Operation '{op}' is not allowed for action_type '{self.action_type}'."
            )

    async def _find_one(self, collection, args: list[Any], kwargs: dict[str, Any]) -> Any:
        query = args[0] if args else {}
        projection = args[1] if len(args) > 1 else kwargs.get("projection")
        if not isinstance(query, dict):
            raise RuntimeError("find_one requires a query object.")
        return await self._call_with_timeout(
            lambda: collection.find_one(query, projection=projection)
        )

    async def _find(self, collection, args: list[Any], kwargs: dict[str, Any]) -> list[Any]:
        query = args[0] if args else {}
        projection = args[1] if len(args) > 1 else kwargs.get("projection")
        limit = kwargs.get("limit", self.max_docs)
        if not isinstance(query, dict):
            raise RuntimeError("find requires a query object.")
        if projection is not None and not isinstance(projection, dict):
            raise RuntimeError("find projection must be an object.")
        limit = max(1, min(self.max_docs, int(limit)))
        cursor = collection.find(query, projection=projection)
        if hasattr(cursor, "limit"):
            limited = cursor.limit(limit)
            cursor = await _maybe_await(limited)
        return await self._cursor_to_list(cursor, limit=limit)

    async def _aggregate(self, collection, args: list[Any], kwargs: dict[str, Any]) -> list[Any]:
        pipeline = args[0] if args else []
        if not isinstance(pipeline, list):
            raise RuntimeError("aggregate requires a pipeline array.")
        safe_pipeline = self._sanitize_pipeline(pipeline)
        try:
            cursor = await self._call_with_timeout(
                lambda: collection.aggregate(safe_pipeline, maxTimeMS=self.query_timeout_ms)
            )
        except TypeError:
            cursor = await self._call_with_timeout(lambda: collection.aggregate(safe_pipeline))
        return await self._cursor_to_list(cursor, limit=self.max_docs)

    async def _count_documents(self, collection, args: list[Any], kwargs: dict[str, Any]) -> int:
        query = args[0] if args else {}
        if not isinstance(query, dict):
            raise RuntimeError("count_documents requires a query object.")
        try:
            result = await self._call_with_timeout(
                lambda: collection.count_documents(query, maxTimeMS=self.query_timeout_ms)
            )
        except TypeError:
            result = await self._call_with_timeout(lambda: collection.count_documents(query))
        return int(result)

    async def _distinct(self, collection, args: list[Any], kwargs: dict[str, Any]) -> list[Any]:
        if not args:
            raise RuntimeError("distinct requires a field name.")
        field = str(args[0] or "").strip()
        if not field:
            raise RuntimeError("distinct requires a non-empty field name.")
        query = args[1] if len(args) > 1 else kwargs.get("query") or {}
        if not isinstance(query, dict):
            raise RuntimeError("distinct query must be an object.")
        try:
            values = await self._call_with_timeout(
                lambda: collection.distinct(field, query, maxTimeMS=self.query_timeout_ms)
            )
        except TypeError:
            values = await self._call_with_timeout(lambda: collection.distinct(field, query))
        if isinstance(values, list):
            return values[: self.max_docs]
        return []

    async def _insert_one(
        self, collection, args: list[Any], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        if not args or not isinstance(args[0], dict):
            raise RuntimeError("insert_one requires a document object.")
        result = await self._call_with_timeout(lambda: collection.insert_one(args[0]))
        return {"inserted_id": getattr(result, "inserted_id", None)}

    async def _insert_many(
        self, collection, args: list[Any], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        docs = args[0] if args else []
        if not isinstance(docs, list) or not all(isinstance(item, dict) for item in docs):
            raise RuntimeError("insert_many requires an array of documents.")
        result = await self._call_with_timeout(
            lambda: collection.insert_many(
                docs[: self.max_docs], ordered=_as_bool(kwargs.get("ordered", True))
            )
        )
        inserted_ids = getattr(result, "inserted_ids", [])
        return {"inserted_ids": inserted_ids if isinstance(inserted_ids, list) else []}

    async def _update_one(
        self, collection, args: list[Any], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        if len(args) < 2 or not isinstance(args[0], dict) or not isinstance(args[1], dict):
            raise RuntimeError("update_one requires filter and update objects.")
        result = await self._call_with_timeout(
            lambda: collection.update_one(args[0], args[1], upsert=_as_bool(kwargs.get("upsert")))
        )
        return {
            "matched_count": int(getattr(result, "matched_count", 0)),
            "modified_count": int(getattr(result, "modified_count", 0)),
            "upserted_id": getattr(result, "upserted_id", None),
        }

    async def _update_many(
        self, collection, args: list[Any], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        if len(args) < 2 or not isinstance(args[0], dict) or not isinstance(args[1], dict):
            raise RuntimeError("update_many requires filter and update objects.")
        result = await self._call_with_timeout(
            lambda: collection.update_many(args[0], args[1], upsert=_as_bool(kwargs.get("upsert")))
        )
        return {
            "matched_count": int(getattr(result, "matched_count", 0)),
            "modified_count": int(getattr(result, "modified_count", 0)),
            "upserted_id": getattr(result, "upserted_id", None),
        }

    async def _delete_one(
        self, collection, args: list[Any], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        if not args or not isinstance(args[0], dict):
            raise RuntimeError("delete_one requires a filter object.")
        result = await self._call_with_timeout(lambda: collection.delete_one(args[0]))
        return {"deleted_count": int(getattr(result, "deleted_count", 0))}

    async def _delete_many(
        self, collection, args: list[Any], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        if not args or not isinstance(args[0], dict):
            raise RuntimeError("delete_many requires a filter object.")
        result = await self._call_with_timeout(lambda: collection.delete_many(args[0]))
        return {"deleted_count": int(getattr(result, "deleted_count", 0))}

    async def _call_with_timeout(self, call: Callable[[], Any]) -> Any:
        try:
            return await asyncio.wait_for(
                _maybe_await(call()),
                timeout=self.query_timeout_ms / 1000,
            )
        except TimeoutError as exc:
            raise RuntimeError("DB operation exceeded timeout.") from exc

    async def _cursor_to_list(self, cursor: Any, *, limit: int) -> list[Any]:
        capped = max(1, min(self.max_docs, int(limit)))
        to_list = getattr(cursor, "to_list", None)
        if callable(to_list):
            try:
                docs = await self._call_with_timeout(lambda: to_list(length=capped))
            except TypeError:
                docs = await self._call_with_timeout(lambda: to_list(capped))
            return docs if isinstance(docs, list) else []
        raise RuntimeError("Database cursor did not support to_list.")

    def _sanitize_pipeline(self, pipeline: list[Any]) -> list[dict[str, Any]]:
        if len(pipeline) > _MAX_PIPELINE_STAGES:
            raise RuntimeError("Aggregation pipeline has too many stages.")
        safe: list[dict[str, Any]] = []
        for stage in pipeline:
            if not isinstance(stage, dict) or len(stage) != 1:
                raise RuntimeError("Each aggregation stage must be a single-key object.")
            operator = next(iter(stage.keys()))
            payload = stage[operator]
            if operator in _BANNED_AGG_STAGES:
                raise RuntimeError(f"Aggregation stage '{operator}' is not allowed.")
            if operator == "$lookup":
                self._validate_lookup_stage(payload)
            safe.append(stage)

        if safe and "$limit" in safe[-1]:
            try:
                safe[-1]["$limit"] = max(1, min(self.max_docs, int(safe[-1]["$limit"])))
                return safe
            except Exception:  # noqa: BLE001
                raise RuntimeError("$limit must be numeric.") from None
        safe.append({"$limit": self.max_docs})
        return safe

    def _validate_lookup_stage(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise RuntimeError("$lookup payload must be an object.")
        from_value = payload.get("from")
        if not isinstance(from_value, str):
            raise RuntimeError("$lookup requires a string 'from' collection.")
        if payload.get("pipeline"):
            raise RuntimeError("$lookup with nested pipeline is not allowed.")
