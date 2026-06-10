from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from pymongo.errors import OperationFailure

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
    # Email is the sole login identifier (the login form has no tenant field), so
    # it must resolve to exactly one account globally.
    await control_db["users"].create_index("email", unique=True)
    await control_db["users"].create_index("tenant_id")
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
        except Exception:
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
