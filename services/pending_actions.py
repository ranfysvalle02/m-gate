from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pymongo import ReturnDocument

from database.mongo import get_tenant_database

PENDING_ACTIONS_COLLECTION = "pending_actions"

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
CONSUMED = "consumed"

ConsumeOutcome = Literal[
    "ok", "not_found", "not_approved", "mismatch", "expired", "already_consumed"
]
DecisionOutcome = Literal["ok", "not_found", "not_pending", "self_approval_forbidden", "expired"]


def _collection(tenant_id: str):
    return get_tenant_database(tenant_id)[PENDING_ACTIONS_COLLECTION]


def _arguments_fingerprint(arguments: dict[str, Any]) -> str:
    payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def create_pending_action(
    *,
    tenant_id: str,
    user_id: str,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    action_type: str | None,
    ttl_seconds: int,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    doc: dict[str, Any] = {
        "_id": uuid.uuid4().hex,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "server": server,
        "tool": tool,
        "arguments": arguments,
        "args_fingerprint": _arguments_fingerprint(arguments),
        "action_type": action_type or "destructive",
        "status": PENDING,
        "created_at": now,
        "expires_at": now + timedelta(seconds=max(1, ttl_seconds)),
    }
    await _collection(tenant_id).insert_one(doc)
    return doc


async def consume_approved_action(
    *,
    tenant_id: str,
    action_id: str,
    user_id: str,
    server: str,
    tool: str,
    arguments: dict[str, Any],
) -> tuple[ConsumeOutcome, dict[str, Any] | None]:
    now = datetime.now(UTC)
    fingerprint = _arguments_fingerprint(arguments)
    doc = await _collection(tenant_id).find_one_and_update(
        {
            "_id": action_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "server": server,
            "tool": tool,
            "args_fingerprint": fingerprint,
            "status": APPROVED,
            "expires_at": {"$gt": now},
        },
        {"$set": {"status": CONSUMED, "consumed_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if doc is not None:
        return "ok", doc

    raw = await _collection(tenant_id).find_one({"_id": action_id, "tenant_id": tenant_id})
    if raw is None:
        return "not_found", None
    expires_at = raw.get("expires_at")
    if isinstance(expires_at, datetime) and expires_at <= now:
        return "expired", raw
    status = str(raw.get("status", ""))
    if status == CONSUMED:
        return "already_consumed", raw
    if status != APPROVED:
        return "not_approved", raw
    if (
        str(raw.get("user_id", "")) != user_id
        or str(raw.get("server", "")) != server
        or str(raw.get("tool", "")) != tool
        or str(raw.get("args_fingerprint", "")) != fingerprint
    ):
        return "mismatch", raw
    return "not_approved", raw


async def approve_action(
    *,
    tenant_id: str,
    action_id: str,
    approver_id: str,
    approver_roles: list[str],
) -> tuple[DecisionOutcome, dict[str, Any] | None]:
    return await _decide_action(
        tenant_id=tenant_id,
        action_id=action_id,
        approver_id=approver_id,
        approver_roles=approver_roles,
        new_status=APPROVED,
    )


async def reject_action(
    *,
    tenant_id: str,
    action_id: str,
    approver_id: str,
    approver_roles: list[str],
) -> tuple[DecisionOutcome, dict[str, Any] | None]:
    return await _decide_action(
        tenant_id=tenant_id,
        action_id=action_id,
        approver_id=approver_id,
        approver_roles=approver_roles,
        new_status=REJECTED,
    )


async def _decide_action(
    *,
    tenant_id: str,
    action_id: str,
    approver_id: str,
    approver_roles: list[str],
    new_status: str,
) -> tuple[DecisionOutcome, dict[str, Any] | None]:
    del approver_roles  # policy checked in router; reserved for future role-aware decisions.

    now = datetime.now(UTC)
    doc = await _collection(tenant_id).find_one_and_update(
        {
            "_id": action_id,
            "tenant_id": tenant_id,
            "status": PENDING,
            "expires_at": {"$gt": now},
            "user_id": {"$ne": approver_id},
        },
        {"$set": {"status": new_status, "decided_at": now, "decided_by": approver_id}},
        return_document=ReturnDocument.AFTER,
    )
    if doc is not None:
        return "ok", doc

    raw = await _collection(tenant_id).find_one({"_id": action_id, "tenant_id": tenant_id})
    if raw is None:
        return "not_found", None
    if str(raw.get("user_id", "")) == approver_id:
        return "self_approval_forbidden", raw
    expires_at = raw.get("expires_at")
    if isinstance(expires_at, datetime) and expires_at <= now:
        return "expired", raw
    if str(raw.get("status", "")) != PENDING:
        return "not_pending", raw
    return "not_pending", raw


async def get_action(*, tenant_id: str, action_id: str) -> dict[str, Any] | None:
    return await _collection(tenant_id).find_one({"_id": action_id, "tenant_id": tenant_id})


async def list_pending_actions(
    *,
    tenant_id: str,
    status: str = PENDING,
    limit: int = 500,
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"tenant_id": tenant_id}
    if status:
        query["status"] = status
    docs = await _collection(tenant_id).find(query).to_list(length=max(1, limit))
    docs.sort(
        key=lambda doc: doc.get("created_at") or datetime.min.replace(tzinfo=UTC), reverse=True
    )
    return docs
