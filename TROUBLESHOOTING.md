# Troubleshooting Guide

This runbook focuses on concrete failure modes implemented in code, not generic
guesses. Each section includes:

- **Symptom**
- **Why it happens**
- **How to fix**
- **What to watch** in Grafana/Prometheus

## 1) Startup fails or registry watcher is unstable on standalone `mongod`

**Symptom**

- Gateway startup fails, or runtime logs show registry watcher/change-stream
  failures.
- `tools/list` and dynamic registry updates behave inconsistently.

**Why it happens**

- The gateway expects Atlas-capable behavior (Search/Vector + change streams).
- Plain standalone `mongod` is not enough for this architecture.
- Mongo connectivity and ping happen via `database/mongo.py`, and registry sync
  relies on change streams in `services/registry_watcher.py`.

**How to fix**

- Use MongoDB Atlas Local or an Atlas cluster.
- Ensure Search + Vector Search are enabled and queryable.
- Re-run bootstrap/provisioning (`AUTO_BOOTSTRAP=true` or `scripts.bootstrap`).

**What to watch**

- `HTTP 5xx Error Rate`
- `Downstream Errors by Type`
- Prometheus alert: `GatewayTargetDown`

## 2) Hybrid search silently using the app-side fallback instead of native `$rankFusion`

**Symptom**

- Hybrid retrieval still returns ranked results, but you want to confirm it ran the
  native server-side stage (the differentiator) rather than the app-side fallback.
- A `WARNING` in the logs: `Native $rankFusion unavailable (...); using app-side RRF
  fallback`.

**Why it happens**

- `$rankFusion` needs **MongoDB 8.1+** (this repo standardizes on 8.3). Older
  servers raise `OperationFailure` (`Unrecognized pipeline stage name`).
- Under Queryable Encryption, an auto-encryption client's `crypt_shared` query
  analysis cannot resolve `$rankFusion`'s sub-pipeline namespaces (`EncryptionError`).
  The gateway routes catalog search through a scoped `bypass_auto_encryption` client
  (`database/mongo.py: get_tenant_database_for_search`) so the native stage runs even
  with QE on. See [`QUERYABLE_ENCRYPTION_CAVEATS.md`](QUERYABLE_ENCRYPTION_CAVEATS.md).
- `services/hybrid_search.py` catches **both** `OperationFailure` and
  `EncryptionError` and falls back to app-side RRF (`_search_hybrid_app_side`) so
  routing never hard-fails; the fallback warns once per cause (then DEBUG).

**How to confirm which path served a query**

- Telemetry: every `tools/search` and routed `tools/list` writes
  `metadata.fusion_path` to `audit_telemetry` — `native_rankfusion`, `app_side_rrf`,
  `vector`, `text`, or `lexical_fallback`.
- Live: the native path returns `scoreDetails` as an **object** (`{value, description,
  details:[…]}`); the app-side fallback returns a **list**.

**How to fix**

- Keep fallback enabled (default behavior is resilient — identical ranking).
- Ensure server + `crypt_shared` are both 8.1+ and on the same minor (image
  `mongodb-atlas-local:8.3` + `MONGODB_CRYPT_VERSION=8.3.2`); after a version change,
  start from a clean data volume so FCV permits the stage.
- Verify text and vector indexes are queryable.

**What to watch**

- `audit_telemetry` rows where `metadata.fusion_path == "app_side_rrf"`
- `Latency p95 by Path` and `Latency Quantiles`
- `HTTP Request Rate by Path` for `/rpc`
- `Downstream Errors by Type`

## 3) Production boot rejects bundled dev downstream signing key

**Symptom**

- App fails to start in `ENVIRONMENT=production` with an error about
  `DOWNSTREAM_JWT_PRIVATE_KEY(_FILE)` / dev key usage.

**Why it happens**

- `config/settings.py` enforces production safety checks.
- The repo-shipped dev private key is explicitly blocked in production.

**How to fix**

- Provide a production private key via `DOWNSTREAM_JWT_PRIVATE_KEY` or
  `DOWNSTREAM_JWT_PRIVATE_KEY_FILE`.
- Keep `DOWNSTREAM_JWT_ENABLED=true` unless you intentionally disable
  downstream JWT brokering.

**What to watch**

- Startup logs
- `HTTP 5xx Error Rate` after rollout

## 4) Embedding provider hiccup at startup

**Symptom**

- Logs show embedding config refresh warning at startup.
- `/health/ready` reports `embedding=false` and status `degraded`.

**Why it happens**

- In `gateway/app.py`, embedding refresh errors are logged and startup continues
  by design (log-and-continue resilience).
- Readiness still probes embedding and marks degraded when provider is down.

**How to fix**

- Validate provider credentials/network reachability.
- Use `POST /admin/embedding/test` and then `PUT /admin/embedding` once valid.
- If secrets changed, confirm `EMBEDDING_SECRET` stability and key decryptability.

**What to watch**

- `Latency Quantiles` (search may degrade)
- `HTTP 5xx Error Rate`
- `Downstream Error Rate`

## 5) JWKS token verification failures (401 vs 503)

**Symptom**

- Clients receive `401 Invalid bearer token` or `503 Authentication temporarily unavailable`.

**Why it happens**

- `gateway/middleware/auth.py` classifies auth failures and emits
  `gateway_auth_failures_total{reason=...}`.
- Client-side token problems map to `401`.
- `jwks_unavailable` (IdP/JWKS failure) maps to `503`.

**How to fix**

- For `401`: verify token expiry, signature, issuer, audience, and `kid`.
- For `503`: restore JWKS endpoint/local JWKS readability and network path.
- Check `JWT_ISSUER`, `JWT_AUDIENCE`, `JWKS_URI` / `JWKS_LOCAL_PATH`.

**What to watch**

- `Auth Failures` panel (reason breakdown)
- Prometheus alert: `JWKSUnavailable`
- Prometheus alert: `AuthFailureSurge`

## 6) Indexes are not queryable yet

**Symptom**

- `/health/ready` returns degraded with `checks.indexes=false`.
- Search/list quality is poor or inconsistent right after bootstrap/reprovision.

**Why it happens**

- `gateway/routers/health.py` explicitly checks queryable status of text/vector
  search indexes before reporting ready.
- Reprovision/index build is asynchronous.

**How to fix**

- Wait for index build completion.
- Re-run tenant provisioning/bootstrap if needed.
- Verify tenant DB and index names are present (`hybrid-vector-search`,
  text index).

**What to watch**

- `Latency Quantiles` and `Latency p95 by Path`
- `HTTP 5xx Error Rate`

## 7) `/metrics` returns 404

**Symptom**

- Prometheus cannot scrape metrics.
- `/metrics` returns `metrics disabled`.

**Why it happens**

- `gateway/routers/metrics.py` returns 404 when `ENABLE_METRICS=false`.

**How to fix**

- Set `ENABLE_METRICS=true` and restart gateway.
- Confirm scrape config targets `gateway:8000` path `/metrics`.

**What to watch**

- Prometheus `up{job="mdb-mcp-gateway"}`
- Alert: `GatewayTargetDown`

## 8) Large payloads get rejected (413)

**Symptom**

- Requests to `/rpc` or `/mcp` fail with `413 Request body too large`.

**Why it happens**

- `gateway/middleware/guardrails.py` enforces size checks both from
  `Content-Length` and actual body size.
- Limit is controlled by `REQUEST_MAX_BYTES`.

**How to fix**

- Reduce request size or increase `REQUEST_MAX_BYTES` deliberately.
- Avoid sending oversized unstructured payloads through JSON-RPC calls.

**What to watch**

- `Guardrail Events` (`layer=request_size`, `decision=blocked`)

## 9) Rate limit responses (429)

**Symptom**

- Client calls receive `429` responses intermittently.

**Why it happens**

- Rate limiting middleware enforces window + max-request policy.
- Limits controlled by `RATE_LIMIT_WINDOW_SECONDS` and `RATE_LIMIT_MAX_REQUESTS`.

**How to fix**

- Increase limits for expected traffic pattern or smooth client bursts.
- Ensure upstream retries/backoff are configured to avoid thundering herds.

**What to watch**

- `HTTP Requests by Status Class` (`4xx` increase)
- `HTTP Request Rate by Path`

## 10) Embedding config change blocked with `409`

**Symptom**

- `PUT /admin/embedding` returns conflict (`409`) indicating reprovision in progress.

**Why it happens**

- Admin embedding update is single-flight by design (prevents mixed vector spaces).
- Implemented in `gateway/routers/admin/embeddings.py` and `services/embedding_reprovision.py`.

**How to fix**

- Wait for `GET /admin/embedding/status` to leave `running`.
- Retry config update after current reprovision completes.

**What to watch**

- `HTTP 5xx Error Rate` and readiness if reprovision is long-running
- Admin telemetry for embedding status transitions

## 11) Queryable Encryption readiness is degraded

**Symptom**

- `/health/ready` returns `503` with `checks.encryption=false`.
- The `qe` payload reports missing `crypt_shared` library or key-vault/KMS issues.

**Why it happens**

- QE is enabled (`QE_ENABLED=true`) but one prerequisite is missing:
  - `mongo_crypt_v1.so` not found at `CRYPT_SHARED_LIB_PATH`,
  - key material not loaded (`AWS_KMS_KEY_ARN(_FILE)` or `QE_LOCAL_MASTER_KEY(_FILE)`),
  - KMS endpoint/credentials misconfigured (LocalStack or AWS),
  - key-vault namespace unreadable.

**How to fix**

- Confirm `CRYPT_SHARED_LIB_PATH` exists in the container:
  `ls /opt/mongodb/lib/mongo_crypt_v1.so`.
- For `KMS_PROVIDER=aws`, verify `AWS_KMS_KEY_ARN_FILE` exists and LocalStack/AWS
  is reachable.
- For `KMS_PROVIDER=local`, confirm key file is valid base64 and decodes to 96 bytes.
- Re-run bootstrap after KMS fixes so encrypted collection provisioning can succeed.

**What to watch**

- `/health/ready` `checks.encryption` and `qe.error`
- `HTTP 5xx Error Rate` during rollout

## 12) Code tool blocked: requirement not permitted by the pip policy

**Symptom**

- Saving a code server returns `422`: `Tool '<name>': Code-tool requirement(s) ...`.
- The Functions Studio shows an amber **"awaiting operator"** or red **"not allowed"**
  chip under a tool's *Requirements*, and Save is blocked.
- A `tools/call` (or the sandbox test-run) fails with `Code-tool requirement(s) not
  permitted by the platform pip ceiling (SANDBOX_ALLOWED_REQUIREMENTS): ...` or `... not
  in this tenant's code-package policy: ...`.

**Why it happens**

Code-tool dependencies are gated by a **two-gate, deny-by-default** allowlist (host `pip`
runs outside the wasm jail). A package installs only when it is in **both**:

- the **global operator ceiling** `SANDBOX_ALLOWED_REQUIREMENTS`, and
- the **tenant allowlist** `code_requirements_allowlist`
  (`PUT /admin/tenants/{id}/code-requirements`).

The **effective** policy is the intersection. An empty tenant allowlist ⇒ stdlib-only,
*even if the global ceiling is permissive*. (This is a breaking change from the previous
global-only behavior: tenants must be opted in explicitly.) The error message names which
gate blocked the package and who can unblock it.

**How to fix**

- **Red "not allowed" / "not in this tenant's code-package policy"** → a tenant admin adds
  the bare distribution name under the console's **Code packages** editor (or
  `PUT /admin/tenants/{id}/code-requirements`).
- **Amber "awaiting operator" / "not permitted by the platform pip ceiling"** → a platform
  operator adds the distribution to `SANDBOX_ALLOWED_REQUIREMENTS` and restarts; the tenant
  entry then becomes effective.
- Use bare names in the tenant allowlist (`requests`), and pin the exact version per tool
  in its *Requirements* (`requests==2.32.3`). Wheels only — a package with no matching
  wheel still fails at install with `Failed to install tool requirements`.
- Policy changes take effect within `TENANT_STATUS_CACHE_TTL_SECONDS` across replicas
  (immediately on the acting node); the console refreshes `whoami` on save.

**What to watch**

- `422` rate on `POST /admin/servers` and `POST /admin/code-tools/validate`.
- `DownstreamError` on `tools/call` for `transport="code"` servers.

## Quick triage sequence

1. Check gateway process health (`/health/live`).
2. Check readiness details (`/health/ready`) to isolate `mongo`, `indexes`,
   `embedding`, and `encryption` (when QE is enabled).
3. Open Grafana dashboard:
   - error rate
   - latency quantiles
   - downstream errors
   - auth failures
   - guardrail blocks
4. Validate Prometheus target health (`up` and alert states).
5. Reproduce with one explicit `POST /rpc` call and inspect logs/telemetry.
