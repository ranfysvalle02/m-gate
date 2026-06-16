from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport

from config.settings import get_settings
from database.mongo import get_tenant_database
from services.authorization import get_authorization_service
from services.code_tools import decrypt_raw_code
from services.credential_broker import (
    CallerIdentity,
    MintedCredential,
    get_credential_broker,
    resolve_auth_scheme,
)
from services.egress_policy import EgressNotAllowed
from services.egress_transport import make_egress_client_factory
from services.embeddings import EmbeddingService, get_embedding_service
from services.metrics import observe_usage
from services.sandbox_executor import (
    ExecRequest,
    Executor,
    SandboxError,
    SandboxProtocolError,
    SandboxTimeoutError,
    get_executor,
)
from services.sandbox_tool_bridge import ToolCallDenied, ToolInvoker
from services.server_guard import assert_mountable
from services.tracing import set_span_attribute, start_span
from services.usage_metering import emit_billing_event, record_usage

logger = logging.getLogger(__name__)

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
    origin: str = "platform"
    endpoint: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None
    enabled: bool = True
    metadata: dict[str, Any] | None = None


@dataclass
class PooledClient:
    client: Client
    credential: MintedCredential


class InMemoryFastMCPRegistry:
    def __init__(
        self,
        embedding_service: EmbeddingService | None = None,
        credential_broker=None,
        executor: Executor | None = None,
    ) -> None:
        self.settings = get_settings()
        self.embedding_service = embedding_service or get_embedding_service(self.settings)
        self.credential_broker = credential_broker or get_credential_broker()
        self.executor = executor or get_executor()
        self._servers: dict[tuple[str, str], DownstreamServer] = {}
        self._lock = asyncio.Lock()
        # Warm, long-lived downstream clients keyed by (tenant, server). FastMCP's
        # Client is reentrant: we hold one base session open here so each call_tool
        # reuses it instead of paying a full connect/handshake per request. A
        # per-key lock serializes connect/evict so concurrent callers don't race
        # to open (or tear down) the same session.
        self._clients: dict[tuple[str, str], PooledClient] = {}
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
        assert_mountable(server_doc)
        tenant_id = str(server_doc.get("tenant_id") or self.settings.default_tenant_id)
        server_name = server_doc["server"]
        origin = str(server_doc.get("origin") or "platform")
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
                origin=origin,
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
        await self.credential_broker.invalidate(server_name, tenant_id=tenant_id)
        await self.sync_tool_catalog(server_doc)

    async def unmount(self, server_name: str, tenant_id: str | None = None) -> None:
        resolved_tenant = tenant_id or self.settings.default_tenant_id
        async with self._lock:
            self._servers.pop((resolved_tenant, server_name), None)
        await self._evict_client((resolved_tenant, server_name))
        await self.credential_broker.invalidate(server_name, tenant_id=resolved_tenant)
        await get_tenant_database(resolved_tenant)["tool_catalog"].delete_many(
            {"server": server_name}
        )

    async def unmount_by_id(self, server_name: str, tenant_id: str | None = None) -> None:
        await self.unmount(server_name, tenant_id=tenant_id)

    async def refresh_server_credentials(self, server_name: str, tenant_id: str) -> None:
        """Drop warm client + cached credential so next call re-authenticates."""
        await self._evict_client((tenant_id, server_name))
        await self.credential_broker.invalidate(server_name, tenant_id=tenant_id)

    async def sync_tool_catalog(self, server_doc: dict[str, Any]) -> None:
        tenant_id = str(server_doc.get("tenant_id") or self.settings.default_tenant_id)
        server_name = server_doc["server"]
        # Code tools are authored, not discovered; their definitions are always
        # supplied on the document and never fetched from a downstream session.
        server_transport = str(server_doc.get("transport") or "")
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
            # Surface the transport on the searchable catalog doc (never raw_code)
            # so the call path can gate code-tool execution from catalog metadata
            # alone, without re-reading the encrypted routing registry.
            tool_metadata = dict(tool.get("metadata") or {})
            if server_transport == "code":
                tool_metadata["transport"] = "code"
                # Surface a per-tool wall-clock budget (when authored) so the
                # quota preflight can project this tool's worst-case sandbox cost
                # from the catalog doc alone. Sanitize to a positive int; drop it
                # otherwise so a bogus value falls back to the global default.
                try:
                    wall_timeout_ms = int(tool_metadata.get("wall_timeout_ms") or 0)
                except (TypeError, ValueError):
                    wall_timeout_ms = 0
                if wall_timeout_ms > 0:
                    tool_metadata["wall_timeout_ms"] = wall_timeout_ms
                else:
                    tool_metadata.pop("wall_timeout_ms", None)
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
                        "metadata": tool_metadata,
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
        caller: CallerIdentity | None = None,
        call_depth: int = 0,
    ) -> dict[str, Any]:
        resolved_tenant = tenant_id or self.settings.default_tenant_id
        server = self.get_server(server_name, tenant_id=resolved_tenant)
        if server is None:
            raise KeyError(f"Server '{server_name}' is not mounted for tenant '{resolved_tenant}'.")
        if server.transport == "code":
            # Code-backed tools are executed by the local sandbox runtime rather
            # than a downstream MCP transport session.
            return await self._execute_code_tool(
                server=server,
                tool_name=tool_name,
                arguments=arguments,
                caller=caller,
                call_depth=call_depth,
            )
        attempts = 3
        timeout_seconds = self.settings.downstream_timeout_ms / 1000
        with start_span(
            "downstream.jsonrpc",
            {
                "mcp.server": server_name,
                "mcp.tool": tool_name,
                "mcp.tenant_id": resolved_tenant,
                # Caller identity is recorded for audit/trace only; the downstream
                # credential is a tenant-scoped workload identity, not this token.
                "mcp.actor": caller.user_id if caller else "unknown-user",
                "downstream.transport": server.transport,
                "downstream.endpoint": server.endpoint,
                "downstream.auth_scheme": self._auth_scheme(server.metadata),
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

    def make_tool_invoker(
        self,
        *,
        tenant_id: str,
        caller: CallerIdentity | None,
        call_depth: int,
    ) -> ToolInvoker | None:
        """Build the host-side callback that runs a sibling code tool.

        Returns None (cross-tool calls disabled for the run) unless the operator
        enabled the bridge, an authenticated caller is present, and there is at
        least one more level of nesting budget left. Every relayed call is
        re-authorized against the ORIGINAL caller's scopes/roles, refuses
        confirmation-gated tools (no human in the loop), and is restricted to
        code servers in the same tenant -- the sandbox never gains network reach.
        """
        if not self.settings.sandbox_tool_bridge_enabled:
            return None
        if caller is None:
            return None
        max_depth = max(0, int(self.settings.sandbox_tool_call_max_depth))
        if call_depth >= max_depth:
            return None

        next_depth = call_depth + 1
        caller_scopes = [scope for scope in (getattr(caller, "scopes", None) or []) if scope]
        caller_roles = [role for role in (getattr(caller, "roles", None) or []) if role]

        async def _invoke(target_server: str, target_tool: str, target_args: dict[str, Any]) -> Any:
            authz = await get_authorization_service().authorize_tool_call(
                tenant_id=tenant_id,
                server=target_server,
                name=target_tool,
                caller_scopes=caller_scopes,
                caller_roles=caller_roles,
            )
            if not authz.allowed:
                raise ToolCallDenied(
                    "forbidden",
                    f"Not authorized to call {target_server}/{target_tool} ({authz.reason}).",
                )
            tool_meta = (
                (authz.tool or {}).get("metadata", {}) if isinstance(authz.tool, dict) else {}
            )
            if bool(tool_meta.get("requires_confirmation")):
                raise ToolCallDenied(
                    "confirmation_required",
                    f"{target_server}/{target_tool} requires human confirmation and "
                    "cannot be called from another tool.",
                )
            target = self.get_server(target_server, tenant_id=tenant_id)
            if target is None:
                raise ToolCallDenied(
                    "tool_not_found",
                    f"Server '{target_server}' is not mounted for this tenant.",
                )
            if target.transport != "code":
                raise ToolCallDenied(
                    "tool_not_callable",
                    f"'{target_server}' is not a code server; only code tools can be "
                    "invoked through context.tools.",
                )
            try:
                return await self.call_tool(
                    target_server,
                    target_tool,
                    target_args,
                    tenant_id=tenant_id,
                    caller=caller,
                    call_depth=next_depth,
                )
            except (DownstreamTimeout, SandboxTimeoutError) as exc:
                raise ToolCallDenied("tool_timeout", str(exc)) from exc
            except KeyError as exc:
                raise ToolCallDenied("tool_not_found", str(exc)) from exc
            except DownstreamError as exc:
                raise ToolCallDenied("tool_error", str(exc)) from exc

        return _invoke

    async def _execute_code_tool(
        self,
        *,
        server: DownstreamServer,
        tool_name: str,
        arguments: dict[str, Any],
        caller: CallerIdentity | None = None,
        call_depth: int = 0,
    ) -> dict[str, Any]:
        if not self.settings.code_tool_execution_enabled:
            raise DownstreamProtocolError("Code tool execution is disabled.")

        routing_doc = await get_tenant_database(server.tenant_id)["routing_registry"].find_one(
            {"_id": server.server}
        )
        if not routing_doc:
            raise KeyError(
                f"Server '{server.server}' is not present in routing_registry for "
                f"tenant '{server.tenant_id}'."
            )

        tool_doc: dict[str, Any] | None = None
        for candidate in routing_doc.get("tools") or []:
            if candidate.get("name") == tool_name:
                tool_doc = candidate
                break
        if tool_doc is None:
            raise KeyError(f"Tool '{tool_name}' is not defined on code server '{server.server}'.")

        raw_code = await decrypt_raw_code(server.tenant_id, tool_doc.get("raw_code"))
        if not raw_code.strip():
            raise DownstreamProtocolError(
                f"Code tool '{server.server}/{tool_name}' has no decryptable source."
            )

        requirements = [
            str(req) for req in (tool_doc.get("requirements") or []) if isinstance(req, str)
        ]
        raw_metadata = tool_doc.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        env = await self._read_server_env(server.tenant_id, server.server)
        tool_invoker = self.make_tool_invoker(
            tenant_id=server.tenant_id,
            caller=caller,
            call_depth=call_depth,
        )
        request = ExecRequest(
            tenant_id=server.tenant_id,
            server=server.server,
            tool=tool_name,
            raw_code=raw_code,
            requirements=requirements,
            arguments=arguments,
            env=env,
            action_type=str(metadata.get("action_type") or "read"),
            tool_invoker=tool_invoker,
            call_depth=call_depth,
        )
        timeout_seconds = self.settings.sandbox_wall_timeout_ms / 1000
        try:
            result = await asyncio.wait_for(self.executor.run(request), timeout=timeout_seconds)
        except TimeoutError as exc:
            raise DownstreamTimeout(
                f"Code tool '{server.server}/{tool_name}' timed out after "
                f"{self.settings.sandbox_wall_timeout_ms}ms"
            ) from exc
        except SandboxTimeoutError as exc:
            raise DownstreamTimeout(str(exc)) from exc
        except SandboxProtocolError as exc:
            raise DownstreamProtocolError(str(exc)) from exc
        except SandboxError as exc:
            raise DownstreamError(str(exc)) from exc

        await record_usage(
            server.tenant_id,
            sandbox_ms=max(0, int(result.elapsed_ms)),
        )
        await emit_billing_event(
            server.tenant_id,
            kind="sandbox_ms",
            amount=max(0, int(result.elapsed_ms)),
            metadata={"server": server.server, "tool": tool_name},
        )
        observe_usage("sandbox_ms", max(0, int(result.elapsed_ms)))
        return self._validate_result(result.payload)

    async def _read_server_env(self, tenant_id: str, server_name: str) -> dict[str, str]:
        doc = await get_tenant_database(tenant_id)["server_secrets"].find_one({"_id": server_name})
        encrypted = doc.get("values", {}) if isinstance(doc, dict) else {}
        if not isinstance(encrypted, dict):
            return {}
        plaintext: dict[str, str] = {}
        for key, value in encrypted.items():
            if not isinstance(key, str):
                continue
            decrypted = await decrypt_raw_code(tenant_id, value if isinstance(value, str) else None)
            if decrypted:
                plaintext[key] = decrypted
        return plaintext

    async def discover_tools(
        self, server_name: str, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        server = self.get_server(server_name, tenant_id=tenant_id)
        if server is None:
            return []
        try:
            # Discovery must also present downstream auth so catalogs can be
            # synced against protected downstream servers.
            credential = await self.credential_broker.mint(
                server_name=server.server,
                tenant_id=server.tenant_id,
                metadata=server.metadata,
            )
            self._assert_credential_transport_security(server=server, credential=credential)
            client = self._build_client(server, credential=credential)
            async with client:
                tools = await client.list_tools()
            return [self._normalize_tool_schema(tool) for tool in tools]
        except Exception:
            # An empty tool list is the safe degraded result, but logging the
            # cause keeps a misconfigured/unreachable downstream distinguishable
            # from a server that legitimately exposes no tools.
            logger.warning(
                "Tool discovery failed for downstream '%s' (tenant=%s); returning no tools.",
                server.server,
                server.tenant_id,
                exc_info=True,
            )
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
            if self._contains_egress_block(exc):
                # The connect-time egress gate (already metered in the transport)
                # rejected this connection; surface a protocol-safe, retry-stable
                # downstream error rather than a generic transport failure.
                target = self._target(server)
                raise DownstreamError(
                    f"Downstream '{target}' blocked by egress allowlist."
                ) from exc
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
        cached client has lost its session (downstream restart, idle reap) or its
        just-in-time credential is within the refresh-skew window of expiry, it is
        discarded and reconnected with a freshly minted token so callers never get
        a dead handle or an expired credential.

        The warm-hit path checks the *stored* credential's expiry rather than
        re-minting on every call, so steady-state requests never contend on the
        broker; a token is only minted when a (re)connect is actually required.
        """
        async with self._key_lock(key):
            pooled = self._clients.get(key)
            if (
                pooled is not None
                and pooled.client.is_connected()
                and not self.credential_broker.near_expiry(pooled.credential)
            ):
                return pooled.client
            if pooled is not None:
                # Stale (disconnected) or near-expiry — drop it before reconnecting.
                await self._close_client(pooled.client)
                self._clients.pop(key, None)

            try:
                credential = await self.credential_broker.mint(
                    server_name=server.server,
                    tenant_id=server.tenant_id,
                    metadata=server.metadata,
                )
            except Exception as exc:
                # A mint failure must surface as a protocol-safe downstream error,
                # never crash the request path. Token contents are never logged.
                target = self._target(server)
                raise DownstreamError(
                    f"Failed to mint downstream credential for '{target}': {exc}"
                ) from exc

            self._assert_credential_transport_security(server=server, credential=credential)
            client = self._build_client(server, credential=credential)
            await client.__aenter__()
            self._clients[key] = PooledClient(client=client, credential=credential)
            return client

    async def _evict_client(self, key: tuple[str, str]) -> None:
        """Remove and close the pooled client for ``key`` if present."""
        async with self._key_lock(key):
            pooled = self._clients.pop(key, None)
        if pooled is not None:
            await self._close_client(pooled.client)

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

    @staticmethod
    def _contains_egress_block(exc: BaseException) -> bool:
        """Detect an :class:`EgressNotAllowed` anywhere in an exception tree.

        The async MCP transport wraps connect-time failures (and may bundle them
        in an ``ExceptionGroup`` via anyio task groups), so we walk both the
        cause/context chain and any exception-group members.
        """
        seen: set[int] = set()
        stack: list[BaseException] = [exc]
        while stack:
            current = stack.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, EgressNotAllowed):
                return True
            members = getattr(current, "exceptions", None)
            if members:
                stack.extend(member for member in members if isinstance(member, BaseException))
            if current.__cause__ is not None:
                stack.append(current.__cause__)
            if current.__context__ is not None:
                stack.append(current.__context__)
        return False

    def _egress_factory(self, server: DownstreamServer):
        """Return a pinning httpx client factory, or None when egress is disabled.

        The factory resolves the effective policy (global ceiling intersected with
        the tenant allowlist) lazily on each connect, so the per-tenant allowlist
        read only happens on a real outbound connection. When the allowlist
        feature is disabled the stock client is used unchanged.
        """
        if not self.settings.egress_allowlist_enabled:
            return None
        return make_egress_client_factory(settings=self.settings, tenant_id=server.tenant_id)

    def _build_client(
        self,
        server: DownstreamServer,
        credential: MintedCredential | None = None,
    ) -> Client:
        headers = credential.headers if credential else None
        if server.transport == "streamable_http":
            return Client(
                StreamableHttpTransport(
                    url=str(server.endpoint),
                    headers=headers,
                    httpx_client_factory=self._egress_factory(server),
                )
            )
        if server.transport == "sse":
            return Client(
                SSETransport(
                    url=str(server.endpoint),
                    headers=headers,
                    httpx_client_factory=self._egress_factory(server),
                )
            )
        if server.transport == "stdio":
            if not server.command:
                raise ValueError(f"Server '{server.server}' stdio transport missing command.")
            env = dict(server.env or {})
            if credential:
                env.update(credential.env)
            return Client(
                StdioTransport(
                    command=server.command,
                    args=server.args or [],
                    env=env or None,
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
    def _auth_scheme(metadata: dict[str, Any] | None) -> str:
        return resolve_auth_scheme(metadata)

    def _assert_credential_transport_security(
        self, *, server: DownstreamServer, credential: MintedCredential
    ) -> None:
        if self.settings.downstream_allow_insecure_credentials:
            return
        if server.transport not in {"streamable_http", "sse"}:
            return
        endpoint = str(server.endpoint or "").strip().lower()
        if not endpoint.startswith("http://"):
            return
        scheme = self._auth_scheme(server.metadata)
        # ``none`` intentionally sends no auth material.
        if scheme == "none":
            return
        if not credential.headers and not credential.env:
            return
        target = self._target(server)
        raise DownstreamError(
            f"Refusing to send downstream '{scheme}' credentials over insecure endpoint '{target}'."
        )

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
