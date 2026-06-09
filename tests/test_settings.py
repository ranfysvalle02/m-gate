"""Tests for config.settings validators: REQUIRE_AUTH back-compat, file-backed
secrets, and production safety guards.
"""

from __future__ import annotations

import pytest

from config.settings import Settings


def test_require_auth_backcompat_maps_to_hs256():
    s = Settings(require_auth=True)
    assert s.auth_mode == "hs256"


def test_explicit_auth_mode_not_overridden_by_require_auth():
    s = Settings(require_auth=True, auth_mode="jwks", jwks_uri="https://x/jwks")
    assert s.auth_mode == "jwks"


def test_file_backed_secret_is_loaded(tmp_path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("super-secret-from-file")
    s = Settings(jwt_secret_file=str(secret_file))
    assert s.jwt_secret == "super-secret-from-file"


def test_prod_rejects_disabled_auth():
    with pytest.raises(ValueError, match="auth_mode=disabled"):
        Settings(environment="production", auth_mode="disabled", admin_ui_enabled=False)


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
    )
    assert s.admin_email == "admin@example.com"
