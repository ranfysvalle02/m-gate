from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any

from bson.binary import Binary
from bson.codec_options import DEFAULT_CODEC_OPTIONS
from pymongo.asynchronous.encryption import AsyncClientEncryption
from pymongo.asynchronous.mongo_client import AsyncMongoClient
from pymongo.encryption import Algorithm
from pymongo.encryption_options import AutoEncryptionOpts
from pymongo.errors import DuplicateKeyError

from config.settings import Settings, get_settings

ROUTING_REGISTRY_ENCRYPTED_FIELDS: dict[str, Any] = {
    "fields": [
        {"path": "env", "bsonType": "object", "keyId": None},
        {"path": "command", "bsonType": "string", "keyId": None},
        {"path": "args", "bsonType": "array", "keyId": None},
        {"path": "metadata", "bsonType": "object", "keyId": None},
    ]
}


def _key_vault_parts(namespace: str) -> tuple[str, str]:
    try:
        db_name, collection_name = namespace.split(".", 1)
    except ValueError as exc:
        raise ValueError("QE_KEY_VAULT_NAMESPACE must look like '<db>.<collection>'.") from exc
    if not db_name or not collection_name:
        raise ValueError("QE_KEY_VAULT_NAMESPACE must include both db and collection names.")
    return db_name, collection_name


def _atlas_client_options(settings: Settings) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if settings.atlas_tls:
        options["tls"] = True
    if settings.atlas_tls_ca_file:
        options["tlsCAFile"] = settings.atlas_tls_ca_file
    if settings.atlas_auth_source:
        options["authSource"] = settings.atlas_auth_source
    if settings.atlas_auth_mechanism:
        options["authMechanism"] = settings.atlas_auth_mechanism
    if settings.atlas_username:
        options["username"] = settings.atlas_username
    if settings.atlas_password:
        options["password"] = settings.atlas_password
    return options


def _local_master_key_bytes(settings: Settings) -> bytes:
    if not settings.qe_local_master_key:
        raise ValueError("QE local KMS requires QE_LOCAL_MASTER_KEY or QE_LOCAL_MASTER_KEY_FILE.")
    try:
        key = base64.b64decode(settings.qe_local_master_key, validate=True)
    except binascii.Error as exc:
        raise ValueError("QE_LOCAL_MASTER_KEY must be valid base64.") from exc
    if len(key) != 96:
        raise ValueError("QE_LOCAL_MASTER_KEY must decode to exactly 96 bytes.")
    return key


def kms_providers(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    if settings.kms_provider == "local":
        return {"local": {"key": _local_master_key_bytes(settings)}}
    if settings.kms_provider == "aws":
        provider: dict[str, Any] = {}
        if settings.aws_access_key_id:
            provider["accessKeyId"] = settings.aws_access_key_id
        if settings.aws_secret_access_key:
            provider["secretAccessKey"] = settings.aws_secret_access_key
        return {"aws": provider}
    raise ValueError("KMS_PROVIDER must be 'local' or 'aws' when QE is enabled.")


def master_key(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    if settings.kms_provider == "local":
        return {}
    if settings.kms_provider != "aws":
        raise ValueError("master_key is only available for KMS_PROVIDER=local|aws.")
    if not settings.aws_kms_key_arn:
        raise ValueError("AWS_KMS_KEY_ARN is required for KMS_PROVIDER=aws.")
    payload: dict[str, Any] = {
        "region": settings.aws_default_region,
        "key": settings.aws_kms_key_arn,
    }
    if settings.aws_kms_endpoint:
        payload["endpoint"] = settings.aws_kms_endpoint
    return payload


def build_auto_encryption_opts(settings: Settings | None = None) -> AutoEncryptionOpts:
    settings = settings or get_settings()
    crypt_shared_lib_path = settings.crypt_shared_lib_path or str(
        Path("/opt/mongodb/lib/mongo_crypt_v1.so")
    )
    return AutoEncryptionOpts(
        kms_providers(settings),
        settings.qe_key_vault_namespace,
        crypt_shared_lib_path=crypt_shared_lib_path,
        crypt_shared_lib_required=True,
    )


def build_watcher_client(settings: Settings | None = None) -> AsyncMongoClient:
    """Client for the registry change-stream watcher under Queryable Encryption.

    The shared app client auto-encrypts, and libmongocrypt forbids the
    cluster-wide ``aggregate``/``$changeStream`` such a watcher needs ("non-collection
    command not supported for auto encryption: aggregate"). ``bypass_auto_encryption``
    skips the command analysis that imposes that restriction, so the cluster-wide
    change stream is allowed — while automatic *decryption* of the encrypted
    ``routing_registry`` fields in each change event still happens. Decryption uses
    the embedded libmongocrypt, so no crypt_shared/mongocryptd is required here.
    """
    settings = settings or get_settings()
    opts = AutoEncryptionOpts(
        kms_providers(settings),
        settings.qe_key_vault_namespace,
        bypass_auto_encryption=True,
    )
    return AsyncMongoClient(
        settings.mongodb_uri,
        auto_encryption_opts=opts,
        **_atlas_client_options(settings),
    )


def _key_vault_client(settings: Settings) -> AsyncMongoClient:
    return AsyncMongoClient(settings.mongodb_uri, **_atlas_client_options(settings))


def get_client_encryption(
    settings: Settings | None = None,
) -> tuple[AsyncMongoClient, AsyncClientEncryption]:
    settings = settings or get_settings()
    client = _key_vault_client(settings)
    client_encryption: AsyncClientEncryption = AsyncClientEncryption(
        kms_providers(settings),
        settings.qe_key_vault_namespace,
        client,
        DEFAULT_CODEC_OPTIONS,
    )
    return client, client_encryption


async def ensure_key_vault(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    key_vault_db, key_vault_collection = _key_vault_parts(settings.qe_key_vault_namespace)
    key_vault_client = _key_vault_client(settings)
    try:
        collection = key_vault_client[key_vault_db][key_vault_collection]
        await collection.create_index(
            "keyAltNames",
            unique=True,
            partialFilterExpression={"keyAltNames": {"$exists": True}},
        )
    finally:
        close_result = key_vault_client.close()
        if close_result is not None:
            await close_result


async def create_encrypted_routing_registry(tenant_db, settings: Settings | None = None):
    settings = settings or get_settings()
    if "routing_registry" in set(await tenant_db.list_collection_names()):
        return tenant_db["routing_registry"]
    key_vault_client, client_encryption = get_client_encryption(settings)
    try:
        collection, _encrypted_fields = await client_encryption.create_encrypted_collection(
            tenant_db,
            "routing_registry",
            ROUTING_REGISTRY_ENCRYPTED_FIELDS,
            kms_provider=settings.kms_provider,
            master_key=master_key(settings),
        )
        return collection
    finally:
        await client_encryption.close()
        close_result = key_vault_client.close()
        if close_result is not None:
            await close_result


def _tenant_key_alt_name(tenant_id: str) -> str:
    return f"tenant:{tenant_id}"


async def ensure_tenant_data_key(tenant_id: str, settings: Settings | None = None) -> Any:
    settings = settings or get_settings()
    key_alt_name = _tenant_key_alt_name(tenant_id)
    key_vault_client, client_encryption = get_client_encryption(settings)
    try:
        existing = await client_encryption.get_key_by_alt_name(key_alt_name)
        if existing is not None:
            return existing["_id"]
        try:
            return await client_encryption.create_data_key(
                settings.kms_provider,
                master_key=master_key(settings),
                key_alt_names=[key_alt_name],
            )
        except DuplicateKeyError:
            # Another concurrent provisioner/save likely created the same keyAltName.
            existing = await client_encryption.get_key_by_alt_name(key_alt_name)
            if existing is None:
                raise
            return existing["_id"]
    finally:
        await client_encryption.close()
        close_result = key_vault_client.close()
        if close_result is not None:
            await close_result


async def encrypt_tenant_secret(
    tenant_id: str,
    plaintext: str,
    settings: Settings | None = None,
) -> Binary:
    settings = settings or get_settings()
    key_alt_name = _tenant_key_alt_name(tenant_id)
    await ensure_tenant_data_key(tenant_id, settings)
    key_vault_client, client_encryption = get_client_encryption(settings)
    try:
        encrypted = await client_encryption.encrypt(
            plaintext.encode("utf-8"),
            Algorithm.AEAD_AES_256_CBC_HMAC_SHA_512_Random,
            key_alt_name=key_alt_name,
        )
        return encrypted
    finally:
        await client_encryption.close()
        close_result = key_vault_client.close()
        if close_result is not None:
            await close_result


async def decrypt_tenant_secret(
    ciphertext: Binary,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    key_vault_client, client_encryption = get_client_encryption(settings)
    try:
        decrypted = await client_encryption.decrypt(ciphertext)
        if isinstance(decrypted, bytes):
            return decrypted.decode("utf-8")
        return str(decrypted)
    finally:
        await client_encryption.close()
        close_result = key_vault_client.close()
        if close_result is not None:
            await close_result


async def delete_tenant_data_key(tenant_id: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    key_alt_name = _tenant_key_alt_name(tenant_id)
    key_vault_client, client_encryption = get_client_encryption(settings)
    try:
        existing = await client_encryption.get_key_by_alt_name(key_alt_name)
        if existing is None:
            return False
        result = await client_encryption.delete_key(existing["_id"])
        return bool(getattr(result, "deleted_count", 0))
    finally:
        await client_encryption.close()
        close_result = key_vault_client.close()
        if close_result is not None:
            await close_result


async def qe_status(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    status: dict[str, Any] = {
        "enabled": settings.qe_enabled,
        "kms_provider": settings.kms_provider,
        "key_vault_namespace": settings.qe_key_vault_namespace,
        "crypt_shared_lib_path": settings.crypt_shared_lib_path
        or "/opt/mongodb/lib/mongo_crypt_v1.so",
    }
    if not settings.qe_enabled:
        status["ok"] = True
        return status
    shared_lib_path = Path(status["crypt_shared_lib_path"])
    status["crypt_shared_lib_present"] = shared_lib_path.exists()
    key_vault_db, key_vault_collection = _key_vault_parts(settings.qe_key_vault_namespace)
    key_vault_client = _key_vault_client(settings)
    try:
        await key_vault_client.admin.command("ping")
        await key_vault_client[key_vault_db][key_vault_collection].estimated_document_count()
        status["key_vault_reachable"] = True
        status["ok"] = bool(status["crypt_shared_lib_present"])
    except Exception as exc:
        status["key_vault_reachable"] = False
        status["ok"] = False
        status["error"] = exc.__class__.__name__
    finally:
        close_result = key_vault_client.close()
        if close_result is not None:
            await close_result
    return status
