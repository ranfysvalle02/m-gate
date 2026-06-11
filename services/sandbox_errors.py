from __future__ import annotations

# Shared, wasmtime-free sandbox primitives. Both the worker (which imports
# wasmtime) and the worker pool (which must NOT, so it can run where wasmtime is
# absent) depend on this module, so size formulas that must agree across the
# process boundary live here.

# Frame ``type`` values the host treats as a mid-execution host round-trip
# (relay to a bridge) rather than the worker's final result frame. Both the
# throwaway executor and the warm pool route on this set so a new bridge kind
# only has to be added in one place.
BRIDGE_RPC_FRAME_TYPES = frozenset({"db_rpc", "tool_rpc"})


def frame_budget_bytes(max_output_bytes: int) -> int:
    """Hard ceiling for a serialized result frame emitted by a sandbox worker.

    A frame carries truncated stdout and stderr (each <= the output cap) plus the
    caller-controlled result and JSON overhead, so the worst-case legitimate line
    is ~2x the output cap. The pool's StreamReader limit is derived from this
    same formula (and kept strictly larger) so a within-budget frame always fits
    in a single ``readline`` across the parent/worker boundary.
    """
    return 2 * max(1024, int(max_output_bytes)) + 128 * 1024


class SandboxError(Exception):
    """Base class for sandbox execution failures."""


class SandboxTimeoutError(SandboxError):
    """Sandbox execution exceeded its wall-clock deadline."""


class SandboxProtocolError(SandboxError):
    """Sandbox worker returned malformed or non-JSON output."""
