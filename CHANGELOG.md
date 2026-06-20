```# Changelog

## Unreleased

### Feature: `context.http` — opt-in, host-mediated outbound HTTP for code tools
- **New `context.http` resource.** Code tools can now make outbound HTTPS calls
  (`context.http.get/head/post/put/patch/delete(url, params=, headers=, auth=, json=)`)
  returning a small response object (`.status`, `.ok`, `.headers`, `.text`,
  `.content`, `.json()`). The wasm sandbox still has **no sockets**; each call is
  relayed over the existing `/job/rpc` file bridge to a new host-side
  `services/sandbox_http_bridge.py`. Off by default (`SANDBOX_HTTP_BRIDGE_ENABLED`).
- **Reuses the egress firewall, always fail-closed.** The host makes the call
  through `PinnedEgressTransport` in a new code-egress mode
  (`services/egress_policy.py::build_code_egress_rules`) that forces
  `enabled=True` + `default_deny=True` + `require_tenant_allowlist=True`
  **regardless of `EGRESS_ALLOWLIST_ENABLED`** — SSRF denylist + `EGRESS_GLOBAL_ALLOWLIST`
  (platform ceiling) intersected with the per-tenant egress allowlist + DNS-rebinding-proof
  IP pinning, re-validated on every redirect hop. An empty effective allowlist
  (`tenant ∩ global = ∅`) blocks every host. **https only.**
- **Server-side secret injection.** `auth="ENV_KEY"` resolves the value from the
  server's encrypted env on the host and attaches it (default
  `Authorization: Bearer …`, or a custom header); the secret never enters the
  function source, the URL, logs, or the response. Unknown keys raise
  `http_auth_unknown_key`.
- **Write methods gated by `action_type`.** `read` tools may use `get`/`head`;
  `write`/`destructive` tools may also `post`/`put`/`patch`/`delete` with a
  request body (size-capped).
- **Bounded + metered.** Per-invocation call budget, per-call timeout,
  request/response size caps, a per-`(tenant, host)` circuit breaker, and
  per-tenant/global outbound concurrency caps (`SANDBOX_HTTP_*`). Each call emits
  a `sandbox_http_egress_request` billing event. `GET /admin/whoami` returns an
  `http_egress` summary (enabled flag, effective hosts), surfaced as a
  "Network / egress" row in the Functions Studio sandbox contract and a new
  `context.http` tab in the "What is context?" modal.
- **Docs:** refreshed `CONTEXT.md`, `NETWORK-SECURITY.md`, `SECURITY.md`,
  `docs/API.md`, `README.md`, `ARCHITECTURE.md`, `DESIGN.md`, `PRODUCTION.md`,
  `database/seed.py`, `.env.example`, and `TROUBLESHOOTING.md` (new failure mode #13).

### Feature: per-tenant code-package (pip) policy — BREAKING
- **Tenant pip allowlist, intersected with the operator ceiling.** What a code
  tool may install is now `SANDBOX_ALLOWED_REQUIREMENTS` (global operator ceiling)
  **∩** a per-tenant allowlist (`code_requirements_allowlist`, new on the tenant
  control doc). A package must be in **both**. New admin endpoints
  `GET`/`PUT /admin/tenants/{id}/code-requirements` (tenant-admin for own tenant,
  platform-admin cross-tenant; refused while read-only) manage the tenant list, and
  the console gains a **Code packages** editor per tenant. Backed by a cached
  `services/tenant_pip_policy.py` mirroring the egress/tool-policy pattern.
- **BREAKING:** the global ceiling alone no longer grants installs. An **empty
  tenant allowlist means stdlib-only** for that tenant, regardless of the ceiling —
  fail-closed. Existing tenants that relied on global-only behavior must be opted in
  per tenant (set their `code-requirements` allowlist). There is no compatibility
  shim.
- **Enforced consistently end-to-end.** The same `tenant ∩ ceiling` intersection is
  applied at authoring (`POST /admin/code-tools/validate` returns actor-targeted
  error issues), on server save (`422`), in the sandbox test-run, and at runtime —
  where the executor installs only the trusted-caller-resolved effective list,
  re-clamped to the operator ceiling, wheels-only. Rejections name *which* gate
  blocked the package and *who* can unblock it (platform operator vs. tenant admin).
- **Magical authoring UX.** The Functions Studio renders a live chip per requirement
  (green = installs, amber "awaiting operator", red "not allowed"), the sandbox
  contract card reflects the tenant's effective packages, and `GET /admin/whoami`
  now returns a `code_requirements` summary so the UI needs no extra round trip.
- **Docs:** refreshed `SECURITY.md`, `README.md`, `docs/API.md`, `CONTEXT.md`,
  `TROUBLESHOOTING.md` (new failure mode #12), `ARCHITECTURE.md`, and `.env.example`.

### Bug Fixes
- **Pinned the MCP SDK to stop a transitive dependency skew.** `fastmcp==3.4.2` requires `mcp>=1.24.0,<2.0`, but `mcp` was never pinned, so environments drifted to `mcp 1.23.3` — a version that dropped `streamable_http_client`, which `fastmcp` imports at module load. The result was an `ImportError` on `import fastmcp` that made every router-level test fail at *collection* time (the suite couldn't even start). `requirements.txt` now pins `mcp==1.28.0` (the latest in-range release) so the SDK pair is deterministic and the full unit suite collects and runs.
- **Severe Server Deadlock (MCP streaming).** Fixed a catastrophic deadlock that froze the entire `uvicorn` event loop and pegged the CPU at 100% when an MCP client connected via Server-Sent Events (SSE). The deadlock occurred because `GuardrailsMiddleware` wrapped the ASGI `receive` channel with an immediate-return payload, causing `sse-starlette`'s disconnect-monitoring loop to spin infinitely without yielding to the async loop. Streaming `/mcp` endpoints now bypass the body-buffering guardrails entirely.
- **MongoDB QE Timeout in Rate Limiter.** Rate limit clock synchronization via `mongo_server_now()` now correctly leverages the QE-bypass client. This prevents timeout errors (`ServerSelectionTimeoutError`) when Queryable Encryption's query analysis attempts to parse the `hostInfo` command.

### Feature: the admin console "Users" page is now a "Credentials" experience
- **Reframed around the outcome, not the persona.** The console's **Users** tab is
  now **Credentials** (🔑). The unit you create is framed as *the scoped bearer
  token an MCP client pastes in to reach your tools*, not a "user."
- **One streamlined builder, with a default that just works.** The old four-card
  grid (read-only/full/explore/custom) collapses into a single **Create a
  credential** card: optional name, client picker, an **Access level** selector,
  and one **Create credential** button. Access level defaults to **⚡ Full access**
  (the only tier with a *Recommended* badge) because a credential exists to run
  tools — **🛡️ Read-only** (`tool:invoke` + `derive_safe_scopes`) and **🔍 Explore**
  (`viewer` + `tool:read`, discover-only) are deliberate step-downs, not the
  starting point. An **Advanced** disclosure still exposes explicit
  email/password/role/scopes for power users. Roles, scopes, and the
  `POST /admin/users/{demo,team,viewer}` endpoints are **unchanged** — the three
  tiers just route to the existing presets — so this is a UX reframe, fully
  backward compatible (the nav key stays `users`, so deep links and routing are
  untouched).
- **Client-aware connect flow.** The credential modal now generates ready-to-paste
  config for **Cursor**, **Claude Desktop**, and **VS Code** via a segmented
  switcher that renders the right schema per client (`mcpServers` url/headers for
  Cursor & Claude; `servers` + `type:"http"` for VS Code), alongside the existing
  curl verify snippet. The same bearer powers all three. An optional **name** and
  default **client** can be set before minting.
- **Agent credentials vs console operators.** The single user list is split into two
  grouped tables: **Agent credentials** (anything carrying `tool:invoke` /
  `tool:read` / `viewer` — the MCP tokens) and **Console operators** (the humans who
  sign in: `admin` / `platform-admin`). Same data, grouped by intent, so the page
  reads honestly at a glance.
- **Optional credential name + client (cosmetic, additive).** `POST /admin/users`,
  `/users/demo`, `/users/team`, and `/users/viewer` now accept optional `label` and
  `client` fields (surfaced on `UserResponse`), purely for recognition in the
  console — neither is ever consulted for authorization. Schemaless and backward
  compatible: records minted before this fall back to the generated email.

### Feature: read-only tenants, viewer principals, and per-tenant tool curation
- **Read-only tenants.** A tenant can be frozen with a new `read_only` flag
  (orthogonal to `status`): it stays `active` and fully discoverable, but
  `tools/call` on `/rpc` and `/mcp` is refused at the dispatch gate
  (`403`/`tenant_read_only`, via `assert_tenant_writable`) and tenant-scoped
  config mutations — servers/env, tool policy, server/tool enable-disable, egress,
  **and user management (create/demo/viewer/update/delete)** — are blocked for
  tenant-admins (`_require_tenant_writable`). Platform-admin always bypasses.
  Toggle via `POST /admin/tenants/{id}/read-only` / `/read-write` (platform-admin
  only).
- **Two new least-privilege principals.**
  - `viewer` — a read-only **web-console** role: loads the admin UI and views
    tool source, but every mutating `/admin` call is refused at a single RBAC
    choke point (`403 Read-only access`). A full admin that also carries `viewer`
    is never downgraded.
  - `tool:read` — an MCP **data-plane** role that clears the coarse gate for
    discovery (`tools/list` / `tools/search`) but is refused on `tools/call`
    (`invoke_not_permitted`) by the new invoke-capability check in
    `authorize_tool_call`.
- **One-click viewer user.** A new **Create viewer user** button (and
  `POST /admin/users/viewer`) provisions the complete read-only showcase identity
  in one credential — `viewer` + `tool:read` with catalog-derived scopes — so the
  holder gets a read-only console login *and* a discover-only MCP token (bearer +
  Cursor `mcp.json`). The safe twin of the demo-user button.
- **Per-tenant tool curation.** `GET`/`PUT /admin/tenants/{id}/tool-policy` manage
  an `allowlist` (`server/name` or `server/*`; empty = unrestricted) and a
  `max_tools` cap (enforced at server registration with a `422`). Curation filters
  discovery *and* invocation, so a showcase only ever surfaces the curated set.
- **Enable/disable controls.** `POST /admin/servers/{name}/enable` / `/disable`
  mount/unmount a virtual server (origin-aware: tenant-admins toggle their own
  `tenant`-origin servers; `platform`-origin requires platform-admin), and
  `POST /admin/tools/{server}/{name}/enable` / `/disable` toggle a per-tool
  kill-switch (`disabled_tools` overlay) that refuses the tool for **everyone,
  including admins** (`tool_disabled`). Both are blocked when the tenant is
  read-only (platform-admin bypasses).
- **Console UX.** A sticky read-only banner, `canMutate()`/`is_read_only` gating
  of mutating affordances, a tenant read-only toggle + tool-policy editor, and
  per-server/per-tool enable switches. `GET /admin/whoami` now returns
  `is_read_only` and `tenant_read_only`. Server-side `403` remains the real guard;
  UI hiding is UX only.
- **Settings:** new `PLATFORM_VIEWER_ROLE` (default `viewer`).
- **Docs:** new [`READONLY.md`](READONLY.md) — a screenshot-driven walkthrough
  (freeze → curate → viewer login) with an enforcement diagram, API table, and
  troubleshooting; cross-linked from the README, QUICKSTART, AUTH, and API docs.
- **Seeded read-only persona.** `docker compose up` now bootstraps a stable
  `viewer@demo.com` / `viewer-demo` account alongside the existing
  `agent@demo.com`, giving three purposeful, reproducible demo tiers out of the
  box — platform admin (`demo@demo.com`), can-invoke power user (`agent@demo.com`),
  and read-only showcase (`viewer@demo.com`). Local/dev only; skipped in
  production, idempotent, and documented in QUICKSTART + `docker-compose.yml`.

### Fix: `POST /auth/token` (password grant) now works for non-admin users on `/rpc`/`/mcp`
- Under `hs256`, `POST /auth/token` minted an **admin-session** token, which
  `AuthMiddleware` only accepts on the MCP surface for console principals
  (`admin`/`viewer`). A plain tool user (e.g. the documented `agent@demo.com`
  with `tool:invoke`) got `401 "Invalid bearer token"` because the session token
  fell through to the data-plane decode and failed signature verification — the
  exact terminal flow shown in QUICKSTART and the connect modal.
- The endpoint is now `auth_mode`-aware, mirroring the console's **Generate
  token**: `hs256` mints a real *scoped* data-plane bearer (roles **and** scopes,
  signed with `JWT_SECRET`) accepted for any role; `jwks` keeps the roles-only
  admin-session fallback (issue scoped tokens from your IdP). `resolve_login_principal`
  now also returns the principal's `scopes` so the minted bearer clears per-call
  authorization.

### Breaking: security is always enforced — `AUTH_MODE=disabled` removed
- **`AUTH_MODE=disabled` is gone.** The `AUTH_MODE` setting now accepts only
  `hs256` (default) or `jwks`; loading with `disabled` fails validation. Every
  caller on `/rpc` and `/mcp` must present a verified credential and clear the
  `admin`/`tool:invoke` RBAC gate — there is no trusted-by-default path. All
  `disabled`-mode branches were deleted from `gateway/middleware/auth.py` (incl.
  the `X-MCP-Scopes` header-trust path and the `SCOPES_HEADER` setting),
  `gateway/middleware/rbac.py`, and `gateway/mcp_server.py`.
- **The `X-MCP-Scopes` header is no longer trusted.** Scopes come exclusively
  from verified token claims (`groups`/`scopes`).
- **Fail-safe session authority (security hardening).** An admin-session token
  that carries no `roles` claim is now treated as an authenticated identity with
  **zero** authority instead of an implicit platform-admin. Every token this
  gateway mints already embeds explicit roles, so a missing/empty claim can only
  come from a stale or hand-forged token — those no longer escalate. The CLI dev
  helper `scripts/mint_token.py` is documented as the `AUTH_MODE=jwks`-only path,
  and the misleading "mint a local dev token" snippet was removed from the
  tenant-connect UI in favor of the Generate-token button and `POST /auth/token`.
- **Generate-token onboarding.** The admin console's **Users → Generate token**
  flow is the fastest way to get a working bearer; the token endpoint
  (`POST /admin/users/{id}/token`) drops the `disabled` branch and `scopes_header`
  field, and now writes a structured **audit log** on every mint (actor, target,
  tenant, roles, ttl).
- **One-click demo user.** A new **Create demo user** button (and
  `POST /admin/users/demo`) provisions a ready-to-use, tool-invoking account in one
  step: generated password + **catalog-derived scopes** (`server:*` plus every tool
  scope in the tenant) so it can discover *and* invoke tools immediately, then hands
  back a bearer + Cursor `mcp.json`. The manual create form's **Demo** role preset
  auto-fills the same scopes via `GET /admin/users/demo-scopes`. Demo creation is
  audit-logged; the generated password is shown once and never logged or stored in
  cleartext. This replaces the previous "create a user, then know to add `tool:invoke`
  and the right scopes yourself" expert-only path.
- **Automagic secure local setup.** `docker compose up` now generates a random
  `JWT_SECRET` (secrets-init → `JWT_SECRET_FILE`), runs the gateway and bootstrap
  with `AUTH_MODE=hs256`, and seeds a ready-to-use demo account `agent@demo.com`
  (role Demo / `tool:invoke`, local password) so the Generate-token button has an
  immediate target. Demo seeding is skipped when `ENVIRONMENT=production`.
- **Migration:** if you ran with `AUTH_MODE=disabled`, switch to `hs256` and set a
  strong `JWT_SECRET` (or `jwks` with your IdP). Replace any `X-MCP-Scopes`
  callers with a bearer token whose claims carry the desired scopes.

### Feature: native server-side `$rankFusion` under Queryable Encryption
- Hybrid search now runs **native server-side `$rankFusion`** even when Queryable
  Encryption is enabled. Previously an auto-encryption client's `crypt_shared`
  query analysis could not resolve the namespaces inside `$rankFusion`'s
  sub-pipelines ("No resolved namespace provided"), forcing the app-side fallback.
- Catalog search is now routed through a scoped **`bypass_auto_encryption`** client
  (`database/mongo.py`: `get_qe_bypass_client` / `get_tenant_database_for_search`),
  which skips client-side query analysis while preserving automatic decryption.
  `tool_catalog` holds no encrypted fields, so the bypass is safe; the client is
  scoped to reads/aggregations and is **never** used to write `routing_registry`.
- The **app-side reciprocal-rank-fusion (RRF) fallback is retained** as a safety
  net for clusters without native `$rankFusion` (8.1+) or any residual QE/version
  drift, and now also catches `EncryptionError` (not just `OperationFailure`).
- **Observability:** the previously silent fallback now logs a WARNING (deduped to
  once per cause per process, DEBUG thereafter) so a degraded native path is
  visible instead of indistinguishable from healthy.
- **Version alignment:** the whole stack now standardizes on the latest 8.x —
  `mongodb-atlas-local:8.3` (Compose + integration tier + CI) with a matching
  `crypt_shared` 8.3.2 baked into the image. `$rankFusion` needs **8.1+**, so the
  prior `8.0` pin silently exercised the fallback while claiming to test the native
  stage; server and `crypt_shared` are kept on the same minor. Docs corrected from
  the inaccurate "8.0+" to "8.1+".
- **Observability:** `tools/search` and routed `tools/list` now record which
  retrieval/fusion path served the request (`native_rankfusion` / `app_side_rrf` /
  `vector` / `text` / `lexical_fallback`) in `audit_telemetry` metadata via a
  concurrency-safe `ContextVar` (`get_last_fusion_path`), with no API contract
  change.
- New: `QUERYABLE_ENCRYPTION_CAVEATS.md` documents the QE × `$rankFusion`
  interaction, the options matrix, and verification steps.

### Feature: always-included (pinned) tools
- Tools flagged `metadata.always_included` are now pinned to the top of every
  routed result — `tools/search`, `tools/list?query=…`, and the `/mcp`
  `search_tools` meta-tool — regardless of semantic/hybrid relevance. Each pinned
  result is tagged `pinned: true`.
- Pinning is **scope-safe**: the pin fetch runs the same identity-bound scope
  filter as the ranked arms (`services/hybrid_search.py`), so it never surfaces a
  tool the caller could not otherwise discover.
- Pins are **budget-bounded**: they take reserved seats inside the caller's
  `limit` rather than inflating it, keeping prompt cost flat. If an admin pins
  more tools than `limit`, the explicit intent wins and all pins are returned.
- Authorable from Admin Studio via an "Always included" toggle on each tool, with
  an advisory (non-blocking) warning past a recommended count of 5.
- New setting `HYBRID_PIN_ALWAYS_INCLUDED` (default `true`) globally disables the
  behavior. `search_tools` was refactored to separate ranking (`_search_ranked`)
  from pinning (`_fetch_always_included` + `_merge_pinned`).

### Security: `/mcp` meta-tool authorization at parity with `/rpc`
- The FastMCP `/mcp` meta-tool surface (`search_tools`, `list_catalog_tools`,
  `call_downstream_tool`) now enforces the same controls as the `/rpc` data plane.
  Previously `/mcp` was weaker: it skipped per-call authorization, was not covered
  by coarse RBAC / the account kill-switch, and let a `tenant_id` argument override
  the token claim — a real gap the moment `/mcp` is relied on for tenant isolation.
- `call_downstream_tool` now runs per-call `authorize_tool_call` (the tool must
  exist in the tenant catalog and the caller must satisfy its scopes, or be
  `admin`), raising `ToolError (forbidden)` otherwise — mirroring `/rpc`'s
  `tools/call`.
- `call_downstream_tool` now also enforces the tenant **usage quota** (raises
  `ToolError (quota_exceeded)`), **meters** the billable call, **propagates the
  caller identity** to the downstream hop, and writes an **`audit_telemetry`** row
  for every outcome (`live_execution_success` / `forbidden` / `quota_exceeded` /
  `tenant_suspended`) under the same `method="tools/call"` label as `/rpc`.
  Previously `/mcp` calls bypassed quotas, were not metered, and left no audit
  trail. The shared "record a billable call" step now lives in a single module
  (`services/data_plane.py`) used by both surfaces so they cannot drift again.
- `RbacMiddleware` (`gateway/middleware/rbac.py`) now gates `/mcp` in addition to
  `/rpc`: it requires `admin`/`tool:invoke`, honors the per-user kill-switch, and
  hydrates `session_context` roles on both surfaces.
- **Tenant binding:** the `/mcp` meta-tools derive the tenant from the
  gateway-verified `request.state` (via FastMCP `get_http_request()`). A
  `tenant_id` argument that does not match the verified claim is rejected with
  `ToolError (cross_tenant_forbidden)` — no cross-tenant override on `/mcp`.

### Hardening: tightened broad exception handling
- Added observability to previously silent failure sites so a degraded result is
  no longer indistinguishable from a healthy one:
  `services/proxy_registry.py` tool discovery (logs before returning no tools),
  `services/tenant_provisioner.py` deprovision (logs an error when the tenant DB
  drop is skipped so it is not silently orphaned), `services/guardrails.py` PII NER
  redaction, `services/sandbox_pool.py` worker spawn/warmup, and
  `services/cache_migration.py` search-index lookup.
- Narrowed broad `except Exception` to the known failure type where the mode is
  known: `json.JSONDecodeError` (`gateway/routers/auth.py`,
  `gateway/middleware/span_extractor.py`), `ImportError` (optional-dependency
  guards in `services/metrics.py`, `services/tracing.py`, `gateway/app.py`,
  `gateway/mcp_server.py`, `services/guardrails.py`), `binascii.Error`
  (`database/encryption.py`), `pymongo` errors (`services/tenant_provisioner.py`,
  `services/server_exporter.py`), and `ValueError` / `OSError`
  (`services/sandbox_worker.py`).

### Robustness: sandbox cold start decoupled from a tool's wall budget
- A code tool's `wall_timeout_ms` was conflated with sandbox **cold start** — the
  worker subprocess spawn, wasm module compile, and in-guest CPython boot. Under
  host CPU load that boot alone could exceed the budget, so a trivial, fast tool
  spuriously **timed out** (the host read deadline fired, or the guest's own epoch
  wall-timer interrupted it mid-boot) even though it did almost no work. Now a new
  `sandbox_worker_startup_grace_ms` (default 10s) gives boot its own headroom:
  the host read backstop and the worker's epoch wall-timer both add it on top of
  `wall_timeout_ms`, so the budget bounds the tool's **compute**, not its boot.
  Raw compute/memory remain bounded precisely by wasm fuel + the memory limit, so
  a real CPU/output bomb is still killed; only legitimate slow-boot calls benefit.
- The warm pool's acquire wait now floors at the warmup ceiling (not the job's
  wall budget) so a worker that dies mid-job and is respawned in the background
  doesn't spuriously starve the next call with "no warm worker" while a healthy
  replacement is seconds away.
- Fixed the live sandbox integration tests (`tests/integration/`) to spawn the
  worker on the test runner's own interpreter (`sys.executable`) instead of a bare
  `python` on `PATH` — the latter frequently resolved to a wasmtime-less
  interpreter (e.g. a pyenv shim), so every worker died on `import wasmtime` and
  surfaced as a confusing "worker exited", while the runtime-availability guard
  (which probed the *runner*) never skipped. The guard now probes the actual
  worker interpreter, so a wasmtime-less worker skips honestly.

### Refactor: decomposed the admin router into a package
- Split the ~1,900-line / 47-handler `gateway/routers/admin.py` god-module into a
  `gateway/routers/admin/` package grouped by resource (`tenants`, `servers`,
  `users`, `embeddings`, `code_tools`, `explore`, `actions`, `catalog`), with a
  shared `_common` module holding the authorization guards, the `settings` /
  search / cache-migration singletons, and the test-injection seams.
- No behavior or HTTP-surface change: every route keeps its exact `/admin` path and
  the public import surface (`import gateway.routers.admin as admin`;
  `admin.create_user(...)`, `admin.settings`) is preserved. Test seams are now
  patched on the single `admin._common` module.

### Inbound MCP-client auth (username/password + OAuth seam)
- Added `POST /auth/token` — an OAuth2 Resource Owner Password Credentials grant
  that exchanges a username/password (managed users + bootstrap admin) for a
  short-lived bearer the gateway already accepts on `/rpc` and `/mcp`. Works in
  every `AUTH_MODE`.
- Added optional HTTP Basic on the MCP surface behind `MCP_BASIC_AUTH_ENABLED`
  (default off); bad Basic credentials return `401` with a `WWW-Authenticate: Basic`
  challenge.
- Added an OAuth discovery seam: `GET /.well-known/oauth-protected-resource`
  (RFC 9728) advertises the configured issuer when `AUTH_MODE=jwks` or
  `OAUTH_METADATA_ENABLED=true`, and bearer `401`s carry a
  `WWW-Authenticate: Bearer resource_metadata=...` hint. Full OAuth remains
  bring-your-own-IdP via `AUTH_MODE=jwks`; the gateway is a resource server, not
  an authorization server.
- Extracted the shared `resolve_login_principal` so the admin UI login and the
  token endpoint authenticate against a single source of truth.
- Added Admin Studio connect-modal snippet showing the `/auth/token`
  username/password flow.

### Simplified downstream auth (workload identity only)
- Reduced downstream credential brokering to a gateway workload identity:
  `metadata.auth.scheme` is now `jwt` (default) or `none`. Third-party
  credentials (API keys, basic auth, OAuth client secrets) are intentionally not
  brokered per-server by the gateway — they are owned by the downstream service
  or the tenant; use `scheme=none` and terminate that auth downstream.
- Removed the `basic` / `api_key` strategies, the `oauth2_client_credentials`
  stub, and the related save-time secret-key / header / stdio validation and
  Admin Studio metadata snippets / secret-key presets.
- Kept credential-transport hardening: the `jwt` bearer is refused over plaintext
  `http://` unless `DOWNSTREAM_ALLOW_INSECURE_CREDENTIALS=true`. Cache
  invalidation on server update/secret rotation and the `downstream.auth_scheme`
  span attribute are retained.

### Export a code server as a runnable FastMCP project (`.zip`)
- Added `GET /admin/servers/{server_name}/export` and an **Export server (.zip)**
  button in the server editor: download a self-contained
  [FastMCP](https://github.com/jlowin/fastmcp) project that runs every tool on
  the server outside the gateway.
- Reconstructs the sandbox `context` locally so authored code runs **unmodified**:
  `context.db` (real `pymongo`), `context.env` (per-server), and `context.tools`
  / `context.call` (in-process sibling calls).
- **Smart cross-tool bundling:** statically resolves `context.tools` /
  `context.call` references and bundles the transitive closure of sibling code
  tools (even across servers in the tenant), wiring them into an in-process
  registry so `tool_a` calling `tool_b` keeps working. A depth guard
  (`MCP_TOOL_CALL_MAX_DEPTH`) fails cyclic calls closed.
- **Secrets are never exported** — only the *names* of `context.env` keys are
  written to `.env.example` (blank placeholders). The project ships with
  `server.py`, `requirements.txt` (gateway-pinned `fastmcp`/`pymongo` + each
  tool's pins), a `mcp_context/` runtime, per-tool modules with the verbatim
  authored source, a README, and `.gitignore`.
- **Drops straight into a client:** ships an executable `run.sh` (venv +
  install + load `.env` + run) and a `.python-version`; the README includes a
  paste-ready `mcpServers` config for Cursor/Claude Desktop and a "What's
  inside" tour. The generated `server.py` picks its transport from the
  environment (`MCP_TRANSPORT=stdio|http`, `MCP_HOST`, `MCP_PORT`) — serve over
  HTTP with no code edits.

### Cross-tool calls: `context.tools` (tenant-as-namespace)
- Added `context.tools` / `context.call(...)` so a code tool can invoke sibling
  code tools in the same tenant namespace
  (`context.tools.<server>.<tool>(**kwargs)`), composing small tools into
  workflows without duplicating logic.
- Relays each call through the host over the existing sandbox bridge (new
  `tool_rpc` frame kind) — the sandbox stays fully network-isolated.
- Re-authorizes every relayed call against the **original caller's** scopes,
  restricts targets to `transport="code"` servers, refuses
  confirmation-gated tools, and bounds fan-out via nesting depth +
  per-invocation call budget (cycles/recursion fail closed).
- Made the executor's concurrency guards re-entrant so a nested sibling call
  can't deadlock against the per-tenant/global semaphore.
- Added settings `SANDBOX_TOOL_BRIDGE_ENABLED` (opt-in),
  `SANDBOX_TOOL_CALL_MAX_DEPTH`, `SANDBOX_TOOL_MAX_CALLS_PER_INVOCATION`,
  `SANDBOX_TOOL_MAX_RESULT_BYTES`.
- Seeded an `analytics/track_and_report` demo that composes `track_click` +
  `get_click_stats` through `context.tools`.
- Added a **context.tools** tab to the *What is `context`?* guide and a
  "Callable tools" insert palette (with parameter hints) in the function editor.

### Admin Studio polish and `context` ergonomics
- Redesigned the sandbox test panel's empty/running states (themed resting card
  and a busy state) instead of the flat gray placeholder.
- Renamed the per-server "Workspace" affordance to **Edit** / "Editing server"
  for a more intuitive mental model.
- Consolidated the two embeddings nav entries into a single **Embeddings**
  section with a Platform / This-tenant scope toggle.
- Reworked Telemetry with summary cards (events, success rate, errors, avg/p95
  latency) and semantic status pills.
- Made `context` resource-only: dropped `context.utcnow()` and moved the BSON id
  helper to `context.db.ObjectId(...)`. Use stdlib `datetime` for timestamps.
- Added PyMongo-style write results: `insert_one(...).inserted_id`,
  `update_*` → `matched_count`/`modified_count`/`upserted_id`, `delete_*` →
  `deleted_count`, plus `.acknowledged` (attribute and dict access).
- Made function return values BSON-aware: return MongoDB documents, `ObjectId`s,
  and `datetime`s directly and they serialize to JSON automatically.
- Added a **What is `context`?** modal in the function editor — a live,
  copy-paste guide to `context.db`, `context.env`, write results, and types.

### Server Workspace, Server-Scoped Auth, and Per-Server `context.env`
- Refactored Admin Studio around a per-MCP-server workspace with tabs for Tools,
  Search, Explore DB, and Secrets. Search and Explore flows now run within the
  selected server workspace instead of a global top-level section.
- Enforced server-namespaced authorization on `tools/call`: callers now require
  `server:<server>` (or `server:*`) in addition to tool-level scopes.
- Applied server-scope filtering to discovery (`tools/list` and hybrid search)
  so callers only see tools for allowed servers.
- Replaced per-tenant sandbox secrets with per-server encrypted env storage
  (`server_secrets`) and exposed values in sandbox runtime as `context.env`.
  This intentionally removes legacy `secrets` argument / `SECRETS` compatibility.
- Replaced admin endpoints `GET/PUT /admin/tenants/{tenant}/sandbox-secrets`
  with `GET/PUT /admin/servers/{server}/env` (key names only in responses).
- Seeded an `analytics` demo server with `track_click` (write) and
  `get_click_stats` (read), demonstrating `context.db` + `context.env`.
- Enabled `SANDBOX_DB_BRIDGE_ENABLED=true` in local dev defaults
  (`.env.example` / Compose) so the click-tracker demo works out-of-the-box,
  while production remains an explicit policy choice.

### Virtual DB bridge and authoring UX
- Added a tenant-scoped virtual DB bridge for sandboxed code tools:
  `context.db[...]` now executes host-relayed MongoDB operations without exposing
  network access or database credentials inside the wasm sandbox.
- Added host-side DB operation policy enforcement keyed to each tool's
  `metadata.action_type` (`read` / `write` / `destructive`), including collection
  validation, aggregation stage hardening, and bridge call/result limits.
- Added the Admin Studio **Explore Database** experience (collections, samples,
  read-only query runner, and generated `context.db[...]` snippet insertion/copy)
  backed by new endpoints:
  `GET /admin/explore/collections`, `POST /admin/explore/sample`,
  `POST /admin/explore/query`.
- Added bridge configuration settings:
  `SANDBOX_DB_BRIDGE_ENABLED`, `SANDBOX_DB_MAX_DOCS`,
  `SANDBOX_DB_QUERY_TIMEOUT_MS`, `SANDBOX_DB_MAX_CALLS_PER_INVOCATION`,
  `SANDBOX_DB_MAX_RESULT_BYTES`.
- Refreshed stale code-tool docs/comments to reflect current runtime behavior
  (code tools are executable in wasm when enabled; no longer "storage-only").

### Observability and docs
- Added `ARCHITECTURE.md` as the canonical as-built system map (component flow,
  control-vs-tenant data plane, `tools/call` lifecycle, subsystem deep dives,
  and roadmap gaps), and linked it from the main README deploy path.
- Added a turnkey observability stack for local demos: Prometheus scrape config,
  Prometheus alert rules, Grafana provisioning, and a prebuilt gateway dashboard
  wired into `docker-compose.yml`.
- Added `docs/API.md` to document the combined REST/admin and JSON-RPC (`/rpc`)
  surfaces, plus protocol error codes and request examples.
- Added `TROUBLESHOOTING.md` as a dedicated runbook for concrete failure modes
  implemented in code (Atlas capability gaps, `$rankFusion` fallback, JWKS
  outages, embedding startup failures, index-queryable timing, and more).
- Enriched FastAPI OpenAPI metadata with `version`, `description`, and route tags
  so `/docs` and `/redoc` are useful operational references.
- Added optional Kubernetes `ServiceMonitor` manifest (`deploy/k8s/servicemonitor.yaml`)
  and updated deployment docs to point to the observability stack and runbook.

### Embeddings
- Added pluggable, admin-configurable embedding providers: Ollama (default),
  OpenAI, Azure OpenAI, Voyage AI, and Google Gemini, implemented over `httpx`
  behind a shared `BaseHttpEmbeddingService` (cache + retry + circuit breaker).
- Vector width is now detected at runtime by embedding a probe string
  (`EMBEDDING_PROBE_TEXT`) instead of being hand-configured; index dimensions and
  `embedding_version` flow from the active provider via `active_embedding_identity()`.
- Added a runtime, gateway-wide embedding configuration persisted in the control
  DB (`gateway_config`) with API keys encrypted at rest (Fernet, keyed by
  `EMBEDDING_SECRET`) and masked in all API responses. The active provider is
  surfaced through a stable proxy so existing call sites follow config changes.
- Added platform-admin embedding endpoints: `GET/PUT /admin/embedding`,
  `POST /admin/embedding/test` (dry-run reachability + dimension detection), and
  `GET /admin/embedding/status`, plus an **Embeddings** section in the admin UI.
- Applying a config change auto-reprovisions the embedding space in the
  background: re-embeds every tenant's `tool_catalog`, drops/recreates the
  `hybrid-vector-search` indexes with the new `numDimensions`, refreshes the
  semantic cache, and re-embeds the guardrail signature corpus. Progress is
  tracked in `control_db.embedding_status` and polled by the UI.
- Hardening: the stored dimension is always the width the provider actually
  returns (detected on every apply), so a vector index can never drift out of
  sync with its data.
- Hardening: Gemini authenticates via the `x-goog-api-key` header instead of a
  `?key=` query param, so API keys never leak into URLs, `httpx` error strings,
  logs, or status documents.
- Hardening: embedding reprovision is single-flight — a config change is rejected
  with `409` while a run is in progress, and a crashed/stale `running` job is
  reclaimed after one hour so it can never lock out future runs.
- New settings: `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_BASE_URL`,
  `EMBEDDING_API_KEY(_FILE)`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`,
  `AZURE_OPENAI_DEPLOYMENT`, `EMBEDDING_PROBE_TEXT`, and `EMBEDDING_SECRET(_FILE)`.
  The `OLLAMA_*` variables configure the default Ollama provider.

### Security and correctness
- Added MongoDB Queryable Encryption support for `routing_registry` secret fields
  (`env`, `command`, `args`, `metadata`) with KMS providers:
  - `aws` (LocalStack in local Compose, real AWS KMS in production)
  - `local` (base64-encoded 96-byte master key for no-KMS environments)
  Provisioning now creates an encrypted `routing_registry` collection and key
  vault metadata in `encryption.__keyVault`.
- Added auth modes (`disabled`, `hs256`, `jwks`) with production safety validation.
- Implemented JWKS-based token verification with local offline JWKS support.
- Added dynamic downstream credential brokering (`services/credential_broker.py`):
  downstream calls now present a short-lived RS256 JWT minted per `(tenant, server)`
  — a tenant-scoped *workload identity*, not the end-user token — injected as a
  transport credential (`Authorization` for HTTP/SSE, env var for stdio) instead of
  relying on static long-lived downstream secrets. Tokens are never logged, and the
  bundled dev signing key is rejected in `ENVIRONMENT=production`.
- Enforced scope authorization in `tools/call` (not only discovery).
- Isolated semantic cache entries by `tenant_id`.
- Replaced toy guardrails with reusable injection + PII redaction service.
- Upgraded guardrails to a layered model: deterministic regex floor + optional
  semantic injection detector over a versioned `guardrail_signatures` vector
  corpus + optional Presidio NER redaction fallback.
- Added guardrail resilience controls (`GUARDRAIL_FAIL_MODE`, timeout, circuit
  breaker) and inbound span extraction (`params.query` + string arguments) so
  classification focuses on semantically meaningful payload content.
- Made semantic cache embedding provenance first-class (`embedding_model`,
  `embedding_dim`, `embedding_version`) and version-gated lookups to prevent
  cross-model false positives after embedding model upgrades.
- Added semantic cache migration operations (status / purge / reembed) in both
  admin API (`POST /admin/cache/migrate`) and CLI (`scripts/migrate_cache.py`).
- Fixed semantic-cache `reembed` migration completeness: stale entries are now
  processed across all batches (not capped to one `batch_size` slice), and
  migration summaries now include `remaining_entries` for explicit operator
  visibility.
- Eliminated index/filter drift risk by centralizing semantic-cache and
  guardrail-signature vector index specs beside their query filters and adding
  Docker-free contract tests that ensure filter keys are index-declared.
- Hardened tenant physical isolation by disambiguating tenant DB names with a
  stable hash suffix (`tenant_db_name()` collision-safe for `tenant-a`,
  `tenant.a`, `tenant_a`-style IDs).
- Made the tenant boundary explicit: tenant-scoped RPC methods now call
  `ensure_tenant_ready()`, which provisions an unknown tenant on first use
  (`AUTO_PROVISION_TENANTS=true`, cached per process) or returns a clear
  `tenant_not_provisioned` JSON-RPC error instead of failing as a silent empty
  result. Disable auto-provisioning where tenant ids are untrusted.
- Added explicit tenant deprovisioning support: `deprovision_tenant()` drops the
  tenant database, deletes the control-plane tenant record, and evicts in-process
  readiness caches; exposed as `DELETE /admin/tenants/{tenant_id}` (platform-admin).
- Added usage-event rollup access via
  `GET /admin/tenants/{tenant_id}/usage/events` and `summarize_billing_events()`
  so persisted billing events (`calls`, `sandbox_ms`) are queryable without
  direct DB inspection.

### Routing and resiliency
- Fixed active-active registry-watcher scaling: each replica now persists its own
  change-stream resume token (`routing_registry::<instance_id>`) so pods do not
  overwrite each other's stream position.
- Embedding reprovision status is now incrementally updated with
  `progress={completed,total}` and partial tenant summaries while the run is in
  flight, so admin polling can distinguish healthy progress from a stalled job.
- Added TTL lifecycle management for watcher resume-state docs
  (`WATCHER_RESUME_TTL_SECONDS`) with index-option conflict handling.
- Integrated JWT rotation with downstream warm-client pooling: the warm-hit path
  checks the stored credential's refresh-skew window (no broker contention in steady
  state) and only evicts/reconnects with a freshly minted token when a (re)connect is
  actually needed, so calls always use a fresh JIT credential without dropping pool
  semantics. Catalog discovery presents the same credential.
- Warm sandbox worker-pool acquisition now skips/recycles workers already dead
  while idle in the free queue, reducing user-visible `tools/call` failures from
  transient worker exits.
- Sliding-window rate limiter that weights the previous window into the current
  one, closing the 2x burst-at-the-boundary gap. Buckets live one extra window so
  the rolling calculation can read the prior window before TTL cleanup.
- Made downstream timeout detection type-based: the connect+call is bounded by
  our own `asyncio.wait_for` deadline and timeouts are recognized by walking the
  exception cause/context chain for known timeout types (`TimeoutError`,
  `httpx.TimeoutException`) instead of substring-matching the error message.
- Added schema validation on normalized downstream results: a result must be a
  JSON object with JSON-serializable values, otherwise it surfaces as a
  protocol-safe `DownstreamProtocolError` (no retry) rather than crashing deep in
  serialization or poisoning the cache.
- Made JWKS rotation prompt: a token whose `kid` is absent from the cached key
  set triggers an immediate out-of-band refresh (throttled to once per
  `JWKS_MIN_REFRESH_SECONDS`) instead of waiting out the cache TTL.
- Added GA-safe application-side RRF fallback for hybrid search.
- Added embedding retries, circuit breaker, and lexical fallback.
- Added differential schema hashing to skip unnecessary tool re-embedding.
- Added cache policy metadata (`cacheable`, TTL, invalidations) with write-through invalidation.

### Platform hardening
- Fixed health/metrics being unreachable under `hs256`/`jwks` auth: `AuthMiddleware` now
  exempts `/health`, `/health/live`, `/health/ready`, and `/metrics`
  (`_is_observability_path`) so k8s `httpGet` probes and Prometheus scrapes work in every
  auth mode without a token. These expose only status + aggregate counters (no tenant
  data); the rate limiter also skips them so infra traffic never spends a tenant's budget.
- Enabled `--proxy-headers` in the container and surfaced `FORWARDED_ALLOW_IPS`
  (`.env.example`, k8s ConfigMap, Helm values) so per-IP rate limiting (`request.client.host`)
  and `Secure` admin cookies (`X-Forwarded-Proto`) behave correctly behind a TLS-terminating
  proxy — while only trusting forwarded headers from the configured proxy range.
- Replaced rate limiter with atomic window bucket counters and limit headers.
- Added request IDs, structured JSON logging, Prometheus `/metrics`, and readiness/liveness probes.
- Added deployment scaffolding (`deploy/k8s`, Helm chart) and CI (`ruff`, `mypy`, `pytest`).
- Added pre-commit config, `requirements-dev.txt`, and coverage configuration.

### Protocol and API
- Added `initialize` RPC method and paginated `tools/list` cursors.
- Added catalog version tracking and list-changed signaling support.
- Expanded mounted MCP server tools to include paginated catalog listing and downstream tool proxying.

### Observability
- Added native OpenTelemetry spans around JSON-RPC handling and downstream MCP
  hops (`services/tracing.py`), with attributes for tenant, tool, authorization
  outcome, cache result, and retry count. Degrades to a no-op when the OTel SDK
  is absent or `ENABLE_TRACING` is off, so it never affects the request path.
- Bounded Prometheus label cardinality in the metrics middleware: HTTP method and
  request path are normalized to a small fixed allow-set (unknown paths collapse to
  `other`, unknown methods to `OTHER`), closing a memory-exhaustion vector where a
  scanner hitting random URLs would mint an unbounded number of time series.

### Testing and quality gates
- Fixed a broken CI gate: added package `__init__.py` files (mypy no longer
  aborts on a duplicate `metrics` module) and cleared all `ruff`/`mypy` findings
  so `ruff check`, `ruff format --check`, and `mypy .` pass clean.
- Added an in-memory async MongoDB fake and deterministic embedding stub
  (`tests/fakes.py`) so DB- and search-backed code is testable without Atlas or
  Ollama.
- Grew the unit suite from 16 to 132 tests covering the JSON-RPC router end to
  end, the full middleware chain, JWKS/HS256 auth, the embedding circuit
  breaker, semantic-cache tenant isolation, the authorization matrix, guardrail
  redaction (incl. Luhn-validated cards), settings prod-safety, and tracing.
- Added an integration tier (18 tests, `tests/integration/`) that runs against a
  real MongoDB Atlas Local cluster and a live embedding provider: native
  `$rankFusion` ranking + `scoreDetails` receipts, semantic-vector and lexical
  retrieval, engine-side scope filtering, semantic-cache `$vectorSearch`
  round-trip with tenant isolation, catalog sync with real embeddings, index
  DDL idempotency, and a concurrency/latency benchmark for the hybrid-search hot
  path (50 concurrent searches, mean ~300ms / p95 ~400ms on Atlas Local).
- Made the integration tier own its engine: it provisions a pinned
  `mongodb/mongodb-atlas-local` container via testcontainers, verifies it is
  genuinely search-capable (a plain `mongod` is rejected, never silently used),
  runs the real bootstrap into an isolated throwaway database, and drops it on
  teardown — verified across repeated runs to leave zero leftover containers or
  databases. An `INTEGRATION_MONGODB_URI` override targets an existing cluster
  (still verified, still isolated). The tier skips cleanly when Docker is
  absent.
- Fixed a real defect surfaced by the integration tier: the `semantic_cache`
  vector index was missing the `tenant_id` filter field, which made every cache
  lookup fail on a live cluster. The bootstrap definition already included it;
  the test now permanently guards against that index/definition drift.
- Enforced an 80% coverage floor on the unit tier in CI (`--cov-fail-under=80`)
  and added the `ruff format --check` step. The integration tier runs as a
  separate CI job where testcontainers provisions the pinned Atlas Local image
  on the runner's Docker daemon and Ollama supplies real embeddings.
