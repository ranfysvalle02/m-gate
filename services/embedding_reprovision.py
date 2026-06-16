"""Embedding reprovisioning orchestrator.

When the active embedding provider/model/dimensions change, the existing vector
data and indexes become stale: catalog embeddings live in the wrong vector
space, and Atlas vector indexes are pinned to a fixed ``numDimensions`` that
cannot be altered in place. This module rebuilds everything consistently across
all tenants and the control plane, reporting progress through a single status
document so the admin panel can poll it.

Ordering matters per tenant:
  1. Re-embed every ``tool_catalog`` document with the active provider.
  2. Drop + recreate the vector index with the (possibly new) dimensions.
  3. Re-embed / clear the semantic cache via the migration service.

The control plane (guardrail signatures) is rebuilt once up front.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from config.settings import Settings, get_settings
from database.indexes import VECTOR_INDEX_NAME, ensure_tool_catalog_indexes
from database.mongo import get_control_database, get_tenant_database
from services.cache_migration import SemanticCacheMigrationService
from services.embedding_config import (
    active_embedding_identity,
    get_active_embedding_service,
    get_embedding_service_for,
    tenant_embedding_identity,
)
from services.embeddings import EmbeddingService, embedding_version_for
from services.guardrails import resync_guardrail_signatures
from services.registry_watcher import _bump_catalog_version
from services.tenant_provisioner import ensure_control_plane_indexes

logger = logging.getLogger(__name__)

STATUS_COLLECTION = "embedding_status"
STATUS_ID = "reprovision"

# A "running" job older than this is treated as stale (e.g. the gateway crashed
# mid-run), so a fresh trigger is allowed instead of locking out forever.
_REPROVISION_STALE_SECONDS = 3600

_background_tasks: set[asyncio.Task] = set()


class ReprovisionInProgressError(RuntimeError):
    """A reprovision job is already running."""


def _now() -> datetime:
    return datetime.now(UTC)


def _status_id_for(tenant_id: str | None = None) -> str:
    if not tenant_id:
        return STATUS_ID
    return f"{STATUS_ID}:{tenant_id}"


def _is_running_and_fresh(status: dict[str, Any]) -> bool:
    """True if a job is genuinely in-flight (not a stale, crashed run)."""
    if status.get("state") != "running":
        return False
    started_at = status.get("started_at")
    if not isinstance(started_at, datetime):
        return True
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return (_now() - started_at).total_seconds() < _REPROVISION_STALE_SECONDS


async def get_reprovision_status() -> dict[str, Any]:
    try:
        doc = await get_control_database()[STATUS_COLLECTION].find_one({"_id": _status_id_for()})
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not read reprovision status: %s", exc)
        doc = None
    if not doc:
        return {"state": "idle"}
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


async def _write_status(**fields: Any) -> None:
    status_id = str(fields.pop("_id", _status_id_for()))
    await get_control_database()[STATUS_COLLECTION].update_one(
        {"_id": status_id},
        {"$set": {"_id": status_id, **fields}},
        upsert=True,
    )


async def is_reprovision_running() -> bool:
    """True if a non-stale reprovision job is currently in flight."""
    return _is_running_and_fresh(await get_reprovision_status())


async def get_tenant_reprovision_status(tenant_id: str) -> dict[str, Any]:
    try:
        doc = await get_control_database()[STATUS_COLLECTION].find_one(
            {"_id": _status_id_for(tenant_id)}
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not read tenant reprovision status: %s", exc)
        doc = None
    if not doc:
        return {"state": "idle"}
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


async def is_tenant_reprovision_running(tenant_id: str) -> bool:
    return _is_running_and_fresh(await get_tenant_reprovision_status(tenant_id))


async def _all_tenant_ids(settings: Settings) -> list[str]:
    docs = await get_control_database()["tenants"].find({}).to_list(length=10_000)
    ids = sorted(
        {str(doc.get("tenant_id")) for doc in docs if isinstance(doc.get("tenant_id"), str)}
    )
    if settings.default_tenant_id not in ids:
        ids.append(settings.default_tenant_id)
    return ids


async def _flush_catalog_batch(
    collection: Any,
    service: EmbeddingService,
    batch: list[tuple[str, str, str]],
) -> int:
    if not batch:
        return 0
    vectors = await service.embed_texts([text for _, _, text in batch])
    now = _now()
    for (server, name, _text), embedding in zip(batch, vectors, strict=True):
        await collection.update_one(
            {"server": server, "name": name},
            {"$set": {"embedding": embedding, "updated_at": now}},
        )
    return len(batch)


async def _reembed_tool_catalog(
    collection: Any,
    service: EmbeddingService,
    *,
    page_size: int,
) -> int:
    """Re-embed every catalog doc, streaming by ``_id`` so memory stays O(page).

    Sorting by ``_id`` gives a stable forward scan: rewriting the (large)
    ``embedding`` field can grow a doc and move it on disk, and only an ordered
    cursor guarantees each doc is visited exactly once. Texts are accumulated
    into a window and embedded in a single batched call per flush.
    """
    count = 0
    batch: list[tuple[str, str, str]] = []
    window = max(1, page_size)
    cursor = collection.find({}).sort("_id", 1)
    async for doc in cursor:
        server = str(doc.get("server", ""))
        name = str(doc.get("name", ""))
        if not server or not name:
            continue
        description = str(doc.get("description", "") or "")
        text = f"{server}\n{name}\n{description}".strip()
        batch.append((server, name, text))
        if len(batch) >= window:
            count += await _flush_catalog_batch(collection, service, batch)
            batch.clear()
    count += await _flush_catalog_batch(collection, service, batch)
    return count


async def _recreate_vector_index(
    collection: Any,
    *,
    dimensions: int,
    wait_for_queryable: bool,
) -> None:
    # Atlas cannot change a vector index's numDimensions in place, so drop and
    # recreate it. The drop is best-effort: a missing index is fine.
    try:
        await collection.drop_search_index(VECTOR_INDEX_NAME)
    except Exception as exc:  # noqa: BLE001 - index may not exist yet
        logger.info("Vector index drop skipped for recreate (%s).", exc)
    await ensure_tool_catalog_indexes(
        collection=collection,
        wait_for_queryable=wait_for_queryable,
        dimensions=dimensions,
    )


async def _reprovision_tenant(
    tenant_id: str,
    *,
    service: EmbeddingService,
    dimensions: int,
    cache_service: SemanticCacheMigrationService,
    wait_for_queryable: bool,
) -> dict[str, Any]:
    tenant_db = get_tenant_database(tenant_id)
    catalog = tenant_db["tool_catalog"]
    page_size = max(1, cache_service.settings.cache_migration_fetch_page_size)
    reembedded = await _reembed_tool_catalog(catalog, service, page_size=page_size)
    await _recreate_vector_index(
        catalog,
        dimensions=dimensions,
        wait_for_queryable=wait_for_queryable,
    )
    cache_summary = await cache_service.migrate(
        tenant_ids=[tenant_id],
        mode="reembed",
        batch_size=page_size,
    )
    target_version = embedding_version_for(service)
    return {
        "tenant_id": tenant_id,
        "target_model": service.model_id,
        "target_dimensions": dimensions,
        "target_version": target_version,
        "catalog_reembedded": reembedded,
        "cache": cache_summary.get("totals", {}),
    }


async def run_reprovision(
    *,
    started_by: str | None = None,
    settings: Settings | None = None,
    wait_for_queryable: bool = False,
) -> dict[str, Any]:
    """Run the full reprovision synchronously and return the final status."""
    settings = settings or get_settings()
    guardrail_service = get_active_embedding_service()
    model_id, dimensions, version = active_embedding_identity()

    completed_tenants = 0
    tenant_total = 0
    await _write_status(
        state="running",
        started_at=_now(),
        finished_at=None,
        started_by=started_by,
        target_model=model_id,
        target_version=version,
        target_dimensions=dimensions,
        error=None,
        tenants=[],
        totals={},
        progress={"completed": 0, "total": 0},
    )

    try:
        await ensure_control_plane_indexes()
        guardrail_count = await resync_guardrail_signatures(embedding_service=guardrail_service)

        tenant_ids = await _all_tenant_ids(settings)
        tenant_total = len(tenant_ids)
        await _write_status(
            state="running",
            tenants=[],
            totals={},
            progress={"completed": 0, "total": tenant_total},
        )
        summaries: list[dict[str, Any]] = []
        catalog_total = 0
        for tenant_id in tenant_ids:
            tenant_service = await get_embedding_service_for(tenant_id, settings)
            tenant_cache_service = SemanticCacheMigrationService(
                settings=settings,
                embedding_service=tenant_service,
            )
            summary = await _reprovision_tenant(
                tenant_id,
                service=tenant_service,
                dimensions=tenant_service.dimensions,
                cache_service=tenant_cache_service,
                wait_for_queryable=wait_for_queryable,
            )
            summaries.append(summary)
            catalog_total += int(summary.get("catalog_reembedded", 0))
            completed_tenants = len(summaries)
            await _write_status(
                state="running",
                tenants=summaries,
                totals={
                    "tenants": tenant_total,
                    "catalog_reembedded": catalog_total,
                    "guardrail_signatures": guardrail_count,
                },
                progress={"completed": completed_tenants, "total": tenant_total},
            )

        # Force discovery clients to re-list against the rebuilt catalog.
        _bump_catalog_version()

        totals = {
            "tenants": len(tenant_ids),
            "catalog_reembedded": catalog_total,
            "guardrail_signatures": guardrail_count,
        }
        await _write_status(
            state="completed",
            finished_at=_now(),
            tenants=summaries,
            totals=totals,
            progress={"completed": tenant_total, "total": tenant_total},
        )
    except Exception as exc:
        logger.exception("Embedding reprovision failed.")
        await _write_status(
            state="failed",
            finished_at=_now(),
            error=str(exc),
            progress={"completed": completed_tenants, "total": tenant_total},
        )
        raise

    return await get_reprovision_status()


async def _run_in_background(*, started_by: str | None) -> None:
    try:
        await run_reprovision(started_by=started_by)
    except Exception:  # status already records the failure; don't crash the loop
        logger.warning("Background embedding reprovision ended with an error.")


async def run_tenant_reprovision(
    tenant_id: str,
    *,
    started_by: str | None = None,
    settings: Settings | None = None,
    wait_for_queryable: bool = False,
) -> dict[str, Any]:
    settings = settings or get_settings()
    status_id = _status_id_for(tenant_id)
    model_id, dimensions, version = await tenant_embedding_identity(tenant_id, settings)
    await _write_status(
        _id=status_id,
        state="running",
        tenant_id=tenant_id,
        started_at=_now(),
        finished_at=None,
        started_by=started_by,
        target_model=model_id,
        target_version=version,
        target_dimensions=dimensions,
        error=None,
        tenants=[],
        totals={},
        progress={"completed": 0, "total": 1},
    )
    try:
        service = await get_embedding_service_for(tenant_id, settings)
        cache_service = SemanticCacheMigrationService(
            settings=settings,
            embedding_service=service,
        )
        summary = await _reprovision_tenant(
            tenant_id,
            service=service,
            dimensions=dimensions,
            cache_service=cache_service,
            wait_for_queryable=wait_for_queryable,
        )
        _bump_catalog_version()
        await _write_status(
            _id=status_id,
            state="completed",
            tenant_id=tenant_id,
            finished_at=_now(),
            tenants=[summary],
            totals={
                "tenants": 1,
                "catalog_reembedded": int(summary.get("catalog_reembedded", 0)),
                "guardrail_signatures": 0,
            },
            progress={"completed": 1, "total": 1},
        )
    except Exception as exc:
        logger.exception("Tenant embedding reprovision failed for %s.", tenant_id)
        await _write_status(
            _id=status_id,
            tenant_id=tenant_id,
            state="failed",
            finished_at=_now(),
            error=str(exc),
            progress={"completed": 0, "total": 1},
        )
        raise

    return await get_tenant_reprovision_status(tenant_id)


async def _run_tenant_in_background(*, tenant_id: str, started_by: str | None) -> None:
    try:
        await run_tenant_reprovision(tenant_id, started_by=started_by)
    except Exception:  # status already records the failure; don't crash the loop
        logger.warning("Background tenant embedding reprovision ended with an error.")


async def trigger_reprovision(*, started_by: str | None = None) -> dict[str, Any]:
    """Start a reprovision in the background and return the initial status.

    Raises :class:`ReprovisionInProgressError` if one is already running.
    """
    status = await get_reprovision_status()
    if _is_running_and_fresh(status):
        raise ReprovisionInProgressError("An embedding reprovision is already in progress.")

    model_id, dimensions, version = active_embedding_identity()
    await _write_status(
        state="running",
        started_at=_now(),
        finished_at=None,
        started_by=started_by,
        target_model=model_id,
        target_version=version,
        target_dimensions=dimensions,
        error=None,
        tenants=[],
        totals={},
        progress={"completed": 0, "total": 0},
    )

    task = asyncio.create_task(_run_in_background(started_by=started_by))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return await get_reprovision_status()


async def trigger_tenant_reprovision(
    tenant_id: str,
    *,
    started_by: str | None = None,
) -> dict[str, Any]:
    status = await get_tenant_reprovision_status(tenant_id)
    if _is_running_and_fresh(status):
        raise ReprovisionInProgressError(
            f"An embedding reprovision is already in progress for tenant '{tenant_id}'."
        )

    model_id, dimensions, version = await tenant_embedding_identity(tenant_id)
    await _write_status(
        _id=_status_id_for(tenant_id),
        state="running",
        tenant_id=tenant_id,
        started_at=_now(),
        finished_at=None,
        started_by=started_by,
        target_model=model_id,
        target_version=version,
        target_dimensions=dimensions,
        error=None,
        tenants=[],
        totals={},
        progress={"completed": 0, "total": 1},
    )

    task = asyncio.create_task(
        _run_tenant_in_background(tenant_id=tenant_id, started_by=started_by)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return await get_tenant_reprovision_status(tenant_id)
