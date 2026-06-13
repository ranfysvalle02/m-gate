"""Read-only tenant data exploration via the sandbox DB bridge."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Query, Request, status

from models.admin import (
    ExploreCollectionsResponse,
    ExploreQueryRequest,
    ExploreQueryResponse,
    ExploreSampleRequest,
    ExploreSampleResponse,
)
from services.sandbox_db_bridge import SandboxDbBridge

from . import _common as c
from ._common import _require_tenant_admin, _resolve_target_tenant, router, settings


def _field_type_summary(sample_docs: list[dict[str, Any]]) -> dict[str, str]:
    summary: dict[str, str] = {}
    for doc in sample_docs:
        if not isinstance(doc, dict):
            continue
        for key, value in doc.items():
            if key in summary:
                continue
            summary[key] = type(value).__name__
    return summary


def _find_snippet(collection: str, filter_doc: dict[str, Any], limit: int) -> str:
    return (
        f"context.db[{json.dumps(collection)}].find("
        f"{json.dumps(filter_doc, indent=2)}, limit={int(limit)})"
    )


def _aggregate_snippet(collection: str, pipeline: list[dict[str, Any]]) -> str:
    return f"context.db[{json.dumps(collection)}].aggregate({json.dumps(pipeline, indent=2)})"


async def _bridge_read(
    *,
    tenant_id: str,
    op: str,
    collection: str,
    args: list[Any],
    kwargs: dict[str, Any] | None = None,
) -> Any:
    bridge = SandboxDbBridge(
        tenant_id=tenant_id,
        action_type="read",
        settings=settings,
        max_calls_override=5,
    )
    frame = await bridge.handle(
        {
            "id": f"admin-explore-{op}",
            "op": op,
            "collection": collection,
            "args": args,
            "kwargs": kwargs or {},
        }
    )
    if not frame.get("ok"):
        raw_error = frame.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        message = str(error.get("message") or "Explore query failed.")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)
    return frame.get("result")


def _require_db_bridge_enabled() -> None:
    if settings.sandbox_db_bridge_enabled:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Virtual DB bridge is disabled. Enable SANDBOX_DB_BRIDGE_ENABLED.",
    )


@router.get("/explore/collections", response_model=ExploreCollectionsResponse)
async def explore_collections(
    request: Request,
    tenant_id: str | None = Query(default=None),
) -> ExploreCollectionsResponse:
    _require_tenant_admin(request)
    _require_db_bridge_enabled()
    target_tenant = _resolve_target_tenant(
        request, tenant_id if isinstance(tenant_id, str) else None
    )
    names = await c.get_tenant_database(target_tenant).list_collection_names()
    collections = sorted(
        name for name in names if isinstance(name, str) and name and not name.startswith("system.")
    )
    return ExploreCollectionsResponse(tenant_id=target_tenant, collections=collections)


@router.post("/explore/sample", response_model=ExploreSampleResponse)
async def explore_sample(
    request: Request,
    payload: ExploreSampleRequest,
) -> ExploreSampleResponse:
    _require_tenant_admin(request)
    _require_db_bridge_enabled()
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    limit = max(1, min(int(payload.limit), int(settings.sandbox_db_max_docs)))
    raw = await _bridge_read(
        tenant_id=target_tenant,
        op="find",
        collection=payload.collection,
        args=[{}],
        kwargs={"limit": limit},
    )
    sample_docs = [doc for doc in (raw or []) if isinstance(doc, dict)]
    return ExploreSampleResponse(
        tenant_id=target_tenant,
        collection=payload.collection,
        limit=limit,
        field_types=_field_type_summary(sample_docs),
        sample_docs=sample_docs,
        snippet=_find_snippet(payload.collection, {}, limit),
    )


@router.post("/explore/query", response_model=ExploreQueryResponse)
async def explore_query(
    request: Request,
    payload: ExploreQueryRequest,
) -> ExploreQueryResponse:
    _require_tenant_admin(request)
    _require_db_bridge_enabled()
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    limit = max(1, min(int(payload.limit), int(settings.sandbox_db_max_docs)))
    if payload.mode == "aggregate":
        pipeline = [stage for stage in payload.pipeline if isinstance(stage, dict)]
        raw = await _bridge_read(
            tenant_id=target_tenant,
            op="aggregate",
            collection=payload.collection,
            args=[pipeline],
        )
        snippet = _aggregate_snippet(payload.collection, pipeline or [{"$limit": limit}])
    else:
        filter_doc = payload.filter if isinstance(payload.filter, dict) else {}
        raw = await _bridge_read(
            tenant_id=target_tenant,
            op="find",
            collection=payload.collection,
            args=[filter_doc],
            kwargs={"limit": limit},
        )
        snippet = _find_snippet(payload.collection, filter_doc, limit)

    results = [doc for doc in (raw or []) if isinstance(doc, dict)]
    return ExploreQueryResponse(
        tenant_id=target_tenant,
        collection=payload.collection,
        mode=payload.mode,
        limit=limit,
        results=results,
        snippet=snippet,
    )
