from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol

import httpx

from config.settings import Settings, get_settings


class EmbeddingUnavailableError(RuntimeError):
    pass


class EmbeddingService(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed_text(self, text: str) -> list[float]: ...

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...


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


@dataclass
class OllamaEmbeddingService:
    settings: Settings

    def __post_init__(self) -> None:
        self._cache = TtlLruCache(
            max_entries=self.settings.embedding_cache_max_entries,
            ttl_seconds=self.settings.embedding_cache_ttl_seconds,
        )
        self._state_lock = Lock()
        self._circuit_open_until = 0.0
        self._consecutive_failures = 0

    @property
    def model_id(self) -> str:
        return self.settings.ollama_model

    @property
    def dimensions(self) -> int:
        return self.settings.ollama_dimensions

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

        payload: dict[str, object] = {
            "model": self.settings.ollama_model,
            "input": list(texts),
            "truncate": True,
        }
        if self.settings.ollama_dimensions:
            payload["dimensions"] = self.settings.ollama_dimensions

        timeout = httpx.Timeout(self.settings.embedding_timeout_seconds)
        last_error: Exception | None = None
        for attempt in range(1, self.settings.embedding_retry_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        f"{self.settings.ollama_base_url}/api/embed",
                        json=payload,
                    )
                    response.raise_for_status()
                    body: dict[str, Any] = response.json()
                with self._state_lock:
                    self._consecutive_failures = 0
                embeddings = body.get("embeddings")
                if embeddings is None:
                    raise ValueError("Ollama response did not include 'embeddings'.")
                return embeddings
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


def get_embedding_service(settings: Settings | None = None) -> EmbeddingService:
    return OllamaEmbeddingService(settings=settings or get_settings())
