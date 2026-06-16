from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.settings import get_settings
from database.mongo import get_tenant_database
from services.tenant_tool_policy import get_tool_policy, matches_allowlist

# The data-plane invoke capability. A caller may only run ``tools/call`` if it
# carries this role; ``tool:read`` principals clear the coarse RBAC gate (so they
# can discover) but lack this, so invocation is refused here. ``admin`` bypasses.
INVOKE_ROLE = "tool:invoke"


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

        resolved_tenant = tenant_id or get_settings().default_tenant_id
        policy = await get_tool_policy(resolved_tenant)

        # Per-tool kill-switch is absolute: a tenant-disabled tool is refused for
        # every principal, including admins, so disabling truly takes a tool out
        # of service for the tenant rather than just hiding it from non-admins.
        if f"{server}/{name}" in policy["disabled_tools"]:
            return AuthorizationResult(allowed=False, reason="tool_disabled", tool=tool)

        roles = set(caller_roles or [])
        if "admin" in roles:
            return AuthorizationResult(allowed=True, reason="admin_override", tool=tool)

        # Invoke capability: discovery-only principals (``tool:read``) reach this
        # path but cannot run tools. This is the gate that makes a viewer/MCP
        # read-only token safe to hand out for a showcase.
        if INVOKE_ROLE not in roles:
            return AuthorizationResult(allowed=False, reason="invoke_not_permitted", tool=tool)

        # Allowlist: when a tenant has curated its surface, a tool outside the
        # curated set is refused even if the caller's scopes would otherwise allow
        # it. An empty allowlist means unrestricted.
        if not matches_allowlist(server, name, policy["allowlist"]):
            return AuthorizationResult(allowed=False, reason="tool_not_allowlisted", tool=tool)

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
