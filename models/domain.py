from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# Protocol-agnostic request parameters. These describe *what* a caller wants
# (call a tool, search the catalog, list tools) independent of the wire format
# used to express it. The JSON-RPC envelope lives in models/jsonrpc.py; a future
# REST surface could reuse these same models without dragging JSON-RPC along.


class ToolCallParams(BaseModel):
    server: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSearchParams(BaseModel):
    query: str
    limit: int = 10
    vector_weight: float | None = None
    text_weight: float | None = None
    scopes: list[str] | None = None
    # Retrieval strategy: "hybrid" ($rankFusion), "vector" (semantic-only),
    # or "text" (lexical-only). Lets a caller compare the arms side by side.
    mode: Literal["hybrid", "vector", "text"] = "hybrid"


class ToolListParams(BaseModel):
    # When query is present (here or via the X-MCP-Query header), tools/list
    # returns a curated, ranked shortlist instead of the full catalog.
    query: str | None = None
    limit: int | None = None
    scopes: list[str] | None = None
    cursor: str | None = None
    client_catalog_version: int | None = None
