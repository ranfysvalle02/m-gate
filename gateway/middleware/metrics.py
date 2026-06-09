from __future__ import annotations

from time import perf_counter

from services.metrics import observe_request


class MetricsMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        started = perf_counter()
        method = scope.get("method", "UNKNOWN")
        path = scope.get("path", "unknown")
        status_code = 500

        async def send_with_metrics(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_with_metrics)
        finally:
            observe_request(
                method=method,
                path=path,
                status=status_code,
                duration_seconds=(perf_counter() - started),
            )
