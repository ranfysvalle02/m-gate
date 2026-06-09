from __future__ import annotations

import asyncio
import logging
import time

from config.settings import get_settings
from database.mongo import connect_to_mongo, disconnect_from_mongo
from database.seed import routing_registry_seed, seed_bootstrap_data
from services.embedding_config import refresh_active_embedding_config
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
    for doc in routing_registry_seed(default_tenant):
        await registry.mount_or_update(doc)
    logger.info("Tool catalog synchronized from registry seed.")


async def _sync_guardrail_signatures() -> None:
    count = await resync_guardrail_signatures()
    logger.info("Guardrail signature corpus synchronized (%d records).", count)


async def main() -> None:
    await _wait_for_mongo()
    await refresh_active_embedding_config()
    await ensure_control_plane_indexes()
    await provision_tenant(get_settings().default_tenant_id, wait_for_queryable_indexes=True)
    await seed_bootstrap_data()
    await _sync_catalog()
    await _sync_guardrail_signatures()
    await disconnect_from_mongo()
    logger.info("Bootstrap completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
