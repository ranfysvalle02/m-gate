"""Control-plane admin API, decomposed by resource into a router package.

This package replaces the former single ``gateway/routers/admin.py`` module. It
was split because that file had grown to ~1,900 lines / 47 handlers, which made
it the project's biggest review and merge-conflict bottleneck. The HTTP surface
is unchanged: every route still mounts under the ``/admin`` prefix and keeps its
exact path, because all sub-routers register on one shared ``router`` defined in
:mod:`._common` (which owns the ``prefix="/admin"``).

Layout
------
* :mod:`._common` -- the shared ``settings`` object, the search / cache-migration
  singletons, the authorization guards, and the **test-injection seams**
  (database accessors, ``provision_tenant``, ``get_proxy_registry``,
  ``get_executor``, ``get_telemetry_logger``, and the embedding / reprovision
  helpers). Handlers reach seams via ``from . import _common as c`` then
  ``c.<seam>(...)``.
* :mod:`.tenants` -- tenant lifecycle, egress allowlist, per-server secrets,
  usage, and quota.
* :mod:`.servers` -- downstream server registration, inspection, export, and
  lifecycle.
* :mod:`.users` -- managed-user CRUD, whoami, and password self-service.
* :mod:`.embeddings` -- platform-default and per-tenant embedding configuration.
* :mod:`.code_tools` -- linting and sandboxed test-runs for code-backed tools.
* :mod:`.explore` -- read-only tenant data exploration.
* :mod:`.actions` -- the human-in-the-loop approval queue.
* :mod:`.catalog` -- catalog listing, telemetry, stats, search, cache migration.

Test-injection seams
---------------------
Because every router accesses an injectable dependency through ``_common``, the
single patch target for the whole admin surface is :mod:`._common`. Tests patch
e.g. ``admin._common.provision_tenant`` / ``admin._common.get_tenant_database``
once and every router observes it. Handlers and the ``settings`` singleton are
re-exported here, so ``import gateway.routers.admin as admin`` followed by a
direct handler call (``admin.create_user(...)``) or ``admin.settings`` access
keeps working exactly as it did against the old flat module.
"""

from __future__ import annotations

from . import _common
from ._common import router, settings
from .actions import (
    approve_pending_action,
    list_actions,
    reject_pending_action,
)
from .analytics import (
    analytics_overview,
    analytics_quota_utilization,
    analytics_telemetry_trend,
    analytics_top_tools,
    analytics_usage_trend,
)
from .catalog import (
    admin_search,
    admin_stats,
    export_telemetry,
    list_catalog,
    list_telemetry,
    migrate_cache,
)
from .code_tools import test_code_tool, validate_code_tool_endpoint
from .embeddings import (
    _secret_encryption_label,
    get_embedding_config,
    get_embedding_status,
    get_tenant_embedding_config,
    get_tenant_embedding_status,
    reset_tenant_embedding_config,
    test_embedding_config,
    test_tenant_embedding_config,
    update_embedding_config,
    update_tenant_embedding_config,
)
from .explore import explore_collections, explore_query, explore_sample
from .servers import (
    create_or_update_server,
    delete_server,
    disable_server,
    enable_server,
    export_server,
    get_server,
    list_servers,
    patch_server,
)
from .tenants import (
    confirm_tenant,
    create_tenant,
    delete_tenant,
    export_tenant_usage,
    get_egress_allowlist,
    get_server_env,
    get_tenant_tool_policy,
    get_tenant_usage,
    get_tenant_usage_events,
    list_tenants,
    make_tenant_read_only,
    make_tenant_read_write,
    put_egress_allowlist,
    put_server_env,
    put_tenant_tool_policy,
    restore_tenant,
    resume_tenant,
    suspend_tenant,
    unconfirm_tenant,
    update_tenant_quota,
)
from .tools import disable_tool, enable_tool
from .users import (
    change_my_password,
    create_demo_user,
    create_user,
    create_viewer_user,
    delete_user,
    get_user,
    list_users,
    update_user,
    who_am_i,
)

__all__ = [
    "router",
    "settings",
    "_common",
    "_secret_encryption_label",
    # tenants
    "create_tenant",
    "list_tenants",
    "delete_tenant",
    "restore_tenant",
    "suspend_tenant",
    "resume_tenant",
    "confirm_tenant",
    "unconfirm_tenant",
    "make_tenant_read_only",
    "make_tenant_read_write",
    "get_tenant_tool_policy",
    "put_tenant_tool_policy",
    "get_egress_allowlist",
    "put_egress_allowlist",
    "get_server_env",
    "put_server_env",
    "get_tenant_usage",
    "get_tenant_usage_events",
    "export_tenant_usage",
    "update_tenant_quota",
    # servers
    "create_or_update_server",
    "list_servers",
    "get_server",
    "export_server",
    "patch_server",
    "delete_server",
    "enable_server",
    "disable_server",
    # tools (per-tenant overlay)
    "enable_tool",
    "disable_tool",
    # users
    "who_am_i",
    "create_user",
    "create_demo_user",
    "create_viewer_user",
    "list_users",
    "change_my_password",
    "get_user",
    "update_user",
    "delete_user",
    # embeddings
    "get_embedding_config",
    "update_embedding_config",
    "test_embedding_config",
    "get_tenant_embedding_config",
    "update_tenant_embedding_config",
    "reset_tenant_embedding_config",
    "test_tenant_embedding_config",
    "get_tenant_embedding_status",
    "get_embedding_status",
    # code tools
    "validate_code_tool_endpoint",
    "test_code_tool",
    # explore
    "explore_collections",
    "explore_sample",
    "explore_query",
    # actions
    "list_actions",
    "approve_pending_action",
    "reject_pending_action",
    # analytics
    "analytics_overview",
    "analytics_usage_trend",
    "analytics_top_tools",
    "analytics_telemetry_trend",
    "analytics_quota_utilization",
    # catalog
    "list_catalog",
    "list_telemetry",
    "export_telemetry",
    "admin_stats",
    "admin_search",
    "migrate_cache",
]
