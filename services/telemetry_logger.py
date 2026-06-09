from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from database.mongo import get_tenant_database

logger = logging.getLogger(__name__)


class TelemetryLogger:
    def __init__(self) -> None:
        # Hold strong references so fire-and-forget tasks aren't garbage
        # collected mid-flight (an easy-to-miss asyncio footgun).
        self._tasks: set[asyncio.Task[None]] = set()

    async def log(
        self,
        *,
        tenant_id: str,
        user_id: str,
        request_id: str | None = None,
        method: str,
        status: str,
        latency_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await get_tenant_database(tenant_id)["audit_telemetry"].insert_one(
            {
                "timestamp": datetime.now(UTC),
                "tenant_id": tenant_id,
                "user_id": user_id,
                "request_id": request_id,
                "method": method,
                "status": status,
                "latency_ms": latency_ms,
                "metadata": metadata or {},
            }
        )

    async def _safe_log(self, **kwargs: Any) -> None:
        try:
            await self.log(**kwargs)
        except Exception:  # never let telemetry failures affect the request path
            logger.exception("Failed to write telemetry event")

    def log_background(self, **kwargs: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._safe_log(**kwargs))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


_telemetry_logger: TelemetryLogger | None = None


def get_telemetry_logger() -> TelemetryLogger:
    global _telemetry_logger
    if _telemetry_logger is None:
        _telemetry_logger = TelemetryLogger()
    return _telemetry_logger
