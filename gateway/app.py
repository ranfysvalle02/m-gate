from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastmcp.utilities.lifespan import combine_lifespans
from starlette.middleware.cors import CORSMiddleware

from config.settings import get_settings
from database.mongo import connect_to_mongo, disconnect_from_mongo
from gateway.mcp_server import get_mcp_server
from gateway.middleware.auth import AuthMiddleware
from gateway.middleware.guardrails import GuardrailsMiddleware
from gateway.middleware.metrics import MetricsMiddleware
from gateway.middleware.ratelimit import RateLimitMiddleware
from gateway.middleware.rbac import RbacMiddleware
from gateway.middleware.request_context import RequestContextMiddleware
from gateway.routers.admin import router as admin_router
from gateway.routers.auth import router as auth_router
from gateway.routers.health import router as health_router
from gateway.routers.metrics import router as metrics_router
from gateway.routers.rpc import router as rpc_router
from gateway.routers.ui import router as ui_router
from services.embedding_config import refresh_active_embedding_config
from services.embeddings import embedding_version_for, get_embedding_service
from services.proxy_registry import get_proxy_registry
from services.registry_watcher import start_registry_watcher, stop_registry_watcher
from services.sandbox_executor import prewarm_executor, shutdown_executor
from services.tenant_provisioner import (
    ensure_control_plane_indexes,
    provision_tenant,
    start_tenant_purge_reaper,
    stop_tenant_purge_reaper,
)

logger = logging.getLogger(__name__)

_OPENAPI_DESCRIPTION = (
    "Production-ready MCP gateway surface for health, observability, JSON-RPC routing, "
    "and admin operations.\n\n"
    "- JSON-RPC gateway endpoint: `/rpc`\n"
    "- Mounted FastMCP app: `/mcp` (not represented in OpenAPI)\n"
    "- Health and metrics: `/health`, `/health/live`, `/health/ready`, `/metrics`\n\n"
    "See `docs/API.md` for the full REST + JSON-RPC contract."
)

_OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness and readiness probes."},
    {"name": "metrics", "description": "Prometheus scrape endpoint."},
    {"name": "rpc", "description": "Gateway JSON-RPC methods under `/rpc`."},
    {"name": "auth", "description": "Inbound MCP-client auth: token endpoint and OAuth discovery."},
    {"name": "admin", "description": "Control plane and operational admin APIs."},
    {"name": "ui", "description": "Admin UI and login routes."},
]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    settings = get_settings()
    if not settings.log_json:
        return
    root = logging.getLogger()
    formatter = JsonFormatter()
    for handler in root.handlers:
        handler.setFormatter(formatter)


def configure_tracing(app: FastAPI) -> None:
    settings = get_settings()
    if not settings.enable_tracing:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.warning("Tracing is enabled but OpenTelemetry dependencies are missing.")
        return
    FastAPIInstrumentor.instrument_app(app)


@asynccontextmanager
async def app_lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info("Starting %s in %s mode.", settings.app_name, settings.environment)
    await connect_to_mongo(settings)
    # Load the persisted (admin-managed) embedding config before any provisioning
    # so index dimensions and embedding_version reflect the active provider.
    try:
        active = await refresh_active_embedding_config(settings)
        logger.info(
            "Active embedding provider: %s (model=%s, dimensions=%d)",
            active.provider,
            active.model,
            active.dimensions,
        )
    except Exception as exc:  # never block startup on an embedding provider hiccup
        logger.warning("Could not refresh embedding config at startup: %s", exc)
        logger.info(
            "Active embedding version (fallback): %s",
            embedding_version_for(get_embedding_service(settings)),
        )
    if settings.auto_bootstrap:
        await ensure_control_plane_indexes()
        await provision_tenant(settings.default_tenant_id, wait_for_queryable_indexes=False)
    await start_registry_watcher()
    # Reap soft-deleted tenants whose retention window has elapsed (no-op unless
    # TENANT_PURGE_SWEEP_INTERVAL_SECONDS > 0).
    await start_tenant_purge_reaper()
    # Prewarm the sandbox worker pool so the first code-tool call skips the
    # subprocess-spawn + wasm-compile cold start. Never block startup on it.
    if (
        settings.code_tool_execution_enabled
        and settings.code_executor != "disabled"
        and settings.sandbox_pool_size > 0
    ):
        try:
            await prewarm_executor()
            logger.info("Prewarmed sandbox worker pool (size=%d).", settings.sandbox_pool_size)
        except Exception as exc:  # never fail startup on a sandbox warmup hiccup
            logger.warning("Sandbox pool prewarm failed: %s", exc)
    try:
        yield
    finally:
        await stop_registry_watcher()
        await stop_tenant_purge_reaper()
        await get_proxy_registry().aclose()
        await shutdown_executor()
        await disconnect_from_mongo()
        logger.info("Application shutdown complete.")


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    mcp = get_mcp_server()
    mcp_app = mcp.http_app(path="/")

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=_OPENAPI_DESCRIPTION,
        openapi_tags=_OPENAPI_TAGS,
        lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip() for origin in settings.cors_allow_origins.split(",") if origin.strip()
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)
    if settings.enable_metrics:
        app.add_middleware(MetricsMiddleware)
    app.add_middleware(GuardrailsMiddleware)
    app.add_middleware(RbacMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(AuthMiddleware)
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(auth_router)
    app.include_router(rpc_router)
    app.include_router(admin_router)
    if settings.admin_ui_enabled:
        app.mount("/static", StaticFiles(directory="gateway/static"), name="static")
        app.include_router(ui_router, prefix=settings.admin_ui_path)
    app.mount("/mcp/sse", mcp_app)
    app.mount("/mcp", mcp_app)
    configure_tracing(app)
    return app


app = create_app()
