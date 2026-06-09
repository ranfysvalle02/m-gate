from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from database.mongo import get_tenant_database
from models.admin import (
    AdminSearchRequest,
    CacheMigrateRequest,
    ServerPatchRequest,
    ServerUpsertRequest,
    TenantCreateRequest,
)


class _State:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Req:
    def __init__(
        self, *, tenant_id: str = "local-dev", roles: list[str] | None = None, headers=None
    ):
        self.state = _State(tenant_id=tenant_id, roles=roles or [])
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
    result = await admin.create_or_update_server(_Req(), payload)
    assert result["server"] == "weather"
    assert result["transport"] == "streamable_http"

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
    patched = await admin.patch_server(
        _Req(),
        "orders",
        ServerPatchRequest(enabled=False),
        tenant_id=None,
    )
    assert patched["enabled"] is False
    assert unmounted == ["local-dev:orders"]

    deleted = await admin.delete_server(_Req(), "orders", tenant_id=None)
    assert deleted["deleted"] is True


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
