from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency hardening
    Counter = None  # type: ignore[assignment,misc]
    Gauge = None  # type: ignore[assignment,misc]
    Histogram = None  # type: ignore[assignment,misc]
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"
    generate_latest = None  # type: ignore[assignment]
    _PROMETHEUS_AVAILABLE = False


@dataclass
class MetricsSnapshot:
    body: bytes
    content_type: str


REQUEST_COUNT: Any = None
REQUEST_LATENCY: Any = None
DOWNSTREAM_ERRORS: Any = None
CACHE_EVENTS: Any = None
GUARDRAIL_EVENTS: Any = None
AUTH_FAILURES: Any = None
USAGE_EVENTS: Any = None
QUOTA_BLOCKS: Any = None
QUOTA_PREFLIGHT_BLOCKS: Any = None
EGRESS_BLOCKS: Any = None
SANDBOX_POOL_EVENTS: Any = None
SANDBOX_POOL_WORKERS: Any = None

if _PROMETHEUS_AVAILABLE:
    REQUEST_COUNT = Counter(
        "gateway_http_requests_total",
        "Total HTTP requests handled by the gateway.",
        ["method", "path", "status"],
    )
    REQUEST_LATENCY = Histogram(
        "gateway_http_request_duration_seconds",
        "HTTP request latency distribution.",
        ["method", "path"],
    )
    DOWNSTREAM_ERRORS = Counter(
        "gateway_downstream_errors_total",
        "Downstream MCP failures surfaced by the gateway.",
        ["type"],
    )
    CACHE_EVENTS = Counter(
        "gateway_cache_events_total",
        "Cache events recorded by the gateway.",
        ["event"],
    )
    GUARDRAIL_EVENTS = Counter(
        "gateway_guardrail_events_total",
        "Guardrail decisions recorded by the gateway.",
        ["layer", "decision"],
    )
    AUTH_FAILURES = Counter(
        "gateway_auth_failures_total",
        "Bearer-token rejections, labelled by cause (client error vs server-side).",
        ["reason"],
    )
    USAGE_EVENTS = Counter(
        "gateway_usage_events_total",
        "Usage metering increments recorded by the gateway.",
        ["kind"],
    )
    QUOTA_BLOCKS = Counter(
        "gateway_quota_blocks_total",
        "Tenant requests blocked due to configured usage quotas.",
    )
    QUOTA_PREFLIGHT_BLOCKS = Counter(
        "gateway_quota_preflight_blocks_total",
        "Code-tool calls rejected up front because their projected sandbox cost "
        "could not fit the tenant's remaining sandbox-seconds quota.",
    )
    EGRESS_BLOCKS = Counter(
        "gateway_egress_blocks_total",
        "Downstream connections blocked by the egress allowlist.",
        ["stage"],
    )
    SANDBOX_POOL_EVENTS = Counter(
        "gateway_sandbox_pool_events_total",
        "Warm sandbox worker pool lifecycle events.",
        ["event"],
    )
    SANDBOX_POOL_WORKERS = Gauge(
        "gateway_sandbox_pool_workers",
        "Resident warm sandbox worker subprocesses currently in the pool.",
    )


def metrics_available() -> bool:
    return generate_latest is not None and REQUEST_COUNT is not None


def observe_request(*, method: str, path: str, status: int, duration_seconds: float) -> None:
    if REQUEST_COUNT is None or REQUEST_LATENCY is None:
        return
    REQUEST_COUNT.labels(method=method, path=path, status=str(status)).inc()
    REQUEST_LATENCY.labels(method=method, path=path).observe(duration_seconds)


def observe_downstream_error(error_type: str) -> None:
    if DOWNSTREAM_ERRORS is None:
        return
    DOWNSTREAM_ERRORS.labels(type=error_type).inc()


def observe_cache_event(event: str) -> None:
    if CACHE_EVENTS is None:
        return
    CACHE_EVENTS.labels(event=event).inc()


def observe_guardrail_event(layer: str, decision: str) -> None:
    if GUARDRAIL_EVENTS is None:
        return
    GUARDRAIL_EVENTS.labels(layer=layer, decision=decision).inc()


def observe_auth_failure(reason: str) -> None:
    if AUTH_FAILURES is None:
        return
    AUTH_FAILURES.labels(reason=reason).inc()


def observe_usage(kind: str, amount: int) -> None:
    if USAGE_EVENTS is None:
        return
    USAGE_EVENTS.labels(kind=kind).inc(max(0, int(amount)))


def observe_quota_block() -> None:
    if QUOTA_BLOCKS is None:
        return
    QUOTA_BLOCKS.inc()


def observe_quota_preflight_block() -> None:
    if QUOTA_PREFLIGHT_BLOCKS is None:
        return
    QUOTA_PREFLIGHT_BLOCKS.inc()


def observe_egress_block(stage: str) -> None:
    """Record an egress-allowlist block. ``stage`` is ``register`` or ``connect``."""
    if EGRESS_BLOCKS is None:
        return
    EGRESS_BLOCKS.labels(stage=stage).inc()


def observe_sandbox_pool_event(event: str) -> None:
    if SANDBOX_POOL_EVENTS is None:
        return
    SANDBOX_POOL_EVENTS.labels(event=event).inc()


def set_sandbox_pool_workers(count: int) -> None:
    if SANDBOX_POOL_WORKERS is None:
        return
    SANDBOX_POOL_WORKERS.set(max(0, int(count)))


def scrape_metrics() -> MetricsSnapshot:
    if generate_latest is None:
        return MetricsSnapshot(body=b"prometheus_client not installed\n", content_type="text/plain")
    return MetricsSnapshot(body=generate_latest(), content_type=CONTENT_TYPE_LATEST)
