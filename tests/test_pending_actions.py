from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services import pending_actions


@pytest.mark.asyncio
async def test_arguments_fingerprint_is_deterministic():
    a = {"z": 1, "a": {"k": "v"}}
    b = {"a": {"k": "v"}, "z": 1}
    assert pending_actions._arguments_fingerprint(a) == pending_actions._arguments_fingerprint(b)


@pytest.mark.asyncio
async def test_create_and_get_pending_action(patch_mongo):
    created = await pending_actions.create_pending_action(
        tenant_id="local-dev",
        user_id="u1",
        server="orders",
        tool="delete_order",
        arguments={"id": 42},
        action_type="destructive",
        ttl_seconds=120,
    )
    fetched = await pending_actions.get_action(tenant_id="local-dev", action_id=created["_id"])
    assert fetched is not None
    assert fetched["status"] == pending_actions.PENDING
    assert fetched["tool"] == "delete_order"


@pytest.mark.asyncio
async def test_consume_approved_action_happy_path(patch_mongo):
    created = await pending_actions.create_pending_action(
        tenant_id="local-dev",
        user_id="u1",
        server="orders",
        tool="delete_order",
        arguments={"id": 42},
        action_type="destructive",
        ttl_seconds=120,
    )
    patch_mongo["pending_actions"].docs[0]["status"] = pending_actions.APPROVED

    outcome, doc = await pending_actions.consume_approved_action(
        tenant_id="local-dev",
        action_id=created["_id"],
        user_id="u1",
        server="orders",
        tool="delete_order",
        arguments={"id": 42},
    )
    assert outcome == "ok"
    assert doc is not None
    assert doc["status"] == pending_actions.CONSUMED


@pytest.mark.asyncio
async def test_consume_approved_action_rejects_mismatch(patch_mongo):
    created = await pending_actions.create_pending_action(
        tenant_id="local-dev",
        user_id="u1",
        server="orders",
        tool="delete_order",
        arguments={"id": 42},
        action_type="destructive",
        ttl_seconds=120,
    )
    patch_mongo["pending_actions"].docs[0]["status"] = pending_actions.APPROVED

    outcome, _doc = await pending_actions.consume_approved_action(
        tenant_id="local-dev",
        action_id=created["_id"],
        user_id="u1",
        server="orders",
        tool="delete_order",
        arguments={"id": 99},
    )
    assert outcome == "mismatch"


@pytest.mark.asyncio
async def test_consume_approved_action_expired_or_consumed(patch_mongo):
    created = await pending_actions.create_pending_action(
        tenant_id="local-dev",
        user_id="u1",
        server="orders",
        tool="delete_order",
        arguments={"id": 42},
        action_type="destructive",
        ttl_seconds=120,
    )
    doc = patch_mongo["pending_actions"].docs[0]
    doc["status"] = pending_actions.APPROVED
    doc["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    outcome, _ = await pending_actions.consume_approved_action(
        tenant_id="local-dev",
        action_id=created["_id"],
        user_id="u1",
        server="orders",
        tool="delete_order",
        arguments={"id": 42},
    )
    assert outcome == "expired"

    doc["status"] = pending_actions.CONSUMED
    doc["expires_at"] = datetime.now(UTC) + timedelta(seconds=120)
    outcome, _ = await pending_actions.consume_approved_action(
        tenant_id="local-dev",
        action_id=created["_id"],
        user_id="u1",
        server="orders",
        tool="delete_order",
        arguments={"id": 42},
    )
    assert outcome == "already_consumed"


@pytest.mark.asyncio
async def test_approve_reject_and_self_approval_guard(patch_mongo):
    created = await pending_actions.create_pending_action(
        tenant_id="local-dev",
        user_id="requester",
        server="orders",
        tool="delete_order",
        arguments={"id": 42},
        action_type="destructive",
        ttl_seconds=120,
    )
    outcome, approved = await pending_actions.approve_action(
        tenant_id="local-dev",
        action_id=created["_id"],
        approver_id="approver",
        approver_roles=["admin"],
    )
    assert outcome == "ok"
    assert approved is not None
    assert approved["status"] == pending_actions.APPROVED

    created2 = await pending_actions.create_pending_action(
        tenant_id="local-dev",
        user_id="requester-2",
        server="orders",
        tool="delete_order",
        arguments={"id": 43},
        action_type="destructive",
        ttl_seconds=120,
    )
    outcome, rejected = await pending_actions.reject_action(
        tenant_id="local-dev",
        action_id=created2["_id"],
        approver_id="moderator",
        approver_roles=["admin"],
    )
    assert outcome == "ok"
    assert rejected is not None
    assert rejected["status"] == pending_actions.REJECTED

    created3 = await pending_actions.create_pending_action(
        tenant_id="local-dev",
        user_id="same-user",
        server="orders",
        tool="delete_order",
        arguments={"id": 44},
        action_type="destructive",
        ttl_seconds=120,
    )
    outcome, _ = await pending_actions.approve_action(
        tenant_id="local-dev",
        action_id=created3["_id"],
        approver_id="same-user",
        approver_roles=["admin"],
    )
    assert outcome == "self_approval_forbidden"


@pytest.mark.asyncio
async def test_list_pending_actions_filtered_by_status(patch_mongo):
    now = datetime.now(UTC)
    patch_mongo["pending_actions"].docs.extend(
        [
            {
                "_id": "a1",
                "tenant_id": "local-dev",
                "user_id": "u1",
                "server": "orders",
                "tool": "delete_order",
                "arguments": {},
                "action_type": "destructive",
                "status": pending_actions.PENDING,
                "created_at": now - timedelta(minutes=1),
                "expires_at": now + timedelta(minutes=10),
            },
            {
                "_id": "a2",
                "tenant_id": "local-dev",
                "user_id": "u2",
                "server": "orders",
                "tool": "delete_order",
                "arguments": {},
                "action_type": "destructive",
                "status": pending_actions.APPROVED,
                "created_at": now,
                "expires_at": now + timedelta(minutes=10),
            },
        ]
    )
    pending_only = await pending_actions.list_pending_actions(
        tenant_id="local-dev", status=pending_actions.PENDING
    )
    assert len(pending_only) == 1
    assert pending_only[0]["_id"] == "a1"
