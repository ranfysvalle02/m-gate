from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport

from config.settings import get_settings
from database.mongo import get_tenant_database
from services.embeddings import EmbeddingService, get_embedding_service
from services.tracing import set_span_attribute, start_span

# Timeout types we recognize without resorting to message-substring sniffing.
# asyncio.TimeoutError aliases builtins.TimeoutError on 3.11+, listed for clarity.
_TIMEOUT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TimeoutError,
    asyncio.TimeoutError,
    httpx.TimeoutException,
)


class DownstreamError(Exception):
    """A downstream MCP server returned an error or unreachable response."""


class DownstreamTimeout(DownstreamError):
    """A downstream MCP call exceeded the gateway's hard deadline."""


class DownstreamProtocolError(DownstreamError):
    """A downstream response could not be normalized into a valid result object."""


@dataclass
class DownstreamServer:
    tenant_id: str
    server: str
    transport: str
    endpoint: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None
    enabled: bool = True
    metadata: dict[str, Any] | None = None


class InMemoryFastMCPRegistry:
    def __init__(self, embedding_service: EmbeddingService | None = None) -> None:
        self.settings = get_settings()
        self.embedding_service = embedding_service or get_embedding_service(self.settings)
        self._servers: dict[tuple[str, str], DownstreamServer] = {}
        self._lock = asyncio.Lock()
        # Warm, long-lived downstream clients keyed by (tenant, server). FastMCP's
        # Client is reentrant: we hold one base session open here so each call_tool
        # reuses it instead of paying a full connect/handshake per request. A
        # per-key lock serializes connect/evict so concurrent callers don't race
        # to open (or tear down) the same session.
        self._clients: dict[tuple[str, str], Client] = {}
        self._client_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def list_servers(self, tenant_id: str | None = None) -> list[str]:
        resolved_tenant = tenant_id or self.settings.default_tenant_id
        return sorted(
            [name for (tenant, name) in self._servers.keys() if tenant == resolved_tenant]
        )

    def get_server(self, server: str, tenant_id: str | None = None) -> DownstreamServer | None:
        resolved_tenant = tenant_id or self.settings.default_tenant_id
        return self._servers.get((resolved_tenant, server))

    async def mount_or_update(self, server_doc: dict[str, Any]) -> None:
        tenant_id = str(server_doc.get("tenant_id") or self.settings.default_tenant_id)
        server_name = server_doc["server"]
        transport = str(server_doc.get("transport") or "streamable_http")
        endpoint = server_doc.get("endpoint")
        command = server_doc.get("command")
        args = [str(arg) for arg in (server_doc.get("args") or [])]
        raw_env = server_doc.get("env") or {}
        env = {str(key): str(value) for key, value in raw_env.items()}
        cwd = server_doc.get("cwd")
        metadata = server_doc.get("metadata", {})

        if transport in {"streamable_http", "sse"} and not endpoint:
            raise ValueError(
                f"Transport '{transport}' requires endpoint for server '{server_name}'."
            )
        if transport == "stdio" and not command:
            raise ValueError(f"Transport 'stdio' requires command for server '{server_name}'.")

        async with self._lock:
            self._servers[(tenant_id, server_name)] = DownstreamServer(
                tenant_id=tenant_id,
                server=server_name,
                transport=transport,
                endpoint=endpoint,
                command=command,
                args=args,
                env=env,
                cwd=cwd,
                enabled=bool(server_doc.get("enabled", True)),
                metadata=metadata,
            )

        # The connection target may have changed; drop any warm client so the next
        # call reconnects against the new transport/endpoint rather than a stale one.
        await self._evict_client((tenant_id, server_name))
        await self.sync_tool_catalog(server_doc)

    async def unmount(self, server_name: str, tenant_id: str | None = None) -> None:
        resolved_tenant = tenant_id or self.settings.default_tenant_id
        async with self._lock:
            self._servers.pop((resolved_tenant, server_name), None)
        await self._evict_client((resolved_tenant, server_name))
        await get_tenant_database(resolved_tenant)["tool_catalog"].delete_many(
            {"server": server_name}
        )

    async def unmount_by_id(self, server_name: str, tenant_id: str | None = None) -> None:
        await self.unmount(server_name, tenant_id=tenant_id)

    async def sync_tool_catalog(self, server_doc: dict[str, Any]) -> None:
        tenant_id = str(server_doc.get("tenant_id") or self.settings.default_tenant_id)
        server_name = server_doc["server"]
        tools = server_doc.get("tools") or await self.discover_tools(
            server_name=server_name,
            tenant_id=tenant_id,
        )

        server_scopes = (server_doc.get("metadata") or {}).get("scopes") or []
        collection = get_tenant_database(tenant_id)["tool_catalog"]
        now = datetime.now(UTC)
        for tool in tools:
            name = tool.get("name")
            if not name:
                continue
            description = tool.get("description", "")
            scopes = tool.get("scopes") or server_scopes
            text_for_embedding = f"{server_name}\n{name}\n{description}".strip()
            schema_hash = self._schema_hash(
                server_name=server_name,
                name=name,
                description=description,
                input_schema=tool.get("input_schema", {}),
            )
            existing = await collection.find_one(
                {"server": server_name, "name": name},
                {"embedding": 1, "schema_hash": 1},
            )
            if (
                existing
                and existing.get("schema_hash") == schema_hash
                and existing.get("embedding")
            ):
                embedding = existing["embedding"]
            else:
                embedding = await self.embedding_service.embed_text(text_for_embedding)
            await collection.update_one(
                {"server": server_name, "name": name},
                {
                    "$set": {
                        "tenant_id": tenant_id,
                        "server": server_name,
                        "name": name,
                        "description": description,
                        "input_schema": tool.get("input_schema", {}),
                        "scopes": scopes,
                        "metadata": tool.get("metadata", {}),
                        "embedding": embedding,
                        "schema_hash": schema_hash,
                        "updated_at": now,
                    }
                },
                upsert=True,
            )

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_tenant = tenant_id or self.settings.default_tenant_id
        server = self.get_server(server_name, tenant_id=resolved_tenant)
        if server is None:
            raise KeyError(f"Server '{server_name}' is not mounted for tenant '{resolved_tenant}'.")
        attempts = 3
        timeout_seconds = self.settings.downstream_timeout_ms / 1000
        with start_span(
            "downstream.jsonrpc",
            {
                "mcp.server": server_name,
                "mcp.tool": tool_name,
                "mcp.tenant_id": resolved_tenant,
                "downstream.transport": server.transport,
                "downstream.endpoint": server.endpoint,
                "downstream.timeout_ms": self.settings.downstream_timeout_ms,
            },
        ) as span:
            for attempt in range(1, attempts + 1):
                set_span_attribute(span, "downstream.attempts", attempt)
                try:
                    result = await self._call_via_client(
                        server=server,
                        tool_name=tool_name,
                        arguments=arguments,
                        timeout_seconds=timeout_seconds,
                    )
                    return result
                except DownstreamProtocolError:
                    # A malformed/unserializable response is deterministic; retrying
                    # cannot help, so fail fast with a protocol-safe error frame.
                    raise
                except DownstreamError:
                    # Retries cover transient blips; the final failure surfaces a
                    # protocol-safe error frame to the caller (Section 4 of the blog).
                    if attempt == attempts:
                        raise
                    await asyncio.sleep(0.25 * attempt)
        return {}

    async def discover_tools(
        self, server_name: str, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        server = self.get_server(server_name, tenant_id=tenant_id)
        if server is None:
            return []
        try:
            client = self._build_client(server)
            async with client:
                tools = await client.list_tools()
            return [self._normalize_tool_schema(tool) for tool in tools]
        except Exception:
            return []

    async def _call_via_client(
        self,
        *,
        server: DownstreamServer,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        key = (server.tenant_id, server.server)

        async def _invoke() -> Any:
            client = await self._get_or_connect_client(key, server)
            # Reentrant ref-count bump on an already-warm session — cheap, and it
            # self-heals if the base session was dropped since we cached it.
            async with client:
                return await client.call_tool(tool_name, arguments, timeout=timeout_seconds)

        try:
            # Wrap the whole connect+call in our own deadline so a timeout is always
            # a typed asyncio.TimeoutError, regardless of how the client library
            # reports (or fails to report) one. This is the authoritative deadline.
            result = await asyncio.wait_for(_invoke(), timeout=timeout_seconds)
            return self._normalize_tool_result(result)
        except DownstreamError:
            # Validation/protocol errors raised during normalization are already
            # the right shape — surface them unchanged.
            raise
        except _TIMEOUT_EXCEPTIONS as exc:
            # A broken/slow session must not stay pooled; drop it so the retry (and
            # subsequent callers) reconnect against a fresh session.
            await self._evict_client(key)
            raise self._timeout_error(server, timeout_seconds, exc) from exc
        except Exception as exc:
            await self._evict_client(key)
            if self._is_timeout_error(exc):
                raise self._timeout_error(server, timeout_seconds, exc) from exc
            target = self._target(server)
            raise DownstreamError(f"Downstream '{target}' request failed: {exc}") from exc

    def _key_lock(self, key: tuple[str, str]) -> asyncio.Lock:
        lock = self._client_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._client_locks[key] = lock
        return lock

    async def _get_or_connect_client(
        self, key: tuple[str, str], server: DownstreamServer
    ) -> Client:
        """Return a warm, connected client for ``key``, opening one if needed.

        The base ``__aenter__`` is held open for the pooled client's lifetime so
        per-call ``async with`` blocks only bump the reentrant ref-counter. If a
        cached client has lost its session (downstream restart, idle reap), it is
        discarded and reconnected so callers never get a dead handle.
        """
        async with self._key_lock(key):
            client = self._clients.get(key)
            if client is not None and client.is_connected():
                return client
            if client is not None:
                # Cached but no longer connected — drop it before reconnecting.
                await self._close_client(client)
                self._clients.pop(key, None)

            client = self._build_client(server)
            await client.__aenter__()
            self._clients[key] = client
            return client

    async def _evict_client(self, key: tuple[str, str]) -> None:
        """Remove and close the pooled client for ``key`` if present."""
        async with self._key_lock(key):
            client = self._clients.pop(key, None)
        if client is not None:
            await self._close_client(client)

    @staticmethod
    async def _close_client(client: Client) -> None:
        try:
            await client.__aexit__(None, None, None)
        except Exception:  # pragma: no cover - best-effort teardown
            # Teardown failures must never mask the originating error or block
            # eviction; the session is being discarded regardless.
            pass

    async def aclose(self) -> None:
        """Close every pooled client. Call on application shutdown."""
        keys = list(self._clients.keys())
        for key in keys:
            await self._evict_client(key)

    def _timeout_error(
        self,
        server: DownstreamServer,
        timeout_seconds: float,
        cause: BaseException,
    ) -> DownstreamTimeout:
        target = self._target(server)
        return DownstreamTimeout(
            f"Downstream '{target}' timed out after {timeout_seconds * 1000:.0f}ms"
        )

    @staticmethod
    def _is_timeout_error(exc: BaseException) -> bool:
        """Walk the exception cause/context chain for a known timeout type.

        Libraries frequently re-wrap a transport timeout in a generic error; we
        inspect the chain by type instead of grepping the message string.
        """
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, _TIMEOUT_EXCEPTIONS):
                return True
            current = current.__cause__ or current.__context__
        return False

    def _build_client(self, server: DownstreamServer) -> Client:
        if server.transport == "streamable_http":
            return Client(StreamableHttpTransport(url=str(server.endpoint)))
        if server.transport == "sse":
            return Client(SSETransport(url=str(server.endpoint)))
        if server.transport == "stdio":
            if not server.command:
                raise ValueError(f"Server '{server.server}' stdio transport missing command.")
            return Client(
                StdioTransport(
                    command=server.command,
                    args=server.args or [],
                    env=server.env,
                    cwd=server.cwd,
                )
            )
        raise ValueError(
            f"Unsupported transport '{server.transport}' for server '{server.server}'."
        )

    @staticmethod
    def _normalize_tool_schema(tool: Any) -> dict[str, Any]:
        if isinstance(tool, dict):
            name = tool.get("name")
            description = tool.get("description", "")
            input_schema = tool.get("input_schema") or tool.get("inputSchema") or {}
            return {
                "name": name,
                "description": description,
                "input_schema": input_schema,
            }
        name = getattr(tool, "name", None)
        description = getattr(tool, "description", "") or ""
        input_schema = (
            getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
        )
        return {
            "name": name,
            "description": description,
            "input_schema": InMemoryFastMCPRegistry._to_jsonable(input_schema),
        }

    @staticmethod
    def _normalize_tool_result(result: Any) -> dict[str, Any]:
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            jsonable = InMemoryFastMCPRegistry._to_jsonable(structured)
            normalized = jsonable if isinstance(jsonable, dict) else {"data": jsonable}
            return InMemoryFastMCPRegistry._validate_result(normalized)

        data = getattr(result, "data", None)
        if data is not None:
            jsonable = InMemoryFastMCPRegistry._to_jsonable(data)
            normalized = jsonable if isinstance(jsonable, dict) else {"data": jsonable}
            return InMemoryFastMCPRegistry._validate_result(normalized)

        content = getattr(result, "content", None)
        if content is not None:
            return InMemoryFastMCPRegistry._validate_result(
                {"content": InMemoryFastMCPRegistry._to_jsonable(content)}
            )
        return {}

    @staticmethod
    def _validate_result(result: Any) -> dict[str, Any]:
        """Enforce the gateway's contract for a normalized downstream result.

        The result must be a JSON object whose values are JSON-serializable —
        otherwise it cannot be cached or returned in a JSON-RPC ``result`` frame.
        Catching the violation here turns a deep, late serialization crash into a
        protocol-safe downstream error.
        """
        if not isinstance(result, dict):
            raise DownstreamProtocolError(
                f"Downstream result must be a JSON object, got {type(result).__name__}."
            )
        try:
            json.dumps(result)
        except (TypeError, ValueError) as exc:
            raise DownstreamProtocolError(
                f"Downstream result is not JSON-serializable: {exc}"
            ) from exc
        return result

    @staticmethod
    def _to_jsonable(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", by_alias=True)
        if isinstance(value, list):
            return [InMemoryFastMCPRegistry._to_jsonable(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): InMemoryFastMCPRegistry._to_jsonable(item) for key, item in value.items()
            }
        return value

    @staticmethod
    def _target(server: DownstreamServer) -> str:
        if server.transport in {"streamable_http", "sse"}:
            return str(server.endpoint)
        return f"{server.command} {' '.join(server.args or [])}".strip()

    @staticmethod
    def _schema_hash(
        *,
        server_name: str,
        name: str,
        description: str,
        input_schema: dict[str, Any],
    ) -> str:
        payload = {
            "server": server_name,
            "name": name,
            "description": description,
            "input_schema": input_schema,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


_registry: InMemoryFastMCPRegistry | None = None


def get_proxy_registry() -> InMemoryFastMCPRegistry:
    global _registry
    if _registry is None:
        _registry = InMemoryFastMCPRegistry()
    return _registry
