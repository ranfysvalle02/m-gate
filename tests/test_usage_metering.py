from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services import usage_metering


@pytest.mark.asyncio
async def test_current_period_uses_utc_month():
    period = usage_metering.current_period(now=datetime(2026, 6, 10, 12, 0, tzinfo=UTC))
    assert period == "2026-06"


@pytest.mark.asyncio
async def test_record_usage_accumulates_with_inc_upsert(patch_mongo):
    result_a = await usage_metering.record_usage("local-dev", calls=2, sandbox_ms=500)
    result_b = await usage_metering.record_usage("local-dev", calls=1, sandbox_ms=250)
    assert result_a["calls"] == 2
    assert result_b["calls"] == 3
    assert result_b["sandbox_ms"] == 750


@pytest.mark.asyncio
async def test_get_effective_quota_uses_override_when_present(patch_mongo):
    settings = usage_metering.get_settings()
    original_calls = settings.default_quota_calls_per_period
    original_sandbox = settings.default_quota_sandbox_seconds_per_period
    object.__setattr__(settings, "default_quota_calls_per_period", 100)
    object.__setattr__(settings, "default_quota_sandbox_seconds_per_period", 200)
    try:
        fallback = await usage_metering.get_effective_quota("tenant-a")
        assert fallback == {"calls_limit": 100, "sandbox_seconds_limit": 200}

        await usage_metering.set_quota(
            "tenant-a",
            calls_limit=12,
            sandbox_seconds_limit=34,
            updated_by="platform-admin",
        )
        overridden = await usage_metering.get_effective_quota("tenant-a")
        assert overridden == {"calls_limit": 12, "sandbox_seconds_limit": 34}
    finally:
        object.__setattr__(settings, "default_quota_calls_per_period", original_calls)
        object.__setattr__(
            settings,
            "default_quota_sandbox_seconds_per_period",
            original_sandbox,
        )


@pytest.mark.asyncio
async def test_check_quota_unlimited_allows_usage(patch_mongo):
    settings = usage_metering.get_settings()
    original_calls = settings.default_quota_calls_per_period
    original_sandbox = settings.default_quota_sandbox_seconds_per_period
    object.__setattr__(settings, "default_quota_calls_per_period", 0)
    object.__setattr__(settings, "default_quota_sandbox_seconds_per_period", 0)
    try:
        await usage_metering.record_usage("local-dev", calls=999, sandbox_ms=999_999)
        allowed, reason, usage, quota = await usage_metering.check_quota("local-dev")
    finally:
        object.__setattr__(settings, "default_quota_calls_per_period", original_calls)
        object.__setattr__(
            settings,
            "default_quota_sandbox_seconds_per_period",
            original_sandbox,
        )

    assert allowed is True
    assert reason is None
    assert usage["calls"] == 999
    assert quota["calls_limit"] == 0


@pytest.mark.asyncio
async def test_check_quota_blocks_when_calls_limit_exceeded(patch_mongo):
    settings = usage_metering.get_settings()
    original_calls = settings.default_quota_calls_per_period
    object.__setattr__(settings, "default_quota_calls_per_period", 2)
    try:
        await usage_metering.record_usage("local-dev", calls=2)
        allowed, reason, usage, quota = await usage_metering.check_quota("local-dev")
    finally:
        object.__setattr__(settings, "default_quota_calls_per_period", original_calls)

    assert allowed is False
    assert reason == "calls_limit_exceeded"
    assert usage["calls"] == 2
    assert quota["calls_limit"] == 2


@pytest.mark.asyncio
async def test_check_quota_blocks_when_sandbox_limit_exceeded(patch_mongo):
    settings = usage_metering.get_settings()
    original_calls = settings.default_quota_calls_per_period
    original_sandbox = settings.default_quota_sandbox_seconds_per_period
    object.__setattr__(settings, "default_quota_calls_per_period", 0)
    object.__setattr__(settings, "default_quota_sandbox_seconds_per_period", 1)
    try:
        await usage_metering.record_usage("local-dev", sandbox_ms=1_000)
        allowed, reason, usage, quota = await usage_metering.check_quota("local-dev")
    finally:
        object.__setattr__(settings, "default_quota_calls_per_period", original_calls)
        object.__setattr__(
            settings,
            "default_quota_sandbox_seconds_per_period",
            original_sandbox,
        )

    assert allowed is False
    assert reason == "sandbox_seconds_limit_exceeded"
    assert usage["sandbox_ms"] == 1_000
    assert quota["sandbox_seconds_limit"] == 1


@pytest.mark.asyncio
async def test_summarize_billing_events_rolls_up_by_kind_and_sorts_latest_first(patch_mongo):
    await usage_metering.emit_billing_event(
        "local-dev", kind="calls", amount=2, period="2026-06", metadata={"a": 1}
    )
    await usage_metering.emit_billing_event(
        "local-dev", kind="sandbox_ms", amount=300, period="2026-06", metadata={"b": 2}
    )
    await usage_metering.emit_billing_event("local-dev", kind="calls", amount=1, period="2026-06")

    summary = await usage_metering.summarize_billing_events("local-dev", period="2026-06", limit=2)
    assert summary["tenant_id"] == "local-dev"
    assert summary["period"] == "2026-06"
    assert summary["totals_by_kind"] == {"calls": 3, "sandbox_ms": 300}
    assert summary["total_amount"] == 303
    assert len(summary["events"]) == 2
    assert summary["events"][0]["ts"] >= summary["events"][1]["ts"]
