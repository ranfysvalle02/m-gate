"""Tests for the runtime embedding configuration layer: encryption-at-rest,
defaults, validation, control-DB persistence, and active-config resolution.
"""

from __future__ import annotations

import pytest
from bson.binary import Binary

from config.settings import Settings
from database.mongo import get_control_database, get_tenant_database
from services.embedding_config import (
    EMBEDDING_CONFIG_COLLECTION,
    EMBEDDING_CONFIG_ID,
    EmbeddingConfig,
    active_embedding_identity,
    decrypt_api_key,
    default_config_from_settings,
    delete_tenant_config,
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
    # _env_file=None keeps these unit settings hermetic: a developer's local .env
    # (e.g. VOYAGE_API_KEY) must never leak into env-default resolution here.
    return Settings(_env_file=None, **base)


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


def test_voyage_api_key_alone_selects_and_authenticates_voyage():
    # The Voyage drop-in: VOYAGE_API_KEY by itself promotes the default provider
    # (ollama) to voyage and supplies the key, with no other config required.
    cfg = default_config_from_settings(_settings(voyage_api_key="pa-secret"))
    assert cfg.provider == "voyage"
    assert cfg.model == "voyage-3"
    assert cfg.api_key == "pa-secret"
    assert cfg.dimensions == 0  # detected at runtime


def test_explicit_provider_overrides_voyage_drop_in():
    cfg = default_config_from_settings(
        _settings(embedding_provider="openai", voyage_api_key="pa-secret")
    )
    assert cfg.provider == "openai"
    # The Voyage key must never leak into a different provider's config.
    assert cfg.api_key == ""


def test_explicit_ollama_is_respected_even_with_voyage_key():
    # Pinning EMBEDDING_PROVIDER=ollama is an explicit choice and must win over the
    # Voyage drop-in: a stray VOYAGE_API_KEY can never silently flip a pinned
    # provider. (Auto-selection only applies when the provider is left unset.)
    cfg = default_config_from_settings(
        _settings(embedding_provider="ollama", voyage_api_key="pa-secret")
    )
    assert cfg.provider == "ollama"
    assert cfg.api_key == ""


def test_unset_provider_without_voyage_key_defaults_to_ollama():
    cfg = default_config_from_settings(_settings(embedding_provider=None))
    assert cfg.provider == "ollama"


def test_generic_embedding_api_key_wins_over_voyage_key():
    cfg = default_config_from_settings(
        _settings(
            embedding_provider="voyage",
            embedding_api_key="explicit-key",
            voyage_api_key="pa-secret",
        )
    )
    assert cfg.provider == "voyage"
    assert cfg.api_key == "explicit-key"


@pytest.mark.asyncio
async def test_load_persisted_voyage_falls_back_to_env_key(patch_mongo):
    # An admin can switch the platform default to Voyage in the UI without
    # re-pasting the key when VOYAGE_API_KEY is already in the environment.
    settings = _settings(voyage_api_key="pa-env-key")
    cfg = EmbeddingConfig(provider="voyage", model="voyage-3", api_key="", dimensions=1024)
    await save_persisted_config(cfg, settings)
    loaded = await load_persisted_config(settings)
    assert loaded.provider == "voyage"
    assert loaded.api_key == "pa-env-key"
    assert loaded.source == "db"


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
async def test_resolve_dimensions_ollama_falls_back_when_detection_fails(monkeypatch):
    # Ollama has a *declared* width, so an unreachable probe falls back to it
    # rather than crashing the offline default path.
    from fakes import FakeEmbeddingService

    import services.embedding_config as ec

    monkeypatch.setattr(
        ec, "build_provider_service", lambda config, settings=None: FakeEmbeddingService(fail=True)
    )
    cfg = EmbeddingConfig(provider="ollama", model="nomic-embed-text")
    settings = _settings(ollama_dimensions=99)
    resolved = await resolve_dimensions(cfg, settings)
    assert resolved.dimensions == 99


@pytest.mark.parametrize("provider", ["voyage", "openai", "azure_openai", "gemini"])
@pytest.mark.asyncio
async def test_resolve_dimensions_cloud_provider_fails_loudly(monkeypatch, provider):
    # Cloud providers have no safe declared width: guessing one (e.g. reusing
    # Ollama's 768) would build a wrong-width vector index and corrupt retrieval.
    # The resolver must raise instead of silently falling back to ollama_dimensions.
    from fakes import FakeEmbeddingService

    import services.embedding_config as ec
    from services.embeddings import EmbeddingUnavailableError

    monkeypatch.setattr(
        ec, "build_provider_service", lambda config, settings=None: FakeEmbeddingService(fail=True)
    )
    cfg = EmbeddingConfig(provider=provider, model="some-model", api_key="k")
    with pytest.raises(EmbeddingUnavailableError):
        await resolve_dimensions(cfg, _settings(ollama_dimensions=99))


@pytest.mark.asyncio
async def test_load_returns_env_default_when_unset(patch_mongo):
    # Pass hermetic settings so a developer's local .env (e.g. VOYAGE_API_KEY)
    # cannot flip the asserted env default away from ollama.
    cfg = await load_persisted_config(_settings())
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
async def test_delete_tenant_config_reverts_to_platform_default(patch_mongo):
    platform = EmbeddingConfig(
        provider="voyage",
        model="voyage-3",
        api_key="platform-key",
        dimensions=1024,
    )
    await save_persisted_config(platform)
    await save_tenant_config(
        "tenant-a",
        EmbeddingConfig(
            provider="openai", model="text-embedding-3-small", api_key="k", dimensions=8
        ),
    )
    assert (await load_tenant_config("tenant-a")).source == "tenant-db"

    deleted = await delete_tenant_config("tenant-a")
    assert deleted is True

    reverted = await load_tenant_config("tenant-a")
    assert reverted.source == "platform-default"
    assert reverted.provider == "voyage"

    # Deleting again is a harmless no-op that reports nothing was removed.
    assert await delete_tenant_config("tenant-a") is False


@pytest.mark.asyncio
async def test_delete_tenant_config_invalidates_cached_service(patch_mongo, monkeypatch):
    from fakes import FakeEmbeddingService

    import services.embedding_config as ec

    await save_tenant_config(
        "tenant-a",
        EmbeddingConfig(provider="ollama", model="tenant-model", dimensions=8),
    )
    monkeypatch.setattr(
        ec,
        "build_provider_service",
        lambda config, settings=None: FakeEmbeddingService(
            dimensions=config.dimensions or 8, model_id=config.model or "x"
        ),
    )
    cached = await get_embedding_service_for("tenant-a")
    assert cached.model_id == "tenant-model"

    await delete_tenant_config("tenant-a")
    # Cache was dropped, so the tenant now resolves to the platform proxy.
    after = await get_embedding_service_for("tenant-a")
    assert after is not cached


@pytest.mark.asyncio
async def test_tenant_api_key_uses_qe_scheme_when_enabled(patch_mongo, monkeypatch):
    import services.embedding_config as ec

    settings = _settings(
        qe_enabled=True,
        kms_provider="local",
        qe_local_master_key="a" * 128,
    )
    calls = {"encrypt": 0, "decrypt": 0}

    async def _encrypt_tenant_secret(tenant_id: str, plaintext: str, settings=None) -> Binary:
        assert tenant_id == "tenant-qe"
        assert plaintext == "sk-tenant-qe-secret"
        calls["encrypt"] += 1
        return Binary(b"cipher-bytes", subtype=6)

    async def _decrypt_tenant_secret(ciphertext: Binary, settings=None) -> str:
        assert bytes(ciphertext) == b"cipher-bytes"
        calls["decrypt"] += 1
        return "sk-tenant-qe-secret"

    monkeypatch.setattr(ec, "encrypt_tenant_secret", _encrypt_tenant_secret)
    monkeypatch.setattr(ec, "decrypt_tenant_secret", _decrypt_tenant_secret)

    await save_tenant_config(
        "tenant-qe",
        EmbeddingConfig(
            provider="openai",
            model="text-embedding-3-small",
            api_key="sk-tenant-qe-secret",
            dimensions=1536,
        ),
        settings=settings,
    )
    raw = await get_tenant_database("tenant-qe")[EMBEDDING_CONFIG_COLLECTION].find_one(
        {"_id": EMBEDDING_CONFIG_ID}
    )
    assert raw is not None
    assert raw["api_key_encrypted"].startswith("qe::")
    assert "sk-tenant-qe-secret" not in raw["api_key_encrypted"]

    loaded = await load_tenant_config("tenant-qe", settings=settings)
    assert loaded.api_key == "sk-tenant-qe-secret"
    assert calls == {"encrypt": 1, "decrypt": 1}


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
