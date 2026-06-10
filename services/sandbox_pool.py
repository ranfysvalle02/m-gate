from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from config.settings import Settings, get_settings
from services.metrics import observe_sandbox_pool_event, set_sandbox_pool_workers
from services.sandbox_errors import (
    SandboxError,
    SandboxProtocolError,
    SandboxTimeoutError,
    frame_budget_bytes,
)

# Extra time the parent waits past a job's wall timeout before declaring the
# worker hung and killing it. Lets a worker self-report a clean timeout frame
# (and stay reusable) when it is only slightly over deadline.
_READ_GRACE_SECONDS = 0.5

# Headroom over the worker's own frame budget for the result StreamReader so a
# within-budget frame is always read in a single line rather than tripping
# asyncio's default 64 KiB limit. Kept strictly larger than the worker budget
# (services.sandbox_worker._frame_budget_bytes) so the two stay consistent.
_FRAME_HEADROOM_BYTES = 64 * 1024


def _subprocess_env() -> dict[str, str]:
    # Workers need Python import paths/locale but never receive caller secrets.
    env: dict[str, str] = {}
    for key in ("PATH", "PYTHONPATH", "PYTHONHOME", "LC_ALL", "LANG"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


class _PooledWorker:
    __slots__ = ("process", "jobs", "alive")

    def __init__(self, process: Any) -> None:
        self.process = process
        self.jobs = 0
        self.alive = True


class WorkerPool:
    """A pool of prewarmed, long-lived CPython-on-WASI worker subprocesses.

    Workers are spawned once and kept resident so callers reuse an
    already-compiled runtime instead of paying the subprocess-spawn +
    module-compile cold start per call. Each job is still dispatched into a
    fresh wasm ``Store`` inside the worker, so isolation remains per-call.

    A worker that times out, crashes, or is cancelled mid-flight is killed and
    replaced rather than returned to the pool, so a poisoned worker can never
    serve a later caller.
    """

    def __init__(self, settings: Settings | None = None, *, python_bin: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.python_bin = python_bin or sys.executable
        self.size = max(0, self.settings.sandbox_pool_size)
        self.max_jobs = max(0, self.settings.sandbox_worker_max_jobs)
        self._free: asyncio.Queue[_PooledWorker] = asyncio.Queue()
        self._all: set[_PooledWorker] = set()
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False

    async def start(self) -> None:
        """Spawn and warm up to ``size`` workers. Idempotent."""
        async with self._lock:
            if self._started or self._closed:
                return
            self._started = True
            for _ in range(self.size):
                worker = await self._spawn_worker()
                if worker is not None:
                    self._all.add(worker)
                    self._free.put_nowait(worker)
            set_sandbox_pool_workers(len(self._all))

    async def submit(
        self,
        *,
        job_dir: Path,
        limits: dict[str, int],
        timeout_ms: int,
    ) -> dict[str, Any]:
        if self._closed:
            raise SandboxError("Sandbox pool is closed.")
        if not self._started:
            await self.start()

        acquire_timeout = self._acquire_timeout(timeout_ms)
        try:
            worker = await asyncio.wait_for(self._free.get(), timeout=acquire_timeout)
        except TimeoutError as exc:
            observe_sandbox_pool_event("acquire_timeout")
            raise SandboxError(
                "No warm sandbox worker became available before the acquire timeout."
            ) from exc

        try:
            frame = await self._run_on_worker(worker, job_dir, limits, timeout_ms)
        except BaseException:
            # Any failure (timeout, protocol breach, cancellation, or an
            # unexpected error) leaves the worker mid-job/poisoned. Always
            # discard it so it can never serve a later caller AND so the worker
            # is never leaked out of circulation without being replaced.
            self._discard(worker)
            raise
        else:
            observe_sandbox_pool_event("served")
            worker.jobs += 1
            if self.max_jobs and worker.jobs >= self.max_jobs:
                self._discard(worker)
            else:
                self._free.put_nowait(worker)
            return frame

    async def shutdown(self) -> None:
        self._closed = True
        workers = list(self._all)
        self._all.clear()
        # Drain the free queue so a concurrent acquire cannot resurrect a worker.
        while not self._free.empty():
            try:
                self._free.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - defensive
                break
        for worker in workers:
            await self._terminate(worker)
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()
        set_sandbox_pool_workers(0)

    def _command(self) -> list[str]:
        command = [
            self.python_bin,
            "-m",
            "services.sandbox_worker",
            "--serve",
            "--wasm",
            self.settings.sandbox_python_wasm_path,
        ]
        if self.settings.sandbox_module_cache_path:
            command += ["--module-cache", self.settings.sandbox_module_cache_path]
        return command

    def _stream_limit(self) -> int:
        """StreamReader buffer ceiling for a worker's result line.

        Sized to the worker's own frame budget plus headroom so a within-budget
        frame always fits in a single ``readline`` and the default 64 KiB limit
        never trips a legitimate near-cap result.
        """
        return frame_budget_bytes(self.settings.sandbox_max_output_bytes) + _FRAME_HEADROOM_BYTES

    async def _spawn_worker(self) -> _PooledWorker | None:
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_subprocess_env(),
                limit=self._stream_limit(),
            )
        except Exception:
            observe_sandbox_pool_event("spawn_failed")
            return None
        worker = _PooledWorker(process)
        if not await self._ping(worker):
            await self._terminate(worker)
            observe_sandbox_pool_event("spawn_failed")
            return None
        observe_sandbox_pool_event("spawned")
        return worker

    async def _ping(self, worker: _PooledWorker) -> bool:
        warmup = max(0.1, self.settings.sandbox_pool_warmup_timeout_ms / 1000)
        try:
            worker.process.stdin.write(b'{"ping": true}\n')
            await worker.process.stdin.drain()
            line = await asyncio.wait_for(worker.process.stdout.readline(), timeout=warmup)
        except Exception:
            return False
        if not line:
            return False
        try:
            frame = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return False
        return isinstance(frame, dict) and bool(frame.get("pong"))

    async def _run_on_worker(
        self,
        worker: _PooledWorker,
        job_dir: Path,
        limits: dict[str, int],
        timeout_ms: int,
    ) -> dict[str, Any]:
        request = json.dumps({"job_dir": str(job_dir), "limits": limits}) + "\n"
        try:
            worker.process.stdin.write(request.encode("utf-8"))
            await worker.process.stdin.drain()
        except Exception as exc:
            raise SandboxError("Sandbox worker stdin is closed.") from exc

        read_timeout = max(0.05, timeout_ms / 1000 + _READ_GRACE_SECONDS)
        try:
            line = await asyncio.wait_for(worker.process.stdout.readline(), timeout=read_timeout)
        except TimeoutError as exc:
            raise SandboxTimeoutError(
                "Code tool exceeded its deadline inside the warm sandbox worker."
            ) from exc
        except (ValueError, asyncio.LimitOverrunError) as exc:
            # The worker bounds its own frame, so an over-limit line means a
            # protocol breach; fail closed and let the caller recycle the worker.
            raise SandboxProtocolError(
                "Sandbox worker frame exceeded the read buffer limit."
            ) from exc
        if not line:
            raise SandboxError("Sandbox worker exited before returning a result.")
        try:
            frame = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise SandboxProtocolError("Sandbox worker returned a malformed frame.") from exc
        if not isinstance(frame, dict):
            raise SandboxProtocolError("Sandbox worker frame was not a JSON object.")
        return frame

    def _acquire_timeout(self, timeout_ms: int) -> float:
        configured = self.settings.sandbox_pool_acquire_timeout_ms
        if configured > 0:
            return configured / 1000
        return max(0.05, timeout_ms / 1000)

    def _discard(self, worker: _PooledWorker) -> None:
        """Kill a poisoned worker now (non-blocking) and refill in the background."""
        if not worker.alive:
            return
        worker.alive = False
        self._all.discard(worker)
        observe_sandbox_pool_event("recycled")
        try:
            worker.process.kill()
        except Exception:
            pass
        self._schedule(self._reap_and_refill(worker))

    def _schedule(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _reap_and_refill(self, worker: _PooledWorker) -> None:
        try:
            await worker.process.wait()
        except Exception:
            pass
        if self._closed:
            return
        async with self._lock:
            if self._closed or len(self._all) >= self.size:
                set_sandbox_pool_workers(len(self._all))
                return
            replacement = await self._spawn_worker()
            if replacement is None:
                return
            if self._closed:
                await self._terminate(replacement)
                return
            self._all.add(replacement)
            self._free.put_nowait(replacement)
            set_sandbox_pool_workers(len(self._all))

    async def _terminate(self, worker: _PooledWorker) -> None:
        worker.alive = False
        try:
            stdin = worker.process.stdin
            if stdin is not None and not stdin.is_closing():
                stdin.write(b'{"shutdown": true}\n')
                await stdin.drain()
        except Exception:
            pass
        try:
            await asyncio.wait_for(worker.process.wait(), timeout=2.0)
        except Exception:
            try:
                worker.process.kill()
            except Exception:
                pass
            try:
                await worker.process.wait()
            except Exception:
                pass
