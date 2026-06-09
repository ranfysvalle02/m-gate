from __future__ import annotations

import base64
import os

import pytest

from config.settings import Settings
from database.encryption import (
    ROUTING_REGISTRY_ENCRYPTED_FIELDS,
    build_auto_encryption_opts,
    kms_providers,
    master_key,
)


def _b64_key() -> str:
    return base64.b64encode(os.urandom(96)).decode("utf-8")


def test_routing_registry_encrypted_fields_shape() -> None:
    fields = ROUTING_REGISTRY_ENCRYPTED_FIELDS["fields"]
    assert [field["path"] for field in fields] == ["env", "command", "args", "metadata"]
    assert all(field["keyId"] is None for field in fields)


def test_kms_providers_local_uses_decoded_key() -> None:
    settings = Settings(kms_provider="local", qe_local_master_key=_b64_key())
    providers = kms_providers(settings)
    assert "local" in providers
    assert isinstance(providers["local"]["key"], bytes)
    assert len(providers["local"]["key"]) == 96


def test_kms_providers_aws_uses_configured_credentials() -> None:
    settings = Settings(
        kms_provider="aws",
        aws_access_key_id="test-id",
        aws_secret_access_key="test-secret",
        aws_kms_key_arn="arn:aws:kms:us-east-1:000000:key/abc",
    )
    providers = kms_providers(settings)
    assert providers["aws"]["accessKeyId"] == "test-id"
    assert providers["aws"]["secretAccessKey"] == "test-secret"


def test_master_key_aws_includes_endpoint() -> None:
    settings = Settings(
        kms_provider="aws",
        aws_kms_key_arn="arn:aws:kms:us-east-1:000000:key/abc",
        aws_default_region="us-east-1",
        aws_kms_endpoint="localhost.localstack.cloud:4566",
    )
    payload = master_key(settings)
    assert payload["key"] == "arn:aws:kms:us-east-1:000000:key/abc"
    assert payload["region"] == "us-east-1"
    assert payload["endpoint"] == "localhost.localstack.cloud:4566"


def test_build_auto_encryption_opts_uses_crypt_shared_path() -> None:
    settings = Settings(
        kms_provider="local",
        qe_local_master_key=_b64_key(),
        qe_key_vault_namespace="encryption.__keyVault",
        crypt_shared_lib_path="/tmp/mongo_crypt_v1.so",
    )
    opts = build_auto_encryption_opts(settings)
    assert opts._key_vault_namespace == "encryption.__keyVault"
    assert opts._crypt_shared_lib_path == "/tmp/mongo_crypt_v1.so"
    assert opts._crypt_shared_lib_required is True


def test_kms_providers_rejects_none_provider() -> None:
    with pytest.raises(ValueError, match="KMS_PROVIDER"):
        kms_providers(Settings(kms_provider="none"))
