"""Tests for the Ollama embedding service: HTTP behavior, caching (TTL + LRU),
retry, and circuit breaker. HTTP is mocked with respx so no Ollama is needed.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from config.settings import Settings
from services.embeddings import EmbeddingUnavailableError, OllamaEmbeddingService

EMBED_URL = "http://ollama-test:11434/api/embed"


def _settings(**overrides) -> Settings:
    base = {
        "ollama_base_url": "http://ollama-test:11434",
        "ollama_model": "nomic-embed-text",
        "ollama_dimensions": 3,
        "embedding_timeout_seconds": 1.0,
        "embedding_retry_attempts": 2,
        "embedding_retry_backoff_seconds": 0.0,
        "embedding_circuit_failures": 3,
        "embedding_circuit_reset_seconds": 30,
        "embedding_cache_ttl_seconds": 300,
        "embedding_cache_max_entries": 2,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.mark.asyncio
@respx.mock
async def test_embed_text_returns_vector_and_caches():
    route = respx.post(EMBED_URL).mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})
    )
    svc = OllamaEmbeddingService(settings=_settings())

    first = await svc.embed_text("hello")
    second = await svc.embed_text("hello")

    assert first == [0.1, 0.2, 0.3]
    assert second == first
    # Second call served from cache -> only one HTTP request.
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_missing_embeddings_key_raises_unavailable():
    respx.post(EMBED_URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
    svc = OllamaEmbeddingService(settings=_settings())
    with pytest.raises(EmbeddingUnavailableError):
        await svc.embed_text("x")


@pytest.mark.asyncio
@respx.mock
async def test_retries_then_succeeds():
    responses = [
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(200, json={"embeddings": [[1.0, 1.0, 1.0]]}),
    ]
    respx.post(EMBED_URL).mock(side_effect=responses)
    svc = OllamaEmbeddingService(settings=_settings(embedding_retry_attempts=2))
    result = await svc.embed_text("retry")
    assert result == [1.0, 1.0, 1.0]


@pytest.mark.asyncio
@respx.mock
async def test_circuit_breaker_opens_after_consecutive_failures():
    respx.post(EMBED_URL).mock(return_value=httpx.Response(500, json={"error": "down"}))
    # 1 attempt per call so each call counts as one failure toward the breaker.
    svc = OllamaEmbeddingService(
        settings=_settings(embedding_retry_attempts=1, embedding_circuit_failures=3)
    )
    for _ in range(3):
        with pytest.raises(EmbeddingUnavailableError):
            await svc.embed_text("a")
    # Breaker now open: next call fails fast with the breaker message.
    with pytest.raises(EmbeddingUnavailableError, match="circuit breaker"):
        await svc.embed_text("b")


@pytest.mark.asyncio
@respx.mock
async def test_lru_eviction_respects_max_entries():
    respx.post(EMBED_URL).mock(
        side_effect=lambda req: httpx.Response(200, json={"embeddings": [[0.0, 0.0, 0.0]]})
    )
    svc = OllamaEmbeddingService(settings=_settings(embedding_cache_max_entries=2))
    await svc.embed_text("one")
    await svc.embed_text("two")
    await svc.embed_text("three")  # evicts "one"
    assert svc._cache.get("one") is None
    assert svc._cache.get("two") is not None
    assert svc._cache.get("three") is not None


def test_embedding_service_reports_identity():
    svc = OllamaEmbeddingService(settings=_settings(ollama_model="foo", ollama_dimensions=42))
    assert svc.model_id == "foo"
    assert svc.dimensions == 42
