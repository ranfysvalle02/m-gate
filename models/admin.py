from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from models.registry import ToolDocument


class TenantCreateRequest(BaseModel):
    tenant_id: str


class TenantResponse(BaseModel):
    tenant_id: str
    db_name: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ServerUpsertRequest(BaseModel):
    tenant_id: str | None = None
    server: str
    transport: Literal["streamable_http", "sse", "stdio"] = "streamable_http"
    endpoint: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    tools: list[ToolDocument] = Field(default_factory=list)


class ServerPatchRequest(BaseModel):
    tenant_id: str | None = None
    transport: Literal["streamable_http", "sse", "stdio"] | None = None
    endpoint: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None
    tools: list[ToolDocument] | None = None


class CacheMigrateRequest(BaseModel):
    tenant_id: str | None = None
    mode: Literal["status", "purge", "reembed"] = "status"
    batch_size: int = Field(default=200, ge=1, le=10_000)


class WhoAmIResponse(BaseModel):
    tenant_id: str
    user_id: str
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    is_platform_admin: bool
    auth_mode: Literal["disabled", "hs256", "jwks"]


class CatalogListRequest(BaseModel):
    tenant_id: str | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class CatalogItemResponse(BaseModel):
    server: str
    name: str
    description: str = ""
    scopes: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class CatalogListResponse(BaseModel):
    tenant_id: str
    items: list[CatalogItemResponse]
    total: int
    limit: int
    offset: int


class TelemetryListRequest(BaseModel):
    tenant_id: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class TelemetryEventResponse(BaseModel):
    timestamp: datetime | None = None
    tenant_id: str
    user_id: str
    request_id: str | None = None
    method: str
    status: str
    latency_ms: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TelemetryListResponse(BaseModel):
    tenant_id: str
    items: list[TelemetryEventResponse]


class TenantStats(BaseModel):
    tenant_id: str
    server_count: int
    enabled_server_count: int
    tool_count: int


class StatsResponse(BaseModel):
    tenant_count: int | None = None
    catalog_version: int
    telemetry_status_counts: dict[str, int] = Field(default_factory=dict)
    tenants: list[TenantStats] = Field(default_factory=list)


class AdminSearchRequest(BaseModel):
    tenant_id: str | None = None
    query: str
    limit: int = Field(default=10, ge=1, le=50)
    mode: Literal["hybrid", "vector", "text"] = "hybrid"
    vector_weight: float | None = None
    text_weight: float | None = None


EmbeddingProvider = Literal["ollama", "openai", "azure_openai", "voyage", "gemini"]


class EmbeddingConfigResponse(BaseModel):
    provider: EmbeddingProvider
    model: str
    base_url: str | None = None
    dimensions: int
    embedding_version: str
    api_key_set: bool = False
    api_key_hint: str | None = None
    azure_endpoint: str | None = None
    azure_api_version: str | None = None
    azure_deployment: str | None = None
    supported_providers: list[str] = Field(default_factory=list)
    source: str = "env"
    updated_at: datetime | None = None
    updated_by: str | None = None
    reprovision: dict[str, Any] = Field(default_factory=dict)


class EmbeddingConfigUpdateRequest(BaseModel):
    provider: EmbeddingProvider
    # None means "leave unchanged"; for a provider switch the per-provider default
    # model is applied automatically.
    model: str | None = None
    base_url: str | None = None
    # None preserves the stored key; "" clears it; any other value replaces it.
    api_key: str | None = None
    azure_endpoint: str | None = None
    azure_api_version: str | None = None
    azure_deployment: str | None = None
    # Whether to kick off the catalog/cache/guardrail reprovision after saving.
    reprovision: bool = True


class EmbeddingTestRequest(BaseModel):
    provider: EmbeddingProvider
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    azure_endpoint: str | None = None
    azure_api_version: str | None = None
    azure_deployment: str | None = None


class EmbeddingTestResponse(BaseModel):
    ok: bool
    provider: str
    model: str
    dimensions: int | None = None
    embedding_version: str | None = None
    message: str
