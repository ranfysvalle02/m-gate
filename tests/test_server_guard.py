from __future__ import annotations

import ipaddress

import pytest

from config.settings import get_settings
from services.server_guard import (
    EndpointNotAllowed,
    StdioNotAllowed,
    enforce_server_policy,
    ip_is_disallowed,
)


def _server_doc(**overrides):
    base = {
        "server": "demo",
        "transport": "streamable_http",
        "endpoint": "https://example.com/mcp",
        "enabled": True,
    }
    base.update(overrides)
    return base


def _fake_getaddrinfo(*_args, **_kwargs):
    raise RuntimeError("should not be called")


def _addrinfo_for(ip: str):
    return [
        (
            2,  # AF_INET
            1,  # SOCK_STREAM
            6,  # TCP
            "",
            (ip, 443),
        )
    ]


@pytest.mark.parametrize(
    "ip,disallowed",
    [
        ("127.0.0.1", True),
        ("10.0.0.5", True),
        ("169.254.169.254", True),
        ("100.64.0.1", True),
        ("93.184.216.34", False),
        ("8.8.8.8", False),
    ],
)
def test_ip_is_disallowed_shared_denylist(ip: str, disallowed: bool):
    assert ip_is_disallowed(ipaddress.ip_address(ip)) is disallowed


@pytest.mark.asyncio
async def test_enforce_server_policy_blocks_tenant_stdio():
    settings = get_settings()
    doc = _server_doc(transport="stdio", command="python", endpoint=None)

    with pytest.raises(StdioNotAllowed):
        await enforce_server_policy(doc, is_platform_admin=False, settings=settings)


@pytest.mark.asyncio
async def test_enforce_server_policy_allows_platform_stdio():
    settings = get_settings()
    doc = _server_doc(transport="stdio", command="python", endpoint=None)

    result = await enforce_server_policy(doc, is_platform_admin=True, settings=settings)
    assert result["origin"] == "platform"
    assert result["transport"] == "stdio"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_ip", ["127.0.0.1", "169.254.169.254", "10.0.0.5"])
async def test_enforce_server_policy_blocks_tenant_private_endpoints(monkeypatch, blocked_ip: str):
    import services.server_guard as server_guard

    settings = get_settings()
    monkeypatch.setattr(
        server_guard.socket, "getaddrinfo", lambda *_a, **_k: _addrinfo_for(blocked_ip)
    )
    doc = _server_doc(endpoint="https://tenant-endpoint.example/mcp")

    with pytest.raises(EndpointNotAllowed):
        await enforce_server_policy(doc, is_platform_admin=False, settings=settings)


@pytest.mark.asyncio
async def test_enforce_server_policy_allows_tenant_public_endpoint(monkeypatch):
    import services.server_guard as server_guard

    settings = get_settings()
    monkeypatch.setattr(
        server_guard.socket,
        "getaddrinfo",
        lambda *_a, **_k: _addrinfo_for("93.184.216.34"),
    )
    doc = _server_doc(endpoint="https://tenant-endpoint.example/mcp")

    result = await enforce_server_policy(doc, is_platform_admin=False, settings=settings)
    assert result["origin"] == "tenant"


@pytest.mark.asyncio
async def test_enforce_server_policy_ssrf_guard_can_be_disabled(monkeypatch):
    import services.server_guard as server_guard

    settings = get_settings()
    monkeypatch.setattr(settings, "tenant_endpoint_ssrf_guard", False, raising=False)
    monkeypatch.setattr(server_guard.socket, "getaddrinfo", _fake_getaddrinfo)
    doc = _server_doc(endpoint="https://tenant-endpoint.example/mcp")

    result = await enforce_server_policy(doc, is_platform_admin=False, settings=settings)
    assert result["origin"] == "tenant"
