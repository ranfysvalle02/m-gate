from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jwt
import pytest

from config.settings import get_settings
from services.credential_broker import JwtCredentialBroker


def _public_key(kid: str):
    jwks = json.loads(Path("config/dev-jwks.json").read_text(encoding="utf-8"))
    jwk = next(key for key in jwks["keys"] if key.get("kid") == kid)
    return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))


def _broker(settings, **overrides):
    object.__setattr__(
        settings,
        "downstream_jwt_private_key",
        Path("config/dev-private-key.pem").read_text(encoding="utf-8"),
    )
    for key, value in overrides.items():
        object.__setattr__(settings, key, value)
    return JwtCredentialBroker(settings=settings)


@pytest.mark.asyncio
async def test_mint_reuses_cached_token_and_sets_workload_claims(reset_settings):
    settings = get_settings()
    broker = _broker(
        settings, downstream_token_ttl_seconds=120, downstream_token_refresh_skew_seconds=15
    )

    first = await broker.mint("orders", tenant_id="tenant-a")
    second = await broker.mint("orders", tenant_id="tenant-a")

    assert first.token_id == second.token_id  # cached reuse
    token = first.headers[settings.downstream_auth_header].split(" ", 1)[1]
    payload = jwt.decode(
        token,
        key=_public_key(settings.downstream_jwt_kid),
        algorithms=["RS256"],
        audience="orders",
        issuer=settings.downstream_jwt_issuer,
    )
    assert payload["tenant_id"] == "tenant-a"
    assert payload["sub"] == "tenant:tenant-a:gateway"
    assert payload["jti"] == first.token_id
    assert payload["exp"] > payload["iat"]
    # Workload identity carries no volatile per-caller claims.
    assert "groups" not in payload
    assert "roles" not in payload
    assert first.env[settings.downstream_token_env_var] == token


@pytest.mark.asyncio
async def test_mint_is_isolated_per_tenant_and_server(reset_settings):
    settings = get_settings()
    broker = _broker(settings)

    a_orders = await broker.mint("orders", tenant_id="tenant-a")
    b_orders = await broker.mint("orders", tenant_id="tenant-b")
    a_weather = await broker.mint("weather", tenant_id="tenant-a")

    assert len({a_orders.token_id, b_orders.token_id, a_weather.token_id}) == 3


@pytest.mark.asyncio
async def test_mint_refreshes_after_expiry(reset_settings):
    settings = get_settings()
    broker = _broker(
        settings, downstream_token_ttl_seconds=1, downstream_token_refresh_skew_seconds=0
    )

    first = await broker.mint("orders", tenant_id="tenant-a")
    await asyncio.sleep(1.1)
    second = await broker.mint("orders", tenant_id="tenant-a")

    assert first.token_id != second.token_id


@pytest.mark.asyncio
async def test_metadata_audience_override_is_honored(reset_settings):
    settings = get_settings()
    broker = _broker(settings)

    credential = await broker.mint(
        "weather",
        tenant_id="tenant-b",
        metadata={"auth": {"audience": "weather-service"}},
    )

    token = credential.headers[settings.downstream_auth_header].split(" ", 1)[1]
    payload = jwt.decode(
        token,
        key=_public_key(settings.downstream_jwt_kid),
        algorithms=["RS256"],
        audience="weather-service",
        issuer=settings.downstream_jwt_issuer,
    )
    assert payload["tenant_id"] == "tenant-b"


@pytest.mark.asyncio
async def test_unsupported_scheme_is_rejected(reset_settings):
    settings = get_settings()
    broker = _broker(settings)
    with pytest.raises(ValueError, match="Unsupported downstream auth scheme"):
        await broker.mint(
            "weather",
            tenant_id="tenant-a",
            metadata={"auth": {"scheme": "basic"}},
        )


@pytest.mark.asyncio
async def test_none_scheme_returns_empty_credential(reset_settings):
    settings = get_settings()
    broker = _broker(settings)
    credential = await broker.mint(
        "weather",
        tenant_id="tenant-a",
        metadata={"auth": {"scheme": "none"}},
    )
    assert credential.headers == {}
    assert credential.env == {}
    assert broker.near_expiry(credential) is False


@pytest.mark.asyncio
async def test_near_expiry_reflects_refresh_skew(reset_settings):
    settings = get_settings()
    broker = _broker(
        settings, downstream_token_ttl_seconds=10, downstream_token_refresh_skew_seconds=0
    )
    credential = await broker.mint("orders", tenant_id="tenant-a")
    assert broker.near_expiry(credential) is False

    object.__setattr__(settings, "downstream_token_refresh_skew_seconds", 3600)
    assert broker.near_expiry(credential) is True


@pytest.mark.asyncio
async def test_mint_fails_when_disabled(reset_settings):
    settings = get_settings()
    object.__setattr__(settings, "downstream_jwt_enabled", False)
    broker = JwtCredentialBroker(settings=settings)

    with pytest.raises(ValueError, match="disabled"):
        await broker.mint("orders", tenant_id="tenant-a")


@pytest.mark.asyncio
async def test_non_jwt_schemes_do_not_require_downstream_jwt_enabled(reset_settings):
    settings = get_settings()
    object.__setattr__(settings, "downstream_jwt_enabled", False)
    broker = JwtCredentialBroker(settings=settings)
    credential = await broker.mint(
        "orders",
        tenant_id="tenant-a",
        metadata={"auth": {"scheme": "none"}},
    )
    assert credential.headers == {}


@pytest.mark.asyncio
async def test_mint_fails_without_signing_key(reset_settings):
    settings = get_settings()
    object.__setattr__(settings, "downstream_jwt_private_key", "")
    broker = JwtCredentialBroker(settings=settings)

    with pytest.raises(ValueError, match="private key"):
        await broker.mint("orders", tenant_id="tenant-a")


@pytest.mark.asyncio
async def test_invalidate_evicts_targeted_cached_credentials(reset_settings):
    settings = get_settings()
    broker = _broker(settings, downstream_token_ttl_seconds=600)
    first = await broker.mint("orders", tenant_id="tenant-a")
    same = await broker.mint("orders", tenant_id="tenant-a")
    assert first.token_id == same.token_id

    await broker.invalidate("orders", tenant_id="tenant-a")
    refreshed = await broker.mint("orders", tenant_id="tenant-a")
    assert refreshed.token_id != first.token_id
