# API Reference

This gateway exposes two API surfaces:

- **REST/OpenAPI surface** on the main FastAPI app (`/health`, `/metrics`, `/admin`, `/ui`).
- **JSON-RPC gateway surface** at `POST /rpc` for MCP-style tool discovery and invocation.

The mounted FastMCP app under `/mcp` is a separate sub-application and is not
represented in OpenAPI docs.

## Authentication Model

Security is always enforced; `AUTH_MODE` controls how the required bearer token is verified:

- `hs256` (default): bearer JWT verified against `JWT_SECRET`, required except public/observability paths.
- `jwks`: bearer JWT verified via JWKS (remote `JWKS_URI` or local `JWKS_LOCAL_PATH`).

### Inbound MCP-client auth (username/password + OAuth)

MCP clients can authenticate to the gateway surface (`/rpc`, `/mcp`) with a
username/password, in addition to presenting a pre-issued bearer:

- `POST /auth/token` — OAuth2 Resource Owner Password Credentials grant. Accepts
  `application/x-www-form-urlencoded` (`grant_type=password`, `username`, `password`) or
  JSON. Returns `{"access_token","token_type":"bearer","expires_in"}`; send the
  `access_token` as `Authorization: Bearer <token>`. `400` for a non-password grant or
  missing fields; `401` for bad credentials. Always reachable (any `AUTH_MODE`).
- `MCP_BASIC_AUTH_ENABLED=true` lets clients send HTTP Basic directly on `/rpc`/`/mcp`.
- `GET /.well-known/oauth-protected-resource` — RFC 9728 Protected Resource Metadata
  describing the configured authorization server. Returned when `AUTH_MODE=jwks` or
  `OAUTH_METADATA_ENABLED=true`; otherwise `404`. The gateway is an OAuth2/OIDC *resource
  server* (bring your own IdP) and does not implement an authorization server.

Authorization: reaching `/rpc` requires `admin`, `tool:invoke`, or `tool:read`;
**calling** a tool (`tools/call`) additionally requires `admin` or `tool:invoke`
(`tool:read` is discover-only). See AUTH.md for the full role matrix.

Observability endpoints remain reachable without bearer auth:

- `GET /health`
- `GET /health/live`
- `GET /health/ready`
- `GET /metrics`

Useful request headers:

- `Authorization: Bearer <token>`
- `X-Tenant-Id: <tenant>` (used by admin routes that accept tenant scoping)
- `X-MCP-Query: <text>` (semantic shortlist for `tools/list`)

## REST Endpoints

## Health and Metrics

- `GET /health/live`: liveness check (`{"status":"ok"}`).
- `GET /health/ready`: readiness check with `checks` and `errors`.
- `GET /health`: shorthand live health.
- `GET /metrics`: Prometheus exposition payload (`ENABLE_METRICS=true`).

## Admin API

Most admin routes require authenticated principal context. Embedding routes
require the `platform-admin` role.

- **Tenants**
  - `POST /admin/tenants`
  - `GET /admin/tenants`
  - `DELETE /admin/tenants/{tenant_id}` — permanently deprovision a tenant
    (drops tenant DB and removes control-plane tenant record); platform-admin only.
  - `GET /admin/tenants/{tenant_id}/usage` — per-period metered usage (`calls`,
    `sandbox_ms`) with effective quota and remaining budget.
  - `GET /admin/tenants/{tenant_id}/usage/events` — per-period billing-event
    rollup (`totals_by_kind`) plus recent raw events (`kind`, `amount`, `ts`,
    metadata) for auditing and cost attribution.
  - `PUT /admin/tenants/{tenant_id}/quota` — set tenant quota ceilings
    (`calls_limit`, `sandbox_seconds_limit`); platform-admin only.
  - `POST /admin/tenants/{tenant_id}/suspend` — abuse kill-switch: block the tenant
    from the `/rpc` and `/mcp` data planes (optional body `{"reason":"..."}`);
    platform-admin only.
  - `POST /admin/tenants/{tenant_id}/resume` — lift a suspension; platform-admin only.
  - `POST /admin/tenants/{tenant_id}/read-only` — freeze the tenant: it stays
    `active` and discoverable, but `tools/call` and tenant-scoped config
    mutations are refused (optional body `{"reason":"..."}`); platform-admin only.
  - `POST /admin/tenants/{tenant_id}/read-write` — lift a read-only freeze;
    platform-admin only.
  - `GET /admin/tenants/{tenant_id}/tool-policy` — the tenant's curation policy:
    `{allowlist, max_tools, disabled_tools, available_tools}`, where
    `available_tools` is the tenant catalog annotated with `allowlisted`/`enabled`.
    Platform-admin for any tenant; tenant-admin for their own.
  - `PUT /admin/tenants/{tenant_id}/tool-policy` — replace the allowlist + cap
    (`{"allowlist":["orders/*","weather/forecast"],"max_tools":10}`). An empty
    allowlist means unrestricted; `max_tools=0` means unlimited. Entries are
    normalized/de-duplicated. Refused for tenant-admins when the tenant is
    read-only (platform-admin bypasses).
  - `GET /admin/tenants` responses include `status` (`active`/`suspended`), when
    suspended `suspended_reason`, plus `read_only` and (when frozen)
    `read_only_reason`. Suspension/read-only take effect within
    `TENANT_STATUS_CACHE_TTL_SECONDS` across replicas (immediately on the acting node).
  - `GET /admin/tenants/{tenant_id}/egress-allowlist` — the tenant's downstream egress
    allowlist plus the deployment-wide `global_allowlist`, `enforced`, and `default_deny`
    flags. Platform-admin for any tenant; tenant-admin for their own.
  - `PUT /admin/tenants/{tenant_id}/egress-allowlist` — replace the tenant allowlist
    (`{"allowlist":["*.corp.example","api.vendor.com","203.0.113.0/24"]}`). Entries are
    normalized + validated (host globs, exact hosts, IP literals, CIDRs); malformed
    entries return `422`. The tenant list intersects with `EGRESS_GLOBAL_ALLOWLIST` (it
    can only narrow the global ceiling). Egress is enforced when registering downstream
    `streamable_http`/`sse` servers (disallowed endpoints → `422`) and authoritatively at
    connect time with DNS re-resolution + IP pinning. Same RBAC scoping as the GET.
- **Human-in-the-loop approvals** (tenant-admin or platform-admin required)
  - `GET /admin/actions?status=pending` — list pending/approved/rejected actions
    for the active tenant.
  - `POST /admin/actions/{action_id}/approve` — mark a pending action approved.
  - `POST /admin/actions/{action_id}/reject` — mark a pending action rejected.
  - Requesters cannot approve/reject their own actions.
- **Server registry**
  - `POST /admin/servers`
  - `GET /admin/servers`
  - `GET /admin/servers/{server_name}`
  - `PATCH /admin/servers/{server_name}`
  - `DELETE /admin/servers/{server_name}`
  - `POST /admin/servers/{server_name}/enable` — mount the virtual server for the
    tenant. `POST /admin/servers/{server_name}/disable` — unmount it (it stays
    registered but stops serving). Tenant-admins may toggle their own
    `tenant`-origin servers; `platform`-origin servers require platform-admin.
    Refused when the tenant is read-only (platform-admin bypasses). Optional
    `tenant_id` query for platform-admin cross-tenant.
  - `GET /admin/servers/{server_name}/export` — download the code server as a
    self-contained, runnable FastMCP project (`application/zip`). Bundles every
    tool plus the transitive closure of sibling code tools they call via
    `context.tools`/`context.call`, reconstructs `context` locally
    (`context.db` via `pymongo`, `context.env`, in-process `context.tools`), and
    pins `fastmcp`/`pymongo` + each tool's requirements. Secrets are never
    exported — only `context.env` key *names* land in `.env.example`. Response
    headers include `X-Export-Tool-Count` and `X-Export-Servers`. Tenant-admin
    gated; platform-admin may target any tenant via `tenant_id`.
  - `GET /admin/servers/{server_name}/env` — list configured per-server env keys
    for code-tool sandbox execution and downstream auth secrets (values are always redacted;
    optional `tenant_id` query).
  - `PUT /admin/servers/{server_name}/env` — upsert per-server encrypted env
    values (`{"values":{"KEY":"value"}}`, empty string clears a key). Platform-admin
    may target any tenant via `tenant_id`; tenant-admins are scoped to their own tenant.
  - Downstream auth is configured on each server via `metadata.auth` and is limited to a
    gateway-brokered workload identity:
    - `scheme`: `jwt` (default) or `none`
    - `jwt`: optional `audience`
    - `none`: the downstream service or the tenant presents its own credential; the gateway
      injects nothing. Third-party API keys / passwords / OAuth secrets are not brokered
      per-server by the gateway.
  - Save-time validation rejects unknown schemes and the `jwt` bearer over a credentialed
    `http://` endpoint unless `DOWNSTREAM_ALLOW_INSECURE_CREDENTIALS=true`.
  - Example snippets:
    - Workload JWT: `{"auth":{"scheme":"jwt","audience":"downstream-service"}}`
    - Downstream owns its auth: `{"auth":{"scheme":"none"}}`
  - **Code-backed tools** (`transport="code"`): a server may host user-authored
    Python functions instead of a downstream endpoint. Each entry in `tools[]`
    carries `raw_code`, pinned `requirements[]` (`name==version`), and
    `metadata.action_type` (`read`/`write`/`destructive`) / `metadata.requires_confirmation`.
    An entry may also set `metadata.always_included=true` to pin the tool to the
    top of every routed result (`tools/search`, `tools/list?query=…`,
    `/mcp search_tools`) regardless of relevance; pins are still scope-filtered
    and count against the caller's result `limit`.
    On save, source is statically linted (size cap, blocked dangerous
    imports/`exec`/`open`/dunder-escapes, pinned-requirements only) and **encrypted at
    rest**; list responses redact source to a `has_raw_code` flag while
    `GET /admin/servers/{server_name}` decrypts it for the editor. Code tools are
    discoverable via `tools/list`/search. When `CODE_TOOL_EXECUTION_ENABLED=true`,
    `tools/call` executes them in the WebAssembly sandbox runtime (`CODE_EXECUTOR=wasm`);
    when false, the call returns `"code_execution_not_enabled"`. Runtime now
    supports a tenant-scoped virtual DB bridge (`context.db[...]`) that relays
    through the host process (sandbox remains network-isolated), with host-side
    operation allowlists enforced by `metadata.action_type`.
  - `GET /admin/explore/collections` — list tenant collections (excluding
    `system.*`) for the code-tool authoring assistant.
  - `POST /admin/explore/sample` — return bounded sample docs + inferred field
    types + a generated `context.db[...]` snippet.
  - `POST /admin/explore/query` — execute read-only `find` / `aggregate` via the
    same host-side bridge policy and return results + copy/paste snippet.
- **Tools** (per-tool kill-switch; tenant-admin or platform-admin)
  - `POST /admin/tools/{server}/{name}/disable` — add the tool to the tenant
    `disabled_tools` overlay. A disabled tool is hidden from discovery and refused
    on `tools/call` for **everyone, including admins** (`tool_disabled`).
  - `POST /admin/tools/{server}/{name}/enable` — remove the tool from the overlay.
    Both return `{tenant_id,server,name,enabled}`. Refused when the tenant is
    read-only (platform-admin bypasses); optional `tenant_id` query for
    platform-admin cross-tenant.
- **Users** (admin principal required; tenant-admins are scoped to their own tenant).
  User mutations (`create`, `demo`/`viewer` one-click, `update`, `delete`) are
  refused for tenant-admins when the tenant is **read-only** (platform-admin
  bypasses); token minting and self-service password change stay available.
  - `POST /admin/users` — create a user (`email`, `password`, optional `tenant_id`,
    `roles`, `scopes`, `status`, and optional cosmetic `label`/`client`). Only
    `platform-admin` may grant the `platform-admin` role or target another tenant.
    `label` (display name) and `client` (the MCP client it was minted for) are
    recognition-only and never consulted for authorization.
  - `POST /admin/users/demo` — one-click: create a ready-to-use, tool-invoking
    **full-access** credential. Optional body `{"email"?, "tenant_id"?, "label"?,
    "client"?}`; email/tenant are auto-resolved when omitted (a unique
    `demo-<rand>@demo.local` is generated). Grants `user` + `tool:invoke` plus
    **catalog-derived scopes** (`server:*` and every distinct tool scope in the
    tenant) so the account can discover *and* invoke tools immediately. Returns the
    generated `password` exactly once (never retrievable later) alongside the created
    user. Same tenant-scoping RBAC as the other user routes; the creation is
    audit-logged (the password is never logged). This backs the **Full access** tier
    in the Credentials tab.
  - `POST /admin/users/team` — one-click: create a safe-to-share, **read-only**
    credential. Same optional body as `/users/demo`, grants `user` + `tool:invoke`
    but with `derive_safe_scopes` (read-only tools only — every write/destructive
    tool's scope is dropped), so per-call authorization refuses anything that
    mutates. Backs the **Read-only** tier in the Credentials tab.
  - `POST /admin/users/viewer` — one-click: create the complete **read-only**
    showcase identity (the **Explore** tier in the Credentials tab). Same optional
    body and catalog-derived scopes (so discovery is complete) as `/users/demo`, but
    the account carries `user` + `viewer` + `tool:read` (never `tool:invoke`). The
    one credential is read-only on **both** planes: `viewer` makes it a read-only
    **console** login (loads the UI + tool source, every mutation `403`s) and
    `tool:read` makes a minted token a discover-only **MCP** principal
    (`tools/list` / `tools/search` work; `tools/call` → `invoke_not_permitted`).
    Returns the generated `password` once.
  - `GET /admin/users/demo-scopes` — recommended demo `roles`/`scopes` for a tenant
    (optional `tenant_id` query), derived from its live catalog. Backs the console's
    **Demo** role preset so a manually-created demo user gets working scopes.
  - `GET /admin/users` — list users (platform-admin sees all; pass `tenant_id` to scope).
  - `GET /admin/users/{id}` — fetch a user.
  - `PATCH /admin/users/{id}` — update `roles`, `scopes`, `status`, or `password`
    (admin-initiated password reset). Setting `status` to `disabled` revokes the user on
    the `/rpc` plane on their next request (a standing token returns `403 Account
    suspended`), without waiting for token expiry.
  - `DELETE /admin/users/{id}` — delete a user (self-deletion is rejected).
  - `POST /admin/users/me/password` — self-service password change (`current_password`,
    `new_password`); unavailable for the env bootstrap admin.
  - `POST /admin/users/{id}/token` — mint a ready-to-use bearer token carrying that
    user's `tenant_id`, `roles`, and `scopes` (the "Get config" button in the
    Credentials tab). Optional body `{"ttl_minutes": <int>}` (clamped server-side). Same RBAC as the
    other user routes (tenant-admins scoped to their own tenant; never a platform-admin
    user). Auth-mode aware: `hs256` returns a real scoped bearer; `jwks` returns a
    roles-only admin-session token plus a `caveat` (scoped tokens come from your IdP).
    Every mint is recorded in a structured audit log line (actor, target, tenant,
    roles, ttl). The response flags `data_plane_ok=false` when the account lacks
    `admin`/`tool:invoke`, so the token authenticates but is rejected at the
    `/rpc` + `/mcp` gate.
- **Catalog and telemetry**
  - `GET /admin/catalog`
  - `POST /admin/search` (optional `server` filter for selected-server workspace search)
  - `GET /admin/telemetry`
  - `GET /admin/stats`
- **Identity**
  - `GET /admin/whoami` — caller identity: `tenant_id`, `user_id`, `roles`,
    `scopes`, `is_platform_admin`, `auth_mode`, plus `is_read_only` (the caller is
    a read-only `viewer` principal) and `tenant_read_only` (the active tenant is
    frozen — surfaced even to full admins). The console uses these to drive its
    read-only banner and hide mutating affordances.
- **Cache maintenance**
  - `POST /admin/cache/migrate`
- **Embedding control plane** (`platform-admin` required)
  - `GET /admin/embedding`
  - `PUT /admin/embedding`
  - `POST /admin/embedding/test`
  - `GET /admin/embedding/status`

See `/docs` for request/response schemas of REST endpoints.

## JSON-RPC Gateway (`POST /rpc`)

Envelope format follows JSON-RPC 2.0:

```json
{
  "jsonrpc": "2.0",
  "id": "request-id",
  "method": "tools/list",
  "params": {}
}
```

Supported methods:

- `initialize`
- `notifications/tools/list_changed`
- `tools/list`
- `tools/search`
- `tools/call`

Server-scope authorization model:

- discovery (`tools/list`, `tools/search`) is filtered by `server:<name>` scopes,
  and further by the tenant tool policy (allowlist + `disabled_tools` are hidden)
- invocation (`tools/call`) requires the `tool:invoke` role (or `admin`), the tool
  to be within the tenant allowlist (when set) and not disabled, and
  `server:<server>` or `server:*` in caller scopes
- a read-only tenant keeps discovery open but refuses `tools/call`
  (`tenant_read_only`)

### `initialize`

Returns protocol capabilities and gateway identity.

Example:

```bash
curl -s -X POST http://localhost:8000/rpc \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc":"2.0",
    "id":"init-1",
    "method":"initialize",
    "params":{}
  }'
```

### `tools/list`

Returns either:

- full catalog pagination (`cursor`, `next_cursor`) when no query provided, or
- semantic shortlist when `params.query` or `X-MCP-Query` is set.

Request params:

- `query` (optional)
- `limit` (optional)
- `scopes` (optional list)
- `cursor` (optional string offset)
- `client_catalog_version` (optional int)

### `tools/search`

Semantic/lexical retrieval from tool catalog.

Request params:

- `query` (required)
- `limit` (optional, default 10)
- `vector_weight` (optional)
- `text_weight` (optional)
- `scopes` (optional list)
- `mode` (`hybrid` | `vector` | `text`, default `hybrid`)

Tools flagged `metadata.always_included` are pinned to the top of the results
(each tagged `pinned: true`) regardless of `mode` or score, after the same scope
filter is applied, and count against `limit`. Disable globally with
`HYBRID_PIN_ALWAYS_INCLUDED=false`.

### `tools/call`

Invokes downstream tool through the proxy registry.

For `transport="code"` catalog entries:

- `CODE_TOOL_EXECUTION_ENABLED=false` -> JSON-RPC error
  `{"reason":"code_execution_not_enabled"}`.
- `CODE_TOOL_EXECUTION_ENABLED=true` + `CODE_EXECUTOR=wasm` -> executes inside a
  WebAssembly sandbox worker with per-call CPU/memory/wall/output limits
  (throwaway worker or warm pooled worker depending on `SANDBOX_POOL_SIZE`).
- When `SANDBOX_DB_BRIDGE_ENABLED=true`, code tools can call `context.db` from
  sandboxed Python. DB operations remain tenant-scoped and host-enforced by
  action type (`read`/`write`/`destructive`).
- Per-server encrypted env values are injected into sandboxed code as
  `context.env["KEY"]` / `context.env.get("KEY")`.
- When `SANDBOX_TOOL_BRIDGE_ENABLED=true`, code tools can call sibling code
  tools in the same tenant via `context.tools[<server>][<tool>](**kwargs)` /
  `context.call(<server>, <tool>, **kwargs)`. Each relayed call is
  re-authorized against the original caller's scopes, restricted to
  `transport="code"` servers, refuses confirmation-gated tools, and is bounded
  by `SANDBOX_TOOL_CALL_MAX_DEPTH` + `SANDBOX_TOOL_MAX_CALLS_PER_INVOCATION`.
- For any tool call, tenant quota is enforced in-band. When exceeded, `tools/call`
  returns JSON-RPC `RATE_LIMITED` (`-32029`) with
  `{"reason":"quota_exceeded","usage":...,"quota":...}`.
- For catalog entries where `metadata.requires_confirmation=true`:
  - Without `confirmation_id`, the gateway does **not** execute the tool. It
    persists a pending action and returns a JSON-RPC result frame:
    `{"status":"confirmation_required","confirmation":{"action_id":"...","expires_at":"..."}}`.
  - An admin approves/rejects via `/admin/actions/{action_id}/approve|reject`.
  - Caller re-invokes `tools/call` with the same request plus
    `confirmation_id=<action_id>`. Execution proceeds only when the action is
    approved, unexpired, and argument-matched.

Request params:

- `server` (required)
- `name` (required)
- `arguments` (optional object, default `{}`)
- `confirmation_id` (optional; required on the second call for approval-gated tools)

Example:

```bash
curl -s -X POST http://localhost:8000/rpc \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc":"2.0",
    "id":"call-1",
    "method":"tools/call",
    "params":{
      "server":"weather",
      "name":"get_weather",
      "arguments":{"city":"Montreal"}
    }
  }'
```

### `notifications/tools/list_changed`

Returns current `catalog_version` and whether the catalog changed.

## JSON-RPC Error Codes

Gateway-specific and standard error codes:

- `-32700` parse error
- `-32600` invalid request
- `-32601` method not found
- `-32602` invalid params
- `-32603` internal error
- `-32001` unauthorized
- `-32003` forbidden
- `-32004` upstream timeout
- `-32029` rate limited

Common cases:

- Missing/invalid token: `401` (or `503` when JWKS is unavailable).
- Unknown tenant: JSON-RPC error with `tenant_not_provisioned` metadata.
- Downstream timeout: JSON-RPC `UPSTREAM_TIMEOUT`.
- Tenant quota exceeded: JSON-RPC `RATE_LIMITED` with `reason=quota_exceeded`.
- Suspended tenant: JSON-RPC `FORBIDDEN` (`-32003`) with `reason=tenant_suspended`
  (and `detail` carrying the operator-supplied reason when present).
- Read-only tenant: JSON-RPC `FORBIDDEN` (`-32003`) with `reason=tenant_read_only`
  on `tools/call` (discovery still succeeds).
- Tool refused by policy: `FORBIDDEN` (`-32003`) with `reason` one of
  `invoke_not_permitted` (caller lacks `tool:invoke`), `tool_not_allowlisted`
  (outside the tenant allowlist), or `tool_disabled` (per-tool kill-switch).

## `/mcp` Sub-Application Note

The mounted FastMCP transport under `/mcp` is intentionally outside the OpenAPI
schema generated by FastAPI. Use this document and MCP-compatible clients for
that surface; use `/docs` or `/redoc` for REST/admin endpoints.

### `/mcp` meta-tool authorization and tenant binding

The meta-tools (`search_tools`, `list_catalog_tools`, `call_downstream_tool`)
enforce the same authorization as the `/rpc` data plane:

- **Coarse RBAC:** the caller must carry `admin`, `tool:invoke`, or `tool:read`,
  and a suspended account is cut off — same gate as `/rpc`. `tool:read` can
  discover (`search_tools`, `list_catalog_tools`) but `call_downstream_tool` is
  refused (`invoke_not_permitted`).
- **Tenant binding:** the tenant is taken from the verified token claim. Passing a
  `tenant_id` argument that does not match the authenticated tenant raises a FastMCP
  `ToolError` (`cross_tenant_forbidden`). Cross-tenant access is not available on
  `/mcp`; use the platform-admin `/admin` API.
- **Per-call authorization:** `call_downstream_tool` runs `authorize_tool_call`, so
  the named `(server, tool)` must exist in the tenant catalog, not be disabled, be
  within the tenant allowlist (when set), and the caller must carry `tool:invoke`
  and satisfy its required scopes (or be `admin`); otherwise it raises a `ToolError`
  (`forbidden: <reason>`). A suspended tenant raises before authorization; a
  read-only tenant raises `tenant_read_only` before authorization.
- **Quota enforcement:** after authorization, `call_downstream_tool` checks the
  tenant usage quota and raises a `ToolError` (`quota_exceeded: <reason>`) when the
  ceiling is reached — `/mcp` cannot be used to bypass the limits enforced on
  `/rpc`. Successful calls are metered (usage counter + billing event) identically
  to `/rpc`.
- **Audit:** every `/mcp` tool call — success, `forbidden`, `quota_exceeded`, and
  `tenant_suspended` — is written to the `audit_telemetry` time-series under the
  same `method="tools/call"` label as `/rpc`, so a single audit query spans both
  surfaces.
