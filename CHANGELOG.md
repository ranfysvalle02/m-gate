# Changelog

## Unreleased

### Security and correctness
- Added auth modes (`disabled`, `hs256`, `jwks`) with production safety validation.
- Implemented JWKS-based token verification with local offline JWKS support.
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

### Routing and resiliency
- Replaced the fixed-window rate limiter with a sliding-window counter
  (`RATE_LIMIT_STRATEGY=sliding_window`) that weights the previous window into
  the current one, closing the 2x burst-at-the-boundary gap; `fixed_window`
  restores the legacy behavior. Buckets now live one extra window so the
  rolling calculation can read the prior window before TTL cleanup.
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
