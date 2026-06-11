from __future__ import annotations

import pytest

from database.mongo import get_tenant_database, tenant_db_name, tenant_id_from_db_name
from services.authorization import AuthorizationService
from services.hybrid_search import HybridSearchService


@pytest.mark.asyncio
async def test_catalog_list_isolated_per_tenant(patch_mongo, fake_embeddings):
    get_tenant_database("tenant-a")["tool_catalog"].docs.append(
        {
            "server": "orders",
            "name": "find_order",
            "description": "Find order",
            "input_schema": {},
            "scopes": ["orders"],
            "metadata": {},
        }
    )
    get_tenant_database("tenant-b")["tool_catalog"].docs.append(
        {
            "server": "weather",
            "name": "get_forecast",
            "description": "Get forecast",
            "input_schema": {},
            "scopes": ["weather"],
            "metadata": {},
        }
    )

    service = HybridSearchService(embedding_service=fake_embeddings)
    tenant_a_tools = await service.list_tools(tenant_id="tenant-a", limit=10)
    tenant_b_tools = await service.list_tools(tenant_id="tenant-b", limit=10)

    assert [tool["name"] for tool in tenant_a_tools] == ["find_order"]
    assert [tool["name"] for tool in tenant_b_tools] == ["get_forecast"]


@pytest.mark.asyncio
async def test_authorization_isolated_per_tenant(patch_mongo):
    get_tenant_database("tenant-a")["tool_catalog"].docs.append(
        {"server": "orders", "name": "update_order_status", "scopes": ["orders:write"]}
    )
    get_tenant_database("tenant-b")["tool_catalog"].docs.append(
        {"server": "orders", "name": "update_order_status", "scopes": ["readonly"]}
    )

    authz = AuthorizationService()
    tenant_a = await authz.authorize_tool_call(
        tenant_id="tenant-a",
        server="orders",
        name="update_order_status",
        caller_scopes=["orders:write", "server:orders"],
    )
    tenant_b = await authz.authorize_tool_call(
        tenant_id="tenant-b",
        server="orders",
        name="update_order_status",
        caller_scopes=["orders:write", "server:orders"],
    )

    assert tenant_a.allowed is True
    assert tenant_b.allowed is False


def test_tenant_db_name_disambiguates_sanitization_collisions():
    a = tenant_db_name("tenant-a")
    b = tenant_db_name("tenant.a")
    c = tenant_db_name("tenant_a")
    assert len({a, b, c}) == 3


def test_tenant_id_from_db_name_decodes_hashed_names_only():
    hashed_name = tenant_db_name("local-dev")
    # Decoding preserves the sanitized tenant token used in db names.
    assert tenant_id_from_db_name(hashed_name) == "local_dev"
    # Names without the sha256 suffix are not ours and decode to None.
    assert tenant_id_from_db_name("tenant_local_dev") is None
