import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import (  # noqa: E402
    FakeDatabase,
    FakeEmbeddingService,
    FakeMongoClient,
)


@pytest.fixture
def reset_settings():
    """Clear the cached Settings singleton before and after a test.

    Tests that mutate environment variables must use this so get_settings()
    re-reads the environment instead of returning a stale lru_cache hit.
    """
    from config.settings import get_settings
    from services.embedding_config import reset_active_embedding_config

    get_settings.cache_clear()
    reset_active_embedding_config()
    yield
    get_settings.cache_clear()
    reset_active_embedding_config()


@pytest.fixture
def fake_db():
    """A bare in-memory database with no search handlers."""
    return FakeDatabase()


@pytest.fixture
def patch_mongo(monkeypatch, fake_db):
    """Patch database.mongo so every get_database()/get_client() hits the fake.

    Patches at the module-export level and at each import site that bound the
    name directly, so middleware and services all observe the same fake.
    """
    from config.settings import get_settings

    settings = get_settings()
    control_db_name = settings.mongodb_db_name

    import database.mongo as mongo_module

    default_tenant_db_name = mongo_module.tenant_db_name(settings.default_tenant_id)
    client = FakeMongoClient(fake_db, default_db_name=default_tenant_db_name)
    control_db = client[control_db_name]

    def _get_database(name=None):
        db_name = name or control_db_name
        return client[db_name]

    def _get_control_database():
        return client[control_db_name]

    def _get_tenant_database(tenant_id: str):
        return client[mongo_module.tenant_db_name(tenant_id)]

    monkeypatch.setattr(mongo_module, "_client", client, raising=False)
    monkeypatch.setattr(mongo_module, "get_client", lambda: client)
    monkeypatch.setattr(mongo_module, "get_database", _get_database)
    monkeypatch.setattr(mongo_module, "get_control_database", _get_control_database)
    monkeypatch.setattr(mongo_module, "get_tenant_database", _get_tenant_database)

    # The provisioning "ready" cache is process-global; clear it so each test
    # starts from a clean control plane backed by the fresh fake database.
    import services.tenant_provisioner as tenant_provisioner

    tenant_provisioner.reset_ready_tenant_cache()

    # The tenant suspended/active status cache is process-global; clear it so a
    # suspension set in one test never leaks into the next.
    import services.tenant_status as tenant_status

    tenant_status.reset_tenant_status_cache()

    # The per-tenant egress allowlist cache is process-global; clear it too.
    import services.tenant_egress as tenant_egress

    tenant_egress.reset_tenant_egress_cache()

    # The active embedding config/service is process-global; reset it so tests that
    # exercise provisioning/identity start from the env defaults backed by the fake.
    import services.embedding_config as embedding_config

    embedding_config.reset_active_embedding_config()

    # Rebind the name in modules that imported get_database directly.
    for mod_name in [
        "services.authorization",
        "services.cache_manager",
        "services.cache_migration",
        "services.proxy_registry",
        "services.server_exporter",
        "services.hybrid_search",
        "services.telemetry_logger",
        "services.registry_watcher",
        "services.tenant_provisioner",
        "services.embedding_config",
        "services.embedding_reprovision",
        "services.guardrails",
        "services.sandbox_db_bridge",
        "services.users",
        "services.pending_actions",
        "services.usage_metering",
        "services.tenant_status",
        "services.tenant_egress",
        "gateway.middleware.rbac",
        "gateway.middleware.ratelimit",
        "gateway.routers.health",
        "gateway.routers.admin",
    ]:
        module = sys.modules.get(mod_name)
        if module:
            monkeypatch.setattr(module, "get_database", _get_database, raising=False)
            monkeypatch.setattr(
                module, "get_control_database", _get_control_database, raising=False
            )
            monkeypatch.setattr(module, "get_tenant_database", _get_tenant_database, raising=False)

    # Preserve existing test ergonomics: patch_mongo["collection"] points at the
    # default tenant DB, while control DB remains available through patched helpers.
    default_tenant_db = client[default_tenant_db_name]
    default_tenant_db._control_db = control_db  # type: ignore[attr-defined]
    default_tenant_db._client = client  # type: ignore[attr-defined]
    return default_tenant_db


@pytest.fixture
def fake_embeddings():
    return FakeEmbeddingService()
