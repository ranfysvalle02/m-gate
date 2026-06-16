"""Admin control-plane tests for read-only tenants, tool policy, and enable/disable."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from database.mongo import get_control_database, get_tenant_database
from models.admin import (
    DemoUserCreateRequest,
    ServerUpsertRequest,
    TenantStatusUpdateRequest,
    ToolPolicyUpdateRequest,
    UserCreateRequest,
    UserUpdateRequest,
)


class _State:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Req:
    def __init__(
        self,
        *,
        tenant_id: str = "local-dev",
        roles: list[str] | None = None,
        user_id: str = "admin@example.com",
        headers=None,
    ):
        self.state = _State(tenant_id=tenant_id, roles=roles or [], user_id=user_id)
        self.headers = headers or {}


def _platform_admin(admin) -> _Req:
    return _Req(roles=[admin.settings.platform_admin_role])


def _tenant_admin() -> _Req:
    return _Req(roles=["admin"])


class _Registry:
    def __init__(self):
        self.mounted = []
        self.unmounted = []

    async def mount_or_update(self, doc):
        self.mounted.append(doc)

    async def unmount(self, server_name, tenant_id=None):
        self.unmounted.append(server_name)


async def _seed_tenant(tenant_id="local-dev"):
    await get_control_database()["tenants"].insert_one(
        {"tenant_id": tenant_id, "db_name": f"tenant_{tenant_id}", "status": "active"}
    )


# --------------------------------------------------------------------------- #
#  Read-only toggle                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_read_only_toggle_sets_and_clears_flag(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant()
    ro = await admin.make_tenant_read_only(
        _platform_admin(admin), "local-dev", TenantStatusUpdateRequest(reason="showcase")
    )
    assert ro.read_only is True
    assert ro.read_only_reason == "showcase"

    rw = await admin.make_tenant_read_write(_platform_admin(admin), "local-dev")
    assert rw.read_only is False
    assert rw.read_only_reason is None


@pytest.mark.asyncio
async def test_read_only_toggle_requires_platform_admin(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant()
    with pytest.raises(HTTPException) as exc:
        await admin.make_tenant_read_only(_tenant_admin(), "local-dev", None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_read_only_toggle_missing_tenant_404(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.make_tenant_read_only(_platform_admin(admin), "ghost", None)
    assert exc.value.status_code == 404


# --------------------------------------------------------------------------- #
#  Tool policy GET/PUT                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tool_policy_get_lists_available_tools(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant()
    catalog = get_tenant_database("local-dev")["tool_catalog"]
    await catalog.insert_one({"server": "orders", "name": "find_order", "description": "find"})
    await catalog.insert_one({"server": "orders", "name": "delete_order", "description": "del"})

    policy = await admin.get_tenant_tool_policy(_platform_admin(admin), "local-dev")
    assert policy.tenant_id == "local-dev"
    assert len(policy.available_tools) == 2
    assert all(t.allowlisted for t in policy.available_tools)  # empty allowlist == all


@pytest.mark.asyncio
async def test_tool_policy_put_saves_allowlist_and_cap(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant()
    catalog = get_tenant_database("local-dev")["tool_catalog"]
    await catalog.insert_one({"server": "orders", "name": "find_order"})
    await catalog.insert_one({"server": "orders", "name": "delete_order"})

    updated = await admin.put_tenant_tool_policy(
        _platform_admin(admin),
        "local-dev",
        ToolPolicyUpdateRequest(allowlist=["orders/find_order"], max_tools=5),
    )
    assert updated.allowlist == ["orders/find_order"]
    assert updated.max_tools == 5
    by_name = {t.name: t for t in updated.available_tools}
    assert by_name["find_order"].allowlisted is True
    assert by_name["delete_order"].allowlisted is False


@pytest.mark.asyncio
async def test_tool_policy_put_blocked_when_read_only_for_tenant_admin(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant()
    await admin.make_tenant_read_only(_platform_admin(admin), "local-dev", None)

    with pytest.raises(HTTPException) as exc:
        await admin.put_tenant_tool_policy(
            _tenant_admin(),
            "local-dev",
            ToolPolicyUpdateRequest(allowlist=["orders/find_order"], max_tools=0),
        )
    assert exc.value.status_code == 403
    assert "read-only" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_tool_policy_put_allowed_for_platform_admin_when_read_only(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant()
    await admin.make_tenant_read_only(_platform_admin(admin), "local-dev", None)
    # Platform-admin bypasses the writable guard (owns the freeze).
    updated = await admin.put_tenant_tool_policy(
        _platform_admin(admin),
        "local-dev",
        ToolPolicyUpdateRequest(allowlist=[], max_tools=2),
    )
    assert updated.max_tools == 2


# --------------------------------------------------------------------------- #
#  Per-tool enable/disable                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tool_disable_enable_overlay(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant()
    catalog = get_tenant_database("local-dev")["tool_catalog"]
    await catalog.insert_one({"server": "orders", "name": "delete_order"})

    disabled = await admin.disable_tool(_tenant_admin(), "orders", "delete_order", None)
    assert disabled.enabled is False
    doc = await get_control_database()["tenants"].find_one({"tenant_id": "local-dev"})
    assert "orders/delete_order" in (doc.get("disabled_tools") or [])

    enabled = await admin.enable_tool(_tenant_admin(), "orders", "delete_order", None)
    assert enabled.enabled is True


@pytest.mark.asyncio
async def test_tool_disable_unknown_tool_404(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant()
    with pytest.raises(HTTPException) as exc:
        await admin.disable_tool(_tenant_admin(), "orders", "ghost", None)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_tool_disable_blocked_when_read_only(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant()
    catalog = get_tenant_database("local-dev")["tool_catalog"]
    await catalog.insert_one({"server": "orders", "name": "delete_order"})
    await admin.make_tenant_read_only(_platform_admin(admin), "local-dev", None)

    with pytest.raises(HTTPException) as exc:
        await admin.disable_tool(_tenant_admin(), "orders", "delete_order", None)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
#  Server enable/disable                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_server_enable_disable_by_tenant_admin(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    registry = _Registry()
    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: registry)

    await _seed_tenant()
    await get_tenant_database("local-dev")["routing_registry"].insert_one(
        {"_id": "orders", "server": "orders", "origin": "tenant", "enabled": True}
    )

    disabled = await admin.disable_server(_tenant_admin(), "orders", None)
    assert disabled["enabled"] is False
    assert "orders" in registry.unmounted

    enabled = await admin.enable_server(_tenant_admin(), "orders", None)
    assert enabled["enabled"] is True
    assert registry.mounted and registry.mounted[-1]["server"] == "orders"


@pytest.mark.asyncio
async def test_platform_origin_server_toggle_requires_platform_admin(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: _Registry())
    await _seed_tenant()
    await get_tenant_database("local-dev")["routing_registry"].insert_one(
        {"_id": "weather", "server": "weather", "origin": "platform", "enabled": True}
    )

    with pytest.raises(HTTPException) as exc:
        await admin.disable_server(_tenant_admin(), "weather", None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_server_toggle_blocked_when_read_only(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: _Registry())
    await _seed_tenant()
    await get_tenant_database("local-dev")["routing_registry"].insert_one(
        {"_id": "orders", "server": "orders", "origin": "tenant", "enabled": True}
    )
    await admin.make_tenant_read_only(_platform_admin(admin), "local-dev", None)

    with pytest.raises(HTTPException) as exc:
        await admin.disable_server(_tenant_admin(), "orders", None)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
#  Max-tools cap + tenant-writable guard at server registration               #
# --------------------------------------------------------------------------- #


async def _patch_provision_and_registry(admin, monkeypatch):
    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: _Registry())


@pytest.mark.asyncio
async def test_max_tools_cap_rejects_registration(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    await _patch_provision_and_registry(admin, monkeypatch)
    await _seed_tenant()
    # Cap at 1, and one tool already catalogued on a different server.
    await admin.put_tenant_tool_policy(
        _platform_admin(admin), "local-dev", ToolPolicyUpdateRequest(allowlist=[], max_tools=1)
    )
    await get_tenant_database("local-dev")["tool_catalog"].insert_one(
        {"server": "weather", "name": "forecast"}
    )

    payload = ServerUpsertRequest(
        server="utilities",
        transport="code",
        tools=[
            {
                "server": "utilities",
                "name": "json_format",
                "description": "x",
                "input_schema": {"type": "object"},
                "scopes": ["utilities"],
                "raw_code": "def json_format(payload: str) -> dict:\n    return {'payload': payload}\n",
                "requirements": [],
                "metadata": {"action_type": "read"},
            }
        ],
    )
    with pytest.raises(HTTPException) as exc:
        await admin.create_or_update_server(_platform_admin(admin), payload)
    assert exc.value.status_code == 422
    assert "max-tools cap" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_server_create_blocked_when_tenant_read_only(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    await _patch_provision_and_registry(admin, monkeypatch)
    await _seed_tenant()
    await admin.make_tenant_read_only(_platform_admin(admin), "local-dev", None)

    payload = ServerUpsertRequest(
        server="weather",
        transport="streamable_http",
        endpoint="https://weather:8101/mcp",
    )
    # Tenant-admin is blocked by the writable guard.
    with pytest.raises(HTTPException) as exc:
        await admin.create_or_update_server(_tenant_admin(), payload)
    assert exc.value.status_code == 403

    # Platform-admin bypasses and can still register.
    result = await admin.create_or_update_server(_platform_admin(admin), payload)
    assert result["server"] == "weather"


# --------------------------------------------------------------------------- #
#  User management is frozen on a read-only tenant (control-plane parity)      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_user_create_blocked_when_tenant_read_only(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant()
    await admin.make_tenant_read_only(_platform_admin(admin), "local-dev", None)

    payload = UserCreateRequest(email="new@local.dev", password="pw-12345678", roles=["user"])
    # Tenant-admin is frozen out...
    with pytest.raises(HTTPException) as exc:
        await admin.create_user(_tenant_admin(), payload)
    assert exc.value.status_code == 403
    assert "read-only" in str(exc.value.detail).lower()

    # ...but the platform-admin who owns the freeze can still create.
    created = await admin.create_user(_platform_admin(admin), payload)
    assert created.email == "new@local.dev"


@pytest.mark.asyncio
async def test_demo_and_viewer_user_creation_blocked_when_read_only(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant()
    await admin.make_tenant_read_only(_platform_admin(admin), "local-dev", None)

    with pytest.raises(HTTPException) as demo_exc:
        await admin.create_demo_user(_tenant_admin(), DemoUserCreateRequest())
    assert demo_exc.value.status_code == 403

    with pytest.raises(HTTPException) as viewer_exc:
        await admin.create_viewer_user(_tenant_admin(), DemoUserCreateRequest())
    assert viewer_exc.value.status_code == 403


@pytest.mark.asyncio
async def test_user_update_and_delete_blocked_when_tenant_read_only(patch_mongo):
    import gateway.routers.admin as admin

    await _seed_tenant()
    # Create the user while the tenant is still writable.
    created = await admin.create_user(
        _platform_admin(admin),
        UserCreateRequest(email="target@local.dev", password="pw-12345678", roles=["user"]),
    )
    await admin.make_tenant_read_only(_platform_admin(admin), "local-dev", None)

    with pytest.raises(HTTPException) as update_exc:
        await admin.update_user(_tenant_admin(), created.id, UserUpdateRequest(status="disabled"))
    assert update_exc.value.status_code == 403

    with pytest.raises(HTTPException) as delete_exc:
        await admin.delete_user(_tenant_admin(), created.id)
    assert delete_exc.value.status_code == 403

    # Platform-admin bypasses the freeze and can still manage the user.
    updated = await admin.update_user(
        _platform_admin(admin), created.id, UserUpdateRequest(status="disabled")
    )
    assert updated.status == "disabled"
