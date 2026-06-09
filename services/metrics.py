from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    _PROMETHEUS_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency hardening
    Counter = None  # type: ignore[assignment,misc]
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


def scrape_metrics() -> MetricsSnapshot:
    if generate_latest is None:
        return MetricsSnapshot(body=b"prometheus_client not installed\n", content_type="text/plain")
    return MetricsSnapshot(body=generate_latest(), content_type=CONTENT_TYPE_LATEST)
