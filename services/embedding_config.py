"""Runtime-mutable, provider-agnostic embedding configuration.

The gateway historically read a single Ollama embedding config from the
environment. This module makes the embedding backend a first-class, runtime
setting: a single **global** configuration document persisted in the control DB
(API keys encrypted at rest), layered on top of the env defaults, and surfaced
through a stable proxy so existing call sites keep working after a change.

Key ideas:
- ``EmbeddingConfig`` is the resolved, in-memory shape (decrypted key included).
- The persisted control-DB document stores the key *encrypted* and never returns
  it in plaintext to the admin API.
- ``get_active_embedding_service()`` returns a process-wide proxy. All the
  existing ``get_embedding_service()`` callers transparently follow config
  changes because they hold the proxy, not a concrete provider instance.
- ``dimensions`` is detected at runtime (by embedding a probe string) whenever it
  is unknown, so operators never hand-configure vector widths.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from config.settings import Settings, get_settings
from database.mongo import get_control_database
from services.embeddings import (
    PROVIDER_DEFAULT_MODELS,
    SUPPORTED_PROVIDERS,
    BaseHttpEmbeddingService,
    EmbeddingService,
    build_provider_service,
    embedding_version_for,
)

logger = logging.getLogger(__name__)

EMBEDDING_CONFIG_COLLECTION = "gateway_config"
EMBEDDING_CONFIG_ID = "embedding"
_ENC_PREFIX = "enc::"


@dataclass
class EmbeddingConfig:
    provider: str = "ollama"
    model: str = ""
    base_url: str | None = None
    dimensions: int = 0
    api_key: str = ""
    azure_endpoint: str | None = None
    azure_api_version: str = "2023-05-15"
    azure_deployment: str | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None
    # Where the config came from: "env" (defaults) or "db" (admin override).
    source: str = "env"

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def api_key_hint(self) -> str | None:
        if not self.api_key:
            return None
        tail = self.api_key[-4:]
        return f"\u2022\u2022\u2022\u2022{tail}"


# --------------------------------------------------------------------------- #
# Encryption helpers
# --------------------------------------------------------------------------- #
def _fernet(settings: Settings) -> Fernet:
    secret = (
        settings.embedding_secret
        or settings.admin_session_secret
        or settings.jwt_secret
        or "dev-secret"
    )
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_api_key(plaintext: str, settings: Settings | None = None) -> str:
    if not plaintext:
        return ""
    settings = settings or get_settings()
    token = _fernet(settings).encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return f"{_ENC_PREFIX}{token}"


def decrypt_api_key(stored: str | None, settings: Settings | None = None) -> str:
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        # Back-compat / hand-edited plaintext: pass through unchanged.
        return stored
    settings = settings or get_settings()
    try:
        return _fernet(settings).decrypt(stored[len(_ENC_PREFIX) :].encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        logger.warning("Failed to decrypt stored embedding API key: %s", exc)
        return ""


# --------------------------------------------------------------------------- #
# Defaults + validation
# --------------------------------------------------------------------------- #
def default_model_for(provider: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if settings.embedding_model:
        return settings.embedding_model
    if provider == "ollama":
        return settings.ollama_model
    if provider == "azure_openai":
        return settings.azure_openai_deployment or ""
    return PROVIDER_DEFAULT_MODELS.get(provider, "")


def default_config_from_settings(settings: Settings | None = None) -> EmbeddingConfig:
    settings = settings or get_settings()
    provider = settings.embedding_provider
    if provider not in SUPPORTED_PROVIDERS:
        provider = "ollama"
    base_url = settings.embedding_base_url
    if provider == "ollama":
        base_url = base_url or settings.ollama_base_url
        dimensions = settings.ollama_dimensions
    else:
        dimensions = 0
    return EmbeddingConfig(
        provider=provider,
        model=default_model_for(provider, settings),
        base_url=base_url,
        dimensions=dimensions,
        api_key=settings.embedding_api_key or "",
        azure_endpoint=settings.azure_openai_endpoint,
        azure_api_version=settings.azure_openai_api_version,
        azure_deployment=settings.azure_openai_deployment,
        source="env",
    )


def validate_config(config: EmbeddingConfig) -> None:
    """Raise ``ValueError`` if a candidate config is structurally incomplete."""
    if config.provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported provider '{config.provider}'. "
            f"Choose one of: {', '.join(SUPPORTED_PROVIDERS)}."
        )
    if config.provider == "azure_openai":
        if not (config.azure_deployment or config.model):
            raise ValueError("Azure OpenAI requires a deployment name.")
        if not config.azure_endpoint:
            raise ValueError("Azure OpenAI requires an endpoint.")
        if not config.api_key:
            raise ValueError("Azure OpenAI requires an API key.")
        return
    if not config.model:
        raise ValueError(f"Provider '{config.provider}' requires a model name.")
    if config.provider in {"openai", "voyage", "gemini"} and not config.api_key:
        raise ValueError(f"Provider '{config.provider}' requires an API key.")


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _config_from_doc(doc: dict[str, Any], settings: Settings) -> EmbeddingConfig:
    api_key = decrypt_api_key(doc.get("api_key_encrypted"), settings)
    if not api_key:
        # Allow the API key to live purely in the environment even when the rest
        # of the config is DB-managed.
        api_key = settings.embedding_api_key or ""
    return EmbeddingConfig(
        provider=str(doc.get("provider") or settings.embedding_provider),
        model=str(doc.get("model") or ""),
        base_url=doc.get("base_url"),
        dimensions=int(doc.get("dimensions") or 0),
        api_key=api_key,
        azure_endpoint=doc.get("azure_endpoint"),
        azure_api_version=str(doc.get("azure_api_version") or settings.azure_openai_api_version),
        azure_deployment=doc.get("azure_deployment"),
        updated_at=doc.get("updated_at"),
        updated_by=doc.get("updated_by"),
        source="db",
    )


async def load_persisted_config(settings: Settings | None = None) -> EmbeddingConfig:
    settings = settings or get_settings()
    try:
        doc = await get_control_database()[EMBEDDING_CONFIG_COLLECTION].find_one(
            {"_id": EMBEDDING_CONFIG_ID}
        )
    except Exception as exc:  # control DB unavailable -> fall back to env defaults
        logger.warning("Could not load persisted embedding config: %s", exc)
        doc = None
    if not doc:
        return default_config_from_settings(settings)
    return _config_from_doc(doc, settings)


async def save_persisted_config(
    config: EmbeddingConfig,
    settings: Settings | None = None,
    *,
    updated_by: str | None = None,
) -> None:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    doc = {
        "_id": EMBEDDING_CONFIG_ID,
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "dimensions": int(config.dimensions or 0),
        "api_key_encrypted": encrypt_api_key(config.api_key, settings) or None,
        "azure_endpoint": config.azure_endpoint,
        "azure_api_version": config.azure_api_version,
        "azure_deployment": config.azure_deployment,
        "updated_at": now,
        "updated_by": updated_by,
    }
    await get_control_database()[EMBEDDING_CONFIG_COLLECTION].update_one(
        {"_id": EMBEDDING_CONFIG_ID},
        {"$set": doc},
        upsert=True,
    )


# --------------------------------------------------------------------------- #
# Dimension detection
# --------------------------------------------------------------------------- #
async def resolve_dimensions(
    config: EmbeddingConfig,
    settings: Settings | None = None,
) -> EmbeddingConfig:
    """Return ``config`` with a concrete ``dimensions``, detecting if unknown."""
    if config.dimensions and config.dimensions > 0:
        return config
    settings = settings or get_settings()
    service = build_provider_service(config, settings)
    try:
        dims = await service.detect_dimensions()
    except Exception as exc:
        logger.warning(
            "Embedding dimension detection failed for provider '%s' (%s); "
            "falling back to ollama_dimensions=%d.",
            config.provider,
            exc,
            settings.ollama_dimensions,
        )
        dims = settings.ollama_dimensions
    return replace(config, dimensions=dims)


# --------------------------------------------------------------------------- #
# Process-global active config + proxy
# --------------------------------------------------------------------------- #
_active_config: EmbeddingConfig | None = None
_active_service: BaseHttpEmbeddingService | None = None
_lock = Lock()


def _set_active(config: EmbeddingConfig, service: BaseHttpEmbeddingService) -> None:
    global _active_config, _active_service
    with _lock:
        _active_config = config
        _active_service = service


def _resolve_active_service() -> BaseHttpEmbeddingService:
    global _active_config, _active_service
    if _active_service is not None:
        return _active_service
    with _lock:
        if _active_service is None:
            settings = get_settings()
            config = default_config_from_settings(settings)
            _active_config = config
            _active_service = build_provider_service(config, settings)
        return _active_service


def reset_active_embedding_config() -> None:
    """Drop the cached active config/service (used by tests and after a wipe)."""
    global _active_config, _active_service
    with _lock:
        _active_config = None
        _active_service = None


async def refresh_active_embedding_config(settings: Settings | None = None) -> EmbeddingConfig:
    """Reload the active config from the control DB (env fallback) and rebuild it."""
    settings = settings or get_settings()
    config = await load_persisted_config(settings)
    config = await resolve_dimensions(config, settings)
    service = build_provider_service(config, settings)
    _set_active(config, service)
    return config


def active_embedding_config() -> EmbeddingConfig:
    if _active_config is not None:
        return _active_config
    config = default_config_from_settings(get_settings())
    # Prime the cache so identity reads are stable within a process.
    _resolve_active_service()
    return _active_config or config


def active_embedding_identity() -> tuple[str, int, str]:
    """Return ``(model_id, dimensions, embedding_version)`` for the active provider."""
    service = _resolve_active_service()
    return service.model_id, service.dimensions, embedding_version_for(service)


class ActiveEmbeddingService:
    """Stable proxy that always delegates to the currently active provider."""

    @property
    def model_id(self) -> str:
        return _resolve_active_service().model_id

    @property
    def dimensions(self) -> int:
        return _resolve_active_service().dimensions

    async def embed_text(self, text: str) -> list[float]:
        return await _resolve_active_service().embed_text(text)

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return await _resolve_active_service().embed_texts(texts)

    async def detect_dimensions(self) -> int:
        return await _resolve_active_service().detect_dimensions()


_ACTIVE_PROXY = ActiveEmbeddingService()


def get_active_embedding_service() -> EmbeddingService:
    return _ACTIVE_PROXY


# Re-exported for convenience.
__all__ = [
    "EmbeddingConfig",
    "EMBEDDING_CONFIG_COLLECTION",
    "EMBEDDING_CONFIG_ID",
    "encrypt_api_key",
    "decrypt_api_key",
    "default_config_from_settings",
    "default_model_for",
    "validate_config",
    "load_persisted_config",
    "save_persisted_config",
    "resolve_dimensions",
    "refresh_active_embedding_config",
    "reset_active_embedding_config",
    "active_embedding_config",
    "active_embedding_identity",
    "get_active_embedding_service",
]
