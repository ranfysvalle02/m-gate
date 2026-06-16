from __future__ import annotations

import hashlib
import inspect
import re
from datetime import UTC, datetime

from pymongo import AsyncMongoClient

from config.settings import Settings, get_settings
from database.encryption import build_auto_encryption_opts, build_watcher_client

_client: AsyncMongoClient | None = None
# Long-lived companion client that bypasses QE auto-encryption *query analysis*
# (created lazily, only when QE is enabled). See get_qe_bypass_client().
_qe_bypass_client: AsyncMongoClient | None = None


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
        if cfg.qe_enabled:
            options["auto_encryption_opts"] = build_auto_encryption_opts(cfg)
        _client = AsyncMongoClient(cfg.mongodb_uri, **options)
        await _client.admin.command("ping")
    return _client


async def disconnect_from_mongo() -> None:
    global _client, _qe_bypass_client
    if _qe_bypass_client is not None:
        close_result = _qe_bypass_client.close()
        if inspect.isawaitable(close_result):
            await close_result
        _qe_bypass_client = None
    if _client is not None:
        close_result = _client.close()
        if inspect.isawaitable(close_result):
            await close_result
        _client = None


def get_client() -> AsyncMongoClient:
    if _client is None:
        raise RuntimeError("MongoDB client is not connected. Call connect_to_mongo() first.")
    return _client


def get_qe_bypass_client() -> AsyncMongoClient:
    """Long-lived client that bypasses QE auto-encryption *query analysis*.

    Under Queryable Encryption the shared app client must run every command through
    ``crypt_shared``'s allow-listed query analysis. That analysis rejects compound
    Atlas Search stages like ``$rankFusion`` ("No resolved namespace provided")
    even on collections that hold no encrypted fields. This client sets
    ``bypass_auto_encryption=True`` so such reads/aggregations reach the server
    unmodified, while automatic *decryption* of any returned encrypted fields still
    happens (via embedded libmongocrypt; no crypt_shared/mongocryptd needed).

    Scope: READS / AGGREGATIONS on non-encrypted collections only (e.g.
    ``tool_catalog`` hybrid search). NEVER use it to WRITE ``routing_registry`` —
    those fields must still be auto-encrypted by the shared client. Only call this
    when ``qe_enabled`` is true (it builds QE auto-encryption options).
    """
    global _qe_bypass_client
    if _qe_bypass_client is None:
        _qe_bypass_client = build_watcher_client(get_settings())
    return _qe_bypass_client


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
    # Tenant db names are always ``{prefix}{sanitized}_{sha256[:8]}``; anything
    # that doesn't carry the hash suffix is not one of ours.
    match = re.match(r"(?P<tenant>.+)_[0-9a-f]{8}$", raw)
    if not match:
        return None
    return match.group("tenant")


def get_tenant_database(tenant_id: str):
    return get_database(tenant_db_name(tenant_id))


def get_tenant_database_for_search(tenant_id: str):
    """Tenant DB handle for Atlas Search / Vector Search / ``$rankFusion`` reads.

    Under QE, the shared client's auto-encryption query analysis can't analyze
    ``$rankFusion`` (see get_qe_bypass_client), so catalog search is routed through
    the bypass client. Without QE the normal client is returned unchanged. Safe
    because ``tool_catalog`` holds no encrypted fields.
    """
    if get_settings().qe_enabled:
        return get_qe_bypass_client()[tenant_db_name(tenant_id)]
    return get_tenant_database(tenant_id)
