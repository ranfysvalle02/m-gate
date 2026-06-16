from __future__ import annotations

import importlib
import io
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_worker_module(monkeypatch):
    class _Config:
        consume_fuel = False
        epoch_interruption = False
        max_wasm_stack = 0

    class _Engine:
        def __init__(self, _config):
            self.epoch = 0

        def increment_epoch(self):
            self.epoch += 1

    class _Store:
        def __init__(self, _engine):
            self.wasi = None

        def set_fuel(self, _value):
            return None

        def add_fuel(self, _value):
            return None

        def set_limits(self, **_kwargs):
            return None

        def set_epoch_deadline(self, _value):
            return None

        def set_wasi(self, wasi):
            self.wasi = wasi

    class _WasiConfig:
        def __init__(self):
            self.argv = []
            self.stdout_file = ""
            self.stderr_file = ""
            self.host_dir = ""

        def preopen_dir(self, host: str, _guest: str):
            self.host_dir = host

    class _Instance:
        def __init__(self, store):
            self.store = store

        def exports(self, store):
            def _start(_store):
                host_dir = Path(store.wasi.host_dir)
                (host_dir / "sandbox.result.json").write_text(
                    json.dumps({"ok": True, "result": {"ok": True}}), encoding="utf-8"
                )
                Path(store.wasi.stdout_file).write_text("guest out", encoding="utf-8")
                Path(store.wasi.stderr_file).write_text("guest err", encoding="utf-8")

            return {"_start": _start}

    class _Linker:
        def __init__(self, _engine):
            return None

        def define_wasi(self):
            return None

        def instantiate(self, store, _module):
            return _Instance(store)

    class _Module:
        @staticmethod
        def from_file(_engine, _path):
            return object()

    fake_wasmtime = SimpleNamespace(
        Config=_Config,
        Engine=_Engine,
        Linker=_Linker,
        Module=_Module,
        Store=_Store,
        WasiConfig=_WasiConfig,
    )
    monkeypatch.setitem(sys.modules, "wasmtime", fake_wasmtime)
    import services.sandbox_worker as worker

    return importlib.reload(worker)


def test_main_returns_error_when_job_missing(monkeypatch, capsys, tmp_path):
    worker = _load_worker_module(monkeypatch)
    monkeypatch.setattr(worker, "_apply_posix_rlimits", lambda **_kwargs: None)
    missing = tmp_path / "missing-job.json"
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")
    monkeypatch.setattr(
        sys,
        "argv",
        ["sandbox_worker", "--job", str(missing), "--wasm", str(wasm)],
    )
    exit_code = worker.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["error"]["type"] == "protocol_error"


def test_main_success_path_uses_run_wasm(monkeypatch, capsys, tmp_path):
    worker = _load_worker_module(monkeypatch)
    monkeypatch.setattr(worker, "_apply_posix_rlimits", lambda **_kwargs: None)
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"limits": {}}), encoding="utf-8")
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")

    monkeypatch.setattr(
        worker,
        "_run_wasm",
        lambda *_a, **_k: {"ok": True, "result": {"sum": 3}, "stdout": "", "stderr": ""},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["sandbox_worker", "--job", str(job), "--wasm", str(wasm)],
    )
    exit_code = worker.main()
    payload = json.loads(capsys.readouterr().out.strip())
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["result"] == {"sum": 3}
    assert payload["worker_elapsed_ms"] >= 0


def test_run_wasm_success_frame(monkeypatch, tmp_path):
    worker = _load_worker_module(monkeypatch)
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"limits": {}}), encoding="utf-8")
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")

    frame = worker._run_wasm(
        job,
        wasm,
        {
            "fuel": 10_000,
            "memory_bytes": 32 * 1024 * 1024,
            "wall_timeout_ms": 500,
            "max_output_bytes": 128 * 1024,
        },
    )
    assert frame["ok"] is True
    assert frame["result"] == {"ok": True}
    assert frame["stdout"] == "guest out"
    assert frame["stderr"] == "guest err"


def test_run_wasm_maps_fuel_trap_to_error(monkeypatch, tmp_path):
    worker = _load_worker_module(monkeypatch)
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"limits": {}}), encoding="utf-8")
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")

    class _LinkerThatFails(worker.Linker):
        def instantiate(self, _store, _module):
            raise RuntimeError("all fuel consumed by WebAssembly")

    monkeypatch.setattr(worker, "Linker", _LinkerThatFails)
    frame = worker._run_wasm(
        job,
        wasm,
        {
            "fuel": 10_000,
            "memory_bytes": 32 * 1024 * 1024,
            "wall_timeout_ms": 500,
            "max_output_bytes": 128 * 1024,
        },
    )
    assert frame["ok"] is False
    assert frame["error"]["type"] == "fuel_exhausted"


def _warm_worker(monkeypatch):
    worker = _load_worker_module(monkeypatch)
    monkeypatch.setattr(worker, "_build_engine", lambda: object())
    monkeypatch.setattr(worker, "_load_module", lambda *_a, **_k: object())
    # Never mutate the test process's real rlimits; the live integration test
    # exercises the real backstop in a subprocess instead.
    monkeypatch.setattr(worker, "_apply_serve_rlimits", lambda **_k: None)
    return worker


def test_serve_handles_ping_job_and_malformed(monkeypatch, capsys, tmp_path):
    worker = _warm_worker(monkeypatch)
    monkeypatch.setattr(
        worker,
        "_run_wasm",
        lambda *_a, **_k: {"ok": True, "result": {"v": 1}, "stdout": "", "stderr": ""},
    )
    lines = (
        "\n".join(
            [
                '{"ping": true}',
                json.dumps({"job_dir": str(tmp_path), "limits": {"wall_timeout_ms": 500}}),
                "not-json",
                '{"limits": {}}',
                '{"shutdown": true}',
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(lines))
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")

    rc = worker.serve(wasm, None)
    assert rc == 0
    frames = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert frames[0] == {"ok": True, "pong": True}
    assert frames[1]["result"] == {"v": 1}
    assert frames[1]["worker_elapsed_ms"] >= 0
    assert frames[2]["error"]["type"] == "protocol_error"  # malformed json line
    assert frames[3]["error"]["type"] == "protocol_error"  # missing job_dir


def test_serve_warmup_failure_returns_error_code(monkeypatch, tmp_path):
    worker = _load_worker_module(monkeypatch)
    monkeypatch.setattr(worker, "_build_engine", lambda: object())

    def _boom(*_a, **_k):
        raise RuntimeError("compile failed")

    monkeypatch.setattr(worker, "_load_module", _boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")
    assert worker.serve(wasm, "vendor/.wasm-cache") == 1


def test_serve_job_exception_becomes_error_frame(monkeypatch, capsys, tmp_path):
    worker = _warm_worker(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(worker, "_run_wasm", _boom)
    lines = json.dumps({"job_dir": str(tmp_path), "limits": {}}) + "\n" + '{"shutdown": true}\n'
    monkeypatch.setattr(sys, "stdin", io.StringIO(lines))
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")

    worker.serve(wasm, None)
    frame = json.loads(capsys.readouterr().out.splitlines()[0])
    assert frame["ok"] is False
    assert frame["error"]["type"] == "execution_error"


def test_main_serve_dispatches_to_serve(monkeypatch, tmp_path):
    worker = _load_worker_module(monkeypatch)
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")
    captured: dict = {}

    def _fake_serve(wasm_file, module_cache):
        captured["wasm"] = wasm_file
        captured["cache"] = module_cache
        return 0

    monkeypatch.setattr(worker, "serve", _fake_serve)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sandbox_worker", "--serve", "--wasm", str(wasm), "--module-cache", "cache-dir"],
    )
    assert worker.main() == 0
    assert captured["cache"] == "cache-dir"


def test_load_module_compiles_without_cache(monkeypatch, tmp_path):
    worker = _load_worker_module(monkeypatch)
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")
    module = worker._load_module(object(), wasm, None)
    assert module is not None


def test_safe_read_caps_bytes(monkeypatch, tmp_path):
    worker = _load_worker_module(monkeypatch)
    big = tmp_path / "out.log"
    big.write_text("a" * 5000, encoding="utf-8")
    assert worker._safe_read(big, 1000) == "a" * 1000
    assert worker._safe_read(big, 0) == ""
    assert worker._safe_read(tmp_path / "missing.log", 1000) == ""


def test_bounded_frame_passes_small_and_replaces_oversized(monkeypatch):
    worker = _load_worker_module(monkeypatch)
    small = {"ok": True, "result": {"v": 1}, "stdout": "", "stderr": ""}
    assert worker._bounded_frame(small, 64 * 1024) is small

    oversized = {"ok": True, "result": {"blob": "x" * 200_000}, "stdout": "", "stderr": ""}
    bounded = worker._bounded_frame(oversized, 1024)
    assert bounded["ok"] is False
    assert bounded["error"]["type"] == "output_limit"
    assert bounded["stdout"] == ""


def test_bounded_frame_handles_unserializable(monkeypatch):
    worker = _load_worker_module(monkeypatch)
    bounded = worker._bounded_frame({"ok": True, "result": {1, 2, 3}}, 64 * 1024)
    assert bounded["ok"] is False
    assert bounded["error"]["type"] == "protocol_error"


def test_run_wasm_truncates_oversized_guest_output(monkeypatch, tmp_path):
    worker = _load_worker_module(monkeypatch)

    class _BigInstance:
        def __init__(self, store):
            self.store = store

        def exports(self, store):
            def _start(_store):
                host_dir = Path(store.wasi.host_dir)
                (host_dir / "sandbox.result.json").write_text(
                    json.dumps({"ok": True, "result": {"v": 1}}), encoding="utf-8"
                )
                Path(store.wasi.stdout_file).write_text("o" * 50_000, encoding="utf-8")
                Path(store.wasi.stderr_file).write_text("", encoding="utf-8")

            return {"_start": _start}

    class _BigLinker(worker.Linker):
        def instantiate(self, store, _module):
            return _BigInstance(store)

    monkeypatch.setattr(worker, "Linker", _BigLinker)
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"limits": {}}), encoding="utf-8")
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")

    frame = worker._run_wasm(
        job,
        wasm,
        {
            "fuel": 10_000,
            "memory_bytes": 32 * 1024 * 1024,
            "wall_timeout_ms": 500,
            "max_output_bytes": 1000,
        },
    )
    # Guest wrote 50k of stdout; the worker only ever buffers up to the cap.
    assert len(frame["stdout"]) == 1000


def test_apply_serve_rlimits_sets_noncumulative_limits(monkeypatch):
    worker = _load_worker_module(monkeypatch)
    monkeypatch.setattr(worker.os, "name", "posix")
    calls: dict = {"rlimits": {}, "signals": []}

    fake_resource = SimpleNamespace(
        RLIMIT_FSIZE=1,
        RLIMIT_NOFILE=2,
        RLIMIT_CPU=3,
        RLIMIT_AS=4,
        setrlimit=lambda which, pair: calls["rlimits"].__setitem__(which, pair),
    )
    fake_signal = SimpleNamespace(
        SIGXFSZ="xfsz",
        SIG_IGN="ign",
        signal=lambda sig, handler: calls["signals"].append((sig, handler)),
    )
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    monkeypatch.setitem(sys.modules, "signal", fake_signal)

    worker._apply_serve_rlimits(max_output_bytes=128 * 1024)

    # Cumulative limits (CPU / address space) must NOT be set on a resident worker.
    assert fake_resource.RLIMIT_CPU not in calls["rlimits"]
    assert fake_resource.RLIMIT_AS not in calls["rlimits"]
    # FSIZE backstop must clear the frame budget so a legitimate near-cap result
    # (written full to /job/sandbox.result.json with JSON framing) is bounded
    # gracefully by _bounded_frame rather than killed by EFBIG.
    expected_fsize = max(worker.frame_budget_bytes(128 * 1024), 16 * 1024 * 1024, 64 * 1024)
    assert calls["rlimits"][fake_resource.RLIMIT_FSIZE] == (expected_fsize, expected_fsize)
    assert calls["rlimits"][fake_resource.RLIMIT_NOFILE] == (256, 256)
    # SIGXFSZ is ignored so an over-limit write fails the job, not the worker.
    assert ("xfsz", "ign") in calls["signals"]


def test_apply_serve_rlimits_noop_off_posix(monkeypatch):
    worker = _load_worker_module(monkeypatch)
    monkeypatch.setattr(worker.os, "name", "nt")
    # Must not even import resource/signal off-posix; simply returns.
    worker._apply_serve_rlimits(max_output_bytes=1024)


def test_load_module_handles_cache_dir(monkeypatch, tmp_path):
    worker = _load_worker_module(monkeypatch)
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")
    cache = tmp_path / "cache"
    # Compile path: cache miss => compile, then a best-effort (failing) serialize.
    module = worker._load_module(object(), wasm, str(cache))
    assert module is not None
    # Deserialize path: a pre-existing (garbage) artifact triggers the read branch,
    # which falls back to a clean compile when the fake runtime can't deserialize it.
    cache_file = worker._module_cache_file(wasm, str(cache))
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(b"garbage")
    module = worker._load_module(object(), wasm, str(cache))
    assert module is not None


def test_apply_posix_rlimits_cpu_clears_wall_plus_boot_grace(monkeypatch):
    worker = _load_worker_module(monkeypatch)
    monkeypatch.setattr(worker.os, "name", "posix")
    calls: dict = {"rlimits": {}, "signals": []}

    fake_resource = SimpleNamespace(
        RLIMIT_FSIZE=1,
        RLIMIT_NOFILE=2,
        RLIMIT_CPU=3,
        RLIMIT_AS=4,
        setrlimit=lambda which, pair: calls["rlimits"].__setitem__(which, pair),
    )
    fake_signal = SimpleNamespace(
        SIGXFSZ="xfsz",
        SIG_IGN="ign",
        signal=lambda sig, handler: calls["signals"].append((sig, handler)),
    )
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    monkeypatch.setitem(sys.modules, "signal", fake_signal)

    wall_ms, boot_ms = 300, 10_000
    worker._apply_posix_rlimits(
        wall_timeout_ms=wall_ms,
        memory_bytes=64 * 1024 * 1024,
        max_output_bytes=128 * 1024,
        boot_grace_ms=boot_ms,
    )

    cpu_soft, cpu_hard = calls["rlimits"][fake_resource.RLIMIT_CPU]
    wall_budget = math.ceil((wall_ms + boot_ms) / 1000)
    # The CPU backstop must outlast the guest's OWN epoch deadline (wall+boot_grace)
    # so a CPU-bound guest is gracefully epoch-interrupted into a timeout frame,
    # not hard SIGXCPU-killed first -- the regression these CI tests hit when
    # boot_grace was added to the epoch timer but not to this budget.
    assert cpu_soft >= wall_budget
    # ...and clear the one-time cold-compile cost on top of that wall budget.
    assert cpu_soft >= worker._COMPILE_CPU_ALLOWANCE_SECONDS + wall_budget
    assert cpu_hard >= cpu_soft
    # Match serve mode: NOFILE raised to 256 (64 was too tight for cold boot) and
    # SIGXFSZ ignored so an over-limit write fails the job, not the worker.
    assert calls["rlimits"][fake_resource.RLIMIT_NOFILE] == (256, 256)
    assert fake_resource.RLIMIT_FSIZE in calls["rlimits"]
    assert ("xfsz", "ign") in calls["signals"]
    # Never cap address space: wasmtime's large reservation would be killed on CI.
    assert fake_resource.RLIMIT_AS not in calls["rlimits"]


def test_main_one_shot_threads_module_cache_and_boot_grace(monkeypatch, tmp_path):
    worker = _load_worker_module(monkeypatch)
    captured: dict = {}

    def _fake_apply(**kwargs):
        captured["rlimit_kwargs"] = kwargs

    def _fake_run_wasm(job_file, wasm_file, limits, *, module_cache=None, **_k):
        captured["module_cache"] = module_cache
        return {"ok": True, "result": {"v": 1}, "stdout": "", "stderr": ""}

    monkeypatch.setattr(worker, "_apply_posix_rlimits", _fake_apply)
    monkeypatch.setattr(worker, "_run_wasm", _fake_run_wasm)

    job = tmp_path / "job.json"
    job.write_text(
        json.dumps({"limits": {"wall_timeout_ms": 300, "boot_grace_ms": 10_000}}),
        encoding="utf-8",
    )
    wasm = tmp_path / "python.wasm"
    wasm.write_bytes(b"wasm")
    monkeypatch.setattr(
        sys,
        "argv",
        ["sandbox_worker", "--job", str(job), "--wasm", str(wasm), "--module-cache", "cache-dir"],
    )

    assert worker.main() == 0
    # The compiled-module cache is reused for throwaway workers too (was serve-only),
    # so a one-shot worker deserializes instead of paying the full cold compile.
    assert captured["module_cache"] == "cache-dir"
    # boot_grace is threaded into the CPU backstop math so it can never be tighter
    # than the guest's epoch deadline.
    assert captured["rlimit_kwargs"]["boot_grace_ms"] == 10_000
    assert captured["rlimit_kwargs"]["wall_timeout_ms"] == 300
