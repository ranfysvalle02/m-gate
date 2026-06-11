from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from bson import json_util

from config.settings import Settings, get_settings

# A host-side callback that resolves, authorizes, and executes one sibling tool
# in the caller's tenant namespace. It returns the target tool's result payload
# (a JSON-able object) or raises ``ToolCallDenied`` for a policy failure.
ToolInvoker = Callable[[str, str, dict[str, Any]], Awaitable[Any]]


class ToolCallDenied(Exception):
    """A cross-tool call was rejected by policy (authz, depth, transport, ...).

    Carries a stable ``kind`` so the guest sees a typed, actionable failure
    rather than an opaque error string.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _to_extjson(value: Any) -> Any:
    """Normalize to strict JSON-compatible Extended JSON (mirrors the DB bridge)."""
    return json.loads(json_util.dumps(value))


def _from_extjson(value: Any) -> Any:
    """Decode Extended JSON payloads into native Python/BSON types."""
    return json_util.loads(json.dumps(value))


class SandboxToolBridge:
    """Host-side dispatcher for ``context.tools`` cross-tool calls.

    The sandbox stays network-isolated: a code tool that calls a sibling tool
    has its request relayed here, where an injected ``invoker`` re-authorizes it
    against the original caller and runs the target in its own sandbox. This
    bridge owns only the transport-level concerns (call budget, (de)serialize,
    response-size ceiling, structured failure); all trust decisions live in the
    invoker the RPC layer supplies.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        invoker: ToolInvoker,
        settings: Settings | None = None,
        max_calls_override: int | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.tenant_id = tenant_id
        self.invoker = invoker
        self.calls = 0
        configured_max_calls = max(0, int(self.settings.sandbox_tool_max_calls_per_invocation))
        if max_calls_override is not None:
            configured_max_calls = max(0, int(max_calls_override))
        self.max_calls = configured_max_calls
        self.max_result_bytes = max(1024, int(self.settings.sandbox_tool_max_result_bytes))

    async def handle(self, rpc: dict[str, Any]) -> dict[str, Any]:
        rpc_id = rpc.get("id")
        try:
            payload = await self._dispatch(rpc)
            response: dict[str, Any] = {"ok": True, "result": _to_extjson(payload)}
        except ToolCallDenied as exc:
            response = {"ok": False, "error": {"type": exc.kind, "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001 - always return a structured failure
            response = {"ok": False, "error": {"type": "tool_rpc_error", "message": str(exc)}}

        encoded = json.dumps(response).encode("utf-8")
        if len(encoded) > self.max_result_bytes:
            response = {
                "ok": False,
                "error": {
                    "type": "tool_rpc_error",
                    "message": "Tool call response exceeded size limit.",
                },
            }
        return {"type": "tool_rpc_result", "id": rpc_id, **response}

    async def _dispatch(self, rpc: dict[str, Any]) -> Any:
        if self.max_calls > 0 and self.calls >= self.max_calls:
            raise ToolCallDenied(
                "tool_call_limit",
                "Cross-tool call budget exceeded for this invocation.",
            )
        self.calls += 1

        server = str(rpc.get("server") or "").strip()
        tool = str(rpc.get("tool") or "").strip()
        if not server or not tool:
            raise ToolCallDenied(
                "tool_call_invalid",
                "context.tools call requires both a server and a tool name.",
            )

        raw_arguments = rpc.get("arguments")
        arguments = _from_extjson(raw_arguments) if raw_arguments is not None else {}
        if not isinstance(arguments, dict):
            raise ToolCallDenied(
                "tool_call_invalid",
                "Tool arguments must be passed as keyword arguments (an object).",
            )

        return await self.invoker(server, tool, arguments)
