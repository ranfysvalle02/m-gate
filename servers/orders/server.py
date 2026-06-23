from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
from fastapi import FastAPI
from fastapi.responses import JSONResponse
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


_DEV_ENVIRONMENTS = {"development", "dev", "local", "test", "testing"}
_BUNDLED_DEV_JWKS = "config/dev-jwks.json"


def _jwt_verification_enabled() -> bool:
    return os.getenv("DOWNSTREAM_JWT_VERIFY", "").strip().lower() in {"1", "true", "yes", "on"}


def _is_dev_environment() -> bool:
    return os.getenv("ENVIRONMENT", "development").strip().lower() in _DEV_ENVIRONMENTS


def _guard_bundled_dev_jwks(jwks_path: str) -> None:
    """Refuse to verify downstream tokens against the repo's public dev JWKS in any
    non-local environment: its matching private key is published in this repo, so
    anyone could forge tokens this server would accept."""
    normalized = os.path.normpath(jwks_path)
    bundled = os.path.normpath(_BUNDLED_DEV_JWKS)
    is_bundled = normalized == bundled or normalized.endswith(os.sep + bundled)
    if is_bundled and not _is_dev_environment():
        raise RuntimeError(
            "Refusing to trust the bundled dev JWKS (config/dev-jwks.json) outside local "
            "development; set DOWNSTREAM_JWKS_PATH to a real JWKS file."
        )


def _load_jwks_keys() -> dict[str, Any]:
    jwks_path = os.getenv("DOWNSTREAM_JWKS_PATH", "config/dev-jwks.json")
    _guard_bundled_dev_jwks(jwks_path)
    data = json.loads(Path(jwks_path).read_text(encoding="utf-8"))
    keys: dict[str, Any] = {}
    for jwk in data.get("keys", []):
        kid = jwk.get("kid")
        if isinstance(kid, str) and kid:
            keys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
    return keys


_JWKS_KEYS = _load_jwks_keys() if _jwt_verification_enabled() else {}
_JWT_ISSUER = os.getenv("DOWNSTREAM_JWT_ISSUER", "mdb-mcp-gateway")
_JWT_AUDIENCE = os.getenv("DOWNSTREAM_JWT_AUDIENCE", "orders")


@app.middleware("http")
async def verify_downstream_jwt(request, call_next):
    if not _jwt_verification_enabled() or not request.url.path.startswith("/mcp"):
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return JSONResponse(status_code=401, content={"detail": "Missing bearer token."})
    token = auth_header.split(" ", 1)[1].strip()
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        key = _JWKS_KEYS.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise jwt.InvalidTokenError("Unknown signing key id.")
        jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=_JWT_AUDIENCE,
            issuer=_JWT_ISSUER,
        )
    except jwt.InvalidTokenError as exc:
        return JSONResponse(status_code=401, content={"detail": f"Invalid bearer token: {exc}"})
    return await call_next(request)


app.mount("/mcp", mcp_app)
