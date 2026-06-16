from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from config.settings import Settings, get_settings
from database.mongo import get_control_database

TENANTS_COLLECTION = "tenants"

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_DELETED = "deleted"
VALID_STATUSES = frozenset({STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_DELETED})


class TenantInactiveError(Exception):
    """Base for hot-path failures where a tenant is not in the ``active`` state.

    Raised before any tool runs so callers can return a clear, protocol-safe
    error instead of executing tools or consuming resources for a tenant that is
    administratively suspended or soft-deleted. Subclasses carry a machine
    ``status_code`` and a top-level ``message`` so the one error handler on each
    surface can render the right frame without branching per status.
    """

    status_code: str = "tenant_inactive"
    message: str = "Tenant is not active."

    def __init__(self, tenant_id: str, reason: str | None = None) -> None:
        self.tenant_id = tenant_id
        self.reason = reason or self.message
        super().__init__(f"Tenant '{tenant_id}' is {self.status_code}: {self.reason}")


class TenantSuspendedError(TenantInactiveError):
    """A request targeted a tenant whose access is administratively suspended."""

    status_code = "tenant_suspended"
    message = "Tenant access is suspended."


class TenantDeletedError(TenantInactiveError):
    """A request targeted a tenant that has been soft-deleted.

    The tenant is locked out immediately on delete; its data lingers only until
    the retention window elapses and the purge reaper drops it.
    """

    status_code = "tenant_deleted"
    message = "Tenant has been deleted."


# Per-process status cache: {tenant_id: (status, monotonic_expiry)}. Suspension is
# an abuse kill-switch, so a short TTL keeps the hot path cheap while bounding
# cross-replica propagation delay to a few seconds. The acting replica updates its
# own cache on write so a suspend it issues takes effect immediately locally.
_status_cache: dict[str, tuple[str, float]] = {}


def reset_tenant_status_cache() -> None:
    """Clear the in-process status cache (used by tests and after a wipe)."""
    _status_cache.clear()


def _cache_ttl(settings: Settings) -> float:
    return max(0.0, float(settings.tenant_status_cache_ttl_seconds))


def _normalize_status(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized not in VALID_STATUSES:
        raise ValueError(
            f"Invalid tenant status '{status}'. Expected one of {sorted(VALID_STATUSES)}."
        )
    return normalized


async def get_tenant_status(tenant_id: str, *, settings: Settings | None = None) -> str:
    """Return a tenant's status, defaulting to active for unknown/unset tenants."""
    settings = settings or get_settings()
    ttl = _cache_ttl(settings)
    now = time.monotonic()
    cached = _status_cache.get(tenant_id)
    if cached is not None and cached[1] > now:
        return cached[0]

    doc = await get_control_database()[TENANTS_COLLECTION].find_one({"tenant_id": tenant_id})
    status = str((doc or {}).get("status", STATUS_ACTIVE)) or STATUS_ACTIVE
    if status not in VALID_STATUSES:
        status = STATUS_ACTIVE
    if ttl > 0:
        _status_cache[tenant_id] = (status, now + ttl)
    return status


async def assert_tenant_active(tenant_id: str, *, settings: Settings | None = None) -> None:
    """Raise when the tenant is not active (suspended or soft-deleted).

    Raises :class:`TenantSuspendedError` for a suspended tenant and
    :class:`TenantDeletedError` for a soft-deleted one; both subclass
    :class:`TenantInactiveError` so a single handler can map either to a frame.
    """
    settings = settings or get_settings()
    status = await get_tenant_status(tenant_id, settings=settings)
    if status == STATUS_ACTIVE:
        return
    if status == STATUS_DELETED:
        raise TenantDeletedError(tenant_id)
    # Only suspended remains; fetch the reason lazily (off the happy path).
    doc = await get_control_database()[TENANTS_COLLECTION].find_one({"tenant_id": tenant_id})
    reason = str((doc or {}).get("suspended_reason", "")) or None
    raise TenantSuspendedError(tenant_id, reason)


async def set_tenant_status(
    tenant_id: str,
    status: str,
    *,
    updated_by: str | None = None,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Set a tenant's status. Returns the updated control doc, or None if missing.

    ``extra`` carries additional ``$set`` fields applied atomically with the
    status change (e.g. ``deleted_at`` / ``purge_at`` for a soft-delete), so the
    status cache and the lifecycle metadata never land in two separate writes.
    """
    normalized = _normalize_status(status)
    now = datetime.now(UTC)
    updates: dict[str, Any] = {
        "status": normalized,
        "status_updated_at": now,
        "status_updated_by": str(updated_by or "admin"),
        "updated_at": now,
    }
    if normalized == STATUS_SUSPENDED:
        updates["suspended_reason"] = (reason or "").strip()
    else:
        updates["suspended_reason"] = ""
    if extra:
        updates.update(extra)
    doc = await get_control_database()[TENANTS_COLLECTION].find_one_and_update(
        {"tenant_id": tenant_id},
        {"$set": updates},
        return_document=True,
    )
    if doc is None:
        return None
    # Update the local cache immediately so the change is effective on this replica
    # without waiting for the TTL to lapse.
    ttl = _cache_ttl(get_settings())
    if ttl > 0:
        _status_cache[tenant_id] = (normalized, time.monotonic() + ttl)
    else:
        _status_cache.pop(tenant_id, None)
    return doc
