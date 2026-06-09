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
