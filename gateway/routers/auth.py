"""Inbound authentication endpoints for the gateway's own MCP surface.

These let an MCP client authenticate to the gateway (the "virtual" MCP at
``/rpc`` and ``/mcp``) with a username/password, and let spec-compliant clients
discover the OAuth issuer when the gateway runs as an OAuth2/OIDC resource
server (``auth_mode=jwks``).

- ``POST /auth/token`` - OAuth2 Resource Owner Password Credentials grant.
  Exchanges username + password for a short-lived bearer. The token shape is
  ``auth_mode`` aware (mirroring the admin console's "Generate token"):

  * ``hs256`` -- a real *scoped* data-plane bearer signed with ``jwt_secret``.
    ``AuthMiddleware`` accepts it on ``/rpc`` and ``/mcp`` for **any** role
    (not just console admins) and hydrates ``request.state.scopes``, so the
    documented password-grant -> ``/rpc`` flow works for tool users too.
  * ``jwks`` -- the gateway cannot forge a token its IdP-backed verifier
    trusts, so it falls back to a roles-only admin-session token (accepted on
    ``/rpc`` + ``/mcp`` for console principals). Issue scoped tokens from your
    IdP in this mode.
- ``GET /.well-known/oauth-protected-resource`` - RFC 9728 Protected Resource
  Metadata describing the configured authorization server. The gateway does not
  itself implement an OAuth authorization server: full OAuth is "bring your own
  IdP" via ``auth_mode=jwks``; this endpoint only advertises it for discovery.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from config.settings import get_settings
from models.admin import SelfRegisterResponse, UserResponse
from services import registration as registration_service
from services.admin_session import mint_bearer_jwt, mint_session
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
        except json.JSONDecodeError:
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
    roles = list(principal.get("roles") or [])
    scopes = list(principal.get("scopes") or [])
    expires_in = settings.admin_session_ttl_seconds
    if settings.auth_mode == "jwks":
        # The gateway cannot mint a token its IdP-backed verifier would trust, so
        # fall back to a roles-only admin-session token. It authenticates on /rpc +
        # /mcp for console principals; scoped data-plane tokens come from the IdP.
        token = mint_session(
            principal["email"],
            tenant_id=principal["tenant_id"],
            roles=roles,
        )
    else:  # hs256 -- a real, scoped data-plane bearer (verified against jwt_secret)
        # so the password grant produces a credential that clears the /rpc + /mcp
        # gate and per-call authorization for ANY role, not just console admins.
        token = mint_bearer_jwt(
            principal["email"],
            tenant_id=principal["tenant_id"],
            roles=roles,
            scopes=scopes,
            ttl_seconds=expires_in,
        )
    return JSONResponse(
        content={
            "access_token": token,
            "token_type": "bearer",
            "expires_in": expires_in,
        }
    )


async def _read_registration(request: Request) -> tuple[str, str]:
    """Return ``(email, password)`` from a JSON or form body."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return "", ""
        if not isinstance(payload, dict):
            return "", ""
        email = str(payload.get("email") or payload.get("username") or "")
        password = str(payload.get("password") or "")
        return email, password
    body = (await request.body()).decode("utf-8", "replace")
    parsed = parse_qs(body, keep_blank_values=True)
    email = (parsed.get("email") or parsed.get("username") or [""])[0]
    password = parsed.get("password", [""])[0]
    return email, password


@router.post("/auth/register", name="auth_register")
async def register(request: Request) -> JSONResponse:
    """Public self-service sign-up (open beta).

    Creates an instantly-active, tightly-capped ``unconfirmed`` tenant-admin in
    its own freshly-provisioned tenant and returns a ready-to-use bearer. Disabled
    by default; returns 404 unless ``SELF_REGISTRATION_ENABLED=true`` so the
    feature stays dark (and unprobeable) when off. All privilege-bearing fields are
    pinned server-side in :mod:`services.registration`.
    """
    settings = get_settings()
    if not settings.self_registration_enabled:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "Not found."},
        )
    email, password = await _read_registration(request)
    client_ip = request.client.host if request.client else "unknown"
    try:
        result = await registration_service.register_self_service_user(
            email=email,
            password=password,
            client_ip=client_ip,
            settings=settings,
        )
    except registration_service.RegistrationDisabled:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not found."})
    except registration_service.RegistrationValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(exc)}
        )
    except registration_service.RegistrationThrottled as exc:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": str(exc)},
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    except registration_service.BetaFull as exc:
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)})
    except registration_service.users_service.UserAlreadyExists as exc:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})
    response = SelfRegisterResponse(
        user=UserResponse(**result.user),
        tenant_id=result.tenant_id,
        confirmation=result.confirmation,
        auth_mode=result.auth_mode,
        token=result.token,
        token_type=result.token_type,
        expires_in=result.expires_in,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=response.model_dump(mode="json"),
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
