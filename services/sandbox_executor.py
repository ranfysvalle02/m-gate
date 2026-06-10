from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from config.settings import Settings, get_settings
from services.sandbox_errors import (
    SandboxError,
    SandboxProtocolError,
    SandboxTimeoutError,
)

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
    secrets: dict[str, str]
    limits: SandboxLimits | None = None


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
    async def _global_guard(self) -> AsyncIterator[None]:
        """Bound total concurrent sandbox executions across all tenants.

        Acquired OUTSIDE the per-tenant semaphore so the global ceiling caps the
        aggregate host load (workers + temp dirs + pip), not just per-tenant
        fan-out. A no-op when ``sandbox_max_global_concurrency`` is 0.
        """
        if self._global_semaphore is None:
            yield
            return
        async with self._global_semaphore:
            yield

    async def run(self, request: ExecRequest) -> ExecResult:
        if self.settings.code_executor == "disabled":
            raise SandboxError("Code executor is disabled.")
        started = time.perf_counter()
        async with self._global_guard():
            semaphore = await self._tenant_semaphore(request.tenant_id)
            async with semaphore:
                limits = self._resolve_limits(request.limits)
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
                        env=self._subprocess_env(),
                    )
                    try:
                        stdout_raw, stderr_raw = await asyncio.wait_for(
                            process.communicate(),
                            timeout=limits.wall_timeout_ms / 1000,
                        )
                    except TimeoutError as exc:
                        process.kill()
                        await process.communicate()
                        raise SandboxTimeoutError(
                            f"Code tool '{request.server}/{request.tool}' timed out after "
                            f"{limits.wall_timeout_ms}ms."
                        ) from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        stdout = stdout_raw.decode("utf-8", errors="replace")
        stderr = stderr_raw.decode("utf-8", errors="replace")
        if len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) > limits.max_output_bytes:
            raise SandboxProtocolError("Sandbox output exceeded the configured output-size limit.")
        frame = self._decode_worker_frame(stdout)
        if process.returncode != 0 and frame is None:
            message = stderr.strip() or stdout.strip() or f"worker exit code {process.returncode}"
            raise SandboxError(f"Sandbox worker failed: {message}")
        if frame is None:
            raise SandboxProtocolError("Sandbox worker did not emit a JSON result frame.")
        if not frame.get("ok"):
            raise self._frame_error(frame)

        result_value = frame.get("result")
        payload = result_value if isinstance(result_value, dict) else {"data": result_value}
        return ExecResult(
            payload=payload,
            stdout=str(frame.get("stdout") or ""),
            stderr=str(frame.get("stderr") or ""),
            elapsed_ms=elapsed_ms,
        )

    async def _tenant_semaphore(self, tenant_id: str) -> asyncio.Semaphore:
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
            "secrets": request.secrets,
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
        async with self._global_guard():
            semaphore = await self._tenant_semaphore(request.tenant_id)
            async with semaphore:
                limits = self._resolve_limits(request.limits)
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
                        timeout_ms=limits.wall_timeout_ms,
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
