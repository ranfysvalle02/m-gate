"""Lightweight OpenTelemetry span helpers with a safe no-op fallback.

The gateway should emit meaningful spans around the work that matters —
JSON-RPC method handling, downstream MCP hops, cache lookups — not just the
auto-instrumented HTTP envelope. But OpenTelemetry is an optional dependency
and tracing is off by default, so every helper here degrades to a no-op when
the SDK is missing or `enable_tracing` is false. Calling code stays identical
in both modes.

Usage:

    from services.tracing import start_span

    with start_span("rpc.tools/call", {"mcp.tool": name}) as span:
        ...
        set_span_attribute(span, "mcp.cache", "hit")
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from config.settings import get_settings

try:  # pragma: no cover - import guard exercised indirectly
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    _OTEL_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    trace = None  # type: ignore[assignment]
    Status = None  # type: ignore[assignment,misc]
    StatusCode = None  # type: ignore[assignment,misc]
    _OTEL_AVAILABLE = False


def tracing_enabled() -> bool:
    """True only when OTel is importable AND the operator turned tracing on."""
    return _OTEL_AVAILABLE and get_settings().enable_tracing


def _tracer() -> Any:
    return trace.get_tracer("mdb-mcp-gateway")


@contextmanager
def start_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    """Start a span as a context manager.

    Yields the live span when tracing is enabled, otherwise yields None. The
    span is marked ERROR and the exception recorded if the block raises, then
    re-raised — tracing never swallows application errors.
    """
    if not tracing_enabled():
        yield None
        return

    with _tracer().start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            if value is not None:
                span.set_attribute(key, _coerce(value))
        try:
            yield span
        except Exception as exc:  # pragma: no cover - error path needs OTel installed
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise


def set_span_attribute(span: Any, key: str, value: Any) -> None:
    """Set an attribute on a span if both the span and value are present."""
    if span is not None and value is not None:
        span.set_attribute(key, _coerce(value))


def _coerce(value: Any) -> Any:
    """OTel attributes accept str/bool/int/float (and sequences of those).

    Anything else (dicts, None-bearing lists) is stringified so a stray
    attribute can never blow up a request.
    """
    if isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, list | tuple) and all(
        isinstance(v, str | bool | int | float) for v in value
    ):
        return list(value)
    return str(value)
