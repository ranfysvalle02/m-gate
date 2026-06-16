"""Per-tenant tool availability policy: allowlist, max-tools cap, disabled overlay.

This is the second control-plane overlay on top of a tenant's ``tool_catalog``
(the first being :mod:`services.tenant_status`). It lets an operator curate
*which* of a tenant's catalogued tools are discoverable and invocable without
ever mutating the catalog itself — the catalog is rebuilt from downstream
servers by the registry sync, so any per-tenant curation has to live outside it.

Three orthogonal controls live on the ``tenants`` control doc:

* ``tool_allowlist`` — fully-qualified ``server/name`` entries (or ``server/*``
  wildcards). **Empty means unrestricted** (opt-in curation), so existing
  tenants are unaffected until an operator opts in.
* ``max_tools`` — a cap enforced at server registration. ``0`` means unlimited.
* ``disabled_tools`` — a per-tool kill-switch overlay (``server/name`` entries).
  Distinct from the server-level ``enabled`` flag and from the allowlist: a
  disabled tool is always hidden/blocked even if allowlisted.

A tool is *available* (discoverable + invocable) when its ``server/name`` is not
in ``disabled_tools`` **and** (the allowlist is empty **or** it matches the
allowlist). Server-level enable/disable is enforced separately by the routing
registry; this module only governs the tool-level overlay.

The policy is read on the hot path (every discovery + every ``tools/call``), so
it is cached per process with the same short TTL and write-through behavior as
the tenant-status cache.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from config.settings import Settings, get_settings
from database.mongo import get_control_database

TENANTS_COLLECTION = "tenants"


# Per-process policy cache: {tenant_id: (policy, monotonic_expiry)}. Mirrors the
# tenant-status cache (shared TTL, write-through on every setter) so curation is
# effective immediately on the acting replica and bounded elsewhere by the TTL.
_policy_cache: dict[str, tuple[dict[str, Any], float]] = {}


def reset_tenant_tool_policy_cache() -> None:
    """Clear the in-process tool-policy cache (used by tests and after a wipe)."""
    _policy_cache.clear()


def _cache_ttl(settings: Settings) -> float:
    return max(0.0, float(settings.tenant_status_cache_ttl_seconds))


def _clean_entries(values: Iterable[Any] | None) -> list[str]:
    """De-dupe, strip, and sort a list of ``server/name`` entries."""
    return sorted({str(v).strip() for v in (values or []) if str(v).strip()})


def _coerce_cap(value: Any) -> int:
    try:
        cap = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return cap if cap > 0 else 0


def _normalize_policy(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "allowlist": _clean_entries(doc.get("tool_allowlist")),
        "max_tools": _coerce_cap(doc.get("max_tools")),
        "disabled_tools": _clean_entries(doc.get("disabled_tools")),
    }


def _store_cache(tenant_id: str, doc: Mapping[str, Any], settings: Settings) -> dict[str, Any]:
    policy = _normalize_policy(doc)
    ttl = _cache_ttl(settings)
    if ttl > 0:
        _policy_cache[tenant_id] = (policy, time.monotonic() + ttl)
    else:
        _policy_cache.pop(tenant_id, None)
    return policy


async def get_tool_policy(tenant_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Return the tenant's normalized tool policy (cache-first).

    Shape: ``{"allowlist": [...], "max_tools": int, "disabled_tools": [...]}``.
    Unknown/unset tenants get the permissive default (empty allowlist, no cap, no
    disabled tools).
    """
    settings = settings or get_settings()
    ttl = _cache_ttl(settings)
    now = time.monotonic()
    cached = _policy_cache.get(tenant_id)
    if cached is not None and cached[1] > now:
        return cached[0]

    doc = await get_control_database()[TENANTS_COLLECTION].find_one({"tenant_id": tenant_id})
    policy = _normalize_policy(doc or {})
    if ttl > 0:
        _policy_cache[tenant_id] = (policy, now + ttl)
    return policy


def matches_allowlist(server: str, name: str, allowlist: Sequence[str]) -> bool:
    """True when ``server/name`` is permitted by ``allowlist``.

    An empty allowlist permits everything (unrestricted). Otherwise the entry
    must match exactly (``server/name``) or via a server wildcard (``server/*``).
    """
    if not allowlist:
        return True
    return f"{server}/{name}" in allowlist or f"{server}/*" in allowlist


def is_tool_available(policy: Mapping[str, Any], server: str, name: str) -> bool:
    """True when a tool is neither disabled nor excluded by the allowlist."""
    if f"{server}/{name}" in policy.get("disabled_tools", []):
        return False
    return matches_allowlist(server, name, policy.get("allowlist", []))


async def filter_available_tools(
    tenant_id: str,
    tools: Sequence[dict[str, Any]],
    *,
    settings: Settings | None = None,
    server_key: str = "server",
    name_key: str = "name",
) -> list[dict[str, Any]]:
    """Drop tools the tenant policy hides (disabled or not allowlisted).

    Operates on catalog/search result dicts that carry ``server`` and ``name``
    keys (the concrete type every caller passes). Returns the input unchanged
    when nothing restricts the tenant (the common case), so the curation path
    adds no per-item cost until opted into.
    """
    policy = await get_tool_policy(tenant_id, settings=settings)
    if not policy["allowlist"] and not policy["disabled_tools"]:
        return list(tools)
    return [
        tool
        for tool in tools
        if is_tool_available(policy, str(tool.get(server_key, "")), str(tool.get(name_key, "")))
    ]


async def set_tool_policy(
    tenant_id: str,
    *,
    allowlist: Iterable[str] | None,
    max_tools: int,
    updated_by: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Replace the tenant's allowlist + max-tools cap. Returns the updated doc.

    Leaves ``disabled_tools`` untouched (that overlay has its own setter). The
    local cache is refreshed write-through so curation takes effect immediately.
    """
    settings = settings or get_settings()
    now = datetime.now(UTC)
    updates: dict[str, Any] = {
        "tool_allowlist": _clean_entries(allowlist),
        "max_tools": _coerce_cap(max_tools),
        "tool_policy_updated_at": now,
        "tool_policy_updated_by": str(updated_by or "admin"),
        "updated_at": now,
    }
    doc = await get_control_database()[TENANTS_COLLECTION].find_one_and_update(
        {"tenant_id": tenant_id},
        {"$set": updates},
        return_document=True,
    )
    if doc is None:
        return None
    _store_cache(tenant_id, doc, settings)
    return doc


async def set_tool_enabled(
    tenant_id: str,
    server: str,
    name: str,
    enabled: bool,
    *,
    updated_by: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Toggle a single tool's per-tenant disabled overlay. Returns the updated doc.

    Enabling removes the ``server/name`` entry from ``disabled_tools``; disabling
    adds it. The cache is refreshed write-through.
    """
    settings = settings or get_settings()
    now = datetime.now(UTC)
    fq_name = f"{server}/{name}"
    audit: dict[str, Any] = {
        "tool_policy_updated_at": now,
        "tool_policy_updated_by": str(updated_by or "admin"),
        "updated_at": now,
    }
    if enabled:
        update: dict[str, Any] = {"$pull": {"disabled_tools": fq_name}, "$set": audit}
    else:
        update = {"$addToSet": {"disabled_tools": fq_name}, "$set": audit}
    doc = await get_control_database()[TENANTS_COLLECTION].find_one_and_update(
        {"tenant_id": tenant_id},
        update,
        return_document=True,
    )
    if doc is None:
        return None
    _store_cache(tenant_id, doc, settings)
    return doc
