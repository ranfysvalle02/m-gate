"""Account confirmation tier: per-tenant trust state and the caps it implies.

A third control-plane overlay on the ``tenants`` control doc (alongside
:mod:`services.tenant_status` and :mod:`services.tenant_tool_policy`). It records
whether a tenant is ``unconfirmed`` (a fresh self-service sign-up, instantly
active but tightly capped) or ``confirmed`` (promoted by a platform-admin), and
derives the resource caps each tier imposes.

Design notes:

* **Default is** ``confirmed``. Unset/unknown tenants — every tenant that
  predates this feature, plus the env bootstrap admin and admin-created tenants —
  are treated as confirmed (uncapped), so introducing the tier changes nothing
  for them. Only the self-registration path explicitly stamps ``unconfirmed``.
* The confirmation state is read on the server-registration hot path, so it is
  cached per process with the same short TTL and write-through behavior as the
  tenant-status / tool-policy caches.
* The dangerous capability gated by confirmation is registering an *external*
  downstream server (``streamable_http``/``sse``/``stdio``) — the gateway then
  dials that endpoint, an SSRF / egress-abuse vector. Unconfirmed accounts are
  confined to ``code`` servers, which run in the network-isolated wasm sandbox.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from config.settings import Settings, get_settings
from database.mongo import get_control_database

TENANTS_COLLECTION = "tenants"

CONFIRMATION_UNCONFIRMED = "unconfirmed"
CONFIRMATION_CONFIRMED = "confirmed"
VALID_CONFIRMATIONS = frozenset({CONFIRMATION_UNCONFIRMED, CONFIRMATION_CONFIRMED})

# Unset/unknown tenants are confirmed (uncapped) so the tier is purely additive.
DEFAULT_CONFIRMATION = CONFIRMATION_CONFIRMED


@dataclass(frozen=True)
class TierCaps:
    """Resource ceilings implied by an account's confirmation tier.

    ``max_servers`` / ``max_tools`` of ``0`` mean unlimited. ``allowed_transports``
    is the set of downstream transports the tier may register. The quota fields
    are stamped onto the tenant's per-tenant quota at registration / confirmation.
    """

    max_servers: int
    max_tools: int
    allowed_transports: frozenset[str]
    quota_calls_per_period: int
    quota_sandbox_seconds_per_period: int


# Per-process confirmation cache: {tenant_id: (confirmation, monotonic_expiry)}.
# Mirrors the tenant-status cache (same TTL, write-through on every setter) so the
# server-registration gate stays cheap and a promotion is effective immediately on
# the acting replica, bounded elsewhere by the TTL.
_confirmation_cache: dict[str, tuple[str, float]] = {}


def reset_account_tier_cache() -> None:
    """Clear the in-process confirmation cache (used by tests and after a wipe)."""
    _confirmation_cache.clear()


def _cache_ttl(settings: Settings) -> float:
    return max(0.0, float(settings.tenant_status_cache_ttl_seconds))


def _normalize_confirmation(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_CONFIRMATIONS else DEFAULT_CONFIRMATION


def _parse_transports(raw: str) -> frozenset[str]:
    """Parse a comma/space separated transport allowlist into a set."""
    parts = [piece.strip() for piece in raw.replace(",", " ").split()]
    return frozenset(part for part in parts if part)


def tier_caps(confirmation: str, *, settings: Settings | None = None) -> TierCaps:
    """Return the :class:`TierCaps` implied by a confirmation value."""
    settings = settings or get_settings()
    if _normalize_confirmation(confirmation) == CONFIRMATION_UNCONFIRMED:
        return TierCaps(
            max_servers=max(0, int(settings.unconfirmed_max_servers)),
            max_tools=max(0, int(settings.unconfirmed_max_tools)),
            allowed_transports=_parse_transports(settings.unconfirmed_allowed_transports),
            quota_calls_per_period=max(0, int(settings.unconfirmed_quota_calls_per_period)),
            quota_sandbox_seconds_per_period=max(
                0, int(settings.unconfirmed_quota_sandbox_seconds_per_period)
            ),
        )
    return TierCaps(
        max_servers=max(0, int(settings.confirmed_max_servers)),
        max_tools=max(0, int(settings.confirmed_max_tools)),
        allowed_transports=_parse_transports(settings.confirmed_allowed_transports),
        quota_calls_per_period=max(0, int(settings.confirmed_quota_calls_per_period)),
        quota_sandbox_seconds_per_period=max(
            0, int(settings.confirmed_quota_sandbox_seconds_per_period)
        ),
    )


async def get_tenant_confirmation(tenant_id: str, *, settings: Settings | None = None) -> str:
    """Return a tenant's confirmation tier, defaulting to confirmed when unset.

    Cache-first, mirroring :func:`services.tenant_status.get_tenant_status`, so the
    server-registration gate adds no extra round trip once warm.
    """
    settings = settings or get_settings()
    ttl = _cache_ttl(settings)
    now = time.monotonic()
    cached = _confirmation_cache.get(tenant_id)
    if cached is not None and cached[1] > now:
        return cached[0]

    doc = await get_control_database()[TENANTS_COLLECTION].find_one({"tenant_id": tenant_id})
    confirmation = _normalize_confirmation((doc or {}).get("confirmation"))
    if ttl > 0:
        _confirmation_cache[tenant_id] = (confirmation, now + ttl)
    return confirmation


async def get_tenant_tier_caps(tenant_id: str, *, settings: Settings | None = None) -> TierCaps:
    """Convenience: resolve a tenant's confirmation and return its caps."""
    settings = settings or get_settings()
    confirmation = await get_tenant_confirmation(tenant_id, settings=settings)
    return tier_caps(confirmation, settings=settings)


async def set_tenant_confirmation(
    tenant_id: str,
    confirmation: str,
    *,
    updated_by: str | None = None,
) -> dict[str, Any] | None:
    """Set a tenant's confirmation tier. Returns the updated doc, or None if missing.

    Writes the value plus audit fields atomically, then refreshes the local cache
    write-through so the change is effective immediately on this replica (same
    approach as :func:`services.tenant_status.set_tenant_status`).
    """
    normalized = _normalize_confirmation(confirmation)
    if str(confirmation or "").strip().lower() not in VALID_CONFIRMATIONS:
        raise ValueError(
            f"Invalid confirmation '{confirmation}'. Expected one of {sorted(VALID_CONFIRMATIONS)}."
        )
    now = datetime.now(UTC)
    updates: dict[str, Any] = {
        "confirmation": normalized,
        "confirmation_updated_at": now,
        "confirmation_updated_by": str(updated_by or "admin"),
        "updated_at": now,
    }
    doc = await get_control_database()[TENANTS_COLLECTION].find_one_and_update(
        {"tenant_id": tenant_id},
        {"$set": updates},
        return_document=True,
    )
    if doc is None:
        return None
    ttl = _cache_ttl(get_settings())
    if ttl > 0:
        _confirmation_cache[tenant_id] = (normalized, time.monotonic() + ttl)
    else:
        _confirmation_cache.pop(tenant_id, None)
    return doc
