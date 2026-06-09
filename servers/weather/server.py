from __future__ import annotations

from fastapi import FastAPI
from fastmcp import FastMCP

mcp = FastMCP("weather-server")


@mcp.tool
async def get_current_weather(city: str, unit: str = "celsius") -> dict:
    base_temp = 21 if city.lower() != "montreal" else 18
    temp = base_temp if unit == "celsius" else (base_temp * 9 / 5) + 32
    return {"city": city, "unit": unit, "temperature": round(temp, 1), "condition": "partly_cloudy"}


@mcp.tool
async def get_forecast(city: str, days: int = 3) -> list[dict]:
    days = max(1, min(days, 7))
    return [
        {"day": i + 1, "city": city, "high_c": 21 + i, "low_c": 14 + i, "condition": "sunny"}
        for i in range(days)
    ]


@mcp.tool
async def severe_weather_alerts(region: str) -> dict:
    return {"region": region, "alerts": [], "status": "no_active_alerts"}


mcp_app = mcp.http_app(path="/")
app = FastAPI(title="weather-server", lifespan=mcp_app.lifespan)
app.mount("/mcp", mcp_app)
