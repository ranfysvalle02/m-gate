from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Sequence
from threading import Lock
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from config.settings import Settings, get_settings

if TYPE_CHECKING:
    from services.embedding_config import EmbeddingConfig

# Providers the gateway can drive as an embedding backend. Ollama is the
# zero-config local default; the rest are managed cloud providers reached over
# their REST APIs (no vendor SDKs required).
SUPPORTED_PROVIDERS: tuple[str, ...] = ("ollama", "openai", "azure_openai", "voyage", "gemini")

# Per-provider default model used when the operator does not pick one explicitly.
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "ollama": "nomic-embed-text",
    "openai": "text-embedding-3-small",
    "azure_openai": "",  # Azure addresses a *deployment*, supplied separately.
    "voyage": "voyage-3",
    "gemini": "text-embedding-004",
}


class EmbeddingUnavailableError(RuntimeError):
    pass


class EmbeddingService(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed_text(self, text: str) -> list[float]: ...

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def detect_dimensions(self) -> int: ...


def embedding_version_for(service: EmbeddingService) -> str:
    return f"{service.model_id}:{service.dimensions}"


class TtlLruCache:
    """A small thread-safe TTL + LRU cache for embedding vectors.

    Backed by an ``OrderedDict`` so eviction (oldest first) and recency bumps are
    O(1). A ``threading.Lock`` guards every mutation so the cache stays consistent
    even if a single service instance is shared across event loops or threads
    (e.g. a sync worker pool), not just cooperatively-scheduled coroutines.
    """

    def __init__(self, *, max_entries: int, ttl_seconds: float) -> None:
        self._max_entries = max(1, max_entries)
        self._ttl_seconds = max(0.0, ttl_seconds)
        self._items: OrderedDict[str, tuple[list[float], float]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: str) -> list[float] | None:
        with self._lock:
            hit = self._items.get(key)
            if hit is None:
                return None
            value, expires_at = hit
            if time.monotonic() > expires_at:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value

    def set(self, key: str, value: list[float]) -> None:
        expires_at = time.monotonic() + self._ttl_seconds
        with self._lock:
            self._items[key] = (value, expires_at)
            self._items.move_to_end(key)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)


class BaseHttpEmbeddingService:
    """Shared transport for HTTP embedding providers.

    Owns the cross-cutting concerns that should behave identically regardless of
    vendor: a TTL+LRU cache, bounded retries with linear backoff, and a circuit
    breaker that fails fast once a provider is clearly unhealthy. Subclasses only
    implement ``_request_embeddings`` (the provider-specific HTTP call) and expose
    ``model_id`` / ``dimensions``.
    """

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings
        self._cache = TtlLruCache(
            max_entries=settings.embedding_cache_max_entries,
            ttl_seconds=settings.embedding_cache_ttl_seconds,
        )
        self._state_lock = Lock()
        self._circuit_open_until = 0.0
        self._consecutive_failures = 0

    # --- identity (provided by subclasses) -------------------------------------
    @property
    def model_id(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def dimensions(self) -> int:  # pragma: no cover - overridden
        raise NotImplementedError

    # --- public API ------------------------------------------------------------
    async def embed_text(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        vectors = await self.embed_texts([text])
        if not vectors:
            raise ValueError("No embedding returned for input text.")
        vector = vectors[0]
        self._cache.set(text, vector)
        return vector

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        now = time.monotonic()
        with self._state_lock:
            if now < self._circuit_open_until:
                raise EmbeddingUnavailableError("Embedding circuit breaker is open.")

        items = list(texts)
        last_error: Exception | None = None
        for attempt in range(1, self.settings.embedding_retry_attempts + 1):
            try:
                vectors = await self._request_embeddings(items)
                if not vectors:
                    raise ValueError("Embedding provider returned no vectors.")
                with self._state_lock:
                    self._consecutive_failures = 0
                return vectors
            except Exception as exc:
                last_error = exc
                with self._state_lock:
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= self.settings.embedding_circuit_failures:
                        self._circuit_open_until = (
                            time.monotonic() + self.settings.embedding_circuit_reset_seconds
                        )
                if attempt >= self.settings.embedding_retry_attempts:
                    break
                await asyncio.sleep(self.settings.embedding_retry_backoff_seconds * attempt)

        raise EmbeddingUnavailableError(f"Embedding request failed: {last_error}")

    async def detect_dimensions(self) -> int:
        """Measure the provider's native vector width by embedding a probe string.

        This is how the gateway avoids hand-configuring ``dimensions``: ask the
        provider once, count the returned components, and persist that.
        """
        probe = self.settings.embedding_probe_text or "embedding dimension probe"
        vector = await self.embed_text(probe)
        if not vector:
            raise EmbeddingUnavailableError("Probe returned an empty embedding vector.")
        return len(vector)

    # --- provider hook ---------------------------------------------------------
    async def _request_embeddings(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    async def _post_json(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(self.settings.embedding_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=json, headers=headers)
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Embedding provider returned a non-object response.")
        return body

    @staticmethod
    def _data_embeddings(body: dict[str, Any]) -> list[list[float]]:
        """Parse the OpenAI-compatible ``{"data": [{"index", "embedding"}]}`` shape."""
        data = body.get("data")
        if not isinstance(data, list):
            raise ValueError("Provider response did not include a 'data' array.")
        ordered = sorted(
            data, key=lambda item: item.get("index", 0) if isinstance(item, dict) else 0
        )
        vectors: list[list[float]] = []
        for item in ordered:
            embedding = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(embedding, list):
                raise ValueError("Provider response item missing 'embedding'.")
            vectors.append([float(value) for value in embedding])
        return vectors


class OllamaEmbeddingService(BaseHttpEmbeddingService):
    """Local Ollama embeddings via ``POST /api/embed``.

    Unset constructor fields default to the ``OLLAMA_*`` settings, so the provider
    factory can pass the resolved model/base_url/dimensions explicitly while
    direct callers get the configured defaults for free.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        model: str | None = None,
        base_url: str | None = None,
        dimensions: int | None = None,
    ) -> None:
        super().__init__(settings=settings)
        self._model = model or settings.ollama_model
        self._base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self._dimensions = settings.ollama_dimensions if dimensions is None else dimensions

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def _request_embeddings(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {"model": self._model, "input": texts, "truncate": True}
        if self._dimensions:
            payload["dimensions"] = self._dimensions
        body = await self._post_json(f"{self._base_url}/api/embed", json=payload)
        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list):
            raise ValueError("Ollama response did not include 'embeddings'.")
        return [[float(value) for value in row] for row in embeddings]


class OpenAIEmbeddingService(BaseHttpEmbeddingService):
    """OpenAI (and OpenAI-compatible) embeddings via ``POST /v1/embeddings``."""

    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def __init__(
        self,
        *,
        settings: Settings,
        model: str,
        api_key: str,
        base_url: str | None = None,
        dimensions: int = 0,
    ) -> None:
        super().__init__(settings=settings)
        self._model = model
        self._api_key = api_key
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._dimensions = int(dimensions or 0)

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def _request_embeddings(self, texts: list[str]) -> list[list[float]]:
        if not self._api_key:
            raise ValueError("OpenAI API key is required.")
        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload: dict[str, Any] = {"model": self._model, "input": texts}
        body = await self._post_json(f"{self._base_url}/embeddings", json=payload, headers=headers)
        return self._data_embeddings(body)


class AzureOpenAIEmbeddingService(BaseHttpEmbeddingService):
    """Azure OpenAI embeddings against a deployment endpoint."""

    def __init__(
        self,
        *,
        settings: Settings,
        deployment: str,
        api_key: str,
        endpoint: str,
        api_version: str = "2023-05-15",
        dimensions: int = 0,
    ) -> None:
        super().__init__(settings=settings)
        self._deployment = deployment
        self._api_key = api_key
        self._endpoint = (endpoint or "").rstrip("/")
        self._api_version = api_version or "2023-05-15"
        self._dimensions = int(dimensions or 0)

    @property
    def model_id(self) -> str:
        # Azure addresses a deployment; namespace it so the embedding_version is
        # unambiguous across providers that may share an underlying model name.
        return f"azure/{self._deployment}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def _request_embeddings(self, texts: list[str]) -> list[list[float]]:
        if not self._api_key:
            raise ValueError("Azure OpenAI API key is required.")
        if not self._endpoint or not self._deployment:
            raise ValueError("Azure OpenAI requires endpoint and deployment.")
        url = (
            f"{self._endpoint}/openai/deployments/{self._deployment}"
            f"/embeddings?api-version={self._api_version}"
        )
        headers = {"api-key": self._api_key}
        body = await self._post_json(url, json={"input": texts}, headers=headers)
        return self._data_embeddings(body)


class VoyageEmbeddingService(BaseHttpEmbeddingService):
    """Voyage AI embeddings via ``POST /v1/embeddings``."""

    DEFAULT_BASE_URL = "https://api.voyageai.com/v1"

    def __init__(
        self,
        *,
        settings: Settings,
        model: str,
        api_key: str,
        base_url: str | None = None,
        dimensions: int = 0,
    ) -> None:
        super().__init__(settings=settings)
        self._model = model
        self._api_key = api_key
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._dimensions = int(dimensions or 0)

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def _request_embeddings(self, texts: list[str]) -> list[list[float]]:
        if not self._api_key:
            raise ValueError("Voyage AI API key is required.")
        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload: dict[str, Any] = {"model": self._model, "input": texts}
        body = await self._post_json(f"{self._base_url}/embeddings", json=payload, headers=headers)
        return self._data_embeddings(body)


class GeminiEmbeddingService(BaseHttpEmbeddingService):
    """Google Gemini embeddings via the Generative Language ``batchEmbedContents`` API."""

    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        *,
        settings: Settings,
        model: str,
        api_key: str,
        base_url: str | None = None,
        dimensions: int = 0,
    ) -> None:
        super().__init__(settings=settings)
        self._model = model
        self._api_key = api_key
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._dimensions = int(dimensions or 0)

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _model_path(self) -> str:
        return self._model if self._model.startswith("models/") else f"models/{self._model}"

    async def _request_embeddings(self, texts: list[str]) -> list[list[float]]:
        if not self._api_key:
            raise ValueError("Gemini API key is required.")
        model_path = self._model_path()
        # Authenticate via header rather than the ?key= query param so the API key
        # never lands in URLs, httpx error strings, logs, or status documents.
        url = f"{self._base_url}/{model_path}:batchEmbedContents"
        headers = {"x-goog-api-key": self._api_key}
        payload = {
            "requests": [
                {"model": model_path, "content": {"parts": [{"text": text}]}} for text in texts
            ]
        }
        body = await self._post_json(url, json=payload, headers=headers)
        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list):
            raise ValueError("Gemini response did not include 'embeddings'.")
        vectors: list[list[float]] = []
        for item in embeddings:
            values = item.get("values") if isinstance(item, dict) else None
            if not isinstance(values, list):
                raise ValueError("Gemini response item missing 'values'.")
            vectors.append([float(value) for value in values])
        return vectors


def build_provider_service(
    config: EmbeddingConfig,
    settings: Settings | None = None,
) -> BaseHttpEmbeddingService:
    """Construct the concrete provider service for a resolved ``EmbeddingConfig``."""
    settings = settings or get_settings()
    provider = config.provider
    if provider == "ollama":
        return OllamaEmbeddingService(
            settings=settings,
            model=config.model,
            base_url=config.base_url,
            dimensions=config.dimensions or None,
        )
    if provider == "openai":
        return OpenAIEmbeddingService(
            settings=settings,
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            dimensions=config.dimensions,
        )
    if provider == "azure_openai":
        return AzureOpenAIEmbeddingService(
            settings=settings,
            deployment=config.azure_deployment or config.model,
            api_key=config.api_key,
            endpoint=config.azure_endpoint or "",
            api_version=config.azure_api_version,
            dimensions=config.dimensions,
        )
    if provider == "voyage":
        return VoyageEmbeddingService(
            settings=settings,
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            dimensions=config.dimensions,
        )
    if provider == "gemini":
        return GeminiEmbeddingService(
            settings=settings,
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            dimensions=config.dimensions,
        )
    raise ValueError(f"Unsupported embedding provider '{provider}'.")


def get_embedding_service(settings: Settings | None = None) -> EmbeddingService:
    """Return the process-wide active embedding service.

    Returns a stable proxy that always reflects the currently active provider
    configuration, so module-level singletons that captured it keep working after
    a runtime config change. Imported lazily to avoid an import cycle with the
    config layer.
    """
    from services.embedding_config import get_active_embedding_service

    return get_active_embedding_service()
