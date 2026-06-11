#!/usr/bin/env python3
"""Empirically benchmark the WASM sandbox cold/warm execution paths."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.settings import Settings  # noqa: E402
from services.sandbox_executor import (  # noqa: E402
    ExecRequest,
    PooledWasmExecutor,
    SandboxLimits,
    WasmExecutor,
)

DEFAULT_WASM = Path("vendor/python-3.12.0.wasm")
DEFAULT_LIMITS = SandboxLimits(
    fuel=4_000_000_000,
    memory_bytes=256 * 1024 * 1024,
    wall_timeout_ms=15_000,
    max_output_bytes=262_144,
)


def _ensure_wasm_exists(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.exists():
        return resolved
    raise FileNotFoundError(f"{resolved} not found. Run `make fetch-wasm` before benchmarking.")


def _build_request(*, limits: SandboxLimits) -> ExecRequest:
    return ExecRequest(
        tenant_id="bench-tenant",
        server="bench-sandbox",
        tool="run",
        raw_code="def run() -> dict[str, bool]:\n    return {'ok': True}\n",
        requirements=[],
        arguments={},
        env={},
        limits=limits,
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("cannot compute percentile of empty dataset")
    ranked = sorted(values)
    index = max(0, math.ceil((percentile / 100) * len(ranked)) - 1)
    return ranked[index]


def _summarize_ms(values: list[int]) -> dict[str, float]:
    as_float = [float(value) for value in values]
    return {
        "count": float(len(values)),
        "min_ms": min(as_float),
        "max_ms": max(as_float),
        "mean_ms": statistics.fmean(as_float),
        "p50_ms": _percentile(as_float, 50),
        "p95_ms": _percentile(as_float, 95),
    }


def _normalize_ru_maxrss_kb(raw: float) -> float:
    # ru_maxrss is bytes on macOS and KiB on Linux.
    if sys.platform == "darwin":
        return raw / 1024
    return raw


def _child_peak_rss_kb() -> float | None:
    try:
        import resource
    except Exception:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return _normalize_ru_maxrss_kb(float(usage.ru_maxrss))


def _ps_rss_kb(pid: int) -> int | None:
    process = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        return None
    text = process.stdout.strip()
    if not text:
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


def _resident_pool_pids(pool: Any) -> list[int]:
    pids: set[int] = set()
    for worker in getattr(pool, "_all", set()):
        process = getattr(worker, "process", None)
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and pid > 0:
            pids.add(pid)
    return sorted(pids)


async def _run_serial(
    executor: WasmExecutor | PooledWasmExecutor,
    request: ExecRequest,
    *,
    runs: int,
) -> list[int]:
    elapsed_ms: list[int] = []
    for _ in range(max(1, runs)):
        result = await executor.run(request)
        elapsed_ms.append(result.elapsed_ms)
    return elapsed_ms


async def _run_concurrent(
    executor: PooledWasmExecutor,
    request: ExecRequest,
    *,
    calls: int,
) -> tuple[list[int], float]:
    started = time.perf_counter()
    jobs = [executor.run(request) for _ in range(max(1, calls))]
    results = await asyncio.gather(*jobs)
    elapsed = time.perf_counter() - started
    return [item.elapsed_ms for item in results], elapsed


async def benchmark(
    *,
    wasm_path: Path,
    cold_runs: int,
    warm_runs: int,
    concurrent_calls: int,
    concurrent_workers: int,
) -> dict[str, Any]:
    request = _build_request(limits=DEFAULT_LIMITS)
    worker_peak_before = _child_peak_rss_kb()

    cold_settings = Settings(
        sandbox_python_wasm_path=str(wasm_path),
        sandbox_module_cache_path="",
        sandbox_pool_size=0,
        sandbox_max_global_concurrency=0,
        sandbox_max_concurrency_per_tenant=max(1, concurrent_workers),
    )
    cold_executor = WasmExecutor(cold_settings, python_bin=sys.executable)
    cold_no_cache_ms = await _run_serial(cold_executor, request, runs=cold_runs)
    worker_peak_after_cold = _child_peak_rss_kb()

    with tempfile.TemporaryDirectory(prefix="wasm-bench-cache-") as cache_dir:
        cache_settings = Settings(
            sandbox_python_wasm_path=str(wasm_path),
            sandbox_module_cache_path=cache_dir,
            sandbox_pool_size=0,
            sandbox_max_global_concurrency=0,
            sandbox_max_concurrency_per_tenant=max(1, concurrent_workers),
        )
        cache_executor = WasmExecutor(cache_settings, python_bin=sys.executable)
        cold_with_cache_prime_ms = (await _run_serial(cache_executor, request, runs=1))[0]
        cold_with_cache_ms = await _run_serial(cache_executor, request, runs=cold_runs)

    warm_settings = Settings(
        sandbox_python_wasm_path=str(wasm_path),
        sandbox_pool_size=max(1, concurrent_workers),
        sandbox_max_global_concurrency=max(1, concurrent_workers),
        sandbox_max_concurrency_per_tenant=max(1, concurrent_workers),
    )

    warm_serial_executor = PooledWasmExecutor(warm_settings, python_bin=sys.executable)
    await warm_serial_executor.prewarm()
    resident_pids = _resident_pool_pids(warm_serial_executor.pool)
    resident_rss = [rss for pid in resident_pids if (rss := _ps_rss_kb(pid)) is not None]
    warm_serial_ms = await _run_serial(warm_serial_executor, request, runs=warm_runs)
    await warm_serial_executor.aclose()

    warm_concurrent_executor = PooledWasmExecutor(warm_settings, python_bin=sys.executable)
    await warm_concurrent_executor.prewarm()
    warm_concurrent_ms, warm_concurrent_seconds = await _run_concurrent(
        warm_concurrent_executor,
        request,
        calls=concurrent_calls,
    )
    await warm_concurrent_executor.aclose()
    throughput_cps = max(1, concurrent_calls) / max(0.001, warm_concurrent_seconds)

    report = {
        "metadata": {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "wasmtime": version("wasmtime"),
            "python_wasm_path": str(wasm_path),
            "cold_runs": cold_runs,
            "warm_runs": warm_runs,
            "concurrent_calls": concurrent_calls,
            "concurrent_workers": concurrent_workers,
        },
        "cold_no_cache": _summarize_ms(cold_no_cache_ms),
        "cold_with_cache": {
            "prime_ms": float(cold_with_cache_prime_ms),
            **_summarize_ms(cold_with_cache_ms),
        },
        "warm_serial": _summarize_ms(warm_serial_ms),
        "warm_concurrent": {
            "wall_time_seconds": warm_concurrent_seconds,
            "throughput_calls_per_second": throughput_cps,
            **_summarize_ms(warm_concurrent_ms),
        },
        "memory": {
            "worker_peak_rss_kb_before": worker_peak_before,
            "worker_peak_rss_kb_after_cold": worker_peak_after_cold,
            "resident_worker_rss_kb": max(resident_rss) if resident_rss else None,
            "resident_worker_pids": resident_pids,
        },
    }
    return report


def _print_summary(report: dict[str, Any]) -> None:
    cold = report["cold_no_cache"]
    cache = report["cold_with_cache"]
    warm = report["warm_serial"]
    concurrent = report["warm_concurrent"]
    memory = report["memory"]
    print("\nSandbox benchmark summary")
    print(
        f"- Cold no-cache p50/p95: {cold['p50_ms']:.1f} / {cold['p95_ms']:.1f} ms "
        f"(n={int(cold['count'])})"
    )
    print(
        f"- Cold with module cache p50/p95: {cache['p50_ms']:.1f} / {cache['p95_ms']:.1f} ms "
        f"(prime={cache['prime_ms']:.1f} ms)"
    )
    print(
        f"- Warm serial p50/p95: {warm['p50_ms']:.1f} / {warm['p95_ms']:.1f} ms "
        f"(n={int(warm['count'])})"
    )
    print(
        f"- Warm concurrent throughput: {concurrent['throughput_calls_per_second']:.2f} calls/s "
        f"(wall={concurrent['wall_time_seconds']:.3f}s, n={int(concurrent['count'])})"
    )
    print(
        f"- Worker RSS (cold peak -> resident warm): "
        f"{memory['worker_peak_rss_kb_after_cold']} KiB -> {memory['resident_worker_rss_kb']} KiB"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wasm-path",
        default=str(DEFAULT_WASM),
        help="Path to python.wasm (default: vendor/python-3.12.0.wasm).",
    )
    parser.add_argument("--cold-runs", type=int, default=5, help="Serial cold-run sample size.")
    parser.add_argument("--warm-runs", type=int, default=20, help="Serial warm-run sample size.")
    parser.add_argument(
        "--concurrent-calls",
        type=int,
        default=20,
        help="Number of warm concurrent calls for throughput measurement.",
    )
    parser.add_argument(
        "--concurrent-workers",
        type=int,
        default=4,
        help="Warm pool size and global concurrency for concurrent benchmark.",
    )
    parser.add_argument(
        "--json-output",
        default="",
        help="Optional path to write JSON results.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    wasm_path = _ensure_wasm_exists(Path(args.wasm_path))
    report = asyncio.run(
        benchmark(
            wasm_path=wasm_path,
            cold_runs=max(1, args.cold_runs),
            warm_runs=max(1, args.warm_runs),
            concurrent_calls=max(1, args.concurrent_calls),
            concurrent_workers=max(1, args.concurrent_workers),
        )
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    _print_summary(report)
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nWrote benchmark report to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
