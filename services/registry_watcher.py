from __future__ import annotations

import asyncio
import inspect
import logging
import socket
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import OperationFailure

from config.settings import get_settings
from database.encryption import build_watcher_client
from database.mongo import (
    get_client,
    get_control_database,
    get_tenant_database,
    tenant_id_from_db_name,
)
from services.proxy_registry import get_proxy_registry

logger = logging.getLogger(__name__)

_watcher_task: asyncio.Task | None = None
_catalog_version: int = 0
_RESUME_STATE_COLLECTION = "watcher_state"


def _bump_catalog_version() -> None:
    global _catalog_version
    _catalog_version += 1


def get_catalog_version() -> int:
    return _catalog_version


async def _initial_sync_all_tenants(registry: Any) -> None:
    control_db = get_control_database()
    tenant_docs = await control_db["tenants"].find({}).to_list(length=10_000)
    tenant_ids = [
        doc.get("tenant_id") for doc in tenant_docs if isinstance(doc.get("tenant_id"), str)
    ]
    if not tenant_ids:
        tenant_ids = [get_settings().default_tenant_id]

    for tenant_id in tenant_ids:
        collection = get_tenant_database(tenant_id)["routing_registry"]
        cursor = collection.find({"enabled": True})
        for doc in await cursor.to_list(length=1000):
            doc["tenant_id"] = tenant_id
            try:
                await registry.mount_or_update(doc)
                _bump_catalog_version()
            except Exception as exc:
                logger.warning(
                    "Initial sync failed for tenant '%s' server '%s': %s",
                    tenant_id,
                    doc.get("server"),
                    exc,
                )


async def _apply_change(change: dict[str, Any], registry: Any) -> None:
    operation = change.get("operationType")
    full_doc = change.get("fullDocument")
    db_name = (change.get("ns") or {}).get("db")
    tenant_id: str | None = None
    if isinstance(db_name, str):
        try:
            tenant = await get_control_database()["tenants"].find_one({"db_name": db_name})
        except Exception as exc:
            logger.warning("Registry watcher failed tenant lookup for db '%s': %s", db_name, exc)
            return
        if tenant and isinstance(tenant.get("tenant_id"), str):
            tenant_id = tenant["tenant_id"]
        else:
            tenant_id = tenant_id_from_db_name(db_name)
    if not tenant_id:
        logger.warning(
            "Registry watcher could not resolve tenant for change '%s' in db '%s'.",
            change.get("_id"),
            db_name,
        )
        return

    if operation in {"insert", "replace", "update"} and full_doc:
        full_doc["tenant_id"] = tenant_id
        if full_doc.get("enabled", True):
            await registry.mount_or_update(full_doc)
            _bump_catalog_version()
        else:
            await registry.unmount(full_doc["server"], tenant_id=tenant_id)
            _bump_catalog_version()
    elif operation == "delete":
        key = change.get("documentKey", {})
        server_name = key.get("_id") or key.get("server")
        if server_name:
            await registry.unmount(server_name, tenant_id=tenant_id)
            _bump_catalog_version()


def _watcher_instance_id() -> str:
    settings = get_settings()
    return (settings.gateway_instance_id or socket.gethostname()).strip() or "gateway-instance"


def _resume_doc_id(instance_id: str | None = None) -> str:
    return f"routing_registry::{instance_id or _watcher_instance_id()}"


async def _load_resume_token(state_collection: Any, *, resume_doc_id: str) -> Any | None:
    state = await state_collection.find_one({"_id": resume_doc_id})
    if state:
        return state.get("resume_token")
    return None


async def _save_resume_token(
    state_collection: Any, resume_token: Any, *, resume_doc_id: str, instance_id: str
) -> None:
    await state_collection.update_one(
        {"_id": resume_doc_id},
        {
            "$set": {
                "resume_token": resume_token,
                "instance_id": instance_id,
                "updated_at": datetime.now(UTC),
            }
        },
        upsert=True,
    )


async def _clear_resume_token(state_collection: Any, *, resume_doc_id: str) -> None:
    await state_collection.delete_many({"_id": resume_doc_id})


def _is_non_resumable_error(exc: OperationFailure) -> bool:
    if getattr(exc, "code", None) in {280, 286}:
        return True
    has_label = getattr(exc, "has_error_label", None)
    if callable(has_label):
        return bool(has_label("NonResumableChangeStreamError"))
    return False


def _build_watch_client() -> tuple[Any, bool]:
    """Return ``(client, owns_client)`` for the change-stream watcher.

    Under QE the shared app client auto-encrypts and cannot run the cluster-wide
    change stream, so use a dedicated ``bypass_auto_encryption`` client (which we
    own and must close). Otherwise reuse the shared client (which we must NOT
    close — it is owned by the connection manager).
    """
    if get_settings().qe_enabled:
        return build_watcher_client(), True
    return get_client(), False


async def _close_owned_client(client: Any) -> None:
    close_result = client.close()
    if inspect.isawaitable(close_result):
        await close_result


async def _watch_loop() -> None:
    control_db = get_control_database()
    state_collection = control_db[_RESUME_STATE_COLLECTION]
    registry = get_proxy_registry()
    instance_id = _watcher_instance_id()
    resume_doc_id = _resume_doc_id(instance_id)
    client, owns_client = _build_watch_client()

    try:
        resume_token = await _load_resume_token(state_collection, resume_doc_id=resume_doc_id)
        if resume_token is None:
            await _initial_sync_all_tenants(registry)

        while True:
            try:
                watch_kwargs: dict[str, Any] = {
                    "pipeline": [{"$match": {"ns.coll": "routing_registry"}}],
                    "full_document": "updateLookup",
                }
                if resume_token is not None:
                    watch_kwargs["resume_after"] = resume_token
                # AsyncCollection.watch() is a coroutine that resolves to the change
                # stream (an async context manager) — it must be awaited first.
                async with await client.watch(**watch_kwargs) as stream:
                    async for change in stream:
                        try:
                            await _apply_change(change, registry)
                        except Exception as exc:
                            logger.warning(
                                "Skipping bad registry change event '%s': %s",
                                change.get("_id"),
                                exc,
                            )

                        # Persist the latest stream position even if this specific
                        # event failed so a poisoned event does not replay forever.
                        resume_token = stream.resume_token or change.get("_id")
                        if resume_token is not None:
                            await _save_resume_token(
                                state_collection,
                                resume_token,
                                resume_doc_id=resume_doc_id,
                                instance_id=instance_id,
                            )
            except asyncio.CancelledError:
                raise
            except OperationFailure as exc:
                if _is_non_resumable_error(exc):
                    logger.warning("Resume token unusable; clearing and full-resync: %s", exc)
                    resume_token = None
                    await _clear_resume_token(state_collection, resume_doc_id=resume_doc_id)
                    await _initial_sync_all_tenants(registry)
                    continue
                logger.warning("Registry watcher failed, retrying in 3s: %s", exc)
                await asyncio.sleep(3)
            except Exception as exc:
                logger.warning("Registry watcher failed, retrying in 3s: %s", exc)
                await asyncio.sleep(3)
    finally:
        if owns_client:
            await _close_owned_client(client)


async def start_registry_watcher() -> None:
    global _watcher_task
    if _watcher_task is None or _watcher_task.done():
        logger.info("Starting registry watcher loop.")
        _watcher_task = asyncio.create_task(_watch_loop())


async def stop_registry_watcher() -> None:
    global _watcher_task
    if _watcher_task is not None:
        _watcher_task.cancel()
        try:
            await _watcher_task
        except asyncio.CancelledError:
            pass
        logger.info("Registry watcher loop stopped.")
        _watcher_task = None
