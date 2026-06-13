"""Inbound authentication endpoints for the gateway's own MCP surface.

These let an MCP client authenticate to the gateway (the "virtual" MCP at
``/rpc`` and ``/mcp``) with a username/password, and let spec-compliant clients
discover the OAuth issuer when the gateway runs as an OAuth2/OIDC resource
server (``auth_mode=jwks``).

- ``POST /auth/token`` - OAuth2 Resource Owner Password Credentials grant.
  Exchanges username + password for a short-lived bearer (the same signed
  session token the admin UI issues), which ``AuthMiddleware`` already accepts
  on ``/rpc`` and ``/mcp`` in every ``auth_mode``.
- ``GET /.well-known/oauth-protected-resource`` - RFC 9728 Protected Resource
  Metadata describing the configured authorization server. The gateway does not
  itself implement an OAuth authorization server: full OAuth is "bring your own
  IdP" via ``auth_mode=jwks``; this endpoint only advertises it for discovery.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from config.settings import get_settings
from services.admin_session import mint_session
from services.users import resolve_login_principal

router = APIRouter(tags=["auth"])

WELL_KNOWN_PROTECTED_RESOURCE = "/.well-known/oauth-protected-resource"


def oauth_metadata_enabled() -> bool:
    settings = get_settings()
    return settings.oauth_metadata_enabled or settings.auth_mode == "jwks"


async def _read_credentials(request: Request) -> tuple[str, str, str]:
    """Return ``(grant_type, username, password)`` from a form or JSON body."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            payload = await request.json()
        except Exception:
            return "password", "", ""
        if not isinstance(payload, dict):
            return "password", "", ""
        username = str(payload.get("username") or payload.get("email") or "")
        password = str(payload.get("password") or "")
        grant_type = str(payload.get("grant_type") or "password")
        return grant_type, username, password

    body = (await request.body()).decode("utf-8", "replace")
    parsed = parse_qs(body, keep_blank_values=True)
    username = (parsed.get("username") or parsed.get("email") or [""])[0]
    password = parsed.get("password", [""])[0]
    grant_type = parsed.get("grant_type", ["password"])[0]
    return grant_type, username, password


@router.post("/auth/token", name="auth_token")
async def issue_token(request: Request) -> JSONResponse:
    settings = get_settings()
    grant_type, username, password = await _read_credentials(request)
    if grant_type and grant_type != "password":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "unsupported_grant_type",
                "error_description": "Only grant_type=password is supported.",
            },
        )
    if not username or not password:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "invalid_request",
                "error_description": "username and password are required.",
            },
        )
    principal = await resolve_login_principal(username, password)
    if principal is None:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "error": "invalid_grant",
                "error_description": "Invalid username or password.",
            },
        )
    token = mint_session(
        principal["email"],
        tenant_id=principal["tenant_id"],
        roles=principal["roles"],
    )
    return JSONResponse(
        content={
            "access_token": token,
            "token_type": "bearer",
            "expires_in": settings.admin_session_ttl_seconds,
        }
    )


@router.get(WELL_KNOWN_PROTECTED_RESOURCE, name="oauth_protected_resource")
async def protected_resource_metadata(request: Request) -> JSONResponse:
    settings = get_settings()
    if not oauth_metadata_enabled():
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "OAuth metadata is not enabled."},
        )
    metadata: dict[str, Any] = {"resource": str(request.base_url).rstrip("/")}
    if settings.jwt_issuer:
        metadata["authorization_servers"] = [settings.jwt_issuer]
    if settings.jwks_uri:
        metadata["jwks_uri"] = settings.jwks_uri
    if settings.jwt_audience:
        metadata["audience"] = settings.jwt_audience
    metadata["bearer_methods_supported"] = ["header"]
    return JSONResponse(content=metadata)
