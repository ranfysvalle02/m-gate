"""Full authorization decision matrix for AuthorizationService."""

from __future__ import annotations

import pytest

from services.authorization import AuthorizationService


@pytest.fixture
def service(patch_mongo):
    return AuthorizationService()


def _seed(patch_mongo, **doc):
    doc.setdefault("server", "orders")
    doc.setdefault("name", "tool")
    patch_mongo["tool_catalog"].docs.append(doc)


@pytest.mark.asyncio
async def test_unknown_tool_is_denied(service):
    result = await service.authorize_tool_call(
        server="orders", name="nope", caller_scopes=["orders:read"]
    )
    assert result.allowed is False
    assert result.reason == "tool_not_found"


@pytest.mark.asyncio
async def test_admin_override_allows_anything(service, patch_mongo):
    _seed(patch_mongo, name="t", scopes=["orders:write"])
    result = await service.authorize_tool_call(
        server="orders", name="t", caller_scopes=[], caller_roles=["admin"]
    )
    assert result.allowed is True
    assert result.reason == "admin_override"


@pytest.mark.asyncio
async def test_tool_with_no_scopes_is_open(service, patch_mongo):
    _seed(patch_mongo, name="t", scopes=[])
    result = await service.authorize_tool_call(server="orders", name="t", caller_scopes=None)
    assert result.allowed is True
    assert result.reason == "no_scope_required"


@pytest.mark.asyncio
async def test_missing_caller_scope_is_denied(service, patch_mongo):
    _seed(patch_mongo, name="t", scopes=["orders:write"])
    result = await service.authorize_tool_call(server="orders", name="t", caller_scopes=None)
    assert result.allowed is False
    assert result.reason == "missing_scope"


@pytest.mark.asyncio
async def test_scope_intersection_allows(service, patch_mongo):
    _seed(patch_mongo, name="t", scopes=["orders:write", "orders:admin"])
    result = await service.authorize_tool_call(
        server="orders", name="t", caller_scopes=["orders:write"]
    )
    assert result.allowed is True
    assert result.reason == "scope_match"


@pytest.mark.asyncio
async def test_scope_disjoint_denies(service, patch_mongo):
    _seed(patch_mongo, name="t", scopes=["orders:write"])
    result = await service.authorize_tool_call(
        server="orders", name="t", caller_scopes=["orders:read"]
    )
    assert result.allowed is False
    assert result.reason == "scope_mismatch"
