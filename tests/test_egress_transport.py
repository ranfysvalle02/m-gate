from __future__ import annotations

import ipaddress
from types import SimpleNamespace

import httpx
import pytest

from services import egress_transport
from services.egress_policy import EgressNotAllowed, build_code_egress_rules, build_rules
from services.egress_transport import (
    PinnedEgressTransport,
    make_code_egress_client_factory,
    make_egress_client_factory,
)


def _settings(*, enabled=True, global_allowlist="", default_deny=False):
    return SimpleNamespace(
        egress_allowlist_enabled=enabled,
        egress_global_allowlist=global_allowlist,
        egress_default_deny=default_deny,
    )


def _ips(*addresses: str):
    return [ipaddress.ip_address(addr) for addr in addresses]


@pytest.fixture
def captured(monkeypatch):
    """Stub the underlying transport so no real network I/O happens."""
    seen: dict[str, httpx.Request] = {}

    async def _fake_handle(self, request):
        seen["request"] = request
        return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_handle)
    return seen


@pytest.mark.asyncio
async def test_inactive_rules_passthrough_without_resolving(captured, monkeypatch):
    resolved = {"called": False}

    def _resolve(*_a, **_k):
        resolved["called"] = True
        return _ips("93.184.216.34")

    monkeypatch.setattr(egress_transport, "resolve_host", _resolve)
    rules = build_rules(_settings(), tenant_allowlist=[])
    transport = PinnedEgressTransport(rules=rules)

    request = httpx.Request("POST", "https://api.example.com/mcp")
    response = await transport.handle_async_request(request)

    assert response.status_code == 200
    assert resolved["called"] is False
    # Host untouched when enforcement is inactive.
    assert captured["request"].url.host == "api.example.com"


@pytest.mark.asyncio
async def test_active_rules_pin_validated_ip_and_preserve_host(captured, monkeypatch):
    monkeypatch.setattr(egress_transport, "resolve_host", lambda *_a, **_k: _ips("93.184.216.34"))
    rules = build_rules(_settings(global_allowlist="*.example.com"))
    transport = PinnedEgressTransport(rules=rules)

    request = httpx.Request("POST", "https://api.example.com/mcp")
    await transport.handle_async_request(request)

    pinned = captured["request"]
    # Connection target is rewritten to the validated IP literal...
    assert pinned.url.host == "93.184.216.34"
    # ...while the Host header and TLS SNI keep the original hostname.
    assert pinned.headers["host"] == "api.example.com"
    assert pinned.extensions.get("sni_hostname") == "api.example.com"


@pytest.mark.asyncio
async def test_rebinding_to_private_ip_is_blocked_and_metered(captured, monkeypatch):
    # Host is allowlisted by name but resolves to a private IP (DNS rebinding).
    monkeypatch.setattr(egress_transport, "resolve_host", lambda *_a, **_k: _ips("10.0.0.5"))
    blocks: list[str] = []
    monkeypatch.setattr(
        egress_transport, "observe_egress_block", lambda stage: blocks.append(stage)
    )

    rules = build_rules(_settings(global_allowlist="api.example.com"))
    transport = PinnedEgressTransport(rules=rules)

    request = httpx.Request("POST", "https://api.example.com/mcp")
    with pytest.raises(EgressNotAllowed):
        await transport.handle_async_request(request)

    assert blocks == ["connect"]
    assert "request" not in captured  # never reached the underlying transport


@pytest.mark.asyncio
async def test_unlisted_host_is_blocked(captured, monkeypatch):
    monkeypatch.setattr(egress_transport, "resolve_host", lambda *_a, **_k: _ips("93.184.216.34"))
    monkeypatch.setattr(egress_transport, "observe_egress_block", lambda stage: None)
    rules = build_rules(_settings(global_allowlist="api.allowed.example"))
    transport = PinnedEgressTransport(rules=rules)

    request = httpx.Request("POST", "https://evil.example/mcp")
    with pytest.raises(EgressNotAllowed):
        await transport.handle_async_request(request)


@pytest.mark.asyncio
async def test_factory_builds_client_with_pinned_transport():
    rules = build_rules(_settings(global_allowlist="api.example.com"))
    factory = make_egress_client_factory(rules)
    client = factory(headers={"Authorization": "Bearer x"}, follow_redirects=True)
    try:
        assert isinstance(client, httpx.AsyncClient)
        assert isinstance(client._transport, PinnedEgressTransport)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_code_egress_transport_pins_allowlisted_host(captured, monkeypatch):
    monkeypatch.setattr(egress_transport, "resolve_host", lambda *_a, **_k: _ips("93.184.216.34"))
    rules = build_code_egress_rules(
        _settings(enabled=False, global_allowlist="*.example.com"),
        tenant_allowlist=["api.example.com"],
    )
    transport = PinnedEgressTransport(rules=rules, code_egress=True)

    request = httpx.Request("GET", "https://api.example.com/data")
    await transport.handle_async_request(request)

    pinned = captured["request"]
    assert pinned.url.host == "93.184.216.34"
    assert pinned.headers["host"] == "api.example.com"


@pytest.mark.asyncio
async def test_code_egress_transport_denies_with_no_tenant_grant(captured, monkeypatch):
    # Even with the downstream-proxy egress feature OFF, code egress is fail-closed.
    monkeypatch.setattr(egress_transport, "resolve_host", lambda *_a, **_k: _ips("93.184.216.34"))
    monkeypatch.setattr(egress_transport, "observe_egress_block", lambda stage: None)
    rules = build_code_egress_rules(
        _settings(enabled=False, global_allowlist="*.example.com"), tenant_allowlist=[]
    )
    transport = PinnedEgressTransport(rules=rules, code_egress=True)

    request = httpx.Request("GET", "https://api.example.com/data")
    with pytest.raises(EgressNotAllowed):
        await transport.handle_async_request(request)
    assert "request" not in captured


@pytest.mark.asyncio
async def test_code_egress_factory_builds_fail_closed_transport():
    factory = make_code_egress_client_factory(
        settings=_settings(global_allowlist="api.example.com"), tenant_id="tenant-a"
    )
    client = factory()
    try:
        assert isinstance(client, httpx.AsyncClient)
        transport = client._transport
        assert isinstance(transport, PinnedEgressTransport)
        assert transport._code_egress is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_redirect_revalidates_each_hop_and_blocks_private_target(monkeypatch):
    """A redirect target is re-resolved/re-screened before connect."""
    seen: list[str] = []

    async def _fake_handle(self, request):
        seen.append(str(request.url))
        if request.headers.get("host") == "api.example.com":
            return httpx.Response(
                302,
                headers={"location": "https://redirect.example.com/final"},
                request=request,
            )
        return httpx.Response(200, text="ok", request=request)

    def _resolve(host, *_a, **_k):
        if host == "api.example.com":
            return _ips("93.184.216.34")
        if host == "redirect.example.com":
            # Rebinding-style redirect to private address must be blocked.
            return _ips("10.0.0.9")
        return _ips("93.184.216.34")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _fake_handle)
    monkeypatch.setattr(egress_transport, "resolve_host", _resolve)
    monkeypatch.setattr(egress_transport, "observe_egress_block", lambda stage: None)

    rules = build_rules(_settings(global_allowlist="*.example.com"))
    transport = PinnedEgressTransport(rules=rules)
    client = httpx.AsyncClient(transport=transport, follow_redirects=True)
    try:
        with pytest.raises(EgressNotAllowed):
            await client.get("https://api.example.com/start")
    finally:
        await client.aclose()

    # First hop reached the underlying transport; second hop was blocked before connect.
    assert len(seen) == 1
