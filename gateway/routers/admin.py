from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from config.settings import get_settings
from database.mongo import get_control_database, get_tenant_database
from models.admin import (
    AdminSearchRequest,
    CacheMigrateRequest,
    CatalogItemResponse,
    CatalogListRequest,
    CatalogListResponse,
    EmbeddingConfigResponse,
    EmbeddingConfigUpdateRequest,
    EmbeddingTestRequest,
    EmbeddingTestResponse,
    ServerPatchRequest,
    ServerUpsertRequest,
    StatsResponse,
    TelemetryEventResponse,
    TelemetryListRequest,
    TelemetryListResponse,
    TenantCreateRequest,
    TenantResponse,
    TenantStats,
    WhoAmIResponse,
)
from services.cache_migration import SemanticCacheMigrationService
from services.embedding_config import (
    EmbeddingConfig,
    default_model_for,
    load_persisted_config,
    load_tenant_config,
    refresh_active_embedding_config,
    save_persisted_config,
    save_tenant_config,
    validate_config,
)
from services.embedding_reprovision import (
    ReprovisionInProgressError,
    get_reprovision_status,
    get_tenant_reprovision_status,
    is_reprovision_running,
    is_tenant_reprovision_running,
    trigger_reprovision,
    trigger_tenant_reprovision,
)
from services.embeddings import (
    SUPPORTED_PROVIDERS,
    build_provider_service,
    embedding_version_for,
)
from services.hybrid_search import HybridSearchService
from services.proxy_registry import get_proxy_registry
from services.registry_watcher import get_catalog_version
from services.tenant_provisioner import provision_tenant

router = APIRouter(prefix="/admin", tags=["admin"])
settings = get_settings()
cache_migration_service = SemanticCacheMigrationService()
hybrid_search_service = HybridSearchService()


def _is_platform_admin(request: Request) -> bool:
    roles = set(getattr(request.state, "roles", []))
    return settings.platform_admin_role in roles


def _require_platform_admin(request: Request) -> None:
    if not _is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Embedding configuration requires the platform-admin role.",
        )


def _resolve_target_tenant(request: Request, requested_tenant: str | None = None) -> str:
    caller_tenant = getattr(request.state, "tenant_id", settings.default_tenant_id)
    header_tenant = request.headers.get("x-tenant-id")
    target = requested_tenant or header_tenant or caller_tenant
    if target != caller_tenant and not _is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-tenant admin access requires platform-admin role.",
        )
    return target


def _validate_server_doc(server_doc: dict[str, Any]) -> None:
    transport = server_doc.get("transport")
    endpoint = server_doc.get("endpoint")
    command = server_doc.get("command")
    if transport in {"streamable_http", "sse"} and not endpoint:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"transport={transport} requires endpoint.",
        )
    if transport == "stdio" and not command:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="transport=stdio requires command.",
        )


def _to_server_doc(payload: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    server = payload["server"]
    doc = dict(payload)
    doc["tenant_id"] = tenant_id
    doc["_id"] = server
    doc["args"] = [str(arg) for arg in (doc.get("args") or [])]
    doc["env"] = {str(key): str(value) for key, value in (doc.get("env") or {}).items()}
    return doc


def _public_server_doc(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "tenant_id": doc.get("tenant_id"),
        "server": doc.get("server"),
        "transport": doc.get("transport"),
        "endpoint": doc.get("endpoint"),
        "command": doc.get("command"),
        "args": doc.get("args", []),
        "env": doc.get("env", {}),
        "cwd": doc.get("cwd"),
        "enabled": bool(doc.get("enabled", True)),
        "metadata": doc.get("metadata", {}),
        "tools": doc.get("tools", []),
    }


async def _resolve_target_tenants_for_cache_migration(
    request: Request,
    *,
    requested_tenant: str | None,
) -> list[str]:
    if requested_tenant:
        return [_resolve_target_tenant(request, requested_tenant)]

    if _is_platform_admin(request):
        docs = await get_control_database()["tenants"].find({}).to_list(length=10_000)
        tenant_ids = sorted(
            {str(doc.get("tenant_id")) for doc in docs if isinstance(doc.get("tenant_id"), str)}
        )
        return tenant_ids or [settings.default_tenant_id]

    return [getattr(request.state, "tenant_id", settings.default_tenant_id)]


@router.post("/tenants", response_model=TenantResponse)
async def create_tenant(request: Request, payload: TenantCreateRequest) -> TenantResponse:
    tenant_id = _resolve_target_tenant(request, payload.tenant_id)
    db_name = await provision_tenant(tenant_id, wait_for_queryable_indexes=False)
    doc = await get_control_database()["tenants"].find_one({"tenant_id": tenant_id})
    if not doc:
        return TenantResponse(tenant_id=tenant_id, db_name=db_name)
    return TenantResponse(
        tenant_id=tenant_id,
        db_name=str(doc.get("db_name") or db_name),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


@router.get("/tenants", response_model=list[TenantResponse])
async def list_tenants(request: Request) -> list[TenantResponse]:
    control_db = get_control_database()
    if _is_platform_admin(request):
        docs = await control_db["tenants"].find({}).to_list(length=10_000)
    else:
        tenant_id = getattr(request.state, "tenant_id", settings.default_tenant_id)
        doc = await control_db["tenants"].find_one({"tenant_id": tenant_id})
        docs = [doc] if doc else []
    return [
        TenantResponse(
            tenant_id=str(doc.get("tenant_id")),
            db_name=str(doc.get("db_name")),
            created_at=doc.get("created_at"),
            updated_at=doc.get("updated_at"),
        )
        for doc in docs
        if doc
    ]


@router.post("/servers")
async def create_or_update_server(request: Request, payload: ServerUpsertRequest) -> dict[str, Any]:
    tenant_id = _resolve_target_tenant(request, payload.tenant_id)
    await provision_tenant(tenant_id, wait_for_queryable_indexes=False)
    doc = _to_server_doc(payload.model_dump(), tenant_id)
    _validate_server_doc(doc)
    collection = get_tenant_database(tenant_id)["routing_registry"]
    await collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    if doc.get("enabled", True):
        await get_proxy_registry().mount_or_update(doc)
    else:
        await get_proxy_registry().unmount(doc["server"], tenant_id=tenant_id)
    return _public_server_doc(doc)


@router.get("/servers")
async def list_servers(
    request: Request,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    docs = (
        await get_tenant_database(target_tenant)["routing_registry"].find({}).to_list(length=10_000)
    )
    docs.sort(key=lambda item: str(item.get("server", "")))
    return {
        "tenant_id": target_tenant,
        "items": [_public_server_doc(doc) for doc in docs],
    }


@router.get("/servers/{server_name}")
async def get_server(
    request: Request,
    server_name: str,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    doc = await get_tenant_database(target_tenant)["routing_registry"].find_one(
        {"_id": server_name}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Server not found.")
    return _public_server_doc(doc)


@router.patch("/servers/{server_name}")
async def patch_server(
    request: Request,
    server_name: str,
    payload: ServerPatchRequest,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    requested_tenant = payload.tenant_id or tenant_id
    target_tenant = _resolve_target_tenant(request, requested_tenant)
    collection = get_tenant_database(target_tenant)["routing_registry"]
    existing = await collection.find_one({"_id": server_name})
    if not existing:
        raise HTTPException(status_code=404, detail="Server not found.")

    updates = payload.model_dump(exclude_none=True)
    updates.pop("tenant_id", None)
    merged = dict(existing)
    merged.update(updates)
    merged["server"] = server_name
    merged["_id"] = server_name
    merged["tenant_id"] = target_tenant
    merged["args"] = [str(arg) for arg in (merged.get("args") or [])]
    merged["env"] = {str(key): str(value) for key, value in (merged.get("env") or {}).items()}

    _validate_server_doc(merged)
    await collection.replace_one({"_id": server_name}, merged, upsert=True)
    if merged.get("enabled", True):
        await get_proxy_registry().mount_or_update(merged)
    else:
        await get_proxy_registry().unmount(server_name, tenant_id=target_tenant)
    return _public_server_doc(merged)


@router.delete("/servers/{server_name}")
async def delete_server(
    request: Request,
    server_name: str,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    collection = get_tenant_database(target_tenant)["routing_registry"]
    result = await collection.delete_many({"_id": server_name})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Server not found.")
    await get_proxy_registry().unmount(server_name, tenant_id=target_tenant)
    return {"deleted": True, "tenant_id": target_tenant, "server": server_name}


@router.post("/cache/migrate")
async def migrate_cache(request: Request, payload: CacheMigrateRequest) -> dict[str, Any]:
    tenant_ids = await _resolve_target_tenants_for_cache_migration(
        request,
        requested_tenant=payload.tenant_id,
    )
    for tenant_id in tenant_ids:
        await provision_tenant(tenant_id, wait_for_queryable_indexes=False)
    return await cache_migration_service.migrate(
        tenant_ids=tenant_ids,
        mode=payload.mode,
        batch_size=payload.batch_size,
    )


@router.get("/whoami", response_model=WhoAmIResponse)
async def who_am_i(request: Request) -> WhoAmIResponse:
    roles = list(getattr(request.state, "roles", []))
    scopes = list(getattr(request.state, "scopes", []))
    tenant_id = str(getattr(request.state, "tenant_id", settings.default_tenant_id))
    user_id = str(getattr(request.state, "user_id", "anonymous"))
    return WhoAmIResponse(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=roles,
        scopes=scopes,
        is_platform_admin=settings.platform_admin_role in set(roles),
        auth_mode=settings.auth_mode,
    )


@router.get("/catalog", response_model=CatalogListResponse)
async def list_catalog(
    request: Request,
    tenant_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> CatalogListResponse:
    params = CatalogListRequest(tenant_id=tenant_id, limit=limit, offset=offset)
    target_tenant = _resolve_target_tenant(request, params.tenant_id)
    docs = await get_tenant_database(target_tenant)["tool_catalog"].find({}).to_list(length=10_000)
    docs.sort(key=lambda item: (str(item.get("server", "")), str(item.get("name", ""))))
    window = docs[offset : offset + params.limit]
    items = [
        CatalogItemResponse(
            server=str(doc.get("server", "")),
            name=str(doc.get("name", "")),
            description=str(doc.get("description", "")),
            scopes=[scope for scope in doc.get("scopes", []) if isinstance(scope, str)],
            updated_at=doc.get("updated_at"),
        )
        for doc in window
    ]
    return CatalogListResponse(
        tenant_id=target_tenant,
        items=items,
        total=len(docs),
        limit=params.limit,
        offset=params.offset,
    )


@router.get("/telemetry", response_model=TelemetryListResponse)
async def list_telemetry(
    request: Request,
    tenant_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> TelemetryListResponse:
    params = TelemetryListRequest(tenant_id=tenant_id, limit=limit)
    target_tenant = _resolve_target_tenant(request, params.tenant_id)
    docs = (
        await get_tenant_database(target_tenant)["audit_telemetry"].find({}).to_list(length=10_000)
    )
    docs.sort(
        key=lambda item: item.get("timestamp")
        if isinstance(item.get("timestamp"), datetime)
        else datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    items = [
        TelemetryEventResponse(
            timestamp=doc.get("timestamp"),
            tenant_id=str(doc.get("tenant_id", target_tenant)),
            user_id=str(doc.get("user_id", "unknown-user")),
            request_id=doc.get("request_id"),
            method=str(doc.get("method", "")),
            status=str(doc.get("status", "")),
            latency_ms=doc.get("latency_ms"),
            metadata=doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {},
        )
        for doc in docs[: params.limit]
    ]
    return TelemetryListResponse(tenant_id=target_tenant, items=items)


@router.get("/stats", response_model=StatsResponse)
async def admin_stats(request: Request) -> StatsResponse:
    roles = set(getattr(request.state, "roles", []))
    is_platform_admin = settings.platform_admin_role in roles
    if is_platform_admin:
        tenant_docs = await get_control_database()["tenants"].find({}).to_list(length=10_000)
        tenant_ids = sorted(
            {
                str(doc.get("tenant_id"))
                for doc in tenant_docs
                if isinstance(doc.get("tenant_id"), str)
            }
        )
    else:
        tenant_ids = [str(getattr(request.state, "tenant_id", settings.default_tenant_id))]
    if not tenant_ids:
        tenant_ids = [settings.default_tenant_id]

    tenant_rows: list[TenantStats] = []
    status_counts: dict[str, int] = {}
    for tenant_id in tenant_ids:
        tenant_db = get_tenant_database(tenant_id)
        server_docs = await tenant_db["routing_registry"].find({}).to_list(length=10_000)
        tool_docs = await tenant_db["tool_catalog"].find({}).to_list(length=10_000)
        telemetry_docs = await tenant_db["audit_telemetry"].find({}).to_list(length=10_000)
        for doc in telemetry_docs:
            status = str(doc.get("status", "unknown"))
            status_counts[status] = status_counts.get(status, 0) + 1
        tenant_rows.append(
            TenantStats(
                tenant_id=tenant_id,
                server_count=len(server_docs),
                enabled_server_count=sum(
                    1 for doc in server_docs if bool(doc.get("enabled", True))
                ),
                tool_count=len(tool_docs),
            )
        )
    tenant_rows.sort(key=lambda row: row.tenant_id)
    return StatsResponse(
        tenant_count=len(tenant_ids) if is_platform_admin else None,
        catalog_version=get_catalog_version(),
        telemetry_status_counts=status_counts,
        tenants=tenant_rows,
    )


@router.post("/search")
async def admin_search(request: Request, payload: AdminSearchRequest) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    items = await hybrid_search_service.search_tools(
        tenant_id=target_tenant,
        query=payload.query,
        limit=payload.limit,
        vector_weight=payload.vector_weight,
        text_weight=payload.text_weight,
        mode=payload.mode,
    )
    return {"tenant_id": target_tenant, "mode": payload.mode, "items": items}


def _merge_embedding_config(
    current: EmbeddingConfig,
    payload: EmbeddingConfigUpdateRequest | EmbeddingTestRequest,
) -> EmbeddingConfig:
    """Overlay a request onto the current config without losing the stored key.

    - A ``None`` model on a provider switch resolves the new provider's default.
    - ``api_key=None`` keeps the existing key; ``""`` clears it; otherwise replace.
    """
    if payload.model is not None:
        model = payload.model
    elif payload.provider != current.provider:
        model = default_model_for(payload.provider, settings)
    else:
        model = current.model

    api_key = current.api_key if payload.api_key is None else payload.api_key

    base_url: str | None
    if payload.base_url is not None:
        base_url = payload.base_url
    elif payload.provider == current.provider:
        base_url = current.base_url
    else:
        base_url = None

    return EmbeddingConfig(
        provider=payload.provider,
        model=model,
        base_url=base_url,
        dimensions=0,
        api_key=api_key,
        azure_endpoint=(
            payload.azure_endpoint if payload.azure_endpoint is not None else current.azure_endpoint
        ),
        azure_api_version=payload.azure_api_version or current.azure_api_version,
        azure_deployment=(
            payload.azure_deployment
            if payload.azure_deployment is not None
            else current.azure_deployment
        ),
    )


async def _validate_and_detect_dimensions(candidate: EmbeddingConfig) -> EmbeddingConfig:
    try:
        validate_config(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Validate the provider for real and detect the actual vector width by probing
    # it. This both rejects an unusable config before anything is persisted and
    # guarantees the stored dimension always equals the provider's real output
    # length (so Atlas vector indexes can never drift out of sync).
    service = build_provider_service(candidate, settings)
    try:
        detected = await service.detect_dimensions()
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Embedding provider validation failed: {exc}",
        ) from exc
    return replace(candidate, dimensions=detected)


def _embedding_config_response(
    config: EmbeddingConfig,
    reprovision: dict[str, Any] | None = None,
) -> EmbeddingConfigResponse:
    service = build_provider_service(config, settings)
    return EmbeddingConfigResponse(
        provider=config.provider,  # type: ignore[arg-type]
        model=config.model or (config.azure_deployment or ""),
        base_url=config.base_url,
        dimensions=config.dimensions,
        embedding_version=embedding_version_for(service),
        api_key_set=config.has_api_key,
        api_key_hint=config.api_key_hint,
        azure_endpoint=config.azure_endpoint,
        azure_api_version=config.azure_api_version,
        azure_deployment=config.azure_deployment,
        supported_providers=list(SUPPORTED_PROVIDERS),
        source=config.source,
        updated_at=config.updated_at,
        updated_by=config.updated_by,
        reprovision=reprovision or {},
    )


@router.get("/embedding", response_model=EmbeddingConfigResponse)
async def get_embedding_config(request: Request) -> EmbeddingConfigResponse:
    _require_platform_admin(request)
    config = await load_persisted_config(settings)
    reprovision = await get_reprovision_status()
    return _embedding_config_response(config, reprovision)


@router.put("/embedding", response_model=EmbeddingConfigResponse)
async def update_embedding_config(
    request: Request,
    payload: EmbeddingConfigUpdateRequest,
) -> EmbeddingConfigResponse:
    _require_platform_admin(request)
    user_id = str(getattr(request.state, "user_id", "admin"))

    # Refuse to mutate the active embedding config while a reprovision is running:
    # the in-flight job reads the active provider, so swapping it mid-run would
    # produce an inconsistent vector space across tenants.
    if await is_reprovision_running():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An embedding reprovision is in progress; retry once it completes.",
        )

    current = await load_persisted_config(settings)
    candidate = _merge_embedding_config(current, payload)
    candidate = await _validate_and_detect_dimensions(candidate)

    await save_persisted_config(candidate, settings, updated_by=user_id)
    refreshed = await refresh_active_embedding_config(settings)

    reprovision: dict[str, Any] = {}
    if payload.reprovision:
        try:
            reprovision = await trigger_reprovision(started_by=user_id)
        except ReprovisionInProgressError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return _embedding_config_response(refreshed, reprovision)


@router.post("/embedding/test", response_model=EmbeddingTestResponse)
async def test_embedding_config(
    request: Request,
    payload: EmbeddingTestRequest,
) -> EmbeddingTestResponse:
    _require_platform_admin(request)
    current = await load_persisted_config(settings)
    candidate = _merge_embedding_config(current, payload)
    try:
        validate_config(candidate)
        service = build_provider_service(candidate, settings)
        dims = await service.detect_dimensions()
        version = f"{service.model_id}:{dims}"
    except Exception as exc:
        return EmbeddingTestResponse(
            ok=False,
            provider=candidate.provider,
            model=candidate.model or (candidate.azure_deployment or ""),
            message=str(exc),
        )
    return EmbeddingTestResponse(
        ok=True,
        provider=candidate.provider,
        model=service.model_id,
        dimensions=dims,
        embedding_version=version,
        message="Embedding provider reachable.",
    )


@router.get("/tenants/{tenant_id}/embedding", response_model=EmbeddingConfigResponse)
async def get_tenant_embedding_config(
    request: Request,
    tenant_id: str,
) -> EmbeddingConfigResponse:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    config = await load_tenant_config(target_tenant, settings)
    reprovision = await get_tenant_reprovision_status(target_tenant)
    return _embedding_config_response(config, reprovision)


@router.put("/tenants/{tenant_id}/embedding", response_model=EmbeddingConfigResponse)
async def update_tenant_embedding_config(
    request: Request,
    tenant_id: str,
    payload: EmbeddingConfigUpdateRequest,
) -> EmbeddingConfigResponse:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    user_id = str(getattr(request.state, "user_id", "admin"))
    if await is_tenant_reprovision_running(target_tenant):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An embedding reprovision is in progress for tenant '{target_tenant}'.",
        )

    await provision_tenant(target_tenant, wait_for_queryable_indexes=False)
    current = await load_tenant_config(target_tenant, settings)
    candidate = await _validate_and_detect_dimensions(_merge_embedding_config(current, payload))

    await save_tenant_config(target_tenant, candidate, settings, updated_by=user_id)
    refreshed = await load_tenant_config(target_tenant, settings)
    reprovision: dict[str, Any] = {}
    if payload.reprovision:
        try:
            reprovision = await trigger_tenant_reprovision(
                tenant_id=target_tenant,
                started_by=user_id,
            )
        except ReprovisionInProgressError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _embedding_config_response(refreshed, reprovision)


@router.post("/tenants/{tenant_id}/embedding/test", response_model=EmbeddingTestResponse)
async def test_tenant_embedding_config(
    request: Request,
    tenant_id: str,
    payload: EmbeddingTestRequest,
) -> EmbeddingTestResponse:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    current = await load_tenant_config(target_tenant, settings)
    candidate = _merge_embedding_config(current, payload)
    try:
        validate_config(candidate)
        service = build_provider_service(candidate, settings)
        dims = await service.detect_dimensions()
        version = f"{service.model_id}:{dims}"
    except Exception as exc:
        return EmbeddingTestResponse(
            ok=False,
            provider=candidate.provider,
            model=candidate.model or (candidate.azure_deployment or ""),
            message=str(exc),
        )
    return EmbeddingTestResponse(
        ok=True,
        provider=candidate.provider,
        model=service.model_id,
        dimensions=dims,
        embedding_version=version,
        message="Embedding provider reachable.",
    )


@router.get("/tenants/{tenant_id}/embedding/status")
async def get_tenant_embedding_status(
    request: Request,
    tenant_id: str,
) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    return await get_tenant_reprovision_status(target_tenant)


@router.get("/embedding/status")
async def get_embedding_status(request: Request) -> dict[str, Any]:
    _require_platform_admin(request)
    return await get_reprovision_status()
