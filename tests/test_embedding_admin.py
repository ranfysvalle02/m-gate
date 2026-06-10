"""Tests for the embedding admin endpoints: platform-admin gating, config GET/PUT,
dry-run test, and reprovision status. Mongo + embeddings are faked.
"""

from __future__ import annotations

import pytest
from fakes import FakeEmbeddingService
from fastapi import HTTPException

from models.admin import (
    EmbeddingConfigUpdateRequest,
    EmbeddingTestRequest,
)


class _State:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Req:
    def __init__(self, *, roles=None, user_id="demo@demo.com"):
        self.state = _State(tenant_id="local-dev", roles=roles or [], user_id=user_id)
        self.headers = {}


def _platform_admin(admin) -> _Req:
    return _Req(roles=[admin.settings.platform_admin_role, "admin"])


def _stub_provider_build(monkeypatch, admin, *, dims: int):
    """Make provider construction return a deterministic fake so dimension
    detection never touches the network. The fake reports the configured model
    so embedding_version stays meaningful.
    """

    def _build(config, settings=None):
        model_id = config.model or config.azure_deployment or "fake-model"
        return FakeEmbeddingService(dimensions=dims, model_id=model_id)

    monkeypatch.setattr(admin, "build_provider_service", _build)


@pytest.mark.asyncio
async def test_get_embedding_requires_platform_admin(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.get_embedding_config(_Req(roles=["admin"]))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_embedding_returns_env_default(patch_mongo):
    import gateway.routers.admin as admin

    response = await admin.get_embedding_config(_platform_admin(admin))
    assert response.provider == "ollama"
    assert response.dimensions == admin.settings.ollama_dimensions
    assert "openai" in response.supported_providers
    assert response.api_key_set is False
    assert response.reprovision.get("state") == "idle"


@pytest.mark.asyncio
async def test_put_embedding_detects_and_persists(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    _stub_provider_build(monkeypatch, admin, dims=512)
    payload = EmbeddingConfigUpdateRequest(
        provider="ollama",
        model="nomic-embed-text",
        reprovision=False,
    )
    response = await admin.update_embedding_config(_platform_admin(admin), payload)
    assert response.provider == "ollama"
    # Width is whatever the provider actually returned, never a hand-set value.
    assert response.dimensions == 512
    assert response.reprovision == {}

    # Persisted and reloadable.
    reloaded = await admin.get_embedding_config(_platform_admin(admin))
    assert reloaded.dimensions == 512
    assert reloaded.source == "db"


@pytest.mark.asyncio
async def test_put_embedding_triggers_reprovision(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    _stub_provider_build(monkeypatch, admin, dims=256)
    started: dict = {}

    async def fake_trigger(*, started_by=None):
        started["by"] = started_by
        return {"state": "running", "started_by": started_by}

    monkeypatch.setattr(admin, "trigger_reprovision", fake_trigger)

    payload = EmbeddingConfigUpdateRequest(
        provider="ollama",
        model="nomic-embed-text",
        reprovision=True,
    )
    response = await admin.update_embedding_config(_platform_admin(admin), payload)
    assert response.reprovision["state"] == "running"
    assert started["by"] == "demo@demo.com"


@pytest.mark.asyncio
async def test_put_embedding_rejects_unreachable_provider(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    def _build(config, settings=None):
        return FakeEmbeddingService(dimensions=8, fail=True, model_id=config.model)

    monkeypatch.setattr(admin, "build_provider_service", _build)
    payload = EmbeddingConfigUpdateRequest(
        provider="openai",
        model="text-embedding-3-small",
        api_key="sk-bad",
        reprovision=False,
    )
    with pytest.raises(HTTPException) as exc:
        await admin.update_embedding_config(_platform_admin(admin), payload)
    assert exc.value.status_code == 422
    assert "validation failed" in exc.value.detail


@pytest.mark.asyncio
async def test_put_embedding_stores_api_key_masked(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    _stub_provider_build(monkeypatch, admin, dims=1024)
    payload = EmbeddingConfigUpdateRequest(
        provider="voyage",
        model="voyage-3",
        api_key="pa-secret-abcd",
        reprovision=False,
    )
    response = await admin.update_embedding_config(_platform_admin(admin), payload)
    assert response.provider == "voyage"
    assert response.api_key_set is True
    assert response.api_key_hint is not None
    assert "pa-secret" not in (response.api_key_hint or "")


@pytest.mark.asyncio
async def test_test_endpoint_reports_failure_for_missing_key(patch_mongo):
    import gateway.routers.admin as admin

    payload = EmbeddingTestRequest(provider="openai", model="text-embedding-3-small", api_key="")
    response = await admin.test_embedding_config(_platform_admin(admin), payload)
    assert response.ok is False
    assert "API key" in response.message


@pytest.mark.asyncio
async def test_test_endpoint_ok_detects_dimensions(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    _stub_provider_build(monkeypatch, admin, dims=4)
    payload = EmbeddingTestRequest(provider="ollama", model="nomic-embed-text")
    response = await admin.test_embedding_config(_platform_admin(admin), payload)
    assert response.ok is True
    assert response.dimensions == 4
    assert response.embedding_version == "nomic-embed-text:4"


@pytest.mark.asyncio
async def test_status_endpoint_requires_platform_admin(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.get_embedding_status(_Req(roles=["admin"]))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_tenant_embedding_allows_same_tenant_without_platform_admin(patch_mongo):
    import gateway.routers.admin as admin

    response = await admin.get_tenant_embedding_config(_Req(roles=[]), "local-dev")
    assert response.provider == "ollama"
    assert response.source in {"platform-default", "env", "db"}


@pytest.mark.asyncio
async def test_get_tenant_embedding_rejects_cross_tenant_without_platform_admin(patch_mongo):
    import gateway.routers.admin as admin

    with pytest.raises(HTTPException) as exc:
        await admin.get_tenant_embedding_config(_Req(roles=[]), "other-tenant")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_put_tenant_embedding_detects_and_triggers_tenant_reprovision(
    patch_mongo, monkeypatch
):
    import gateway.routers.admin as admin

    _stub_provider_build(monkeypatch, admin, dims=384)
    started: dict = {}

    async def fake_trigger(*, tenant_id: str, started_by=None):
        started["tenant"] = tenant_id
        started["by"] = started_by
        return {"state": "running", "tenant_id": tenant_id, "started_by": started_by}

    monkeypatch.setattr(admin, "trigger_tenant_reprovision", fake_trigger)

    payload = EmbeddingConfigUpdateRequest(
        provider="ollama",
        model="nomic-embed-text",
        reprovision=True,
    )
    response = await admin.update_tenant_embedding_config(_Req(roles=[]), "local-dev", payload)
    assert response.provider == "ollama"
    assert response.dimensions == 384
    assert response.reprovision["state"] == "running"
    assert started["tenant"] == "local-dev"
    assert started["by"] == "demo@demo.com"


@pytest.mark.asyncio
async def test_delete_tenant_embedding_reverts_to_platform_default(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    _stub_provider_build(monkeypatch, admin, dims=384)

    # Create a tenant override first.
    create = EmbeddingConfigUpdateRequest(
        provider="openai",
        model="text-embedding-3-small",
        api_key="sk-tenant-key",
        reprovision=False,
    )
    created = await admin.update_tenant_embedding_config(_Req(roles=[]), "local-dev", create)
    assert created.source == "tenant-db"
    assert created.api_key_set is True

    # Resetting removes the override; the tenant inherits the platform default.
    reset = await admin.reset_tenant_embedding_config(
        _Req(roles=[]), "local-dev", reprovision=False
    )
    assert reset.source in {"platform-default", "env", "db"}
    assert reset.source != "tenant-db"
    assert reset.reprovision == {}

    # And a subsequent GET confirms the override is gone.
    fetched = await admin.get_tenant_embedding_config(_Req(roles=[]), "local-dev")
    assert fetched.source != "tenant-db"


@pytest.mark.asyncio
async def test_delete_tenant_embedding_triggers_reprovision_when_requested(
    patch_mongo, monkeypatch
):
    import gateway.routers.admin as admin

    _stub_provider_build(monkeypatch, admin, dims=384)
    started: dict = {}

    async def fake_trigger(*, tenant_id: str, started_by=None):
        started["tenant"] = tenant_id
        return {"state": "running", "tenant_id": tenant_id}

    monkeypatch.setattr(admin, "trigger_tenant_reprovision", fake_trigger)

    # Need an existing override so the delete reports deleted=True and reprovisions.
    create = EmbeddingConfigUpdateRequest(
        provider="ollama", model="nomic-embed-text", reprovision=False
    )
    await admin.update_tenant_embedding_config(_Req(roles=[]), "local-dev", create)

    reset = await admin.reset_tenant_embedding_config(_Req(roles=[]), "local-dev", reprovision=True)
    assert reset.reprovision["state"] == "running"
    assert started["tenant"] == "local-dev"


@pytest.mark.asyncio
async def test_delete_tenant_embedding_noop_skips_reprovision(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    _stub_provider_build(monkeypatch, admin, dims=8)

    async def fail_trigger(*, tenant_id: str, started_by=None):  # pragma: no cover - must not run
        raise AssertionError("reprovision must not run when there was no override to delete")

    monkeypatch.setattr(admin, "trigger_tenant_reprovision", fail_trigger)

    # No override exists, so reprovision is skipped even when requested.
    reset = await admin.reset_tenant_embedding_config(_Req(roles=[]), "local-dev", reprovision=True)
    assert reset.reprovision == {}
    assert reset.source != "tenant-db"


@pytest.mark.asyncio
async def test_tenant_status_endpoint_returns_tenant_status(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    async def fake_status(tenant_id: str):
        return {"state": "completed", "tenant_id": tenant_id}

    monkeypatch.setattr(admin, "get_tenant_reprovision_status", fake_status)
    status = await admin.get_tenant_embedding_status(_Req(roles=[]), "local-dev")
    assert status["state"] == "completed"
    assert status["tenant_id"] == "local-dev"


def test_secret_encryption_label_variants():
    import gateway.routers.admin as admin
    from services.embedding_config import EmbeddingConfig

    assert admin._secret_encryption_label(EmbeddingConfig(provider="ollama", model="m")) is None

    global_key = EmbeddingConfig(provider="openai", model="m", api_key="k", source="db")
    assert admin._secret_encryption_label(global_key) == "shared-fernet"

    original = admin.settings.qe_enabled
    try:
        object.__setattr__(admin.settings, "qe_enabled", False)
        tenant_off = EmbeddingConfig(provider="openai", model="m", api_key="k", source="tenant-db")
        assert admin._secret_encryption_label(tenant_off) == "shared-fernet"

        object.__setattr__(admin.settings, "qe_enabled", True)
        tenant_on = EmbeddingConfig(provider="openai", model="m", api_key="k", source="tenant-db")
        assert admin._secret_encryption_label(tenant_on) == "per-tenant-dek"
    finally:
        object.__setattr__(admin.settings, "qe_enabled", original)


@pytest.mark.asyncio
async def test_tenant_embedding_response_reports_secret_encryption(patch_mongo, monkeypatch):
    import gateway.routers.admin as admin

    _stub_provider_build(monkeypatch, admin, dims=8)
    payload = EmbeddingConfigUpdateRequest(
        provider="openai",
        model="text-embedding-3-small",
        api_key="sk-tenant-key",
        reprovision=False,
    )
    response = await admin.update_tenant_embedding_config(_Req(roles=[]), "local-dev", payload)
    assert response.api_key_set is True
    assert response.source == "tenant-db"
    # QE is disabled in the unit suite, so the seam falls back to the shared key.
    assert response.secret_encryption == "shared-fernet"

    fetched = await admin.get_tenant_embedding_config(_Req(roles=[]), "local-dev")
    assert fetched.secret_encryption == "shared-fernet"
