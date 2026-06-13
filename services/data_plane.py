"""Transport-neutral helpers shared by the `/rpc` and `/mcp` data planes.

Both tool-invocation surfaces must meter a downstream tool call identically.
Historically the `/mcp` meta-tools skipped usage metering and billing entirely
(only `/rpc` recorded it), so a tenant could run cost-bearing calls off `/mcp`
without them showing up in usage or billing. Centralizing the shared step here
keeps the two surfaces from drifting again: there is exactly one place that
defines "what it means to record a billable tool call."
"""

from __future__ import annotations

from services.metrics import observe_usage
from services.usage_metering import emit_billing_event, record_usage


async def record_billable_call(tenant_id: str, *, server: str, tool: str, source: str) -> None:
    """Record one metered tool call: usage counter, billing event, and metric.

    ``source`` distinguishes how the result was produced (e.g. ``live_execution``
    vs ``cache_hit``) so billing reconciliation can tell a fresh downstream hop
    from a served cache entry.
    """
    usage = await record_usage(tenant_id, calls=1)
    await emit_billing_event(
        tenant_id,
        kind="calls",
        amount=1,
        period=str(usage.get("period", "")) or None,
        metadata={"server": server, "tool": tool, "source": source},
    )
    observe_usage("calls", 1)
