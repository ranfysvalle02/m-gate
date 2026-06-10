from __future__ import annotations

import pytest

from services import users as users_service


@pytest.mark.asyncio
async def test_create_normalizes_email_and_hides_hash(patch_mongo):
    user = await users_service.create_user(
        email="A@Example.com", password="pw123456", tenant_id="t1", roles=["admin"]
    )
    assert user["email"] == "a@example.com"
    assert user["tenant_id"] == "t1"
    assert "password_hash" not in user
    # The stored document keeps a hash, never the plaintext.
    raw = await users_service.get_user_raw(user["id"])
    assert raw is not None
    assert raw["password_hash"] != "pw123456"


@pytest.mark.asyncio
async def test_authenticate_checks_password(patch_mongo):
    user = await users_service.create_user(
        email="a@x.com", password="pw123456", tenant_id="t1", roles=["user"]
    )
    authed = await users_service.authenticate("a@x.com", "pw123456")
    assert authed is not None
    assert authed["id"] == user["id"]
    assert await users_service.authenticate("a@x.com", "bad") is None
    assert await users_service.authenticate("missing@x.com", "pw123456") is None


@pytest.mark.asyncio
async def test_duplicate_email_is_rejected_case_insensitively(patch_mongo):
    await users_service.create_user(
        email="dup@x.com", password="pw123456", tenant_id="t1", roles=["user"]
    )
    with pytest.raises(users_service.UserAlreadyExists):
        await users_service.create_user(
            email="DUP@x.com", password="other", tenant_id="t2", roles=["user"]
        )


@pytest.mark.asyncio
async def test_disabled_user_cannot_authenticate(patch_mongo):
    await users_service.create_user(
        email="d@x.com",
        password="pw123456",
        tenant_id="t1",
        roles=["user"],
        status="disabled",
    )
    assert await users_service.authenticate("d@x.com", "pw123456") is None


@pytest.mark.asyncio
async def test_update_rotates_password_and_roles(patch_mongo):
    user = await users_service.create_user(
        email="u@x.com", password="oldpw", tenant_id="t1", roles=["user"]
    )
    updated = await users_service.update_user(
        user["id"], password="newpw123", roles=["admin"], scopes=["orders"]
    )
    assert updated["roles"] == ["admin"]
    assert updated["scopes"] == ["orders"]
    assert await users_service.authenticate("u@x.com", "newpw123") is not None
    assert await users_service.authenticate("u@x.com", "oldpw") is None


@pytest.mark.asyncio
async def test_update_unknown_user_raises(patch_mongo):
    with pytest.raises(users_service.UserNotFound):
        await users_service.update_user("does-not-exist", status="disabled")


@pytest.mark.asyncio
async def test_list_users_filters_by_tenant(patch_mongo):
    await users_service.create_user(
        email="a@x.com", password="pw123456", tenant_id="t1", roles=["user"]
    )
    await users_service.create_user(
        email="b@x.com", password="pw123456", tenant_id="t2", roles=["user"]
    )
    only_t1 = await users_service.list_users(tenant_id="t1")
    assert [u["email"] for u in only_t1] == ["a@x.com"]
    assert len(await users_service.list_users()) == 2


@pytest.mark.asyncio
async def test_sync_and_clear_session_context_on_delete(patch_mongo):
    control = patch_mongo._control_db
    user = await users_service.create_user(
        email="g@x.com",
        password="pw123456",
        tenant_id="t1",
        roles=["admin"],
        scopes=["orders"],
    )
    await users_service.sync_session_context(user)
    ctx = control["session_context"].docs
    matched = [d for d in ctx if d.get("user_id") == "g@x.com"]
    assert matched and matched[0]["roles"] == ["admin"]
    assert matched[0]["scopes"] == ["orders"]
    # Status is mirrored so the /rpc RBAC gate can revoke a disabled user.
    assert matched[0]["status"] == "active"

    assert await users_service.delete_user(user["id"]) is True
    assert await users_service.get_user_raw(user["id"]) is None
    assert not [d for d in control["session_context"].docs if d.get("user_id") == "g@x.com"]


@pytest.mark.asyncio
async def test_sync_mirrors_disabled_status(patch_mongo):
    control = patch_mongo._control_db
    user = await users_service.create_user(
        email="d@x.com",
        password="pw123456",
        tenant_id="t1",
        roles=["user"],
    )
    await users_service.sync_session_context(user)
    disabled = await users_service.update_user(user["id"], status="disabled")
    await users_service.sync_session_context(disabled)
    matched = [d for d in control["session_context"].docs if d.get("user_id") == "d@x.com"]
    assert matched and matched[0]["status"] == "disabled"


@pytest.mark.asyncio
async def test_delete_missing_user_returns_false(patch_mongo):
    assert await users_service.delete_user("nope") is False
