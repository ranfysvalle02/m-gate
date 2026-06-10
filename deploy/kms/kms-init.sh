#!/bin/sh
set -eu

KEY_FILE="/kms-config/kms_key_id"
AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL:-http://localstack:4566}"

# Only reuse the cached ARN if that key still exists in the (possibly restarted)
# LocalStack KMS. Community LocalStack does not persist KMS keys across restarts,
# so a cached ARN can dangle and make QE fail with a KMS NotFoundException.
# Recreate the key when the cached one is missing.
if [ -s "$KEY_FILE" ]; then
  CACHED_ARN="$(cat "$KEY_FILE")"
  if aws --endpoint-url "$AWS_ENDPOINT_URL" kms describe-key --key-id "$CACHED_ARN" >/dev/null 2>&1; then
    echo "KMS key already initialized: $CACHED_ARN"
    exit 0
  fi
  echo "Cached KMS key $CACHED_ARN no longer exists in LocalStack; recreating."
fi

KEY_ARN="$(aws --endpoint-url "$AWS_ENDPOINT_URL" kms create-key \
  --description "mdb-mcp-gateway Queryable Encryption key" \
  --query 'KeyMetadata.Arn' \
  --output text)"

aws --endpoint-url "$AWS_ENDPOINT_URL" kms create-alias \
  --alias-name alias/mdb-mcp-gateway-qe \
  --target-key-id "$KEY_ARN" >/dev/null 2>&1 || true

printf "%s" "$KEY_ARN" > "$KEY_FILE"
echo "Initialized KMS key: $KEY_ARN"
