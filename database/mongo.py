from __future__ import annotations

import hashlib
import inspect
import re
from datetime import UTC, datetime

from pymongo import AsyncMongoClient

from config.settings import Settings, get_settings

_client: AsyncMongoClient | None = None


async def connect_to_mongo(settings: Settings | None = None) -> AsyncMongoClient:
    global _client
    if _client is None:
        cfg = settings or get_settings()
        options: dict = {}
        if cfg.atlas_tls:
            options["tls"] = True
        if cfg.atlas_tls_ca_file:
            options["tlsCAFile"] = cfg.atlas_tls_ca_file
        if cfg.atlas_auth_source:
            options["authSource"] = cfg.atlas_auth_source
        if cfg.atlas_auth_mechanism:
            options["authMechanism"] = cfg.atlas_auth_mechanism
        if cfg.atlas_username:
            options["username"] = cfg.atlas_username
        if cfg.atlas_password:
            options["password"] = cfg.atlas_password
        _client = AsyncMongoClient(cfg.mongodb_uri, **options)
        await _client.admin.command("ping")
    return _client


async def disconnect_from_mongo() -> None:
    global _client
    if _client is not None:
        close_result = _client.close()
        if inspect.isawaitable(close_result):
            await close_result
        _client = None


def get_client() -> AsyncMongoClient:
    if _client is None:
        raise RuntimeError("MongoDB client is not connected. Call connect_to_mongo() first.")
    return _client


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def mongo_server_now() -> datetime:
    """Return the MongoDB server's wall-clock time as a UTC datetime.

    Used as the single source of truth for the distributed rate limiter so all
    gateway replicas agree on window boundaries regardless of local clock skew.
    Reads ``hostInfo.system.currentTime`` (falling back to ``localTime``); raises
    if neither is present so callers can degrade to the local clock explicitly.
    """
    host_info = await get_client().admin.command("hostInfo")
    if not isinstance(host_info, dict):
        raise RuntimeError("MongoDB hostInfo did not return a document.")

    system = host_info.get("system")
    current_time = system.get("currentTime") if isinstance(system, dict) else None
    if isinstance(current_time, datetime):
        return _as_utc(current_time)

    local_time = host_info.get("localTime")
    if isinstance(local_time, datetime):
        return _as_utc(local_time)
    raise RuntimeError("MongoDB hostInfo did not include a datetime clock value.")


def get_database(name: str | None = None):
    cfg = get_settings()
    db_name = name or cfg.mongodb_db_name
    return get_client()[db_name]


def get_control_database():
    return get_database(get_settings().mongodb_db_name)


def tenant_db_name(tenant_id: str) -> str:
    cfg = get_settings()
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", tenant_id).strip("_")
    if not sanitized:
        sanitized = "default"
    tenant_hash = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:8]
    return f"{cfg.tenant_db_prefix}{sanitized}_{tenant_hash}"


def tenant_id_from_db_name(db_name: str) -> str | None:
    cfg = get_settings()
    if not db_name.startswith(cfg.tenant_db_prefix):
        return None
    raw = db_name.removeprefix(cfg.tenant_db_prefix)
    if not raw:
        return None
    # Backwards compatibility with legacy db names that had no hash suffix.
    match = re.match(r"(?P<tenant>.+)_[0-9a-f]{8}$", raw)
    if match:
        return match.group("tenant")
    return raw


def get_tenant_database(tenant_id: str):
    return get_database(tenant_db_name(tenant_id))
