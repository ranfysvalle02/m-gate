"""Admin analytics: scalable cross-tenant / per-tenant usage and telemetry rollups.

Every endpoint is server-side aggregated (``$group`` pipelines, ``count_documents``)
so no full collection is ever buffered into the gateway. Scope is derived from the
caller's role, exactly like the rest of the admin surface:

* a **platform-admin** with no explicit tenant sees a cross-tenant rollup
  (``scope="platform"``); passing ``?tenant_id=`` narrows to that one tenant.
* a **tenant-admin** is always confined to their own tenant (``scope="tenant"``);
  ``_resolve_target_tenant`` raises 403 if they try to reach another tenant.

The control-plane collections (``usage_counters`` / ``usage_events``) live in one
database, so their rollups are a single aggregation. ``audit_telemetry`` is a
*per-tenant* time-series (one collection per tenant DB), so the telemetry trend
iterates the bounded tenant list and merges the per-tenant buckets in memory.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Query, Request

from models.admin import (
    AnalyticsOverviewResponse,
    QuotaUtilizationEntry,
    QuotaUtilizationResponse,
    TelemetryTrendPoint,
    TelemetryTrendResponse,
    TopToolEntry,
    TopToolsResponse,
    UsageTrendPoint,
    UsageTrendResponse,
)
from services.account_tier import CONFIRMATION_UNCONFIRMED
from services.usage_metering import (
    TENANT_QUOTAS_COLLECTION,
    USAGE_COUNTERS_COLLECTION,
    USAGE_EVENTS_COLLECTION,
    current_period,
)

from . import _common as c
from ._common import (
    _is_platform_admin,
    _require_tenant_admin,
    _resolve_target_tenant,
    router,
    settings,
)

# A telemetry record is counted as an "error" when its status matches any of these
# failure markers (case-insensitive). Successes ("live_execution_success",
# "cache_hit", ...) do not match, so the complement is the success count.
_TELEMETRY_ERROR_REGEX = (
    "error|fail|exceed|forbidden|denied|deny|invalid|timeout|block|reject|unauthor"
)


def _analytics_scope(
    request: Request, requested_tenant: str | None
) -> tuple[str, list[str] | None]:
    """Resolve (scope, tenant_ids) for an analytics request.

    Returns ``("platform", None)`` for a platform-admin with no explicit tenant
    (aggregate across all tenants, no tenant filter), otherwise
    ``("tenant", [target])`` confined to a single tenant. ``_resolve_target_tenant``
    enforces that a tenant-admin can only ever name their own tenant.
    """
    explicit = requested_tenant or request.headers.get("x-tenant-id")
    if _is_platform_admin(request) and not explicit:
        return "platform", None
    target = _resolve_target_tenant(request, requested_tenant)
    return "tenant", [target]


async def _all_tenant_ids() -> list[str]:
    docs = (
        await c.get_control_database()["tenants"].find({}, {"tenant_id": 1}).to_list(length=10_000)
    )
    ids = sorted({str(d.get("tenant_id")) for d in docs if isinstance(d.get("tenant_id"), str)})
    return ids or [settings.default_tenant_id]


def _recent_periods(months: int, *, now: datetime | None = None) -> list[str]:
    """Return the last ``months`` period keys (``YYYY-MM``), oldest first."""
    cursor = now or datetime.now(UTC)
    year, month = cursor.year, cursor.month
    periods: list[str] = []
    for _ in range(max(1, months)):
        periods.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return sorted(periods)


def _extract_percentile(value: Any) -> float | None:
    """Normalize a ``$percentile`` result (a single-element list) to a float."""
    if isinstance(value, list):
        return float(value[0]) if value else None
    if isinstance(value, int | float):
        return float(value)
    return None


@router.get("/analytics/overview", response_model=AnalyticsOverviewResponse)
async def analytics_overview(
    request: Request,
    tenant_id: str | None = Query(default=None),
) -> AnalyticsOverviewResponse:
    """Headline KPIs for the current period plus (platform-only) beta headroom."""
    _require_tenant_admin(request)
    scope, tenant_ids = _analytics_scope(request, tenant_id)
    period = current_period()
    control = c.get_control_database()

    match: dict[str, Any] = {"period": period}
    if scope == "tenant":
        match["tenant_id"] = {"$in": tenant_ids}
    cursor = await control[USAGE_COUNTERS_COLLECTION].aggregate(
        [
            {"$match": match},
            {
                "$group": {
                    "_id": None,
                    "calls": {"$sum": "$calls"},
                    "sandbox_ms": {"$sum": "$sandbox_ms"},
                }
            },
        ]
    )
    rows = await cursor.to_list(length=1)
    totals = rows[0] if rows else {}
    calls = int(totals.get("calls", 0) or 0)
    sandbox_ms = int(totals.get("sandbox_ms", 0) or 0)

    if scope == "platform":
        # Counts only — never buffer the tenant/user collections. Confirmation is
        # always stored normalized (services.account_tier._normalize_confirmation),
        # so an exact equality count matches the unconfirmed tier; everything else
        # (explicit "confirmed" + the unset default) is confirmed.
        tenants = control["tenants"]
        tenant_count = await tenants.count_documents({})
        unconfirmed = await tenants.count_documents({"confirmation": CONFIRMATION_UNCONFIRMED})
        confirmed = max(0, tenant_count - unconfirmed)
        self_registered = await control["users"].count_documents({"self_registered": True})
        return AnalyticsOverviewResponse(
            scope="platform",
            period=period,
            tenant_count=tenant_count,
            calls=calls,
            sandbox_ms=sandbox_ms,
            confirmed_count=confirmed,
            unconfirmed_count=unconfirmed,
            self_registered_count=int(self_registered),
            self_registration_max_tenants=int(settings.self_registration_max_tenants),
        )

    return AnalyticsOverviewResponse(
        scope="tenant",
        period=period,
        tenant_count=len(tenant_ids or []),
        calls=calls,
        sandbox_ms=sandbox_ms,
    )


@router.get("/analytics/usage-trend", response_model=UsageTrendResponse)
async def analytics_usage_trend(
    request: Request,
    months: int = Query(default=6, ge=1, le=24),
    tenant_id: str | None = Query(default=None),
) -> UsageTrendResponse:
    """Per-period ``calls`` / ``sandbox_ms`` time series over the last N months."""
    _require_tenant_admin(request)
    scope, tenant_ids = _analytics_scope(request, tenant_id)
    periods = _recent_periods(months)

    match: dict[str, Any] = {"period": {"$in": periods}}
    if scope == "tenant":
        match["tenant_id"] = {"$in": tenant_ids}
    cursor = await c.get_control_database()[USAGE_COUNTERS_COLLECTION].aggregate(
        [
            {"$match": match},
            {
                "$group": {
                    "_id": "$period",
                    "calls": {"$sum": "$calls"},
                    "sandbox_ms": {"$sum": "$sandbox_ms"},
                }
            },
        ]
    )
    rows = await cursor.to_list(length=10_000)
    by_period = {str(r.get("_id")): r for r in rows}
    points = [
        UsageTrendPoint(
            period=p,
            calls=int(by_period.get(p, {}).get("calls", 0) or 0),
            sandbox_ms=int(by_period.get(p, {}).get("sandbox_ms", 0) or 0),
        )
        for p in periods
    ]
    return UsageTrendResponse(scope=scope, points=points)


@router.get("/analytics/top-tools", response_model=TopToolsResponse)
async def analytics_top_tools(
    request: Request,
    period: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    tenant_id: str | None = Query(default=None),
) -> TopToolsResponse:
    """Most-invoked tools and servers for a period, from ``usage_events``."""
    _require_tenant_admin(request)
    scope, tenant_ids = _analytics_scope(request, tenant_id)
    resolved_period = period or current_period()

    match: dict[str, Any] = {"period": resolved_period, "kind": "calls"}
    if scope == "tenant":
        match["tenant_id"] = {"$in": tenant_ids}
    events = c.get_control_database()[USAGE_EVENTS_COLLECTION]

    tool_cursor = await events.aggregate(
        [
            {"$match": match},
            {
                "$group": {
                    "_id": {"server": "$metadata.server", "tool": "$metadata.tool"},
                    "calls": {"$sum": "$amount"},
                }
            },
            {"$sort": {"calls": -1}},
            {"$limit": limit},
        ]
    )
    tool_rows = await tool_cursor.to_list(length=limit)
    server_cursor = await events.aggregate(
        [
            {"$match": match},
            {"$group": {"_id": "$metadata.server", "calls": {"$sum": "$amount"}}},
            {"$sort": {"calls": -1}},
            {"$limit": limit},
        ]
    )
    server_rows = await server_cursor.to_list(length=limit)

    tools = [
        TopToolEntry(
            server=str((r.get("_id") or {}).get("server") or "unknown"),
            tool=str((r.get("_id") or {}).get("tool") or "unknown"),
            calls=int(r.get("calls", 0) or 0),
        )
        for r in tool_rows
    ]
    servers = [
        TopToolEntry(
            server=str(r.get("_id") or "unknown"),
            calls=int(r.get("calls", 0) or 0),
        )
        for r in server_rows
    ]
    return TopToolsResponse(scope=scope, period=resolved_period, tools=tools, servers=servers)


async def _telemetry_trend_for_tenant(
    tenant_id: str, *, since: datetime, bin_size: int
) -> list[dict[str, Any]]:
    pipeline = [
        {"$match": {"timestamp": {"$gte": since}}},
        {
            "$group": {
                "_id": {"$dateTrunc": {"date": "$timestamp", "unit": "hour", "binSize": bin_size}},
                "total": {"$sum": 1},
                "errors": {
                    "$sum": {
                        "$cond": [
                            {
                                "$regexMatch": {
                                    "input": {"$ifNull": ["$status", ""]},
                                    "regex": _TELEMETRY_ERROR_REGEX,
                                    "options": "i",
                                }
                            },
                            1,
                            0,
                        ]
                    }
                },
                "latency_avg_ms": {"$avg": "$latency_ms"},
                "latency_p95_ms": {
                    "$percentile": {
                        "input": "$latency_ms",
                        "p": [0.95],
                        "method": "approximate",
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]
    cursor = await c.get_tenant_database(tenant_id)["audit_telemetry"].aggregate(pipeline)
    return await cursor.to_list(length=10_000)


@router.get("/analytics/telemetry-trend", response_model=TelemetryTrendResponse)
async def analytics_telemetry_trend(
    request: Request,
    hours: int = Query(default=24, ge=1, le=168),
    tenant_id: str | None = Query(default=None),
) -> TelemetryTrendResponse:
    """Success/error counts and latency over time from per-tenant ``audit_telemetry``.

    Buckets are hourly. Platform scope merges the bounded per-tenant series:
    counts add, average latency is total-weighted, and p95 takes the per-tenant
    max as a conservative upper-bound proxy (exact cross-DB percentiles are not
    computable without a rollup collection).
    """
    _require_tenant_admin(request)
    scope, tenant_ids = _analytics_scope(request, tenant_id)
    since = datetime.now(UTC) - timedelta(hours=hours)
    bin_size = 1
    target_tenants = tenant_ids if scope == "tenant" else await _all_tenant_ids()

    merged: dict[datetime, dict[str, Any]] = {}
    for tid in target_tenants or []:
        for row in await _telemetry_trend_for_tenant(tid, since=since, bin_size=bin_size):
            bucket = row.get("_id")
            if not isinstance(bucket, datetime):
                continue
            total = int(row.get("total", 0) or 0)
            errors = int(row.get("errors", 0) or 0)
            avg = row.get("latency_avg_ms")
            p95 = _extract_percentile(row.get("latency_p95_ms"))
            slot = merged.setdefault(
                bucket,
                {"total": 0, "errors": 0, "latency_weight": 0.0, "p95": None},
            )
            slot["total"] += total
            slot["errors"] += errors
            if isinstance(avg, int | float) and total:
                slot["latency_weight"] += float(avg) * total
            if p95 is not None:
                slot["p95"] = p95 if slot["p95"] is None else max(slot["p95"], p95)

    points = [
        TelemetryTrendPoint(
            bucket=bucket,
            total=slot["total"],
            errors=slot["errors"],
            latency_avg_ms=(slot["latency_weight"] / slot["total"]) if slot["total"] else None,
            latency_p95_ms=slot["p95"],
        )
        for bucket, slot in sorted(merged.items())
    ]
    return TelemetryTrendResponse(scope=scope, points=points)


@router.get("/analytics/quota-utilization", response_model=QuotaUtilizationResponse)
async def analytics_quota_utilization(
    request: Request,
    tenant_id: str | None = Query(default=None),
) -> QuotaUtilizationResponse:
    """Per-tenant usage-vs-quota utilization for the current period."""
    _require_tenant_admin(request)
    scope, tenant_ids = _analytics_scope(request, tenant_id)
    period = current_period()
    control = c.get_control_database()

    match: dict[str, Any] = {"period": period}
    if scope == "tenant":
        match["tenant_id"] = {"$in": tenant_ids}
    cursor = await control[USAGE_COUNTERS_COLLECTION].aggregate(
        [
            {"$match": match},
            {
                "$group": {
                    "_id": "$tenant_id",
                    "calls": {"$sum": "$calls"},
                    "sandbox_ms": {"$sum": "$sandbox_ms"},
                }
            },
        ]
    )
    usage_rows = await cursor.to_list(length=10_000)
    usage_by_tenant = {str(r.get("_id")): r for r in usage_rows}

    # A single-tenant view should still render even with zero usage this period.
    if scope == "tenant" and tenant_ids:
        usage_by_tenant.setdefault(tenant_ids[0], {"_id": tenant_ids[0]})

    default_calls = max(0, int(settings.default_quota_calls_per_period))
    default_sandbox = max(0, int(settings.default_quota_sandbox_seconds_per_period))
    quota_docs = (
        await control[TENANT_QUOTAS_COLLECTION]
        .find({"_id": {"$in": list(usage_by_tenant.keys())}})
        .to_list(length=10_000)
    )
    quota_by_tenant = {str(d.get("_id")): d for d in quota_docs}

    entries: list[QuotaUtilizationEntry] = []
    for tid, usage in usage_by_tenant.items():
        quota = quota_by_tenant.get(tid, {})
        calls_limit = max(0, int(quota.get("calls_limit", default_calls) or 0))
        sandbox_limit = max(0, int(quota.get("sandbox_seconds_limit", default_sandbox) or 0))
        calls = int(usage.get("calls", 0) or 0)
        sandbox_ms = int(usage.get("sandbox_ms", 0) or 0)
        entries.append(
            QuotaUtilizationEntry(
                tenant_id=tid,
                period=period,
                calls=calls,
                calls_limit=calls_limit,
                sandbox_ms=sandbox_ms,
                sandbox_seconds_limit=sandbox_limit,
                calls_utilization_pct=(
                    round(calls / calls_limit * 100, 1) if calls_limit > 0 else None
                ),
                sandbox_utilization_pct=(
                    round(sandbox_ms / (sandbox_limit * 1000) * 100, 1)
                    if sandbox_limit > 0
                    else None
                ),
            )
        )
    entries.sort(
        key=lambda e: (e.calls_utilization_pct or 0, e.calls),
        reverse=True,
    )
    return QuotaUtilizationResponse(scope=scope, period=period, tenants=entries)
