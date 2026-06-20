"""Host-side dispatcher for the sandbox ``context.http`` outbound HTTP bridge.

The wasm guest has no network. A code tool that calls ``context.http.get(...)``
has its request relayed here (over the same ``/job/rpc`` file channel the DB and
cross-tool bridges use), where the host makes the actual call through the SAME
egress stack as the downstream proxy:

* :class:`~services.egress_transport.PinnedEgressTransport` in ``code_egress``
  mode -- always-active, deny-by-default SSRF screening + the global ceiling
  intersected with the tenant egress allowlist + DNS-rebinding-proof IP pinning,
  re-validated on every redirect hop.

This bridge owns only the transport-level concerns the egress stack does not:

* method policy (``https`` only; write verbs require the tool's ``action_type``
  to be ``write``/``destructive`` -- read tools are GET/HEAD only),
* per-invocation call budget, per-call timeout, request/response size ceilings,
* server-side secret injection (``auth="ENV_KEY"`` resolves from the server's
  encrypted env on the host, so the value never has to live in the function's
  URL/body/logs),
* a per-``(tenant, host)`` circuit breaker + per-tenant/global concurrency caps
  so one tenant's runaway loop cannot saturate the shared egress, and
* per-call metering (``emit_billing_event``).

All trust decisions about *which host* are delegated to the egress rules; this
module never widens them.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from config.settings import Settings, get_settings
from services.egress_policy import EgressNotAllowed
from services.egress_transport import make_code_egress_client_factory
from services.usage_metering import emit_billing_event

# Read tools may only read (GET/HEAD); write/destructive tools may mutate.
_READ_METHODS = frozenset({"GET", "HEAD"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ALL_METHODS = _READ_METHODS | _WRITE_METHODS
_METHODS_BY_ACTION = {
    "read": _READ_METHODS,
    "write": _ALL_METHODS,
    "destructive": _ALL_METHODS,
}

# Response headers that must never be relayed back into the guest: cookies (auth
# material) and hop-by-hop headers (meaningless across the bridge).
_STRIPPED_RESPONSE_HEADERS = frozenset(
    {
        "set-cookie",
        "set-cookie2",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class HttpEgressDenied(Exception):
    """An outbound HTTP request was rejected by policy.

    Carries a stable ``kind`` so the guest sees a typed, actionable failure
    rather than an opaque string.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class _CircuitBreaker:
    """A minimal consecutive-failure breaker (mirrors the embedding breaker).

    Keyed per ``(tenant, host)`` by the registry below so one failing upstream
    cannot trip egress for unrelated hosts. Async-only, single event loop, so no
    lock is needed.
    """

    def __init__(self, *, failures: int, reset_seconds: float) -> None:
        self._max_failures = max(1, int(failures))
        self._reset_seconds = max(0.0, float(reset_seconds))
        self._open_until = 0.0
        self._consecutive_failures = 0

    def raise_if_open(self, host: str) -> None:
        if self._reset_seconds <= 0:
            return
        if time.monotonic() < self._open_until:
            raise HttpEgressDenied(
                "http_circuit_open",
                f"Outbound HTTP to '{host}' is temporarily disabled "
                "(too many recent failures); retry later.",
            )

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._open_until = 0.0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_failures:
            self._open_until = time.monotonic() + self._reset_seconds


class _EgressConcurrency:
    """Process-global outbound-HTTP concurrency manager (mirrors the executor).

    Stacks a global semaphore (outer) and a lazily-created per-tenant semaphore
    (inner), plus a per-``(tenant, host)`` circuit breaker registry. Lives at
    module scope so the limits are shared across every code-tool invocation in
    the process, not reset per call.
    """

    def __init__(self) -> None:
        self._global: asyncio.Semaphore | None = None
        self._global_limit = -1
        self._tenant: dict[str, asyncio.Semaphore] = {}
        self._tenant_limit = -1
        self._breakers: dict[tuple[str, str], _CircuitBreaker] = {}

    def _global_semaphore(self, settings: Settings) -> asyncio.Semaphore | None:
        limit = max(0, int(settings.sandbox_http_max_global_concurrency))
        if limit != self._global_limit:
            self._global_limit = limit
            self._global = asyncio.Semaphore(limit) if limit > 0 else None
        return self._global

    def _tenant_semaphore(self, settings: Settings, tenant_id: str) -> asyncio.Semaphore:
        limit = max(1, int(settings.sandbox_http_max_concurrency_per_tenant))
        if limit != self._tenant_limit:
            # A changed limit (tests / hot-reload) rebuilds the registry rather
            # than leaving stale semaphores with the old bound.
            self._tenant_limit = limit
            self._tenant = {}
        semaphore = self._tenant.get(tenant_id)
        if semaphore is None:
            semaphore = asyncio.Semaphore(limit)
            self._tenant[tenant_id] = semaphore
        return semaphore

    def breaker(self, settings: Settings, tenant_id: str, host: str) -> _CircuitBreaker:
        key = (tenant_id, host)
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = _CircuitBreaker(
                failures=settings.sandbox_http_breaker_failures,
                reset_seconds=settings.sandbox_http_breaker_reset_seconds,
            )
            self._breakers[key] = breaker
        return breaker

    def reset(self) -> None:
        self._global = None
        self._global_limit = -1
        self._tenant = {}
        self._tenant_limit = -1
        self._breakers = {}


_CONCURRENCY = _EgressConcurrency()


def reset_http_egress_state() -> None:
    """Clear process-global breaker/concurrency state (used by tests)."""
    _CONCURRENCY.reset()


class SandboxHttpBridge:
    """Tenant-scoped, host-side outbound-HTTP RPC dispatcher for code tools."""

    def __init__(
        self,
        *,
        tenant_id: str,
        action_type: str,
        env: dict[str, str] | None = None,
        settings: Settings | None = None,
        server: str | None = None,
        tool: str | None = None,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.tenant_id = tenant_id
        self.action_type = action_type if action_type in _METHODS_BY_ACTION else "read"
        self.env = dict(env or {})
        self.server = server
        self.tool = tool
        self.calls = 0
        self.max_calls = max(0, int(self.settings.sandbox_http_max_calls_per_invocation))
        self.timeout_ms = max(1, int(self.settings.sandbox_http_timeout_ms))
        self.max_response_bytes = max(1024, int(self.settings.sandbox_http_max_response_bytes))
        self.max_request_bytes = max(0, int(self.settings.sandbox_http_max_request_bytes))
        # client_factory is an injection seam for tests; production always builds
        # the always-fail-closed code-egress client.
        self._client_factory = client_factory or make_code_egress_client_factory(
            settings=self.settings, tenant_id=tenant_id
        )

    @property
    def allowed_methods(self) -> frozenset[str]:
        return _METHODS_BY_ACTION[self.action_type]

    async def handle(self, rpc: dict[str, Any]) -> dict[str, Any]:
        rpc_id = rpc.get("id")
        try:
            payload = await self._dispatch(rpc)
            response: dict[str, Any] = {"ok": True, "result": payload}
        except HttpEgressDenied as exc:
            response = {"ok": False, "error": {"type": exc.kind, "message": str(exc)}}
        except EgressNotAllowed as exc:
            response = {"ok": False, "error": {"type": "egress_blocked", "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001 - always return a structured failure
            response = {"ok": False, "error": {"type": "http_rpc_error", "message": str(exc)}}
        return {"type": "http_rpc_result", "id": rpc_id, **response}

    async def _dispatch(self, rpc: dict[str, Any]) -> dict[str, Any]:
        if self.max_calls > 0 and self.calls >= self.max_calls:
            raise HttpEgressDenied(
                "http_call_limit",
                "Outbound HTTP call budget exceeded for this invocation.",
            )
        self.calls += 1

        method = str(rpc.get("method") or "GET").strip().upper()
        url = str(rpc.get("url") or "").strip()
        host = self._validate_request(method, url)
        headers = self._build_headers(rpc)
        params = rpc.get("params") if isinstance(rpc.get("params"), dict) else None
        content, json_body = self._build_body(method, rpc)

        breaker = _CONCURRENCY.breaker(self.settings, self.tenant_id, host)
        breaker.raise_if_open(host)

        global_semaphore = _CONCURRENCY._global_semaphore(self.settings)
        tenant_semaphore = _CONCURRENCY._tenant_semaphore(self.settings, self.tenant_id)

        try:
            response = await self._send(
                method=method,
                url=url,
                headers=headers,
                params=params,
                content=content,
                json_body=json_body,
                global_semaphore=global_semaphore,
                tenant_semaphore=tenant_semaphore,
            )
        except httpx.TimeoutException as exc:
            # A timeout is a genuine upstream-health signal -> trips the breaker.
            breaker.record_failure()
            raise HttpEgressDenied(
                "http_timeout",
                f"Outbound HTTP request timed out after {self.timeout_ms}ms.",
            ) from exc
        except httpx.HTTPError as exc:
            breaker.record_failure()
            raise HttpEgressDenied(
                "http_request_failed", f"Outbound HTTP request failed: {exc}"
            ) from exc
        # A policy denial (EgressNotAllowed) propagates uncaught to handle(); it is
        # NOT an upstream-health failure, so it never trips the breaker.
        breaker.record_success()
        body_len = len(response.content or b"")
        result = self._encode_response(response)
        await self._meter(host, response.status_code, body_len)
        return result

    def _validate_request(self, method: str, url: str) -> str:
        if method not in _ALL_METHODS:
            raise HttpEgressDenied("http_method_invalid", f"Unsupported HTTP method '{method}'.")
        if method not in self.allowed_methods:
            raise HttpEgressDenied(
                "http_method_forbidden",
                f"Method '{method}' is not allowed for an action_type='{self.action_type}' "
                "tool. Author the tool as write/destructive to use write methods.",
            )
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise HttpEgressDenied(
                "http_scheme_forbidden",
                "context.http requires https:// URLs.",
            )
        if not parsed.hostname:
            raise HttpEgressDenied("http_url_invalid", "Request URL is missing a host.")
        return parsed.hostname.lower()

    def _build_headers(self, rpc: dict[str, Any]) -> dict[str, str]:
        raw = rpc.get("headers")
        headers: dict[str, str] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if not isinstance(key, str):
                    continue
                if key.lower() == "host":
                    # Host is derived from the (pinned) URL; never caller-set.
                    continue
                headers[key] = str(value)
        self._inject_auth(rpc.get("auth"), headers)
        return headers

    def _inject_auth(self, auth: Any, headers: dict[str, str]) -> None:
        """Resolve ``auth="ENV_KEY"`` from the server env, host-side.

        The guest passes a *key name*; the host looks it up in the decrypted
        per-server env and attaches it as a bearer token (or a custom header when
        the caller passes ``{"key": "ENV_KEY", "header": "X-Api-Key", ...}``).
        The secret value is never echoed back to the guest.
        """
        if auth is None:
            return
        if isinstance(auth, str):
            key, header, scheme = auth, "Authorization", "Bearer"
        elif isinstance(auth, dict):
            key = str(auth.get("key") or "")
            header = str(auth.get("header") or "Authorization")
            raw_scheme = auth.get("scheme")
            scheme = "Bearer" if raw_scheme is None else str(raw_scheme)
        else:
            raise HttpEgressDenied(
                "http_auth_invalid",
                "auth must be an env key name or {'key','header','scheme'}.",
            )
        if not key:
            raise HttpEgressDenied("http_auth_invalid", "auth env key name is required.")
        if key not in self.env:
            raise HttpEgressDenied(
                "http_auth_unknown_key",
                f"auth references env key '{key}', which is not set on this server's Secrets.",
            )
        value = self.env[key]
        headers[header] = f"{scheme} {value}".strip() if scheme else value

    def _build_body(self, method: str, rpc: dict[str, Any]) -> tuple[bytes | None, Any]:
        json_body = rpc.get("json")
        data = rpc.get("data")
        if json_body is None and data is None:
            return None, None
        if method in _READ_METHODS:
            raise HttpEgressDenied(
                "http_body_forbidden",
                f"A {method} request cannot carry a body.",
            )
        if json_body is not None:
            import json as _json

            encoded = _json.dumps(json_body).encode("utf-8")
            self._check_request_size(encoded)
            return None, json_body
        if isinstance(data, str):
            encoded = data.encode("utf-8")
        elif isinstance(data, bytes | bytearray):
            encoded = bytes(data)
        else:
            raise HttpEgressDenied(
                "http_body_invalid",
                "data must be a string; use json= for structured bodies.",
            )
        self._check_request_size(encoded)
        return encoded, None

    def _check_request_size(self, encoded: bytes) -> None:
        if self.max_request_bytes and len(encoded) > self.max_request_bytes:
            raise HttpEgressDenied(
                "http_request_too_large",
                f"Request body exceeds the {self.max_request_bytes}-byte limit.",
            )

    async def _send(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        content: bytes | None,
        json_body: Any,
        global_semaphore: asyncio.Semaphore | None,
        tenant_semaphore: asyncio.Semaphore,
    ) -> httpx.Response:
        timeout = httpx.Timeout(self.timeout_ms / 1000)
        factory = self._client_factory

        async def _run() -> httpx.Response:
            async with factory(timeout=timeout) as client:
                request = client.build_request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    content=content,
                    json=json_body,
                )
                # A non-streaming send fully reads + buffers the body, so the
                # response stays usable after the client context closes.
                return await client.send(request)

        if global_semaphore is not None:
            async with global_semaphore, tenant_semaphore:
                return await _run()
        async with tenant_semaphore:
            return await _run()

    def _encode_response(self, response: httpx.Response) -> dict[str, Any]:
        body = response.content or b""
        if len(body) > self.max_response_bytes:
            raise HttpEgressDenied(
                "http_response_too_large",
                f"Response body exceeds the {self.max_response_bytes}-byte limit.",
            )
        headers = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower() not in _STRIPPED_RESPONSE_HEADERS
        }
        result: dict[str, Any] = {
            "status": int(response.status_code),
            "headers": headers,
            "url": str(response.url),
        }
        try:
            result["text"] = body.decode("utf-8")
        except UnicodeDecodeError:
            result["content_b64"] = base64.b64encode(body).decode("ascii")
        return result

    async def _meter(self, host: str, status: Any, byte_count: int) -> None:
        try:
            await emit_billing_event(
                self.tenant_id,
                kind="sandbox_http_egress_request",
                amount=1,
                metadata={
                    "host": host,
                    "status": int(status) if isinstance(status, int) else None,
                    "bytes": int(byte_count),
                    "server": self.server,
                    "tool": self.tool,
                },
            )
        except Exception:  # noqa: BLE001 - metering must never fail the call
            pass
