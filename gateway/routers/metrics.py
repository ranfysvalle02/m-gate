from __future__ import annotations

from fastapi import APIRouter, Response

from config.settings import get_settings
from services.metrics import scrape_metrics

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def metrics() -> Response:
    if not get_settings().enable_metrics:
        return Response(status_code=404, content=b"metrics disabled\n", media_type="text/plain")
    snapshot = scrape_metrics()
    return Response(content=snapshot.body, media_type=snapshot.content_type)
