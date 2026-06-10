from __future__ import annotations

from types import SimpleNamespace

import pytest

import scripts.bench_sandbox as bench


def test_ensure_wasm_exists_returns_resolved_path(tmp_path):
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")
    resolved = bench._ensure_wasm_exists(wasm)
    assert resolved == wasm.resolve()


def test_ensure_wasm_exists_raises_when_missing(tmp_path):
    missing = tmp_path / "missing.wasm"
    with pytest.raises(FileNotFoundError, match="make fetch-wasm"):
        bench._ensure_wasm_exists(missing)


def test_summarize_ms_and_percentiles_are_stable():
    summary = bench._summarize_ms([10, 20, 30, 40, 50])
    assert summary["count"] == 5.0
    assert summary["min_ms"] == 10.0
    assert summary["max_ms"] == 50.0
    assert summary["mean_ms"] == 30.0
    assert summary["p50_ms"] == 30.0
    assert summary["p95_ms"] == 50.0


@pytest.mark.asyncio
async def test_benchmark_report_shape_is_deterministic_with_mocks(monkeypatch, tmp_path):
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")

    async def _fake_run_serial(_executor, _request, *, runs):
        return [100 + idx for idx in range(runs)]

    async def _fake_run_concurrent(_executor, _request, *, calls):
        return [50] * calls, 2.0

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bench, "_run_serial", _fake_run_serial)
    monkeypatch.setattr(bench, "_run_concurrent", _fake_run_concurrent)
    monkeypatch.setattr(bench, "_child_peak_rss_kb", lambda: 1234.0)
    monkeypatch.setattr(bench, "_resident_pool_pids", lambda _pool: [11111])
    monkeypatch.setattr(bench, "_ps_rss_kb", lambda _pid: 2222)
    monkeypatch.setattr(bench, "version", lambda _dist: "45.0.0")
    monkeypatch.setattr(bench.PooledWasmExecutor, "prewarm", _noop)
    monkeypatch.setattr(bench.PooledWasmExecutor, "aclose", _noop)
    monkeypatch.setattr(
        bench.PooledWasmExecutor, "pool", SimpleNamespace(_all=set()), raising=False
    )

    report = await bench.benchmark(
        wasm_path=wasm,
        cold_runs=3,
        warm_runs=4,
        concurrent_calls=5,
        concurrent_workers=2,
    )
    assert report["metadata"]["wasmtime"] == "45.0.0"
    assert report["cold_no_cache"]["count"] == 3.0
    assert report["cold_with_cache"]["prime_ms"] == 100.0
    assert report["warm_serial"]["count"] == 4.0
    assert report["warm_concurrent"]["count"] == 5.0
    assert report["warm_concurrent"]["throughput_calls_per_second"] == 2.5
    assert report["memory"]["worker_peak_rss_kb_after_cold"] == 1234.0
    assert report["memory"]["resident_worker_rss_kb"] == 2222
    assert report["memory"]["resident_worker_pids"] == [11111]
