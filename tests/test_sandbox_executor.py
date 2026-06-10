from __future__ import annotations

import asyncio

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


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        delay_seconds: float = 0.0,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.delay_seconds = delay_seconds
        self.killed = False

    async def communicate(self):
        if self.delay_seconds and not self.killed:
            await asyncio.sleep(self.delay_seconds)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True
        self.delay_seconds = 0.0
        self.returncode = -9


def _request(*, limits: SandboxLimits | None = None) -> ExecRequest:
    return ExecRequest(
        tenant_id="tenant-a",
        server="my-funcs",
        tool="add",
        raw_code="def add(a: int, b: int) -> int:\n    return a + b\n",
        requirements=[],
        arguments={"a": 1, "b": 2},
        secrets={},
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

    with pytest.raises(SandboxProtocolError, match="JSON"):
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
        stdout=b'{"ok": true, "result": {"sum": 3}, "stdout": "", "stderr": ""}\n',
        stderr=b"x" * 2048,
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
    """A worker stand-in that blocks in communicate() until released, so a test
    can observe how many runs are in flight at once."""

    def __init__(self, active: dict, release: asyncio.Event, frame: bytes) -> None:
        self.active = active
        self.release = release
        self._frame = frame
        self.returncode = 0
        self.killed = False

    async def communicate(self):
        self.active["now"] += 1
        self.active["max"] = max(self.active["max"], self.active["now"])
        await self.release.wait()
        self.active["now"] -= 1
        return self._frame, b""

    def kill(self):
        self.killed = True


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
