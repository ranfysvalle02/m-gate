"""Tests for the tracing helpers in both no-op (disabled) and active modes.

When enable_tracing is on we install an in-memory span exporter so we can
assert spans and attributes are actually emitted; when off, the helpers must
be inert and never touch OpenTelemetry.
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from services import tracing


def test_disabled_start_span_yields_none(monkeypatch):
    monkeypatch.setattr(tracing, "get_settings", lambda: Settings(enable_tracing=False))
    with tracing.start_span("noop", {"k": "v"}) as span:
        assert span is None
    # set_span_attribute on None is a harmless no-op.
    tracing.set_span_attribute(None, "k", "v")


def test_disabled_does_not_swallow_exceptions(monkeypatch):
    monkeypatch.setattr(tracing, "get_settings", lambda: Settings(enable_tracing=False))
    with pytest.raises(ValueError):
        with tracing.start_span("noop"):
            raise ValueError("boom")


def test_coerce_types():
    assert tracing._coerce("s") == "s"
    assert tracing._coerce(3) == 3
    assert tracing._coerce(True) is True
    assert tracing._coerce(["a", "b"]) == ["a", "b"]
    # Non-primitive collapses to its string form.
    assert tracing._coerce({"a": 1}) == "{'a': 1}"


@pytest.fixture
def in_memory_tracer(monkeypatch):
    """Wire a real in-memory OTel tracer so spans can be inspected."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # trace.set_tracer_provider only takes effect once per process; override the
    # module's tracer accessor directly so the test is order-independent.
    monkeypatch.setattr(tracing, "_tracer", lambda: provider.get_tracer("test"))
    monkeypatch.setattr(tracing, "get_settings", lambda: Settings(enable_tracing=True))
    monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", True)
    return exporter


def test_enabled_emits_span_with_attributes(in_memory_tracer):
    with tracing.start_span("rpc tools/call", {"mcp.tool": "get_forecast"}) as span:
        tracing.set_span_attribute(span, "mcp.cache", "miss")
    spans = in_memory_tracer.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "rpc tools/call"
    assert spans[0].attributes["mcp.tool"] == "get_forecast"
    assert spans[0].attributes["mcp.cache"] == "miss"


def test_enabled_records_exception_and_sets_error_status(in_memory_tracer):
    from opentelemetry.trace import StatusCode

    with pytest.raises(RuntimeError):
        with tracing.start_span("rpc failing"):
            raise RuntimeError("downstream exploded")
    spans = in_memory_tracer.get_finished_spans()
    assert spans[0].status.status_code == StatusCode.ERROR
    assert any(e.name == "exception" for e in spans[0].events)
