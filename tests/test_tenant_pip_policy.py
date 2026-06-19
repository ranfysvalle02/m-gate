from __future__ import annotations

import pytest

from config.settings import Settings
from services.tenant_pip_policy import (
    PipPolicyError,
    effective_allowlist,
    evaluate_requirements,
    get_effective_pip_allowlist,
    get_tenant_pip_allowlist,
    global_ceiling_names,
    normalize_requirement_name,
    reset_tenant_pip_policy_cache,
    set_tenant_pip_allowlist,
    validate_allowlist_entries,
)


def test_normalize_requirement_name_strips_pin_and_extras():
    assert normalize_requirement_name("Requests==2.32.3") == "requests"
    assert normalize_requirement_name("Flask[async]==3.0") == "flask"
    assert normalize_requirement_name("typing_extensions") == "typing-extensions"
    assert normalize_requirement_name("  oRjSoN  ") == "orjson"
    assert normalize_requirement_name("") == ""


def test_global_ceiling_names_parses_comma_and_space():
    settings = Settings(sandbox_allowed_requirements="Requests, orjson  pandas")
    assert global_ceiling_names(settings) == {"requests", "orjson", "pandas"}
    assert global_ceiling_names(Settings(sandbox_allowed_requirements="")) == set()


def test_validate_allowlist_entries_normalizes_and_dedupes():
    entries = validate_allowlist_entries(["Requests", "requests", " orjson ", ""])
    assert entries == ["orjson", "requests"]


@pytest.mark.parametrize(
    "bad",
    ["requests==2.0", "flask[async]", "https://x/y.whl", "bad name", "../evil", "a/b"],
)
def test_validate_allowlist_entries_rejects_non_bare_names(bad):
    with pytest.raises(PipPolicyError):
        validate_allowlist_entries([bad])


def test_effective_allowlist_is_intersection():
    settings = Settings(sandbox_allowed_requirements="requests orjson")
    assert effective_allowlist(["requests", "pandas"], settings=settings) == ["requests"]
    # Empty tenant list => nothing effective even with a permissive ceiling.
    assert effective_allowlist([], settings=settings) == []
    # Empty ceiling => nothing effective even with a tenant list.
    assert effective_allowlist(["requests"], settings=Settings()) == []


def test_evaluate_requirements_splits_blocked_reasons():
    settings = Settings(sandbox_allowed_requirements="requests orjson")
    decision = evaluate_requirements(
        ["requests==2.32.3", "orjson==3.10", "pandas==2.0"],
        tenant_allowlist=["requests"],  # orjson in ceiling but not tenant-allowed
        settings=settings,
    )
    assert decision.allowed == ("requests",)
    assert decision.blocked_by_tenant == ("orjson",)  # ceiling-ok, tenant missing
    assert decision.blocked_by_global == ("pandas",)  # not in ceiling at all
    assert decision.ok is False
    msg = decision.error_message()
    assert "platform pip ceiling" in msg
    assert "tenant's code-package policy" in msg


def test_evaluate_requirements_ok_when_all_effective():
    settings = Settings(sandbox_allowed_requirements="requests")
    decision = evaluate_requirements(
        ["Requests==2.32.3"], tenant_allowlist=["requests"], settings=settings
    )
    assert decision.ok is True
    assert decision.allowed == ("requests",)


def test_evaluate_requirements_dedupes_by_normalized_name():
    settings = Settings(sandbox_allowed_requirements="typing-extensions")
    decision = evaluate_requirements(
        ["Typing_Extensions==4.0", "typing-extensions==4.1", "TYPING.EXTENSIONS==4.2"],
        tenant_allowlist=["typing-extensions"],
        settings=settings,
    )
    # All three spellings collapse to one PEP 503 distribution name.
    assert decision.requested == ("typing-extensions",)
    assert decision.ok is True


@pytest.mark.asyncio
async def test_get_tenant_pip_allowlist_defaults_empty(patch_mongo):
    assert await get_tenant_pip_allowlist("never-seen") == []


@pytest.mark.asyncio
async def test_set_and_get_tenant_pip_allowlist_round_trip(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1"})

    doc = await set_tenant_pip_allowlist(
        "t1", ["Requests", "requests", " orjson "], updated_by="ops@x"
    )
    assert doc is not None
    assert doc["code_requirements_allowlist"] == ["orjson", "requests"]
    assert doc["code_requirements_updated_by"] == "ops@x"

    reset_tenant_pip_policy_cache()
    assert await get_tenant_pip_allowlist("t1") == ["orjson", "requests"]


@pytest.mark.asyncio
async def test_set_tenant_pip_allowlist_rejects_invalid_entry(patch_mongo):
    control = patch_mongo._control_db
    await control["tenants"].insert_one({"tenant_id": "t1", "db_name": "db_t1"})
    with pytest.raises(PipPolicyError):
        await set_tenant_pip_allowlist("t1", ["requests==2.0"])


@pytest.mark.asyncio
async def test_set_tenant_pip_allowlist_unknown_tenant_returns_none(patch_mongo):
    assert await set_tenant_pip_allowlist("ghost", ["requests"]) is None


@pytest.mark.asyncio
async def test_get_effective_pip_allowlist_intersects_with_ceiling(patch_mongo, monkeypatch):
    import services.tenant_pip_policy as policy

    control = patch_mongo._control_db
    await control["tenants"].insert_one(
        {
            "tenant_id": "t1",
            "db_name": "db_t1",
            "code_requirements_allowlist": ["requests", "pandas"],
        }
    )
    settings = Settings(sandbox_allowed_requirements="requests orjson")
    monkeypatch.setattr(policy, "get_settings", lambda: settings)
    # pandas is tenant-allowed but not in the ceiling => excluded from effective.
    assert await get_effective_pip_allowlist("t1", settings=settings) == ["requests"]
