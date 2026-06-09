from __future__ import annotations

from enum import IntEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# JSON-RPC 2.0 wire envelope. This module is the protocol layer: it owns only
# the framing (request/response/error) and the JSON-RPC error codes. The
# request *parameters* are protocol-agnostic and live in models/domain.py.

JsonId = str | int | None


class JsonRpcErrorCode(IntEnum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    SERVER_ERROR = -32000
    UNAUTHORIZED = -32001
    FORBIDDEN = -32003
    RATE_LIMITED = -32029
    UPSTREAM_TIMEOUT = -32004


class JsonRpcError(BaseModel):
    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: JsonId = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: JsonId = None
    result: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    error: JsonRpcError | None = None


def make_error_response(
    request_id: JsonId,
    code: JsonRpcErrorCode,
    message: str,
    data: dict[str, Any] | None = None,
) -> JsonRpcResponse:
    return JsonRpcResponse(
        id=request_id,
        error=JsonRpcError(code=int(code), message=message, data=data),
    )
