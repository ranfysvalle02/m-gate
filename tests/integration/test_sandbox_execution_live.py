from __future__ import annotations

import functools
import subprocess
import sys
from pathlib import Path

import pytest

from services.sandbox_executor import (
    ExecRequest,
    SandboxError,
    SandboxLimits,
    SandboxTimeoutError,
    WasmExecutor,
)

pytestmark = pytest.mark.integration

# Spawn workers on the test runner's OWN interpreter. It is the one guaranteed to
# have wasmtime installed and to be able to import the ``services`` package; a
# bare ``python`` on PATH (e.g. a pyenv shim) frequently resolves to a DIFFERENT
# interpreter without wasmtime, so the worker would die on ``import wasmtime`` and
# surface as a confusing "worker exited before returning a result" instead of an
# honest skip. This also matches production, where the executor defaults to
# ``sys.executable``. Do not hardcode ``"python"`` here.
WORKER_PYTHON = sys.executable


@functools.cache
def _worker_has_wasmtime(python_bin: str) -> bool:
    """Probe the WORKER interpreter (not just the test runner) for wasmtime.

    The two can differ, and only the worker actually imports wasmtime, so this is
    the interpreter whose capability decides whether the tier can run.
    """
    try:
        probe = subprocess.run(
            [
                python_bin,
                "-c",
                "import importlib.util as u, sys; sys.exit(0 if u.find_spec('wasmtime') else 1)",
            ],
            capture_output=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - any spawn/probe failure means "unavailable"
        return False
    return probe.returncode == 0


def _skip_if_wasm_runtime_unavailable(settings) -> None:
    """Skip when the wasm runtime is not usable on this host.

    The worker is spawned as a subprocess that imports ``wasmtime`` and loads the
    pinned ``python.wasm``. If either is missing the worker dies on startup, so
    the test would otherwise fail (or, for ``pytest.raises(SandboxError)`` cases,
    *falsely pass* by catching the wrong error) instead of skipping honestly.
    """
    if not Path(settings.sandbox_python_wasm_path).exists():
        pytest.skip("python.wasm is missing; run `make fetch-wasm` first.")
    if not _worker_has_wasmtime(WORKER_PYTHON):
        pytest.skip(
            f"wasmtime is not installed for the worker interpreter ({WORKER_PYTHON}); "
            "the sandbox runtime is unavailable."
        )


def _request(
    *,
    code: str,
    tool: str = "run",
    arguments: dict | None = None,
    env: dict | None = None,
    limits: SandboxLimits | None = None,
) -> ExecRequest:
    return ExecRequest(
        tenant_id="local-dev",
        server="sandbox-tests",
        tool=tool,
        raw_code=code,
        requirements=[],
        arguments=arguments or {},
        env=env or {},
        limits=limits,
    )


@pytest.fixture
def sandbox_executor(settings):
    _skip_if_wasm_runtime_unavailable(settings)
    return WasmExecutor(settings=settings, python_bin=WORKER_PYTHON)


@pytest.mark.asyncio
async def test_wasm_happy_path_returns_jsonable_result(sandbox_executor):
    req = _request(
        code="def run(a: int, b: int):\n    return {'sum': a + b}\n", arguments={"a": 2, "b": 5}
    )
    result = await sandbox_executor.run(req)
    assert result.payload == {"sum": 7}


@pytest.mark.asyncio
async def test_wasm_exposes_server_env_in_context(sandbox_executor):
    req = _request(
        code="def run():\n    return {'token': context.env.get('API_KEY', '')}\n",
        env={"API_KEY": "secret-123"},
    )
    result = await sandbox_executor.run(req)
    assert result.payload == {"token": "secret-123"}


@pytest.mark.asyncio
async def test_wasm_contains_network_exfil_attempt(sandbox_executor):
    req = _request(
        code=(
            "def run():\n"
            "    import socket\n"
            "    sock = socket.socket()\n"
            "    sock.connect(('example.com', 80))\n"
            "    return {'ok': True}\n"
        )
    )
    with pytest.raises(SandboxError):
        await sandbox_executor.run(req)


@pytest.mark.asyncio
async def test_wasm_times_out_cpu_bomb(sandbox_executor):
    req = _request(
        code="def run():\n    while True:\n        pass\n",
        limits=SandboxLimits(
            fuel=1_000_000,
            memory_bytes=64 * 1024 * 1024,
            wall_timeout_ms=300,
            max_output_bytes=128 * 1024,
        ),
    )
    with pytest.raises(SandboxTimeoutError):
        await sandbox_executor.run(req)


@pytest.mark.asyncio
async def test_wasm_contains_memory_bomb(sandbox_executor):
    req = _request(
        code=(
            "def run():\n    data = []\n    while True:\n        data.append('x' * 1024 * 1024)\n"
        ),
        limits=SandboxLimits(
            fuel=40_000_000,
            memory_bytes=32 * 1024 * 1024,
            wall_timeout_ms=800,
            max_output_bytes=128 * 1024,
        ),
    )
    with pytest.raises(SandboxError):
        await sandbox_executor.run(req)


@pytest.mark.asyncio
async def test_wasm_blocks_db_reach_attempt(sandbox_executor):
    req = _request(
        code=(
            "def run():\n"
            "    import socket\n"
            "    sock = socket.socket()\n"
            "    sock.settimeout(0.5)\n"
            "    sock.connect(('127.0.0.1', 27017))\n"
            "    return {'connected': True}\n"
        )
    )
    with pytest.raises(SandboxError):
        await sandbox_executor.run(req)


@pytest.mark.asyncio
async def test_wasm_cannot_reach_host_env_or_fs(sandbox_executor):
    req = _request(
        code=(
            "def run():\n"
            "    import os\n"
            "    p = os.environ.get('PATH')\n"
            "    with open('/tmp/escape.txt', 'w', encoding='utf-8') as h:\n"
            "        h.write('owned')\n"
            "    return {'path': p}\n"
        )
    )
    with pytest.raises(SandboxError):
        await sandbox_executor.run(req)


@pytest.mark.asyncio
async def test_wasm_bounds_oversized_result(sandbox_executor):
    # A function returning far more than the output cap must fail closed, not
    # stream an unbounded frame back to the parent.
    req = _request(
        code="def run():\n    return {'blob': 'x' * 2_000_000}\n",
        limits=SandboxLimits(
            fuel=40_000_000,
            memory_bytes=128 * 1024 * 1024,
            wall_timeout_ms=2_000,
            max_output_bytes=4096,
        ),
    )
    with pytest.raises(SandboxError):
        await sandbox_executor.run(req)


@pytest.mark.asyncio
async def test_pooled_worker_survives_output_bomb_then_serves(settings):
    _skip_if_wasm_runtime_unavailable(settings)
    from services.sandbox_executor import PooledWasmExecutor

    pooled_settings = settings.model_copy(update={"sandbox_pool_size": 1})
    executor = PooledWasmExecutor(settings=pooled_settings, python_bin=WORKER_PYTHON)
    await executor.prewarm()
    try:
        bomb = _request(
            code="def run():\n    return {'blob': 'x' * 2_000_000}\n",
            limits=SandboxLimits(40_000_000, 128 * 1024 * 1024, 2_000, 4096),
        )
        with pytest.raises(SandboxError):
            await executor.run(bomb)
        # The bound is graceful: the resident worker stays healthy and serves the
        # next job rather than being poisoned by the oversized result.
        ok = _request(code="def run():\n    return {'ok': True}\n")
        result = await executor.run(ok)
        assert result.payload == {"ok": True}
    finally:
        await executor.aclose()
