from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from database.mongo import get_tenant_database
from models.admin import (
    AdminSearchRequest,
    CacheMigrateRequest,
    EgressAllowlistUpdateRequest,
    QuotaUpdateRequest,
    SandboxSecretsUpdateRequest,
    ServerPatchRequest,
    ServerUpsertRequest,
    TenantCreateRequest,
    TenantStatusUpdateRequest,
)
from models.registry import ToolDocument


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


@pytest.mark.asyncio
async def test_create_server_upserts_registry_and_mounts(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    mounted: list[dict] = []

    class _Registry:
        async def mount_or_update(self, doc):
            mounted.append(doc)

        async def unmount(self, server_name, tenant_id=None):
            return None

    monkeypatch.setattr(admin, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin, "get_proxy_registry", lambda: _Registry())

    payload = ServerUpsertRequest(
        server="weather",
        transport="streamable_http",
        endpoint="http://weather:8101/mcp",
        metadata={"domain": "weather"},
    )
    req = _Req(roles=[admin.settings.platform_admin_role])
    result = await admin.create_or_update_server(req, payload)
    assert result["server"] == "weather"
    assert result["transport"] == "streamable_http"
    assert result["origin"] == "platform"

    docs = get_tenant_database("local-dev")["routing_registry"].docs
    assert len(docs) == 1
    assert docs[0]["endpoint"] == "http://weather:8101/mcp"
    assert mounted and mounted[0]["server"] == "weather"


@pytest.mark.asyncio
async def test_cross_tenant_server_write_requires_platform_admin(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    monkeypatch.setattr(admin, "provision_tenant", fake_provision)
    payload = ServerUpsertRequest(
        tenant_id="tenant-b",
        server="weather",
        transport="streamable_http",
        endpoint="http://weather:8101/mcp",
    )
    with pytest.raises(HTTPException) as exc:
        await admin.create_or_update_server(_Req(tenant_id="local-dev", roles=["admin"]), payload)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_platform_admin_can_list_other_tenant_servers(patch_mongo):
    import gateway.routers.admin as admin

    get_tenant_database("tenant-b")["routing_registry"].docs.append(
        {
            "_id": "orders",
            "tenant_id": "tenant-b",
            "server": "orders",
            "transport": "streamable_http",
            "endpoint": "http://orders:8102/mcp",
            "enabled": True,
            "metadata": {},
            "tools": [],
        }
    )
    response = await admin.list_servers(
        _Req(tenant_id="local-dev", roles=[admin.settings.platform_admin_role]),
        tenant_id="tenant-b",
    )
    assert response["tenant_id"] == "tenant-b"
    assert response["items"][0]["server"] == "orders"


@pytest.mark.asyncio
async def test_patch_and_delete_server(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    tenant_db = get_tenant_database("local-dev")
    tenant_db["routing_registry"].docs.append(
        {
            "_id": "orders",
            "tenant_id": "local-dev",
            "server": "orders",
            "transport": "streamable_http",
            "endpoint": "http://orders:8102/mcp",
            "enabled": True,
            "metadata": {},
            "tools": [],
        }
    )

    unmounted: list[str] = []

    class _Registry:
        async def mount_or_update(self, doc):
            return None

        async def unmount(self, server_name, tenant_id=None):
            unmounted.append(f"{tenant_id}:{server_name}")

    monkeypatch.setattr(admin, "get_proxy_registry", lambda: _Registry())
    req = _Req(roles=[admin.settings.platform_admin_role])
    patched = await admin.patch_server(
        req,
        "orders",
        ServerPatchRequest(enabled=False),
        tenant_id=None,
    )
    assert patched["enabled"] is False
    assert unmounted == ["local-dev:orders"]

    deleted = await admin.delete_server(req, "orders", tenant_id=None)
    assert deleted["deleted"] is True


@pytest.mark.asyncio
async def test_tenant_admin_cannot_create_stdio_server(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    monkeypatch.setattr(admin, "provision_tenant", fake_provision)
    payload = ServerUpsertRequest(
        server="secure-stdio",
        transport="stdio",
        command="python",
        args=["-m", "servers.weather.server"],
    )
    with pytest.raises(HTTPException) as exc:
        await admin.create_or_update_server(_Req(tenant_id="local-dev", roles=["admin"]), payload)
    assert exc.value.status_code == 422
    assert "Tenant servers may not use stdio" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_tenant_admin_cannot_create_private_http_endpoint(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin
    import services.server_guard as server_guard

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    monkeypatch.setattr(admin, "provision_tenant", fake_provision)
    monkeypatch.setattr(
        server_guard.socket,
        "getaddrinfo",
        lambda *_a, **_k: [(2, 1, 6, "", ("127.0.0.1", 8101))],
    )
    payload = ServerUpsertRequest(
        server="private-weather",
        transport="streamable_http",
        endpoint="http://private.internal/mcp",
    )
    with pytest.raises(HTTPException) as exc:
        await admin.create_or_update_server(_Req(tenant_id="local-dev", roles=["admin"]), payload)
    assert exc.value.status_code == 422
    assert "disallowed address" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_create_and_list_tenant_scoped_view(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    monkeypatch.setattr(admin, "provision_tenant", fake_provision)
    created = await admin.create_tenant(_Req(), TenantCreateRequest(tenant_id="local-dev"))
    assert created.tenant_id == "local-dev"

    visible = await admin.list_tenants(_Req(tenant_id="local-dev"))
    assert all(item.tenant_id == "local-dev" for item in visible)


@pytest.mark.asyncio
async def test_suspend_and_resume_tenant(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    control = patch_mongo._control_db
    await control["tenants"].insert_one(
        {"tenant_id": "tenant-a", "db_name": "db_tenant-a", "status": "active"}
    )

    events: list[dict] = []

    class _Telemetry:
        def log_background(self, **kwargs):
            events.append(kwargs)

    monkeypatch.setattr(admin, "get_telemetry_logger", lambda: _Telemetry())
    platform_admin = _Req(roles=[admin.settings.platform_admin_role])

    suspended = await admin.suspend_tenant(
        platform_admin, "tenant-a", TenantStatusUpdateRequest(reason="payment failure")
    )
    assert suspended.status == "suspended"
    assert suspended.suspended_reason == "payment failure"

    resumed = await admin.resume_tenant(platform_admin, "tenant-a")
    assert resumed.status == "active"
    assert resumed.suspended_reason is None

    statuses = [event["status"] for event in events]
    assert "tenant_suspended" in statuses
    assert "tenant_resumed" in statuses


@pytest.mark.asyncio
async def test_suspend_tenant_requires_platform_admin(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.suspend_tenant(_Req(tenant_id="tenant-a", roles=["admin"]), "tenant-a", None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_suspend_unknown_tenant_returns_404(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    class _Telemetry:
        def log_background(self, **kwargs):
            return None

    monkeypatch.setattr(admin, "get_telemetry_logger", lambda: _Telemetry())
    with pytest.raises(HTTPException) as exc:
        await admin.suspend_tenant(_Req(roles=[admin.settings.platform_admin_role]), "ghost", None)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_tenants_surfaces_status(patch_mongo):
    import gateway.routers.admin as admin

    control = patch_mongo._control_db
    await control["tenants"].insert_one(
        {
            "tenant_id": "tenant-a",
            "db_name": "db",
            "status": "suspended",
            "suspended_reason": "abuse",
        }
    )
    listed = await admin.list_tenants(_Req(roles=[admin.settings.platform_admin_role]))
    by_id = {tenant.tenant_id: tenant for tenant in listed}
    assert by_id["tenant-a"].status == "suspended"
    assert by_id["tenant-a"].suspended_reason == "abuse"


@pytest.mark.asyncio
async def test_put_and_get_sandbox_secrets_redacts_values(patch_mongo):
    import gateway.routers.admin as admin

    req = _Req(tenant_id="local-dev", roles=["admin"])
    response = await admin.put_sandbox_secrets(
        req,
        "local-dev",
        SandboxSecretsUpdateRequest(values={"API_KEY": "secret-token", "EMPTY": ""}),
    )
    assert response.tenant_id == "local-dev"
    assert response.keys == ["API_KEY"]

    stored = get_tenant_database("local-dev")["sandbox_secrets"].docs[0]
    assert stored["values"]["API_KEY"].startswith(("enc::", "qe::"))
    assert "secret-token" not in stored["values"]["API_KEY"]

    listed = await admin.get_sandbox_secrets(req, "local-dev")
    assert listed.keys == ["API_KEY"]


@pytest.mark.asyncio
async def test_sandbox_secrets_cross_tenant_requires_platform_admin(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.get_sandbox_secrets(_Req(tenant_id="tenant-a", roles=["admin"]), "tenant-b")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_list_actions_returns_pending_items(patch_mongo):
    import gateway.routers.admin as admin

    now = datetime.now(UTC)
    get_tenant_database("local-dev")["pending_actions"].docs.extend(
        [
            {
                "_id": "a1",
                "tenant_id": "local-dev",
                "user_id": "requester",
                "server": "orders",
                "tool": "delete_order",
                "arguments": {"id": 1},
                "action_type": "destructive",
                "status": "pending",
                "created_at": now,
                "expires_at": now,
            },
            {
                "_id": "a2",
                "tenant_id": "local-dev",
                "user_id": "requester",
                "server": "orders",
                "tool": "delete_order",
                "arguments": {"id": 2},
                "action_type": "destructive",
                "status": "approved",
                "created_at": now,
                "expires_at": now,
            },
        ]
    )
    response = await admin.list_actions(
        _Req(roles=["admin"]), tenant_id=None, action_status="pending"
    )
    assert response.tenant_id == "local-dev"
    assert len(response.items) == 1
    assert response.items[0].action_id == "a1"


@pytest.mark.asyncio
async def test_approve_action_and_reject_action_are_audited(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    now = datetime.now(UTC)
    get_tenant_database("local-dev")["pending_actions"].docs.extend(
        [
            {
                "_id": "approve-me",
                "tenant_id": "local-dev",
                "user_id": "requester",
                "server": "orders",
                "tool": "delete_order",
                "arguments": {"id": 1},
                "args_fingerprint": "x",
                "action_type": "destructive",
                "status": "pending",
                "created_at": now,
                "expires_at": now.replace(year=now.year + 1),
            },
            {
                "_id": "reject-me",
                "tenant_id": "local-dev",
                "user_id": "requester",
                "server": "orders",
                "tool": "delete_order",
                "arguments": {"id": 2},
                "args_fingerprint": "y",
                "action_type": "destructive",
                "status": "pending",
                "created_at": now,
                "expires_at": now.replace(year=now.year + 1),
            },
        ]
    )
    events: list[dict] = []

    class _Telemetry:
        def log_background(self, **kwargs):
            events.append(kwargs)

    monkeypatch.setattr(admin, "get_telemetry_logger", lambda: _Telemetry())
    approver = _Req(roles=["admin"], user_id="approver")

    approved = await admin.approve_pending_action(approver, "approve-me", tenant_id=None)
    rejected = await admin.reject_pending_action(approver, "reject-me", tenant_id=None)
    assert approved.status == "approved"
    assert rejected.status == "rejected"
    statuses = [event["status"] for event in events]
    assert "action_approved" in statuses
    assert "action_rejected" in statuses


@pytest.mark.asyncio
async def test_approve_action_rejects_self_approval(patch_mongo):
    import gateway.routers.admin as admin

    now = datetime.now(UTC)
    get_tenant_database("local-dev")["pending_actions"].docs.append(
        {
            "_id": "self-action",
            "tenant_id": "local-dev",
            "user_id": "requester",
            "server": "orders",
            "tool": "delete_order",
            "arguments": {"id": 1},
            "args_fingerprint": "x",
            "action_type": "destructive",
            "status": "pending",
            "created_at": now,
            "expires_at": now.replace(year=now.year + 1),
        }
    )
    with pytest.raises(HTTPException) as exc:
        await admin.approve_pending_action(
            _Req(roles=["admin"], user_id="requester"),
            "self-action",
            tenant_id=None,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_actions_endpoints_require_admin_role_and_cross_tenant_guard(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.list_actions(_Req(roles=["user"]), tenant_id=None, action_status="pending")
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        await admin.list_actions(
            _Req(tenant_id="tenant-a", roles=["admin"]),
            tenant_id="tenant-b",
            action_status="pending",
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_tenant_usage_returns_usage_quota_and_remaining(patch_mongo):
    import gateway.routers.admin as admin

    control = patch_mongo._control_db
    control["usage_counters"].docs.append(
        {
            "tenant_id": "local-dev",
            "period": "2026-06",
            "calls": 7,
            "sandbox_ms": 2_500,
        }
    )
    control["tenant_quotas"].docs.append(
        {
            "_id": "local-dev",
            "tenant_id": "local-dev",
            "calls_limit": 10,
            "sandbox_seconds_limit": 5,
        }
    )
    response = await admin.get_tenant_usage(
        _Req(tenant_id="local-dev", roles=["admin"]), "local-dev"
    )
    assert response.tenant_id == "local-dev"
    assert response.usage.calls == 7
    assert response.usage.sandbox_ms == 2_500
    assert response.quota.calls_limit == 10
    assert response.quota.sandbox_seconds_limit == 5
    assert response.remaining.calls_remaining == 3
    assert response.remaining.sandbox_seconds_remaining == 3


@pytest.mark.asyncio
async def test_update_tenant_quota_requires_platform_admin(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.update_tenant_quota(
            _Req(tenant_id="local-dev", roles=["admin"]),
            "local-dev",
            QuotaUpdateRequest(calls_limit=100, sandbox_seconds_limit=200),
        )
    assert exc.value.status_code == 403

    updated = await admin.update_tenant_quota(
        _Req(tenant_id="local-dev", roles=[admin.settings.platform_admin_role]),
        "local-dev",
        QuotaUpdateRequest(calls_limit=100, sandbox_seconds_limit=200),
    )
    assert updated.tenant_id == "local-dev"
    assert updated.calls_limit == 100
    assert updated.sandbox_seconds_limit == 200


@pytest.mark.asyncio
async def test_cache_migrate_defaults_to_caller_tenant(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    class _Migrator:
        async def migrate(self, *, tenant_ids, mode, batch_size):
            return {"tenant_ids": tenant_ids, "mode": mode, "batch_size": batch_size}

    monkeypatch.setattr(admin, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin, "cache_migration_service", _Migrator())
    result = await admin.migrate_cache(_Req(tenant_id="tenant-a"), CacheMigrateRequest())
    assert result["tenant_ids"] == ["tenant-a"]
    assert result["mode"] == "status"


@pytest.mark.asyncio
async def test_cache_migrate_cross_tenant_requires_platform_admin(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    class _Migrator:
        async def migrate(self, *, tenant_ids, mode, batch_size):
            return {"tenant_ids": tenant_ids, "mode": mode, "batch_size": batch_size}

    monkeypatch.setattr(admin, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin, "cache_migration_service", _Migrator())
    with pytest.raises(HTTPException) as exc:
        await admin.migrate_cache(
            _Req(tenant_id="tenant-a"),
            CacheMigrateRequest(tenant_id="tenant-b"),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_whoami_returns_request_identity(patch_mongo):
    import gateway.routers.admin as admin

    req = _Req(tenant_id="tenant-a", roles=["admin", admin.settings.platform_admin_role])
    req.state.user_id = "demo@demo.com"
    req.state.scopes = ["orders", "readonly"]
    response = await admin.who_am_i(req)
    assert response.tenant_id == "tenant-a"
    assert response.user_id == "demo@demo.com"
    assert response.is_platform_admin is True


@pytest.mark.asyncio
async def test_catalog_endpoint_paginates(patch_mongo):
    import gateway.routers.admin as admin

    collection = get_tenant_database("local-dev")["tool_catalog"]
    collection.docs.extend(
        [
            {"server": "weather", "name": "a_tool", "description": "A", "scopes": ["readonly"]},
            {"server": "weather", "name": "b_tool", "description": "B", "scopes": []},
        ]
    )
    response = await admin.list_catalog(_Req(), tenant_id=None, limit=1, offset=0)
    assert response.total == 2
    assert len(response.items) == 1
    assert response.items[0].name == "a_tool"


@pytest.mark.asyncio
async def test_telemetry_endpoint_returns_latest_first(patch_mongo):
    import gateway.routers.admin as admin

    now = datetime.now(UTC)
    collection = get_tenant_database("local-dev")["audit_telemetry"]
    collection.docs.extend(
        [
            {
                "timestamp": now,
                "tenant_id": "local-dev",
                "user_id": "u1",
                "method": "tools/call",
                "status": "ok",
            },
            {
                "timestamp": now.replace(microsecond=0),
                "tenant_id": "local-dev",
                "user_id": "u1",
                "method": "tools/list",
                "status": "ok",
            },
        ]
    )
    response = await admin.list_telemetry(_Req(), tenant_id=None, limit=2)
    assert len(response.items) == 2
    assert response.items[0].method == "tools/call"


@pytest.mark.asyncio
async def test_admin_stats_aggregates_per_tenant(patch_mongo):
    import gateway.routers.admin as admin

    get_tenant_database("local-dev")["routing_registry"].docs.append(
        {"server": "weather", "enabled": True}
    )
    get_tenant_database("local-dev")["tool_catalog"].docs.append(
        {"server": "weather", "name": "tool"}
    )
    get_tenant_database("local-dev")["audit_telemetry"].docs.append({"status": "cache_hit"})
    response = await admin.admin_stats(_Req())
    assert response.catalog_version >= 0
    assert response.tenants[0].tenant_id == "local-dev"
    assert response.tenants[0].server_count == 1
    assert response.telemetry_status_counts["cache_hit"] == 1


@pytest.mark.asyncio
async def test_admin_search_delegates_to_hybrid_service(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    class _Search:
        async def search_tools(
            self,
            *,
            tenant_id,
            query,
            limit,
            vector_weight=None,
            text_weight=None,
            mode="hybrid",
            allowed_scopes=None,
        ):
            return [{"tenant_id": tenant_id, "name": query, "mode": mode, "limit": limit}]

    monkeypatch.setattr(admin, "hybrid_search_service", _Search())
    payload = AdminSearchRequest(query="weather", mode="hybrid", limit=3)
    response = await admin.admin_search(_Req(), payload)
    assert response["tenant_id"] == "local-dev"
    assert response["items"][0]["name"] == "weather"


# --------------------------------------------------------------------------- #
# Egress allowlist (per-tenant) admin endpoints
# --------------------------------------------------------------------------- #
def _addrinfo_for(ip: str):
    return [(2, 1, 6, "", (ip, 443))]


@pytest.mark.asyncio
async def test_get_egress_allowlist_defaults_empty_with_global(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    monkeypatch.setattr(admin.settings, "egress_global_allowlist", "*.corp.example", raising=False)
    response = await admin.get_egress_allowlist(_Req(roles=["admin"]), "local-dev")
    assert response.tenant_id == "local-dev"
    assert response.allowlist == []
    assert response.global_allowlist == ["*.corp.example"]
    assert response.enforced is True


@pytest.mark.asyncio
async def test_put_and_get_egress_allowlist_round_trip(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    control = patch_mongo._control_db
    await control["tenants"].insert_one(
        {"tenant_id": "local-dev", "db_name": "db", "status": "active"}
    )

    class _Telemetry:
        def log_background(self, **kwargs):
            return None

    monkeypatch.setattr(admin, "get_telemetry_logger", lambda: _Telemetry())

    updated = await admin.put_egress_allowlist(
        _Req(roles=["admin"], user_id="ops@example.com"),
        "local-dev",
        EgressAllowlistUpdateRequest(
            allowlist=["API.Vendor.com", "*.corp.example", "203.0.113.0/24"]
        ),
    )
    assert updated.allowlist == ["api.vendor.com", "*.corp.example", "203.0.113.0/24"]
    assert updated.updated_by == "ops@example.com"

    fetched = await admin.get_egress_allowlist(_Req(roles=["admin"]), "local-dev")
    assert fetched.allowlist == ["api.vendor.com", "*.corp.example", "203.0.113.0/24"]


@pytest.mark.asyncio
async def test_put_egress_allowlist_rejects_invalid_entry(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "local-dev", "db_name": "db"})
    monkeypatch.setattr(
        admin,
        "get_telemetry_logger",
        lambda: type("T", (), {"log_background": lambda *a, **k: None})(),
    )

    with pytest.raises(HTTPException) as exc:
        await admin.put_egress_allowlist(
            _Req(roles=["admin"]),
            "local-dev",
            EgressAllowlistUpdateRequest(allowlist=["bad host with spaces"]),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_put_egress_allowlist_unknown_tenant_404(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    monkeypatch.setattr(
        admin,
        "get_telemetry_logger",
        lambda: type("T", (), {"log_background": lambda *a, **k: None})(),
    )
    with pytest.raises(HTTPException) as exc:
        await admin.put_egress_allowlist(
            _Req(roles=[admin.settings.platform_admin_role]),
            "ghost",
            EgressAllowlistUpdateRequest(allowlist=["api.vendor.com"]),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_egress_allowlist_cross_tenant_requires_platform_admin(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.get_egress_allowlist(_Req(tenant_id="tenant-a", roles=["admin"]), "tenant-b")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_register_server_blocked_by_global_allowlist(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin
    import services.egress_policy as egress_policy

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    monkeypatch.setattr(admin, "provision_tenant", fake_provision)
    monkeypatch.setattr(
        admin.settings, "egress_global_allowlist", "*.allowed.example", raising=False
    )
    monkeypatch.setattr(
        egress_policy.socket, "getaddrinfo", lambda *_a, **_k: _addrinfo_for("93.184.216.34")
    )

    payload = ServerUpsertRequest(
        server="vendor",
        transport="streamable_http",
        endpoint="https://evil.example/mcp",
    )
    with pytest.raises(HTTPException) as exc:
        await admin.create_or_update_server(
            _Req(roles=[admin.settings.platform_admin_role]), payload
        )
    assert exc.value.status_code == 422
    assert "egress allowlist" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_register_server_allowed_by_global_allowlist(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin
    import services.egress_policy as egress_policy

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    class _Registry:
        async def mount_or_update(self, doc):
            return None

        async def unmount(self, server_name, tenant_id=None):
            return None

    monkeypatch.setattr(admin, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin, "get_proxy_registry", lambda: _Registry())
    monkeypatch.setattr(
        admin.settings, "egress_global_allowlist", "*.allowed.example", raising=False
    )
    monkeypatch.setattr(
        egress_policy.socket, "getaddrinfo", lambda *_a, **_k: _addrinfo_for("93.184.216.34")
    )

    payload = ServerUpsertRequest(
        server="vendor",
        transport="streamable_http",
        endpoint="https://api.allowed.example/mcp",
    )
    result = await admin.create_or_update_server(
        _Req(roles=[admin.settings.platform_admin_role]), payload
    )
    assert result["server"] == "vendor"
    assert result["endpoint"] == "https://api.allowed.example/mcp"


@pytest.mark.asyncio
async def test_register_server_blocked_by_tenant_allowlist(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin
    import services.egress_policy as egress_policy

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    control = patch_mongo._control_db
    await control["tenants"].insert_one(
        {"tenant_id": "local-dev", "db_name": "db", "egress_allowlist": ["api.vendor.com"]}
    )

    monkeypatch.setattr(admin, "provision_tenant", fake_provision)
    monkeypatch.setattr(
        egress_policy.socket, "getaddrinfo", lambda *_a, **_k: _addrinfo_for("93.184.216.34")
    )

    payload = ServerUpsertRequest(
        server="vendor",
        transport="streamable_http",
        endpoint="https://other.vendor.com/mcp",
    )
    with pytest.raises(HTTPException) as exc:
        await admin.create_or_update_server(
            _Req(roles=[admin.settings.platform_admin_role]), payload
        )
    assert exc.value.status_code == 422
    assert "tenant egress allowlist" in str(exc.value.detail)


# --------------------------------------------------------------------------- #
# Code-backed tools (transport="code") — Phase 2: storage only
# --------------------------------------------------------------------------- #
_CODE_SRC = "def add(a: int, b: int) -> int:\n    return a + b\n"


def _code_tool(**overrides):
    base = {
        "server": "my-funcs",
        "name": "add",
        "description": "Add two numbers",
        "raw_code": _CODE_SRC,
        "requirements": ["httpx==0.27.0"],
        "metadata": {"action_type": "read", "requires_confirmation": False},
    }
    base.update(overrides)
    return ToolDocument(**base)


def _patch_code_admin(monkeypatch, admin, mounted=None):
    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    class _Registry:
        async def mount_or_update(self, doc):
            if mounted is not None:
                mounted.append(doc)

        async def unmount(self, server_name, tenant_id=None):
            return None

    monkeypatch.setattr(admin, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin, "get_proxy_registry", lambda: _Registry())


@pytest.mark.asyncio
async def test_tenant_can_author_code_tool_encrypted_at_rest(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    mounted: list[dict] = []
    _patch_code_admin(monkeypatch, admin, mounted)

    payload = ServerUpsertRequest(server="my-funcs", transport="code", tools=[_code_tool()])
    result = await admin.create_or_update_server(_Req(roles=["admin"]), payload)

    assert result["transport"] == "code"
    assert result["origin"] == "tenant"
    # The public/list view redacts authored source, exposing only a presence flag.
    assert result["tools"][0]["has_raw_code"] is True
    assert "raw_code" not in result["tools"][0]

    stored = get_tenant_database("local-dev")["routing_registry"].docs[0]
    stored_code = stored["tools"][0]["raw_code"]
    assert stored_code.startswith(("enc::", "qe::"))
    assert _CODE_SRC not in stored_code  # ciphertext, never plaintext
    # Code servers carry no downstream connection target.
    assert stored["endpoint"] is None and stored["command"] is None
    assert mounted and mounted[0]["transport"] == "code"


@pytest.mark.asyncio
async def test_get_code_server_decrypts_source_for_editor(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    _patch_code_admin(monkeypatch, admin)
    await admin.create_or_update_server(
        _Req(roles=["admin"]),
        ServerUpsertRequest(server="my-funcs", transport="code", tools=[_code_tool()]),
    )

    detail = await admin.get_server(_Req(roles=["admin"]), "my-funcs", tenant_id=None)
    assert detail["tools"][0]["raw_code"] == _CODE_SRC
    assert detail["tools"][0]["requirements"] == ["httpx==0.27.0"]
    assert detail["tools"][0]["metadata"]["action_type"] == "read"


@pytest.mark.asyncio
async def test_code_tool_lint_rejects_unsafe_source(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    _patch_code_admin(monkeypatch, admin)
    unsafe = _code_tool(raw_code="import os\n\ndef add(a, b):\n    return os.getcwd()\n")
    payload = ServerUpsertRequest(server="my-funcs", transport="code", tools=[unsafe])

    with pytest.raises(HTTPException) as exc:
        await admin.create_or_update_server(_Req(roles=["admin"]), payload)
    assert exc.value.status_code == 422
    assert "not allowed" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_code_tool_requires_at_least_one_function(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    _patch_code_admin(monkeypatch, admin)
    payload = ServerUpsertRequest(server="my-funcs", transport="code", tools=[])
    with pytest.raises(HTTPException) as exc:
        await admin.create_or_update_server(_Req(roles=["admin"]), payload)
    assert exc.value.status_code == 422
    assert "at least one authored function" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_patch_code_tool_does_not_double_encrypt(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    _patch_code_admin(monkeypatch, admin)
    await admin.create_or_update_server(
        _Req(roles=["admin"]),
        ServerUpsertRequest(server="my-funcs", transport="code", tools=[_code_tool()]),
    )
    stored_before = get_tenant_database("local-dev")["routing_registry"].docs[0]["tools"][0][
        "raw_code"
    ]

    # Patch an unrelated field; the existing (encrypted) tools round-trip unchanged.
    await admin.patch_server(
        _Req(roles=["admin"]),
        "my-funcs",
        ServerPatchRequest(metadata={"domain": "math"}),
        tenant_id=None,
    )
    stored_after = get_tenant_database("local-dev")["routing_registry"].docs[0]["tools"][0][
        "raw_code"
    ]
    assert stored_after == stored_before
    # Still decryptable to the original plaintext (i.e. not re-encrypted twice).
    detail = await admin.get_server(_Req(roles=["admin"]), "my-funcs", tenant_id=None)
    assert detail["tools"][0]["raw_code"] == _CODE_SRC
