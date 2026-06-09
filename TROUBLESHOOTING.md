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

## 2) `$rankFusion` unavailable, hybrid search quality drops

**Symptom**

- Hybrid retrieval appears degraded or behaves like lexical/vector fallback.
- You may see backend `OperationFailure` related to aggregation features.

**Why it happens**

- `$rankFusion` is not available in some deployments/configurations.
- `services/hybrid_search.py` catches `OperationFailure` and intentionally
  falls back to app-side RRF (`_search_hybrid_app_side`) to keep routing alive.

**How to fix**

- Keep fallback enabled (default behavior is resilient).
- Upgrade/align MongoDB/Atlas feature set if you require server-side rank fusion.
- Verify text and vector indexes are queryable.

**What to watch**

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
- Implemented in `gateway/routers/admin.py` and `services/embedding_reprovision.py`.

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
