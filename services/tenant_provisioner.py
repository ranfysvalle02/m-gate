from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo.errors import OperationFailure, PyMongoError

import database.mongo as mongo_module
from config.settings import Settings, get_settings
from database.encryption import (
    create_encrypted_routing_registry,
    ensure_key_vault,
    ensure_tenant_data_key,
)
from database.indexes import ensure_tool_catalog_indexes, upsert_search_index
from database.mongo import (
    get_control_database,
    get_tenant_database,
    tenant_db_name,
)
from services.cache_manager import semantic_cache_index_spec
from services.embedding_config import active_embedding_identity, tenant_embedding_identity
from services.guardrails import guardrail_signature_index_spec
from services.tenant_status import (
    STATUS_ACTIVE,
    STATUS_DELETED,
    set_tenant_status,
)

logger = logging.getLogger(__name__)

# Background reaper that hard-drops soft-deleted tenants past their retention.
_reaper_task: asyncio.Task | None = None


class UnknownTenantError(Exception):
    """A request referenced a tenant that is not provisioned and auto-provisioning
    is disabled. Surfacing this explicitly prevents the silent empty-result failures
    that happen when a query runs against a tenant database that was never created.
    """

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        super().__init__(
            f"Tenant '{tenant_id}' is not provisioned. Provision it via "
            "`POST /admin/tenants` (or scripts/admin.py) or enable "
            "AUTO_PROVISION_TENANTS."
        )


# Tenants confirmed ready in this process. Provisioning is idempotent, so this is
# purely a hot-path optimization to avoid a control-plane round-trip per request.
_ready_tenants: set[str] = set()
_ready_locks: dict[str, asyncio.Lock] = {}
_ready_locks_guard = asyncio.Lock()
_provision_locks: dict[str, asyncio.Lock] = {}
_provision_locks_guard = asyncio.Lock()


def reset_ready_tenant_cache() -> None:
    """Clear the in-process provisioning cache (used by tests and after a wipe)."""
    _ready_tenants.clear()
    _ready_locks.clear()
    _provision_locks.clear()


def _evict_tenant_cache(tenant_id: str) -> None:
    """Forget one tenant from in-process provisioning caches."""
    _ready_tenants.discard(tenant_id)
    _ready_locks.pop(tenant_id, None)
    _provision_locks.pop(tenant_id, None)


async def _tenant_lock_for(
    tenant_id: str,
    *,
    lock_map: dict[str, asyncio.Lock],
    guard: asyncio.Lock,
) -> asyncio.Lock:
    async with guard:
        lock = lock_map.get(tenant_id)
        if lock is None:
            lock = asyncio.Lock()
            lock_map[tenant_id] = lock
        return lock


async def ensure_tenant_ready(
    tenant_id: str,
    *,
    settings: Settings | None = None,
) -> bool:
    """Guarantee a tenant's database + indexes exist before tenant-scoped queries.

    Resolution order, cached per process:
      1. Already confirmed ready -> return immediately.
      2. A `tenants` control-plane document exists -> mark ready.
      3. Otherwise auto-provision (when enabled) or raise ``UnknownTenantError`` so
         the caller can return a clear error instead of a silent empty result.
    """
    settings = settings or get_settings()
    if tenant_id in _ready_tenants:
        return True

    ready_lock = await _tenant_lock_for(
        tenant_id,
        lock_map=_ready_locks,
        guard=_ready_locks_guard,
    )
    async with ready_lock:
        if tenant_id in _ready_tenants:
            return True

        control_db = get_control_database()
        existing = await control_db["tenants"].find_one({"tenant_id": tenant_id})
        if existing is not None:
            _ready_tenants.add(tenant_id)
            return True

        if not settings.auto_provision_tenants:
            raise UnknownTenantError(tenant_id)

        # Provision lazily without blocking on index build completion: the request
        # path should not wait for Atlas to finish materializing vector indexes.
        await provision_tenant(tenant_id, wait_for_queryable_indexes=False)
        _ready_tenants.add(tenant_id)
        return True


async def ensure_control_plane_indexes() -> None:
    settings = get_settings()
    control_db = get_control_database()
    await control_db["tenants"].create_index("tenant_id", unique=True)
    # Backs the soft-delete purge reaper's "due for purge" scan. NOT a TTL index:
    # a TTL would delete the control doc and orphan the physical tenant database,
    # so the reaper (which drops the DB first) is the source of truth.
    await control_db["tenants"].create_index([("status", 1), ("purge_at", 1)])
    # Email is the sole login identifier (the login form has no tenant field), so
    # it must resolve to exactly one account globally.
    await control_db["users"].create_index("email", unique=True)
    await control_db["users"].create_index("tenant_id")
    # Backs the self-registration beta cap count ({"self_registered": True}).
    await control_db["users"].create_index("self_registered")
    # Per-IP sign-up throttle buckets: a unique (client_ip, window) key and a TTL on
    # expires_at so used buckets are reaped automatically.
    await control_db["registration_attempts"].create_index("expires_at", expireAfterSeconds=0)
    await control_db["registration_attempts"].create_index(
        [("client_ip", 1), ("window_epoch", 1)],
        unique=True,
    )
    await _ensure_watcher_state_ttl_index(
        control_db=control_db,
        ttl_seconds=settings.watcher_resume_ttl_seconds,
    )
    await control_db["session_context"].create_index("expires_at", expireAfterSeconds=0)
    await control_db["session_context"].create_index([("tenant_id", 1), ("user_id", 1)])
    await control_db["rate_limit_buckets"].create_index("expires_at", expireAfterSeconds=0)
    await control_db["rate_limit_buckets"].create_index(
        [("tenant_id", 1), ("client_ip", 1), ("window_epoch", 1)],
        unique=True,
    )
    await control_db["usage_counters"].create_index([("tenant_id", 1), ("period", 1)], unique=True)
    await control_db["usage_events"].create_index([("tenant_id", 1), ("ts", -1)])
    # Backs the analytics top-tools/top-servers rollup, which $matches a single
    # (tenant_id, period) then $groups by metadata.server/tool.
    await control_db["usage_events"].create_index([("tenant_id", 1), ("period", 1)])
    await control_db["guardrail_signatures"].create_index("category")
    _model_id, dimensions, embedding_version = active_embedding_identity()
    guardrail_index_spec = guardrail_signature_index_spec(
        embedding_version=embedding_version,
        dimensions=dimensions,
    )
    await upsert_search_index(
        control_db["guardrail_signatures"],
        name=guardrail_index_spec["name"],
        definition=guardrail_index_spec["definition"],
        index_type="vectorSearch",
    )


async def _ensure_watcher_state_ttl_index(*, control_db, ttl_seconds: int) -> None:
    collection = control_db["watcher_state"]
    desired_ttl = max(1, int(ttl_seconds))
    target_name = "updated_at_1"
    list_indexes = getattr(collection, "list_indexes", None)
    if callable(list_indexes):
        try:
            index_cursor = list_indexes()
            if asyncio.iscoroutine(index_cursor):
                index_cursor = await index_cursor
            indexes = await index_cursor.to_list(length=100)
        except PyMongoError:
            indexes = []
        existing = next(
            (idx for idx in indexes if isinstance(idx, dict) and idx.get("name") == target_name),
            None,
        )
        if existing is not None and int(existing.get("expireAfterSeconds") or -1) != desired_ttl:
            drop_index = getattr(collection, "drop_index", None)
            if callable(drop_index):
                dropped = drop_index(target_name)
                if asyncio.iscoroutine(dropped):
                    await dropped
    await collection.create_index("updated_at", expireAfterSeconds=desired_ttl)


async def provision_tenant(
    tenant_id: str,
    *,
    wait_for_queryable_indexes: bool = True,
) -> str:
    settings = get_settings()
    provision_lock = await _tenant_lock_for(
        tenant_id,
        lock_map=_provision_locks,
        guard=_provision_locks_guard,
    )
    async with provision_lock:
        now = datetime.now(UTC)
        await ensure_control_plane_indexes()

        control_db = get_control_database()
        await control_db["tenants"].update_one(
            {"tenant_id": tenant_id},
            {
                "$set": {
                    "tenant_id": tenant_id,
                    "db_name": tenant_db_name(tenant_id),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now, "status": "active"},
            },
            upsert=True,
        )

        tenant_db = get_tenant_database(tenant_id)
        collections = set(await tenant_db.list_collection_names())
        if "audit_telemetry" not in collections:
            try:
                await tenant_db.command(
                    {
                        "create": "audit_telemetry",
                        "timeseries": {
                            "timeField": "timestamp",
                            "metaField": "tenant_id",
                            "granularity": "seconds",
                        },
                    }
                )
            except OperationFailure:
                # Collection exists race between concurrent provisioners.
                pass

        if settings.qe_enabled:
            await ensure_key_vault(settings)
            await create_encrypted_routing_registry(tenant_db, settings)
            await ensure_tenant_data_key(tenant_id, settings)
        await tenant_db["tool_catalog"].create_index([("server", 1), ("name", 1)], unique=True)
        await tenant_db["routing_registry"].create_index("server", unique=True)
        await tenant_db["semantic_cache"].create_index("expires_at", expireAfterSeconds=0)
        await tenant_db["pending_actions"].create_index("expires_at", expireAfterSeconds=0)
        await tenant_db["pending_actions"].create_index([("status", 1), ("created_at", -1)])
        _model_id, dimensions, embedding_version = await tenant_embedding_identity(
            tenant_id, settings=settings
        )
        cache_index_spec = semantic_cache_index_spec(
            embedding_version=embedding_version,
            dimensions=dimensions,
        )

        await upsert_search_index(
            tenant_db["semantic_cache"],
            name=cache_index_spec["name"],
            index_type="vectorSearch",
            definition=cache_index_spec["definition"],
        )

        await ensure_tool_catalog_indexes(
            collection=tenant_db["tool_catalog"],
            wait_for_queryable=wait_for_queryable_indexes,
            dimensions=dimensions,
        )
        _ready_tenants.add(tenant_id)
        return tenant_db_name(tenant_id)


async def deprovision_tenant(tenant_id: str) -> bool:
    """Delete a tenant's control-plane record and drop its tenant database."""
    control_db = get_control_database()
    result = await control_db["tenants"].delete_many({"tenant_id": tenant_id})

    db_name = tenant_db_name(tenant_id)
    try:
        client = mongo_module.get_client()
    except Exception:
        # The control-plane record is already gone; if we cannot reach the client
        # the tenant database is left orphaned. Log loudly so an operator can drop
        # it manually rather than discovering stray databases later.
        logger.error(
            "Could not obtain MongoDB client to drop tenant database '%s' during "
            "deprovision; the database may be left orphaned.",
            db_name,
            exc_info=True,
        )
        client = None
    if client is not None:
        drop_database = getattr(client, "drop_database", None)
        if callable(drop_database):
            dropped = drop_database(db_name)
            if inspect.isawaitable(dropped):
                await dropped

    _evict_tenant_cache(tenant_id)
    return int(result.deleted_count) > 0


async def soft_delete_tenant(
    tenant_id: str,
    *,
    retention_days: int | None = None,
    actor: str | None = None,
) -> dict[str, Any] | None:
    """Mark a tenant deleted and schedule its purge, keeping the data for now.

    The tenant is locked out of the hot path immediately (status ``deleted``),
    but its database is retained until ``purge_at`` so the deletion stays
    reversible via :func:`restore_tenant`. The physical drop is the purge
    reaper's job (:func:`purge_expired_tenants`); a Mongo TTL index alone would
    delete the control doc and orphan the tenant database. Returns the updated
    control doc, or ``None`` if the tenant does not exist.
    """
    settings = get_settings()
    days = settings.tenant_retention_days if retention_days is None else retention_days
    now = datetime.now(UTC)
    purge_at = now + timedelta(days=max(0, int(days)))
    doc = await set_tenant_status(
        tenant_id,
        STATUS_DELETED,
        updated_by=actor,
        extra={"deleted_at": now, "purge_at": purge_at, "purge_started_at": None},
    )
    if doc is None:
        return None
    # Drop the hot-path readiness cache so a soft-deleted tenant is re-checked
    # (and rejected) rather than served from the optimistic ready set.
    _evict_tenant_cache(tenant_id)
    return doc


async def restore_tenant(
    tenant_id: str,
    *,
    actor: str | None = None,
) -> dict[str, Any] | None:
    """Reverse a soft-delete: flip a ``deleted`` tenant back to ``active``.

    Returns the updated control doc on success, or ``None`` when the tenant does
    not exist or is not currently soft-deleted (already active/suspended, or
    already purged), so the caller can surface the right HTTP status.
    """
    control_db = get_control_database()
    existing = await control_db["tenants"].find_one({"tenant_id": tenant_id})
    if existing is None or str(existing.get("status")) != STATUS_DELETED:
        return None
    doc = await set_tenant_status(
        tenant_id,
        STATUS_ACTIVE,
        updated_by=actor,
        extra={"deleted_at": None, "purge_at": None, "purge_started_at": None},
    )
    if doc is not None:
        _evict_tenant_cache(tenant_id)
    return doc


async def purge_expired_tenants(
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    """Hard-drop soft-deleted tenants whose retention window has elapsed.

    Each due tenant is claimed atomically with ``find_one_and_update`` so two
    active-active replicas never purge the same tenant twice, then hard-deleted
    via :func:`deprovision_tenant`. Returns the number of tenants purged.
    """
    control_db = get_control_database()
    cutoff = now or datetime.now(UTC)
    purged = 0
    # Tenants whose hard-drop failed this pass. Their claim is released so a
    # future sweep retries, but they are excluded from re-claiming within *this*
    # call so one persistently-failing tenant cannot loop or starve the others.
    failed: list[str] = []
    for _ in range(max(0, int(limit))):
        claim_filter: dict[str, Any] = {
            "status": STATUS_DELETED,
            "purge_at": {"$lte": cutoff},
            "purge_started_at": None,
        }
        if failed:
            claim_filter["tenant_id"] = {"$nin": failed}
        claimed = await control_db["tenants"].find_one_and_update(
            claim_filter,
            {"$set": {"purge_started_at": cutoff}},
            return_document=True,
        )
        if claimed is None:
            break
        tenant_id = str(claimed.get("tenant_id") or "")
        if not tenant_id:
            continue
        try:
            await deprovision_tenant(tenant_id)
            purged += 1
        except Exception:
            # Release the claim so a later sweep retries: deprovision is
            # idempotent (drop DB + delete doc), so a transient failure must not
            # strand the tenant's data past its retention window forever.
            logger.error(
                "Failed to purge expired tenant '%s'; releasing the claim to retry "
                "on a later sweep.",
                tenant_id,
                exc_info=True,
            )
            failed.append(tenant_id)
            try:
                await control_db["tenants"].update_one(
                    {"tenant_id": tenant_id, "status": STATUS_DELETED},
                    {"$set": {"purge_started_at": None}},
                )
            except Exception:
                logger.error(
                    "Failed to release purge claim for tenant '%s'; it will need manual cleanup.",
                    tenant_id,
                    exc_info=True,
                )
    return purged


async def _reaper_loop(interval_seconds: int) -> None:
    interval = max(1, int(interval_seconds))
    while True:
        try:
            await asyncio.sleep(interval)
            purged = await purge_expired_tenants()
            if purged:
                logger.info("Tenant purge reaper dropped %d expired tenant(s).", purged)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the reaper die on a transient error
            logger.warning("Tenant purge reaper iteration failed, will retry: %s", exc)


async def start_tenant_purge_reaper() -> None:
    """Start the background purge reaper when configured (no-op when disabled)."""
    global _reaper_task
    settings = get_settings()
    interval = settings.tenant_purge_sweep_interval_seconds
    if interval <= 0:
        return
    if _reaper_task is None or _reaper_task.done():
        logger.info("Starting tenant purge reaper (every %ds).", interval)
        _reaper_task = asyncio.create_task(_reaper_loop(interval))


async def stop_tenant_purge_reaper() -> None:
    """Stop the background purge reaper if it is running."""
    global _reaper_task
    if _reaper_task is not None:
        _reaper_task.cancel()
        try:
            await _reaper_task
        except asyncio.CancelledError:
            pass
        _reaper_task = None
