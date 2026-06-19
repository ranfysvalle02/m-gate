"""Tool catalog listing, telemetry, aggregate stats, search, and cache migration."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from models.admin import (
    AdminSearchRequest,
    CacheMigrateRequest,
    CatalogItemResponse,
    CatalogListRequest,
    CatalogListResponse,
    StatsResponse,
    TelemetryEventResponse,
    TelemetryListRequest,
    TelemetryListResponse,
    TenantStats,
)
from services.registry_watcher import get_catalog_version
from services.tenant_tool_policy import filter_available_tools

from . import _common as c
from ._common import (
    _build_time_range,
    _is_platform_admin,
    _require_tenant_admin,
    _resolve_target_tenant,
    router,
    settings,
)


async def _resolve_target_tenants_for_cache_migration(
    request: Request,
    *,
    requested_tenant: str | None,
) -> list[str]:
    if requested_tenant:
        return [_resolve_target_tenant(request, requested_tenant)]

    if _is_platform_admin(request):
        docs = await c.get_control_database()["tenants"].find({}).to_list(length=10_000)
        tenant_ids = sorted(
            {str(doc.get("tenant_id")) for doc in docs if isinstance(doc.get("tenant_id"), str)}
        )
        return tenant_ids or [settings.default_tenant_id]

    return [getattr(request.state, "tenant_id", settings.default_tenant_id)]


@router.post("/cache/migrate")
async def migrate_cache(request: Request, payload: CacheMigrateRequest) -> dict[str, Any]:
    tenant_ids = await _resolve_target_tenants_for_cache_migration(
        request,
        requested_tenant=payload.tenant_id,
    )
    for tenant_id in tenant_ids:
        await c.provision_tenant(tenant_id, wait_for_queryable_indexes=False)
    return await c.cache_migration_service.migrate(
        tenant_ids=tenant_ids,
        mode=payload.mode,
        batch_size=payload.batch_size,
    )


@router.get("/catalog", response_model=CatalogListResponse)
async def list_catalog(
    request: Request,
    tenant_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> CatalogListResponse:
    params = CatalogListRequest(tenant_id=tenant_id, limit=limit, offset=offset)
    target_tenant = _resolve_target_tenant(request, params.tenant_id)
    docs = (
        await c.get_tenant_database(target_tenant)["tool_catalog"].find({}).to_list(length=10_000)
    )
    # Read-only (viewer) principals see only the curated set so a showcase console
    # mirrors what the data plane exposes; admins keep the full catalog for editing.
    if getattr(request.state, "is_read_only_principal", False):
        docs = list(await filter_available_tools(target_tenant, docs))
    docs.sort(key=lambda item: (str(item.get("server", "")), str(item.get("name", ""))))
    window = docs[offset : offset + params.limit]
    items = [
        CatalogItemResponse(
            server=str(doc.get("server", "")),
            name=str(doc.get("name", "")),
            description=str(doc.get("description", "")),
            scopes=[scope for scope in doc.get("scopes", []) if isinstance(scope, str)],
            input_schema=doc.get("input_schema")
            if isinstance(doc.get("input_schema"), dict)
            else {},
            metadata=doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {},
            transport=str((doc.get("metadata") or {}).get("transport", "")) or None,
            action_type=str((doc.get("metadata") or {}).get("action_type", "")) or None,
            updated_at=doc.get("updated_at"),
        )
        for doc in window
    ]
    return CatalogListResponse(
        tenant_id=target_tenant,
        items=items,
        total=len(docs),
        limit=params.limit,
        offset=params.offset,
    )


@router.get("/telemetry", response_model=TelemetryListResponse)
async def list_telemetry(
    request: Request,
    tenant_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> TelemetryListResponse:
    params = TelemetryListRequest(tenant_id=tenant_id, limit=limit)
    target_tenant = _resolve_target_tenant(request, params.tenant_id)
    docs = (
        await c.get_tenant_database(target_tenant)["audit_telemetry"]
        .find({})
        .to_list(length=10_000)
    )
    docs.sort(
        key=lambda item: item.get("timestamp")
        if isinstance(item.get("timestamp"), datetime)
        else datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    items = [
        TelemetryEventResponse(
            timestamp=doc.get("timestamp"),
            tenant_id=str(doc.get("tenant_id", target_tenant)),
            user_id=str(doc.get("user_id", "unknown-user")),
            request_id=doc.get("request_id"),
            method=str(doc.get("method", "")),
            status=str(doc.get("status", "")),
            latency_ms=doc.get("latency_ms"),
            metadata=doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {},
        )
        for doc in docs[: params.limit]
    ]
    return TelemetryListResponse(tenant_id=target_tenant, items=items)


@router.get("/telemetry/export")
async def export_telemetry(
    request: Request,
    tenant_id: str | None = Query(default=None),
    export_format: str = Query(default="jsonl", alias="format"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream a tenant's ``audit_telemetry`` as JSONL (one object per line).

    Records are iterated off a ``find()`` cursor (``async for``) over the
    ``timestamp`` range and serialized line by line, so the full time-series is
    never loaded into memory the way the buffered ``/telemetry`` listing is.
    """
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    if export_format != "jsonl":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only format=jsonl is supported for telemetry export.",
        )

    query: dict[str, Any] = {}
    ts_range = _build_time_range(from_, to)
    if ts_range is not None:
        query["timestamp"] = ts_range

    cursor = (
        c.get_tenant_database(target_tenant)["audit_telemetry"].find(query).sort("timestamp", 1)
    )

    async def _stream() -> AsyncIterator[str]:
        async for doc in cursor:
            doc.pop("_id", None)
            yield json.dumps(doc, default=str, separators=(",", ":")) + "\n"

    filename = f"telemetry-{target_tenant}.jsonl"
    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/stats", response_model=StatsResponse)
async def admin_stats(request: Request) -> StatsResponse:
    roles = set(getattr(request.state, "roles", []))
    is_platform_admin = settings.platform_admin_role in roles
    if is_platform_admin:
        tenant_docs = (
            await c.get_control_database()["tenants"]
            .find({}, {"tenant_id": 1})
            .to_list(length=10_000)
        )
        tenant_ids = sorted(
            {
                str(doc.get("tenant_id"))
                for doc in tenant_docs
                if isinstance(doc.get("tenant_id"), str)
            }
        )
    else:
        tenant_ids = [str(getattr(request.state, "tenant_id", settings.default_tenant_id))]
    if not tenant_ids:
        tenant_ids = [settings.default_tenant_id]

    tenant_rows: list[TenantStats] = []
    status_counts: dict[str, int] = {}
    for tenant_id in tenant_ids:
        tenant_db = c.get_tenant_database(tenant_id)
        # Counts and a status rollup, computed server-side: no collection (and in
        # particular not the unbounded audit_telemetry time-series) is loaded into
        # the gateway. "enabled" defaults to true, so a missing flag counts as
        # enabled via {"$ne": False}.
        server_count = await tenant_db["routing_registry"].count_documents({})
        enabled_server_count = await tenant_db["routing_registry"].count_documents(
            {"enabled": {"$ne": False}}
        )
        tool_count = await tenant_db["tool_catalog"].count_documents({})
        status_cursor = await tenant_db["audit_telemetry"].aggregate(
            [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
        )
        for row in await status_cursor.to_list(length=10_000):
            status_key = str(row.get("_id") or "unknown")
            status_counts[status_key] = status_counts.get(status_key, 0) + int(
                row.get("count", 0) or 0
            )
        tenant_rows.append(
            TenantStats(
                tenant_id=tenant_id,
                server_count=int(server_count),
                enabled_server_count=int(enabled_server_count),
                tool_count=int(tool_count),
            )
        )
    tenant_rows.sort(key=lambda row: row.tenant_id)
    return StatsResponse(
        tenant_count=len(tenant_ids) if is_platform_admin else None,
        catalog_version=get_catalog_version(),
        telemetry_status_counts=status_counts,
        tenants=tenant_rows,
    )


@router.post("/search")
async def admin_search(request: Request, payload: AdminSearchRequest) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    items = await c.hybrid_search_service.search_tools(
        tenant_id=target_tenant,
        query=payload.query,
        limit=payload.limit,
        vector_weight=payload.vector_weight,
        text_weight=payload.text_weight,
        mode=payload.mode,
        server=payload.server,
    )
    return {
        "tenant_id": target_tenant,
        "mode": payload.mode,
        "server": payload.server,
        "items": items,
    }
