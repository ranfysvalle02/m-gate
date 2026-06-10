"""Connect-time egress enforcement for downstream HTTP/SSE clients.

This is the authoritative, DNS-rebinding-proof gate. On *every* connect it:

1. resolves the effective policy (global ceiling from settings, intersected with
   the tenant's allowlist read fresh from the control plane),
2. re-resolves the endpoint host and screens each resolved IP against the SSRF
   denylist + the allowlist, and
3. **pins** the connection to a validated IP — rewriting the request URL host to
   that IP while preserving the original ``Host`` header and TLS SNI — so a host
   that resolves to one IP at check time cannot be swapped for a private/internal
   IP between the check and the socket connect (TOCTOU / rebinding).

A blocked connect raises :class:`EgressNotAllowed` (and records a metric) so the
proxy can surface a protocol-safe downstream error.

The policy is resolved lazily at request time (not at client-build time) so the
per-tenant allowlist read only happens on an actual outbound connection.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from config.settings import Settings
from services.egress_policy import EgressNotAllowed, EgressRules, build_rules, resolve_host
from services.metrics import observe_egress_block
from services.tenant_egress import get_tenant_egress_allowlist


class PinnedEgressTransport(httpx.AsyncHTTPTransport):
    """An ``AsyncHTTPTransport`` that validates + pins egress per request.

    Construct with either a static :class:`EgressRules` (``rules=``) or a
    ``settings`` + ``tenant_id`` pair, in which case the effective rules (global
    ceiling intersected with the tenant allowlist) are resolved per request.
    """

    def __init__(
        self,
        *,
        rules: EgressRules | None = None,
        settings: Settings | None = None,
        tenant_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._static_rules = rules
        self._settings = settings
        self._tenant_id = tenant_id

    async def _resolve_rules(self) -> EgressRules:
        if self._static_rules is not None:
            return self._static_rules
        if self._settings is None:
            # No policy source configured => nothing to enforce.
            return build_rules(_NULL_SETTINGS, tenant_allowlist=[])
        tenant_allowlist = await get_tenant_egress_allowlist(
            self._tenant_id or "", settings=self._settings
        )
        return build_rules(
            self._settings,
            tenant_allowlist=tenant_allowlist,
            global_allowlist=self._settings.egress_global_allowlist,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        rules = await self._resolve_rules()
        # Inactive policy => behave exactly like the stock transport.
        if not rules.is_active:
            return await super().handle_async_request(request)

        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)

        try:
            ips = await asyncio.to_thread(resolve_host, host, port)
            rules.evaluate(host, ips)
            pinned = self._select_pinned_ip(rules, host, ips)
        except EgressNotAllowed:
            observe_egress_block("connect")
            raise

        # Preserve the original Host header + TLS server name, then connect to the
        # validated IP literal so resolution cannot be re-raced after the check.
        original_host_header = request.headers.get("host") or self._default_host_header(request)
        request.headers["host"] = original_host_header
        request.extensions = {**request.extensions, "sni_hostname": host}
        request.url = request.url.copy_with(host=str(pinned))
        return await super().handle_async_request(request)

    @staticmethod
    def _default_host_header(request: httpx.Request) -> str:
        host = request.url.host
        port = request.url.port
        default_port = 443 if request.url.scheme == "https" else 80
        if port is None or port == default_port:
            return host
        return f"{host}:{port}"

    @staticmethod
    def _select_pinned_ip(rules: EgressRules, host: str, ips: list[Any]) -> Any:
        # ``evaluate`` already proved at least one address is permitted; pick the
        # first address that individually satisfies the policy so the pinned IP is
        # itself validated (not merely a sibling of a validated one).
        for ip in ips:
            try:
                rules.evaluate(host, [ip])
                return ip
            except EgressNotAllowed:
                continue
        raise EgressNotAllowed(f"No permitted address found for host '{host}'.")


def make_egress_client_factory(
    rules: EgressRules | None = None,
    *,
    settings: Settings | None = None,
    tenant_id: str | None = None,
):
    """Build an ``McpHttpClientFactory``-compatible client factory.

    Pass static ``rules`` (used by tests) or a ``settings`` + ``tenant_id`` pair
    (used by the proxy) to resolve the effective policy per request. fastmcp
    invokes the factory with ``headers``, ``auth``, ``follow_redirects`` and
    (optionally) ``timeout`` keyword arguments; ``**kwargs`` keeps it
    forward-compatible.
    """

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        follow_redirects: bool = True,
        **_kwargs: Any,
    ) -> httpx.AsyncClient:
        transport = PinnedEgressTransport(rules=rules, settings=settings, tenant_id=tenant_id)
        client_kwargs: dict[str, Any] = {
            "transport": transport,
            "follow_redirects": follow_redirects,
            "timeout": timeout if timeout is not None else httpx.Timeout(30.0, read=300.0),
        }
        if headers is not None:
            client_kwargs["headers"] = headers
        if auth is not None:
            client_kwargs["auth"] = auth
        return httpx.AsyncClient(**client_kwargs)

    return factory


# A settings stand-in for the "no policy source" path: enforcement fully off.
class _NullSettings:
    egress_allowlist_enabled = False
    egress_default_deny = False
    egress_global_allowlist = ""
    egress_allowlist_cache_ttl_seconds = 0


_NULL_SETTINGS: Any = _NullSettings()
