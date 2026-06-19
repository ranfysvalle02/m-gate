"""Per-tenant code-tool dependency (pip) policy.

A fourth control-plane overlay on the ``tenants`` control doc (alongside
:mod:`services.tenant_status`, :mod:`services.tenant_tool_policy`, and
:mod:`services.account_tier`). It governs *which* third-party PyPI distributions
a tenant's code tools may install into the wasm sandbox.

Two layers, intersected:

* **Global ceiling** — ``SANDBOX_ALLOWED_REQUIREMENTS`` (operator env, parsed
  here via :func:`global_ceiling_names`). The hard operator guardrail: host pip
  runs *outside* the wasm jail, so only distributions an operator vetted may ever
  be fetched, and only as prebuilt wheels.
* **Tenant allowlist** — ``code_requirements_allowlist`` on the tenant control
  doc, a list of bare distribution names a tenant admin curates.

The **effective policy is the intersection** ``tenant_allowlist ∩ global_ceiling``
(see :func:`effective_allowlist`). This is a deliberate **hard break** from the
previous global-only behavior:

* An **empty tenant allowlist means no third-party installs** (stdlib only),
  regardless of how permissive the global ceiling is. Curation is opt-in per
  tenant, and unset/legacy tenants start locked down — fail-closed.
* A tenant entry that is *not* in the global ceiling has **no effect** (it cannot
  widen past the operator ceiling); the UI surfaces it as "awaiting operator".

The same intersection is enforced everywhere a requirement can enter the system —
authoring lint/validate, server save, the sandbox workbench test-run, and the
runtime install in :mod:`services.sandbox_executor` — so what an author sees while
typing is exactly what the runtime permits.

Read on the (cached) authoring + runtime paths, so the policy is cached per
process with the same short TTL and write-through behavior as the tenant-status /
tool-policy / account-tier caches.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from config.settings import Settings, get_settings
from database.mongo import get_control_database

TENANTS_COLLECTION = "tenants"

# A bare PyPI distribution name (PEP 508 name grammar). No version pin, extras,
# URL, VCS ref, path, or whitespace — the tenant allowlist curates *distributions*,
# while the exact ``==`` pin lives on each tool's ``requirements``.
_DISTRIBUTION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Per-process allowlist cache: {tenant_id: (allowlist, monotonic_expiry)}. Mirrors
# the tenant-status cache (shared TTL, write-through on every setter) so curation
# is effective immediately on the acting replica and bounded elsewhere by the TTL.
_allowlist_cache: dict[str, tuple[list[str], float]] = {}


class PipPolicyError(ValueError):
    """A tenant pip-policy allowlist entry was malformed and cannot be stored."""


def reset_tenant_pip_policy_cache() -> None:
    """Clear the in-process pip-policy cache (used by tests and after a wipe)."""
    _allowlist_cache.clear()


def _cache_ttl(settings: Settings) -> float:
    return max(0.0, float(settings.tenant_status_cache_ttl_seconds))


def normalize_requirement_name(spec: Any) -> str:
    """Normalize a requirement (or bare name) to its PEP 503 distribution name.

    Strips any ``==`` version pin and ``[extras]``, then lowercases and collapses
    ``-_.`` runs so allowlist matching is spelling-insensitive. This is the single
    source of truth shared by the authoring lint, the admin policy editor, and the
    sandbox executor, so a name matches identically in every layer.
    """
    base = str(spec).split("==", 1)[0].split("[", 1)[0].strip()
    if not base:
        return ""
    return re.sub(r"[-_.]+", "-", base).lower()


def _split_tokens(raw: str) -> list[str]:
    """Split a comma/whitespace-separated requirement string into raw tokens."""
    return [token for token in re.split(r"[,\s]+", raw or "") if token.strip()]


def global_ceiling_names(settings: Settings | None = None) -> set[str]:
    """Operator ceiling: normalized distribution names from ``SANDBOX_ALLOWED_REQUIREMENTS``.

    Empty when the operator has not allowlisted anything — in which case no
    third-party install is ever permitted for any tenant (stdlib only).
    """
    settings = settings or get_settings()
    names: set[str] = set()
    for token in _split_tokens(settings.sandbox_allowed_requirements or ""):
        name = normalize_requirement_name(token)
        if name:
            names.add(name)
    return names


def validate_allowlist_entries(entries: Iterable[Any] | None) -> list[str]:
    """Normalize + validate tenant allowlist entries; sorted, de-duped, lowercased.

    Each entry must be a bare distribution name (``requests``, ``Pillow``); a
    pinned spec, URL, path, or extras is rejected so the policy can never store an
    ambiguous or unsafe entry. Raises :class:`PipPolicyError` on the first bad one.
    """
    names: set[str] = set()
    for raw in entries or []:
        token = str(raw).strip()
        if not token:
            continue
        if not _DISTRIBUTION_NAME_RE.match(token):
            raise PipPolicyError(
                f"Invalid package '{token}'. List bare distribution names like "
                "'requests' — no version pins (==), extras ([...]), URLs, or paths."
            )
        names.add(normalize_requirement_name(token))
    return sorted(names)


def _coerce_stored_allowlist(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: set[str] = set()
    for item in value:
        name = normalize_requirement_name(item) if isinstance(item, str) else ""
        if name:
            names.add(name)
    return sorted(names)


def effective_allowlist(
    tenant_allowlist: Iterable[str], *, settings: Settings | None = None
) -> list[str]:
    """The intersection ``tenant_allowlist ∩ global_ceiling`` — what actually installs."""
    settings = settings or get_settings()
    ceiling = global_ceiling_names(settings)
    tenant = {normalize_requirement_name(name) for name in tenant_allowlist}
    tenant.discard("")
    return sorted(tenant & ceiling)


@dataclass(frozen=True)
class PipPolicyDecision:
    """Outcome of evaluating a tool's requirements against the effective policy.

    ``requested`` is the de-duped set of normalized distribution names asked for.
    A requirement is ``blocked_by_global`` when the operator ceiling does not
    include it, or ``blocked_by_tenant`` when the ceiling allows it but the tenant
    has not curated it. The split lets each surface tell the actor *who* can
    unblock it (a platform operator vs. a tenant admin).
    """

    requested: tuple[str, ...]
    allowed: tuple[str, ...]
    blocked_by_global: tuple[str, ...]
    blocked_by_tenant: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.blocked_by_global and not self.blocked_by_tenant

    def error_message(self) -> str:
        """A single, actor-targeted rejection string for save/test/runtime errors."""
        parts: list[str] = []
        if self.blocked_by_global:
            parts.append(
                "not permitted by the platform pip ceiling "
                f"(SANDBOX_ALLOWED_REQUIREMENTS): {', '.join(self.blocked_by_global)} "
                "(a platform operator must allow them)"
            )
        if self.blocked_by_tenant:
            parts.append(
                "not in this tenant's code-package policy: "
                f"{', '.join(self.blocked_by_tenant)} "
                "(a tenant admin must add them to the tenant's allowed packages)"
            )
        return "Code-tool requirement(s) " + "; ".join(parts) + "."


def evaluate_requirements(
    requirements: Iterable[Any],
    *,
    tenant_allowlist: Iterable[str],
    settings: Settings | None = None,
) -> PipPolicyDecision:
    """Classify each requirement against the effective ``tenant ∩ global`` policy.

    Pure given an explicit ``tenant_allowlist`` (so it is reused identically by the
    runtime — which is handed the tenant's effective list — and by the authoring
    paths). Order-preserving and de-duped by normalized name.
    """
    settings = settings or get_settings()
    ceiling = global_ceiling_names(settings)
    tenant = {normalize_requirement_name(name) for name in tenant_allowlist}
    tenant.discard("")

    requested: list[str] = []
    seen: set[str] = set()
    for spec in requirements:
        name = normalize_requirement_name(spec)
        if not name or name in seen:
            continue
        seen.add(name)
        requested.append(name)

    allowed: list[str] = []
    blocked_by_global: list[str] = []
    blocked_by_tenant: list[str] = []
    for name in requested:
        if name not in ceiling:
            blocked_by_global.append(name)
        elif name not in tenant:
            blocked_by_tenant.append(name)
        else:
            allowed.append(name)
    return PipPolicyDecision(
        requested=tuple(requested),
        allowed=tuple(allowed),
        blocked_by_global=tuple(blocked_by_global),
        blocked_by_tenant=tuple(blocked_by_tenant),
    )


async def get_tenant_pip_allowlist(
    tenant_id: str, *, settings: Settings | None = None
) -> list[str]:
    """Return a tenant's configured code-package allowlist (empty when unset)."""
    settings = settings or get_settings()
    ttl = _cache_ttl(settings)
    now = time.monotonic()
    cached = _allowlist_cache.get(tenant_id)
    if cached is not None and cached[1] > now:
        return list(cached[0])

    doc = await get_control_database()[TENANTS_COLLECTION].find_one({"tenant_id": tenant_id})
    allowlist = _coerce_stored_allowlist((doc or {}).get("code_requirements_allowlist"))
    if ttl > 0:
        _allowlist_cache[tenant_id] = (list(allowlist), now + ttl)
    return allowlist


async def get_effective_pip_allowlist(
    tenant_id: str, *, settings: Settings | None = None
) -> list[str]:
    """Convenience: a tenant's allowlist intersected with the operator ceiling."""
    settings = settings or get_settings()
    tenant_allowlist = await get_tenant_pip_allowlist(tenant_id, settings=settings)
    return effective_allowlist(tenant_allowlist, settings=settings)


async def evaluate_tenant_requirements(
    tenant_id: str,
    requirements: Iterable[Any],
    *,
    settings: Settings | None = None,
) -> PipPolicyDecision:
    """Load a tenant's allowlist (cached) and classify ``requirements`` against it."""
    settings = settings or get_settings()
    tenant_allowlist = await get_tenant_pip_allowlist(tenant_id, settings=settings)
    return evaluate_requirements(requirements, tenant_allowlist=tenant_allowlist, settings=settings)


async def set_tenant_pip_allowlist(
    tenant_id: str,
    entries: Iterable[Any],
    *,
    updated_by: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Persist a tenant's code-package allowlist. Returns the doc, or None if missing.

    Entries are validated + normalized (raising :class:`PipPolicyError` on a
    malformed entry) so an invalid policy can never be stored. The local cache is
    refreshed write-through so curation takes effect immediately on this replica.
    """
    settings = settings or get_settings()
    normalized = validate_allowlist_entries(entries)
    now = datetime.now(UTC)
    doc = await get_control_database()[TENANTS_COLLECTION].find_one_and_update(
        {"tenant_id": tenant_id},
        {
            "$set": {
                "code_requirements_allowlist": normalized,
                "code_requirements_updated_at": now,
                "code_requirements_updated_by": str(updated_by or "admin"),
                "updated_at": now,
            }
        },
        return_document=True,
    )
    if doc is None:
        return None
    ttl = _cache_ttl(settings)
    if ttl > 0:
        _allowlist_cache[tenant_id] = (list(normalized), time.monotonic() + ttl)
    else:
        _allowlist_cache.pop(tenant_id, None)
    return doc
