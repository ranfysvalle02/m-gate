"""Tests for config.settings validators: file-backed secrets, embedding
defaults, and production safety guards.
"""

from __future__ import annotations

import base64
import os

import pytest

from config.settings import Settings


def test_file_backed_secret_is_loaded(tmp_path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("super-secret-from-file")
    s = Settings(jwt_secret_file=str(secret_file))
    assert s.jwt_secret == "super-secret-from-file"


def test_disabled_auth_mode_is_rejected():
    """The open 'disabled' auth mode no longer exists; security is always on."""
    with pytest.raises(ValueError, match="auth_mode"):
        Settings(auth_mode="disabled")


def test_prod_rejects_weak_hs256_secret():
    with pytest.raises(ValueError, match="too weak"):
        Settings(
            environment="production",
            auth_mode="hs256",
            jwt_secret="dev-secret",
            admin_ui_enabled=False,
        )


def test_prod_accepts_strong_hs256_secret():
    s = Settings(
        environment="production",
        auth_mode="hs256",
        jwt_secret="a-sufficiently-long-secret-value",
        cors_allow_origins="https://app.example.com",
        admin_ui_enabled=False,
        downstream_jwt_enabled=False,
    )
    assert s.auth_mode == "hs256"


def test_prod_jwks_requires_issuer_and_audience():
    with pytest.raises(ValueError, match="jwt_issuer and jwt_audience"):
        Settings(
            environment="production",
            auth_mode="jwks",
            jwks_uri="https://issuer/jwks",
            admin_ui_enabled=False,
        )


def test_prod_jwks_requires_a_key_source():
    with pytest.raises(ValueError, match="jwks_uri or jwks_local_path"):
        Settings(
            environment="production",
            auth_mode="jwks",
            jwt_issuer="iss",
            jwt_audience="aud",
            admin_ui_enabled=False,
        )


def test_prod_jwks_valid_config_passes():
    s = Settings(
        environment="production",
        auth_mode="jwks",
        jwt_issuer="iss",
        jwt_audience="aud",
        jwks_local_path="/tmp/jwks.json",
        cors_allow_origins="https://app.example.com",
        admin_ui_enabled=False,
        downstream_jwt_enabled=False,
    )
    assert s.auth_mode == "jwks"


def test_prod_rejects_wildcard_cors():
    with pytest.raises(ValueError, match="Wildcard cors_allow_origins"):
        Settings(
            environment="production",
            auth_mode="hs256",
            jwt_secret="a-sufficiently-long-secret-value",
            cors_allow_origins="*",
            admin_ui_enabled=False,
        )


def test_prod_accepts_explicit_cors_origins():
    s = Settings(
        environment="production",
        auth_mode="hs256",
        jwt_secret="a-sufficiently-long-secret-value",
        cors_allow_origins="https://app.example.com,https://admin.example.com",
        admin_ui_enabled=False,
        downstream_jwt_enabled=False,
    )
    assert s.cors_allow_origins == "https://app.example.com,https://admin.example.com"


def test_dev_allows_wildcard_cors():
    s = Settings(environment="development", cors_allow_origins="*")
    assert s.cors_allow_origins == "*"


def test_admin_ui_path_is_normalized():
    s = Settings(admin_ui_path="ui")
    assert s.admin_ui_path == "/ui"


def test_admin_session_secret_defaults_to_jwt_secret():
    s = Settings(jwt_secret="abc1234567890")
    assert s.admin_session_secret == "abc1234567890"


def test_prod_admin_ui_requires_credentials():
    with pytest.raises(ValueError, match="admin_email is required"):
        Settings(
            environment="production",
            auth_mode="hs256",
            jwt_secret="a-sufficiently-long-secret-value",
            cors_allow_origins="https://app.example.com",
            admin_ui_enabled=True,
        )


def test_embedding_provider_unset_auto_selects_ollama_offline():
    # The provider is intentionally unset by default so resolution can auto-select:
    # with no VOYAGE_API_KEY the offline Ollama default applies.
    from services.embedding_config import default_config_from_settings

    s = Settings()
    assert s.embedding_provider is None
    assert default_config_from_settings(s).provider == "ollama"
    assert s.embedding_model is None
    assert s.azure_openai_api_version == "2023-05-15"


def test_embedding_secret_defaults_to_session_secret():
    s = Settings(jwt_secret="abc1234567890")
    # Falls back through admin_session_secret -> jwt_secret when unset.
    assert s.embedding_secret == "abc1234567890"


def test_embedding_api_key_is_file_backed(tmp_path):
    key_file = tmp_path / "embed.key"
    key_file.write_text("sk-from-file")
    s = Settings(embedding_api_key_file=str(key_file))
    assert s.embedding_api_key == "sk-from-file"


def test_prod_admin_ui_with_strong_credentials_passes():
    s = Settings(
        environment="production",
        auth_mode="hs256",
        jwt_secret="a-sufficiently-long-secret-value",
        cors_allow_origins="https://app.example.com",
        admin_ui_enabled=True,
        admin_email="admin@example.com",
        admin_password="a-long-and-strong-password",
        admin_session_secret="another-long-secret-value",
        downstream_jwt_enabled=False,
    )
    assert s.admin_email == "admin@example.com"


def test_prod_rejects_bundled_dev_downstream_key():
    with pytest.raises(ValueError, match="dev signing key must not be used in production"):
        Settings(
            environment="production",
            auth_mode="hs256",
            jwt_secret="a-sufficiently-long-secret-value",
            cors_allow_origins="https://app.example.com",
            admin_ui_enabled=False,
            downstream_jwt_enabled=True,
        )


def test_prod_accepts_explicit_downstream_key():
    s = Settings(
        environment="production",
        auth_mode="hs256",
        jwt_secret="a-sufficiently-long-secret-value",
        cors_allow_origins="https://app.example.com",
        admin_ui_enabled=False,
        downstream_jwt_enabled=True,
        downstream_jwt_private_key_file=None,
        downstream_jwt_private_key="-----BEGIN PRIVATE KEY-----prod-----END PRIVATE KEY-----",
    )
    assert s.downstream_jwt_enabled is True


def test_prod_qe_rejects_none_kms_provider():
    with pytest.raises(ValueError, match="KMS_PROVIDER=local or KMS_PROVIDER=aws"):
        Settings(
            environment="production",
            auth_mode="hs256",
            jwt_secret="a-sufficiently-long-secret-value",
            cors_allow_origins="https://app.example.com",
            admin_ui_enabled=False,
            downstream_jwt_enabled=False,
            qe_enabled=True,
            kms_provider="none",
        )


def test_prod_qe_local_rejects_invalid_base64_key():
    with pytest.raises(ValueError, match="valid base64"):
        Settings(
            environment="production",
            auth_mode="hs256",
            jwt_secret="a-sufficiently-long-secret-value",
            cors_allow_origins="https://app.example.com",
            admin_ui_enabled=False,
            downstream_jwt_enabled=False,
            qe_enabled=True,
            kms_provider="local",
            qe_local_master_key="not-base64",
        )


def test_prod_qe_local_rejects_wrong_key_length():
    bad_key = base64.b64encode(os.urandom(64)).decode("utf-8")
    with pytest.raises(ValueError, match="exactly 96 bytes"):
        Settings(
            environment="production",
            auth_mode="hs256",
            jwt_secret="a-sufficiently-long-secret-value",
            cors_allow_origins="https://app.example.com",
            admin_ui_enabled=False,
            downstream_jwt_enabled=False,
            qe_enabled=True,
            kms_provider="local",
            qe_local_master_key=bad_key,
        )


def test_prod_qe_aws_requires_key_arn():
    with pytest.raises(ValueError, match="AWS_KMS_KEY_ARN"):
        Settings(
            environment="production",
            auth_mode="hs256",
            jwt_secret="a-sufficiently-long-secret-value",
            cors_allow_origins="https://app.example.com",
            admin_ui_enabled=False,
            downstream_jwt_enabled=False,
            qe_enabled=True,
            kms_provider="aws",
        )


def test_prod_qe_aws_with_key_arn_is_valid():
    s = Settings(
        environment="production",
        auth_mode="hs256",
        jwt_secret="a-sufficiently-long-secret-value",
        cors_allow_origins="https://app.example.com",
        admin_ui_enabled=False,
        downstream_jwt_enabled=False,
        qe_enabled=True,
        kms_provider="aws",
        aws_kms_key_arn="arn:aws:kms:us-east-1:000000:key/abc",
    )
    assert s.qe_enabled is True
