from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastmcp import FastMCP

mcp = FastMCP("orders-server")


@mcp.tool
async def find_order(order_id: str) -> dict:
    return {
        "order_id": order_id,
        "status": "processing",
        "total": 149.99,
        "currency": "USD",
        "updated_at": datetime.now(UTC).isoformat(),
    }


@mcp.tool
async def list_customer_orders(customer_id: str, limit: int = 5) -> list[dict]:
    limit = max(1, min(limit, 20))
    return [
        {
            "order_id": f"ORD-{customer_id}-{i + 1}",
            "status": "delivered" if i % 2 == 0 else "processing",
            "total": round(25.5 + i * 10.25, 2),
        }
        for i in range(limit)
    ]


@mcp.tool
async def update_order_status(order_id: str, status: str) -> dict:
    return {
        "order_id": order_id,
        "status": status,
        "updated_at": datetime.now(UTC).isoformat(),
    }


mcp_app = mcp.http_app(path="/")
app = FastAPI(title="orders-server", lifespan=mcp_app.lifespan)
app.mount("/mcp", mcp_app)
