from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import UTC, datetime, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse
from pymongo import ReturnDocument

from config.settings import get_settings
from database.mongo import get_control_database, mongo_server_now


class RateLimitMiddleware:
    """Per-(tenant, client-ip) request limiter.

    The default ``sliding_window`` strategy keeps a count per fixed sub-window but
    estimates the rate over a rolling window by weighting the previous window by
    the fraction of it that still overlaps "now". This removes the classic
    fixed-window failure mode where a caller can spend its full quota at the end of
    one window and again at the start of the next — a 2x burst across the boundary.
    Setting ``rate_limit_strategy=fixed_window`` restores the legacy behavior.
    """

    def __init__(self, app):
        self.app = app
        self.settings = get_settings()
        self._logger = logging.getLogger(__name__)
        self._clock_offset_seconds = 0.0
        self._clock_last_sync_monotonic = 0.0
        self._clock_refresh_interval_seconds = 30.0
        self._clock_refresh_lock = asyncio.Lock()

    def _now(self) -> datetime:
        # Indirection point so tests can drive deterministic window math.
        return datetime.now(UTC) + timedelta(seconds=self._clock_offset_seconds)

    async def _maybe_refresh_clock_offset(self) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic - self._clock_last_sync_monotonic < self._clock_refresh_interval_seconds:
            return
        async with self._clock_refresh_lock:
            now_monotonic = time.monotonic()
            if (
                now_monotonic - self._clock_last_sync_monotonic
                < self._clock_refresh_interval_seconds
            ):
                return
            local_before = datetime.now(UTC)
            try:
                remote_now = await mongo_server_now()
            except Exception as exc:
                self._logger.warning(
                    "Rate limiter clock sync failed; using local clock: %s",
                    exc,
                )
                self._clock_last_sync_monotonic = now_monotonic
                return
            local_after = datetime.now(UTC)
            midpoint = local_before + ((local_after - local_before) / 2)
            self._clock_offset_seconds = (remote_now - midpoint).total_seconds()
            self._clock_last_sync_monotonic = now_monotonic

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request = Request(scope, receive=receive)
        if request.url.path.startswith("/health"):
            return await self.app(scope, request.receive, send)

        await self._maybe_refresh_clock_offset()
        now = self._now()
        now_ts = now.timestamp()
        window_seconds = max(1, self.settings.rate_limit_window_seconds)
        limit = self.settings.rate_limit_max_requests
        sliding = self.settings.rate_limit_strategy == "sliding_window"

        current_epoch = int(now_ts // window_seconds) * window_seconds
        window_end_epoch = current_epoch + window_seconds
        # Keep buckets alive an extra window so the sliding calculation can still
        # read the immediately-previous window before TTL cleanup reaps it.
        bucket_lifetime_end = window_end_epoch + (window_seconds if sliding else 0)
        expires_at = datetime.fromtimestamp(bucket_lifetime_end, tz=UTC)

        tenant_id = getattr(request.state, "tenant_id", self.settings.default_tenant_id)
        client_ip = request.client.host if request.client else "unknown"

        collection = get_control_database()["rate_limit_buckets"]
        bucket = await collection.find_one_and_update(
            {"tenant_id": tenant_id, "client_ip": client_ip, "window_epoch": current_epoch},
            {
                "$inc": {"count": 1},
                "$setOnInsert": {
                    "tenant_id": tenant_id,
                    "client_ip": client_ip,
                    "window_epoch": current_epoch,
                    "created_at": now,
                },
                "$set": {"updated_at": now, "expires_at": expires_at},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        current_count = int((bucket or {}).get("count", 1))

        effective = float(current_count)
        if sliding:
            previous = await collection.find_one(
                {
                    "tenant_id": tenant_id,
                    "client_ip": client_ip,
                    "window_epoch": current_epoch - window_seconds,
                }
            )
            previous_count = int((previous or {}).get("count", 0))
            if previous_count:
                elapsed = now_ts - current_epoch
                previous_weight = max(0.0, (window_seconds - elapsed) / window_seconds)
                effective += previous_count * previous_weight

        retry_after = max(0, int(math.ceil(window_end_epoch - now_ts)))
        remaining = max(0, limit - int(math.ceil(effective)))
        headers = {
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(window_end_epoch),
        }

        if effective > limit:
            response = JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded."},
                headers=headers,
            )
            return await response(scope, request.receive, send)

        async def send_with_rate_headers(message):
            if message["type"] == "http.response.start":
                raw_headers = list(message.get("headers", []))
                raw_headers.extend(
                    [
                        (b"x-ratelimit-limit", str(limit).encode("utf-8")),
                        (b"x-ratelimit-remaining", str(remaining).encode("utf-8")),
                        (b"x-ratelimit-reset", str(window_end_epoch).encode("utf-8")),
                    ]
                )
                message["headers"] = raw_headers
            await send(message)

        await self.app(scope, request.receive, send_with_rate_headers)
