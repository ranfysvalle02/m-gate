"""Smoke tests for application + MCP server construction.

These don't start the server; they assert create_app() wires the middleware
stack and routes, and that the MCP server registers its tools. Heavy I/O
(Mongo connect, registry watcher) lives in the lifespan and is not triggered
here.
"""

from __future__ import annotations

import pytest


def test_create_app_builds_with_expected_routes(reset_settings):
    from gateway.app import create_app

    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/rpc" in paths
    assert "/health/live" in paths
    assert "/metrics" in paths
    assert "/ui/" in paths
    assert "/ui/login" in paths
    assert any(getattr(r, "path", "") == "/static" for r in app.routes)
    # MCP transport is mounted under /mcp.
    assert any(getattr(r, "path", "").startswith("/mcp") for r in app.routes)


def test_create_app_registers_middleware_stack(reset_settings):
    from gateway.app import create_app

    app = create_app()
    middleware_classes = {m.cls.__name__ for m in app.user_middleware}
    # RequestContextMiddleware is intentionally not registered (disabled during
    # the SSE deadlock investigation — see things-to-lookout-for.md §1 and the
    # note in gateway/app.create_app), so it is not asserted here.
    for name in [
        "AuthMiddleware",
        "RateLimitMiddleware",
        "RbacMiddleware",
        "GuardrailsMiddleware",
    ]:
        assert name in middleware_classes


def test_json_formatter_emits_valid_json():
    import json
    import logging

    from gateway.app import JsonFormatter

    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"


def test_mcp_server_registers_tools(reset_settings):
    from gateway.mcp_server import get_mcp_server

    server = get_mcp_server()
    assert server is not None
    # Singleton: repeated calls return the same instance.
    assert get_mcp_server() is server


@pytest.mark.asyncio
async def test_mcp_server_lists_expected_tools(reset_settings):
    from gateway.mcp_server import get_mcp_server

    server = get_mcp_server()
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert {"search_tools", "list_catalog_tools", "call_downstream_tool"} <= names


def test_build_auth_verifier_returns_none_for_hs256(reset_settings, monkeypatch):
    from config.settings import get_settings
    from gateway.mcp_server import _build_auth_verifier

    # The FastMCP verifier is only built for jwks; hs256 is verified by the
    # gateway middleware, so no FastMCP verifier is constructed.
    monkeypatch.setenv("AUTH_MODE", "hs256")
    get_settings.cache_clear()
    assert _build_auth_verifier() is None
