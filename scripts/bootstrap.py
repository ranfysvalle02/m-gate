from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from config.settings import get_settings
from database.mongo import connect_to_mongo, disconnect_from_mongo, get_control_database
from database.seed import guardrail_signatures_seed, routing_registry_seed, seed_bootstrap_data
from services.embeddings import embedding_version_for, get_embedding_service
from services.guardrails import guardrail_signature_lookup_filter
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
    for doc in routing_registry_seed(default_tenant):
        await registry.mount_or_update(doc)
    logger.info("Tool catalog synchronized from registry seed.")


async def _sync_guardrail_signatures() -> None:
    settings = get_settings()
    embedding_service = get_embedding_service(settings)
    embedding_version = embedding_version_for(embedding_service)
    collection = get_control_database()["guardrail_signatures"]
    for signature in guardrail_signatures_seed():
        text = str(signature["text"])
        embedding = await embedding_service.embed_text(text)
        now = datetime.now(UTC)
        await collection.update_one(
            {"_id": signature["_id"]},
            {
                "$set": {
                    **signature,
                    "embedding": embedding,
                    "embedding_version": embedding_version,
                    **guardrail_signature_lookup_filter(embedding_version=embedding_version),
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    logger.info(
        "Guardrail signature corpus synchronized (%d records).", len(guardrail_signatures_seed())
    )


async def main() -> None:
    await _wait_for_mongo()
    await ensure_control_plane_indexes()
    await provision_tenant(get_settings().default_tenant_id, wait_for_queryable_indexes=True)
    await seed_bootstrap_data()
    await _sync_catalog()
    await _sync_guardrail_signatures()
    await disconnect_from_mongo()
    logger.info("Bootstrap completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
