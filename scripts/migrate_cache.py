from __future__ import annotations

import argparse
import asyncio
import json

from config.settings import get_settings
from database.mongo import connect_to_mongo, disconnect_from_mongo, get_control_database
from services.cache_migration import SemanticCacheMigrationService
from services.tenant_provisioner import provision_tenant


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Semantic cache migration helper.")
    parser.add_argument(
        "--mode",
        choices=["status", "purge", "reembed"],
        default="status",
        help="Migration mode to execute.",
    )
    parser.add_argument(
        "--tenant-id",
        action="append",
        default=[],
        help="Optional tenant id to target (repeatable). Defaults to all tenants.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=200,
        help="Maximum stale entries to re-embed per tenant in reembed mode.",
    )
    return parser


async def _target_tenants(explicit_tenants: list[str]) -> list[str]:
    if explicit_tenants:
        return sorted(set(explicit_tenants))

    docs = await get_control_database()["tenants"].find({}).to_list(length=10_000)
    tenants = sorted(
        {str(doc.get("tenant_id")) for doc in docs if isinstance(doc.get("tenant_id"), str)}
    )
    if tenants:
        return tenants
    return [get_settings().default_tenant_id]


async def _main() -> None:
    args = _build_parser().parse_args()
    await connect_to_mongo(get_settings())
    try:
        tenant_ids = await _target_tenants(args.tenant_id)
        for tenant_id in tenant_ids:
            await provision_tenant(tenant_id, wait_for_queryable_indexes=False)

        service = SemanticCacheMigrationService()
        result = await service.migrate(
            tenant_ids=tenant_ids,
            mode=args.mode,
            batch_size=max(1, args.batch_size),
        )
        print(json.dumps(result, indent=2, default=str))
    finally:
        await disconnect_from_mongo()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
