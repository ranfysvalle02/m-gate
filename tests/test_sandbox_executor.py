import asyncio
from typing import Any

import pytest

from config.settings import Settings
from services.sandbox_executor import (
    ExecRequest,
    SandboxError,
    SandboxLimits,
    SandboxProtocolError,
    SandboxTimeoutError,
    WasmExecutor,
)


class _FakeStdin:
    def __init__(self, process: "_FakeProcess") -> None:
        self._process = process
        self._closing = False

    def write(self, data: bytes) -> None:
        self._process.handle_stdin(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self._closing


class _FakeStdout:
    def __init__(self, process: "_FakeProcess") -> None:
        self._process = process

    async def readline(self) -> bytes:
        return await self._process.readline()


class _FakeStderr:
    def __init__(self, process: "_FakeProcess") -> None:
        self._process = process

    async def read(self) -> bytes:
        return self._process.stderr_bytes


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes | list[bytes] = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        delay_seconds: float = 0.0,
        stdin_response: Any = None,
    ) -> None:
        if isinstance(stdout, list):
            self._stdout_lines = list(stdout)
        else:
            self._stdout_lines = [stdout] if stdout else []
        self.stderr_bytes = stderr
        self.returncode = returncode
        self.delay_seconds = delay_seconds
        self.killed = False
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout(self)
        self.stderr = _FakeStderr(self)
        self.stdin_writes: list[bytes] = []
        self._stdin_response = stdin_response
        self._done = asyncio.Event()

    async def communicate(self):
        if self.delay_seconds and not self.killed:
            await asyncio.sleep(self.delay_seconds)
        stdout = b"".join(self._stdout_lines)
        self._stdout_lines = []
        self._done.set()
        return stdout, self.stderr_bytes

    async def readline(self) -> bytes:
        if self.delay_seconds and not self.killed:
            await asyncio.sleep(self.delay_seconds)
        if self._stdout_lines:
            line = self._stdout_lines.pop(0)
            if not line.endswith(b"\n"):
                line += b"\n"
            if not self._stdout_lines:
                self._done.set()
            return line
        self._done.set()
        return b""

    def handle_stdin(self, data: bytes) -> None:
        self.stdin_writes.append(data)
        if self._stdin_response is None:
            return
        for line in data.decode("utf-8", errors="replace").splitlines():
            payload = line.strip()
            if not payload:
                continue
            response = self._stdin_response(payload)
            if response is None:
                continue
            if isinstance(response, bytes):
                self._stdout_lines.append(response)
            else:
                self._stdout_lines.append(str(response).encode("utf-8"))

    def kill(self):
        self.killed = True
        self.delay_seconds = 0.0
        self.returncode = -9
        self._stdout_lines.clear()
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode


def _request(*, limits: SandboxLimits | None = None) -> ExecRequest:
    return ExecRequest(
        tenant_id="tenant-a",
        server="my-funcs",
        tool="add",
        raw_code="def add(a: int, b: int) -> int:\n    return a + b\n",
        requirements=[],
        arguments={"a": 1, "b": 2},
        env={},
        limits=limits,
    )


@pytest.mark.asyncio
async def test_run_success_parses_worker_frame(monkeypatch):
    process = _FakeProcess(
        stdout=b'{"ok": true, "result": {"sum": 3}, "stdout": "out", "stderr": ""}\n',
        returncode=0,
    )

    async def _spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    settings = Settings()
    executor = WasmExecutor(settings=settings, python_bin="python")

    result = await executor.run(_request())
    assert result.payload == {"sum": 3}
    assert result.stdout == "out"
    assert result.stderr == ""
    assert result.elapsed_ms >= 0


@pytest.mark.asyncio
async def test_run_times_out_and_kills_worker(monkeypatch):
    process = _FakeProcess(delay_seconds=1.0)

    async def _spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    settings = Settings()
    executor = WasmExecutor(settings=settings, python_bin="python")

    with pytest.raises(SandboxTimeoutError):
        await executor.run(_request(limits=SandboxLimits(1000, 1024 * 1024, 10, 1024)))
    assert process.killed is True


@pytest.mark.asyncio
async def test_run_rejects_non_json_worker_output(monkeypatch):
    process = _FakeProcess(stdout=b"not-json", stderr=b"", returncode=0)

    async def _spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    settings = Settings()
    executor = WasmExecutor(settings=settings, python_bin="python")

    with pytest.raises(SandboxProtocolError, match="malformed"):
        await executor.run(_request())


@pytest.mark.asyncio
async def test_run_maps_worker_timeout_frame(monkeypatch):
    process = _FakeProcess(
        stdout=(
            b'{"ok": false, "error": {"type": "timeout", "message": "sandbox timed out"}, '
            b'"stdout": "", "stderr": ""}\n'
        ),
        returncode=1,
    )

    async def _spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    settings = Settings()
    executor = WasmExecutor(settings=settings, python_bin="python")

    with pytest.raises(SandboxTimeoutError, match="timed out"):
        await executor.run(_request())


@pytest.mark.asyncio
async def test_run_enforces_output_limit(monkeypatch):
    process = _FakeProcess(
        stdout=(
            b'{"ok": true, "result": {"sum": 3}, '
            b'"stdout": "", "stderr": "' + (b"x" * 2048) + b'"}\n'
        ),
        returncode=0,
    )

    async def _spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    settings = Settings()
    executor = WasmExecutor(settings=settings, python_bin="python")
    with pytest.raises(SandboxProtocolError, match="output-size"):
        await executor.run(_request(limits=SandboxLimits(1000, 1024 * 1024, 1000, 100)))


@pytest.mark.asyncio
async def test_run_handles_db_rpc_frames_before_final_result(monkeypatch):
    rpc_frame = b'{"type":"db_rpc","id":1,"op":"find","collection":"users","args":[{}],"kwargs":{"limit":1}}\n'
    final_frame = b'{"ok": true, "result": {"sum": 3}, "stdout": "", "stderr": ""}\n'
    process = _FakeProcess(stdout=[rpc_frame, final_frame], returncode=0)

    class _Bridge:
        def __init__(self, **_kwargs):
            return None

        async def handle(self, frame):
            assert frame["type"] == "db_rpc"
            return {"type": "db_rpc_result", "id": frame["id"], "ok": True, "result": []}

    async def _spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr("services.sandbox_executor.SandboxDbBridge", _Bridge)
    executor = WasmExecutor(settings=Settings(sandbox_db_bridge_enabled=True), python_bin="python")
    result = await executor.run(_request())
    assert result.payload == {"sum": 3}
    assert any(b'"type": "db_rpc_result"' in line for line in process.stdin_writes)


@pytest.mark.asyncio
async def test_stage_requirements_failure_surfaces_error(monkeypatch):
    process = _FakeProcess(stdout=b"", stderr=b"pip failed", returncode=1)

    async def _spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    # Allowlist the package so it reaches pip (which then fails, as mocked).
    settings = Settings(sandbox_allowed_requirements="requests")
    executor = WasmExecutor(settings=settings, python_bin="python")
    request = _request()
    request = ExecRequest(**{**request.__dict__, "requirements": ["requests==2.32.3"]})
    with pytest.raises(SandboxError, match="requirements"):
        await executor.run(request)


@pytest.mark.asyncio
async def test_requirements_not_in_allowlist_are_rejected_before_pip(monkeypatch):
    spawned = {"count": 0}

    async def _spawn(*_args, **_kwargs):
        spawned["count"] += 1
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    # Default settings => empty allowlist => any requirement is denied.
    executor = WasmExecutor(settings=Settings(), python_bin="python")
    request = ExecRequest(**{**_request().__dict__, "requirements": ["evil-pkg==6.6.6"]})
    with pytest.raises(SandboxError, match="not permitted by the sandbox allowlist"):
        await executor.run(request)
    # The host pip subprocess must never be spawned for a denied requirement.
    assert spawned["count"] == 0


@pytest.mark.asyncio
async def test_run_maps_output_limit_frame(monkeypatch):
    process = _FakeProcess(
        stdout=(
            b'{"ok": false, "error": {"type": "output_limit", "message": "result too big"}, '
            b'"stdout": "", "stderr": ""}\n'
        ),
        returncode=1,
    )

    async def _spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    executor = WasmExecutor(settings=Settings(), python_bin="python")
    with pytest.raises(SandboxError) as excinfo:
        await executor.run(_request())
    # output_limit maps to a plain (non-timeout) SandboxError so the caller does
    # not mistake an oversized result for a deadline breach.
    assert excinfo.type is SandboxError
    assert "too big" in str(excinfo.value)


class _GateProcess:
    """A worker stand-in that blocks in readline() until released, so a test
    can observe how many runs are in flight at once."""

    def __init__(self, active: dict, release: asyncio.Event, frame: bytes) -> None:
        self.active = active
        self.release = release
        self._frame = frame
        self.returncode = 0
        self.killed = False
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStdout(self)
        self.stderr = _FakeStderr(self)
        self.stderr_bytes = b""
        self._sent = False
        self._done = asyncio.Event()

    def handle_stdin(self, data: bytes) -> None:
        return None

    async def readline(self):
        if self._sent:
            self._done.set()
            return b""
        self.active["now"] += 1
        self.active["max"] = max(self.active["max"], self.active["now"])
        await self.release.wait()
        self.active["now"] -= 1
        self._sent = True
        self._done.set()
        return self._frame if self._frame.endswith(b"\n") else self._frame + b"\n"

    def kill(self):
        self.killed = True
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode


@pytest.mark.asyncio
async def test_global_concurrency_caps_total_inflight(monkeypatch):
    active = {"now": 0, "max": 0}
    release = asyncio.Event()
    frame = b'{"ok": true, "result": {"v": 1}, "stdout": "", "stderr": ""}\n'

    async def _spawn(*_args, **_kwargs):
        return _GateProcess(active, release, frame)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    # Global cap of 1, but a generous per-tenant cap, so only the global ceiling
    # can serialize two same-tenant runs.
    settings = Settings(sandbox_max_global_concurrency=1, sandbox_max_concurrency_per_tenant=8)
    executor = WasmExecutor(settings=settings, python_bin="python")
    limits = SandboxLimits(1000, 1024 * 1024, 5000, 4096)
    t1 = asyncio.create_task(executor.run(_request(limits=limits)))
    t2 = asyncio.create_task(executor.run(_request(limits=limits)))
    await asyncio.sleep(0.05)
    assert active["max"] == 1
    release.set()
    await asyncio.gather(t1, t2)
    assert active["max"] == 1


@pytest.mark.asyncio
async def test_no_global_cap_allows_parallel_inflight(monkeypatch):
    active = {"now": 0, "max": 0}
    release = asyncio.Event()
    frame = b'{"ok": true, "result": {"v": 1}, "stdout": "", "stderr": ""}\n'

    async def _spawn(*_args, **_kwargs):
        return _GateProcess(active, release, frame)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    # No global cap (0) + per-tenant cap of 2 => both runs proceed concurrently.
    settings = Settings(sandbox_max_global_concurrency=0, sandbox_max_concurrency_per_tenant=2)
    executor = WasmExecutor(settings=settings, python_bin="python")
    assert executor._global_semaphore is None
    limits = SandboxLimits(1000, 1024 * 1024, 5000, 4096)
    t1 = asyncio.create_task(executor.run(_request(limits=limits)))
    t2 = asyncio.create_task(executor.run(_request(limits=limits)))
    await asyncio.sleep(0.05)
    assert active["max"] == 2
    release.set()
    await asyncio.gather(t1, t2)


@pytest.mark.asyncio
async def test_allowlisted_requirements_install_wheels_only(monkeypatch):
    captured = {"cmd": None}

    async def _spawn(*args, **_kwargs):
        # First spawn is the pip install (capture its argv); return success.
        if captured["cmd"] is None:
            captured["cmd"] = list(args)
            return _FakeProcess(stdout=b"", stderr=b"", returncode=0)
        # Second spawn is the wasm worker; return a valid result frame.
        return _FakeProcess(
            stdout=b'{"ok": true, "result": {"ok": 1}, "stdout": "", "stderr": ""}\n',
            returncode=0,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    settings = Settings(sandbox_allowed_requirements="Requests, orjson")
    executor = WasmExecutor(settings=settings, python_bin="python")
    # Name normalization is spelling-insensitive (Req_uests matches "requests").
    request = ExecRequest(**{**_request().__dict__, "requirements": ["requests==2.32.3"]})
    result = await executor.run(request)
    assert result.payload == {"ok": 1}
    # The pip command forces wheels and disables transitive installs.
    assert "--only-binary=:all:" in captured["cmd"]
    assert "--no-deps" in captured["cmd"]


async def _noop_invoker(server, tool, args):  # pragma: no cover - replaced per test
    return {}


@pytest.mark.asyncio
async def test_run_routes_tool_rpc_frames_to_tool_bridge(monkeypatch):
    tool_frame = (
        b'{"type":"tool_rpc","id":7,"server":"analytics","tool":"track","arguments":{"x":1}}\n'
    )
    final_frame = b'{"ok": true, "result": {"done": true}, "stdout": "", "stderr": ""}\n'
    process = _FakeProcess(stdout=[tool_frame, final_frame], returncode=0)

    class _ToolBridge:
        def __init__(self, **_kwargs):
            return None

        async def handle(self, frame):
            assert frame["type"] == "tool_rpc"
            assert frame["server"] == "analytics"
            assert frame["tool"] == "track"
            return {
                "type": "tool_rpc_result",
                "id": frame["id"],
                "ok": True,
                "result": {"echo": frame["tool"]},
            }

    async def _spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr("services.sandbox_executor.SandboxToolBridge", _ToolBridge)
    executor = WasmExecutor(
        settings=Settings(sandbox_tool_bridge_enabled=True), python_bin="python"
    )
    request = ExecRequest(**{**_request().__dict__, "tool_invoker": _noop_invoker})
    result = await executor.run(request)
    assert result.payload == {"done": True}
    assert any(b'"type": "tool_rpc_result"' in line for line in process.stdin_writes)


@pytest.mark.asyncio
async def test_tool_rpc_frame_without_invoker_is_protocol_breach(monkeypatch):
    # Bridge flag on but no invoker supplied => the run never enables the tool
    # bridge, so an unexpected tool_rpc frame is a protocol error (fail closed).
    tool_frame = b'{"type":"tool_rpc","id":1,"server":"a","tool":"t","arguments":{}}\n'
    process = _FakeProcess(stdout=[tool_frame], returncode=0)

    async def _spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    executor = WasmExecutor(
        settings=Settings(sandbox_tool_bridge_enabled=True), python_bin="python"
    )
    with pytest.raises(SandboxProtocolError):
        await executor.run(_request())


@pytest.mark.asyncio
async def test_nested_request_gets_independent_tenant_semaphore():
    executor = WasmExecutor(
        settings=Settings(sandbox_max_concurrency_per_tenant=1), python_bin="python"
    )
    shared = await executor._tenant_semaphore("t1")
    nested = await executor._tenant_semaphore("t1", nested=True)
    assert nested is not shared
    # Exhaust the shared per-tenant slot; a nested sibling call must not block.
    await shared.acquire()
    assert nested.locked() is False


@pytest.mark.asyncio
async def test_nested_global_guard_is_noop_at_capacity():
    executor = WasmExecutor(
        settings=Settings(sandbox_max_global_concurrency=1), python_bin="python"
    )
    nested = ExecRequest(**{**_request().__dict__, "call_depth": 1})

    async def _enter() -> bool:
        # Hold the only global slot, then a nested run must still pass through.
        async with executor._global_guard():
            async with executor._global_guard(nested):
                return True

    assert await asyncio.wait_for(_enter(), timeout=0.5) is True


def test_job_payload_reflects_tool_bridge_state():
    executor = WasmExecutor(
        settings=Settings(sandbox_tool_bridge_enabled=True), python_bin="python"
    )
    limits = executor._resolve_limits(None)
    enabled = executor._job_payload(
        ExecRequest(**{**_request().__dict__, "tool_invoker": _noop_invoker}), [], limits
    )
    disabled = executor._job_payload(_request(), [], limits)
    assert enabled["tool_bridge"] is True
    assert disabled["tool_bridge"] is False
    # The blocked parent gets headroom for nested sandbox latency.
    assert enabled["db_rpc_wait_ms"] > disabled["db_rpc_wait_ms"]
