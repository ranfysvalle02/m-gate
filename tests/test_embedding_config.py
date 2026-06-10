"""Tests for the runtime embedding configuration layer: encryption-at-rest,
defaults, validation, control-DB persistence, and active-config resolution.
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from database.mongo import get_control_database, get_tenant_database
from services.embedding_config import (
    EMBEDDING_CONFIG_COLLECTION,
    EMBEDDING_CONFIG_ID,
    EmbeddingConfig,
    active_embedding_identity,
    decrypt_api_key,
    default_config_from_settings,
    encrypt_api_key,
    get_embedding_service_for,
    invalidate_tenant_embedding,
    load_persisted_config,
    load_tenant_config,
    refresh_active_embedding_config,
    resolve_dimensions,
    save_persisted_config,
    save_tenant_config,
    tenant_embedding_identity,
    validate_config,
)


def _settings(**overrides) -> Settings:
    base = {"embedding_secret": "unit-test-embedding-secret"}
    base.update(overrides)
    return Settings(**base)


def test_encrypt_decrypt_round_trip_and_marker():
    settings = _settings()
    token = encrypt_api_key("sk-super-secret", settings)
    assert token.startswith("enc::")
    assert "sk-super-secret" not in token
    assert decrypt_api_key(token, settings) == "sk-super-secret"


def test_decrypt_treats_non_encrypted_values_as_absent():
    # Keys are always written encrypted; a missing/plaintext value is "no key".
    assert decrypt_api_key("plain-key", _settings()) == ""
    assert decrypt_api_key(None, _settings()) == ""


def test_decrypt_with_wrong_secret_returns_empty():
    token = encrypt_api_key("sk-secret", _settings(embedding_secret="secret-a"))
    assert decrypt_api_key(token, _settings(embedding_secret="secret-b")) == ""


def test_api_key_hint_masks_value():
    cfg = EmbeddingConfig(provider="openai", model="m", api_key="sk-abcd1234")
    assert cfg.has_api_key is True
    assert cfg.api_key_hint is not None
    assert cfg.api_key_hint.endswith("1234")
    assert "sk-abcd" not in cfg.api_key_hint


def test_default_config_from_settings_ollama():
    cfg = default_config_from_settings(_settings(ollama_model="nomic", ollama_dimensions=768))
    assert cfg.provider == "ollama"
    assert cfg.model == "nomic"
    assert cfg.dimensions == 768
    assert cfg.source == "env"


def test_default_config_from_settings_openai_defers_dimensions():
    cfg = default_config_from_settings(_settings(embedding_provider="openai"))
    assert cfg.provider == "openai"
    assert cfg.model == "text-embedding-3-small"
    # Cloud providers detect width at runtime, so the default is "unknown".
    assert cfg.dimensions == 0


@pytest.mark.parametrize(
    "config,message",
    [
        (EmbeddingConfig(provider="openai", model="", api_key="k"), "requires a model"),
        (EmbeddingConfig(provider="openai", model="m", api_key=""), "requires an API key"),
        (EmbeddingConfig(provider="bogus", model="m"), "Unsupported provider"),
        (
            EmbeddingConfig(provider="azure_openai", model="dep", api_key="k"),
            "requires an endpoint",
        ),
    ],
)
def test_validate_config_rejects_incomplete(config, message):
    with pytest.raises(ValueError, match=message):
        validate_config(config)


def test_validate_config_accepts_complete_azure():
    validate_config(
        EmbeddingConfig(
            provider="azure_openai",
            model="dep",
            api_key="k",
            azure_endpoint="https://x.openai.azure.com",
            azure_deployment="dep",
        )
    )


@pytest.mark.asyncio
async def test_resolve_dimensions_keeps_known_width():
    cfg = EmbeddingConfig(provider="ollama", model="nomic", dimensions=768)
    resolved = await resolve_dimensions(cfg, _settings())
    assert resolved.dimensions == 768


@pytest.mark.asyncio
async def test_resolve_dimensions_falls_back_when_detection_fails(monkeypatch):
    # An unreachable provider must not crash startup; we fall back to ollama_dimensions.
    from fakes import FakeEmbeddingService

    import services.embedding_config as ec

    monkeypatch.setattr(
        ec, "build_provider_service", lambda config, settings=None: FakeEmbeddingService(fail=True)
    )
    cfg = EmbeddingConfig(provider="openai", model="text-embedding-3-small", api_key="k")
    settings = _settings(ollama_dimensions=99)
    resolved = await resolve_dimensions(cfg, settings)
    assert resolved.dimensions == 99


@pytest.mark.asyncio
async def test_load_returns_env_default_when_unset(patch_mongo):
    cfg = await load_persisted_config()
    assert cfg.provider == "ollama"
    assert cfg.source == "env"


@pytest.mark.asyncio
async def test_save_and_load_round_trip_encrypts_key(patch_mongo):
    cfg = EmbeddingConfig(
        provider="openai",
        model="text-embedding-3-small",
        api_key="sk-secret-123456",
        dimensions=1536,
    )
    await save_persisted_config(cfg, updated_by="admin@example.com")

    raw = await get_control_database()[EMBEDDING_CONFIG_COLLECTION].find_one(
        {"_id": EMBEDDING_CONFIG_ID}
    )
    assert raw["api_key_encrypted"].startswith("enc::")
    assert "sk-secret-123456" not in raw["api_key_encrypted"]

    loaded = await load_persisted_config()
    assert loaded.provider == "openai"
    assert loaded.model == "text-embedding-3-small"
    assert loaded.dimensions == 1536
    assert loaded.api_key == "sk-secret-123456"
    assert loaded.updated_by == "admin@example.com"
    assert loaded.source == "db"


@pytest.mark.asyncio
async def test_refresh_sets_active_identity(patch_mongo):
    cfg = EmbeddingConfig(
        provider="voyage",
        model="voyage-3",
        api_key="pa-key",
        dimensions=1024,
    )
    await save_persisted_config(cfg)
    refreshed = await refresh_active_embedding_config()
    assert refreshed.provider == "voyage"
    model_id, dimensions, version = active_embedding_identity()
    assert model_id == "voyage-3"
    assert dimensions == 1024
    assert version == "voyage-3:1024"


@pytest.mark.asyncio
async def test_load_tenant_config_falls_back_to_platform_default(patch_mongo):
    cfg = EmbeddingConfig(
        provider="voyage",
        model="voyage-3",
        api_key="platform-key",
        dimensions=1024,
    )
    await save_persisted_config(cfg)
    loaded = await load_tenant_config("tenant-a")
    assert loaded.provider == "voyage"
    assert loaded.model == "voyage-3"
    assert loaded.dimensions == 1024
    assert loaded.source == "platform-default"


@pytest.mark.asyncio
async def test_save_and_load_tenant_round_trip_encrypts_key(patch_mongo):
    cfg = EmbeddingConfig(
        provider="openai",
        model="text-embedding-3-small",
        api_key="sk-tenant-secret-123456",
        dimensions=1536,
    )
    await save_tenant_config("tenant-a", cfg, updated_by="tenant-admin@example.com")

    raw = await get_tenant_database("tenant-a")[EMBEDDING_CONFIG_COLLECTION].find_one(
        {"_id": EMBEDDING_CONFIG_ID}
    )
    assert raw is not None
    assert raw["api_key_encrypted"].startswith("enc::")
    assert "sk-tenant-secret-123456" not in raw["api_key_encrypted"]

    loaded = await load_tenant_config("tenant-a")
    assert loaded.provider == "openai"
    assert loaded.model == "text-embedding-3-small"
    assert loaded.dimensions == 1536
    assert loaded.api_key == "sk-tenant-secret-123456"
    assert loaded.updated_by == "tenant-admin@example.com"
    assert loaded.source == "tenant-db"


@pytest.mark.asyncio
async def test_get_embedding_service_for_caches_tenant_service(patch_mongo, monkeypatch):
    from fakes import FakeEmbeddingService

    import services.embedding_config as ec

    await save_tenant_config(
        "tenant-a",
        EmbeddingConfig(provider="ollama", model="tenant-model", dimensions=8),
    )

    calls = {"count": 0}

    def _build(config, settings=None):
        calls["count"] += 1
        return FakeEmbeddingService(dimensions=config.dimensions or 8, model_id=config.model)

    monkeypatch.setattr(ec, "build_provider_service", _build)
    svc1 = await get_embedding_service_for("tenant-a")
    svc2 = await get_embedding_service_for("tenant-a")
    assert svc1 is svc2
    assert calls["count"] == 1

    invalidate_tenant_embedding("tenant-a")
    svc3 = await get_embedding_service_for("tenant-a")
    assert svc3 is not svc1
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_tenant_embedding_identity_uses_tenant_specific_service(patch_mongo, monkeypatch):
    from fakes import FakeEmbeddingService

    import services.embedding_config as ec

    await save_tenant_config(
        "tenant-a",
        EmbeddingConfig(provider="ollama", model="tenant-model", dimensions=12),
    )

    monkeypatch.setattr(
        ec,
        "build_provider_service",
        lambda config, settings=None: FakeEmbeddingService(
            dimensions=config.dimensions or 12,
            model_id=config.model or "tenant-model",
        ),
    )
    model_id, dimensions, version = await tenant_embedding_identity("tenant-a")
    assert model_id == "tenant-model"
    assert dimensions == 12
    assert version == "tenant-model:12"
