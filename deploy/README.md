# Deployment Notes

## Atlas Connection Security

The gateway supports file-backed secrets and Atlas connection hardening via env vars:

- `MONGODB_URI` or `MONGODB_URI_FILE`
- `ATLAS_TLS=true`
- `ATLAS_TLS_CA_FILE=/path/to/ca.pem`
- `ATLAS_AUTH_SOURCE=admin`
- `ATLAS_AUTH_MECHANISM=SCRAM-SHA-256`
- `ATLAS_USERNAME=<username>`
- `ATLAS_PASSWORD` or `ATLAS_PASSWORD_FILE`

For X.509 client certificate auth, use a URI in `MONGODB_URI` that includes
the certificate/key options required by your Atlas deployment.

## Observability

The gateway exposes Prometheus metrics at `GET /metrics` and keeps health probes
at `GET /health/live` and `GET /health/ready`.

For a local demo stack with Prometheus + Grafana already wired, run:

```bash
docker compose up --build
```

- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000`
- Dashboard JSON: `deploy/grafana/dashboard.json`
- Alert rules: `deploy/prometheus/alerts.yaml`

### Prometheus Operator (`ServiceMonitor`)

If your cluster uses Prometheus Operator, apply
[`deploy/k8s/servicemonitor.yaml`](k8s/servicemonitor.yaml) and ensure labels
match your Prometheus release selector.

### Raw scrape snippet (non-Operator)

```yaml
scrape_configs:
  - job_name: mdb-mcp-gateway
    metrics_path: /metrics
    static_configs:
      - targets: ["mdb-mcp-gateway.mdb-mcp-gateway.svc.cluster.local:80"]
```
