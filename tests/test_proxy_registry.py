from datetime import UTC, datetime, timedelta

import httpx
import pytest

from services.code_tools import encrypt_raw_code
from services.credential_broker import CallerIdentity, MintedCredential
from services.proxy_registry import (
    DownstreamError,
    DownstreamProtocolError,
    DownstreamServer,
    DownstreamTimeout,
    InMemoryFastMCPRegistry,
)
from services.sandbox_executor import ExecResult
from services.sandbox_tool_bridge import ToolCallDenied
from services.server_guard import StdioNotAllowed


@pytest.fixture(autouse=True)
def _restore_settings_singleton():
    """Several tests here flip feature flags (sandbox bridge, code execution) by
    mutating the cached ``Settings`` singleton in place via ``object.__setattr__``.
    Drop the cache afterwards so those mutations never leak into later test files
    (e.g. code_tool_execution_enabled bleeding into the rpc-router suite)."""
    from config.settings import get_settings

    yield
    get_settings.cache_clear()


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


def _credential(token_id: str) -> MintedCredential:
    return MintedCredential(
        headers={"Authorization": f"Bearer token-{token_id}"},
        env={"MCP_DOWNSTREAM_TOKEN": f"token-{token_id}"},
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        token_id=token_id,
    )


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
async def test_mount_or_update_rejects_tenant_origin_stdio():
    registry = InMemoryFastMCPRegistry()
    doc = {
        "_id": "secure-stdio",
        "tenant_id": "tenant-a",
        "origin": "tenant",
        "server": "secure-stdio",
        "transport": "stdio",
        "command": "python",
        "args": ["-m", "servers.weather.server"],
        "enabled": True,
    }

    with pytest.raises(StdioNotAllowed):
        await registry.mount_or_update(doc)


@pytest.mark.asyncio
async def test_mount_or_update_invalidates_broker_cache():
    class _Broker:
        def __init__(self):
            self.calls: list[tuple[str, str | None]] = []

        async def mint(self, **_kwargs):
            return _credential("unused")

        def near_expiry(self, credential, now=None):
            return False

        async def invalidate(self, server_name, *, tenant_id=None):
            self.calls.append((server_name, tenant_id))

    broker = _Broker()
    registry = InMemoryFastMCPRegistry(credential_broker=broker)

    async def _noop_sync(_doc):
        return None

    registry.sync_tool_catalog = _noop_sync  # type: ignore[method-assign]
    await registry.mount_or_update(
        {
            "tenant_id": "local-dev",
            "server": "weather",
            "transport": "streamable_http",
            "endpoint": "https://weather:8101/mcp",
            "enabled": True,
        }
    )
    assert broker.calls == [("weather", "local-dev")]


@pytest.mark.asyncio
async def test_build_client_injects_bearer_headers_and_env():
    registry = InMemoryFastMCPRegistry()
    minted = _credential("abc")
    streamable = registry._build_client(  # noqa: SLF001 - transport unit test
        DownstreamServer(
            tenant_id="t1",
            server="weather",
            transport="streamable_http",
            endpoint="http://weather:8101/mcp",
        ),
        credential=minted,
    )
    sse = registry._build_client(  # noqa: SLF001 - transport unit test
        DownstreamServer(
            tenant_id="t1",
            server="weather",
            transport="sse",
            endpoint="http://weather:8101/sse",
        ),
        credential=minted,
    )
    stdio = registry._build_client(  # noqa: SLF001 - transport unit test
        DownstreamServer(
            tenant_id="t1",
            server="weather",
            transport="stdio",
            command="python",
            args=["-m", "server"],
            env={"EXISTING": "1"},
        ),
        credential=minted,
    )

    assert streamable.transport.headers["Authorization"] == "Bearer token-abc"
    assert sse.transport.headers["Authorization"] == "Bearer token-abc"
    assert stdio.transport.env["EXISTING"] == "1"
    assert stdio.transport.env["MCP_DOWNSTREAM_TOKEN"] == "token-abc"


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
async def test_call_tool_surfaces_broker_mint_failures():
    class _FailingBroker:
        async def mint(self, **_kwargs):
            raise ValueError("signing key unavailable")

        def near_expiry(self, credential, now=None):
            return False

        async def invalidate(self, server_name, *, tenant_id=None):
            return None

    registry = InMemoryFastMCPRegistry(credential_broker=_FailingBroker())
    registry._servers[("local-dev", "weather")] = _weather_server()

    with pytest.raises(DownstreamError, match="Failed to mint downstream credential"):
        await registry.call_tool("weather", "get_current_weather", {})


@pytest.mark.asyncio
async def test_call_via_client_maps_timeout_to_downstream_timeout(monkeypatch):
    registry = InMemoryFastMCPRegistry()
    server = DownstreamServer(
        tenant_id="local-dev",
        server="weather",
        transport="streamable_http",
        endpoint="https://weather:8101/mcp",
    )

    class _TimeoutClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def call_tool(self, *_args, **_kwargs):
            raise TimeoutError("network timeout")

    monkeypatch.setattr(registry, "_build_client", lambda _server, **_kwargs: _TimeoutClient())
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
        endpoint="https://weather:8101/mcp",
    )


@pytest.mark.asyncio
async def test_connect_rejects_jwt_credential_on_insecure_http_endpoint(reset_settings):
    # The default `jwt` scheme attaches a Bearer header; sending it over plaintext
    # http:// must be refused unless DOWNSTREAM_ALLOW_INSECURE_CREDENTIALS=true.
    registry = InMemoryFastMCPRegistry()
    server = DownstreamServer(
        tenant_id="local-dev",
        server="weather",
        transport="streamable_http",
        endpoint="http://weather:8101/mcp",
    )
    with pytest.raises(DownstreamError, match="insecure endpoint"):
        await registry._get_or_connect_client(("local-dev", "weather"), server)  # noqa: SLF001


@pytest.mark.asyncio
async def test_call_via_client_detects_wrapped_timeout_by_type(monkeypatch):
    """A timeout re-wrapped in a generic exception is still classified as a timeout
    via the cause chain — no message-substring sniffing required."""
    registry = InMemoryFastMCPRegistry()
    wrapped = RuntimeError("downstream call failed")
    wrapped.__cause__ = httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(
        registry, "_build_client", lambda _s, **_kwargs: _ScriptedClient(raises=wrapped)
    )
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
        lambda _s, **_kwargs: _ScriptedClient(
            raises=ValueError("config error: timeout must be positive")
        ),
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
    monkeypatch.setattr(
        registry, "_build_client", lambda _s, **_kwargs: _ScriptedClient(returns=result)
    )
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
    monkeypatch.setattr(
        registry, "_build_client", lambda _s, **_kwargs: _ScriptedClient(returns=result)
    )
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
    monkeypatch.setattr(registry, "_build_client", lambda _s, **_kwargs: client)

    for _ in range(3):
        result = await registry.call_tool("weather", "get_current_weather", {})
        assert result == {"ok": True}

    # One base session opened and held; three reentrant calls reused it.
    assert client.opens == 1
    assert client.call_count == 3
    assert ("local-dev", "weather") in registry._clients


@pytest.mark.asyncio
async def test_pool_reconnects_when_credential_near_expiry(monkeypatch):
    """When the stored JIT credential is within the refresh skew, the pool evicts
    and reconnects with a freshly minted token instead of reusing the warm client."""

    class _ExpiringBroker:
        def __init__(self):
            self.mint_calls = 0

        async def mint(self, server_name, *, tenant_id, metadata=None):
            del server_name, tenant_id, metadata
            self.mint_calls += 1
            return _credential(f"tok-{self.mint_calls}")

        def near_expiry(self, credential, now=None):
            return True

        async def invalidate(self, server_name, *, tenant_id=None):
            return None

    broker = _ExpiringBroker()
    registry = InMemoryFastMCPRegistry(credential_broker=broker)
    registry._servers[("local-dev", "weather")] = _weather_server()
    built_clients: list[_PoolableClient] = []

    def _build(_server, **_kwargs):
        client = _PoolableClient(returns=_ResultObj(structured_content={"ok": True}))
        built_clients.append(client)
        return client

    monkeypatch.setattr(registry, "_build_client", _build)

    for _ in range(3):
        await registry.call_tool("weather", "get_current_weather", {})

    # Every call sees a near-expiry credential, so each remints and reconnects.
    assert broker.mint_calls == 3
    assert len(built_clients) == 3


@pytest.mark.asyncio
async def test_pool_evicts_client_on_failure(monkeypatch):
    registry = InMemoryFastMCPRegistry()
    registry._servers[("local-dev", "weather")] = _weather_server()
    client = _PoolableClient(raises=RuntimeError("downstream blew up"))
    monkeypatch.setattr(registry, "_build_client", lambda _s, **_kwargs: client)

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
    monkeypatch.setattr(registry, "_build_client", lambda _s, **_kwargs: client)

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
async def test_sync_tool_catalog_stamps_code_transport_without_raw_code(
    patch_mongo, fake_embeddings
):
    """Code tools are embedded/indexed for discovery with a ``transport=code``
    metadata stamp, and their encrypted source is never copied into the catalog."""
    from database.mongo import get_tenant_database

    registry = InMemoryFastMCPRegistry(embedding_service=fake_embeddings)
    server_doc = {
        "tenant_id": "local-dev",
        "server": "my-funcs",
        "transport": "code",
        "tools": [
            {
                "server": "my-funcs",
                "name": "add",
                "description": "Add two numbers",
                "input_schema": {},
                "raw_code": "enc::ciphertext-should-not-leak",
                "requirements": ["httpx==0.27.0"],
                "metadata": {"action_type": "read"},
            }
        ],
    }
    await registry.sync_tool_catalog(server_doc)

    catalog = get_tenant_database("local-dev")["tool_catalog"].docs
    assert len(catalog) == 1
    entry = catalog[0]
    assert entry["metadata"]["transport"] == "code"
    assert entry["metadata"]["action_type"] == "read"
    assert "raw_code" not in entry
    assert entry["embedding"]  # description was embedded for search


@pytest.mark.asyncio
async def test_sync_tool_catalog_fails_open_when_embeddings_unavailable(patch_mongo):
    """A save should still succeed when embeddings are temporarily unavailable.

    Catalog docs are written with fallback vectors so tools remain discoverable via
    list/catalog endpoints instead of hanging the save path on embedding outages.
    """
    from database.mongo import get_tenant_database
    from fakes import FakeEmbeddingService

    registry = InMemoryFastMCPRegistry(
        embedding_service=FakeEmbeddingService(dimensions=3, fail=True)
    )
    server_doc = {
        "tenant_id": "local-dev",
        "server": "my-funcs",
        "transport": "code",
        "tools": [
            {
                "server": "my-funcs",
                "name": "word_count",
                "description": "Count words",
                "input_schema": {},
                "metadata": {"action_type": "read"},
            }
        ],
    }

    await registry.sync_tool_catalog(server_doc)

    catalog = get_tenant_database("local-dev")["tool_catalog"].docs
    assert len(catalog) == 1
    entry = catalog[0]
    assert entry["server"] == "my-funcs"
    assert entry["name"] == "word_count"
    assert entry["embedding"] == [0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_call_tool_code_transport_respects_execution_flag():
    registry = InMemoryFastMCPRegistry()
    registry._servers[("local-dev", "my-funcs")] = DownstreamServer(
        tenant_id="local-dev",
        server="my-funcs",
        transport="code",
    )
    settings = registry.settings
    original = settings.code_tool_execution_enabled
    object.__setattr__(settings, "code_tool_execution_enabled", False)
    try:
        with pytest.raises(DownstreamProtocolError, match="disabled"):
            await registry.call_tool("my-funcs", "add", {})
    finally:
        object.__setattr__(settings, "code_tool_execution_enabled", original)


@pytest.mark.asyncio
async def test_call_tool_code_transport_executes_via_executor(patch_mongo):
    from database.mongo import get_tenant_database
    from services import usage_metering

    class _Executor:
        def __init__(self):
            self.captured = None

        async def run(self, request):
            self.captured = request
            return ExecResult(payload={"sum": 3}, stdout="", stderr="", elapsed_ms=1)

    executor = _Executor()
    registry = InMemoryFastMCPRegistry(executor=executor)
    registry._servers[("local-dev", "my-funcs")] = DownstreamServer(
        tenant_id="local-dev",
        server="my-funcs",
        transport="code",
    )
    settings = registry.settings
    original = settings.code_tool_execution_enabled
    object.__setattr__(settings, "code_tool_execution_enabled", True)
    try:
        encrypted_code = await encrypt_raw_code(
            "local-dev", "def add(a: int, b: int) -> int:\n    return a + b\n"
        )
        encrypted_secret = await encrypt_raw_code("local-dev", "token-123")
        tenant_db = get_tenant_database("local-dev")
        tenant_db["routing_registry"].docs.append(
            {
                "_id": "my-funcs",
                "tenant_id": "local-dev",
                "server": "my-funcs",
                "transport": "code",
                "tools": [
                    {
                        "name": "add",
                        "raw_code": encrypted_code,
                        "requirements": ["httpx==0.27.0"],
                    }
                ],
            }
        )
        tenant_db["server_secrets"].docs.append(
            {"_id": "my-funcs", "values": {"API_KEY": encrypted_secret}}
        )
        result = await registry.call_tool("my-funcs", "add", {"a": 1, "b": 2})
    finally:
        object.__setattr__(settings, "code_tool_execution_enabled", original)

    assert result == {"sum": 3}
    assert executor.captured is not None
    assert executor.captured.tool == "add"
    assert executor.captured.arguments == {"a": 1, "b": 2}
    assert executor.captured.env["API_KEY"] == "token-123"
    usage = await usage_metering.get_usage("local-dev")
    assert usage["sandbox_ms"] == 1


@pytest.mark.asyncio
async def test_aclose_closes_all_pooled_clients(monkeypatch):
    registry = InMemoryFastMCPRegistry()
    registry._servers[("local-dev", "weather")] = _weather_server()
    client = _PoolableClient(returns=_ResultObj(structured_content={"ok": True}))
    monkeypatch.setattr(registry, "_build_client", lambda _s, **_kwargs: client)

    await registry.call_tool("weather", "get_current_weather", {})
    await registry.aclose()
    assert registry._clients == {}
    assert client.closes >= 1


# ---- cross-tool bridge invoker (context.tools) ------------------------------


def _enable_tool_bridge(registry, **overrides):
    s = registry.settings
    object.__setattr__(s, "sandbox_tool_bridge_enabled", True)
    for key, value in overrides.items():
        object.__setattr__(s, key, value)
    return s


def _caller(scopes, roles=None):
    # Data-plane callers that reach the tool invoker always carry tool:invoke
    # (the coarse RBAC gate enforces it upstream); default to it so these tests
    # exercise the confirmation/callable/scope paths rather than the invoke gate.
    return CallerIdentity(
        user_id="u1",
        scopes=list(scopes),
        roles=list(roles if roles is not None else ["tool:invoke"]),
    )


@pytest.mark.asyncio
async def test_make_tool_invoker_disabled_when_flag_off():
    registry = InMemoryFastMCPRegistry()
    object.__setattr__(registry.settings, "sandbox_tool_bridge_enabled", False)
    assert (
        registry.make_tool_invoker(
            tenant_id="local-dev", caller=_caller(["server:*"]), call_depth=0
        )
        is None
    )


@pytest.mark.asyncio
async def test_make_tool_invoker_disabled_without_caller():
    registry = InMemoryFastMCPRegistry()
    _enable_tool_bridge(registry)
    assert registry.make_tool_invoker(tenant_id="local-dev", caller=None, call_depth=0) is None


@pytest.mark.asyncio
async def test_make_tool_invoker_none_at_max_depth():
    registry = InMemoryFastMCPRegistry()
    _enable_tool_bridge(registry, sandbox_tool_call_max_depth=2)
    caller = _caller(["server:*"])
    assert registry.make_tool_invoker(tenant_id="local-dev", caller=caller, call_depth=2) is None
    assert (
        registry.make_tool_invoker(tenant_id="local-dev", caller=caller, call_depth=1) is not None
    )


@pytest.mark.asyncio
async def test_invoker_denies_when_scope_missing(patch_mongo):
    from database.mongo import get_tenant_database

    registry = InMemoryFastMCPRegistry()
    _enable_tool_bridge(registry)
    db = get_tenant_database("local-dev")
    db["tool_catalog"].docs.append(
        {"server": "a", "name": "t", "scopes": ["analytics:write"], "metadata": {}}
    )
    registry._servers[("local-dev", "a")] = DownstreamServer(
        tenant_id="local-dev", server="a", transport="code"
    )
    inv = registry.make_tool_invoker(
        tenant_id="local-dev", caller=_caller(["server:a"]), call_depth=0
    )
    with pytest.raises(ToolCallDenied) as excinfo:
        await inv("a", "t", {})
    assert excinfo.value.kind == "forbidden"


@pytest.mark.asyncio
async def test_invoker_rejects_confirmation_gated_tool(patch_mongo):
    from database.mongo import get_tenant_database

    registry = InMemoryFastMCPRegistry()
    _enable_tool_bridge(registry)
    db = get_tenant_database("local-dev")
    db["tool_catalog"].docs.append(
        {"server": "a", "name": "t", "scopes": [], "metadata": {"requires_confirmation": True}}
    )
    registry._servers[("local-dev", "a")] = DownstreamServer(
        tenant_id="local-dev", server="a", transport="code"
    )
    inv = registry.make_tool_invoker(
        tenant_id="local-dev", caller=_caller(["server:*"]), call_depth=0
    )
    with pytest.raises(ToolCallDenied) as excinfo:
        await inv("a", "t", {})
    assert excinfo.value.kind == "confirmation_required"


@pytest.mark.asyncio
async def test_invoker_rejects_non_code_target(patch_mongo):
    from database.mongo import get_tenant_database

    registry = InMemoryFastMCPRegistry()
    _enable_tool_bridge(registry)
    db = get_tenant_database("local-dev")
    db["tool_catalog"].docs.append({"server": "a", "name": "t", "scopes": [], "metadata": {}})
    registry._servers[("local-dev", "a")] = DownstreamServer(
        tenant_id="local-dev",
        server="a",
        transport="streamable_http",
        endpoint="http://a:8101/mcp",
    )
    inv = registry.make_tool_invoker(
        tenant_id="local-dev", caller=_caller(["server:*"]), call_depth=0
    )
    with pytest.raises(ToolCallDenied) as excinfo:
        await inv("a", "t", {})
    assert excinfo.value.kind == "tool_not_callable"


@pytest.mark.asyncio
async def test_invoker_executes_sibling_code_tool_at_next_depth(patch_mongo):
    from database.mongo import get_tenant_database

    class _Executor:
        def __init__(self):
            self.captured = None

        async def run(self, request):
            self.captured = request
            return ExecResult(
                payload={"ok": True, "depth": request.call_depth},
                stdout="",
                stderr="",
                elapsed_ms=1,
            )

    executor = _Executor()
    registry = InMemoryFastMCPRegistry(executor=executor)
    _enable_tool_bridge(registry, code_tool_execution_enabled=True)
    db = get_tenant_database("local-dev")
    db["tool_catalog"].docs.append(
        {"server": "a", "name": "t", "scopes": [], "metadata": {"action_type": "read"}}
    )
    encrypted = await encrypt_raw_code("local-dev", "def t():\n    return {}\n")
    db["routing_registry"].docs.append(
        {
            "_id": "a",
            "tenant_id": "local-dev",
            "server": "a",
            "transport": "code",
            "tools": [{"name": "t", "raw_code": encrypted, "requirements": []}],
        }
    )
    registry._servers[("local-dev", "a")] = DownstreamServer(
        tenant_id="local-dev", server="a", transport="code"
    )
    inv = registry.make_tool_invoker(
        tenant_id="local-dev", caller=_caller(["server:*"]), call_depth=0
    )
    result = await inv("a", "t", {"k": 1})
    assert result == {"ok": True, "depth": 1}
    assert executor.captured.call_depth == 1
    assert executor.captured.arguments == {"k": 1}
    # The sibling run also carries an invoker so it can fan out one level deeper.
    assert executor.captured.tool_invoker is not None
