"""Tests for the local-only demo user seeded by ``database.seed._seed_demo_user``.

The demo account exists so the admin console's "Generate token" flow has an
immediate target out of the box. It must be idempotent and must never be seeded
when running in production.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from database.seed import _DEMO_USER_EMAIL, _DEMO_USER_PASSWORD, _seed_demo_user
from services import users as users_service


@pytest.mark.asyncio
async def test_seeds_demo_user_with_tool_invoke(reset_settings, patch_mongo):
    await _seed_demo_user("local-dev")

    doc = await users_service.find_user_by_email(_DEMO_USER_EMAIL)
    assert doc is not None
    assert doc["tenant_id"] == "local-dev"
    # The Demo role must carry tool:invoke so a minted token clears the /rpc + /mcp gate.
    assert set(doc["roles"]) == {"user", "tool:invoke"}
    assert doc["scopes"]  # demo scopes present
    assert doc["status"] == "active"

    # The seeded password actually authenticates (so /auth/token works too).
    principal = await users_service.authenticate(_DEMO_USER_EMAIL, _DEMO_USER_PASSWORD)
    assert principal is not None


@pytest.mark.asyncio
async def test_seed_demo_user_is_idempotent(reset_settings, patch_mongo):
    await _seed_demo_user("local-dev")
    # A second run must not raise (UserAlreadyExists is swallowed) and must not
    # create a duplicate.
    await _seed_demo_user("local-dev")

    users = await users_service.list_users(tenant_id="local-dev")
    matching = [u for u in users if u["email"] == _DEMO_USER_EMAIL]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_seed_demo_user_skipped_in_production(reset_settings, patch_mongo, monkeypatch):
    # Avoid tripping the production settings validator by stubbing get_settings
    # in the seed module to report a production environment.
    import database.seed as seed_module

    monkeypatch.setattr(
        seed_module,
        "get_settings",
        lambda: SimpleNamespace(environment="production"),
    )

    await _seed_demo_user("local-dev")

    assert await users_service.find_user_by_email(_DEMO_USER_EMAIL) is None
