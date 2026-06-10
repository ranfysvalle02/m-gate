# API Reference

This gateway exposes two API surfaces:

- **REST/OpenAPI surface** on the main FastAPI app (`/health`, `/metrics`, `/admin`, `/ui`).
- **JSON-RPC gateway surface** at `POST /rpc` for MCP-style tool discovery and invocation.

The mounted FastMCP app under `/mcp` is a separate sub-application and is not
represented in OpenAPI docs.

## Authentication Model

`AUTH_MODE` controls bearer-token behavior:

- `disabled`: REST and JSON-RPC are open (admin UI still uses session auth).
- `hs256`: bearer JWT required except public/observability paths.
- `jwks`: bearer JWT verified via JWKS (remote `JWKS_URI` or local `JWKS_LOCAL_PATH`).

Observability endpoints remain reachable without bearer auth:

- `GET /health`
- `GET /health/live`
- `GET /health/ready`
- `GET /metrics`

Useful request headers:

- `Authorization: Bearer <token>`
- `X-Tenant-Id: <tenant>` (used by admin routes that accept tenant scoping)
- `X-MCP-Query: <text>` (semantic shortlist for `tools/list`)
- `X-MCP-Scopes: scopeA,scopeB` (used in `auth_mode=disabled`)

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
  - `GET /admin/tenants/{tenant_id}/sandbox-secrets` — list configured secret keys
    for code-tool sandbox execution (values are always redacted).
  - `PUT /admin/tenants/{tenant_id}/sandbox-secrets` — upsert per-tenant sandbox
    secrets (`{"values":{"KEY":"value"}}`, empty string clears a key). Platform-admin
    may target any tenant; tenant-admins are scoped to their own tenant.
  - `GET /admin/tenants/{tenant_id}/usage` — per-period metered usage (`calls`,
    `sandbox_ms`) with effective quota and remaining budget.
  - `PUT /admin/tenants/{tenant_id}/quota` — set tenant quota ceilings
    (`calls_limit`, `sandbox_seconds_limit`); platform-admin only.
  - `POST /admin/tenants/{tenant_id}/suspend` — abuse kill-switch: block the tenant
    from the `/rpc` and `/mcp` data planes (optional body `{"reason":"..."}`);
    platform-admin only.
  - `POST /admin/tenants/{tenant_id}/resume` — lift a suspension; platform-admin only.
  - `GET /admin/tenants` responses include `status` (`active`/`suspended`) and, when
    suspended, `suspended_reason`. Suspension takes effect within
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
  - **Code-backed tools** (`transport="code"`): a server may host user-authored
    Python functions instead of a downstream endpoint. Each entry in `tools[]`
    carries `raw_code`, pinned `requirements[]` (`name==version`), and
    `metadata.action_type` (`read`/`write`/`destructive`) / `metadata.requires_confirmation`.
    On save, source is statically linted (size cap, blocked dangerous
    imports/`exec`/`open`/dunder-escapes, pinned-requirements only) and **encrypted at
    rest**; list responses redact source to a `has_raw_code` flag while
    `GET /admin/servers/{server_name}` decrypts it for the editor. Code tools are
    discoverable via `tools/list`/search. When `CODE_TOOL_EXECUTION_ENABLED=true`,
    `tools/call` executes them in the WebAssembly sandbox runtime (`CODE_EXECUTOR=wasm`);
    when false, the call returns `"code_execution_not_enabled"`.
- **Users** (admin principal required; tenant-admins are scoped to their own tenant)
  - `POST /admin/users` — create a user (`email`, `password`, optional `tenant_id`,
    `roles`, `scopes`, `status`). Only `platform-admin` may grant the `platform-admin`
    role or target another tenant.
  - `GET /admin/users` — list users (platform-admin sees all; pass `tenant_id` to scope).
  - `GET /admin/users/{id}` — fetch a user.
  - `PATCH /admin/users/{id}` — update `roles`, `scopes`, `status`, or `password`
    (admin-initiated password reset). Setting `status` to `disabled` revokes the user on
    the `/rpc` plane on their next request (a standing token returns `403 Account
    suspended`), without waiting for token expiry.
  - `DELETE /admin/users/{id}` — delete a user (self-deletion is rejected).
  - `POST /admin/users/me/password` — self-service password change (`current_password`,
    `new_password`); unavailable for the env bootstrap admin.
- **Catalog and telemetry**
  - `GET /admin/catalog`
  - `POST /admin/search`
  - `GET /admin/telemetry`
  - `GET /admin/stats`
- **Identity**
  - `GET /admin/whoami`
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

### `tools/call`

Invokes downstream tool through the proxy registry.

For `transport="code"` catalog entries:

- `CODE_TOOL_EXECUTION_ENABLED=false` -> JSON-RPC error
  `{"reason":"code_execution_not_enabled"}`.
- `CODE_TOOL_EXECUTION_ENABLED=true` + `CODE_EXECUTOR=wasm` -> executes inside a
  throwaway WebAssembly sandbox worker with per-call CPU/memory/wall/output limits.
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

## `/mcp` Sub-Application Note

The mounted FastMCP transport under `/mcp` is intentionally outside the OpenAPI
schema generated by FastAPI. Use this document and MCP-compatible clients for
that surface; use `/docs` or `/redoc` for REST/admin endpoints.
