from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from config.settings import Settings, get_settings
from services.sandbox_db_bridge import SandboxDbBridge
from services.sandbox_errors import (
    BRIDGE_RPC_FRAME_TYPES,
    SandboxError,
    SandboxProtocolError,
    SandboxTimeoutError,
)
from services.sandbox_tool_bridge import SandboxToolBridge, ToolInvoker

__all__ = [
    "ExecRequest",
    "ExecResult",
    "Executor",
    "PooledWasmExecutor",
    "SandboxError",
    "SandboxLimits",
    "SandboxProtocolError",
    "SandboxTimeoutError",
    "WasmExecutor",
    "get_executor",
    "prewarm_executor",
    "shutdown_executor",
]


@dataclass(frozen=True)
class SandboxLimits:
    fuel: int
    memory_bytes: int
    wall_timeout_ms: int
    max_output_bytes: int


@dataclass(frozen=True)
class ExecRequest:
    tenant_id: str
    server: str
    tool: str
    raw_code: str
    requirements: list[str]
    arguments: dict[str, Any]
    env: dict[str, str]
    action_type: str = "read"
    limits: SandboxLimits | None = None
    # Host-side callback the cross-tool bridge uses to run a sibling tool. When
    # None (the default), ``context.tools`` is disabled for this run. Never
    # serialized into the worker job; it stays in-process on the host.
    tool_invoker: ToolInvoker | None = None
    # Nesting depth of this run within a cross-tool call chain. 0 is a direct
    # caller-initiated invocation; >0 is a sibling call relayed by the bridge.
    # Nested runs skip the concurrency guards the originating run already holds
    # (the originating run accounts for the slot), so a re-entrant call cannot
    # deadlock against the per-tenant/global semaphore.
    call_depth: int = 0


@dataclass(frozen=True)
class ExecResult:
    payload: dict[str, Any]
    stdout: str
    stderr: str
    elapsed_ms: int


class Executor(Protocol):
    async def run(self, request: ExecRequest) -> ExecResult:
        """Execute one code-backed tool call inside an isolated runtime."""


class WasmExecutor:
    """Execute code tools via a throwaway WebAssembly worker subprocess."""

    def __init__(self, settings: Settings | None = None, *, python_bin: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.python_bin = python_bin or sys.executable
        self._tenant_limits: dict[str, asyncio.Semaphore] = {}
        self._tenant_limits_lock = asyncio.Lock()
        global_limit = max(0, self.settings.sandbox_max_global_concurrency)
        self._global_semaphore = asyncio.Semaphore(global_limit) if global_limit > 0 else None

    @contextlib.asynccontextmanager
    async def _global_guard(self, request: ExecRequest | None = None) -> AsyncIterator[None]:
        """Bound total concurrent sandbox executions across all tenants.

        Acquired OUTSIDE the per-tenant semaphore so the global ceiling caps the
        aggregate host load (workers + temp dirs + pip), not just per-tenant
        fan-out. A no-op when ``sandbox_max_global_concurrency`` is 0, and a
        no-op for a nested (re-entrant) sibling call -- the originating run
        already holds the slot, so re-acquiring here would deadlock at limit 1.
        """
        nested = request is not None and request.call_depth > 0
        if nested or self._global_semaphore is None:
            yield
            return
        async with self._global_semaphore:
            yield

    async def run(self, request: ExecRequest) -> ExecResult:
        if self.settings.code_executor == "disabled":
            raise SandboxError("Code executor is disabled.")
        started = time.perf_counter()
        process = None
        stderr_task: asyncio.Task[bytes] | None = None
        frame: dict[str, Any] | None = None
        stderr = ""
        limits = self._resolve_limits(request.limits)
        worker_timeout_ms = self._worker_timeout_ms(
            wall_timeout_ms=limits.wall_timeout_ms,
            db_bridge_enabled=self._db_bridge_enabled(),
            tool_bridge_enabled=self._tool_bridge_enabled(request),
        )
        dispatch = self._bridge_dispatcher(request)
        async with self._global_guard(request):
            semaphore = await self._tenant_semaphore(
                request.tenant_id, nested=request.call_depth > 0
            )
            async with semaphore:
                with tempfile.TemporaryDirectory(prefix="mcp-sbx-") as temp_dir:
                    temp_path = Path(temp_dir)
                    job_path = temp_path / "job.json"
                    python_paths = await self._stage_requirements(
                        requirements=request.requirements,
                        workspace=temp_path,
                        timeout_ms=limits.wall_timeout_ms,
                    )
                    job_path.write_text(
                        json.dumps(self._job_payload(request, python_paths, limits)),
                        encoding="utf-8",
                    )
                    command = [
                        self.python_bin,
                        "-m",
                        "services.sandbox_worker",
                        "--job",
                        str(job_path),
                        "--wasm",
                        self.settings.sandbox_python_wasm_path,
                    ]
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        stdin=asyncio.subprocess.PIPE,
                        env=self._subprocess_env(),
                    )
                    if process.stdout is None or process.stdin is None:
                        raise SandboxError("Sandbox worker did not expose stdio pipes.")
                    stderr_task = asyncio.create_task(
                        process.stderr.read() if process.stderr is not None else asyncio.sleep(0, result=b"")
                    )
                    try:
                        frame = await self._pump_worker_frames(
                            reader=process.stdout,
                            writer=process.stdin,
                            timeout_ms=worker_timeout_ms,
                            dispatch=dispatch,
                        )
                    except TimeoutError as exc:
                        process.kill()
                        if stderr_task is not None:
                            with contextlib.suppress(Exception):
                                await stderr_task
                        with contextlib.suppress(Exception):
                            await process.wait()
                        raise SandboxTimeoutError(
                            f"Code tool '{request.server}/{request.tool}' timed out after "
                            f"{worker_timeout_ms}ms."
                        ) from exc
                    finally:
                        if process.stdin is not None and not process.stdin.is_closing():
                            with contextlib.suppress(Exception):
                                process.stdin.write(b'{"shutdown": true}\n')
                                await process.stdin.drain()
                        with contextlib.suppress(Exception):
                            await process.wait()
                    stderr_raw = b""
                    if stderr_task is not None:
                        stderr_raw = await stderr_task

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        stderr = stderr_raw.decode("utf-8", errors="replace")
        if frame is None:
            message = stderr.strip()
            if process is not None:
                message = message or f"worker exit code {process.returncode}"
            raise SandboxError(f"Sandbox worker failed: {message}")
        return self._result_from_frame(frame, limits=limits, elapsed_ms=elapsed_ms)

    def _db_bridge_enabled(self) -> bool:
        return bool(self.settings.sandbox_db_bridge_enabled)

    def _tool_bridge_enabled(self, request: ExecRequest) -> bool:
        # The cross-tool bridge only activates when the operator enables it AND
        # the caller supplied an invoker (the host-side, authorized re-entry
        # point). A run without an invoker can never reach a sibling tool.
        return bool(self.settings.sandbox_tool_bridge_enabled) and request.tool_invoker is not None

    def _worker_timeout_ms(
        self,
        *,
        wall_timeout_ms: int,
        db_bridge_enabled: bool,
        tool_bridge_enabled: bool = False,
    ) -> int:
        timeout_ms = max(1, int(wall_timeout_ms))
        if db_bridge_enabled:
            max_calls = max(0, int(self.settings.sandbox_db_max_calls_per_invocation))
            per_call = max(0, int(self.settings.sandbox_db_query_timeout_ms))
            # Allow host-side DB RPC latency without treating it as guest compute time.
            timeout_ms += max_calls * per_call
        if tool_bridge_enabled:
            # A sibling tool runs in its own sandbox; give the blocked parent
            # guest headroom for that nested latency. The real ceiling on the
            # whole chain stays the caller's asyncio.wait_for over the run.
            timeout_ms += max(1, int(wall_timeout_ms))
        return timeout_ms

    def _bridge_dispatcher(
        self, request: ExecRequest
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None:
        """Build the single host-side handler the worker pump relays frames to.

        Routes each mid-execution frame to the matching bridge by ``type`` so a
        DB RPC and a cross-tool RPC share one channel. Returns None when neither
        bridge is active for this run (the pump then treats any bridge frame as
        a protocol breach).
        """
        db_enabled = self._db_bridge_enabled()
        tool_enabled = self._tool_bridge_enabled(request)
        if not db_enabled and not tool_enabled:
            return None

        db_bridge = (
            SandboxDbBridge(
                tenant_id=request.tenant_id,
                action_type=request.action_type,
                settings=self.settings,
            )
            if db_enabled
            else None
        )
        tool_bridge = (
            SandboxToolBridge(
                tenant_id=request.tenant_id,
                invoker=request.tool_invoker,
                settings=self.settings,
            )
            if tool_enabled and request.tool_invoker is not None
            else None
        )

        async def _dispatch(frame: dict[str, Any]) -> dict[str, Any]:
            if frame.get("type") == "tool_rpc":
                if tool_bridge is None:
                    return {
                        "type": "tool_rpc_result",
                        "id": frame.get("id"),
                        "ok": False,
                        "error": {
                            "type": "tool_rpc_error",
                            "message": "Cross-tool bridge is disabled for this run.",
                        },
                    }
                return await tool_bridge.handle(frame)
            if db_bridge is None:
                return {
                    "type": "db_rpc_result",
                    "id": frame.get("id"),
                    "ok": False,
                    "error": {
                        "type": "db_rpc_error",
                        "message": "DB bridge is disabled for this run.",
                    },
                }
            return await db_bridge.handle(frame)

        return _dispatch

    async def _pump_worker_frames(
        self,
        *,
        reader,
        writer,
        timeout_ms: int,
        dispatch: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + max(0.05, timeout_ms / 1000)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Sandbox worker timed out before returning a result frame.")
            line = await asyncio.wait_for(reader.readline(), timeout=remaining)
            if not line:
                raise SandboxError("Sandbox worker exited before returning a result.")
            try:
                frame = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                raise SandboxProtocolError("Sandbox worker returned a malformed frame.") from exc
            if not isinstance(frame, dict):
                raise SandboxProtocolError("Sandbox worker frame was not a JSON object.")
            if frame.get("type") not in BRIDGE_RPC_FRAME_TYPES:
                return frame
            if dispatch is None:
                raise SandboxProtocolError(
                    "Sandbox worker requested a host bridge call when no bridge is enabled."
                )
            response = await dispatch(frame)
            writer.write((json.dumps(response) + "\n").encode("utf-8"))
            await writer.drain()

    async def _tenant_semaphore(
        self, tenant_id: str, *, nested: bool = False
    ) -> asyncio.Semaphore:
        if nested:
            # A re-entrant sibling call must not contend on (and stall behind)
            # the per-tenant slot the originating run already holds. A fresh,
            # uncontended semaphore keeps the `async with` shape without gating;
            # depth + per-invocation call budgets bound the real fan-out.
            return asyncio.Semaphore(1)
        async with self._tenant_limits_lock:
            semaphore = self._tenant_limits.get(tenant_id)
            if semaphore is None:
                semaphore = asyncio.Semaphore(
                    max(1, self.settings.sandbox_max_concurrency_per_tenant)
                )
                self._tenant_limits[tenant_id] = semaphore
            return semaphore

    def _resolve_limits(self, request_limits: SandboxLimits | None) -> SandboxLimits:
        if request_limits is not None:
            return request_limits
        return SandboxLimits(
            fuel=max(1, self.settings.sandbox_fuel),
            memory_bytes=max(1, self.settings.sandbox_memory_bytes),
            wall_timeout_ms=max(1, self.settings.sandbox_wall_timeout_ms),
            max_output_bytes=max(1, self.settings.sandbox_max_output_bytes),
        )

    @staticmethod
    def _limits_payload(limits: SandboxLimits) -> dict[str, int]:
        return {
            "fuel": limits.fuel,
            "memory_bytes": limits.memory_bytes,
            "wall_timeout_ms": limits.wall_timeout_ms,
            "max_output_bytes": limits.max_output_bytes,
        }

    def _job_payload(
        self,
        request: ExecRequest,
        python_paths: list[str],
        limits: SandboxLimits,
    ) -> dict[str, Any]:
        return {
            "tenant_id": request.tenant_id,
            "server": request.server,
            "tool": request.tool,
            "raw_code": request.raw_code,
            "requirements": request.requirements,
            "python_paths": python_paths,
            "arguments": request.arguments,
            "env": request.env,
            "action_type": request.action_type,
            "db_bridge": self._db_bridge_enabled(),
            "tool_bridge": self._tool_bridge_enabled(request),
            "db_rpc_wait_ms": self._worker_timeout_ms(
                wall_timeout_ms=limits.wall_timeout_ms,
                db_bridge_enabled=self._db_bridge_enabled(),
                tool_bridge_enabled=self._tool_bridge_enabled(request),
            ),
            "limits": self._limits_payload(limits),
        }

    def _result_from_frame(
        self,
        frame: dict[str, Any],
        *,
        limits: SandboxLimits,
        elapsed_ms: int,
    ) -> ExecResult:
        stdout = str(frame.get("stdout") or "")
        stderr = str(frame.get("stderr") or "")
        if len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) > limits.max_output_bytes:
            raise SandboxProtocolError("Sandbox output exceeded the configured output-size limit.")
        if not frame.get("ok"):
            raise self._frame_error(frame)
        result_value = frame.get("result")
        payload = result_value if isinstance(result_value, dict) else {"data": result_value}
        return ExecResult(payload=payload, stdout=stdout, stderr=stderr, elapsed_ms=elapsed_ms)

    @staticmethod
    def _dist_name(requirement: str) -> str:
        """Normalize a requirement to its PEP 503 distribution name.

        Strips the version pin and any extras, then lowercases and collapses
        ``-_.`` runs so allowlist matching is spelling-insensitive.
        """
        base = requirement.split("==", 1)[0].split("[", 1)[0].strip()
        return re.sub(r"[-_.]+", "-", base).lower()

    def _allowed_requirement_names(self) -> set[str]:
        raw = self.settings.sandbox_allowed_requirements or ""
        names: set[str] = set()
        for token in re.split(r"[,\s]+", raw):
            normalized = self._dist_name(token) if token.strip() else ""
            if normalized:
                names.add(normalized)
        return names

    async def _stage_requirements(
        self,
        *,
        requirements: list[str],
        workspace: Path,
        timeout_ms: int,
    ) -> list[str]:
        normalized = [req.strip() for req in requirements if isinstance(req, str) and req.strip()]
        if not normalized:
            return []
        # Deny-by-default: host pip runs OUTSIDE the wasm jail, so only operator-
        # allowlisted distributions may be installed. This is enforced here (the
        # security boundary), not just at authoring time.
        allowed = self._allowed_requirement_names()
        rejected = sorted({self._dist_name(req) for req in normalized} - allowed)
        if rejected:
            raise SandboxError(
                "Code-tool requirement(s) not permitted by the sandbox allowlist: "
                + ", ".join(rejected)
                + ". An operator must add them to SANDBOX_ALLOWED_REQUIREMENTS."
            )
        target = workspace / "site-packages"
        command = [
            self.python_bin,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-deps",
            # Wheels only: never run a source build (setup.py) on the host. Any
            # arbitrary code in an allowlisted wheel only runs later, in-sandbox.
            "--only-binary=:all:",
            "--target",
            str(target),
            *normalized,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(1.0, timeout_ms / 1000),
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise SandboxTimeoutError(
                "Installing tool requirements exceeded sandbox timeout."
            ) from exc
        if process.returncode != 0:
            error = stderr.decode("utf-8", errors="replace").strip()
            raise SandboxError(f"Failed to install tool requirements: {error}")
        return ["/job/site-packages"]

    @staticmethod
    def _subprocess_env() -> dict[str, str]:
        # Worker needs Python import paths/locale but never receives caller secrets.
        env: dict[str, str] = {}
        for key in ("PATH", "PYTHONPATH", "PYTHONHOME", "LC_ALL", "LANG"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    @staticmethod
    def _decode_worker_frame(stdout: str) -> dict[str, Any] | None:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            return None
        candidate = lines[-1]
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, dict):
            return None
        return decoded

    @staticmethod
    def _frame_error(frame: dict[str, Any]) -> SandboxError:
        raw_error = frame.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        kind = str(error.get("type") or "execution_error")
        message = str(error.get("message") or "Sandbox execution failed.")
        if kind in {"timeout", "fuel_exhausted", "epoch_timeout"}:
            return SandboxTimeoutError(message)
        if kind == "protocol_error":
            return SandboxProtocolError(message)
        return SandboxError(message)


class PooledWasmExecutor(WasmExecutor):
    """Execute code tools against a pool of prewarmed, long-lived WASI workers.

    Each call still stages requirements into a fresh temp dir, writes its own
    ``job.json``, and runs inside a brand-new wasm ``Store``/instance, so the
    per-call isolation guarantees are identical to :class:`WasmExecutor`. The
    only thing reused across calls is the resident worker process and its
    already-compiled CPython module, which is what removes the cold start.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        python_bin: str | None = None,
        pool: Any = None,
    ) -> None:
        super().__init__(settings, python_bin=python_bin)
        if pool is None:
            from services.sandbox_pool import WorkerPool

            pool = WorkerPool(self.settings, python_bin=self.python_bin)
        self.pool = pool

    async def prewarm(self) -> None:
        await self.pool.start()

    async def aclose(self) -> None:
        await self.pool.shutdown()

    async def run(self, request: ExecRequest) -> ExecResult:
        if self.settings.code_executor == "disabled":
            raise SandboxError("Code executor is disabled.")
        started = time.perf_counter()
        limits = self._resolve_limits(request.limits)
        worker_timeout_ms = self._worker_timeout_ms(
            wall_timeout_ms=limits.wall_timeout_ms,
            db_bridge_enabled=self._db_bridge_enabled(),
            tool_bridge_enabled=self._tool_bridge_enabled(request),
        )
        dispatch = self._bridge_dispatcher(request)
        async with self._global_guard(request):
            semaphore = await self._tenant_semaphore(
                request.tenant_id, nested=request.call_depth > 0
            )
            async with semaphore:
                with tempfile.TemporaryDirectory(prefix="mcp-sbx-") as temp_dir:
                    temp_path = Path(temp_dir)
                    job_path = temp_path / "job.json"
                    python_paths = await self._stage_requirements(
                        requirements=request.requirements,
                        workspace=temp_path,
                        timeout_ms=limits.wall_timeout_ms,
                    )
                    job_path.write_text(
                        json.dumps(self._job_payload(request, python_paths, limits)),
                        encoding="utf-8",
                    )
                    frame = await self.pool.submit(
                        job_dir=temp_path,
                        limits=self._limits_payload(limits),
                        timeout_ms=worker_timeout_ms,
                        dispatch=dispatch,
                    )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return self._result_from_frame(frame, limits=limits, elapsed_ms=elapsed_ms)


_executor: Executor | None = None


def get_executor() -> Executor:
    global _executor
    if _executor is None:
        settings = get_settings()
        if settings.sandbox_pool_size > 0 and settings.code_executor != "disabled":
            _executor = PooledWasmExecutor(settings)
        else:
            _executor = WasmExecutor(settings)
    return _executor


async def prewarm_executor() -> None:
    """Prewarm the active executor's worker pool, if it has one (no-op otherwise)."""
    prewarm = getattr(get_executor(), "prewarm", None)
    if prewarm is not None:
        await prewarm()


async def shutdown_executor() -> None:
    """Tear down the active executor's worker pool, if any, on app shutdown."""
    executor = _executor
    if executor is None:
        return
    aclose = getattr(executor, "aclose", None)
    if aclose is not None:
        await aclose()
