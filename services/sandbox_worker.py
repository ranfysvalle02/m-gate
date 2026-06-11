from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from wasmtime import Config, Engine, Linker, Module, Store, WasiConfig

from services.sandbox_errors import frame_budget_bytes


def _bootstrap_program() -> str:
    return """
import json
import sys
import traceback

job_path = sys.argv[1]
result_path = sys.argv[2]

def write_frame(frame):
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(frame, handle)

try:
    with open(job_path, "r", encoding="utf-8") as handle:
        job = json.load(handle)
    namespace = {}
    exec(job.get("raw_code", ""), namespace, namespace)
    function_name = job.get("tool")
    func = namespace.get(function_name)
    if not callable(func):
        raise RuntimeError(f"Function '{function_name}' is not callable.")

    arguments = dict(job.get("arguments") or {})
    secrets = dict(job.get("secrets") or {})
    if "secrets" in getattr(func, "__code__", object()).co_varnames and "secrets" not in arguments:
        arguments["secrets"] = secrets
    else:
        namespace["SECRETS"] = secrets

    extra_paths = [p for p in (job.get("python_paths") or []) if isinstance(p, str) and p]
    if extra_paths:
        sys.path = extra_paths + sys.path

    result = func(**arguments)
    json.dumps(result)
    write_frame({"ok": True, "result": result})
except Exception as exc:  # noqa: BLE001 - sandbox seam reports structured error
    write_frame(
        {
            "ok": False,
            "error": {
                "type": "execution_error",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        }
    )
"""


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_read(path: Path, limit: int = 256 * 1024) -> str:
    """Read at most ``limit`` chars of a guest output file.

    Bounds worker memory regardless of how large the guest made the file: the
    parent only ever has to buffer a frame derived from these reads.
    """
    if not path.exists():
        return ""
    cap = max(0, int(limit))
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            data = handle.read(cap + 1)
    except OSError:
        return ""
    if len(data) > cap:
        return data[:cap]
    return data


def _bounded_frame(frame: dict[str, Any], max_output_bytes: int) -> dict[str, Any]:
    """Guarantee the emitted JSON line stays within a hard size budget.

    The result the guest returns is caller-controlled and otherwise unbounded,
    so a single frame could force a parent reader to buffer an arbitrarily large
    line. If the serialized frame exceeds the budget we replace it with a small,
    fail-closed ``output_limit`` error instead of emitting the oversized line.
    """
    budget = frame_budget_bytes(max_output_bytes)
    try:
        serialized = json.dumps(frame)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": {
                "type": "protocol_error",
                "message": "Sandbox result was not JSON-serializable.",
            },
            "stdout": "",
            "stderr": "",
        }
    if len(serialized.encode("utf-8")) <= budget:
        return frame
    return {
        "ok": False,
        "error": {
            "type": "output_limit",
            "message": "Sandbox result exceeded the configured output-size limit.",
        },
        "stdout": "",
        "stderr": "",
    }


def _apply_serve_rlimits(*, max_output_bytes: int) -> None:
    """Apply non-cumulative POSIX ceilings to a long-lived warm worker.

    Cumulative limits (RLIMIT_CPU, RLIMIT_AS) are intentionally omitted: CPU is
    additive across jobs and would eventually kill a healthy resident worker,
    and a tight address-space cap can break wasmtime's large virtual-memory
    reservation. Per-job CPU/memory stay bounded by wasm fuel + epoch
    interruption + the parent's wall-clock kill and ``store.set_limits``.

    RLIMIT_FSIZE bounds guest-written output on disk; SIGXFSZ is ignored so an
    over-limit write fails the job with EFBIG instead of killing the worker.
    RLIMIT_NOFILE caps descriptor growth.
    """
    if os.name != "posix":
        return
    import resource
    import signal

    try:
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    except (OSError, ValueError):
        pass
    # Backstop against a runaway guest filling host disk -- NOT precise output
    # enforcement (done by _safe_read truncation + _bounded_frame). The guest
    # writes the full result to /job/sandbox.result.json, which serializes
    # LARGER than max_output_bytes once JSON framing/escaping is added, so the
    # ceiling must clear the frame budget or a legitimate near-limit result
    # would trip EFBIG before the graceful output_limit frame can be produced.
    fsize_cap = max(frame_budget_bytes(max_output_bytes), 64 * 1024)
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_cap, fsize_cap))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    except (OSError, ValueError):
        # Platform/container policy may reject a control; rely on the in-wasm
        # and parent-side limits that always apply.
        return


def _apply_posix_rlimits(*, wall_timeout_ms: int, memory_bytes: int, max_output_bytes: int) -> None:
    if os.name != "posix":
        return
    import resource

    # RLIMIT_CPU is a coarse process-level backstop. Keep it intentionally
    # looser than the per-call wall timeout so wasmtime/module bootstrap and
    # JSON framing don't get SIGXCPU-killed before parent-side timeout/fuel
    # enforcement can produce a protocol-safe frame.
    cpu_seconds = max(5, math.ceil(wall_timeout_ms / 1000) + 5)
    rss_cap = max(memory_bytes * 2, 64 * 1024 * 1024)
    # Clear the frame budget (the guest-written result.json exceeds the raw
    # output cap once JSON-framed) so a legitimate near-limit result is bounded
    # gracefully by _bounded_frame instead of being killed by EFBIG.
    fsize_cap = max(frame_budget_bytes(max_output_bytes), 64 * 1024)
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))
        resource.setrlimit(resource.RLIMIT_AS, (rss_cap, rss_cap))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_cap, fsize_cap))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    except Exception:
        # Platform/container limits may reject one or more controls; keep
        # execution alive and rely on wasmtime + parent timeout for enforcement.
        return


def _build_engine() -> Engine:
    config = Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    config.max_wasm_stack = 512 * 1024
    return Engine(config)


def _module_cache_file(wasm_file: Path, cache_path: str) -> Path:
    """Derive a cache filename keyed by wasm CONTENT + wasmtime version.

    A content hash (not size+mtime) makes the key deterministic across a docker
    build->run boundary: BuildKit can rewrite file timestamps, so an mtime-based
    key would make a precompiled artifact baked at image-build time MISS at
    startup (silently reintroducing the cold compile this cache exists to avoid).
    The content hash also invalidates correctly when ``python.wasm`` is swapped
    or the runtime is upgraded; any incompatible artifact that still slips
    through fails closed in ``_load_module`` (deserialize error -> clean
    recompile), so the key only needs to be stable, not perfectly versioned.
    """
    try:
        from importlib.metadata import version as _pkg_version

        runtime_version = _pkg_version("wasmtime")
    except Exception:  # pragma: no cover - metadata always present at runtime
        try:
            import wasmtime

            runtime_version = str(getattr(wasmtime, "__version__", "unknown"))
        except Exception:
            runtime_version = "unknown"
    content_digest = hashlib.sha256(wasm_file.read_bytes()).hexdigest()
    signature = f"{content_digest}:{runtime_version}"
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:32]
    return Path(cache_path) / f"python-{digest}.cwasm"


def _load_module(engine: Engine, wasm_file: Path, cache_path: str | None = None) -> Module:
    """Compile python.wasm, deserializing a cached artifact when available.

    Compiling CPython-on-WASI is the dominant cold-start cost; reusing a
    serialized module lets a respawned worker skip it. Any cache error falls
    back to a clean compile so a corrupt/incompatible cache can never wedge a
    worker.
    """
    cache_file: Path | None = None
    if cache_path:
        try:
            cache_file = _module_cache_file(wasm_file, cache_path)
            if cache_file.exists():
                return Module.deserialize_file(engine, str(cache_file))
        except Exception:
            cache_file = cache_file if cache_path else None
    module = Module.from_file(engine, str(wasm_file))
    if cache_file is not None:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".cwasm.tmp")
            tmp.write_bytes(module.serialize())
            tmp.replace(cache_file)
        except Exception:
            pass
    return module


def _run_wasm(
    job_file: Path,
    wasm_file: Path,
    limits: dict[str, Any],
    *,
    engine: Engine | None = None,
    module: Module | None = None,
) -> dict[str, Any]:
    job_dir = job_file.parent
    stdout_file = job_dir / "sandbox.stdout.log"
    stderr_file = job_dir / "sandbox.stderr.log"
    result_file = job_dir / "sandbox.result.json"

    if engine is None:
        engine = _build_engine()
    linker = Linker(engine)
    linker.define_wasi()

    store = Store(engine)
    store.set_fuel(int(limits["fuel"]))
    store.set_limits(memory_size=int(limits["memory_bytes"]))
    store.set_epoch_deadline(1)

    wall_timeout = max(1, int(limits["wall_timeout_ms"]))
    timer = threading.Timer(wall_timeout / 1000, engine.increment_epoch)
    timer.daemon = True
    timer.start()

    wasi = WasiConfig()
    wasi.argv = [
        "python",
        "-c",
        _bootstrap_program(),
        "/job/job.json",
        "/job/sandbox.result.json",
    ]
    wasi.stdout_file = str(stdout_file)
    wasi.stderr_file = str(stderr_file)
    wasi.preopen_dir(str(job_dir), "/job")
    store.set_wasi(wasi)

    frame: dict[str, Any] | None = None
    try:
        if module is None:
            module = Module.from_file(engine, str(wasm_file))
        instance = linker.instantiate(store, module)
        start = instance.exports(store)["_start"]
        if not callable(start):
            raise RuntimeError("WASM export '_start' is not a function.")
        start(store)
        if result_file.exists():
            parsed = _read_json(result_file)
            if isinstance(parsed, dict):
                frame = parsed
    except Exception as exc:  # noqa: BLE001 - map trap kinds to protocol-safe frame
        message = str(exc)
        error_type = "execution_error"
        if "fuel" in message.lower():
            error_type = "fuel_exhausted"
        elif "epoch" in message.lower() or "interrupt" in message.lower():
            error_type = "timeout"
        elif "memory" in message.lower():
            error_type = "memory_limit"
        frame = {
            "ok": False,
            "error": {
                "type": error_type,
                "message": message,
            },
        }
    finally:
        timer.cancel()

    if frame is None:
        frame = {
            "ok": False,
            "error": {
                "type": "protocol_error",
                "message": "Sandbox produced no result frame.",
            },
        }
    max_output_bytes = int(limits.get("max_output_bytes", 256 * 1024))
    frame["stdout"] = _safe_read(stdout_file, max_output_bytes)
    frame["stderr"] = _safe_read(stderr_file, max_output_bytes)
    return _bounded_frame(frame, max_output_bytes)


def _normalize_limits(limits: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(limits or {})
    normalized.setdefault("fuel", 40_000_000)
    normalized.setdefault("memory_bytes", 256 * 1024 * 1024)
    normalized.setdefault("wall_timeout_ms", 2_000)
    normalized.setdefault("max_output_bytes", 256 * 1024)
    return normalized


def _emit(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame))
    sys.stdout.write("\n")
    sys.stdout.flush()


def serve(wasm_file: Path, module_cache: str | None) -> int:
    """Run as a long-lived warm worker, one job per stdin line.

    stdout is reserved for newline-delimited JSON result frames; the engine and
    compiled module are built once and reused, while each job still executes in
    a fresh wasm Store, preserving per-call isolation. Cumulative POSIX rlimits
    (CPU/address-space) are intentionally NOT applied here -- they would
    eventually kill a healthy long-lived worker -- so wasmtime fuel + epoch
    interruption plus the parent's wall-clock kill bound each job's CPU/memory.
    Non-cumulative ceilings (output file size, descriptors) ARE applied via
    ``_apply_serve_rlimits`` so a single job still cannot exhaust host disk/fds.
    """
    engine = _build_engine()
    try:
        module = _load_module(engine, wasm_file, module_cache)
    except Exception as exc:  # noqa: BLE001 - report warmup failure and exit
        sys.stderr.write(f"sandbox worker warmup failed: {exc}\n")
        sys.stderr.flush()
        return 1
    # Apply after warmup so the (potentially large) module-cache write is not
    # blocked by RLIMIT_FSIZE; default to the platform output cap if unset.
    _apply_serve_rlimits(max_output_bytes=_normalize_limits({})["max_output_bytes"])

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _emit({"ok": False, "error": {"type": "protocol_error", "message": "Malformed job."}})
            continue
        if not isinstance(request, dict):
            _emit({"ok": False, "error": {"type": "protocol_error", "message": "Malformed job."}})
            continue
        if request.get("shutdown"):
            break
        if request.get("ping"):
            _emit({"ok": True, "pong": True})
            continue
        job_dir = request.get("job_dir")
        if not job_dir:
            _emit({"ok": False, "error": {"type": "protocol_error", "message": "Missing job_dir."}})
            continue
        job_file = Path(job_dir) / "job.json"
        limits = _normalize_limits(request.get("limits") or {})
        started = time.perf_counter()
        try:
            frame = _run_wasm(job_file, wasm_file, limits, engine=engine, module=module)
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
            frame = {
                "ok": False,
                "error": {"type": "execution_error", "message": str(exc)},
                "stdout": "",
                "stderr": "",
            }
        frame["worker_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        _emit(frame)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run code-tool invocations in a WASI sandbox.")
    parser.add_argument("--job")
    parser.add_argument("--wasm", required=True)
    parser.add_argument("--serve", action="store_true", help="Run as a long-lived warm worker.")
    parser.add_argument("--module-cache", default=None, help="Compiled-module cache directory.")
    args = parser.parse_args()

    wasm_file = Path(args.wasm).resolve()
    if not wasm_file.exists():
        _emit(
            {"ok": False, "error": {"type": "execution_error", "message": "python.wasm not found."}}
        )
        return 1

    if args.serve:
        return serve(wasm_file, args.module_cache)

    if not args.job:
        _emit({"ok": False, "error": {"type": "protocol_error", "message": "Missing --job."}})
        return 1
    job_file = Path(args.job).resolve()
    if not job_file.exists():
        _emit({"ok": False, "error": {"type": "protocol_error", "message": "Job file not found."}})
        return 1

    job = _read_json(job_file)
    limits = _normalize_limits(job.get("limits") or {})
    _apply_posix_rlimits(
        wall_timeout_ms=int(limits["wall_timeout_ms"]),
        memory_bytes=int(limits["memory_bytes"]),
        max_output_bytes=int(limits["max_output_bytes"]),
    )

    started = time.perf_counter()
    frame = _run_wasm(job_file, wasm_file, limits)
    frame["worker_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    _emit(frame)
    return 0 if frame.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
