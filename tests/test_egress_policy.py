from __future__ import annotations

import ipaddress
from types import SimpleNamespace

import pytest

from services import egress_policy
from services.egress_policy import (
    EgressNotAllowed,
    build_code_egress_rules,
    build_rules,
    check_endpoint_allowed,
    effective_code_egress_hosts,
    parse_allowlist,
    validate_entries,
    validate_entry,
)


def _settings(*, enabled=True, global_allowlist="", default_deny=False):
    return SimpleNamespace(
        egress_allowlist_enabled=enabled,
        egress_global_allowlist=global_allowlist,
        egress_default_deny=default_deny,
    )


def _ips(*addresses: str):
    return [ipaddress.ip_address(addr) for addr in addresses]


def test_parse_allowlist_splits_dedups_and_lowercases():
    assert parse_allowlist("A.com, b.com  b.com\nC.NET") == ["a.com", "b.com", "c.net"]
    assert parse_allowlist(["X.com", "x.com", "y.com"]) == ["x.com", "y.com"]
    assert parse_allowlist(None) == []


@pytest.mark.parametrize(
    "entry",
    ["api.example.com", "*.example.com", "203.0.113.5", "203.0.113.0/24", "2001:db8::/32"],
)
def test_validate_entry_accepts_supported_forms(entry: str):
    assert validate_entry(entry) == entry.lower()


@pytest.mark.parametrize("entry", ["", "   ", "has space.com", "bad/cidr/x", "10.0.0.0/99"])
def test_validate_entry_rejects_malformed(entry: str):
    with pytest.raises(EgressNotAllowed):
        validate_entry(entry)


def test_validate_entries_dedups():
    assert validate_entries(["A.com", "a.com", "b.com"]) == ["a.com", "b.com"]


def test_rules_inactive_when_unconfigured():
    rules = build_rules(_settings(), tenant_allowlist=[])
    assert rules.is_active is False
    # Inactive evaluate is a no-op even for a private IP (caller owns that gate).
    rules.evaluate("anything.example", _ips("10.0.0.1"))


def test_rules_inactive_when_disabled_even_with_allowlist():
    rules = build_rules(_settings(enabled=False, global_allowlist="api.example.com"))
    assert rules.is_active is False


def test_evaluate_allows_exact_host():
    rules = build_rules(_settings(global_allowlist="api.example.com"))
    rules.evaluate("api.example.com", _ips("93.184.216.34"))
    with pytest.raises(EgressNotAllowed):
        rules.evaluate("evil.example.com", _ips("93.184.216.34"))


def test_evaluate_allows_wildcard_subdomain_but_not_apex():
    rules = build_rules(_settings(global_allowlist="*.example.com"))
    rules.evaluate("api.example.com", _ips("93.184.216.34"))
    with pytest.raises(EgressNotAllowed):
        rules.evaluate("example.com", _ips("93.184.216.34"))


def test_evaluate_allows_cidr_match():
    rules = build_rules(_settings(global_allowlist="93.184.216.0/24"))
    rules.evaluate("anyhost.example", _ips("93.184.216.34"))
    with pytest.raises(EgressNotAllowed):
        rules.evaluate("anyhost.example", _ips("203.0.113.9"))


def test_evaluate_blocks_denylisted_ip_even_when_host_matches():
    # Host is allowlisted but it resolves to a private address (rebinding) -> blocked.
    rules = build_rules(_settings(global_allowlist="api.example.com"))
    with pytest.raises(EgressNotAllowed):
        rules.evaluate("api.example.com", _ips("10.0.0.5"))


def test_default_deny_blocks_everything_when_empty():
    rules = build_rules(_settings(default_deny=True))
    assert rules.is_active is True
    with pytest.raises(EgressNotAllowed):
        rules.evaluate("api.example.com", _ips("93.184.216.34"))


def test_global_and_tenant_intersection():
    # Global permits the whole zone; tenant narrows to one host.
    rules = build_rules(
        _settings(global_allowlist="*.example.com"),
        tenant_allowlist=["api.example.com"],
    )
    rules.evaluate("api.example.com", _ips("93.184.216.34"))
    # Inside the global ceiling but outside the tenant list -> blocked.
    with pytest.raises(EgressNotAllowed):
        rules.evaluate("other.example.com", _ips("93.184.216.34"))


def test_tenant_outside_global_ceiling_is_blocked():
    rules = build_rules(
        _settings(global_allowlist="*.example.com"),
        tenant_allowlist=["api.vendor.com"],
    )
    # Satisfies tenant but not the global ceiling.
    with pytest.raises(EgressNotAllowed):
        rules.evaluate("api.vendor.com", _ips("93.184.216.34"))


@pytest.mark.asyncio
async def test_check_endpoint_allowed_noop_when_inactive(monkeypatch):
    called = {"resolve": False}

    def _resolve(*_a, **_k):
        called["resolve"] = True
        return _ips("93.184.216.34")

    monkeypatch.setattr(egress_policy, "resolve_host", _resolve)
    await check_endpoint_allowed(
        "https://anything.example/mcp",
        tenant_allowlist=[],
        settings=_settings(),
    )
    assert called["resolve"] is False


@pytest.mark.asyncio
async def test_check_endpoint_allowed_blocks_unlisted(monkeypatch):
    monkeypatch.setattr(egress_policy, "resolve_host", lambda *_a, **_k: _ips("93.184.216.34"))
    with pytest.raises(EgressNotAllowed):
        await check_endpoint_allowed(
            "https://evil.example/mcp",
            tenant_allowlist=[],
            settings=_settings(global_allowlist="api.allowed.example"),
        )


@pytest.mark.asyncio
async def test_check_endpoint_allowed_permits_listed(monkeypatch):
    monkeypatch.setattr(egress_policy, "resolve_host", lambda *_a, **_k: _ips("93.184.216.34"))
    await check_endpoint_allowed(
        "https://api.allowed.example/mcp",
        tenant_allowlist=[],
        settings=_settings(global_allowlist="*.allowed.example"),
    )


# ---- Code egress (context.http) rules: always fail-closed ------------------


def test_code_egress_denies_all_even_when_allowlist_feature_off():
    # The downstream-proxy toggle is OFF and no allowlist is configured, yet code
    # egress must still be active + deny-by-default (no SSRF bypass for tenant code).
    rules = build_code_egress_rules(
        _settings(enabled=False, global_allowlist=""), tenant_allowlist=[]
    )
    assert rules.is_active is True
    with pytest.raises(EgressNotAllowed):
        rules.evaluate("api.example.com", _ips("93.184.216.34"))


def test_code_egress_empty_tenant_blocks_even_within_global_ceiling():
    # Strict intersect: a permissive global ceiling does NOT grant access on its
    # own. With no tenant grant the effective set is empty -> deny.
    rules = build_code_egress_rules(
        _settings(enabled=False, global_allowlist="*.example.com"), tenant_allowlist=[]
    )
    with pytest.raises(EgressNotAllowed):
        rules.evaluate("api.example.com", _ips("93.184.216.34"))


def test_code_egress_allows_tenant_grant_within_ceiling():
    rules = build_code_egress_rules(
        _settings(enabled=False, global_allowlist="*.example.com"),
        tenant_allowlist=["api.example.com"],
    )
    rules.evaluate("api.example.com", _ips("93.184.216.34"))
    # Tenant grant outside the global ceiling stays blocked.
    rules_outside = build_code_egress_rules(
        _settings(enabled=False, global_allowlist="*.example.com"),
        tenant_allowlist=["api.vendor.com"],
    )
    with pytest.raises(EgressNotAllowed):
        rules_outside.evaluate("api.vendor.com", _ips("93.184.216.34"))


def test_code_egress_always_screens_ssrf_on_allowlisted_host():
    rules = build_code_egress_rules(
        _settings(enabled=False, global_allowlist="api.example.com"),
        tenant_allowlist=["api.example.com"],
    )
    with pytest.raises(EgressNotAllowed):
        rules.evaluate("api.example.com", _ips("169.254.169.254"))


def test_effective_code_egress_hosts_intersects_with_ceiling():
    settings = _settings(global_allowlist="api.example.com, cdn.example.com")
    assert effective_code_egress_hosts(
        ["api.example.com", "api.vendor.com"], settings=settings
    ) == ["api.example.com"]
    # No ceiling configured -> tenant grants stand alone.
    assert effective_code_egress_hosts(
        ["api.example.com"], settings=_settings(global_allowlist="")
    ) == ["api.example.com"]
