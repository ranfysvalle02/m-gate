import pytest

from services.authorization import AuthorizationService


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    async def find_one(self, query):
        return self.docs.get((query.get("server"), query.get("name")))


class _FakeDb(dict):
    pass


def _patch_policy(monkeypatch, *, allowlist=None, disabled=None, max_tools=0):
    """Patch the tenant tool-policy lookup the authorizer consults.

    Keeps these unit tests hermetic: ``authorize_tool_call`` now reads the
    tenant's allowlist/disabled overlay, so without this it would reach for the
    real control DB. Default is the permissive policy (empty allowlist, nothing
    disabled), matching an un-curated tenant.
    """

    async def _fake(tenant_id, *, settings=None):
        return {
            "allowlist": list(allowlist or []),
            "max_tools": max_tools,
            "disabled_tools": list(disabled or []),
        }

    monkeypatch.setattr("services.authorization.get_tool_policy", _fake)


@pytest.fixture(autouse=True)
def _default_permissive_policy(monkeypatch):
    # Most tests want the un-curated tenant; individual tests override via
    # _patch_policy(...) when they exercise the allowlist/disabled paths.
    _patch_policy(monkeypatch)


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
        caller_roles=["tool:invoke"],
    )
    allowed = await service.authorize_tool_call(
        server="orders",
        name="update_order_status",
        caller_scopes=["orders:write", "server:orders"],
        caller_roles=["tool:invoke"],
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
        caller_roles=["tool:invoke"],
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
        caller_roles=["tool:invoke"],
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
        caller_roles=["tool:invoke"],
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
        caller_roles=["tool:invoke"],
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
        caller_roles=["tool:invoke"],
    )
    assert result.allowed is True
    assert captured["tenant_id"] == "tenant-123"


# --------------------------------------------------------------------------- #
#  Invoke capability, allowlist, and disabled overlay                         #
# --------------------------------------------------------------------------- #


def _orders_db(monkeypatch):
    docs = {
        ("orders", "find_order"): {
            "server": "orders",
            "name": "find_order",
            "scopes": [],
        }
    }
    fake_db = _FakeDb(tool_catalog=_FakeCollection(docs))
    monkeypatch.setattr("services.authorization.get_tenant_database", lambda tenant_id: fake_db)


@pytest.mark.asyncio
async def test_invoke_not_permitted_for_discover_only_role(monkeypatch):
    # A tool:read principal clears the coarse RBAC gate (so it can discover) but
    # carries no invoke capability — tools/call is refused here.
    _orders_db(monkeypatch)
    service = AuthorizationService()
    result = await service.authorize_tool_call(
        server="orders",
        name="find_order",
        caller_scopes=["server:orders"],
        caller_roles=["tool:read"],
    )
    assert result.allowed is False
    assert result.reason == "invoke_not_permitted"


@pytest.mark.asyncio
async def test_tool_not_allowlisted(monkeypatch):
    _orders_db(monkeypatch)
    _patch_policy(monkeypatch, allowlist=["weather/get_forecast"])
    service = AuthorizationService()
    result = await service.authorize_tool_call(
        server="orders",
        name="find_order",
        caller_scopes=["server:orders"],
        caller_roles=["tool:invoke"],
    )
    assert result.allowed is False
    assert result.reason == "tool_not_allowlisted"


@pytest.mark.asyncio
async def test_allowlist_exact_match_allows(monkeypatch):
    _orders_db(monkeypatch)
    _patch_policy(monkeypatch, allowlist=["orders/find_order"])
    service = AuthorizationService()
    result = await service.authorize_tool_call(
        server="orders",
        name="find_order",
        caller_scopes=["server:orders"],
        caller_roles=["tool:invoke"],
    )
    assert result.allowed is True


@pytest.mark.asyncio
async def test_allowlist_server_wildcard_allows(monkeypatch):
    _orders_db(monkeypatch)
    _patch_policy(monkeypatch, allowlist=["orders/*"])
    service = AuthorizationService()
    result = await service.authorize_tool_call(
        server="orders",
        name="find_order",
        caller_scopes=["server:orders"],
        caller_roles=["tool:invoke"],
    )
    assert result.allowed is True


@pytest.mark.asyncio
async def test_admin_bypasses_allowlist(monkeypatch):
    # An admin token is not constrained by the curated allowlist.
    _orders_db(monkeypatch)
    _patch_policy(monkeypatch, allowlist=["weather/get_forecast"])
    service = AuthorizationService()
    result = await service.authorize_tool_call(
        server="orders",
        name="find_order",
        caller_scopes=[],
        caller_roles=["admin"],
    )
    assert result.allowed is True
    assert result.reason == "admin_override"


@pytest.mark.asyncio
async def test_disabled_tool_blocked_for_everyone(monkeypatch):
    # A tenant-disabled tool is refused even to an admin: disabling truly takes a
    # tool out of service for the tenant rather than hiding it from non-admins.
    _orders_db(monkeypatch)
    _patch_policy(monkeypatch, disabled=["orders/find_order"])
    service = AuthorizationService()
    for roles in (["admin"], ["tool:invoke"], ["tool:read"]):
        result = await service.authorize_tool_call(
            server="orders",
            name="find_order",
            caller_scopes=["server:orders"],
            caller_roles=roles,
        )
        assert result.allowed is False
        assert result.reason == "tool_disabled"
