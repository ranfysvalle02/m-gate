"""Platform-default and per-tenant embedding provider configuration."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import HTTPException, Request, status

from models.admin import (
    EmbeddingConfigResponse,
    EmbeddingConfigUpdateRequest,
    EmbeddingTestRequest,
    EmbeddingTestResponse,
)
from services.embedding_config import (
    EmbeddingConfig,
    default_model_for,
    delete_tenant_config,
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
    is_reprovision_running,
    is_tenant_reprovision_running,
)
from services.embeddings import SUPPORTED_PROVIDERS, embedding_version_for

from . import _common as c
from ._common import _require_platform_admin, _resolve_target_tenant, router, settings


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
    service = c.build_provider_service(candidate, settings)
    try:
        detected = await service.detect_dimensions()
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Embedding provider validation failed: {exc}",
        ) from exc
    return replace(candidate, dimensions=detected)


def _secret_encryption_label(config: EmbeddingConfig) -> str | None:
    """Describe how this config's API key is protected at rest.

    Tenant overrides use a per-tenant Queryable Encryption DEK when QE is on;
    everything else (platform default, QE disabled) uses the shared Fernet seam.
    """
    if not config.has_api_key:
        return None
    if config.source == "tenant-db" and settings.qe_enabled:
        return "per-tenant-dek"
    return "shared-fernet"


def _embedding_config_response(
    config: EmbeddingConfig,
    reprovision: dict[str, Any] | None = None,
) -> EmbeddingConfigResponse:
    service = c.build_provider_service(config, settings)
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
        secret_encryption=_secret_encryption_label(config),
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
            reprovision = await c.trigger_reprovision(started_by=user_id)
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
        service = c.build_provider_service(candidate, settings)
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
    reprovision = await c.get_tenant_reprovision_status(target_tenant)
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

    await c.provision_tenant(target_tenant, wait_for_queryable_indexes=False)
    current = await load_tenant_config(target_tenant, settings)
    candidate = await _validate_and_detect_dimensions(_merge_embedding_config(current, payload))

    await save_tenant_config(target_tenant, candidate, settings, updated_by=user_id)
    refreshed = await load_tenant_config(target_tenant, settings)
    reprovision: dict[str, Any] = {}
    if payload.reprovision:
        try:
            reprovision = await c.trigger_tenant_reprovision(
                tenant_id=target_tenant,
                started_by=user_id,
            )
        except ReprovisionInProgressError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _embedding_config_response(refreshed, reprovision)


@router.delete("/tenants/{tenant_id}/embedding", response_model=EmbeddingConfigResponse)
async def reset_tenant_embedding_config(
    request: Request,
    tenant_id: str,
    reprovision: bool = False,
) -> EmbeddingConfigResponse:
    """Remove a tenant's override so it inherits the platform default again.

    Optionally re-embeds the tenant (``?reprovision=true``) so its stored vectors
    realign with the inherited platform provider.
    """
    target_tenant = _resolve_target_tenant(request, tenant_id)
    user_id = str(getattr(request.state, "user_id", "admin"))
    if await is_tenant_reprovision_running(target_tenant):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An embedding reprovision is in progress for tenant '{target_tenant}'.",
        )

    deleted = await delete_tenant_config(target_tenant, settings)
    refreshed = await load_tenant_config(target_tenant, settings)
    repro: dict[str, Any] = {}
    if reprovision and deleted:
        try:
            repro = await c.trigger_tenant_reprovision(
                tenant_id=target_tenant,
                started_by=user_id,
            )
        except ReprovisionInProgressError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _embedding_config_response(refreshed, repro)


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
        service = c.build_provider_service(candidate, settings)
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
    return await c.get_tenant_reprovision_status(target_tenant)


@router.get("/embedding/status")
async def get_embedding_status(request: Request) -> dict[str, Any]:
    _require_platform_admin(request)
    return await get_reprovision_status()
