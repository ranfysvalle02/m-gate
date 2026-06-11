from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from config.settings import Settings
from services.sandbox_errors import SandboxError, SandboxTimeoutError
from services.sandbox_executor import (
    ExecRequest,
    PooledWasmExecutor,
    SandboxLimits,
    SandboxProtocolError,
    WasmExecutor,
)
from services.sandbox_pool import WorkerPool


class _FakeStreamWriter:
    def __init__(self, proc: FakeServeProcess) -> None:
        self._proc = proc
        self._closing = False

    def write(self, data: bytes) -> None:
        self._proc.feed(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True


class _FakeStreamReader:
    def __init__(self, proc: FakeServeProcess) -> None:
        self._proc = proc

    async def readline(self) -> bytes:
        return await self._proc.next_line()


class FakeServeProcess:
    """A stand-in for the long-lived `sandbox_worker --serve` subprocess."""

    def __init__(
        self,
        *,
        job_behavior: str = "ok",
        frame: dict | None = None,
        pong: bool = True,
        returncode: int = 0,
    ) -> None:
        self.job_behavior = job_behavior
        self.frame = frame or {"ok": True, "result": {"v": 1}, "stdout": "", "stderr": ""}
        self.pong = pong
        self._final_returncode = returncode
        self.returncode: int | None = None
        self.pid = id(self) % 100000
        self.killed = False
        self.jobs_seen = 0
        self.stdin = _FakeStreamWriter(self)
        self.stdout = _FakeStreamReader(self)
        self._out: asyncio.Queue[bytes] = asyncio.Queue()
        self._exited = asyncio.Event()

    def feed(self, data: bytes) -> None:
        for line in data.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            request = json.loads(line)
            if request.get("type") == "db_rpc_result":
                if self.job_behavior == "rpc":
                    self._out.put_nowait(json.dumps(self.frame).encode() + b"\n")
                continue
            if request.get("ping"):
                payload = {"ok": True, "pong": True} if self.pong else {"ok": False}
                self._out.put_nowait(json.dumps(payload).encode() + b"\n")
            elif request.get("shutdown"):
                self._exit()
            else:
                self.jobs_seen += 1
                if self.job_behavior == "ok":
                    self._out.put_nowait(json.dumps(self.frame).encode() + b"\n")
                elif self.job_behavior == "rpc":
                    self._out.put_nowait(
                        b'{"type":"db_rpc","id":1,"op":"find","collection":"users","args":[{}],"kwargs":{"limit":1}}\n'
                    )
                elif self.job_behavior == "crash":
                    self._exit()
                elif self.job_behavior == "overrun":
                    # Simulate a result line that blows past the StreamReader limit.
                    self._out.put_nowait(b"__OVERRUN__")
                # "timeout" => never respond

    def _exit(self) -> None:
        if self.returncode is None:
            self.returncode = self._final_returncode
        self._out.put_nowait(b"")
        self._exited.set()

    async def next_line(self) -> bytes:
        data = await self._out.get()
        if data == b"__OVERRUN__":
            raise ValueError("Separator is not found, and chunk exceed the limit")
        return data

    def kill(self) -> None:
        self.killed = True
        self._exit()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0


def _spawn_factory(processes: list, **kwargs):
    async def _spawn(*_args, **_kwargs):
        proc = FakeServeProcess(**kwargs)
        processes.append(proc)
        return proc

    return _spawn


def _settings(**overrides) -> Settings:
    base = {"sandbox_pool_size": 1, "sandbox_pool_warmup_timeout_ms": 1000}
    base.update(overrides)
    return Settings(**base)


@pytest.mark.asyncio
async def test_start_spawns_and_warms_workers(monkeypatch):
    procs: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_factory(procs))
    pool = WorkerPool(_settings(sandbox_pool_size=2), python_bin="python")
    await pool.start()
    assert len(procs) == 2
    assert pool._free.qsize() == 2
    await pool.shutdown()
    assert all(p.returncode is not None for p in procs)


@pytest.mark.asyncio
async def test_start_is_idempotent(monkeypatch):
    procs: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_factory(procs))
    pool = WorkerPool(_settings(sandbox_pool_size=1), python_bin="python")
    await pool.start()
    await pool.start()
    assert len(procs) == 1
    await pool.shutdown()


@pytest.mark.asyncio
async def test_submit_returns_frame_and_returns_worker(monkeypatch):
    procs: list = []
    frame = {"ok": True, "result": {"sum": 3}, "stdout": "out", "stderr": ""}
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_factory(procs, frame=frame))
    pool = WorkerPool(_settings(sandbox_pool_size=1), python_bin="python")
    await pool.start()
    out = await pool.submit(
        job_dir=Path("/tmp/x"), limits={"wall_timeout_ms": 1000}, timeout_ms=1000
    )
    assert out["result"] == {"sum": 3}
    assert pool._free.qsize() == 1
    assert procs[0].jobs_seen == 1
    await pool.shutdown()


@pytest.mark.asyncio
async def test_submit_handles_db_rpc_frames(monkeypatch):
    procs: list = []
    frame = {"ok": True, "result": {"sum": 3}, "stdout": "", "stderr": ""}
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _spawn_factory(procs, job_behavior="rpc", frame=frame)
    )
    pool = WorkerPool(_settings(sandbox_pool_size=1), python_bin="python")
    await pool.start()
    seen: list[dict] = []

    async def _dispatch(rpc_frame):
        seen.append(rpc_frame)
        return {"type": "db_rpc_result", "id": rpc_frame["id"], "ok": True, "result": []}

    out = await pool.submit(
        job_dir=Path("/tmp/x"),
        limits={"wall_timeout_ms": 1000},
        timeout_ms=1000,
        dispatch=_dispatch,
    )
    assert seen and seen[0]["type"] == "db_rpc"
    assert out["result"] == {"sum": 3}
    await pool.shutdown()


@pytest.mark.asyncio
async def test_submit_timeout_kills_and_refills_worker(monkeypatch):
    procs: list = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _spawn_factory(procs, job_behavior="timeout")
    )
    pool = WorkerPool(_settings(sandbox_pool_size=1), python_bin="python")
    await pool.start()
    with pytest.raises(SandboxTimeoutError):
        await pool.submit(job_dir=Path("/tmp/x"), limits={}, timeout_ms=10)
    assert procs[0].killed is True
    # Background refill spawns a replacement to keep the pool at target size.
    for _ in range(50):
        if len(procs) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(procs) == 2
    await pool.shutdown()


@pytest.mark.asyncio
async def test_submit_cancellation_kills_and_refills_worker(monkeypatch):
    procs: list = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _spawn_factory(procs, job_behavior="timeout")
    )
    pool = WorkerPool(_settings(sandbox_pool_size=1), python_bin="python")
    await pool.start()
    task = asyncio.create_task(pool.submit(job_dir=Path("/tmp/x"), limits={}, timeout_ms=5000))
    await asyncio.sleep(0.05)  # let the call acquire the worker and block on readline
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert procs[0].killed is True
    for _ in range(50):
        if len(procs) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(procs) == 2
    await pool.shutdown()


@pytest.mark.asyncio
async def test_submit_worker_crash_raises_and_refills(monkeypatch):
    procs: list = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _spawn_factory(procs, job_behavior="crash")
    )
    pool = WorkerPool(_settings(sandbox_pool_size=1), python_bin="python")
    await pool.start()
    with pytest.raises(SandboxError, match="exited"):
        await pool.submit(job_dir=Path("/tmp/x"), limits={}, timeout_ms=1000)
    for _ in range(50):
        if len(procs) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(procs) == 2
    await pool.shutdown()


@pytest.mark.asyncio
async def test_worker_recycled_after_max_jobs(monkeypatch):
    procs: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_factory(procs))
    pool = WorkerPool(
        _settings(sandbox_pool_size=1, sandbox_worker_max_jobs=1), python_bin="python"
    )
    await pool.start()
    await pool.submit(job_dir=Path("/tmp/a"), limits={}, timeout_ms=1000)
    # First worker should have been recycled; the second submit waits for the refill.
    out = await pool.submit(job_dir=Path("/tmp/b"), limits={}, timeout_ms=1000)
    assert out["result"] == {"v": 1}
    assert len(procs) == 2
    assert procs[0].killed is True
    await pool.shutdown()


@pytest.mark.asyncio
async def test_acquire_timeout_when_no_worker_free(monkeypatch):
    procs: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_factory(procs))
    pool = WorkerPool(
        _settings(sandbox_pool_size=1, sandbox_pool_acquire_timeout_ms=20), python_bin="python"
    )
    await pool.start()
    pool._free.get_nowait()  # drain the only worker so acquire must time out
    with pytest.raises(SandboxError, match="acquire"):
        await pool.submit(job_dir=Path("/tmp/x"), limits={}, timeout_ms=5000)
    await pool.shutdown()


@pytest.mark.asyncio
async def test_submit_skips_dead_idle_worker_and_uses_replacement(monkeypatch):
    procs: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_factory(procs))
    pool = WorkerPool(_settings(sandbox_pool_size=1), python_bin="python")
    await pool.start()
    # Simulate an idle worker dying before it is acquired by a request.
    procs[0]._exit()  # noqa: SLF001 - test-only whitebox helper

    out = await pool.submit(job_dir=Path("/tmp/x"), limits={}, timeout_ms=1000)
    assert out["result"] == {"v": 1}
    for _ in range(50):
        if len(procs) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(procs) == 2
    assert procs[0].killed is True
    await pool.shutdown()


@pytest.mark.asyncio
async def test_start_drops_workers_that_fail_to_warm(monkeypatch):
    procs: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_factory(procs, pong=False))
    pool = WorkerPool(_settings(sandbox_pool_size=2), python_bin="python")
    await pool.start()
    assert len(pool._all) == 0
    assert pool._free.qsize() == 0
    await pool.shutdown()


@pytest.mark.asyncio
async def test_spawn_failure_is_swallowed(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise OSError("cannot fork")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    pool = WorkerPool(_settings(sandbox_pool_size=1), python_bin="python")
    await pool.start()
    assert len(pool._all) == 0
    await pool.shutdown()


@pytest.mark.asyncio
async def test_submit_after_shutdown_raises(monkeypatch):
    procs: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_factory(procs))
    pool = WorkerPool(_settings(sandbox_pool_size=1), python_bin="python")
    await pool.start()
    await pool.shutdown()
    with pytest.raises(SandboxError, match="closed"):
        await pool.submit(job_dir=Path("/tmp/x"), limits={}, timeout_ms=1000)


@pytest.mark.asyncio
async def test_spawn_sizes_stream_limit_to_output_cap(monkeypatch):
    captured: dict = {}

    async def _spawn(*_args, **kwargs):
        captured["limit"] = kwargs.get("limit")
        return FakeServeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    settings = _settings(sandbox_pool_size=1, sandbox_max_output_bytes=262_144)
    pool = WorkerPool(settings, python_bin="python")
    await pool.start()
    # The StreamReader buffer must exceed the worker's own frame budget so a
    # legitimate near-cap frame is never truncated by asyncio's default limit.
    assert captured["limit"] is not None
    assert captured["limit"] > 2 * 262_144
    assert captured["limit"] == pool._stream_limit()
    await pool.shutdown()


@pytest.mark.asyncio
async def test_submit_overrun_frame_raises_protocol_error_and_recycles(monkeypatch):
    procs: list = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _spawn_factory(procs, job_behavior="overrun")
    )
    pool = WorkerPool(_settings(sandbox_pool_size=1), python_bin="python")
    await pool.start()
    with pytest.raises(SandboxProtocolError, match="read buffer limit"):
        await pool.submit(job_dir=Path("/tmp/x"), limits={}, timeout_ms=1000)
    # The offending worker must be discarded and replaced, never reused.
    assert procs[0].killed is True
    for _ in range(50):
        if len(procs) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(procs) == 2
    await pool.shutdown()


@pytest.mark.asyncio
async def test_submit_unexpected_error_discards_worker_no_leak(monkeypatch):
    procs: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_factory(procs))
    pool = WorkerPool(_settings(sandbox_pool_size=1), python_bin="python")
    await pool.start()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("unexpected non-sandbox failure")

    monkeypatch.setattr(pool, "_run_on_worker", _boom)
    with pytest.raises(RuntimeError):
        await pool.submit(job_dir=Path("/tmp/x"), limits={}, timeout_ms=1000)
    # A non-SandboxError must still discard the worker (no capacity leak) and
    # trigger a background refill back to target size.
    assert procs[0].killed is True
    assert pool._free.qsize() == 0
    for _ in range(50):
        if len(procs) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(procs) == 2
    await pool.shutdown()


# ---- PooledWasmExecutor ----------------------------------------------------


class _FakePool:
    def __init__(self, frame: dict) -> None:
        self.frame = frame
        self.submitted: list = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.closed = True

    async def submit(self, *, job_dir, limits, timeout_ms, dispatch=None):
        self.submitted.append((job_dir, limits, timeout_ms, dispatch))
        return self.frame


def _request(*, limits: SandboxLimits | None = None) -> ExecRequest:
    return ExecRequest(
        tenant_id="tenant-a",
        server="my-funcs",
        tool="add",
        raw_code="def add(a, b):\n    return a + b\n",
        requirements=[],
        arguments={"a": 1, "b": 2},
        env={},
        limits=limits,
    )


@pytest.mark.asyncio
async def test_pooled_executor_run_uses_pool():
    frame = {"ok": True, "result": {"sum": 3}, "stdout": "o", "stderr": ""}
    pool = _FakePool(frame)
    executor = PooledWasmExecutor(Settings(), python_bin="python", pool=pool)
    result = await executor.run(_request())
    assert result.payload == {"sum": 3}
    assert result.stdout == "o"
    assert pool.submitted and pool.submitted[0][2] >= 1
    await executor.prewarm()
    await executor.aclose()
    assert pool.started is True
    assert pool.closed is True


@pytest.mark.asyncio
async def test_pooled_executor_enforces_output_limit():
    frame = {"ok": True, "result": {"sum": 3}, "stdout": "x" * 100, "stderr": ""}
    executor = PooledWasmExecutor(Settings(), python_bin="python", pool=_FakePool(frame))
    with pytest.raises(SandboxProtocolError, match="output-size"):
        await executor.run(_request(limits=SandboxLimits(1000, 1024 * 1024, 1000, 10)))


@pytest.mark.asyncio
async def test_pooled_executor_maps_error_frame():
    frame = {"ok": False, "error": {"type": "timeout", "message": "deadline"}}
    executor = PooledWasmExecutor(Settings(), python_bin="python", pool=_FakePool(frame))
    with pytest.raises(SandboxTimeoutError):
        await executor.run(_request())


@pytest.mark.asyncio
async def test_pooled_executor_disabled_runtime_raises():
    executor = PooledWasmExecutor(
        Settings(code_executor="disabled"), python_bin="python", pool=_FakePool({})
    )
    with pytest.raises(SandboxError, match="disabled"):
        await executor.run(_request())


# ---- get_executor selection + lifecycle ------------------------------------


def test_get_executor_selects_pooled_when_enabled(monkeypatch, reset_settings):
    monkeypatch.setenv("SANDBOX_POOL_SIZE", "2")
    monkeypatch.setenv("CODE_EXECUTOR", "wasm")
    import services.sandbox_executor as se

    monkeypatch.setattr(se, "_executor", None)
    executor = se.get_executor()
    assert isinstance(executor, se.PooledWasmExecutor)


def test_get_executor_defaults_to_throwaway(monkeypatch, reset_settings):
    monkeypatch.setenv("SANDBOX_POOL_SIZE", "0")
    import services.sandbox_executor as se

    monkeypatch.setattr(se, "_executor", None)
    executor = se.get_executor()
    assert isinstance(executor, WasmExecutor)
    assert not isinstance(executor, se.PooledWasmExecutor)


@pytest.mark.asyncio
async def test_prewarm_and_shutdown_executor_lifecycle(monkeypatch, reset_settings):
    monkeypatch.setenv("SANDBOX_POOL_SIZE", "1")
    monkeypatch.setenv("CODE_EXECUTOR", "wasm")
    procs: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_factory(procs))
    import services.sandbox_executor as se

    monkeypatch.setattr(se, "_executor", None)
    await se.prewarm_executor()
    assert isinstance(se._executor, se.PooledWasmExecutor)
    assert len(procs) == 1
    await se.shutdown_executor()
    monkeypatch.setattr(se, "_executor", None)


@pytest.mark.asyncio
async def test_prewarm_is_noop_for_throwaway_executor(monkeypatch, reset_settings):
    monkeypatch.setenv("SANDBOX_POOL_SIZE", "0")
    import services.sandbox_executor as se

    monkeypatch.setattr(se, "_executor", None)
    await se.prewarm_executor()
    assert isinstance(se._executor, WasmExecutor)
    await se.shutdown_executor()  # no aclose() => no-op
    monkeypatch.setattr(se, "_executor", None)
