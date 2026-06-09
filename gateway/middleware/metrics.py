from __future__ import annotations

from time import perf_counter

from config.settings import get_settings
from services.metrics import observe_request

# HTTP methods are attacker-controllable tokens (h11 accepts arbitrary ones), so we
# only label known methods and bucket the rest. Same idea for paths below: an
# unbounded `path` label is a Prometheus cardinality / memory-exhaustion vector — a
# scanner hitting /<random> on every request would otherwise create a new time series
# each time. We collapse paths to a small, fixed set of known top-level prefixes.
_KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_BASE_KNOWN_PREFIXES = frozenset({"health", "metrics", "rpc", "admin", "static", "mcp"})


class MetricsMiddleware:
    def __init__(self, app) -> None:
        self.app = app
        # The admin UI path is configurable; fold its first segment into the allow-set.
        ui_segment = get_settings().admin_ui_path.strip("/").split("/", 1)[0]
        self._known_prefixes = set(_BASE_KNOWN_PREFIXES)
        if ui_segment:
            self._known_prefixes.add(ui_segment)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        started = perf_counter()
        method = self._label_method(scope.get("method", "UNKNOWN"))
        path = self._label_path(scope.get("path", ""))
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

    @staticmethod
    def _label_method(method: str) -> str:
        upper = method.upper()
        return upper if upper in _KNOWN_METHODS else "OTHER"

    def _label_path(self, path: str) -> str:
        segment = path.strip("/").split("/", 1)[0]
        if not segment:
            return "/"
        if segment in self._known_prefixes:
            return f"/{segment}"
        return "other"
