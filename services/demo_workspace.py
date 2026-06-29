"""One-click, isolated, self-expiring demo workspaces (demo tenants).

The unit of a "demo" on this platform is a **tenant**, not a server: a tenant is
the isolation + identity boundary (its own database, users, ``/mcp`` endpoint,
and quotas), so "spin up 2 demos for 2 customers" maps cleanly to two isolated
tenants that can never bleed into each other — never two servers crammed into
one tenant.

This module composes the existing primitives into a single platform-admin
action:

1. :func:`services.tenant_provisioner.provision_tenant` — a fresh, isolated DB.
2. confirm the tenant (``confirmed`` tier) so the curated multi-tool pack seeds
   and the recipient can author their own functions, lifting the unconfirmed
   1-server/1-tool cap.
3. :func:`services.demo_seed.seed_demo_pack` — a capability-aware tool pack +
   sample data, so the demo shines on the first call.
4. a ready-to-share **tenant-admin** login for the demo tenant (full authoring
   experience, confined to that one isolated tenant — it is never a
   platform-admin and can never see another tenant).

Bulletproofing:

* **Capped on the tenant axis** (``max_demo_tenants``), mirroring the
  self-registration beta cap — each demo is a database.
* **Self-expiring**: every demo carries ``expires_at`` and is hard-reaped by
  :func:`reap_expired_demo_workspaces` (claimed atomically so active-active
  replicas never double-reap), invoked lazily on create/list and by the
  background tenant reaper. Abandoned demos cannot pile up.
* **Atomic-ish provisioning**: any failure after the tenant DB is created rolls
  the whole thing back (drop DB + purge users), so a half-built demo never
  lingers.
* **Safe teardown**: deletion refuses any tenant that is not ``origin="demo"``,
  so this surface can never nuke a real customer tenant.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from config.settings import Settings, get_settings
from database.mongo import get_control_database, tenant_db_name
from services import users as users_service
from services.account_tier import (
    CONFIRMATION_CONFIRMED,
    set_tenant_confirmation,
    tier_caps,
)
from services.demo_seed import DemoSeedResult, seed_demo_pack
from services.tenant_provisioner import deprovision_tenant, provision_tenant
from services.tenant_status import STATUS_DELETED
from services.usage_metering import set_quota
from services.users import SELF_SERVICE_ROLES, derive_demo_scopes

logger = logging.getLogger(__name__)

TENANTS_COLLECTION = "tenants"
SESSION_CONTEXT_COLLECTION = "session_context"

# Tags a tenant as a demo workspace (the cleanup + cap + reap key off this).
DEMO_ORIGIN = "demo"
DEMO_ACTOR = "demo-workspace"
# Auto-generated demo logins live on this domain (mirrors the demo-user surface).
_DEMO_EMAIL_DOMAIN = "demo.local"


class DemoWorkspaceError(Exception):
    """Base class for demo-workspace failures."""


class DemoWorkspacesDisabled(DemoWorkspaceError):
    """Demo workspaces are turned off (``demo_workspaces_enabled=false``)."""


class DemoCapReached(DemoWorkspaceError):
    """The configured ``max_demo_tenants`` ceiling has been reached."""


@dataclass
class DemoWorkspace:
    """A demo workspace's public shape (password present only at creation time)."""

    tenant_id: str
    db_name: str
    label: str | None
    client: str | None
    status: str
    created_at: datetime | None
    created_by: str | None
    expires_at: datetime | None
    expired: bool
    user_id: str
    user_email: str
    servers: list[str] = field(default_factory=list)
    tools: int = 0
    bridges: dict[str, bool] = field(default_factory=dict)
    password: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: Any) -> datetime | None:
    """Normalize a stored timestamp to an aware UTC datetime (Mongo strips tz)."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _clamp_ttl_hours(ttl_hours: int | None, settings: Settings) -> int:
    default = max(1, int(settings.demo_ttl_hours))
    ceiling = max(1, int(settings.demo_ttl_max_hours))
    if ttl_hours is None:
        return min(default, ceiling)
    return max(1, min(int(ttl_hours), ceiling))


def _active_demo_query() -> dict[str, Any]:
    return {"origin": DEMO_ORIGIN, "status": {"$ne": STATUS_DELETED}}


def _workspace_from_doc(doc: dict[str, Any], *, now: datetime) -> DemoWorkspace:
    expires_at = _as_aware(doc.get("expires_at"))
    tenant_id = str(doc.get("tenant_id") or "")
    return DemoWorkspace(
        tenant_id=tenant_id,
        db_name=str(doc.get("db_name") or tenant_db_name(tenant_id)),
        label=(str(doc["demo_label"]) if doc.get("demo_label") else None),
        client=(str(doc["demo_client"]) if doc.get("demo_client") else None),
        status=str(doc.get("status") or "active"),
        created_at=_as_aware(doc.get("created_at")),
        created_by=(str(doc["demo_created_by"]) if doc.get("demo_created_by") else None),
        expires_at=expires_at,
        expired=bool(expires_at and expires_at <= now),
        user_id=str(doc.get("demo_user_id") or ""),
        user_email=str(doc.get("demo_user_email") or ""),
        servers=[str(s) for s in (doc.get("demo_servers") or []) if isinstance(s, str)],
        tools=int(doc.get("demo_tools") or 0),
        bridges=dict(doc.get("demo_bridges") or {}),
    )


async def _purge_users_and_sessions(tenant_id: str) -> None:
    """Delete a demo tenant's control-plane users + session-context rows.

    ``deprovision_tenant`` drops the tenant database and the ``tenants`` control
    doc, but managed users live in the *control* DB (auth happens before a tenant
    is resolved). Email is globally unique, so leaving a demo's users behind would
    both clutter the directory and block reusing that address — purge them.
    """
    if not tenant_id:
        return
    control_db = get_control_database()
    try:
        await control_db[users_service.USERS_COLLECTION].delete_many({"tenant_id": tenant_id})
        await control_db[SESSION_CONTEXT_COLLECTION].delete_many({"tenant_id": tenant_id})
    except Exception:
        logger.error(
            "Failed to purge users/sessions for demo tenant '%s'; they may need "
            "manual cleanup.",
            tenant_id,
            exc_info=True,
        )


async def _teardown_demo_tenant(tenant_id: str) -> None:
    """Best-effort full teardown: drop the tenant DB + control doc, then its users."""
    try:
        await deprovision_tenant(tenant_id)
    except Exception:
        logger.error(
            "Failed to deprovision demo tenant '%s'; it may need manual cleanup.",
            tenant_id,
            exc_info=True,
        )
    await _purge_users_and_sessions(tenant_id)


async def reap_expired_demo_workspaces(
    *, now: datetime | None = None, limit: int = 100
) -> int:
    """Hard-drop demo workspaces whose ``expires_at`` has elapsed.

    Each due demo is claimed atomically with ``find_one_and_update`` (setting
    ``demo_reaping_at``) so two active-active replicas never reap the same tenant
    twice, then fully torn down. Returns the number reaped. Never raises.
    """
    control_db = get_control_database()
    cutoff = now or _now()
    reaped = 0
    failed: list[str] = []
    for _ in range(max(0, int(limit))):
        claim_filter: dict[str, Any] = {
            "origin": DEMO_ORIGIN,
            "status": {"$ne": STATUS_DELETED},
            "expires_at": {"$lte": cutoff},
            "demo_reaping_at": None,
        }
        if failed:
            claim_filter["tenant_id"] = {"$nin": failed}
        try:
            claimed = await control_db[TENANTS_COLLECTION].find_one_and_update(
                claim_filter,
                {"$set": {"demo_reaping_at": cutoff}},
                return_document=True,
            )
        except Exception:
            logger.warning("Demo reap claim query failed; will retry next sweep.", exc_info=True)
            break
        if claimed is None:
            break
        tenant_id = str(claimed.get("tenant_id") or "")
        if not tenant_id:
            continue
        try:
            await _teardown_demo_tenant(tenant_id)
            reaped += 1
        except Exception:
            failed.append(tenant_id)
            logger.error("Failed to reap expired demo tenant '%s'.", tenant_id, exc_info=True)
            try:
                await control_db[TENANTS_COLLECTION].update_one(
                    {"tenant_id": tenant_id},
                    {"$set": {"demo_reaping_at": None}},
                )
            except Exception:
                pass
    if reaped:
        logger.info("Demo reaper dropped %d expired demo workspace(s).", reaped)
    return reaped


async def count_active_demos() -> int:
    return int(await get_control_database()[TENANTS_COLLECTION].count_documents(_active_demo_query()))


async def list_demo_workspaces(*, reap: bool = True) -> list[DemoWorkspace]:
    """Return active demo workspaces (most recent first). Optionally reaps first."""
    if reap:
        await reap_expired_demo_workspaces()
    now = _now()
    docs = (
        await get_control_database()[TENANTS_COLLECTION]
        .find(_active_demo_query())
        .to_list(length=1000)
    )
    workspaces = [_workspace_from_doc(doc, now=now) for doc in docs]
    workspaces.sort(key=lambda w: (w.created_at or datetime.min.replace(tzinfo=UTC)), reverse=True)
    return workspaces


async def delete_demo_workspace(tenant_id: str) -> bool:
    """Hard-delete a demo workspace. Refuses any non-demo tenant (returns False).

    The ``origin="demo"`` guard makes this surface incapable of dropping a real
    customer tenant even if handed an arbitrary id.
    """
    doc = await get_control_database()[TENANTS_COLLECTION].find_one({"tenant_id": tenant_id})
    if doc is None or str(doc.get("origin")) != DEMO_ORIGIN:
        return False
    await _teardown_demo_tenant(tenant_id)
    logger.info("Demo workspace deleted: tenant=%s", tenant_id)
    return True


async def _create_demo_admin_user(
    *,
    tenant_id: str,
    scopes: list[str],
    created_by: str | None,
    label: str | None,
    client: str | None,
) -> tuple[dict[str, Any], str]:
    """Mint a tenant-admin login for the demo tenant; returns (user, password).

    Retries the auto-generated email on the astronomically-unlikely collision so
    one-click provisioning never fails on a clash.
    """
    password = secrets.token_urlsafe(12)
    last_exc: users_service.UserAlreadyExists | None = None
    for _ in range(5):
        email = f"demo-{secrets.token_hex(3)}@{_DEMO_EMAIL_DOMAIN}"
        try:
            user = await users_service.create_user(
                email=email,
                password=password,
                tenant_id=tenant_id,
                roles=list(SELF_SERVICE_ROLES),
                scopes=scopes,
                status="active",
                created_by=created_by,
                label=label or "Demo workspace",
                client=client,
            )
            return user, password
        except users_service.UserAlreadyExists as exc:
            last_exc = exc
    raise DemoWorkspaceError(str(last_exc) if last_exc else "Could not mint a demo login.")


async def provision_demo_workspace(
    *,
    label: str | None = None,
    client: str | None = None,
    ttl_hours: int | None = None,
    created_by: str | None = None,
    settings: Settings | None = None,
) -> DemoWorkspace:
    """Provision a fully-seeded, isolated, self-expiring demo workspace.

    Raises :class:`DemoWorkspacesDisabled` when the feature is off and
    :class:`DemoCapReached` when ``max_demo_tenants`` is hit. Any failure after
    the tenant database is created rolls the whole thing back.
    """
    settings = settings or get_settings()
    if not settings.demo_workspaces_enabled:
        raise DemoWorkspacesDisabled("Demo workspaces are disabled.")

    # Reap first so the cap reflects reality (expired demos shouldn't block a new
    # one), then enforce the tenant-axis ceiling.
    await reap_expired_demo_workspaces()
    cap = max(0, int(settings.max_demo_tenants))
    if cap > 0 and await count_active_demos() >= cap:
        raise DemoCapReached(
            f"The demo workspace limit ({cap}) has been reached. "
            "Delete an existing demo before creating another."
        )

    ttl_hours = _clamp_ttl_hours(ttl_hours, settings)
    now = _now()
    expires_at = now + timedelta(hours=ttl_hours)
    tenant_id = f"{settings.demo_tenant_prefix}{uuid.uuid4().hex[:12]}"

    db_name = await provision_tenant(tenant_id, wait_for_queryable_indexes=False)
    try:
        # Confirmed so the multi-tool pack seeds and the recipient can author code
        # tools (the unconfirmed tier would cap them at 1 server / 1 tool).
        await set_tenant_confirmation(tenant_id, CONFIRMATION_CONFIRMED, updated_by=DEMO_ACTOR)
        caps = tier_caps(CONFIRMATION_CONFIRMED, settings=settings)

        seed: DemoSeedResult = await seed_demo_pack(tenant_id, settings=settings)

        # Derive scopes AFTER seeding so a minted data-plane token can invoke every
        # seeded tool over /rpc + /mcp (the tenant-admin role also bypasses per-call
        # scope checks within the tenant, so this is belt-and-suspenders).
        scopes = await derive_demo_scopes(tenant_id)
        user, password = await _create_demo_admin_user(
            tenant_id=tenant_id,
            scopes=scopes,
            created_by=created_by or DEMO_ACTOR,
            label=label,
            client=client,
        )

        await set_quota(
            tenant_id,
            calls_limit=caps.quota_calls_per_period,
            sandbox_seconds_limit=caps.quota_sandbox_seconds_per_period,
            updated_by=DEMO_ACTOR,
        )
        await users_service.sync_session_context(user)

        # Stamp the demo metadata last: the tenant doc now describes a complete,
        # discoverable demo (origin tag drives cap/reap/cleanup; the rest powers
        # the console list without re-deriving anything).
        await get_control_database()[TENANTS_COLLECTION].update_one(
            {"tenant_id": tenant_id},
            {
                "$set": {
                    "origin": DEMO_ORIGIN,
                    "created_via": DEMO_ACTOR,
                    "demo_label": (label or "").strip() or None,
                    "demo_client": (client or "").strip() or None,
                    "demo_created_by": created_by or DEMO_ACTOR,
                    "demo_created_at": now,
                    "expires_at": expires_at,
                    "demo_reaping_at": None,
                    "demo_user_id": user["id"],
                    "demo_user_email": user["email"],
                    "demo_servers": seed.servers,
                    "demo_tools": seed.tools,
                    "demo_bridges": seed.bridges,
                    "updated_at": now,
                }
            },
        )
    except Exception:
        await _teardown_demo_tenant(tenant_id)
        raise

    logger.info(
        "Demo workspace provisioned: tenant=%s by=%s servers=%s tools=%d expires_at=%s",
        tenant_id,
        created_by or DEMO_ACTOR,
        seed.servers,
        seed.tools,
        expires_at.isoformat(),
    )
    return DemoWorkspace(
        tenant_id=tenant_id,
        db_name=db_name,
        label=(label or "").strip() or None,
        client=(client or "").strip() or None,
        status="active",
        created_at=now,
        created_by=created_by or DEMO_ACTOR,
        expires_at=expires_at,
        expired=False,
        user_id=user["id"],
        user_email=user["email"],
        servers=seed.servers,
        tools=seed.tools,
        bridges=seed.bridges,
        password=password,
    )
