"""Tests for the health and metrics routers, the metrics middleware, and the
metrics service helpers.
"""

from __future__ import annotations

import pytest

# --------------------------------------------------------------------------
# Health router
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_live_always_ok():
    from gateway.routers.health import health_live

    assert await health_live() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_legacy_delegates_to_live():
    from gateway.routers.health import health_legacy

    assert await health_legacy() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_ready_degraded_when_indexes_missing(patch_mongo, monkeypatch, reset_settings):
    from fastapi import Response

    import gateway.routers.health as health

    # Mongo ping succeeds (fake), but no search indexes are queryable.
    async def empty_indexes(*args, **kwargs):
        class _Cur:
            async def to_list(self, length=None):
                return []

        return _Cur()

    monkeypatch.setattr(patch_mongo["tool_catalog"], "list_search_indexes", empty_indexes)

    # Make embedding healthcheck succeed so only the index check fails.
    from fakes import FakeEmbeddingService

    monkeypatch.setattr(health, "get_embedding_service", lambda s=None: FakeEmbeddingService())

    response = Response()
    result = await health.health_ready(response)
    assert result["checks"]["mongo"] is True
    assert result["checks"]["indexes"] is False
    assert result["status"] == "degraded"
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_health_ready_probes_embedding_when_auth_enabled(
    patch_mongo, monkeypatch, reset_settings
):
    from fastapi import Response

    import gateway.routers.health as health
    from config.settings import get_settings

    # Auth is on (prod-like): the embedding provider must still be probed, and a
    # dead provider must mark readiness degraded rather than being skipped.
    settings = get_settings()
    object.__setattr__(settings, "auth_mode", "hs256")
    monkeypatch.setattr(health, "get_settings", lambda: settings)

    async def queryable_indexes(*args, **kwargs):
        from database.indexes import TEXT_INDEX_NAME, VECTOR_INDEX_NAME

        class _Cur:
            async def to_list(self, length=None):
                return [
                    {"name": VECTOR_INDEX_NAME, "queryable": True},
                    {"name": TEXT_INDEX_NAME, "queryable": True},
                ]

        return _Cur()

    monkeypatch.setattr(patch_mongo["tool_catalog"], "list_search_indexes", queryable_indexes)

    class _DeadEmbedding:
        async def embed_text(self, text):
            raise RuntimeError("provider down")

    monkeypatch.setattr(health, "get_embedding_service", lambda s=None: _DeadEmbedding())

    response = Response()
    result = await health.health_ready(response)
    assert result["checks"]["embedding"] is False
    assert result["status"] == "degraded"
    assert response.status_code == 503


# --------------------------------------------------------------------------
# Metrics service + router
# --------------------------------------------------------------------------


def test_observe_request_and_scrape_roundtrip():
    from services.metrics import observe_request, scrape_metrics

    observe_request(method="GET", path="/x", status=200, duration_seconds=0.01)
    snapshot = scrape_metrics()
    assert isinstance(snapshot.body, bytes)
    # Our counter name should appear in the exposition format.
    assert b"gateway_http_requests_total" in snapshot.body


def test_observe_downstream_and_cache_events_do_not_raise():
    from services.metrics import observe_cache_event, observe_downstream_error

    observe_downstream_error("timeout")
    observe_cache_event("hit")


@pytest.mark.asyncio
async def test_metrics_router_returns_payload_when_enabled(reset_settings):
    from gateway.routers.metrics import metrics

    response = await metrics()
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_metrics_router_404_when_disabled(monkeypatch, reset_settings):
    import gateway.routers.metrics as metrics_router
    from config.settings import get_settings

    settings = get_settings()
    object.__setattr__(settings, "enable_metrics", False)
    monkeypatch.setattr(metrics_router, "get_settings", lambda: settings)
    response = await metrics_router.metrics()
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Metrics middleware
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_middleware_observes_status():
    from gateway.middleware.metrics import MetricsMiddleware

    recorded = {}

    def fake_observe(*, method, path, status, duration_seconds):
        recorded.update(method=method, path=path, status=status)

    import gateway.middleware.metrics as mm

    mm.observe_request = fake_observe

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    mw = MetricsMiddleware(app)
    scope = {"type": "http", "method": "POST", "path": "/rpc"}

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        sent.append(m)

    await mw(scope, receive, send)
    assert recorded == {"method": "POST", "path": "/rpc", "status": 201}
