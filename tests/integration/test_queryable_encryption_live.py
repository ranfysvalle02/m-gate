from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

import pytest
from bson.binary import Binary
from pymongo import MongoClient

from config.settings import get_settings
from database.mongo import (
    connect_to_mongo,
    disconnect_from_mongo,
    get_tenant_database,
    tenant_db_name,
)
from services.tenant_provisioner import ensure_control_plane_indexes, provision_tenant

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def qe_settings(atlas_uri, monkeypatch):
    pytest.importorskip("pymongocrypt")
    crypt_shared = Path(os.environ.get("CRYPT_SHARED_LIB_PATH", "/opt/mongodb/lib/mongo_crypt_v1.so"))
    if not crypt_shared.exists():
        pytest.skip(
            f"Queryable Encryption integration test requires crypt_shared at {crypt_shared}.",
            allow_module_level=True,
        )

    db_name = f"itest_qe_{uuid.uuid4().hex[:8]}"
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


async def test_qe_encrypts_routing_registry_and_auto_decrypts_reads(qe_settings):
    await connect_to_mongo(qe_settings)
    try:
        await ensure_control_plane_indexes()
        await provision_tenant(qe_settings.default_tenant_id, wait_for_queryable_indexes=False)

        tenant_db = get_tenant_database(qe_settings.default_tenant_id)
        await tenant_db["routing_registry"].replace_one(
            {"_id": "qe-itest"},
            {
                "_id": "qe-itest",
                "tenant_id": qe_settings.default_tenant_id,
                "server": "qe-itest",
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "servers.weather.server"],
                "env": {"DOWNSTREAM_API_TOKEN": "super-secret-token"},
                "metadata": {"purpose": "qe-live-test"},
                "enabled": True,
                "tools": [],
            },
            upsert=True,
        )

        app_view = await tenant_db["routing_registry"].find_one({"_id": "qe-itest"})
        assert app_view is not None
        assert app_view["env"]["DOWNSTREAM_API_TOKEN"] == "super-secret-token"
        assert app_view["command"] == "python"

        raw_client = MongoClient(
            qe_settings.mongodb_uri, serverSelectionTimeoutMS=3000, directConnection=True
        )
        try:
            raw_view = raw_client[tenant_db_name(qe_settings.default_tenant_id)]["routing_registry"].find_one(
                {"_id": "qe-itest"}
            )
        finally:
            raw_client.close()

        assert raw_view is not None
        assert isinstance(raw_view["env"], Binary)
        assert isinstance(raw_view["command"], Binary)
        assert raw_view["env"] != app_view["env"]
        assert raw_view["command"] != app_view["command"]
    finally:
        await disconnect_from_mongo()
