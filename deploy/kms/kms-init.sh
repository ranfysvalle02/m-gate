#!/bin/sh
set -eu

KEY_FILE="/kms-config/kms_key_id"

if [ -s "$KEY_FILE" ]; then
  echo "KMS key already initialized: $(cat "$KEY_FILE")"
  exit 0
fi

AWS_ENDPOINT_URL="${AWS_ENDPOINT_URL:-http://localstack:4566}"

KEY_ARN="$(aws --endpoint-url "$AWS_ENDPOINT_URL" kms create-key \
  --description "mdb-mcp-gateway Queryable Encryption key" \
  --query 'KeyMetadata.Arn' \
  --output text)"

aws --endpoint-url "$AWS_ENDPOINT_URL" kms create-alias \
  --alias-name alias/mdb-mcp-gateway-qe \
  --target-key-id "$KEY_ARN" >/dev/null 2>&1 || true

printf "%s" "$KEY_ARN" > "$KEY_FILE"
echo "Initialized KMS key: $KEY_ARN"
