from __future__ import annotations

import pytest

from services.tenant_tool_policy import (
    filter_available_tools,
    get_tool_policy,
    is_tool_available,
    matches_allowlist,
    reset_tenant_tool_policy_cache,
    set_tool_enabled,
    set_tool_policy,
)


def test_matches_allowlist_semantics():
    # Empty allowlist == unrestricted.
    assert matches_allowlist("orders", "find", []) is True
    assert matches_allowlist("orders", "find", ["orders/find"]) is True
    assert matches_allowlist("orders", "find", ["orders/*"]) is True
    assert matches_allowlist("orders", "find", ["weather/forecast"]) is False


def test_is_tool_available_disabled_overrides_allowlist():
    policy = {"allowlist": ["orders/find"], "disabled_tools": ["orders/find"], "max_tools": 0}
    # Disabled wins even if the tool is allowlisted.
    assert is_tool_available(policy, "orders", "find") is False
    policy2 = {"allowlist": ["orders/find"], "disabled_tools": [], "max_tools": 0}
    assert is_tool_available(policy2, "orders", "find") is True


@pytest.mark.asyncio
async def test_get_tool_policy_defaults_permissive(patch_mongo):
    policy = await get_tool_policy("never-seen")
    assert policy == {"allowlist": [], "max_tools": 0, "disabled_tools": []}


@pytest.mark.asyncio
async def test_set_tool_policy_persists_and_normalizes(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1"})

    doc = await set_tool_policy(
        "t1",
        allowlist=["orders/find", "orders/find", "  weather/* ", ""],
        max_tools=3,
        updated_by="ops@x",
    )
    assert doc is not None
    policy = await get_tool_policy("t1")
    # De-duped, stripped, sorted; empties dropped.
    assert policy["allowlist"] == ["orders/find", "weather/*"]
    assert policy["max_tools"] == 3


@pytest.mark.asyncio
async def test_negative_max_tools_coerced_to_unlimited(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1"})
    await set_tool_policy("t1", allowlist=[], max_tools=-5)
    policy = await get_tool_policy("t1")
    assert policy["max_tools"] == 0


@pytest.mark.asyncio
async def test_set_tool_enabled_toggles_disabled_overlay(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1"})

    await set_tool_enabled("t1", "orders", "delete_order", False, updated_by="ops@x")
    policy = await get_tool_policy("t1")
    assert "orders/delete_order" in policy["disabled_tools"]

    # Disabling again is idempotent (addToSet), not duplicated.
    await set_tool_enabled("t1", "orders", "delete_order", False)
    reset_tenant_tool_policy_cache()
    policy = await get_tool_policy("t1")
    assert policy["disabled_tools"].count("orders/delete_order") == 1

    await set_tool_enabled("t1", "orders", "delete_order", True)
    policy = await get_tool_policy("t1")
    assert "orders/delete_order" not in policy["disabled_tools"]


@pytest.mark.asyncio
async def test_set_tool_enabled_missing_tenant_returns_none(patch_mongo):
    assert await set_tool_enabled("ghost", "s", "n", False) is None


@pytest.mark.asyncio
async def test_filter_available_tools_applies_allowlist_and_disabled(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1"})
    await set_tool_policy("t1", allowlist=["orders/*"], max_tools=0)
    await set_tool_enabled("t1", "orders", "delete_order", False)

    tools = [
        {"server": "orders", "name": "find_order"},
        {"server": "orders", "name": "delete_order"},  # disabled
        {"server": "weather", "name": "forecast"},  # not allowlisted
    ]
    filtered = await filter_available_tools("t1", tools)
    names = {(t["server"], t["name"]) for t in filtered}
    assert names == {("orders", "find_order")}


@pytest.mark.asyncio
async def test_filter_available_tools_unrestricted_returns_all(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1"})
    tools = [
        {"server": "orders", "name": "find_order"},
        {"server": "weather", "name": "forecast"},
    ]
    filtered = await filter_available_tools("t1", tools)
    assert len(filtered) == 2
