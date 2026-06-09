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

- [ ] `AUTH_MODE` ≠ `disabled`.
- [ ] `hs256`: `JWT_SECRET` ≥16 chars and not a known weak value.
- [ ] `jwks`: `JWT_ISSUER` **and** `JWT_AUDIENCE` set, plus `JWKS_URI` **or** `JWKS_LOCAL_PATH`.
- [ ] `CORS_ALLOW_ORIGINS` is **not** `*` (explicit origin list).
- [ ] If `ADMIN_UI_ENABLED=true`: `ADMIN_EMAIL` set, `ADMIN_PASSWORD` ≥12 chars (not weak),
      `ADMIN_SESSION_SECRET` ≥16 chars (not weak).
- [ ] Downstream JWT brokering is not using the bundled dev key
      (`DOWNSTREAM_JWT_PRIVATE_KEY_FILE` ≠ `config/dev-private-key.pem`), unless
      `DOWNSTREAM_JWT_ENABLED=false`.

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
  will not work. Use an Atlas cluster or Atlas Local; `$rankFusion` needs MongoDB 8.0+
  (the gateway falls back to application-side RRF otherwise).
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
- **PodDisruptionBudget**: `deploy/k8s/pdb.yaml` keeps capacity during voluntary
  disruptions. Add an HPA on CPU/RPS for autoscaling.
- **Graceful shutdown**: the lifespan stops the watcher, closes pooled downstream clients,
  and disconnects Mongo on shutdown.

---

## 8. Guardrails posture

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

## 9. Health probes and the auth interaction

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

## 10. Observability & alerting

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

## 11. Bootstrapping, provisioning & migrations

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

## 12. Upgrades & rollbacks

- The image is stateless; roll forward/back by changing the image tag
  (`kubectl set image …` / `helm upgrade --set image.tag=…`). State stays in MongoDB.
- Do not roll back across an embedding-dimension change without accounting for the vector
  index width; a reprovision is idempotent and safe to re-run.
- Roll one replica first and watch `/health/ready` + error metrics before completing the
  rollout (the PDB protects capacity during the roll).

---

## 13. Production readiness checklist

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
