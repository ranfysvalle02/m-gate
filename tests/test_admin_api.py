from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from database.mongo import get_tenant_database, tenant_db_name
from models.admin import (
    AdminSearchRequest,
    CacheMigrateRequest,
    CodeToolTestRequest,
    EgressAllowlistUpdateRequest,
    ExploreQueryRequest,
    ExploreSampleRequest,
    QuotaUpdateRequest,
    ServerEnvUpdateRequest,
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

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: _Registry())

    payload = ServerUpsertRequest(
        server="weather",
        transport="streamable_http",
        endpoint="https://weather:8101/mcp",
        metadata={"domain": "weather"},
    )
    req = _Req(roles=[admin.settings.platform_admin_role])
    result = await admin.create_or_update_server(req, payload)
    assert result["server"] == "weather"
    assert result["transport"] == "streamable_http"
    assert result["origin"] == "platform"

    docs = get_tenant_database("local-dev")["routing_registry"].docs
    assert len(docs) == 1
    assert docs[0]["endpoint"] == "https://weather:8101/mcp"
    assert mounted and mounted[0]["server"] == "weather"


@pytest.mark.asyncio
async def test_create_server_rejects_unknown_auth_scheme(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    class _Registry:
        async def mount_or_update(self, doc):
            return None

        async def unmount(self, server_name, tenant_id=None):
            return None

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: _Registry())
    payload = ServerUpsertRequest(
        server="weather",
        transport="streamable_http",
        endpoint="https://weather:8101/mcp",
        metadata={"auth": {"scheme": "custom"}},
    )
    with pytest.raises(HTTPException) as exc:
        await admin.create_or_update_server(
            _Req(roles=[admin.settings.platform_admin_role]),
            payload,
        )
    assert exc.value.status_code == 422
    assert "Unsupported metadata.auth.scheme" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_create_server_blocks_insecure_endpoint_for_jwt_credential(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    class _Registry:
        async def mount_or_update(self, doc):
            return None

        async def unmount(self, server_name, tenant_id=None):
            return None

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: _Registry())
    monkeypatch.setattr(admin.settings, "downstream_allow_insecure_credentials", False)
    payload = ServerUpsertRequest(
        server="weather",
        transport="streamable_http",
        endpoint="http://weather:8101/mcp",
        metadata={"auth": {"scheme": "jwt"}},
    )
    with pytest.raises(HTTPException) as exc:
        await admin.create_or_update_server(
            _Req(roles=[admin.settings.platform_admin_role]),
            payload,
        )
    assert exc.value.status_code == 422
    assert "DOWNSTREAM_ALLOW_INSECURE_CREDENTIALS" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_code_server_save_preserves_multiple_tools(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    class _Registry:
        async def mount_or_update(self, doc):
            return None

        async def unmount(self, server_name, tenant_id=None):
            return None

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: _Registry())

    payload = ServerUpsertRequest(
        server="utilities",
        transport="code",
        metadata={"domain": "utilities", "runtime": "wasm"},
        tools=[
            ToolDocument(
                server="utilities",
                name="json_format",
                description="pretty print json",
                input_schema={"type": "object"},
                scopes=["utilities"],
                raw_code="def json_format(payload: str) -> dict:\n    return {'payload': payload}\n",
                requirements=[],
                metadata={"action_type": "read"},
            ),
            ToolDocument(
                server="utilities",
                name="hash_text",
                description="hash text",
                input_schema={"type": "object"},
                scopes=["utilities"],
                raw_code="def hash_text(text: str) -> dict:\n    return {'text': text}\n",
                requirements=[],
                metadata={"action_type": "read"},
            ),
        ],
    )
    await admin.create_or_update_server(_Req(roles=[admin.settings.platform_admin_role]), payload)
    stored = get_tenant_database("local-dev")["routing_registry"].docs[0]
    assert stored["transport"] == "code"
    assert len(stored.get("tools") or []) == 2
    assert {tool.get("name") for tool in stored.get("tools") or []} == {"json_format", "hash_text"}


@pytest.mark.asyncio
async def test_cross_tenant_server_write_requires_platform_admin(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
    payload = ServerUpsertRequest(
        tenant_id="tenant-b",
        server="weather",
        transport="streamable_http",
        endpoint="https://weather:8101/mcp",
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
            "endpoint": "https://orders:8102/mcp",
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

    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: _Registry())
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
async def test_export_server_returns_zip_attachment(patch_mongo, monkeypatch):
    import io
    import zipfile

    import gateway.routers.admin as admin
    from services import server_exporter

    async def _identity(_tenant, stored, *_a, **_k):
        return stored or ""

    monkeypatch.setattr(server_exporter, "decrypt_raw_code", _identity)

    get_tenant_database("local-dev")["routing_registry"].docs.append(
        {
            "_id": "utilities",
            "tenant_id": "local-dev",
            "server": "utilities",
            "transport": "code",
            "enabled": True,
            "metadata": {},
            "tools": [
                {
                    "name": "echo",
                    "description": "echo text",
                    "raw_code": "def echo(text: str) -> dict:\n    return {'text': text}\n",
                    "requirements": [],
                    "metadata": {"action_type": "read"},
                    "input_schema": {},
                    "scopes": [],
                }
            ],
        }
    )

    req = _Req(roles=[admin.settings.platform_admin_role])
    response = await admin.export_server(req, "utilities", tenant_id=None)
    assert response.media_type == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="utilities-mcp.zip"'

    with zipfile.ZipFile(io.BytesIO(response.body)) as zf:
        names = set(zf.namelist())
    assert "utilities-mcp/server.py" in names
    assert "utilities-mcp/tools/utilities/echo.py" in names


@pytest.mark.asyncio
async def test_export_non_code_server_returns_400(patch_mongo):
    import gateway.routers.admin as admin

    get_tenant_database("local-dev")["routing_registry"].docs.append(
        {
            "_id": "weather",
            "tenant_id": "local-dev",
            "server": "weather",
            "transport": "streamable_http",
            "endpoint": "http://weather:8101/mcp",
            "tools": [],
        }
    )
    req = _Req(roles=[admin.settings.platform_admin_role])
    with pytest.raises(HTTPException) as excinfo:
        await admin.export_server(req, "weather", tenant_id=None)
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_export_missing_server_returns_404(patch_mongo):
    import gateway.routers.admin as admin

    req = _Req(roles=[admin.settings.platform_admin_role])
    with pytest.raises(HTTPException) as excinfo:
        await admin.export_server(req, "ghost", tenant_id=None)
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_tenant_admin_cannot_create_stdio_server(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
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
async def test_test_code_tool_endpoint_executes_and_returns_payload(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    class _Result:
        payload = {"ok": True, "echo": "Lisbon"}
        elapsed_ms = 8.4

    class _Executor:
        async def run(self, request):
            assert request.server == "weather"
            assert request.tool == "get_current_weather"
            assert request.arguments["city"] == "Lisbon"
            assert request.action_type == "read"
            return _Result()

    monkeypatch.setattr(admin._common, "get_executor", lambda: _Executor())

    response = await admin.test_code_tool(
        _Req(roles=["admin"]),
        "weather",
        "get_current_weather",
        CodeToolTestRequest(
            raw_code=(
                "def get_current_weather(city: str, unit: str = 'celsius') -> dict:\n"
                "    return {'city': city, 'unit': unit}\n"
            ),
            arguments={"city": "Lisbon"},
            requirements=[],
        ),
    )
    assert response.ok is True
    assert response.result == {"ok": True, "echo": "Lisbon"}


@pytest.mark.asyncio
async def test_test_code_tool_endpoint_returns_lint_error(patch_mongo):
    import gateway.routers.admin as admin

    response = await admin.test_code_tool(
        _Req(roles=["admin"]),
        "weather",
        "get_current_weather",
        CodeToolTestRequest(
            raw_code="import os\ndef get_current_weather(city: str) -> dict:\n    return {'city': city}\n",
            arguments={"city": "Lisbon"},
            requirements=[],
        ),
    )
    assert response.ok is False
    assert "not allowed" in (response.error or "").lower()


@pytest.mark.asyncio
async def test_explore_collections_lists_non_system_collections(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    monkeypatch.setattr(admin.settings, "sandbox_db_bridge_enabled", True)
    tenant_db = get_tenant_database("local-dev")
    tenant_db["users"].docs.append({"_id": "u1"})
    tenant_db["system.profile"].docs.append({"_id": "ignored"})

    response = await admin.explore_collections(_Req(roles=["admin"]))
    assert response.tenant_id == "local-dev"
    assert "users" in response.collections
    assert "system.profile" not in response.collections


@pytest.mark.asyncio
async def test_explore_sample_and_query_return_snippets(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    monkeypatch.setattr(admin.settings, "sandbox_db_bridge_enabled", True)
    tenant_db = get_tenant_database("local-dev")
    tenant_db["users"].docs.extend(
        [{"_id": "u1", "status": "active"}, {"_id": "u2", "status": "inactive"}]
    )

    sample = await admin.explore_sample(
        _Req(roles=["admin"]),
        ExploreSampleRequest(collection="users", limit=1),
    )
    assert sample.collection == "users"
    assert len(sample.sample_docs) == 1
    assert "context.db" in sample.snippet

    queried = await admin.explore_query(
        _Req(roles=["admin"]),
        ExploreQueryRequest(collection="users", mode="find", filter={"status": "active"}, limit=10),
    )
    assert queried.mode == "find"
    assert queried.results == [{"_id": "u1", "status": "active"}]
    assert "context.db" in queried.snippet


@pytest.mark.asyncio
async def test_explore_query_rejects_banned_aggregate_stage(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    monkeypatch.setattr(admin.settings, "sandbox_db_bridge_enabled", True)
    with pytest.raises(HTTPException) as exc:
        await admin.explore_query(
            _Req(roles=["admin"]),
            ExploreQueryRequest(
                collection="users",
                mode="aggregate",
                pipeline=[{"$match": {}}, {"$out": "archive"}],
            ),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_tenant_admin_cannot_create_private_http_endpoint(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin
    import services.server_guard as server_guard

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
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

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
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

    monkeypatch.setattr(admin._common, "get_telemetry_logger", lambda: _Telemetry())
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

    monkeypatch.setattr(admin._common, "get_telemetry_logger", lambda: _Telemetry())
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
async def test_put_and_get_server_env_redacts_values(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    evicted: list[tuple[str, str]] = []
    invalidated: list[tuple[str, str | None]] = []

    class _CredentialBroker:
        async def invalidate(self, server_name, *, tenant_id=None):
            invalidated.append((server_name, tenant_id))

    class _Registry:
        credential_broker = _CredentialBroker()

        async def refresh_server_credentials(self, server_name, *, tenant_id):
            evicted.append((tenant_id, server_name))
            await self.credential_broker.invalidate(server_name, tenant_id=tenant_id)

    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: _Registry())
    req = _Req(tenant_id="local-dev", roles=["admin"])
    get_tenant_database("local-dev")["routing_registry"].docs.append(
        {"_id": "analytics", "server": "analytics", "tenant_id": "local-dev"}
    )
    response = await admin.put_server_env(
        req,
        "analytics",
        ServerEnvUpdateRequest(values={"API_KEY": "secret-token", "EMPTY": ""}),
        tenant_id="local-dev",
    )
    assert response.tenant_id == "local-dev"
    assert response.server == "analytics"
    assert response.keys == ["API_KEY"]

    stored = get_tenant_database("local-dev")["server_secrets"].docs[0]
    assert stored["values"]["API_KEY"].startswith(("enc::", "qe::"))
    assert "secret-token" not in stored["values"]["API_KEY"]

    listed = await admin.get_server_env(req, "analytics", tenant_id="local-dev")
    assert listed.keys == ["API_KEY"]
    assert evicted == [("local-dev", "analytics")]
    assert invalidated == [("analytics", "local-dev")]


@pytest.mark.asyncio
async def test_server_env_cross_tenant_requires_platform_admin(patch_mongo):
    import gateway.routers.admin as admin

    get_tenant_database("tenant-b")["routing_registry"].docs.append(
        {"_id": "analytics", "server": "analytics", "tenant_id": "tenant-b"}
    )
    with pytest.raises(HTTPException) as exc:
        await admin.get_server_env(
            _Req(tenant_id="tenant-a", roles=["admin"]),
            "analytics",
            tenant_id="tenant-b",
        )
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

    monkeypatch.setattr(admin._common, "get_telemetry_logger", lambda: _Telemetry())
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
async def test_get_tenant_usage_events_returns_rollup_and_recent_events(patch_mongo):
    import gateway.routers.admin as admin

    control = patch_mongo._control_db
    now = datetime.now(UTC)
    control["usage_events"].docs.extend(
        [
            {
                "tenant_id": "local-dev",
                "period": "2026-06",
                "kind": "calls",
                "amount": 2,
                "ts": now,
                "metadata": {"source": "live_execution"},
            },
            {
                "tenant_id": "local-dev",
                "period": "2026-06",
                "kind": "sandbox_ms",
                "amount": 500,
                "ts": now.replace(microsecond=0),
                "metadata": {},
            },
            {
                "tenant_id": "local-dev",
                "period": "2026-06",
                "kind": "calls",
                "amount": 1,
                "ts": now.replace(microsecond=0),
                "metadata": {},
            },
        ]
    )
    response = await admin.get_tenant_usage_events(
        _Req(tenant_id="local-dev", roles=["admin"]),
        "local-dev",
        period="2026-06",
        limit=2,
    )
    assert response.tenant_id == "local-dev"
    assert response.totals_by_kind == {"calls": 3, "sandbox_ms": 500}
    assert response.total_amount == 503
    assert len(response.events) == 2


async def _collect_stream(response) -> str:
    parts: list[str] = []
    async for chunk in response.body_iterator:
        parts.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    return "".join(parts)


@pytest.mark.asyncio
async def test_export_tenant_usage_streams_csv(patch_mongo):
    import gateway.routers.admin as admin

    control = patch_mongo._control_db
    base = datetime(2026, 6, 1, tzinfo=UTC)
    control["usage_events"].docs.extend(
        [
            {
                "tenant_id": "local-dev",
                "period": "2026-06",
                "kind": "calls",
                "amount": 2,
                "ts": base,
                "metadata": {"source": "live_execution"},
            },
            {
                "tenant_id": "local-dev",
                "period": "2026-06",
                "kind": "sandbox_ms",
                "amount": 500,
                "ts": base + timedelta(hours=1),
                "metadata": {},
            },
            # A different tenant's event must never leak into this export.
            {
                "tenant_id": "other",
                "period": "2026-06",
                "kind": "calls",
                "amount": 99,
                "ts": base,
                "metadata": {},
            },
        ]
    )

    response = await admin.export_tenant_usage(
        _Req(tenant_id="local-dev", roles=["admin"]),
        "local-dev",
        export_format="csv",
        from_=None,
        to=None,
    )
    assert response.media_type == "text/csv"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"

    body = await _collect_stream(response)
    rows = list(csv.reader(io.StringIO(body)))
    assert rows[0] == ["ts", "tenant_id", "period", "kind", "amount", "source"]
    data_rows = rows[1:]
    assert len(data_rows) == 2
    assert {row[3] for row in data_rows} == {"calls", "sandbox_ms"}
    # Ascending ts order and the metadata source projected onto the row.
    assert data_rows[0][3] == "calls"
    assert data_rows[0][5] == "live_execution"
    assert all(row[1] == "local-dev" for row in data_rows)


@pytest.mark.asyncio
async def test_export_tenant_usage_applies_date_range(patch_mongo):
    import gateway.routers.admin as admin

    control = patch_mongo._control_db
    base = datetime(2026, 6, 1, tzinfo=UTC)
    control["usage_events"].docs.extend(
        [
            {"tenant_id": "local-dev", "period": "p", "kind": "calls", "amount": 1, "ts": base},
            {
                "tenant_id": "local-dev",
                "period": "p",
                "kind": "calls",
                "amount": 1,
                "ts": base + timedelta(days=2),
            },
            {
                "tenant_id": "local-dev",
                "period": "p",
                "kind": "calls",
                "amount": 1,
                "ts": base + timedelta(days=5),
            },
        ]
    )

    response = await admin.export_tenant_usage(
        _Req(tenant_id="local-dev", roles=["admin"]),
        "local-dev",
        export_format="csv",
        from_="2026-06-02",
        to="2026-06-04",
    )
    body = await _collect_stream(response)
    data_rows = list(csv.reader(io.StringIO(body)))[1:]
    # Only the single event inside [06-02, 06-04] survives the range filter.
    assert len(data_rows) == 1


@pytest.mark.asyncio
async def test_export_tenant_usage_requires_admin(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.export_tenant_usage(
            _Req(tenant_id="local-dev", roles=["viewer"]),
            "local-dev",
            export_format="csv",
            from_=None,
            to=None,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_export_tenant_usage_rejects_unknown_format(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.export_tenant_usage(
            _Req(tenant_id="local-dev", roles=["admin"]),
            "local-dev",
            export_format="parquet",
            from_=None,
            to=None,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_export_tenant_usage_rejects_bad_timestamp(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.export_tenant_usage(
            _Req(tenant_id="local-dev", roles=["admin"]),
            "local-dev",
            export_format="csv",
            from_="not-a-date",
            to=None,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_export_telemetry_streams_jsonl(patch_mongo):
    import gateway.routers.admin as admin

    base = datetime(2026, 6, 1, tzinfo=UTC)
    get_tenant_database("local-dev")["audit_telemetry"].docs.extend(
        [
            {
                "timestamp": base,
                "tenant_id": "local-dev",
                "user_id": "u1",
                "request_id": "r1",
                "method": "tools/call",
                "status": "ok",
                "latency_ms": 12.0,
                "metadata": {"server": "s"},
            },
            {
                "timestamp": base + timedelta(minutes=1),
                "tenant_id": "local-dev",
                "user_id": "u2",
                "request_id": "r2",
                "method": "tools/list",
                "status": "ok",
                "latency_ms": 3.0,
                "metadata": {},
            },
        ]
    )

    response = await admin.export_telemetry(
        _Req(tenant_id="local-dev", roles=["admin"]),
        tenant_id="local-dev",
        export_format="jsonl",
        from_=None,
        to=None,
    )
    assert response.media_type == "application/x-ndjson"
    body = await _collect_stream(response)
    lines = [line for line in body.splitlines() if line.strip()]
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert [r["method"] for r in records] == ["tools/call", "tools/list"]
    assert all("_id" not in r for r in records)


@pytest.mark.asyncio
async def test_export_telemetry_rejects_unknown_format(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.export_telemetry(
            _Req(tenant_id="local-dev", roles=["admin"]),
            tenant_id="local-dev",
            export_format="csv",
            from_=None,
            to=None,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_delete_tenant_requires_platform_admin_and_soft_deletes_by_default(
    patch_mongo, monkeypatch
):
    import gateway.routers.admin as admin
    import services.tenant_provisioner as tp

    class _Telemetry:
        def log_background(self, **kwargs):
            return None

    monkeypatch.setattr(admin._common, "get_telemetry_logger", lambda: _Telemetry())
    object.__setattr__(admin.settings, "qe_enabled", False)
    await tp.provision_tenant("tenant-z", wait_for_queryable_indexes=False)

    with pytest.raises(HTTPException) as exc:
        await admin.delete_tenant(_Req(tenant_id="tenant-z", roles=["admin"]), "tenant-z")
    assert exc.value.status_code == 403

    # Default delete is a reversible soft-delete: the tenant is locked out but its
    # control doc and database are retained until the purge window elapses.
    response = await admin.delete_tenant(
        _Req(roles=[admin.settings.platform_admin_role]),
        "tenant-z",
    )
    assert response.deleted is True
    assert response.status == "deleted"
    assert response.tenant_id == "tenant-z"
    assert response.purge_at is not None
    doc = await patch_mongo._control_db["tenants"].find_one({"tenant_id": "tenant-z"})
    assert doc is not None and doc["status"] == "deleted"
    assert tenant_db_name("tenant-z") in patch_mongo._client._databases  # noqa: SLF001


@pytest.mark.asyncio
async def test_delete_tenant_hard_drops_database(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin
    import services.tenant_provisioner as tp

    class _Telemetry:
        def log_background(self, **kwargs):
            return None

    monkeypatch.setattr(admin._common, "get_telemetry_logger", lambda: _Telemetry())
    object.__setattr__(admin.settings, "qe_enabled", False)
    await tp.provision_tenant("tenant-hard", wait_for_queryable_indexes=False)

    response = await admin.delete_tenant(
        _Req(roles=[admin.settings.platform_admin_role]),
        "tenant-hard",
        hard=True,
    )
    assert response.deleted is True
    assert response.status == "purged"
    assert await patch_mongo._control_db["tenants"].find_one({"tenant_id": "tenant-hard"}) is None
    assert tenant_db_name("tenant-hard") not in patch_mongo._client._databases  # noqa: SLF001


@pytest.mark.asyncio
async def test_restore_tenant_reverses_soft_delete(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin
    import services.tenant_provisioner as tp

    class _Telemetry:
        def log_background(self, **kwargs):
            return None

    monkeypatch.setattr(admin._common, "get_telemetry_logger", lambda: _Telemetry())
    object.__setattr__(admin.settings, "qe_enabled", False)
    await tp.provision_tenant("tenant-r", wait_for_queryable_indexes=False)
    platform_admin = _Req(roles=[admin.settings.platform_admin_role])

    await admin.delete_tenant(platform_admin, "tenant-r")
    restored = await admin.restore_tenant(platform_admin, "tenant-r")
    assert restored.restored is True
    assert restored.status == "active"
    doc = await patch_mongo._control_db["tenants"].find_one({"tenant_id": "tenant-r"})
    assert doc is not None and doc["status"] == "active"


@pytest.mark.asyncio
async def test_restore_tenant_requires_platform_admin(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.restore_tenant(_Req(tenant_id="tenant-r", roles=["admin"]), "tenant-r")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_restore_tenant_404_when_not_soft_deleted(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin
    import services.tenant_provisioner as tp

    class _Telemetry:
        def log_background(self, **kwargs):
            return None

    monkeypatch.setattr(admin._common, "get_telemetry_logger", lambda: _Telemetry())
    object.__setattr__(admin.settings, "qe_enabled", False)
    await tp.provision_tenant("tenant-active", wait_for_queryable_indexes=False)

    with pytest.raises(HTTPException) as exc:
        await admin.restore_tenant(
            _Req(roles=[admin.settings.platform_admin_role]), "tenant-active"
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_cache_migrate_defaults_to_caller_tenant(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    class _Migrator:
        async def migrate(self, *, tenant_ids, mode, batch_size):
            return {"tenant_ids": tenant_ids, "mode": mode, "batch_size": batch_size}

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin._common, "cache_migration_service", _Migrator())
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

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin._common, "cache_migration_service", _Migrator())
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
            server=None,
        ):
            return [
                {
                    "tenant_id": tenant_id,
                    "name": query,
                    "mode": mode,
                    "limit": limit,
                    "server": server,
                }
            ]

    monkeypatch.setattr(admin._common, "hybrid_search_service", _Search())
    payload = AdminSearchRequest(query="weather", mode="hybrid", limit=3, server="weather")
    response = await admin.admin_search(_Req(), payload)
    assert response["tenant_id"] == "local-dev"
    assert response["items"][0]["name"] == "weather"
    assert response["items"][0]["server"] == "weather"


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

    monkeypatch.setattr(admin._common, "get_telemetry_logger", lambda: _Telemetry())

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
        admin._common,
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
        admin._common,
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

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
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

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: _Registry())
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

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
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
# Code-backed tools (transport="code") — authoring + runtime behavior
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

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: _Registry())


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
    # Code servers carry no downstream connection target. The encrypted routing
    # fields (command/env/args) are omitted entirely so Queryable Encryption never
    # has to encrypt a null value.
    assert stored["endpoint"] is None and stored.get("command") is None
    assert "command" not in stored and "env" not in stored and "args" not in stored
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
