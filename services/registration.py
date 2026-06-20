"""Public self-service registration for the open beta.

Orchestrates the one path that lets an anonymous caller create an account
without an admin in the loop. Every privilege-bearing field (roles, scopes,
tenant id, confirmation tier) is pinned server-side here — the public endpoint in
:mod:`gateway.routers.auth` only forwards an email + password — so a registrant
can never escalate beyond a tenant-admin of its own freshly-provisioned,
tightly-capped tenant.

Abuse controls, layered:

* ``self_registration_enabled`` master switch (off => the flow is unreachable).
* a per-IP sign-up throttle backed by the control DB (the one public, pre-tenant
  endpoint the tenant-scoped rate limiter cannot key on),
* a global beta cap on the number of self-registered tenants (each tenant is a
  MongoDB database, so this bounds the Atlas-namespace footprint),
* the ``unconfirmed`` confirmation tier (see :mod:`services.account_tier`), which
  confines a fresh sign-up to the network-isolated code sandbox with a tiny quota
  until a platform-admin promotes it.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pymongo import ReturnDocument

from config.settings import Settings, get_settings
from database.mongo import get_control_database
from services import users as users_service
from services.account_tier import (
    CONFIRMATION_UNCONFIRMED,
    set_tenant_confirmation,
    tier_caps,
)
from services.admin_session import mint_bearer_jwt, mint_session
from services.tenant_provisioner import deprovision_tenant, provision_tenant
from services.usage_metering import set_quota
from services.users import SELF_SERVICE_ROLES, normalize_email

logger = logging.getLogger(__name__)

REGISTRATION_ATTEMPTS_COLLECTION = "registration_attempts"
SELF_REGISTRATION_ACTOR = "self-registration"

# Pragmatic, dependency-free email shape check (the endpoint is public so it
# validates a floor; it is not a full RFC 5322 validator).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegistrationError(Exception):
    """Base class for self-registration failures."""


class RegistrationDisabled(RegistrationError):
    """Self-registration is turned off (``self_registration_enabled=false``)."""


class RegistrationValidationError(RegistrationError):
    """The submitted email or password failed validation."""


class RegistrationThrottled(RegistrationError):
    """Too many sign-ups from this client IP within the throttle window."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__("Too many registration attempts; please try again later.")


class BetaFull(RegistrationError):
    """The global self-registration tenant cap has been reached."""


@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of a successful registration, including a ready-to-use token."""

    user: dict[str, Any]
    tenant_id: str
    confirmation: str
    auth_mode: str
    token: str
    token_type: str
    expires_in: int


def _validate_credentials(email: str, password: str, settings: Settings) -> str:
    normalized = normalize_email(email)
    if not normalized or not _EMAIL_RE.match(normalized):
        raise RegistrationValidationError("A valid email address is required.")
    min_len = max(1, int(settings.self_registration_min_password_length))
    if len(password or "") < min_len:
        raise RegistrationValidationError(f"Password must be at least {min_len} characters.")
    return normalized


async def _enforce_ip_throttle(client_ip: str, settings: Settings) -> None:
    """Fixed-window per-IP sign-up throttle backed by the control DB.

    Mirrors the rate-limiter's bucket shape (``$inc`` on an upserted
    ``(client_ip, window_epoch)`` doc with a TTL ``expires_at``) so it works
    across replicas without in-process state. A short fixed window is sufficient
    here — this guards account creation, not steady request traffic.
    """
    window = max(1, int(settings.self_registration_window_seconds))
    limit = max(0, int(settings.self_registration_max_per_ip))
    if limit <= 0:
        return
    now = datetime.now(UTC)
    now_ts = now.timestamp()
    window_epoch = int(now_ts // window) * window
    window_end = window_epoch + window
    # Keep the bucket one extra window so a late attempt in the same window still
    # sees the count before TTL cleanup reaps it.
    expires_at = datetime.fromtimestamp(window_end + window, tz=UTC)
    collection = get_control_database()[REGISTRATION_ATTEMPTS_COLLECTION]
    bucket = await collection.find_one_and_update(
        {"client_ip": client_ip, "window_epoch": window_epoch},
        {
            "$inc": {"count": 1},
            "$setOnInsert": {
                "client_ip": client_ip,
                "window_epoch": window_epoch,
                "created_at": now,
            },
            "$set": {"updated_at": now, "expires_at": expires_at},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    if int((bucket or {}).get("count", 1)) > limit:
        raise RegistrationThrottled(retry_after_seconds=int(window_end - now_ts))


async def _enforce_beta_cap(settings: Settings) -> None:
    cap = max(0, int(settings.self_registration_max_tenants))
    if cap <= 0:
        return
    count = await get_control_database()[users_service.USERS_COLLECTION].count_documents(
        {"self_registered": True}
    )
    if count >= cap:
        raise BetaFull("The beta is currently full; new sign-ups are paused.")


def _mint_token(
    *, email: str, tenant_id: str, scopes: list[str], settings: Settings
) -> tuple[str, int]:
    """Mint an auth-mode-aware credential, mirroring POST /auth/token."""
    expires_in = settings.admin_session_ttl_seconds
    if settings.auth_mode == "jwks":
        # The gateway cannot forge a token its IdP-backed verifier trusts, so fall
        # back to a roles-only admin-session token (accepted on /rpc + /mcp).
        token = mint_session(email, tenant_id=tenant_id, roles=list(SELF_SERVICE_ROLES))
    else:  # hs256 -- a real, scoped data-plane bearer.
        token = mint_bearer_jwt(
            email,
            tenant_id=tenant_id,
            roles=list(SELF_SERVICE_ROLES),
            scopes=scopes,
            ttl_seconds=expires_in,
        )
    return token, expires_in


async def register_self_service_user(
    *,
    email: str,
    password: str,
    client_ip: str,
    settings: Settings | None = None,
) -> RegistrationResult:
    """Create an instantly-active, unconfirmed tenant-admin in its own tenant.

    Raises a :class:`RegistrationError` subclass that the endpoint maps to the
    right HTTP status (disabled => 404, throttled => 429, beta full => 403,
    duplicate email => 409, validation => 422).
    """
    settings = settings or get_settings()
    if not settings.self_registration_enabled:
        raise RegistrationDisabled("Self-service registration is disabled.")

    normalized = _validate_credentials(email, password, settings)
    await _enforce_ip_throttle(client_ip, settings)
    await _enforce_beta_cap(settings)

    # Pre-check uniqueness BEFORE provisioning so a duplicate email never orphans a
    # freshly-created tenant database (create_user re-checks under the unique index).
    if await users_service.find_user_by_email(normalized) is not None:
        raise users_service.UserAlreadyExists(f"A user with email '{normalized}' already exists.")

    tenant_id = f"{settings.self_registration_tenant_prefix}{uuid.uuid4().hex[:12]}"
    scopes = ["server:*"]
    caps = tier_caps(CONFIRMATION_UNCONFIRMED, settings=settings)

    await provision_tenant(tenant_id, wait_for_queryable_indexes=False)
    try:
        # Stamp the unconfirmed tier BEFORE the user can authenticate. The tier
        # defaults to ``confirmed`` (uncapped) when unset, so failing to stamp it
        # would fail OPEN — stamping first (and rolling back the tenant on any
        # error here) keeps a half-finished sign-up fail-CLOSED (capped).
        await set_tenant_confirmation(
            tenant_id, CONFIRMATION_UNCONFIRMED, updated_by=SELF_REGISTRATION_ACTOR
        )
        user = await users_service.create_user(
            email=normalized,
            password=password,
            tenant_id=tenant_id,
            roles=list(SELF_SERVICE_ROLES),
            scopes=scopes,
            status="active",
            created_by=SELF_REGISTRATION_ACTOR,
            self_registered=True,
        )
    except users_service.UserAlreadyExists:
        # Lost a race against a concurrent sign-up for the same email after we
        # provisioned: roll back the just-created tenant so it is not orphaned.
        await _rollback_tenant(tenant_id)
        raise
    except Exception:
        await _rollback_tenant(tenant_id)
        raise

    await set_quota(
        tenant_id,
        calls_limit=caps.quota_calls_per_period,
        sandbox_seconds_limit=caps.quota_sandbox_seconds_per_period,
        updated_by=SELF_REGISTRATION_ACTOR,
    )
    # Mirror roles/scopes/status into session_context so the /rpc + /mcp kill-switch
    # and role hydration work on the very first request with the minted token.
    await users_service.sync_session_context(user)

    # Give the fresh tenant a runnable starter tool so its /mcp endpoint is never
    # empty on first connect. Best-effort and fail-soft (seed_starter_server never
    # raises): a seeding/embedding hiccup must not break a successful sign-up.
    if settings.seed_starter_tools_on_register:
        from services.starter_seed import seed_starter_server

        await seed_starter_server(tenant_id, settings=settings)

    token, expires_in = _mint_token(
        email=normalized, tenant_id=tenant_id, scopes=scopes, settings=settings
    )
    logger.info(
        "Self-service account registered: tenant=%s email=%s confirmation=%s",
        tenant_id,
        normalized,
        CONFIRMATION_UNCONFIRMED,
    )
    return RegistrationResult(
        user=user,
        tenant_id=tenant_id,
        confirmation=CONFIRMATION_UNCONFIRMED,
        auth_mode=settings.auth_mode,
        token=token,
        token_type="bearer",
        expires_in=expires_in,
    )


async def _rollback_tenant(tenant_id: str) -> None:
    try:
        await deprovision_tenant(tenant_id)
    except Exception:  # never mask the original error with a cleanup failure
        logger.error(
            "Failed to roll back tenant '%s' after a failed registration; it may "
            "need manual cleanup.",
            tenant_id,
            exc_info=True,
        )
