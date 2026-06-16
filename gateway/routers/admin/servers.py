"""Downstream server registration, inspection, export, and lifecycle."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query, Request, Response, status

from models.admin import ServerPatchRequest, ServerUpsertRequest
from services.code_tools import (
    CODE_TRANSPORT,
    CodeToolValidationError,
    decrypt_raw_code,
    encrypt_raw_code,
    is_encrypted_token,
    lint_code_tool,
)
from services.credential_broker import resolve_auth_scheme
from services.egress_policy import EgressNotAllowed, check_endpoint_allowed
from services.metrics import observe_egress_block
from services.server_exporter import (
    ServerExportError,
    ServerExportNotFound,
    build_server_export,
)
from services.server_guard import EndpointNotAllowed, StdioNotAllowed, enforce_server_policy
from services.tenant_egress import get_tenant_egress_allowlist
from services.tenant_tool_policy import get_tool_policy

from . import _common as c
from ._common import (
    _is_platform_admin,
    _require_tenant_admin,
    _require_tenant_writable,
    _resolve_target_tenant,
    router,
    settings,
)


async def _enforce_max_tools(tenant_id: str, server_name: str, incoming_tool_count: int) -> None:
    """Reject a registration that would push the tenant over its max-tools cap.

    The cap counts the tenant's catalogued tools across *other* servers plus the
    tools this registration contributes, so re-saving an existing server never
    counts its own tools twice. ``0`` means unlimited. Best-effort for
    discovery-based (HTTP) servers whose tools land in the catalog asynchronously;
    authored/code servers (tools known at save time) are enforced exactly.
    """
    cap = (await get_tool_policy(tenant_id))["max_tools"]
    if cap <= 0:
        return
    catalog = c.get_tenant_database(tenant_id)["tool_catalog"]
    existing_other = await catalog.count_documents({"server": {"$ne": server_name}})
    projected = existing_other + max(0, incoming_tool_count)
    if projected > cap:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Registering this server would exceed the tenant's max-tools cap "
                f"({cap}); projected total is {projected}. Raise the cap in the tenant "
                "tool policy or remove tools before saving."
            ),
        )


def _requires_secure_credential_transport(server_doc: dict[str, Any], scheme: str) -> bool:
    if scheme == "none":
        return False
    transport = str(server_doc.get("transport") or "")
    endpoint = server_doc.get("endpoint")
    return (
        transport in {"streamable_http", "sse"}
        and isinstance(endpoint, str)
        and endpoint.strip().lower().startswith("http://")
    )


async def _validate_server_auth(server_doc: dict[str, Any]) -> None:
    # Code-backed tools execute in-process sandbox and do not call downstream transports.
    if str(server_doc.get("transport") or "") == CODE_TRANSPORT:
        return
    scheme = resolve_auth_scheme(server_doc.get("metadata"))
    allowed = {"jwt", "none"}
    if scheme not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Unsupported metadata.auth.scheme '{scheme}'. Use one of: jwt, none. "
                "Third-party downstream credentials (API keys, basic auth, OAuth) are owned "
                "by the downstream service or the tenant, not brokered per-server by the "
                "gateway; use scheme=none and let the downstream present its own credential."
            ),
        )
    if scheme == "jwt" and not settings.downstream_jwt_enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "metadata.auth.scheme=jwt requires DOWNSTREAM_JWT_ENABLED=true. "
                "Use scheme=none if JWT brokering is disabled."
            ),
        )
    if (
        _requires_secure_credential_transport(server_doc, scheme)
        and not settings.downstream_allow_insecure_credentials
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"metadata.auth.scheme={scheme} may not be used with insecure http:// endpoints "
                "unless DOWNSTREAM_ALLOW_INSECURE_CREDENTIALS=true."
            ),
        )


def _validate_server_doc(server_doc: dict[str, Any]) -> None:
    transport = server_doc.get("transport")
    endpoint = server_doc.get("endpoint")
    command = server_doc.get("command")
    if transport in {"streamable_http", "sse"} and not endpoint:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"transport={transport} requires endpoint.",
        )
    if transport == "stdio" and not command:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="transport=stdio requires command.",
        )
    if transport == CODE_TRANSPORT and not (server_doc.get("tools") or []):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="transport=code requires at least one authored function in 'tools'.",
        )


async def _prepare_code_server(doc: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    """Lint and encrypt authored functions before a code server is persisted.

    Each tool's ``raw_code`` is statically linted, then encrypted at rest (unless
    it is already an encrypted token carried over from a prior save). Connection
    fields are cleared since a code tool has no downstream endpoint/command.
    """
    if doc.get("transport") != CODE_TRANSPORT:
        return doc
    tools = doc.get("tools") or []
    prepared: list[dict[str, Any]] = []
    for tool in tools:
        tool = dict(tool)
        tool.setdefault("server", doc["server"])
        raw_code = tool.get("raw_code")
        # Already-encrypted source carried over from a prior save was validated at
        # authoring time; re-linting the ciphertext would always fail. Only newly
        # submitted plaintext is linted and then encrypted.
        if isinstance(raw_code, str) and not is_encrypted_token(raw_code):
            try:
                lint_code_tool(tool)
            except CodeToolValidationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(exc),
                ) from exc
            tool["raw_code"] = await encrypt_raw_code(tenant_id, raw_code)
        # An authored function is not a downstream embedding target.
        tool["embedding"] = []
        prepared.append(tool)
    doc["tools"] = prepared
    # Code servers have no network/process target; keep these unset so the proxy
    # never tries to connect to one. Under Queryable Encryption the encrypted
    # routing fields (env/command/args) cannot store a null, so omit them rather
    # than writing null/empty placeholders that libmongocrypt rejects.
    doc["endpoint"] = None
    doc["cwd"] = None
    for connection_field in ("command", "args", "env"):
        doc.pop(connection_field, None)
    return doc


def _to_server_doc(payload: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    server = payload["server"]
    doc = dict(payload)
    doc["tenant_id"] = tenant_id
    doc["_id"] = server
    doc["args"] = [str(arg) for arg in (doc.get("args") or [])]
    doc["env"] = {str(key): str(value) for key, value in (doc.get("env") or {}).items()}
    return doc


async def _apply_server_policy(doc: dict[str, Any], *, is_platform_admin: bool) -> dict[str, Any]:
    try:
        doc = await enforce_server_policy(
            doc,
            is_platform_admin=is_platform_admin,
            settings=settings,
        )
    except (StdioNotAllowed, EndpointNotAllowed) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # Egress allowlist gate (friendly, fail-fast on save). The connect-time
    # transport is the authoritative gate, but rejecting here gives operators a
    # clear 422 instead of a downstream error at first call.
    transport = str(doc.get("transport") or "")
    endpoint = doc.get("endpoint")
    if transport in {"streamable_http", "sse"} and isinstance(endpoint, str) and endpoint:
        tenant_id = str(doc.get("tenant_id") or settings.default_tenant_id)
        tenant_allowlist = await get_tenant_egress_allowlist(tenant_id, settings=settings)
        try:
            await check_endpoint_allowed(
                endpoint,
                tenant_allowlist=tenant_allowlist,
                settings=settings,
            )
        except EgressNotAllowed as exc:
            observe_egress_block("register")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
    return doc


def _redact_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Strip encrypted source from a tool, leaving a presence flag.

    Authored ``raw_code`` is stored encrypted; never echo the ciphertext (or
    plaintext) in list/collection responses. The single-server fetch decrypts it
    explicitly for the author's editor instead.
    """
    public = {key: value for key, value in tool.items() if key not in {"raw_code", "embedding"}}
    if "raw_code" in tool:
        public["has_raw_code"] = bool(tool.get("raw_code"))
    return public


def _public_server_doc(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "tenant_id": doc.get("tenant_id"),
        "origin": doc.get("origin", "platform"),
        "server": doc.get("server"),
        "transport": doc.get("transport"),
        "endpoint": doc.get("endpoint"),
        "command": doc.get("command"),
        "args": doc.get("args", []),
        "env": doc.get("env", {}),
        "cwd": doc.get("cwd"),
        "enabled": bool(doc.get("enabled", True)),
        "metadata": doc.get("metadata", {}),
        "tools": [_redact_tool(tool) for tool in (doc.get("tools") or [])],
    }


async def _public_server_doc_with_code(doc: dict[str, Any]) -> dict[str, Any]:
    """Like :func:`_public_server_doc` but decrypts ``raw_code`` for the editor.

    Used only by the single-server fetch so an author can load their function
    back into the GUI. Decryption is best-effort: an undecryptable token yields
    an empty body rather than an error.
    """
    public = _public_server_doc(doc)
    if doc.get("transport") != CODE_TRANSPORT:
        return public
    tenant_id = str(doc.get("tenant_id") or settings.default_tenant_id)
    decrypted: list[dict[str, Any]] = []
    for stored, redacted in zip(doc.get("tools") or [], public["tools"], strict=False):
        redacted = dict(redacted)
        redacted["raw_code"] = await decrypt_raw_code(tenant_id, stored.get("raw_code"))
        decrypted.append(redacted)
    public["tools"] = decrypted
    return public


@router.post("/servers")
async def create_or_update_server(request: Request, payload: ServerUpsertRequest) -> dict[str, Any]:
    tenant_id = _resolve_target_tenant(request, payload.tenant_id)
    await _require_tenant_writable(request, tenant_id)
    await c.provision_tenant(tenant_id, wait_for_queryable_indexes=False)
    doc = _to_server_doc(payload.model_dump(), tenant_id)
    _validate_server_doc(doc)
    await _enforce_max_tools(tenant_id, doc["server"], len(doc.get("tools") or []))
    doc = await _apply_server_policy(doc, is_platform_admin=_is_platform_admin(request))
    await _validate_server_auth(doc)
    doc = await _prepare_code_server(doc, tenant_id)
    collection = c.get_tenant_database(tenant_id)["routing_registry"]
    await collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    if doc.get("enabled", True):
        await c.get_proxy_registry().mount_or_update(doc)
    else:
        await c.get_proxy_registry().unmount(doc["server"], tenant_id=tenant_id)
    return _public_server_doc(doc)


@router.get("/servers")
async def list_servers(
    request: Request,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    docs = (
        await c.get_tenant_database(target_tenant)["routing_registry"]
        .find({})
        .to_list(length=10_000)
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
    doc = await c.get_tenant_database(target_tenant)["routing_registry"].find_one(
        {"_id": server_name}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Server not found.")
    return await _public_server_doc_with_code(doc)


@router.get("/servers/{server_name}/export")
async def export_server(
    request: Request,
    server_name: str,
    tenant_id: str | None = Query(default=None),
) -> Response:
    """Download a code server as a runnable, self-contained FastMCP project.

    Bundles every tool on the server plus the transitive closure of sibling
    code tools they call via ``context.tools``/``context.call`` so cross-tool
    composition keeps working in-process. Secrets are never included — only the
    names of ``context.env`` keys are emitted into ``.env.example``.
    """
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    try:
        export = await build_server_export(tenant_id=target_tenant, server_name=server_name)
    except ServerExportNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ServerExportError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return Response(
        content=export.content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "X-Export-Tool-Count": str(export.tool_count),
            "X-Export-Servers": ",".join(export.bundled_servers),
        },
    )


@router.patch("/servers/{server_name}")
async def patch_server(
    request: Request,
    server_name: str,
    payload: ServerPatchRequest,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    requested_tenant = payload.tenant_id or tenant_id
    target_tenant = _resolve_target_tenant(request, requested_tenant)
    await _require_tenant_writable(request, target_tenant)
    collection = c.get_tenant_database(target_tenant)["routing_registry"]
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
    merged["origin"] = existing.get("origin", "platform")

    _validate_server_doc(merged)
    is_platform_admin = _is_platform_admin(request)
    if merged["origin"] == "platform" and not is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform-admin may modify platform-origin servers.",
        )
    await _enforce_max_tools(target_tenant, server_name, len(merged.get("tools") or []))
    merged = await _apply_server_policy(merged, is_platform_admin=is_platform_admin)
    await _validate_server_auth(merged)
    merged = await _prepare_code_server(merged, target_tenant)
    await collection.replace_one({"_id": server_name}, merged, upsert=True)
    if merged.get("enabled", True):
        await c.get_proxy_registry().mount_or_update(merged)
    else:
        await c.get_proxy_registry().unmount(server_name, tenant_id=target_tenant)
    return _public_server_doc(merged)


@router.delete("/servers/{server_name}")
async def delete_server(
    request: Request,
    server_name: str,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    target_tenant = _resolve_target_tenant(request, tenant_id)
    await _require_tenant_writable(request, target_tenant)
    collection = c.get_tenant_database(target_tenant)["routing_registry"]
    existing = await collection.find_one({"_id": server_name})
    if (
        existing
        and str(existing.get("origin", "platform")) == "platform"
        and not _is_platform_admin(request)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform-admin may delete platform-origin servers.",
        )
    result = await collection.delete_many({"_id": server_name})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Server not found.")
    await c.get_proxy_registry().unmount(server_name, tenant_id=target_tenant)
    return {"deleted": True, "tenant_id": target_tenant, "server": server_name}


@router.post("/servers/{server_name}/enable")
async def enable_server(
    request: Request,
    server_name: str,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    return await _set_server_enabled(request, server_name, True, tenant_id)


@router.post("/servers/{server_name}/disable")
async def disable_server(
    request: Request,
    server_name: str,
    tenant_id: str | None = Query(default=None),
) -> dict[str, Any]:
    return await _set_server_enabled(request, server_name, False, tenant_id)


async def _set_server_enabled(
    request: Request,
    server_name: str,
    enabled: bool,
    tenant_id: str | None,
) -> dict[str, Any]:
    """Flip a server's ``enabled`` flag and (un)mount it, origin-aware.

    Tenant-admins may toggle their own (tenant-origin) servers; platform-origin
    servers stay platform-admin only, mirroring ``patch_server``. The toggle is a
    mutation, so it is refused while the tenant is read-only (platform-admin
    bypasses).
    """
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, tenant_id)
    await _require_tenant_writable(request, target_tenant)
    collection = c.get_tenant_database(target_tenant)["routing_registry"]
    existing = await collection.find_one({"_id": server_name})
    if not existing:
        raise HTTPException(status_code=404, detail="Server not found.")
    if str(existing.get("origin", "platform")) == "platform" and not _is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform-admin may modify platform-origin servers.",
        )
    await collection.update_one({"_id": server_name}, {"$set": {"enabled": enabled}})
    existing["enabled"] = enabled
    if enabled:
        await c.get_proxy_registry().mount_or_update(existing)
    else:
        await c.get_proxy_registry().unmount(server_name, tenant_id=target_tenant)
    c.get_telemetry_logger().log_background(
        tenant_id=target_tenant,
        user_id=str(getattr(request.state, "user_id", "admin")),
        method="admin/servers/enable" if enabled else "admin/servers/disable",
        status="server_enabled" if enabled else "server_disabled",
        metadata={"tenant_id": target_tenant, "server": server_name},
    )
    return _public_server_doc(existing)
