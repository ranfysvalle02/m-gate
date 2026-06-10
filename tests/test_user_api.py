from __future__ import annotations

import pytest
from fastapi import HTTPException

from models.admin import PasswordChangeRequest, UserCreateRequest, UserUpdateRequest
from services import users as users_service


class _State:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Req:
    def __init__(
        self,
        *,
        tenant_id: str = "local-dev",
        roles: list[str] | None = None,
        user_id: str = "",
        headers=None,
    ):
        self.state = _State(tenant_id=tenant_id, roles=roles or [], user_id=user_id)
        self.headers = headers or {}


def _platform_admin(**kwargs):
    import gateway.routers.admin as admin

    return _Req(roles=[admin.settings.platform_admin_role], **kwargs)


@pytest.mark.asyncio
async def test_platform_admin_creates_tenant_admin_in_any_tenant(patch_mongo):
    import gateway.routers.admin as admin

    payload = UserCreateRequest(
        email="ta@b.com", password="pw123456", tenant_id="tenant-b", roles=["admin"]
    )
    user = await admin.create_user(_platform_admin(user_id="root"), payload)
    assert user.tenant_id == "tenant-b"
    assert user.roles == ["admin"]
    assert user.email == "ta@b.com"
    # The roles are mirrored into session_context for the /rpc path.
    ctx = patch_mongo._control_db["session_context"].docs
    assert any(d.get("user_id") == "ta@b.com" for d in ctx)


@pytest.mark.asyncio
async def test_tenant_admin_can_create_in_own_tenant_only(patch_mongo):
    import gateway.routers.admin as admin

    req = _Req(tenant_id="t1", roles=["admin"], user_id="ta@t1")
    created = await admin.create_user(
        req, UserCreateRequest(email="m@t1", password="pw123456", roles=["user"])
    )
    assert created.tenant_id == "t1"

    with pytest.raises(HTTPException) as exc:
        await admin.create_user(
            req,
            UserCreateRequest(email="x@t2", password="pw123456", tenant_id="t2", roles=["user"]),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_tenant_admin_cannot_grant_platform_admin(patch_mongo):
    import gateway.routers.admin as admin

    req = _Req(tenant_id="t1", roles=["admin"], user_id="ta@t1")
    with pytest.raises(HTTPException) as exc:
        await admin.create_user(
            req,
            UserCreateRequest(email="p@t1", password="pw123456", roles=["platform-admin"]),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_tenant_admin_cannot_manage_platform_admin_user(patch_mongo):
    import gateway.routers.admin as admin

    padmin = await users_service.create_user(
        email="root@t1",
        password="pw123456",
        tenant_id="t1",
        roles=["platform-admin", "admin"],
    )
    req = _Req(tenant_id="t1", roles=["admin"], user_id="ta@t1")
    with pytest.raises(HTTPException) as exc:
        await admin.get_user(req, padmin["id"])
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc2:
        await admin.update_user(req, padmin["id"], UserUpdateRequest(status="disabled"))
    assert exc2.value.status_code == 403


@pytest.mark.asyncio
async def test_cross_tenant_read_denied_but_platform_admin_allowed(patch_mongo):
    import gateway.routers.admin as admin

    user = await users_service.create_user(
        email="z@t2", password="pw123456", tenant_id="t2", roles=["user"]
    )
    with pytest.raises(HTTPException) as exc:
        await admin.get_user(_Req(tenant_id="t1", roles=["admin"]), user["id"])
    assert exc.value.status_code == 403
    got = await admin.get_user(_platform_admin(), user["id"])
    assert got.email == "z@t2"


@pytest.mark.asyncio
async def test_list_users_scoping(patch_mongo):
    import gateway.routers.admin as admin

    await users_service.create_user(
        email="a@t1", password="pw123456", tenant_id="t1", roles=["user"]
    )
    await users_service.create_user(
        email="b@t2", password="pw123456", tenant_id="t2", roles=["user"]
    )
    everyone = await admin.list_users(_platform_admin(), tenant_id=None)
    assert everyone.tenant_id is None
    assert len(everyone.items) == 2

    scoped = await admin.list_users(_Req(tenant_id="t1", roles=["admin"]), tenant_id=None)
    assert scoped.tenant_id == "t1"
    assert [u.email for u in scoped.items] == ["a@t1"]


@pytest.mark.asyncio
async def test_duplicate_email_returns_conflict(patch_mongo):
    import gateway.routers.admin as admin

    req = _platform_admin()
    await admin.create_user(req, UserCreateRequest(email="dup@x.com", password="pw123456"))
    with pytest.raises(HTTPException) as exc:
        await admin.create_user(req, UserCreateRequest(email="dup@x.com", password="pw123456"))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_self_delete_is_blocked(patch_mongo):
    import gateway.routers.admin as admin

    user = await users_service.create_user(
        email="me@t1", password="pw123456", tenant_id="t1", roles=["admin"]
    )
    req = _platform_admin(tenant_id="t1", user_id="me@t1")
    with pytest.raises(HTTPException) as exc:
        await admin.delete_user(req, user["id"])
    assert exc.value.status_code == 400
    # A different admin can delete it.
    assert (await admin.delete_user(_platform_admin(user_id="other"), user["id"]))["deleted"]


@pytest.mark.asyncio
async def test_change_my_password_flow(patch_mongo):
    import gateway.routers.admin as admin

    await users_service.create_user(
        email="c@t1", password="oldpw123", tenant_id="t1", roles=["admin"]
    )
    req = _Req(tenant_id="t1", roles=["admin"], user_id="c@t1")
    result = await admin.change_my_password(
        req, PasswordChangeRequest(current_password="oldpw123", new_password="newpw123")
    )
    assert result["updated"] is True
    assert await users_service.authenticate("c@t1", "newpw123") is not None

    with pytest.raises(HTTPException) as exc:
        await admin.change_my_password(
            req, PasswordChangeRequest(current_password="wrong", new_password="whatever")
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_change_my_password_unavailable_for_bootstrap_admin(patch_mongo):
    import gateway.routers.admin as admin

    req = _platform_admin(user_id="env-admin@nowhere")
    with pytest.raises(HTTPException) as exc:
        await admin.change_my_password(
            req, PasswordChangeRequest(current_password="x", new_password="y")
        )
    assert exc.value.status_code == 400
