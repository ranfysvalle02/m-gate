from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from models.registry import ToolDocument


class TenantCreateRequest(BaseModel):
    tenant_id: str


class TenantResponse(BaseModel):
    tenant_id: str
    db_name: str
    status: str = "active"
    suspended_reason: str | None = None
    deleted_at: datetime | None = None
    purge_at: datetime | None = None
    # Read-only is orthogonal to ``status``: the tenant stays ``active`` (fully
    # discoverable) but every mutation (tools/call + tenant-side config) is refused.
    read_only: bool = False
    read_only_reason: str | None = None
    # Account confirmation tier: "unconfirmed" (a fresh self-service sign-up, tightly
    # capped) or "confirmed" (promoted by a platform-admin / the default for
    # admin-created tenants).
    confirmation: str = "confirmed"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TenantStatusUpdateRequest(BaseModel):
    reason: str | None = None


class ToolPolicyUpdateRequest(BaseModel):
    # Fully-qualified ``server/name`` entries (or ``server/*`` wildcards). Empty
    # means unrestricted — discovery and invocation see the whole catalog.
    allowlist: list[str] = Field(default_factory=list)
    # Cap on how many tools the tenant may register (enforced at server mount).
    # 0 means unlimited.
    max_tools: int = Field(default=0, ge=0)


class ToolPolicyToolEntry(BaseModel):
    server: str
    name: str
    description: str = ""
    # Effective availability under the current allowlist + disabled overlay, so the
    # editor can render the curated state without recomputing it client-side.
    allowlisted: bool = True
    disabled: bool = False


class ToolPolicyResponse(BaseModel):
    tenant_id: str
    allowlist: list[str] = Field(default_factory=list)
    max_tools: int = 0
    disabled_tools: list[str] = Field(default_factory=list)
    # Every tool in the tenant catalog (not just the curated subset) so an admin can
    # build/edit the allowlist against the full surface.
    available_tools: list[ToolPolicyToolEntry] = Field(default_factory=list)


class ToolEnableResponse(BaseModel):
    tenant_id: str
    server: str
    name: str
    enabled: bool


class TenantDeleteResponse(BaseModel):
    tenant_id: str
    db_name: str
    # "deleted" => soft-deleted (retained until purge_at); "purged" => hard-dropped.
    status: str = "deleted"
    deleted: bool = True
    purge_at: datetime | None = None


class TenantRestoreResponse(BaseModel):
    tenant_id: str
    db_name: str
    status: str = "active"
    restored: bool = True


class ServerUpsertRequest(BaseModel):
    tenant_id: str | None = None
    server: str
    transport: Literal["streamable_http", "sse", "stdio", "code"] = "streamable_http"
    endpoint: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    tools: list[ToolDocument] = Field(default_factory=list)


class ServerPatchRequest(BaseModel):
    tenant_id: str | None = None
    transport: Literal["streamable_http", "sse", "stdio", "code"] | None = None
    endpoint: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None
    tools: list[ToolDocument] | None = None


class CodeToolTestRequest(BaseModel):
    tenant_id: str | None = None
    raw_code: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    requirements: list[str] = Field(default_factory=list)
    action_type: Literal["read", "write", "destructive"] = "read"
    requires_confirmation: bool = False


class CodeToolTestResponse(BaseModel):
    ok: bool
    result: dict[str, Any] | None = None
    elapsed_ms: float | None = None
    error: str | None = None
    # Captured sandbox console streams, so the workbench can show print()/log
    # output and diagnostics for an easy debug loop. Capped server-side.
    stdout: str | None = None
    stderr: str | None = None


class CodeToolValidateRequest(BaseModel):
    tenant_id: str | None = None
    name: str = ""
    raw_code: str = ""
    requirements: list[str] = Field(default_factory=list)
    action_type: Literal["read", "write", "destructive"] = "read"
    input_schema: dict[str, Any] = Field(default_factory=dict)


class CodeToolValidationIssue(BaseModel):
    severity: Literal["error", "warning"] = "error"
    message: str
    line: int | None = None


class CodeToolValidateResponse(BaseModel):
    ok: bool
    issues: list[CodeToolValidationIssue] = Field(default_factory=list)
    suggested_schema: dict[str, Any] | None = None


class ExploreCollectionsResponse(BaseModel):
    tenant_id: str
    collections: list[str] = Field(default_factory=list)


class ExploreSampleRequest(BaseModel):
    tenant_id: str | None = None
    collection: str
    limit: int = Field(default=10, ge=1, le=100)


class ExploreSampleResponse(BaseModel):
    tenant_id: str
    collection: str
    limit: int
    field_types: dict[str, str] = Field(default_factory=dict)
    sample_docs: list[dict[str, Any]] = Field(default_factory=list)
    snippet: str = ""


class ExploreQueryRequest(BaseModel):
    tenant_id: str | None = None
    collection: str
    mode: Literal["find", "aggregate"] = "find"
    filter: dict[str, Any] = Field(default_factory=dict)
    pipeline: list[dict[str, Any]] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=100)


class ExploreQueryResponse(BaseModel):
    tenant_id: str
    collection: str
    mode: Literal["find", "aggregate"]
    limit: int
    results: list[dict[str, Any]] = Field(default_factory=list)
    snippet: str = ""


class CacheMigrateRequest(BaseModel):
    tenant_id: str | None = None
    mode: Literal["status", "purge", "reembed"] = "status"
    batch_size: int = Field(default=200, ge=1, le=10_000)


class UserCreateRequest(BaseModel):
    email: str
    password: str
    # None -> the caller's tenant (platform-admin may target any tenant).
    tenant_id: str | None = None
    # Least-privilege default: a bare "user" can authenticate but has no admin
    # console surface. Promote to "admin" (tenant-admin) or, for a platform-admin
    # only, "platform-admin" explicitly.
    roles: list[str] = Field(default_factory=lambda: ["user"])
    scopes: list[str] = Field(default_factory=list)
    status: Literal["active", "disabled"] = "active"
    # Cosmetic only: a human-friendly display name for the credential and the MCP
    # client it was minted for. Neither grants privilege; both aid recognition in
    # the console. ``label`` is bounded to keep the stored doc tidy.
    label: str | None = Field(default=None, max_length=120)
    client: str | None = Field(default=None, max_length=40)


class UserUpdateRequest(BaseModel):
    # All optional: only the provided fields are changed. ``password`` rotates the
    # stored hash; omit it to leave the credential untouched.
    password: str | None = None
    roles: list[str] | None = None
    scopes: list[str] | None = None
    status: Literal["active", "disabled"] | None = None


class UserResponse(BaseModel):
    id: str
    tenant_id: str
    email: str
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    status: str = "active"
    # True for accounts minted by the public self-service sign-up flow.
    self_registered: bool = False
    # Cosmetic display metadata: an operator-supplied name and the MCP client the
    # credential was minted for. Absent on older records (falls back to email).
    label: str | None = None
    client: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by: str | None = None


class UserListResponse(BaseModel):
    # ``None`` when a platform-admin lists across all tenants.
    tenant_id: str | None = None
    items: list[UserResponse] = Field(default_factory=list)


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class UserTokenRequest(BaseModel):
    # Optional override for the minted token's lifetime. Omitted -> the server
    # default (``admin_session_ttl_seconds``). Bounded server-side.
    ttl_minutes: int | None = None


class UserTokenResponse(BaseModel):
    auth_mode: str
    token: str | None = None
    token_type: str = "bearer"
    expires_in: int | None = None
    tenant_id: str
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    # False when roles carry neither ``admin`` nor ``tool:invoke`` (the token
    # authenticates but is rejected at the /rpc + /mcp RBAC gate).
    data_plane_ok: bool = True
    # Mode-specific limitation surfaced to the operator (e.g. jwks roles-only).
    caveat: str | None = None


class DemoUserCreateRequest(BaseModel):
    # All optional: omit everything for a fully auto-generated demo account.
    # ``email`` lets an operator pick a recognizable address; otherwise a unique
    # ``demo-<rand>@demo.local`` is generated. ``tenant_id`` follows the same
    # platform-admin cross-tenant rules as the rest of the user surface.
    email: str | None = None
    tenant_id: str | None = None
    # Cosmetic only (see UserCreateRequest): a recognizable name for the minted
    # credential and the MCP client it targets. Never affect roles or scopes.
    label: str | None = Field(default=None, max_length=120)
    client: str | None = Field(default=None, max_length=40)


class DemoUserCreateResponse(BaseModel):
    user: UserResponse
    # The generated (or operator-set) password, returned exactly once at creation
    # time so it can be handed to the demo consumer. It is never retrievable later.
    password: str
    # True when this run created the account; False if the email already existed
    # (the existing account is returned untouched and ``password`` is empty).
    created: bool = True


class DemoScopesResponse(BaseModel):
    tenant_id: str
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)


class SelfRegisterRequest(BaseModel):
    # The public sign-up surface accepts only an email + password; roles, scopes,
    # tenant, and confirmation tier are pinned server-side (services/registration.py).
    email: str
    password: str


class SelfRegisterResponse(BaseModel):
    user: UserResponse
    tenant_id: str
    # Always "unconfirmed" for a fresh sign-up; surfaced so the client can show the
    # account's tier and the caps it implies.
    confirmation: str
    auth_mode: str
    # A ready-to-use credential for the new account, returned once at sign-up.
    token: str
    token_type: str = "bearer"
    expires_in: int


class EgressAllowlistUpdateRequest(BaseModel):
    # Host globs / exact hosts / IP literals / CIDRs the tenant may reach.
    # An empty list clears the tenant allowlist (global guardrail still applies).
    allowlist: list[str] = Field(default_factory=list)


class EgressAllowlistResponse(BaseModel):
    tenant_id: str
    allowlist: list[str] = Field(default_factory=list)
    # The operator-configured global ceiling, surfaced read-only for context.
    global_allowlist: list[str] = Field(default_factory=list)
    enforced: bool = True
    default_deny: bool = False
    updated_at: datetime | None = None
    updated_by: str | None = None


class PipPolicyUpdateRequest(BaseModel):
    # Bare PyPI distribution names this tenant's code tools may install (e.g.
    # ["requests", "orjson"]). Empty clears the tenant allowlist, which — under
    # the intersection model — means the tenant may install no third-party
    # packages (stdlib only), regardless of the global ceiling.
    allowlist: list[str] = Field(default_factory=list)


class PipPolicyEntry(BaseModel):
    # One curated tenant entry plus whether the operator ceiling currently admits
    # it. ``in_global_ceiling=False`` => the entry is stored but has no effect
    # until a platform operator adds it to SANDBOX_ALLOWED_REQUIREMENTS.
    name: str
    in_global_ceiling: bool = False


class PipPolicyResponse(BaseModel):
    tenant_id: str
    # The tenant's curated allowlist (normalized distribution names).
    allowlist: list[str] = Field(default_factory=list)
    # The operator ceiling (SANDBOX_ALLOWED_REQUIREMENTS), surfaced read-only.
    global_ceiling: list[str] = Field(default_factory=list)
    # What actually installs: ``allowlist ∩ global_ceiling``.
    effective: list[str] = Field(default_factory=list)
    # Per-entry detail so the editor can flag "awaiting operator" entries.
    entries: list[PipPolicyEntry] = Field(default_factory=list)
    # True when the operator ceiling is non-empty (any third-party install is even
    # possible). False => no tenant may install anything until an operator opts in.
    global_restricted: bool = False
    # Whether transport="code" execution is on at all (CODE_TOOL_EXECUTION_ENABLED).
    execution_enabled: bool = False
    updated_at: datetime | None = None
    updated_by: str | None = None


class ServerEnvUpdateRequest(BaseModel):
    # Empty string clears a key; omitted keys are unchanged.
    values: dict[str, str] = Field(default_factory=dict)


class ServerEnvResponse(BaseModel):
    tenant_id: str
    server: str
    keys: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None
    updated_by: str | None = None


class PendingActionResponse(BaseModel):
    action_id: str
    tenant_id: str
    user_id: str
    server: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    action_type: str = "destructive"
    status: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None


class PendingActionListResponse(BaseModel):
    tenant_id: str
    items: list[PendingActionResponse] = Field(default_factory=list)


class QuotaUpdateRequest(BaseModel):
    calls_limit: int = Field(default=0, ge=0)
    sandbox_seconds_limit: int = Field(default=0, ge=0)


class QuotaResponse(BaseModel):
    tenant_id: str
    calls_limit: int = 0
    sandbox_seconds_limit: int = 0


class UsageTotals(BaseModel):
    calls: int = 0
    sandbox_ms: int = 0


class UsageRemaining(BaseModel):
    calls_remaining: int | None = None
    sandbox_seconds_remaining: int | None = None


class UsageResponse(BaseModel):
    tenant_id: str
    period: str
    usage: UsageTotals
    quota: QuotaResponse
    remaining: UsageRemaining


class UsageEventRecord(BaseModel):
    kind: str
    amount: int = 0
    period: str = ""
    ts: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class UsageEventsResponse(BaseModel):
    tenant_id: str
    period: str
    totals_by_kind: dict[str, int] = Field(default_factory=dict)
    total_amount: int = 0
    events: list[UsageEventRecord] = Field(default_factory=list)


class CodeRequirementsPolicySummary(BaseModel):
    """Compact effective code-package policy for the caller's tenant.

    Drives the Functions Studio's per-requirement allow/deny chips and the sandbox
    contract card without a second round trip: the UI marks a requirement allowed
    iff its normalized distribution name is in ``effective``.
    """

    # What actually installs for this tenant: ``allowlist ∩ global_ceiling``.
    effective: list[str] = Field(default_factory=list)
    # The tenant's curated allowlist (may include entries not yet in the ceiling).
    allowlist: list[str] = Field(default_factory=list)
    # The operator ceiling (SANDBOX_ALLOWED_REQUIREMENTS).
    global_ceiling: list[str] = Field(default_factory=list)
    # True when the operator ceiling is non-empty (any third-party install possible).
    global_restricted: bool = False
    # Whether transport="code" execution is enabled at all.
    execution_enabled: bool = False


class HttpEgressPolicySummary(BaseModel):
    """Compact effective outbound-HTTP (``context.http``) policy for the tenant.

    Drives the Sandbox-contract "Network / egress" row: the UI shows whether the
    bridge is enabled and which hosts the tenant's code may reach. Outbound HTTP
    is always deny-by-default — an empty effective set blocks every host.
    """

    # Whether the host-mediated context.http bridge is enabled at all.
    enabled: bool = False
    # Effective reachable hosts: tenant egress allowlist ∩ global ceiling.
    effective: list[str] = Field(default_factory=list)
    # The tenant's curated egress allowlist (may include entries outside the ceiling).
    allowlist: list[str] = Field(default_factory=list)
    # The operator ceiling (EGRESS_GLOBAL_ALLOWLIST).
    global_ceiling: list[str] = Field(default_factory=list)
    # True when the operator ceiling is non-empty.
    global_restricted: bool = False
    # Always True for code egress: every reachable host is an explicit grant.
    default_deny: bool = True


class SandboxBridgesSummary(BaseModel):
    """Which host-mediated sandbox bridges are enabled for this deployment.

    Lets the Functions Studio gate affordances on the *real* server flag instead
    of discovering a disabled bridge only after a failed call: e.g. the DB
    explorer is shown disabled (not broken) when ``db_bridge_enabled`` is false.
    """

    # context.db / the read-only Explore Database surface.
    db_bridge_enabled: bool = False
    # context.tools cross-tool composition.
    tool_bridge_enabled: bool = False
    # context.http outbound egress (mirrors http_egress.enabled for convenience).
    http_bridge_enabled: bool = False


class WhoAmIResponse(BaseModel):
    tenant_id: str
    user_id: str
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    is_platform_admin: bool
    # True for a read-only (viewer) console principal: the UI uses this to hide
    # mutating affordances. The server-side 403 in RbacMiddleware is the real guard.
    is_read_only: bool = False
    # True when the caller's tenant itself is read-only (so even a full admin sees
    # tenant-scoped mutations refused, while platform-admin can still toggle it off).
    tenant_read_only: bool = False
    # The caller tenant's confirmation tier ("unconfirmed" => capped self-service
    # account; "confirmed" => promoted/normal). Lets the console show the tier.
    confirmation: str = "confirmed"
    # Effective code-package policy, so the Functions Studio can render
    # allow/deny chips and contract context without an extra request.
    code_requirements: CodeRequirementsPolicySummary = Field(
        default_factory=CodeRequirementsPolicySummary
    )
    http_egress: HttpEgressPolicySummary = Field(default_factory=HttpEgressPolicySummary)
    # Which sandbox bridges this deployment enables, so the Studio can disable
    # (and explain) capability-off affordances up front instead of failing late.
    sandbox: SandboxBridgesSummary = Field(default_factory=SandboxBridgesSummary)
    auth_mode: Literal["hs256", "jwks"]


class CatalogListRequest(BaseModel):
    tenant_id: str | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class CatalogItemResponse(BaseModel):
    server: str
    name: str
    description: str = ""
    scopes: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    transport: str | None = None
    action_type: str | None = None
    updated_at: datetime | None = None


class CatalogListResponse(BaseModel):
    tenant_id: str
    items: list[CatalogItemResponse]
    total: int
    limit: int
    offset: int


class TelemetryListRequest(BaseModel):
    tenant_id: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class TelemetryEventResponse(BaseModel):
    timestamp: datetime | None = None
    tenant_id: str
    user_id: str
    request_id: str | None = None
    method: str
    status: str
    latency_ms: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TelemetryListResponse(BaseModel):
    tenant_id: str
    items: list[TelemetryEventResponse]


class TenantStats(BaseModel):
    tenant_id: str
    server_count: int
    enabled_server_count: int
    tool_count: int


class StatsResponse(BaseModel):
    tenant_count: int | None = None
    catalog_version: int
    telemetry_status_counts: dict[str, int] = Field(default_factory=dict)
    tenants: list[TenantStats] = Field(default_factory=list)


class AdminSearchRequest(BaseModel):
    tenant_id: str | None = None
    server: str | None = None
    query: str
    limit: int = Field(default=10, ge=1, le=50)
    mode: Literal["hybrid", "vector", "text"] = "hybrid"
    vector_weight: float | None = None
    text_weight: float | None = None


EmbeddingProvider = Literal["ollama", "openai", "azure_openai", "voyage", "gemini"]


class EmbeddingConfigResponse(BaseModel):
    provider: EmbeddingProvider
    model: str
    base_url: str | None = None
    dimensions: int
    embedding_version: str
    api_key_set: bool = False
    api_key_hint: str | None = None
    azure_endpoint: str | None = None
    azure_api_version: str | None = None
    azure_deployment: str | None = None
    supported_providers: list[str] = Field(default_factory=list)
    source: str = "env"
    # How the stored API key is protected at rest: "per-tenant-dek" (Queryable
    # Encryption DEK), "shared-fernet" (deployment-wide key), or None when no key
    # is stored. Purely informational for the admin UI; the scheme is automatic.
    secret_encryption: str | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None
    reprovision: dict[str, Any] = Field(default_factory=dict)


class EmbeddingConfigUpdateRequest(BaseModel):
    provider: EmbeddingProvider
    # None means "leave unchanged"; for a provider switch the per-provider default
    # model is applied automatically.
    model: str | None = None
    base_url: str | None = None
    # None preserves the stored key; "" clears it; any other value replaces it.
    api_key: str | None = None
    azure_endpoint: str | None = None
    azure_api_version: str | None = None
    azure_deployment: str | None = None
    # Whether to kick off the catalog/cache/guardrail reprovision after saving.
    reprovision: bool = True


class EmbeddingTestRequest(BaseModel):
    provider: EmbeddingProvider
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    azure_endpoint: str | None = None
    azure_api_version: str | None = None
    azure_deployment: str | None = None


class EmbeddingTestResponse(BaseModel):
    ok: bool
    provider: str
    model: str
    dimensions: int | None = None
    embedding_version: str | None = None
    message: str


# --------------------------------------------------------------------------- #
#  Admin analytics (gateway/routers/admin/analytics.py)                        #
# --------------------------------------------------------------------------- #
class AnalyticsOverviewResponse(BaseModel):
    """Headline numbers for the dashboard.

    ``scope`` is ``"platform"`` for a platform-admin (cross-tenant rollup) or
    ``"tenant"`` for a tenant-admin (their tenant only). Beta-headroom fields are
    populated only for the platform scope (they are global counters).
    """

    scope: Literal["platform", "tenant"]
    period: str
    tenant_count: int
    calls: int = 0
    sandbox_ms: int = 0
    confirmed_count: int | None = None
    unconfirmed_count: int | None = None
    self_registered_count: int | None = None
    self_registration_max_tenants: int | None = None


class UsageTrendPoint(BaseModel):
    period: str
    calls: int = 0
    sandbox_ms: int = 0


class UsageTrendResponse(BaseModel):
    scope: Literal["platform", "tenant"]
    points: list[UsageTrendPoint] = Field(default_factory=list)


class TopToolEntry(BaseModel):
    server: str
    # None for the "top servers" rollup, which groups by server only.
    tool: str | None = None
    calls: int = 0


class TopToolsResponse(BaseModel):
    scope: Literal["platform", "tenant"]
    period: str
    tools: list[TopToolEntry] = Field(default_factory=list)
    servers: list[TopToolEntry] = Field(default_factory=list)


class TelemetryTrendPoint(BaseModel):
    bucket: datetime
    total: int = 0
    errors: int = 0
    latency_avg_ms: float | None = None
    latency_p95_ms: float | None = None


class TelemetryTrendResponse(BaseModel):
    scope: Literal["platform", "tenant"]
    points: list[TelemetryTrendPoint] = Field(default_factory=list)


class QuotaUtilizationEntry(BaseModel):
    tenant_id: str
    period: str
    calls: int = 0
    calls_limit: int = 0
    sandbox_ms: int = 0
    sandbox_seconds_limit: int = 0
    # 0-100, or None when the corresponding limit is 0 (unlimited).
    calls_utilization_pct: float | None = None
    sandbox_utilization_pct: float | None = None


class QuotaUtilizationResponse(BaseModel):
    scope: Literal["platform", "tenant"]
    period: str
    tenants: list[QuotaUtilizationEntry] = Field(default_factory=list)
