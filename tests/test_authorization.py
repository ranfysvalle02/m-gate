import pytest

from services.authorization import AuthorizationService


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    async def find_one(self, query):
        return self.docs.get((query.get("server"), query.get("name")))


class _FakeDb(dict):
    pass


@pytest.mark.asyncio
async def test_authorization_enforces_scope(monkeypatch):
    docs = {
        ("orders", "update_order_status"): {
            "server": "orders",
            "name": "update_order_status",
            "scopes": ["orders:write"],
        }
    }
    fake_db = _FakeDb(tool_catalog=_FakeCollection(docs))
    monkeypatch.setattr("services.authorization.get_tenant_database", lambda tenant_id: fake_db)

    service = AuthorizationService()
    denied = await service.authorize_tool_call(
        server="orders",
        name="update_order_status",
        caller_scopes=["orders", "readonly", "server:orders"],
        caller_roles=[],
    )
    allowed = await service.authorize_tool_call(
        server="orders",
        name="update_order_status",
        caller_scopes=["orders:write", "server:orders"],
        caller_roles=[],
    )
    assert denied.allowed is False
    assert denied.reason == "scope_mismatch"
    assert allowed.allowed is True


@pytest.mark.asyncio
async def test_authorization_admin_override(monkeypatch):
    docs = {
        ("orders", "update_order_status"): {
            "server": "orders",
            "name": "update_order_status",
            "scopes": ["orders:write"],
        }
    }
    fake_db = _FakeDb(tool_catalog=_FakeCollection(docs))
    monkeypatch.setattr("services.authorization.get_tenant_database", lambda tenant_id: fake_db)

    service = AuthorizationService()
    result = await service.authorize_tool_call(
        server="orders",
        name="update_order_status",
        caller_scopes=[],
        caller_roles=["admin"],
    )
    assert result.allowed is True
    assert result.reason == "admin_override"


@pytest.mark.asyncio
async def test_authorization_allows_when_scope_not_required(monkeypatch):
    docs = {
        ("weather", "get_forecast"): {
            "server": "weather",
            "name": "get_forecast",
            "scopes": [],
        }
    }
    fake_db = _FakeDb(tool_catalog=_FakeCollection(docs))
    monkeypatch.setattr("services.authorization.get_tenant_database", lambda tenant_id: fake_db)

    service = AuthorizationService()
    result = await service.authorize_tool_call(
        server="weather",
        name="get_forecast",
        caller_scopes=["server:weather"],
        caller_roles=[],
    )
    assert result.allowed is True
    assert result.reason == "no_scope_required"


@pytest.mark.asyncio
async def test_authorization_denies_when_scope_missing(monkeypatch):
    docs = {
        ("orders", "update_order_status"): {
            "server": "orders",
            "name": "update_order_status",
            "scopes": ["orders:write"],
        }
    }
    fake_db = _FakeDb(tool_catalog=_FakeCollection(docs))
    monkeypatch.setattr("services.authorization.get_tenant_database", lambda tenant_id: fake_db)

    service = AuthorizationService()
    result = await service.authorize_tool_call(
        server="orders",
        name="update_order_status",
        caller_scopes=["server:orders"],
        caller_roles=[],
    )
    assert result.allowed is False
    assert result.reason == "scope_mismatch"


@pytest.mark.asyncio
async def test_authorization_denies_when_server_scope_missing(monkeypatch):
    docs = {
        ("orders", "find_order"): {
            "server": "orders",
            "name": "find_order",
            "scopes": [],
        }
    }
    fake_db = _FakeDb(tool_catalog=_FakeCollection(docs))
    monkeypatch.setattr("services.authorization.get_tenant_database", lambda tenant_id: fake_db)

    service = AuthorizationService()
    result = await service.authorize_tool_call(
        server="orders",
        name="find_order",
        caller_scopes=["orders"],
        caller_roles=[],
    )
    assert result.allowed is False
    assert result.reason == "server_scope_required"


@pytest.mark.asyncio
async def test_authorization_denies_when_tool_missing(monkeypatch):
    fake_db = _FakeDb(tool_catalog=_FakeCollection({}))
    monkeypatch.setattr("services.authorization.get_tenant_database", lambda tenant_id: fake_db)

    service = AuthorizationService()
    result = await service.authorize_tool_call(
        server="missing",
        name="tool",
        caller_scopes=["x"],
        caller_roles=[],
    )
    assert result.allowed is False
    assert result.reason == "tool_not_found"


@pytest.mark.asyncio
async def test_authorization_passes_through_tenant_id(monkeypatch):
    docs = {
        ("orders", "find_order"): {
            "server": "orders",
            "name": "find_order",
            "scopes": [],
        }
    }
    fake_db = _FakeDb(tool_catalog=_FakeCollection(docs))
    captured = {}

    def _fake_get_tenant_database(tenant_id):
        captured["tenant_id"] = tenant_id
        return fake_db

    monkeypatch.setattr("services.authorization.get_tenant_database", _fake_get_tenant_database)
    service = AuthorizationService()
    result = await service.authorize_tool_call(
        tenant_id="tenant-123",
        server="orders",
        name="find_order",
        caller_scopes=["server:orders"],
        caller_roles=[],
    )
    assert result.allowed is True
    assert captured["tenant_id"] == "tenant-123"
