# Changelog

## Unreleased

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
