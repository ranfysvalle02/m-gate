from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from config.settings import Settings

ServerOrigin = str

_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


class StdioNotAllowed(ValueError):
    """Raised when a tenant-origin server attempts to use stdio."""


class EndpointNotAllowed(ValueError):
    """Raised when a tenant-origin endpoint is not publicly routable."""


def assign_origin(is_platform_admin: bool) -> ServerOrigin:
    return "platform" if is_platform_admin else "tenant"


def _normalized_origin(raw_origin: object | None) -> ServerOrigin:
    if raw_origin in {"platform", "tenant"}:
        return str(raw_origin)
    return "platform"


def _ip_is_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved or ip.is_unspecified:
        return True
    if ip in _CGNAT_NETWORK:
        return True
    return False


def ip_is_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Public SSRF denylist check shared with the egress allowlist enforcer.

    A single definition of "not publicly routable" (loopback, link-local, private,
    reserved, unspecified, CGNAT) so registration-time and connect-time gates can
    never drift apart.
    """
    return _ip_is_disallowed(ip)


async def validate_tenant_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise EndpointNotAllowed("Tenant endpoints must use http or https.")
    if not parsed.hostname:
        raise EndpointNotAllowed("Tenant endpoint hostname is required.")

    host = parsed.hostname
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise EndpointNotAllowed(f"Unable to resolve endpoint host '{host}': {exc}") from exc

    seen = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        address = sockaddr[0]
        if address in seen:
            continue
        seen.add(address)
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise EndpointNotAllowed(
                f"Resolved address '{address}' for host '{host}' is invalid."
            ) from exc
        if _ip_is_disallowed(ip):
            raise EndpointNotAllowed(
                f"Tenant endpoint '{endpoint}' resolves to disallowed address '{address}'."
            )


async def enforce_server_policy(
    doc: dict,
    *,
    is_platform_admin: bool,
    settings: Settings,
) -> dict:
    explicit_origin = doc.get("origin")
    resolved_origin = _normalized_origin(explicit_origin)

    if (
        explicit_origin in {"platform", "tenant"}
        and not is_platform_admin
        and explicit_origin == "platform"
    ):
        raise StdioNotAllowed("Only platform-admin may manage platform-origin servers.")

    if explicit_origin not in {"platform", "tenant"}:
        resolved_origin = assign_origin(is_platform_admin)

    transport = str(doc.get("transport") or "")
    if resolved_origin == "tenant" and (
        transport == "stdio" or doc.get("command") or doc.get("cwd")
    ):
        raise StdioNotAllowed("Tenant servers may not use stdio transport or host commands.")

    if (
        resolved_origin == "tenant"
        and settings.tenant_endpoint_ssrf_guard
        and transport in {"streamable_http", "sse"}
    ):
        endpoint = doc.get("endpoint")
        if isinstance(endpoint, str) and endpoint:
            await validate_tenant_endpoint(endpoint)

    doc["origin"] = resolved_origin
    return doc


def assert_mountable(doc: dict) -> None:
    origin = _normalized_origin(doc.get("origin"))
    transport = str(doc.get("transport") or "")
    if origin == "tenant" and transport == "stdio":
        server = doc.get("server")
        raise StdioNotAllowed(f"Tenant-origin server '{server}' cannot use stdio transport.")
