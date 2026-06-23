# Production Deployment & Operations

How to run the MongoDB MCP Gateway safely in production. This is the **operations and
hardening** authority; for the step-by-step deploy mechanics (Compose / single container /
Kubernetes / Helm) use [`DEPLOYMENT.md`](DEPLOYMENT.md). For the security model and
network boundaries see [`SECURITY.md`](SECURITY.md) and
[`NETWORK-SECURITY.md`](NETWORK-SECURITY.md).

> **TL;DR** — Set `ENVIRONMENT=production` (it fails closed on misconfiguration), use a
> real `AUTH_MODE`, supply your own downstream signing key and a stable
> `EMBEDDING_SECRET`, put the gateway behind a TLS-terminating proxy started with
> `--proxy-headers`, point it at an Atlas cluster (replica-set + Search), run ≥2
> replicas, and restrict egress.

---

## 1. Architecture recap (what runs in prod)

The gateway image is **stateless** — all state lives in MongoDB Atlas. You can scale it
horizontally and roll it forward/back freely.

| Component | Production requirement |
| --- | --- |
| Gateway (`gateway.app:app`) | ≥2 replicas behind a TLS-terminating LB/ingress |
| MongoDB | **Atlas cluster** (or Atlas Local) — replica set (change streams), Search + Vector Search |
| Embedding provider | Ollama (in-cluster) or a cloud provider (OpenAI/Azure/Voyage/Gemini) |
| Downstream MCP servers | Private network; verify the gateway's workload JWT |

---

## 2. Pre-flight: fail-closed production validation

When `ENVIRONMENT=production`, the gateway **refuses to start** unless all of these hold
(`config/settings.py::_validate_prod_safety`). Treat startup failure as a feature.

- [ ] `hs256`: `JWT_SECRET` ≥16 chars and not a known weak value.
- [ ] `jwks`: `JWT_ISSUER` **and** `JWT_AUDIENCE` set, plus `JWKS_URI` **or** `JWKS_LOCAL_PATH`.
- [ ] `CORS_ALLOW_ORIGINS` is **not** `*` (explicit origin list).
- [ ] If `ADMIN_UI_ENABLED=true`: `ADMIN_EMAIL` set, `ADMIN_PASSWORD` ≥12 chars (not weak),
      `ADMIN_SESSION_SECRET` ≥16 chars (not weak).

The bundled dev keypair (`config/dev-private-key.pem` / `config/dev-jwks.json`, kid
`dev-local-key-1`) is **published in this repo**, so its private half is public. The
following checks therefore apply to **every environment that is not an explicit
local/dev/test environment** — i.e. `staging`, `production`, and any unrecognized
`ENVIRONMENT` value all fail closed:

- [ ] Downstream JWT brokering is not signing with the bundled dev key
      (`DOWNSTREAM_JWT_PRIVATE_KEY_FILE` ≠ `config/dev-private-key.pem`), unless
      `DOWNSTREAM_JWT_ENABLED=false`.
- [ ] Inbound `jwks` auth is not trusting the bundled dev JWKS
      (`JWKS_LOCAL_PATH` ≠ `config/dev-jwks.json`); point it at your real IdP via
      `JWKS_URI`/`JWKS_LOCAL_PATH`.

The bundled downstream demo servers (`servers/orders`, `servers/weather`) enforce the
same rule independently: with `DOWNSTREAM_JWT_VERIFY` enabled they refuse to start
outside a local/dev environment while `DOWNSTREAM_JWKS_PATH` still points at the
bundled `config/dev-jwks.json`.

---

## 3. Production configuration reference

Full list with defaults: [`.env.example`](.env.example). Production essentials:

### Identity & access

| Variable | Recommended prod value | Notes |
| --- | --- | --- |
| `ENVIRONMENT` | `production` | Enables fail-closed validation |
| `AUTH_MODE` | `jwks` | `hs256` acceptable for a shared-secret setup |
| `JWT_ISSUER` / `JWT_AUDIENCE` | your IdP values | Enforced when set |
| `JWKS_URI` *or* `JWKS_LOCAL_PATH` | your IdP JWKS | `JWKS_CACHE_TTL_SECONDS`, `JWKS_MIN_REFRESH_SECONDS` tune caching |
| `CORS_ALLOW_ORIGINS` | explicit origins | Never `*` |
| `ADMIN_UI_ENABLED` | `false` if you admin via API/CLI | Reduces attack surface |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD(_FILE)` | strong | Required if UI enabled |
| `ADMIN_SESSION_SECRET(_FILE)` | long random, stable | Signs admin session cookies |
| `PLATFORM_ADMIN_ROLE` | `platform-admin` | Role name granting platform admin |

### Downstream credential brokering

| Variable | Recommended prod value | Notes |
| --- | --- | --- |
| `DOWNSTREAM_JWT_ENABLED` | `true` | Mint workload tokens to downstreams |
| `DOWNSTREAM_JWT_PRIVATE_KEY_FILE` | **your own key path** | Dev key is rejected in prod |
| `DOWNSTREAM_JWT_KID` / `DOWNSTREAM_JWT_ISSUER` | your values | Downstreams verify against these |
| `DOWNSTREAM_TOKEN_TTL_SECONDS` | `120` (default) | Short-lived; rotated before expiry |
| `DOWNSTREAM_TOKEN_REFRESH_SKEW_SECONDS` | `15` (default) | Reconnect skew window |

### Data plane (MongoDB Atlas)

| Variable | Notes |
| --- | --- |
| `MONGODB_URI` / `MONGODB_URI_FILE` | `mongodb+srv://…` with credentials, or use `ATLAS_*` |
| `MONGODB_DB_NAME` | Control-plane DB name |
| `ATLAS_TLS` | `true` for TLS to Atlas |
| `ATLAS_TLS_CA_FILE` | Custom CA if needed |
| `ATLAS_USERNAME` / `ATLAS_PASSWORD(_FILE)` | SCRAM auth (or X.509 via URI) |
| `ATLAS_AUTH_SOURCE` / `ATLAS_AUTH_MECHANISM` | e.g. `admin` / `SCRAM-SHA-256` |
| `AUTO_BOOTSTRAP` | `true` to create indexes + seed + sync catalog on startup |
| `AUTO_PROVISION_TENANTS` | `false` where tenant ids are untrusted |

### Queryable Encryption (optional but recommended for registry secrets)

| Variable | Notes |
| --- | --- |
| `QE_ENABLED` | `true` enables field-level encryption for `routing_registry` secret fields |
| `KMS_PROVIDER` | `aws` (recommended in prod) or `local` (dev/demo only) |
| `QE_KEY_VAULT_NAMESPACE` | Defaults to `encryption.__keyVault` |
| `AWS_KMS_KEY_ARN(_FILE)` | Required for `KMS_PROVIDER=aws` |
| `AWS_DEFAULT_REGION` / `AWS_KMS_ENDPOINT` | Region required; endpoint optional (LocalStack/dev) |
| `QE_LOCAL_MASTER_KEY(_FILE)` | Base64 96-byte key for `KMS_PROVIDER=local` |
| `CRYPT_SHARED_LIB_PATH` | Path to `mongo_crypt_v1.so` in the runtime container |

### Embeddings

| Variable | Notes |
| --- | --- |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` | Or configure at runtime in the admin panel (control DB wins) |
| `EMBEDDING_API_KEY(_FILE)` | For cloud providers; prefer the file mount |
| `EMBEDDING_SECRET(_FILE)` | **Encrypts stored API keys — keep stable** (see §5) |

### Guardrails, limits, observability

| Variable | Default | Prod guidance |
| --- | --- | --- |
| `GUARDRAIL_FAIL_MODE` | `open` | Consider `closed` for high-security tenants (availability trade-off, see §8) |
| `GUARDRAIL_ML_ENABLED` | `false` | Enable for semantic injection detection |
| `GUARDRAIL_PII_NER_ENABLED` | `false` | Enable Presidio NER redaction fallback |
| `REQUEST_MAX_BYTES` | `262144` | Caps inbound body size (→ 413) |
| `DOWNSTREAM_TIMEOUT_MS` | `2000` | Hard per-call deadline |
| `RATE_LIMIT_WINDOW_SECONDS` / `RATE_LIMIT_MAX_REQUESTS` | `60` / `120` | Per `(tenant, client-ip)` |
| `ENABLE_METRICS` | `true` | Prometheus `/metrics` |
| `ENABLE_TRACING` | `false` | `true` for OpenTelemetry spans |
| `LOG_JSON` | `true` | Structured logs with request IDs |
| `GATEWAY_INSTANCE_ID` | host name | Per-replica id for the change-stream watcher |
| `WATCHER_RESUME_TTL_SECONDS` | `86400` | TTL for per-instance watcher resume docs |

### Downstream egress allowlist

Restricts which downstream hosts/networks the gateway may connect to. This covers
both outbound surfaces: the downstream-server proxy and the opt-in code-tool
`context.http` bridge (the wasm sandbox itself has no sockets). Enforced at
registration (`422`) and, authoritatively, at connect time with DNS re-resolution +
IP pinning to defeat rebinding. **Code egress (`context.http`) is always
deny-by-default and SSRF-screened, even when `EGRESS_ALLOWLIST_ENABLED=false`** — see
the `SANDBOX_HTTP_*` row below.
See [`SECURITY.md`](SECURITY.md#network-egress-controls-per-tenant-allowlists).

| Variable | Default | Prod guidance |
| --- | --- | --- |
| `EGRESS_ALLOWLIST_ENABLED` | `true` | Master switch for the egress allowlist feature |
| `EGRESS_GLOBAL_ALLOWLIST` | _(empty)_ | Deployment-wide ceiling: comma/space-separated host globs (`*.corp.example`), exact hosts, IP literals, CIDRs. Empty = no global ceiling |
| `EGRESS_DEFAULT_DENY` | `false` | `true` = deny every host unless explicitly allowlisted (recommended for locked-down fleets) |
| `EGRESS_ALLOWLIST_CACHE_TTL_SECONDS` | `5` | Per-replica TTL for cached per-tenant allowlists read from the control DB |

Per-tenant allowlists are managed at runtime via
`PUT /admin/tenants/{tenant_id}/egress-allowlist` and intersect with the global
ceiling. Watch `gateway_egress_blocks_total{stage}` (`register`/`connect`).

#### Code-tool outbound HTTP (`context.http`)

Off by default. When enabled, sandboxed code can make host-mediated HTTPS calls,
screened by the **same** egress stack as above. Code egress is **always
deny-by-default and SSRF-screened**, independent of `EGRESS_ALLOWLIST_ENABLED`: a
tenant reaches a host only if it is on that tenant's egress allowlist **and** under
`EGRESS_GLOBAL_ALLOWLIST`. Each call emits a `sandbox_http_egress_request` billing
event.

| Variable | Default | Prod guidance |
| --- | --- | --- |
| `SANDBOX_HTTP_BRIDGE_ENABLED` | `false` | Master switch for `context.http`. Leave off unless tenants need outbound HTTP |
| `SANDBOX_HTTP_TIMEOUT_MS` | `5000` | Per-call timeout |
| `SANDBOX_HTTP_MAX_CALLS_PER_INVOCATION` | `10` | Per-tool-run call budget |
| `SANDBOX_HTTP_MAX_RESPONSE_BYTES` | `262144` | Response body cap |
| `SANDBOX_HTTP_MAX_REQUEST_BYTES` | `131072` | Request body cap (write methods) |
| `SANDBOX_HTTP_BREAKER_FAILURES` / `SANDBOX_HTTP_BREAKER_RESET_SECONDS` | `5` / `30` | Per-(tenant, host) circuit breaker |
| `SANDBOX_HTTP_MAX_CONCURRENCY_PER_TENANT` / `SANDBOX_HTTP_MAX_GLOBAL_CONCURRENCY` | `8` / `0` | Outbound concurrency caps (`0` = unlimited global) |

---

## 4. Run behind a proxy correctly (required)

The gateway serves plain HTTP and must sit behind a TLS-terminating ingress/LB/mesh.
The image already runs uvicorn with `--proxy-headers`, so the only thing you must
configure is **which proxy to trust** via `FORWARDED_ALLOW_IPS` (read natively by
uvicorn). This is required for correct per-IP rate limiting and `Secure` cookies (see
[`NETWORK-SECURITY.md`](NETWORK-SECURITY.md#important-network-behaviors-read-before-you-ship)):

```bash
FORWARDED_ALLOW_IPS="10.0.0.0/8"   # your ingress/LB pod IP or CIDR
```

It's wired into `.env.example`, the k8s ConfigMap, and Helm `values.yaml` (default
`10.0.0.0/8` — narrow it to your proxy). **Never set `*`** if the gateway is reachable
directly. If you cannot preserve the client IP, rely on the LB/WAF for per-IP rate
limiting.

---

## 5. Secrets management

- **Mount secrets as files / from a secret manager** (Kubernetes Secrets + KMS, Vault,
  cloud secret managers). Every sensitive value has a `*_FILE` companion
  (`MONGODB_URI_FILE`, `JWT_SECRET_FILE`, `ADMIN_PASSWORD_FILE`,
  `ADMIN_SESSION_SECRET_FILE`, `EMBEDDING_API_KEY_FILE`, `EMBEDDING_SECRET_FILE`,
  `DOWNSTREAM_JWT_PRIVATE_KEY_FILE`, `ATLAS_PASSWORD_FILE`). Never put secrets in a
  ConfigMap or bake them into the image.
- **Downstream signing key**: generate a dedicated RS256 keypair, mount the private key,
  and distribute the public key (JWKS) to downstreams so they can verify the workload JWT.
- **⚠️ Keep `EMBEDDING_SECRET` stable.** Embedding-provider API keys are encrypted at rest
  with it (Fernet). If you rotate or change it, previously stored keys can no longer be
  decrypted (they read as empty) and must be re-entered in the admin panel. Set it once
  per environment and store it like any other long-lived secret. If unset, it falls back
  to `ADMIN_SESSION_SECRET`, then `JWT_SECRET`.
- **Treat QE key material as Tier-0 secrets.** Protect `AWS_KMS_KEY_ARN(_FILE)` and/or
  `QE_LOCAL_MASTER_KEY(_FILE)`, and include `encryption.__keyVault` in your backup and
  disaster-recovery scope.
- **Rotation**: JWKS keys rotate at the IdP (the resolver picks them up out-of-band).
  Rotate the downstream signing key by publishing the new public key to downstreams first,
  then swapping the private key. Rotate `JWT_SECRET`/`ADMIN_SESSION_SECRET` during a
  maintenance window (rotating the session secret invalidates active admin sessions).

---

## 6. Database (MongoDB Atlas)

- **Must be Atlas-capable**: the registry watcher uses **change streams** (replica set
  required) and routing uses **Atlas Search + Vector Search**. A standalone `mongod`
  will not work. Use an Atlas cluster or Atlas Local; `$rankFusion` needs MongoDB 8.1+
  (this repo standardizes on 8.3; the gateway falls back to application-side RRF otherwise).
- **Harden the connection**: TLS (`ATLAS_TLS=true`), SCRAM or X.509 auth, and a least-
  privilege database user. See [`deploy/README.md`](deploy/README.md).
- **Lock the network**: use Atlas PrivateLink/peering and a strict network access list so
  only the gateway's egress can reach the cluster.
- **First boot**: run the bootstrap once (`AUTO_BOOTSTRAP=true`, the Compose `bootstrap`
  job, or `python -m scripts.bootstrap`) to create indexes, seed the control plane, and
  sync the downstream catalog with embeddings.
- **Backups & encryption at rest**: configure in Atlas. If QE is enabled, include
  `encryption.__keyVault` in backup/restore runbooks.

---

## 7. Scaling & high availability

- **Stateless replicas**: run ≥2 (the manifests default to 2) behind a load balancer.
  All shared state — rate-limit buckets, sessions, catalog, cache — is in MongoDB.
- **Active-active change-stream watcher**: each replica persists its **own** resume token
  (`routing_registry::<instance_id>`, from `GATEWAY_INSTANCE_ID` or the host name), so
  replicas don't clobber each other's stream position. Stale resume docs self-clean via a
  TTL (`WATCHER_RESUME_TTL_SECONDS`). Give each replica a stable, unique `GATEWAY_INSTANCE_ID`
  (e.g. the pod name) if host names are not unique/stable.
- **Distributed rate limiter**: shared via MongoDB and synchronized to the DB server clock,
  so the limit is consistent across replicas regardless of pod clock skew.
- **Warm downstream connection pool**: one warm client per `(tenant, server)` per replica,
  re-credentialed on token rotation. No sticky sessions required.
- **Warm sandbox worker pool**: `SANDBOX_POOL_SIZE>0` keeps that many CPython-on-WASI
  workers resident per replica, each with the `python.wasm` module already compiled, so a
  code-tool call skips the subprocess-spawn + module-compile cold start (typically the
  dominant latency for the first call). Every job still runs in a fresh wasm `Store`, so
  isolation stays per-call. The pool is prewarmed at startup (only when
  `CODE_TOOL_EXECUTION_ENABLED`), refills itself when a worker times out/crashes/recycles
  (`SANDBOX_WORKER_MAX_JOBS`), and a compiled-module cache (`SANDBOX_MODULE_CACHE_PATH`)
  makes respawns fast. Leave `SANDBOX_POOL_SIZE=0` (default) to spawn a throwaway worker
  per call. Observe it via `gateway_sandbox_pool_workers` and
  `gateway_sandbox_pool_events_total{event}` (`spawned`, `served`, `recycled`,
  `acquire_timeout`, `spawn_failed`).
- **Sandbox concurrency ceilings**: each call is bounded by a per-tenant semaphore
  (`SANDBOX_MAX_CONCURRENCY_PER_TENANT`) *and* a process-wide ceiling
  (`SANDBOX_MAX_GLOBAL_CONCURRENCY`, 0 = off) that caps total simultaneous executions across
  all tenants on a replica. Size the global cap to the host's CPU/RSS budget so a fan-out of
  many tenants cannot oversubscribe a node; in pooled mode it pairs naturally with
  `SANDBOX_POOL_SIZE` (set the global cap >= pool size). A blocked call fails fast at the
  pool acquire timeout (`SANDBOX_POOL_ACQUIRE_TIMEOUT_MS`) rather than queuing unboundedly.
  Result frames are hard-capped, so a single call's worst-case memory footprint is ~2x
  `SANDBOX_MAX_OUTPUT_BYTES`; multiply by the global cap for per-replica sizing.

### Sandbox capacity planning (analytical)

Use this model to size a replica before collecting host-specific benchmarks:

- **Warm throughput** per replica:
  `min(SANDBOX_POOL_SIZE, max(1, SANDBOX_MAX_GLOBAL_CONCURRENCY)) / mean_job_seconds`
- **Cold throughput** per replica:
  `1 / (mean_job_seconds + mean_spawn_seconds + mean_compile_seconds)` per concurrent slot.
- **Warm latency (p50)**:
  approximately `mean_job_seconds` (+ queueing only when semaphores saturate).
- **Cold latency (p50)**:
  approximately `mean_job_seconds + mean_spawn_seconds + mean_compile_seconds`.
  `SANDBOX_MODULE_CACHE_PATH` reduces respawn compile time but not in-job runtime.
- **Per-call memory envelope**:
  roughly `2 * SANDBOX_MAX_OUTPUT_BYTES + SANDBOX_MEMORY_BYTES + worker_base_rss`.
- **Per-replica memory envelope**:
  roughly `(SANDBOX_MAX_GLOBAL_CONCURRENCY * per_call_envelope) + (SANDBOX_POOL_SIZE * resident_worker_rss)`.

Tuning guidance:

- Set `SANDBOX_POOL_SIZE` to expected concurrent code-tool demand *per replica*.
- Set `SANDBOX_MAX_GLOBAL_CONCURRENCY` to the same order as pool size, then cap it by real
  host CPU/RSS limits.
- Keep `SANDBOX_MAX_CONCURRENCY_PER_TENANT` below the global cap to preserve fairness.
- Keep `SANDBOX_POOL_ACQUIRE_TIMEOUT_MS` short enough to fail fast during saturation rather
  than building unbounded queue latency.
- Use `SANDBOX_WORKER_MAX_JOBS` as a resident-worker recycle backstop if long-lived RSS drift
  appears.

Operational signals to watch:

- `gateway_sandbox_pool_workers`: should converge near `SANDBOX_POOL_SIZE`.
- `gateway_sandbox_pool_events_total{event="acquire_timeout"}`: sustained growth indicates
  under-provisioned pool/concurrency settings.
- `gateway_sandbox_pool_events_total{event="served"}` versus `{event="recycled"}`:
  spikes in recycle rate can indicate unstable workers or overly aggressive max-jobs settings.
- `sandbox_ms` usage metering (section 8): use this as the billing/cost attribution driver
  per tenant once pricing policy is defined.

### Sandbox measured baseline (empirical)

Runbook command (from repo root, with `.venv` active and `python.wasm` fetched):

- `python scripts/bench_sandbox.py`

Current baseline sample (captured on `macOS-26.4.1-arm64-arm-64bit`, Python `3.11.13`,
`wasmtime 45.0.0`, `python-3.12.0.wasm`):

- **Cold no-cache latency** (n=5): p50 `691 ms`, p95 `724 ms`.
- **Cold with module cache** (n=5 + prime): prime `689 ms`, p50 `686 ms`, p95 `716 ms`.
- **Warm serial latency** (n=20): p50 `71 ms`, p95 `76 ms`.
- **Warm concurrent throughput** (`pool=4`, `global=4`, 20 calls): `54.48 calls/s`.
- **Worker memory**: cold child-peak `495696 KiB`; resident warm worker peak `544160 KiB`.

Treat these as a machine-specific baseline, not a universal SLO. Re-run
`python scripts/bench_sandbox.py` on your target deployment class and update this section with
those host-local numbers before final capacity sizing.
- **PodDisruptionBudget**: `deploy/k8s/pdb.yaml` keeps capacity during voluntary
  disruptions. Add an HPA on CPU/RPS for autoscaling.
- **Graceful shutdown**: the lifespan stops the watcher, closes pooled downstream clients,
  tears down the warm sandbox worker pool, and disconnects Mongo on shutdown.

---

## 8. Quotas & usage metering

The gateway now supports per-tenant metering + quota enforcement on `tools/call`.

- **Metering dimensions**:
  - `calls` (incremented on successful billable completions, including cache hits).
  - `sandbox_ms` (incremented from code-tool sandbox elapsed runtime).
- **Defaults**:
  - `USAGE_QUOTA_PERIOD=monthly`
  - `DEFAULT_QUOTA_CALLS_PER_PERIOD=0`
  - `DEFAULT_QUOTA_SANDBOX_SECONDS_PER_PERIOD=0`
  - `0` means unlimited (metering still runs).
- **Enforcement**: when a tenant exceeds a configured limit, `tools/call` returns
  JSON-RPC `RATE_LIMITED` (`-32029`) with `reason=quota_exceeded` and the current
  `usage`/`quota` snapshot.
- **Billing hook**: usage events are appended to control-plane `usage_events`
  (`tenant_id`, `period`, `kind`, `amount`, `ts`) for downstream billing pipelines.
- **Admin control plane**:
  - `GET /admin/tenants/{tenant_id}/usage`
  - `PUT /admin/tenants/{tenant_id}/quota` (platform-admin only)

### Tenant suspension (abuse kill-switch)

Quotas cap steady-state spend; suspension is the **instant** lever for an actively
abusive or compromised tenant.

- **Suspend / resume** (platform-admin only):
  - `POST /admin/tenants/{tenant_id}/suspend` — optional body `{"reason":"..."}`.
  - `POST /admin/tenants/{tenant_id}/resume`.
  - Also surfaced as Suspend/Resume buttons on the **Tenants** admin console tab.
- **Effect**: a suspended tenant is blocked on both data planes — `/rpc`
  (`tools/call`, `tools/list`, `tools/search`) and the mounted `/mcp` tools. `/rpc`
  returns JSON-RPC `FORBIDDEN` (`-32003`) with `reason=tenant_suspended`. The admin
  console, billing, and other control-plane reads remain available.
- **Propagation**: status is cached per replica for `TENANT_STATUS_CACHE_TTL_SECONDS`
  (default `5`). The acting node honors the change immediately; other replicas pick it
  up within the TTL. Set to `0` to read status on every request (no cache) if you need
  zero-delay propagation at the cost of one extra control-plane read per data-plane call.
- **Audit**: each suspend/resume is written to the tenant telemetry/audit trail
  (`tenant_suspended` / `tenant_resumed`) with the acting admin and reason.

**Per-user revocation.** For a single bad actor (rather than a whole tenant), disable
the user via `PATCH /admin/users/{id}` with `{"status":"disabled"}` (or the Disable
button on the Users tab). A user's status is mirrored into the `session_context` the
`/rpc` RBAC gate already reads, so a disabled account is rejected with `403 Account
suspended` on its **next** request — a standing bearer token stops working immediately,
without waiting for token expiry. Re-enable with `{"status":"active"}`. (This applies to
the `/rpc` plane; the `/mcp` mount still enforces tenant-level suspension.)

---

## 9. Guardrails posture

- The **deterministic regex floor** for prompt-injection and the **request size cap** are
  always on for `/rpc` and `/mcp`.
- The **semantic injection classifier** (`GUARDRAIL_ML_ENABLED`) and **Presidio NER PII
  redaction** (`GUARDRAIL_PII_NER_ENABLED`) are optional; enable them for stronger DLP.
- `GUARDRAIL_FAIL_MODE` decides what happens if the classifier times out or trips its
  circuit breaker:
  - `open` (default): allow the request — favors availability.
  - `closed`: block the request — favors safety.
  Choose based on tenant risk. A timeout (`GUARDRAIL_TIMEOUT_SECONDS`) and circuit breaker
  keep a slow classifier from pinning the request path either way.

---

## 10. Health probes and the auth interaction

The gateway exposes:

| Endpoint | Use |
| --- | --- |
| `GET /health/live` | Liveness — process up |
| `GET /health/ready` | Readiness — Mongo reachable + indexes queryable + embedding probe ok |
| `GET /metrics` | Prometheus metrics (`ENABLE_METRICS=true`) |

**Health and metrics are unauthenticated in every auth mode.** `AuthMiddleware` exempts
`/health`, `/health/live`, `/health/ready`, and `/metrics`
(`gateway/middleware/auth.py::_is_observability_path`), so the bundled k8s `httpGet`
probes and Prometheus scrapes work as-is under `hs256`/`jwks` — no probe token required.
These endpoints expose only status + aggregate counters (no tenant data), and the rate
limiter skips them so infra traffic never consumes a tenant's budget.

Because they are open, **protect them at the network layer**: keep `/health` and
`/metrics` off the public internet and scope `/metrics` to your scrape network. The
bundled NetworkPolicy already restricts ingress to the same namespace + ingress
controller; tighten it to your Prometheus source if needed.

---

## 11. Observability & alerting

- **Logs**: `LOG_JSON=true` → structured logs with request IDs. Auth failures are logged
  by *category* (`expired`, `bad_signature`, `bad_audience`, `jwks_unavailable`, …),
  never with token contents.
- **Metrics** (`/metrics`): auth failures (by reason), guardrail events
  (inbound blocked / outbound redacted / size blocked), rate-limit decisions, cache
  hit/miss, and downstream errors/latency.
- **Tracing**: `ENABLE_TRACING=true` → OpenTelemetry spans around RPC handling and
  downstream hops (`mcp.actor`, server, tool, tenant, auth outcome, retries). No-ops if
  the OTel SDK is absent.

Suggested alerts:

- Spike in `401`/auth failures (`bad_signature`/`expired`) → credential stuffing or a
  client misconfig; `jwks_unavailable` → your IdP is down (these surface as `503`).
- Sustained `429` → abuse or an under-provisioned limit.
- Guardrail `blocked`/`redacted` rate → injection attempts / PII exposure.
- `/health/ready` flapping → Mongo, index, or embedding-provider problems.
- Downstream error/timeout rate → a failing tool server.

---

## 12. Bootstrapping, provisioning & migrations

- **Embedding changes are data migrations, not code deploys.** Switching providers/models
  from the admin panel triggers a background reprovision: re-embed every tenant's catalog,
  drop/recreate vector indexes at the new width, refresh the semantic cache, and re-embed
  the guardrail corpus. Search degrades to lexical-only while indexes rebuild. Track at
  `GET /admin/embedding/status`.
- A reprovision is **single-flight** (a concurrent config change returns `409`) and a
  crashed run is reclaimed after one hour, so it can always be retried by re-applying the
  config.
- **Tenants**: with `AUTO_PROVISION_TENANTS=true` a tenant DB + indexes are created on
  first use; set `false` for untrusted tenant ids and provision via `POST /admin/tenants`
  or `python -m scripts.admin`.

---

## 13. Upgrades & rollbacks

- The image is stateless; roll forward/back by changing the image tag
  (`kubectl set image …` / `helm upgrade --set image.tag=…`). State stays in MongoDB.
- Do not roll back across an embedding-dimension change without accounting for the vector
  index width; a reprovision is idempotent and safe to re-run.
- Roll one replica first and watch `/health/ready` + error metrics before completing the
  rollout (the PDB protects capacity during the roll).

---

## 14. Production readiness checklist

**Identity & access**
- [ ] `ENVIRONMENT=production`, `AUTH_MODE` real (`jwks` preferred) with issuer + audience.
- [ ] `CORS_ALLOW_ORIGINS` explicit (never `*`).
- [ ] Admin UI disabled, or strong `ADMIN_EMAIL`/`ADMIN_PASSWORD` + stable `ADMIN_SESSION_SECRET`.
- [ ] Dedicated `DOWNSTREAM_JWT_PRIVATE_KEY(_FILE)`; public key (JWKS) distributed to downstreams.

**Secrets**
- [ ] All secrets file-mounted / from a secret manager (not ConfigMaps, not the image).
- [ ] Stable `EMBEDDING_SECRET` set once and stored safely.

**Network**
- [ ] TLS terminated in front of the gateway; `FORWARDED_ALLOW_IPS` set to the proxy IP/CIDR (the image already runs `--proxy-headers`).
- [ ] `:8000` not exposed directly; `/admin` + `/ui` restricted to a trusted network.
- [ ] Egress restricted (NetworkPolicy CIDRs scoped to Atlas/provider/downstreams).
- [ ] Downstream MCP servers + embedding provider on a private network.

**Data**
- [ ] Atlas cluster (replica set + Search + Vector Search), TLS + auth, locked network access list.
- [ ] Bootstrap run once; backups configured in Atlas.

**Resilience & ops**
- [ ] ≥2 replicas; unique stable `GATEWAY_INSTANCE_ID` per replica; PDB in place.
- [ ] Readiness/liveness validated **for the chosen auth mode** (see §9).
- [ ] Metrics scraped, tracing decided, alerts wired (see §10).
- [ ] Guardrail fail mode chosen deliberately (`open` vs `closed`).
