from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

import pytest
from pymongo import MongoClient

from config.settings import get_settings
from database.encryption import delete_tenant_data_key
from database.mongo import connect_to_mongo, disconnect_from_mongo, tenant_db_name
from services.embedding_config import EmbeddingConfig, load_tenant_config, save_tenant_config
from services.tenant_provisioner import ensure_control_plane_indexes, provision_tenant

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def qe_settings(atlas_uri, monkeypatch):
    pytest.importorskip("pymongocrypt")
    crypt_shared = Path(
        os.environ.get("CRYPT_SHARED_LIB_PATH", "/opt/mongodb/lib/mongo_crypt_v1.so")
    )
    if not crypt_shared.exists():
        pytest.skip(
            f"Queryable Encryption integration test requires crypt_shared at {crypt_shared}.",
            allow_module_level=True,
        )

    db_name = f"itest_tenant_secret_{uuid.uuid4().hex[:8]}"
    local_master_key = base64.b64encode(os.urandom(96)).decode("utf-8")

    monkeypatch.setenv("MONGODB_URI", atlas_uri)
    monkeypatch.setenv("MONGODB_DB_NAME", db_name)
    monkeypatch.setenv("QE_ENABLED", "true")
    monkeypatch.setenv("KMS_PROVIDER", "local")
    monkeypatch.setenv("QE_LOCAL_MASTER_KEY", local_master_key)
    monkeypatch.setenv("CRYPT_SHARED_LIB_PATH", str(crypt_shared))

    get_settings.cache_clear()
    settings = get_settings()
    try:
        yield settings
    finally:
        get_settings.cache_clear()
        client = MongoClient(atlas_uri, serverSelectionTimeoutMS=3000, directConnection=True)
        try:
            client.drop_database(db_name)
        finally:
            client.close()


async def test_tenant_embedding_secret_round_trip_and_crypto_shred(qe_settings):
    tenant_id = "qe-tenant-secret"
    await connect_to_mongo(qe_settings)
    try:
        await ensure_control_plane_indexes()
        await provision_tenant(tenant_id, wait_for_queryable_indexes=False)

        await save_tenant_config(
            tenant_id,
            EmbeddingConfig(
                provider="openai",
                model="text-embedding-3-small",
                api_key="sk-live-tenant-secret",
                dimensions=1536,
            ),
            settings=qe_settings,
            updated_by="itest",
        )

        raw_client = MongoClient(
            qe_settings.mongodb_uri, serverSelectionTimeoutMS=3000, directConnection=True
        )
        try:
            raw_doc = raw_client[tenant_db_name(tenant_id)]["gateway_config"].find_one(
                {"_id": "embedding"}
            )
        finally:
            raw_client.close()

        assert raw_doc is not None
        assert raw_doc["api_key_encrypted"].startswith("qe::")
        assert "sk-live-tenant-secret" not in raw_doc["api_key_encrypted"]

        loaded = await load_tenant_config(tenant_id, settings=qe_settings)
        assert loaded.api_key == "sk-live-tenant-secret"

        assert await delete_tenant_data_key(tenant_id, qe_settings) is True
        assert await delete_tenant_data_key(tenant_id, qe_settings) is False

        after_shred = await load_tenant_config(tenant_id, settings=qe_settings)
        assert after_shred.api_key == ""
    finally:
        await disconnect_from_mongo()
