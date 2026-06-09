# Queryable Encryption Guide

This project supports MongoDB Queryable Encryption (QE) for downstream MCP
registry secrets stored in `routing_registry`.

## Scope

When `QE_ENABLED=true`, the gateway provisions `routing_registry` as an
encrypted collection and protects these fields:

- `env` (object)
- `command` (string)
- `args` (array)
- `metadata` (object)

These fields are encrypted client-side before writes and transparently decrypted
on reads through the gateway's MongoDB client.

## Key hierarchy

- **CMK** (customer master key): external root key (`aws` KMS or `local` key).
- **DEKs** (data encryption keys): stored in `encryption.__keyVault`, wrapped by
  the CMK.
- **Encrypted fields**: `routing_registry` fields listed above, encrypted with a
  DEK generated during encrypted collection creation.

## Local development defaults (docker-compose)

`docker-compose.yml` enables QE by default for `bootstrap` and `gateway` using
LocalStack KMS:

- `KMS_PROVIDER=aws`
- `AWS_KMS_ENDPOINT=localhost.localstack.cloud:4566`
- `AWS_KMS_KEY_ARN_FILE=/kms-config/kms_key_id`
- `CRYPT_SHARED_LIB_PATH=/opt/mongodb/lib/mongo_crypt_v1.so`

`kms-init` creates a KMS key in LocalStack and writes its ARN to the shared
`kms_config` volume.

## Local no-KMS mode

Use `KMS_PROVIDER=local` with a base64-encoded 96-byte master key:

```bash
python - <<'PY'
import base64, os
print(base64.b64encode(os.urandom(96)).decode())
PY
```

Store the output in a file and set:

- `QE_LOCAL_MASTER_KEY_FILE=/path/to/local-master-key.b64`

## Production notes

- Use `KMS_PROVIDER=aws` with a real CMK ARN (`AWS_KMS_KEY_ARN` or `_FILE`).
- Back up and protect `encryption.__keyVault` together with your control plane.
- Keep KMS access tightly scoped to the gateway runtime identity.
- Keep `crypt_shared` available in the runtime image and set
  `CRYPT_SHARED_LIB_PATH` correctly.

## Health and readiness

`/health/ready` includes an `encryption` readiness check and a `qe` object when
QE is enabled. If key-vault access or `crypt_shared` resolution fails, readiness
returns degraded (`503`).

## Operational limitations

- This integration uses unindexed encrypted fields for registry secrets; it does
  not currently add encrypted query predicates.
- QE does not support time-series collections; `audit_telemetry` remains
  unencrypted by QE.
- Avoid aggregation/filter patterns that require server-side introspection of
  encrypted payload contents.

## Troubleshooting quick checks

1. Confirm `QE_ENABLED=true` and `KMS_PROVIDER` are set for both `bootstrap` and
   `gateway`.
2. Confirm `AWS_KMS_KEY_ARN_FILE` (or local key file) exists and is readable.
3. Confirm `CRYPT_SHARED_LIB_PATH` points to `mongo_crypt_v1.so`.
4. Check `/health/ready` -> `checks.encryption` and `qe` details.
