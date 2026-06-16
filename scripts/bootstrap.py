from __future__ import annotations

import asyncio
import logging
import time

from config.settings import get_settings
from database.mongo import connect_to_mongo, disconnect_from_mongo, get_tenant_database
from database.seed import seed_bootstrap_data
from services.embedding_config import refresh_active_embedding_config
from services.embeddings import EmbeddingUnavailableError, get_embedding_service
from services.guardrails import resync_guardrail_signatures
from services.proxy_registry import get_proxy_registry
from services.tenant_provisioner import ensure_control_plane_indexes, provision_tenant

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bootstrap")


async def _wait_for_mongo(timeout_seconds: int = 90) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            await connect_to_mongo(get_settings())
            logger.info("MongoDB is ready.")
            return
        except Exception as exc:
            logger.info("Waiting for MongoDB... (%s)", exc)
            await asyncio.sleep(2)
    raise TimeoutError("MongoDB did not become ready in time.")


async def _sync_catalog() -> None:
    registry = get_proxy_registry()
    default_tenant = get_settings().default_tenant_id
    docs = (
        await get_tenant_database(default_tenant)["routing_registry"]
        .find({"enabled": True})
        .to_list(length=10_000)
    )
    for doc in docs:
        doc["tenant_id"] = default_tenant
        await registry.mount_or_update(doc)
    logger.info("Tool catalog synchronized from persisted routing_registry.")


async def _sync_guardrail_signatures() -> None:
    count = await resync_guardrail_signatures()
    logger.info("Guardrail signature corpus synchronized (%d records).", count)


async def _embedding_preflight() -> None:
    """Fail fast with actionable guidance when embeddings are unreachable."""
    settings = get_settings()
    service = get_embedding_service(settings)
    provider = getattr(service, "provider_name", None) or settings.embedding_provider or "ollama"
    model = getattr(service, "model_id", None) or settings.embedding_model or ""
    probe = settings.embedding_probe_text or "bootstrap preflight"
    try:
        vector = await service.embed_text(probe)
    except EmbeddingUnavailableError as exc:
        message = _preflight_guidance(provider, model, exc)
        logger.error(message)
        raise RuntimeError(message) from exc
    except Exception as exc:
        message = (
            f"Embedding preflight failed for provider '{provider}' model '{model}': {exc}. "
            "Fix provider connectivity and rerun bootstrap."
        )
        logger.error(message)
        raise RuntimeError(message) from exc
    logger.info(
        "Embedding preflight ok (provider=%s model=%s dimensions=%d).",
        provider,
        model,
        len(vector),
    )


def _preflight_guidance(provider: str, model: str, exc: Exception) -> str:
    """Return provider-specific, actionable guidance for an unreachable provider."""
    if provider == "voyage":
        return (
            f"Embedding preflight failed for Voyage AI (model '{model}'): {exc}. "
            "The gateway needs a reachable embedding provider before catalog sync can run. "
            "Confirm VOYAGE_API_KEY (or VOYAGE_API_KEY_FILE) is set and valid, and that the "
            "host can reach https://api.voyageai.com, then re-run: docker compose up --build"
        )
    if provider == "ollama":
        return (
            f"Embedding preflight failed for the local Ollama default (model '{model}'): {exc}. "
            "Start Ollama on your host and pull the model:\n\n"
            "  ollama pull nomic-embed-text\n\n"
            "Or set VOYAGE_API_KEY in .env to use Voyage AI instead, then re-run: "
            "docker compose up --build"
        )
    return (
        f"Embedding preflight failed for provider '{provider}' (model '{model}'): {exc}. "
        "Verify the provider's API key/endpoint and connectivity, then re-run: "
        "docker compose up --build"
    )


async def main() -> None:
    await _wait_for_mongo()
    await refresh_active_embedding_config()
    await _embedding_preflight()
    await ensure_control_plane_indexes()
    await provision_tenant(get_settings().default_tenant_id, wait_for_queryable_indexes=True)
    await seed_bootstrap_data()
    await _sync_catalog()
    await _sync_guardrail_signatures()
    await disconnect_from_mongo()
    logger.info("Bootstrap completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
