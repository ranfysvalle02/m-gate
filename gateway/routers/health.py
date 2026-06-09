from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status

from config.settings import get_settings
from database.indexes import TEXT_INDEX_NAME, VECTOR_INDEX_NAME
from database.encryption import qe_status
from database.mongo import get_client, get_tenant_database
from services.embeddings import get_embedding_service

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health/live")
async def health_live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(response: Response) -> dict:
    checks: dict[str, bool] = {
        "mongo": False,
        "indexes": False,
        "embedding": False,
        "encryption": True,
    }
    errors: dict[str, str] = {}
    qe: dict | None = None
    settings = get_settings()

    try:
        await get_client().admin.command("ping")
        checks["mongo"] = True
    except Exception as exc:
        checks["mongo"] = False
        errors["mongo"] = exc.__class__.__name__
        logger.warning("Readiness mongo ping failed: %s", exc)

    if checks["mongo"]:
        try:
            index_cursor = await get_tenant_database(settings.default_tenant_id)[
                "tool_catalog"
            ].list_search_indexes()
            indexes = await index_cursor.to_list(length=20)
            names = {idx.get("name"): idx.get("queryable", False) for idx in indexes}
            checks["indexes"] = bool(names.get(VECTOR_INDEX_NAME) and names.get(TEXT_INDEX_NAME))
        except Exception as exc:
            checks["indexes"] = False
            errors["indexes"] = exc.__class__.__name__
            logger.warning("Readiness index check failed: %s", exc)

    # The embedding provider is a hard dependency of routing regardless of how the
    # gateway authenticates callers; a dead provider silently degrades hybrid
    # search to lexical-only. Probe it in every auth mode so prod isn't blind.
    try:
        await get_embedding_service(settings).embed_text("healthcheck")
        checks["embedding"] = True
    except Exception as exc:
        checks["embedding"] = False
        errors["embedding"] = exc.__class__.__name__
        logger.warning("Readiness embedding check failed: %s", exc)

    if settings.qe_enabled:
        try:
            qe = await qe_status(settings)
            checks["encryption"] = bool(qe.get("ok"))
            if not checks["encryption"]:
                errors["encryption"] = str(qe.get("error") or "not_ready")
        except Exception as exc:
            checks["encryption"] = False
            errors["encryption"] = exc.__class__.__name__
            logger.warning("Readiness encryption check failed: %s", exc)

    healthy = all(checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if healthy else "degraded",
        "checks": checks,
        "errors": errors,
        "qe": qe,
    }


@router.get("/health")
async def health() -> dict:
    return await health_live()
