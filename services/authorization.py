from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.settings import get_settings
from database.mongo import get_tenant_database


@dataclass
class AuthorizationResult:
    allowed: bool
    reason: str
    tool: dict[str, Any] | None = None


class AuthorizationService:
    async def get_tool(
        self,
        *,
        tenant_id: str | None = None,
        server: str,
        name: str,
    ) -> dict[str, Any] | None:
        resolved_tenant = tenant_id or get_settings().default_tenant_id
        return await get_tenant_database(resolved_tenant)["tool_catalog"].find_one(
            {"server": server, "name": name}
        )

    async def authorize_tool_call(
        self,
        *,
        tenant_id: str | None = None,
        server: str,
        name: str,
        caller_scopes: list[str] | None,
        caller_roles: list[str] | None = None,
    ) -> AuthorizationResult:
        tool = await self.get_tool(tenant_id=tenant_id, server=server, name=name)
        if tool is None:
            return AuthorizationResult(allowed=False, reason="tool_not_found")

        roles = set(caller_roles or [])
        if "admin" in roles:
            return AuthorizationResult(allowed=True, reason="admin_override", tool=tool)

        normalized_scopes = {scope for scope in (caller_scopes or []) if isinstance(scope, str)}
        required_server_scope = f"server:{server}"
        if required_server_scope not in normalized_scopes and "server:*" not in normalized_scopes:
            return AuthorizationResult(allowed=False, reason="server_scope_required", tool=tool)

        required_scopes = [scope for scope in tool.get("scopes", []) if isinstance(scope, str)]
        if not required_scopes:
            return AuthorizationResult(allowed=True, reason="no_scope_required", tool=tool)

        if not normalized_scopes:
            return AuthorizationResult(allowed=False, reason="missing_scope", tool=tool)

        if set(required_scopes).intersection(normalized_scopes):
            return AuthorizationResult(allowed=True, reason="scope_match", tool=tool)

        return AuthorizationResult(allowed=False, reason="scope_mismatch", tool=tool)


_authorization_service: AuthorizationService | None = None


def get_authorization_service() -> AuthorizationService:
    global _authorization_service
    if _authorization_service is None:
        _authorization_service = AuthorizationService()
    return _authorization_service
