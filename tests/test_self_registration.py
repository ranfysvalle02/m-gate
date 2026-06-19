"""Tests for self-service beta registration and the unconfirmed/confirmed tiers.

Covers three surfaces:

* ``services/registration.py`` — the public sign-up orchestration (happy path,
  feature flag, per-IP throttle, beta cap, duplicate-email rollback, validation).
* ``services/account_tier.py`` enforcement at server registration — the abuse gate
  that confines an ``unconfirmed`` account to ``code`` servers with tiny caps, and
  the platform-admin bypass.
* The admin confirm/unconfirm transitions that lift/restore those caps and re-stamp
  the per-tenant quota.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import gateway.routers.admin as admin
import gateway.routers.admin.servers as servers_router
import gateway.routers.auth as auth_router
from config.settings import get_settings
from database.mongo import get_control_database, get_tenant_database
from services import registration as registration_service
from services import users as users_service
from services.account_tier import (
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_UNCONFIRMED,
    get_tenant_confirmation,
)
from services.usage_metering import get_effective_quota


# --------------------------------------------------------------------------- #
#  Test doubles                                                               #
# --------------------------------------------------------------------------- #
class _State:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Req:
    def __init__(self, *, tenant_id="local-dev", roles=None, user_id="admin@example.com"):
        self.state = _State(tenant_id=tenant_id, roles=roles or [], user_id=user_id)
        self.headers = {}


class _Client:
    def __init__(self, host: str):
        self.host = host


class _RegRequest:
    """Minimal Request stand-in for the public POST /auth/register handler."""

    def __init__(self, payload: dict, *, content_type="application/json", client_ip="203.0.113.7"):
        self._payload = payload
        self.headers = {"content-type": content_type}
        self.client = _Client(client_ip)

    async def json(self):
        return self._payload

    async def body(self):
        return b""


def _platform_admin() -> _Req:
    return _Req(roles=[get_settings().platform_admin_role])


def _enable_registration(**overrides) -> None:
    """Turn on self-registration and apply any tier/limit overrides in-place."""
    settings = get_settings()
    object.__setattr__(settings, "self_registration_enabled", True)
    for key, value in overrides.items():
        object.__setattr__(settings, key, value)


async def _seed_tenant(tenant_id="local-dev", *, confirmation=CONFIRMATION_CONFIRMED):
    await get_control_database()["tenants"].insert_one(
        {
            "tenant_id": tenant_id,
            "db_name": f"tenant_{tenant_id}",
            "status": "active",
            "confirmation": confirmation,
        }
    )


# --------------------------------------------------------------------------- #
#  Registration service: happy path                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_register_happy_path_provisions_unconfirmed_tenant(reset_settings, patch_mongo):
    _enable_registration()

    result = await registration_service.register_self_service_user(
        email="newbie@example.com", password="hunter2-strong", client_ip="198.51.100.1"
    )

    assert result.confirmation == CONFIRMATION_UNCONFIRMED
    assert result.token
    assert result.tenant_id.startswith(get_settings().self_registration_tenant_prefix)
    assert result.user["self_registered"] is True
    # Tenant-admin of its OWN tenant, never platform-admin.
    assert set(result.user["roles"]) == {"user", "admin"}
    assert get_settings().platform_admin_role not in result.user["roles"]

    # The account is persisted, flagged, and instantly usable.
    principal = await users_service.authenticate("newbie@example.com", "hunter2-strong")
    assert principal is not None

    # The tenant is provisioned and stamped unconfirmed.
    assert await get_tenant_confirmation(result.tenant_id) == CONFIRMATION_UNCONFIRMED
    tenant_doc = await get_control_database()["tenants"].find_one({"tenant_id": result.tenant_id})
    assert tenant_doc is not None

    # Quota was stamped to the unconfirmed tier.
    quota = await get_effective_quota(result.tenant_id)
    assert quota["calls_limit"] == get_settings().unconfirmed_quota_calls_per_period
    assert (
        quota["sandbox_seconds_limit"]
        == get_settings().unconfirmed_quota_sandbox_seconds_per_period
    )


# --------------------------------------------------------------------------- #
#  Registration service: guards                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_register_disabled_raises(reset_settings, patch_mongo):
    # Default posture: the flag is off.
    with pytest.raises(registration_service.RegistrationDisabled):
        await registration_service.register_self_service_user(
            email="x@example.com", password="hunter2-strong", client_ip="198.51.100.1"
        )


@pytest.mark.asyncio
async def test_register_short_password_rejected(reset_settings, patch_mongo):
    _enable_registration(self_registration_min_password_length=10)
    with pytest.raises(registration_service.RegistrationValidationError):
        await registration_service.register_self_service_user(
            email="x@example.com", password="short", client_ip="198.51.100.1"
        )


@pytest.mark.asyncio
async def test_register_bad_email_rejected(reset_settings, patch_mongo):
    _enable_registration()
    with pytest.raises(registration_service.RegistrationValidationError):
        await registration_service.register_self_service_user(
            email="not-an-email", password="hunter2-strong", client_ip="198.51.100.1"
        )


@pytest.mark.asyncio
async def test_register_ip_throttle(reset_settings, patch_mongo):
    _enable_registration(self_registration_max_per_ip=1, self_registration_window_seconds=3600)

    await registration_service.register_self_service_user(
        email="first@example.com", password="hunter2-strong", client_ip="203.0.113.9"
    )
    with pytest.raises(registration_service.RegistrationThrottled) as exc:
        await registration_service.register_self_service_user(
            email="second@example.com", password="hunter2-strong", client_ip="203.0.113.9"
        )
    assert exc.value.retry_after_seconds >= 1

    # A different IP is unaffected by the first IP's bucket.
    other = await registration_service.register_self_service_user(
        email="third@example.com", password="hunter2-strong", client_ip="203.0.113.10"
    )
    assert other.confirmation == CONFIRMATION_UNCONFIRMED


@pytest.mark.asyncio
async def test_register_beta_cap(reset_settings, patch_mongo):
    _enable_registration(self_registration_max_tenants=1, self_registration_max_per_ip=0)

    await registration_service.register_self_service_user(
        email="one@example.com", password="hunter2-strong", client_ip="203.0.113.1"
    )
    with pytest.raises(registration_service.BetaFull):
        await registration_service.register_self_service_user(
            email="two@example.com", password="hunter2-strong", client_ip="203.0.113.2"
        )


@pytest.mark.asyncio
async def test_register_duplicate_email_does_not_orphan_tenant(reset_settings, patch_mongo):
    _enable_registration(self_registration_max_per_ip=0)
    # An existing account (e.g. an admin-created user) owns this email.
    await users_service.create_user(
        email="taken@example.com", password="x", tenant_id="local-dev", roles=["user"]
    )

    prefix = get_settings().self_registration_tenant_prefix
    before = await get_control_database()["tenants"].count_documents(
        {"tenant_id": {"$regex": f"^{prefix}"}}
    )
    with pytest.raises(users_service.UserAlreadyExists):
        await registration_service.register_self_service_user(
            email="taken@example.com", password="hunter2-strong", client_ip="203.0.113.3"
        )
    after = await get_control_database()["tenants"].count_documents(
        {"tenant_id": {"$regex": f"^{prefix}"}}
    )
    # The duplicate is rejected BEFORE provisioning, so no tenant is orphaned.
    assert after == before == 0


# --------------------------------------------------------------------------- #
#  Public endpoint mapping                                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_endpoint_returns_404_when_disabled(reset_settings, patch_mongo):
    resp = await auth_router.register(
        _RegRequest({"email": "a@b.co", "password": "hunter2-strong"})
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_happy_path_returns_201(reset_settings, patch_mongo):
    _enable_registration()
    resp = await auth_router.register(
        _RegRequest({"email": "creator@example.com", "password": "hunter2-strong"})
    )
    assert resp.status_code == 201
    body = json.loads(resp.body)
    assert body["confirmation"] == CONFIRMATION_UNCONFIRMED
    assert body["token"]
    assert body["user"]["self_registered"] is True
    assert body["tenant_id"].startswith(get_settings().self_registration_tenant_prefix)


@pytest.mark.asyncio
async def test_endpoint_validation_returns_422(reset_settings, patch_mongo):
    _enable_registration()
    resp = await auth_router.register(
        _RegRequest({"email": "creator@example.com", "password": "x"})
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_duplicate_returns_409(reset_settings, patch_mongo):
    _enable_registration(self_registration_max_per_ip=0)
    await users_service.create_user(
        email="dup@example.com", password="x", tenant_id="local-dev", roles=["user"]
    )
    resp = await auth_router.register(
        _RegRequest({"email": "dup@example.com", "password": "hunter2-strong"})
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_endpoint_throttle_returns_429_with_retry_after(reset_settings, patch_mongo):
    _enable_registration(self_registration_max_per_ip=1, self_registration_window_seconds=3600)
    ok = await auth_router.register(
        _RegRequest(
            {"email": "a@example.com", "password": "hunter2-strong"}, client_ip="203.0.113.55"
        )
    )
    assert ok.status_code == 201
    throttled = await auth_router.register(
        _RegRequest(
            {"email": "b@example.com", "password": "hunter2-strong"}, client_ip="203.0.113.55"
        )
    )
    assert throttled.status_code == 429
    assert "Retry-After" in throttled.headers


# --------------------------------------------------------------------------- #
#  Tier enforcement at server registration                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unconfirmed_blocked_from_non_code_transport(reset_settings, patch_mongo):
    await _seed_tenant("acct", confirmation=CONFIRMATION_UNCONFIRMED)
    req = _Req(roles=["admin"], tenant_id="acct")
    doc = {"server": "ext", "transport": "streamable_http", "endpoint": "https://x", "tools": []}
    with pytest.raises(HTTPException) as exc:
        await servers_router._enforce_account_tier(req, "acct", doc)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_unconfirmed_allows_code_transport(reset_settings, patch_mongo):
    await _seed_tenant("acct", confirmation=CONFIRMATION_UNCONFIRMED)
    req = _Req(roles=["admin"], tenant_id="acct")
    doc = {"server": "calc", "transport": "code", "tools": [{"name": "a"}]}
    # Within the 1-server / 1-tool caps -> no raise.
    await servers_router._enforce_account_tier(req, "acct", doc)


@pytest.mark.asyncio
async def test_unconfirmed_second_server_rejected(reset_settings, patch_mongo):
    await _seed_tenant("acct", confirmation=CONFIRMATION_UNCONFIRMED)
    await get_tenant_database("acct")["routing_registry"].insert_one(
        {"_id": "first", "server": "first", "origin": "tenant", "transport": "code"}
    )
    req = _Req(roles=["admin"], tenant_id="acct")
    doc = {"server": "second", "transport": "code", "tools": [{"name": "a"}]}
    with pytest.raises(HTTPException) as exc:
        await servers_router._enforce_account_tier(req, "acct", doc)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_unconfirmed_too_many_tools_rejected(reset_settings, patch_mongo):
    await _seed_tenant("acct", confirmation=CONFIRMATION_UNCONFIRMED)
    req = _Req(roles=["admin"], tenant_id="acct")
    doc = {"server": "calc", "transport": "code", "tools": [{"name": "a"}, {"name": "b"}]}
    with pytest.raises(HTTPException) as exc:
        await servers_router._enforce_account_tier(req, "acct", doc)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_platform_admin_bypasses_tier(reset_settings, patch_mongo):
    await _seed_tenant("acct", confirmation=CONFIRMATION_UNCONFIRMED)
    doc = {"server": "ext", "transport": "streamable_http", "endpoint": "https://x", "tools": []}
    # A platform-admin is never gated by tier caps, even on an unconfirmed tenant.
    await servers_router._enforce_account_tier(_platform_admin(), "acct", doc)


@pytest.mark.asyncio
async def test_confirmed_tenant_allows_external_transport(reset_settings, patch_mongo):
    await _seed_tenant("acct", confirmation=CONFIRMATION_CONFIRMED)
    req = _Req(roles=["admin"], tenant_id="acct")
    doc = {"server": "ext", "transport": "streamable_http", "endpoint": "https://x", "tools": []}
    await servers_router._enforce_account_tier(req, "acct", doc)


@pytest.mark.asyncio
async def test_unknown_tenant_defaults_to_confirmed(reset_settings, patch_mongo):
    # No control doc at all -> treated as confirmed (uncapped), so the tier is purely
    # additive and never breaks tenants that predate the feature.
    req = _Req(roles=["admin"], tenant_id="ghost")
    doc = {"server": "ext", "transport": "streamable_http", "endpoint": "https://x", "tools": []}
    await servers_router._enforce_account_tier(req, "ghost", doc)


@pytest.mark.asyncio
async def test_tier_enforced_end_to_end_via_create_server(reset_settings, patch_mongo, monkeypatch):
    from models.admin import ServerUpsertRequest

    async def fake_provision(tenant_id: str, wait_for_queryable_indexes: bool = True):
        return f"tenant_{tenant_id}"

    class _Registry:
        async def mount_or_update(self, doc):
            pass

        async def unmount(self, server_name, tenant_id=None):
            pass

    monkeypatch.setattr(admin._common, "provision_tenant", fake_provision)
    monkeypatch.setattr(admin._common, "get_proxy_registry", lambda: _Registry())
    await _seed_tenant("acct", confirmation=CONFIRMATION_UNCONFIRMED)

    payload = ServerUpsertRequest(
        server="ext",
        transport="streamable_http",
        endpoint="https://example.com/mcp",
        tools=[],
    )
    req = _Req(roles=["admin"], tenant_id="acct")
    with pytest.raises(HTTPException) as exc:
        await admin.create_or_update_server(req, payload)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
#  Admin confirm / unconfirm transitions                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_confirm_lifts_caps_and_restamps_quota(reset_settings, patch_mongo):
    await _seed_tenant("acct", confirmation=CONFIRMATION_UNCONFIRMED)

    res = await admin.confirm_tenant(_platform_admin(), "acct")
    assert res.confirmation == CONFIRMATION_CONFIRMED
    assert await get_tenant_confirmation("acct") == CONFIRMATION_CONFIRMED

    # Quota was re-stamped to the confirmed tier (0 => unlimited by default).
    quota = await get_effective_quota("acct")
    assert quota["calls_limit"] == get_settings().confirmed_quota_calls_per_period

    # And an external transport is now allowed.
    req = _Req(roles=["admin"], tenant_id="acct")
    doc = {"server": "ext", "transport": "streamable_http", "endpoint": "https://x", "tools": []}
    await servers_router._enforce_account_tier(req, "acct", doc)


@pytest.mark.asyncio
async def test_unconfirm_reverts_and_restamps_quota(reset_settings, patch_mongo):
    await _seed_tenant("acct", confirmation=CONFIRMATION_CONFIRMED)

    res = await admin.unconfirm_tenant(_platform_admin(), "acct")
    assert res.confirmation == CONFIRMATION_UNCONFIRMED
    assert await get_tenant_confirmation("acct") == CONFIRMATION_UNCONFIRMED

    quota = await get_effective_quota("acct")
    assert quota["calls_limit"] == get_settings().unconfirmed_quota_calls_per_period
    assert (
        quota["sandbox_seconds_limit"]
        == get_settings().unconfirmed_quota_sandbox_seconds_per_period
    )


@pytest.mark.asyncio
async def test_confirm_requires_platform_admin(reset_settings, patch_mongo):
    await _seed_tenant("acct", confirmation=CONFIRMATION_UNCONFIRMED)
    with pytest.raises(HTTPException) as exc:
        await admin.confirm_tenant(_Req(roles=["admin"], tenant_id="acct"), "acct")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_confirm_unknown_tenant_returns_404(reset_settings, patch_mongo):
    with pytest.raises(HTTPException) as exc:
        await admin.confirm_tenant(_platform_admin(), "nope")
    assert exc.value.status_code == 404
