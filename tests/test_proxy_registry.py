import httpx
import pytest

from services.proxy_registry import (
    DownstreamError,
    DownstreamProtocolError,
    DownstreamServer,
    DownstreamTimeout,
    InMemoryFastMCPRegistry,
)


class _ScriptedClient:
    """Async client stub whose call_tool raises a scripted exception or returns
    a scripted result object."""

    def __init__(self, *, raises=None, returns=None):
        self._raises = raises
        self._returns = returns

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def call_tool(self, *_args, **_kwargs):
        if self._raises is not None:
            raise self._raises
        return self._returns


class _ResultObj:
    def __init__(self, **attrs):
        for key, value in attrs.items():
            setattr(self, key, value)


@pytest.mark.asyncio
async def test_build_client_supports_all_transports():
    registry = InMemoryFastMCPRegistry()
    streamable = registry._build_client(  # noqa: SLF001 - transport unit test
        DownstreamServer(
            tenant_id="t1",
            server="weather",
            transport="streamable_http",
            endpoint="http://weather:8101/mcp",
        )
    )
    sse = registry._build_client(  # noqa: SLF001 - transport unit test
        DownstreamServer(
            tenant_id="t1",
            server="weather",
            transport="sse",
            endpoint="http://weather:8101/sse",
        )
    )
    stdio = registry._build_client(  # noqa: SLF001 - transport unit test
        DownstreamServer(
            tenant_id="t1",
            server="weather",
            transport="stdio",
            command="python",
            args=["-m", "server"],
        )
    )

    assert streamable.transport.__class__.__name__ == "StreamableHttpTransport"
    assert sse.transport.__class__.__name__ == "SSETransport"
    assert stdio.transport.__class__.__name__ == "StdioTransport"


@pytest.mark.asyncio
async def test_call_tool_retries_before_succeeding():
    registry = InMemoryFastMCPRegistry()
    registry._servers[("local-dev", "weather")] = DownstreamServer(
        tenant_id="local-dev",
        server="weather",
        transport="streamable_http",
        endpoint="http://weather:8101/mcp",
    )

    attempts = {"count": 0}

    async def fake_call_via_client(*, server, tool_name, arguments, timeout_seconds):
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise DownstreamError("transient")
        return {
            "ok": True,
            "server": server.server,
            "tool_name": tool_name,
            "arguments": arguments,
            "timeout_seconds": timeout_seconds,
        }

    registry._call_via_client = fake_call_via_client  # type: ignore[method-assign]

    result = await registry.call_tool("weather", "get_current_weather", {"city": "NYC"})
    assert result["ok"] is True
    assert attempts["count"] == 2


@pytest.mark.asyncio
async def test_call_tool_surfaces_timeout_after_retries():
    registry = InMemoryFastMCPRegistry()
    registry._servers[("local-dev", "weather")] = DownstreamServer(
        tenant_id="local-dev",
        server="weather",
        transport="streamable_http",
        endpoint="http://weather:8101/mcp",
    )

    attempts = {"count": 0}

    async def always_timeout(*, server, tool_name, arguments, timeout_seconds):
        attempts["count"] += 1
        raise DownstreamTimeout("timed out")

    registry._call_via_client = always_timeout  # type: ignore[method-assign]

    with pytest.raises(DownstreamTimeout):
        await registry.call_tool("weather", "get_current_weather", {"city": "NYC"})
    assert attempts["count"] == 3


@pytest.mark.asyncio
async def test_call_via_client_maps_timeout_to_downstream_timeout(monkeypatch):
    registry = InMemoryFastMCPRegistry()
    server = DownstreamServer(
        tenant_id="local-dev",
        server="weather",
        transport="streamable_http",
        endpoint="http://weather:8101/mcp",
    )

    class _TimeoutClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def call_tool(self, *_args, **_kwargs):
            raise TimeoutError("network timeout")

    monkeypatch.setattr(registry, "_build_client", lambda _server: _TimeoutClient())
    with pytest.raises(DownstreamTimeout):
        await registry._call_via_client(  # noqa: SLF001 - unit test helper
            server=server,
            tool_name="get_current_weather",
            arguments={"city": "NYC"},
            timeout_seconds=1.0,
        )


def _weather_server() -> DownstreamServer:
    return DownstreamServer(
        tenant_id="local-dev",
        server="weather",
        transport="streamable_http",
        endpoint="http://weather:8101/mcp",
    )


@pytest.mark.asyncio
async def test_call_via_client_detects_wrapped_timeout_by_type(monkeypatch):
    """A timeout re-wrapped in a generic exception is still classified as a timeout
    via the cause chain — no message-substring sniffing required."""
    registry = InMemoryFastMCPRegistry()
    wrapped = RuntimeError("downstream call failed")
    wrapped.__cause__ = httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(registry, "_build_client", lambda _s: _ScriptedClient(raises=wrapped))
    with pytest.raises(DownstreamTimeout):
        await registry._call_via_client(  # noqa: SLF001 - unit test helper
            server=_weather_server(),
            tool_name="get_current_weather",
            arguments={},
            timeout_seconds=1.0,
        )


@pytest.mark.asyncio
async def test_call_via_client_non_timeout_with_timeout_word_is_not_a_timeout(monkeypatch):
    """A non-timeout error whose message merely contains the word "timeout" must
    surface as a generic DownstreamError, proving we no longer match on substrings."""
    registry = InMemoryFastMCPRegistry()
    monkeypatch.setattr(
        registry,
        "_build_client",
        lambda _s: _ScriptedClient(raises=ValueError("config error: timeout must be positive")),
    )
    with pytest.raises(DownstreamError) as excinfo:
        await registry._call_via_client(  # noqa: SLF001 - unit test helper
            server=_weather_server(),
            tool_name="get_current_weather",
            arguments={},
            timeout_seconds=1.0,
        )
    assert not isinstance(excinfo.value, DownstreamTimeout)


@pytest.mark.asyncio
async def test_call_via_client_validates_serializable_result(monkeypatch):
    registry = InMemoryFastMCPRegistry()
    result = _ResultObj(structured_content={"temp": 21, "unit": "C"})
    monkeypatch.setattr(registry, "_build_client", lambda _s: _ScriptedClient(returns=result))
    normalized = await registry._call_via_client(  # noqa: SLF001 - unit test helper
        server=_weather_server(),
        tool_name="get_current_weather",
        arguments={},
        timeout_seconds=1.0,
    )
    assert normalized == {"temp": 21, "unit": "C"}


@pytest.mark.asyncio
async def test_call_via_client_rejects_unserializable_result(monkeypatch):
    registry = InMemoryFastMCPRegistry()
    # A set survives _to_jsonable unchanged and is not JSON-serializable.
    result = _ResultObj(structured_content={"tags": {"a", "b"}})
    monkeypatch.setattr(registry, "_build_client", lambda _s: _ScriptedClient(returns=result))
    with pytest.raises(DownstreamProtocolError):
        await registry._call_via_client(  # noqa: SLF001 - unit test helper
            server=_weather_server(),
            tool_name="get_current_weather",
            arguments={},
            timeout_seconds=1.0,
        )


@pytest.mark.asyncio
async def test_call_tool_does_not_retry_protocol_errors():
    registry = InMemoryFastMCPRegistry()
    registry._servers[("local-dev", "weather")] = _weather_server()

    attempts = {"count": 0}

    async def protocol_failure(*, server, tool_name, arguments, timeout_seconds):
        attempts["count"] += 1
        raise DownstreamProtocolError("malformed result")

    registry._call_via_client = protocol_failure  # type: ignore[method-assign]

    with pytest.raises(DownstreamProtocolError):
        await registry.call_tool("weather", "get_current_weather", {})
    assert attempts["count"] == 1


def test_validate_result_rejects_non_object():
    with pytest.raises(DownstreamProtocolError):
        InMemoryFastMCPRegistry._validate_result(["not", "an", "object"])  # noqa: SLF001


class _PoolableClient:
    """Client stub that models FastMCP's reentrant, ref-counted session lifecycle,
    so tests can prove the pool holds ONE warm session and only nested calls reuse
    it. ``opens`` counts genuine session starts (counter 0 -> 1); ``closes`` counts
    genuine teardowns (counter 1 -> 0)."""

    def __init__(self, *, raises=None, returns=None):
        self._raises = raises
        self._returns = returns
        self._nesting = 0
        self.opens = 0
        self.closes = 0
        self.call_count = 0

    async def __aenter__(self):
        if self._nesting == 0:
            self.opens += 1
        self._nesting += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._nesting = max(0, self._nesting - 1)
        if self._nesting == 0:
            self.closes += 1
        return False

    def is_connected(self) -> bool:
        return self._nesting > 0

    async def call_tool(self, *_args, **_kwargs):
        self.call_count += 1
        if self._raises is not None:
            raise self._raises
        return self._returns


@pytest.mark.asyncio
async def test_pool_reuses_one_warm_client_across_calls(monkeypatch):
    registry = InMemoryFastMCPRegistry()
    registry._servers[("local-dev", "weather")] = _weather_server()
    client = _PoolableClient(returns=_ResultObj(structured_content={"ok": True}))
    monkeypatch.setattr(registry, "_build_client", lambda _s: client)

    for _ in range(3):
        result = await registry.call_tool("weather", "get_current_weather", {})
        assert result == {"ok": True}

    # One base session opened and held; three reentrant calls reused it.
    assert client.opens == 1
    assert client.call_count == 3
    assert ("local-dev", "weather") in registry._clients


@pytest.mark.asyncio
async def test_pool_evicts_client_on_failure(monkeypatch):
    registry = InMemoryFastMCPRegistry()
    registry._servers[("local-dev", "weather")] = _weather_server()
    client = _PoolableClient(raises=RuntimeError("downstream blew up"))
    monkeypatch.setattr(registry, "_build_client", lambda _s: client)

    with pytest.raises(DownstreamError):
        # call_tool retries; each attempt should evict and rebuild from the pool.
        await registry.call_tool("weather", "get_current_weather", {})

    # The broken session must not stay pooled.
    assert ("local-dev", "weather") not in registry._clients


@pytest.mark.asyncio
async def test_unmount_evicts_pooled_client(monkeypatch):
    registry = InMemoryFastMCPRegistry()
    registry._servers[("local-dev", "weather")] = _weather_server()
    client = _PoolableClient(returns=_ResultObj(structured_content={"ok": True}))
    monkeypatch.setattr(registry, "_build_client", lambda _s: client)

    async def _noop_delete(*_a, **_k):
        return None

    # Avoid touching Mongo on unmount; we only assert pool teardown here.
    monkeypatch.setattr(registry, "sync_tool_catalog", _noop_delete)
    await registry.call_tool("weather", "get_current_weather", {})
    assert ("local-dev", "weather") in registry._clients

    import services.proxy_registry as pr

    class _FakeCol:
        async def delete_many(self, *_a, **_k):
            return None

    monkeypatch.setattr(pr, "get_tenant_database", lambda _t: {"tool_catalog": _FakeCol()})
    await registry.unmount("weather", tenant_id="local-dev")
    assert ("local-dev", "weather") not in registry._clients
    assert client.closes >= 1


@pytest.mark.asyncio
async def test_aclose_closes_all_pooled_clients(monkeypatch):
    registry = InMemoryFastMCPRegistry()
    registry._servers[("local-dev", "weather")] = _weather_server()
    client = _PoolableClient(returns=_ResultObj(structured_content={"ok": True}))
    monkeypatch.setattr(registry, "_build_client", lambda _s: client)

    await registry.call_tool("weather", "get_current_weather", {})
    await registry.aclose()
    assert registry._clients == {}
    assert client.closes >= 1
