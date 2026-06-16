"""Tests for the embedding services: HTTP behavior, caching (TTL + LRU), retry,
circuit breaker, runtime dimension detection, and every supported provider's
request/response shape. HTTP is mocked with respx so no provider is needed.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from config.settings import Settings
from services.embedding_config import EmbeddingConfig
from services.embeddings import (
    AzureOpenAIEmbeddingService,
    EmbeddingUnavailableError,
    GeminiEmbeddingService,
    OllamaEmbeddingService,
    OpenAIEmbeddingService,
    VoyageEmbeddingService,
    build_provider_service,
)

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
    # _env_file=None keeps these unit settings hermetic: a developer's local .env
    # (e.g. VOYAGE_API_KEY) must never leak into provider construction here.
    return Settings(_env_file=None, **base)


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


@pytest.mark.asyncio
@respx.mock
async def test_detect_dimensions_measures_probe_vector():
    respx.post(EMBED_URL).mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3, 0.4, 0.5]]})
    )
    svc = OllamaEmbeddingService(settings=_settings(ollama_dimensions=0))
    assert await svc.detect_dimensions() == 5


@pytest.mark.asyncio
@respx.mock
async def test_openai_provider_parses_data_and_sends_bearer():
    captured: dict = {}

    def _responder(request):
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.4, 0.5]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            },
        )

    route = respx.post("https://api.openai.com/v1/embeddings").mock(side_effect=_responder)
    svc = OpenAIEmbeddingService(
        settings=_settings(), model="text-embedding-3-small", api_key="sk-test"
    )
    vectors = await svc.embed_texts(["a", "b"])
    # Results are reordered by the provider's `index` field.
    assert vectors == [[0.1, 0.2], [0.4, 0.5]]
    assert captured["auth"] == "Bearer sk-test"
    assert route.called


@pytest.mark.asyncio
async def test_openai_missing_key_raises_unavailable():
    svc = OpenAIEmbeddingService(settings=_settings(), model="text-embedding-3-small", api_key="")
    with pytest.raises(EmbeddingUnavailableError):
        await svc.embed_text("x")


@pytest.mark.asyncio
@respx.mock
async def test_azure_openai_uses_deployment_url_and_api_key_header():
    captured: dict = {}

    def _responder(request):
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("api-key")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}]})

    respx.post("https://my-res.openai.azure.com/openai/deployments/embed-dep/embeddings").mock(
        side_effect=_responder
    )
    svc = AzureOpenAIEmbeddingService(
        settings=_settings(),
        deployment="embed-dep",
        api_key="azure-key",
        endpoint="https://my-res.openai.azure.com",
        api_version="2023-05-15",
    )
    vector = await svc.embed_text("hello")
    assert vector == [1.0, 2.0, 3.0]
    assert "api-version=2023-05-15" in captured["url"]
    assert captured["api_key"] == "azure-key"
    assert svc.model_id == "azure/embed-dep"


@pytest.mark.asyncio
@respx.mock
async def test_voyage_provider_parses_data():
    respx.post("https://api.voyageai.com/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.7, 0.8]}]})
    )
    svc = VoyageEmbeddingService(settings=_settings(), model="voyage-3", api_key="pa-test")
    assert await svc.embed_text("hi") == [0.7, 0.8]


@pytest.mark.asyncio
@respx.mock
async def test_gemini_provider_parses_values_and_uses_header_auth():
    captured: dict = {}

    def _responder(request):
        captured["url"] = str(request.url)
        captured["api_key_header"] = request.headers.get("x-goog-api-key")
        return httpx.Response(200, json={"embeddings": [{"values": [0.1, 0.2, 0.3]}]})

    respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:batchEmbedContents"
    ).mock(side_effect=_responder)
    svc = GeminiEmbeddingService(settings=_settings(), model="text-embedding-004", api_key="g-key")
    assert await svc.embed_text("hello") == [0.1, 0.2, 0.3]
    # The key must never appear in the URL (it would leak into error strings/logs).
    assert "g-key" not in captured["url"]
    assert captured["api_key_header"] == "g-key"


def test_build_provider_service_selects_correct_class():
    settings = _settings()
    cases = {
        "ollama": OllamaEmbeddingService,
        "openai": OpenAIEmbeddingService,
        "azure_openai": AzureOpenAIEmbeddingService,
        "voyage": VoyageEmbeddingService,
        "gemini": GeminiEmbeddingService,
    }
    for provider, cls in cases.items():
        config = EmbeddingConfig(
            provider=provider,
            model="m",
            api_key="k",
            azure_endpoint="https://x.openai.azure.com",
            azure_deployment="dep",
            dimensions=4,
        )
        assert isinstance(build_provider_service(config, settings), cls)


def test_build_provider_service_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unsupported embedding provider"):
        build_provider_service(EmbeddingConfig(provider="bogus", model="m"), _settings())
