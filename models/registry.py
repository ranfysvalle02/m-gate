from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolDocument(BaseModel):
    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] = Field(default_factory=list)
    # Identity-bound scope tags. Mapped to the caller's verified groups/scopes
    # claim and used as a metadata filter on $vectorSearch (Section 1 of the blog).
    scopes: list[str] = Field(default_factory=list)
    # Code-backed tools (transport="code") only: the user-authored Python source
    # and its pinned PyPI requirements. ``raw_code`` is encrypted at rest before
    # it is persisted and is never written to the searchable tool_catalog.
    raw_code: str | None = None
    requirements: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoutingRegistryDocument(BaseModel):
    tenant_id: str = "local-dev"
    origin: Literal["platform", "tenant"] = "platform"
    server: str
    transport: Literal["streamable_http", "sse", "stdio", "code"] = "streamable_http"
    endpoint: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True
    tools: list[ToolDocument] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
