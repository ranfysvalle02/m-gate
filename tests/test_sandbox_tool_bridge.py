import pytest
from bson import ObjectId

from config.settings import Settings
from services.sandbox_tool_bridge import SandboxToolBridge, ToolCallDenied


def _bridge(invoker, **overrides):
    return SandboxToolBridge(
        tenant_id="tenant-a",
        invoker=invoker,
        settings=Settings(**overrides),
    )


@pytest.mark.asyncio
async def test_handle_success_roundtrips_result():
    async def invoker(server, tool, args):
        assert server == "analytics"
        assert tool == "track_click"
        assert args == {"target": "home"}
        return {"count": 3}

    bridge = _bridge(invoker)
    resp = await bridge.handle(
        {
            "id": 1,
            "server": "analytics",
            "tool": "track_click",
            "arguments": {"target": "home"},
        }
    )
    assert resp == {"type": "tool_rpc_result", "id": 1, "ok": True, "result": {"count": 3}}


@pytest.mark.asyncio
async def test_handle_decodes_extjson_arguments_to_native():
    seen: dict = {}

    async def invoker(server, tool, args):
        seen.update(args)
        return {"ok": True}

    bridge = _bridge(invoker)
    await bridge.handle(
        {
            "id": 2,
            "server": "s",
            "tool": "t",
            "arguments": {"id": {"$oid": "0123456789abcdef01234567"}},
        }
    )
    # Extended JSON in must arrive at the invoker as a native BSON type.
    assert isinstance(seen["id"], ObjectId)


@pytest.mark.asyncio
async def test_handle_maps_denied_to_typed_error():
    async def invoker(server, tool, args):
        raise ToolCallDenied("forbidden", "not allowed here")

    bridge = _bridge(invoker)
    resp = await bridge.handle({"id": 3, "server": "s", "tool": "t", "arguments": {}})
    assert resp["ok"] is False
    assert resp["error"]["type"] == "forbidden"
    assert "not allowed here" in resp["error"]["message"]


@pytest.mark.asyncio
async def test_handle_maps_unexpected_error_to_generic_failure():
    async def invoker(server, tool, args):
        raise RuntimeError("boom")

    bridge = _bridge(invoker)
    resp = await bridge.handle({"id": 4, "server": "s", "tool": "t", "arguments": {}})
    assert resp["ok"] is False
    assert resp["error"]["type"] == "tool_rpc_error"


@pytest.mark.asyncio
async def test_handle_requires_server_and_tool():
    async def invoker(server, tool, args):  # pragma: no cover - should not run
        return {}

    bridge = _bridge(invoker)
    resp = await bridge.handle({"id": 5, "server": "", "tool": "", "arguments": {}})
    assert resp["ok"] is False
    assert resp["error"]["type"] == "tool_call_invalid"


@pytest.mark.asyncio
async def test_handle_rejects_non_object_arguments():
    async def invoker(server, tool, args):  # pragma: no cover - should not run
        return {}

    bridge = _bridge(invoker)
    resp = await bridge.handle({"id": 6, "server": "s", "tool": "t", "arguments": [1, 2]})
    assert resp["ok"] is False
    assert resp["error"]["type"] == "tool_call_invalid"


@pytest.mark.asyncio
async def test_handle_enforces_call_budget():
    calls = {"n": 0}

    async def invoker(server, tool, args):
        calls["n"] += 1
        return {}

    bridge = _bridge(invoker, sandbox_tool_max_calls_per_invocation=2)
    ok1 = await bridge.handle({"id": 1, "server": "s", "tool": "t", "arguments": {}})
    ok2 = await bridge.handle({"id": 2, "server": "s", "tool": "t", "arguments": {}})
    over = await bridge.handle({"id": 3, "server": "s", "tool": "t", "arguments": {}})
    assert ok1["ok"] and ok2["ok"]
    assert over["ok"] is False
    assert over["error"]["type"] == "tool_call_limit"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_handle_enforces_result_size_cap():
    async def invoker(server, tool, args):
        return {"blob": "x" * 5000}

    bridge = _bridge(invoker, sandbox_tool_max_result_bytes=1024)
    resp = await bridge.handle({"id": 1, "server": "s", "tool": "t", "arguments": {}})
    assert resp["ok"] is False
    assert "size limit" in resp["error"]["message"]
