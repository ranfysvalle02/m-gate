from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from config.settings import get_settings
from services.hybrid_search import HybridSearchService
from services.proxy_registry import get_proxy_registry
from services.registry_watcher import get_catalog_version

try:
    from fastmcp.server.auth.providers.jwt import JWTVerifier
except Exception:  # pragma: no cover - defensive for FastMCP API drift
    JWTVerifier = None  # type: ignore[assignment,misc]

try:
    from fastmcp.server.dependencies import get_access_token
except Exception:  # pragma: no cover - defensive for FastMCP API drift
    get_access_token = None  # type: ignore[assignment]

hybrid_search_service = HybridSearchService()
_mcp_server: FastMCP | None = None


def _build_auth_verifier():
    settings = get_settings()
    if settings.auth_mode != "jwks" or JWTVerifier is None:
        return None
    if not settings.jwks_uri:
        # Local-JWKS mode is enforced by the gateway middleware; FastMCP's JWT
        # verifier currently expects a URI endpoint.
        return None
    kwargs: dict[str, Any] = {"jwks_uri": settings.jwks_uri}
    if settings.jwt_issuer:
        kwargs["issuer"] = settings.jwt_issuer
    if settings.jwt_audience:
        kwargs["audience"] = settings.jwt_audience
    return JWTVerifier(**kwargs)


def _register_tools(mcp: FastMCP) -> None:
    settings = get_settings()

    def _resolve_tenant_id(override_tenant_id: str | None = None) -> str:
        if override_tenant_id:
            return override_tenant_id
        if get_access_token is not None:
            try:
                token = get_access_token()
            except Exception:
                token = None
            if token is not None:
                claims = getattr(token, "claims", {}) or {}
                tenant_id = claims.get("tenant_id")
                if isinstance(tenant_id, str) and tenant_id:
                    return tenant_id
        return settings.default_tenant_id

    @mcp.tool
    async def search_tools(
        query: str,
        limit: int = 10,
        vector_weight: float | None = None,
        text_weight: float | None = None,
        allowed_scopes: list[str] | None = None,
        mode: str = "hybrid",
        tenant_id: str | None = None,
    ) -> dict:
        resolved_tenant_id = _resolve_tenant_id(tenant_id)
        items = await hybrid_search_service.search_tools(
            tenant_id=resolved_tenant_id,
            query=query,
            limit=limit,
            vector_weight=vector_weight,
            text_weight=text_weight,
            allowed_scopes=allowed_scopes,
            mode=mode,
        )
        return {
            "query": query,
            "limit": limit,
            "mode": mode,
            "items": items,
        }

    @mcp.tool
    async def list_catalog_tools(
        limit: int = 20,
        cursor: int = 0,
        query: str | None = None,
        allowed_scopes: list[str] | None = None,
        tenant_id: str | None = None,
    ) -> dict:
        resolved_tenant_id = _resolve_tenant_id(tenant_id)
        if query:
            items = await hybrid_search_service.search_tools(
                tenant_id=resolved_tenant_id,
                query=query,
                limit=limit,
                allowed_scopes=allowed_scopes,
            )
            return {
                "tools": items,
                "next_cursor": None,
                "catalog_version": get_catalog_version(),
                "routed": True,
            }

        page_size = min(max(1, limit), settings.catalog_list_limit)
        offset = max(0, cursor)
        items = await hybrid_search_service.list_tools(
            tenant_id=resolved_tenant_id,
            allowed_scopes=allowed_scopes,
            limit=page_size + 1,
            offset=offset,
        )
        has_more = len(items) > page_size
        if has_more:
            items = items[:page_size]
        return {
            "tools": items,
            "next_cursor": offset + page_size if has_more else None,
            "catalog_version": get_catalog_version(),
            "routed": False,
        }

    @mcp.tool
    async def call_downstream_tool(
        server: str,
        name: str,
        arguments: dict,
        tenant_id: str | None = None,
    ) -> dict:
        resolved_tenant_id = _resolve_tenant_id(tenant_id)
        result = await get_proxy_registry().call_tool(
            server_name=server,
            tool_name=name,
            arguments=arguments,
            tenant_id=resolved_tenant_id,
        )
        return {"server": server, "name": name, "result": result}


def _create_server() -> FastMCP:
    server = FastMCP("mdb-mcp-gateway", auth=_build_auth_verifier())
    _register_tools(server)
    return server


def get_mcp_server() -> FastMCP:
    global _mcp_server
    if _mcp_server is None:
        _mcp_server = _create_server()
    return _mcp_server
