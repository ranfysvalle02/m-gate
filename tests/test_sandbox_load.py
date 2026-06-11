from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path

import pytest

from config.settings import Settings
from services.sandbox_errors import SandboxError
from services.sandbox_executor import ExecRequest, SandboxLimits, WasmExecutor
from services.sandbox_pool import WorkerPool


def _request(tenant_id: str, *, limits: SandboxLimits) -> ExecRequest:
    return ExecRequest(
        tenant_id=tenant_id,
        server="sandbox-load",
        tool="run",
        raw_code="def run() -> dict[str, bool]:\n    return {'ok': True}\n",
        requirements=[],
        arguments={},
        env={},
        limits=limits,
    )


class _GatedProcess:
    """Fake worker process that blocks until a shared release event is set."""

    def __init__(
        self,
        *,
        active: dict[str, int],
        release: asyncio.Event,
        tenant_id: str | None = None,
        by_tenant: dict[str, int] | None = None,
        by_tenant_max: dict[str, int] | None = None,
    ) -> None:
        self.active = active
        self.release = release
        self.tenant_id = tenant_id or "unknown"
        self.by_tenant = by_tenant
        self.by_tenant_max = by_tenant_max
        self.returncode = 0
        self.killed = False
        self._frame = b'{"ok": true, "result": {"ok": true}, "stdout": "", "stderr": ""}\n'
        self.stderr = _FakeStreamReader(self)  # placeholder for read()
        self.stdin = _FakeStreamWriter(self)
        self.stdout = _FakeStreamReader(self)
        self._done = asyncio.Event()
        self._sent = False

    async def readline(self) -> bytes:
        if self._sent:
            self._done.set()
            return b""
        self.active["now"] += 1
        self.active["max"] = max(self.active["max"], self.active["now"])
        if self.by_tenant is not None and self.by_tenant_max is not None:
            self.by_tenant[self.tenant_id] = self.by_tenant.get(self.tenant_id, 0) + 1
            self.by_tenant_max[self.tenant_id] = max(
                self.by_tenant_max.get(self.tenant_id, 0), self.by_tenant[self.tenant_id]
            )
        await self.release.wait()
        if self.by_tenant is not None and self.by_tenant_max is not None:
            self.by_tenant[self.tenant_id] -= 1
        self.active["now"] -= 1
        self._sent = True
        self._done.set()
        return self._frame

    async def read(self) -> bytes:
        return b""

    def feed(self, _data: bytes) -> None:
        return None

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode


class _FakeStreamWriter:
    def __init__(self, proc: _FakeServeProcess) -> None:
        self._proc = proc
        self._closing = False

    def write(self, data: bytes) -> None:
        self._proc.feed(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self._closing


class _FakeStreamReader:
    def __init__(self, proc: _FakeServeProcess) -> None:
        self._proc = proc

    async def readline(self) -> bytes:
        if hasattr(self._proc, "next_line"):
            return await self._proc.next_line()
        if hasattr(self._proc, "readline"):
            return await self._proc.readline()
        return b""

    async def read(self) -> bytes:
        if hasattr(self._proc, "read"):
            return await self._proc.read()
        return b""


class _FakeServeProcess:
    """Stand-in for `sandbox_worker --serve` for pool load tests."""

    def __init__(
        self,
        *,
        behaviors: list[str] | None = None,
        gate_event: asyncio.Event | None = None,
    ) -> None:
        self.behaviors = deque(behaviors or ["ok"])
        self.gate_event = gate_event
        self.returncode: int | None = None
        self.killed = False
        self.stdin = _FakeStreamWriter(self)
        self.stdout = _FakeStreamReader(self)
        self._out: asyncio.Queue[bytes] = asyncio.Queue()
        self._exited = asyncio.Event()

    def feed(self, data: bytes) -> None:
        for line in data.decode("utf-8").splitlines():
            request = json.loads(line)
            if request.get("ping"):
                self._out.put_nowait(b'{"ok": true, "pong": true}\n')
                continue
            if request.get("shutdown"):
                self._exit()
                continue
            behavior = self.behaviors.popleft() if self.behaviors else "ok"
            if behavior == "ok":
                self._out.put_nowait(
                    b'{"ok": true, "result": {"v": 1}, "stdout": "", "stderr": ""}\n'
                )
            elif behavior == "crash":
                self._exit()
            elif behavior == "gate":
                if self.gate_event is not None:

                    async def _release_frame() -> None:
                        await self.gate_event.wait()
                        self._out.put_nowait(
                            b'{"ok": true, "result": {"v": 1}, "stdout": "", "stderr": ""}\n'
                        )

                    asyncio.create_task(_release_frame())
            # "timeout" => no frame

    async def next_line(self) -> bytes:
        return await self._out.get()

    def _exit(self) -> None:
        if self.returncode is None:
            self.returncode = 0
        self._out.put_nowait(b"")
        self._exited.set()

    def kill(self) -> None:
        self.killed = True
        self._exit()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0


@pytest.mark.load
@pytest.mark.asyncio
async def test_global_concurrency_ceiling_never_exceeded_under_fanout(monkeypatch):
    active = {"now": 0, "max": 0}
    release = asyncio.Event()

    async def _spawn(*_args, **_kwargs):
        return _GatedProcess(active=active, release=release)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    settings = Settings(sandbox_max_global_concurrency=3, sandbox_max_concurrency_per_tenant=20)
    executor = WasmExecutor(settings=settings, python_bin="python")
    limits = SandboxLimits(1000, 1024 * 1024, 5000, 4096)
    tasks = [
        asyncio.create_task(executor.run(_request(f"tenant-{index % 4}", limits=limits)))
        for index in range(24)
    ]
    await asyncio.sleep(0.08)
    assert active["max"] == 3
    release.set()
    await asyncio.gather(*tasks)
    assert active["max"] == 3


@pytest.mark.load
@pytest.mark.asyncio
async def test_multitenant_fairness_no_starvation_under_global_cap(monkeypatch):
    active = {"now": 0, "max": 0}
    by_tenant: dict[str, int] = {}
    by_tenant_max: dict[str, int] = {}
    finished: dict[str, int] = {}
    release = asyncio.Event()
    queued_tenants: deque[str] = deque()

    original_job_payload = WasmExecutor._job_payload

    def _capture_tenant(self, request: ExecRequest, python_paths: list[str], limits: SandboxLimits):
        queued_tenants.append(request.tenant_id)
        return original_job_payload(self, request, python_paths, limits)

    async def _spawn(*_args, **_kwargs):
        tenant_id = queued_tenants.popleft() if queued_tenants else "unknown"
        return _GatedProcess(
            active=active,
            release=release,
            tenant_id=tenant_id,
            by_tenant=by_tenant,
            by_tenant_max=by_tenant_max,
        )

    monkeypatch.setattr(WasmExecutor, "_job_payload", _capture_tenant)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    settings = Settings(sandbox_max_global_concurrency=3, sandbox_max_concurrency_per_tenant=1)
    executor = WasmExecutor(settings=settings, python_bin="python")
    limits = SandboxLimits(1000, 1024 * 1024, 5000, 4096)

    async def _run_one(tenant_id: str) -> None:
        await executor.run(_request(tenant_id, limits=limits))
        finished[tenant_id] = finished.get(tenant_id, 0) + 1

    tenants = [f"tenant-{index}" for index in range(4)]
    tasks = [asyncio.create_task(_run_one(tenant)) for tenant in tenants for _ in range(4)]
    await asyncio.sleep(0.08)
    release.set()
    await asyncio.gather(*tasks)
    assert active["max"] <= 3
    assert all(by_tenant_max.get(tenant, 0) <= 1 for tenant in tenants)
    assert all(finished.get(tenant) == 4 for tenant in tenants)


@pytest.mark.load
@pytest.mark.asyncio
async def test_pool_no_worker_leak_under_churn(monkeypatch):
    spawn_count = {"value": 0}
    behaviors = [
        ["ok", "timeout"],
        ["crash"],
        ["ok", "ok"],
        ["timeout"],
        ["ok", "crash"],
        ["ok", "ok"],
    ]

    async def _spawn(*_args, **_kwargs):
        index = spawn_count["value"]
        spawn_count["value"] += 1
        return _FakeServeProcess(behaviors=behaviors[index % len(behaviors)])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    pool = WorkerPool(
        Settings(sandbox_pool_size=2, sandbox_pool_warmup_timeout_ms=1000), python_bin="python"
    )
    await pool.start()
    for _ in range(20):
        try:
            await pool.submit(job_dir=Path("/tmp/x"), limits={}, timeout_ms=30)
        except SandboxError:
            pass
        await asyncio.sleep(0.005)
    for _ in range(100):
        if len(pool._all) == 2 and pool._free.qsize() == 2 and not pool._bg_tasks:
            break
        await asyncio.sleep(0.01)
    assert len(pool._all) == 2
    assert pool._free.qsize() == 2
    assert not pool._bg_tasks
    await pool.shutdown()


@pytest.mark.load
@pytest.mark.asyncio
async def test_pool_acquire_timeout_fails_fast_under_saturation(monkeypatch):
    gate = asyncio.Event()
    first = {"spawned": False}

    async def _spawn(*_args, **_kwargs):
        if not first["spawned"]:
            first["spawned"] = True
            return _FakeServeProcess(behaviors=["gate"], gate_event=gate)
        return _FakeServeProcess(behaviors=["ok"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    pool = WorkerPool(
        Settings(
            sandbox_pool_size=1,
            sandbox_pool_warmup_timeout_ms=1000,
            sandbox_pool_acquire_timeout_ms=25,
        ),
        python_bin="python",
    )
    await pool.start()
    blocker = asyncio.create_task(
        pool.submit(job_dir=Path("/tmp/hold"), limits={}, timeout_ms=2000)
    )
    await asyncio.sleep(0.05)

    started = time.perf_counter()
    burst = [pool.submit(job_dir=Path("/tmp/wait"), limits={}, timeout_ms=2000) for _ in range(6)]
    results = await asyncio.gather(*burst, return_exceptions=True)
    elapsed = time.perf_counter() - started
    assert all(isinstance(item, SandboxError) for item in results)
    assert all(
        "acquire timeout" in str(item).lower() for item in results if isinstance(item, Exception)
    )
    assert elapsed < 0.5
    gate.set()
    await blocker
    await pool.shutdown()


@pytest.mark.load
@pytest.mark.asyncio
async def test_worker_recycle_after_max_jobs_under_load(monkeypatch):
    spawned: list[_FakeServeProcess] = []

    async def _spawn(*_args, **_kwargs):
        proc = _FakeServeProcess(behaviors=["ok", "ok", "ok"])
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    pool = WorkerPool(
        Settings(
            sandbox_pool_size=1, sandbox_worker_max_jobs=3, sandbox_pool_warmup_timeout_ms=1000
        ),
        python_bin="python",
    )
    await pool.start()
    for index in range(9):
        frame = await pool.submit(job_dir=Path(f"/tmp/job-{index}"), limits={}, timeout_ms=1000)
        assert frame["ok"] is True
    # 9 jobs with recycle-every-3 on a size-1 pool => at least 3 workers spawned.
    assert len(spawned) >= 3
    for _ in range(50):
        if pool._free.qsize() == 1:
            break
        await asyncio.sleep(0.01)
    assert pool._free.qsize() == 1
    await pool.shutdown()
