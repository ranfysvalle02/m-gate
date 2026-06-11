from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo import ReturnDocument

from config.settings import Settings, get_settings
from database.mongo import get_control_database

USAGE_COUNTERS_COLLECTION = "usage_counters"
TENANT_QUOTAS_COLLECTION = "tenant_quotas"
USAGE_EVENTS_COLLECTION = "usage_events"


def _usage_collection():
    return get_control_database()[USAGE_COUNTERS_COLLECTION]


def _quota_collection():
    return get_control_database()[TENANT_QUOTAS_COLLECTION]


def _events_collection():
    return get_control_database()[USAGE_EVENTS_COLLECTION]


def _non_negative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def current_period(*, now: datetime | None = None, settings: Settings | None = None) -> str:
    now_utc = now or datetime.now(UTC)
    active_settings = settings or get_settings()
    if active_settings.usage_quota_period != "monthly":
        return now_utc.strftime("%Y-%m")
    return now_utc.strftime("%Y-%m")


def _usage_snapshot(doc: dict[str, Any] | None, *, tenant_id: str, period: str) -> dict[str, Any]:
    source = doc or {}
    return {
        "tenant_id": tenant_id,
        "period": period,
        "calls": _non_negative_int(source.get("calls"), default=0),
        "sandbox_ms": _non_negative_int(source.get("sandbox_ms"), default=0),
    }


def _quota_snapshot(doc: dict[str, Any] | None, *, settings: Settings) -> dict[str, int]:
    source = doc or {}
    return {
        "calls_limit": _non_negative_int(
            source.get("calls_limit"),
            default=_non_negative_int(settings.default_quota_calls_per_period),
        ),
        "sandbox_seconds_limit": _non_negative_int(
            source.get("sandbox_seconds_limit"),
            default=_non_negative_int(settings.default_quota_sandbox_seconds_per_period),
        ),
    }


async def record_usage(
    tenant_id: str,
    *,
    calls: int = 0,
    sandbox_ms: int = 0,
    period: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    resolved_period = period or current_period(now=now)
    call_delta = _non_negative_int(calls)
    sandbox_delta = _non_negative_int(sandbox_ms)
    if call_delta == 0 and sandbox_delta == 0:
        return await get_usage(tenant_id, period=resolved_period)

    increments: dict[str, int] = {}
    if call_delta > 0:
        increments["calls"] = call_delta
    if sandbox_delta > 0:
        increments["sandbox_ms"] = sandbox_delta

    doc = await _usage_collection().find_one_and_update(
        {"tenant_id": tenant_id, "period": resolved_period},
        {
            "$inc": increments,
            "$set": {"tenant_id": tenant_id, "period": resolved_period, "updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return _usage_snapshot(doc, tenant_id=tenant_id, period=resolved_period)


async def get_usage(tenant_id: str, *, period: str | None = None) -> dict[str, Any]:
    resolved_period = period or current_period()
    doc = await _usage_collection().find_one({"tenant_id": tenant_id, "period": resolved_period})
    return _usage_snapshot(doc, tenant_id=tenant_id, period=resolved_period)


async def get_effective_quota(
    tenant_id: str, *, settings: Settings | None = None
) -> dict[str, int]:
    active_settings = settings or get_settings()
    doc = await _quota_collection().find_one({"_id": tenant_id})
    return _quota_snapshot(doc, settings=active_settings)


async def set_quota(
    tenant_id: str,
    *,
    calls_limit: int,
    sandbox_seconds_limit: int,
    updated_by: str | None = None,
) -> dict[str, int]:
    now = datetime.now(UTC)
    calls_cap = _non_negative_int(calls_limit)
    sandbox_cap = _non_negative_int(sandbox_seconds_limit)
    payload = {
        "_id": tenant_id,
        "tenant_id": tenant_id,
        "calls_limit": calls_cap,
        "sandbox_seconds_limit": sandbox_cap,
        "updated_at": now,
        "updated_by": updated_by,
    }
    await _quota_collection().replace_one({"_id": tenant_id}, payload, upsert=True)
    return {
        "calls_limit": calls_cap,
        "sandbox_seconds_limit": sandbox_cap,
    }


async def check_quota(
    tenant_id: str,
    *,
    period: str | None = None,
    settings: Settings | None = None,
) -> tuple[bool, str | None, dict[str, Any], dict[str, int]]:
    active_settings = settings or get_settings()
    resolved_period = period or current_period(settings=active_settings)
    usage = await get_usage(tenant_id, period=resolved_period)
    quota = await get_effective_quota(tenant_id, settings=active_settings)

    calls_limit = quota["calls_limit"]
    if calls_limit > 0 and int(usage["calls"]) >= calls_limit:
        return False, "calls_limit_exceeded", usage, quota

    sandbox_limit_seconds = quota["sandbox_seconds_limit"]
    if sandbox_limit_seconds > 0 and int(usage["sandbox_ms"]) >= (sandbox_limit_seconds * 1000):
        return False, "sandbox_seconds_limit_exceeded", usage, quota

    return True, None, usage, quota


async def emit_billing_event(
    tenant_id: str,
    *,
    kind: str,
    amount: int,
    period: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    event_amount = _non_negative_int(amount)
    if event_amount <= 0:
        return
    now = datetime.now(UTC)
    resolved_period = period or current_period(now=now)
    await _events_collection().insert_one(
        {
            "tenant_id": tenant_id,
            "period": resolved_period,
            "kind": kind,
            "amount": event_amount,
            "ts": now,
            "metadata": metadata or {},
        }
    )


async def summarize_billing_events(
    tenant_id: str,
    *,
    period: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    resolved_period = period or current_period()
    docs = (
        await _events_collection()
        .find({"tenant_id": tenant_id, "period": resolved_period})
        .to_list(length=10_000)
    )
    docs.sort(
        key=lambda item: item.get("ts")
        if isinstance(item.get("ts"), datetime)
        else datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )

    totals_by_kind: dict[str, int] = {}
    for doc in docs:
        kind = str(doc.get("kind") or "unknown")
        totals_by_kind[kind] = totals_by_kind.get(kind, 0) + _non_negative_int(doc.get("amount"))

    max_events = max(1, int(limit))
    events = [
        {
            "kind": str(doc.get("kind") or ""),
            "amount": _non_negative_int(doc.get("amount")),
            "period": str(doc.get("period") or resolved_period),
            "ts": doc.get("ts"),
            "metadata": doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {},
        }
        for doc in docs[:max_events]
    ]
    return {
        "tenant_id": tenant_id,
        "period": resolved_period,
        "totals_by_kind": totals_by_kind,
        "total_amount": sum(totals_by_kind.values()),
        "events": events,
    }
