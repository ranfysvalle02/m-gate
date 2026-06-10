"""Per-tenant downstream egress allowlist storage.

The allowlist is stored on the tenant's control-plane document (the same
``tenants`` collection used by :mod:`services.tenant_status`) as a list of host
globs / exact hosts / IP literals / CIDRs. A short per-replica TTL cache keeps
the connect-time hot path cheap while bounding cross-replica propagation delay;
the acting replica updates its own cache immediately on write.

The global operator guardrail lives in settings (``EGRESS_GLOBAL_ALLOWLIST``);
this module only owns the *tenant* layer.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from config.settings import Settings, get_settings
from database.mongo import get_control_database
from services.egress_policy import validate_entries
from services.tenant_status import TENANTS_COLLECTION

# Per-process cache: {tenant_id: (allowlist, monotonic_expiry)}.
_allowlist_cache: dict[str, tuple[list[str], float]] = {}


def reset_tenant_egress_cache() -> None:
    """Clear the in-process allowlist cache (used by tests and after a wipe)."""
    _allowlist_cache.clear()


def _cache_ttl(settings: Settings) -> float:
    return max(0.0, float(settings.egress_allowlist_cache_ttl_seconds))


def _coerce_entries(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


async def get_tenant_egress_allowlist(
    tenant_id: str, *, settings: Settings | None = None
) -> list[str]:
    """Return a tenant's configured egress allowlist (empty when unset)."""
    settings = settings or get_settings()
    ttl = _cache_ttl(settings)
    now = time.monotonic()
    cached = _allowlist_cache.get(tenant_id)
    if cached is not None and cached[1] > now:
        return list(cached[0])

    doc = await get_control_database()[TENANTS_COLLECTION].find_one({"tenant_id": tenant_id})
    entries = _coerce_entries((doc or {}).get("egress_allowlist"))
    if ttl > 0:
        _allowlist_cache[tenant_id] = (list(entries), now + ttl)
    return entries


async def set_tenant_egress_allowlist(
    tenant_id: str,
    entries: list[str],
    *,
    updated_by: str | None = None,
) -> dict[str, Any] | None:
    """Persist a tenant's egress allowlist. Returns the doc, or None if missing.

    Entries are validated + normalized (raising :class:`EgressNotAllowed` on a
    malformed entry) so an invalid policy can never be stored.
    """
    normalized = validate_entries(entries)
    now = datetime.now(UTC)
    doc = await get_control_database()[TENANTS_COLLECTION].find_one_and_update(
        {"tenant_id": tenant_id},
        {
            "$set": {
                "egress_allowlist": normalized,
                "egress_allowlist_updated_at": now,
                "egress_allowlist_updated_by": str(updated_by or "admin"),
                "updated_at": now,
            }
        },
        return_document=True,
    )
    if doc is None:
        return None
    ttl = _cache_ttl(get_settings())
    if ttl > 0:
        _allowlist_cache[tenant_id] = (list(normalized), time.monotonic() + ttl)
    else:
        _allowlist_cache.pop(tenant_id, None)
    return doc
