"""Shared dependencies, authorization guards, and test-injection seams.

The admin surface is split by resource into sibling modules (``tenants``,
``servers``, ``users``, ``embeddings``, ``code_tools``, ``explore``, ``actions``,
``catalog``). This module is the single place those routers reach for the
cross-cutting concerns they all share:

* the process-wide ``settings`` object and the search / cache-migration
  singletons,
* the role and tenant-scope guards every router applies, and
* the injectable seams (database accessors, ``provision_tenant``,
  ``get_proxy_registry``, the executor, the telemetry logger, and the embedding /
  reprovision helpers).

Routers call the seams *through this module* -- ``from . import _common as c``
then ``c.provision_tenant(...)`` -- so a test can patch one attribute here and
have every router observe it, rather than patching each submodule. ``settings``
and the guards are imported by name instead because they are read, not swapped.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from config.settings import get_settings
from database.mongo import get_control_database, get_tenant_database
from services import users as users_service
from services.cache_migration import SemanticCacheMigrationService
from services.embedding_reprovision import (
    get_tenant_reprovision_status,
    trigger_reprovision,
    trigger_tenant_reprovision,
)
from services.embeddings import build_provider_service
from services.hybrid_search import HybridSearchService
from services.proxy_registry import get_proxy_registry
from services.sandbox_executor import get_executor
from services.telemetry_logger import get_telemetry_logger
from services.tenant_provisioner import provision_tenant

# These names are re-exported as patch seams; list them so linters treat the
# otherwise-unused imports as the intentional public surface they are.
__all__ = [
    "settings",
    "cache_migration_service",
    "hybrid_search_service",
    "get_control_database",
    "get_tenant_database",
    "provision_tenant",
    "get_proxy_registry",
    "get_executor",
    "get_telemetry_logger",
    "build_provider_service",
    "trigger_reprovision",
    "trigger_tenant_reprovision",
    "get_tenant_reprovision_status",
    "users_service",
    "_is_platform_admin",
    "_require_platform_admin",
    "_require_tenant_admin",
    "_resolve_target_tenant",
    "_assert_can_assign_roles",
    "_load_managed_user",
]

# All resource sub-routers register on this one router (each module does
# ``@router.get(...)``); ``__init__`` imports the submodules to trigger
# registration and then exposes ``router``. Paths stay exactly as before because
# the ``/admin`` prefix lives here rather than being added per include.
router = APIRouter(prefix="/admin", tags=["admin"])

settings = get_settings()
cache_migration_service = SemanticCacheMigrationService()
hybrid_search_service = HybridSearchService()


def _is_platform_admin(request: Request) -> bool:
    roles = set(getattr(request.state, "roles", []))
    return settings.platform_admin_role in roles


def _require_platform_admin(request: Request) -> None:
    if not _is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Embedding configuration requires the platform-admin role.",
        )


def _require_tenant_admin(request: Request) -> None:
    roles = set(getattr(request.state, "roles", []))
    if _is_platform_admin(request) or "admin" in roles or "tenant-admin" in roles:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Approvals require tenant-admin or platform-admin role.",
    )


def _resolve_target_tenant(request: Request, requested_tenant: str | None = None) -> str:
    caller_tenant = getattr(request.state, "tenant_id", settings.default_tenant_id)
    header_tenant = request.headers.get("x-tenant-id")
    target = requested_tenant or header_tenant or caller_tenant
    if target != caller_tenant and not _is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-tenant admin access requires platform-admin role.",
        )
    return target


def _assert_can_assign_roles(request: Request, roles: list[str]) -> None:
    """A non-platform-admin may never grant (or keep granting) platform-admin."""
    if settings.platform_admin_role in set(roles) and not _is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform-admin may grant the platform-admin role.",
        )


async def _load_managed_user(request: Request, user_id: str) -> dict[str, Any]:
    """Fetch a user the caller is allowed to read/modify, or raise 403/404.

    Platform-admins span all tenants. A tenant-admin may only touch users inside
    their own tenant, and never a platform-admin account.
    """
    doc = await users_service.get_user_raw(user_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if not _is_platform_admin(request):
        caller_tenant = getattr(request.state, "tenant_id", settings.default_tenant_id)
        if str(doc.get("tenant_id")) != caller_tenant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cross-tenant user management requires platform-admin role.",
            )
        if settings.platform_admin_role in set(doc.get("roles", [])):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only platform-admin may manage a platform-admin user.",
            )
    return doc
