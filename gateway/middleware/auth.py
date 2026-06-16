from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from jwt.algorithms import RSAAlgorithm

from config.settings import get_settings
from services.admin_session import ADMIN_SESSION_COOKIE, verify_session
from services.metrics import observe_auth_failure
from services.users import resolve_login_principal

logger = logging.getLogger(__name__)


class JWKSUnavailableError(Exception):
    """The JWKS endpoint could not be reached or returned no usable keys.

    Distinct from a bad token: this is a server-side / IdP problem, so callers
    should map it to a 503 (retryable) rather than a 401 (bad credentials).
    """


class JWKSKeyResolver:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._keys_by_kid: dict[str, Any] = {}
        self._cache_expires_at = 0.0
        # Throttle out-of-band refreshes so a storm of unknown `kid`s (typos or a
        # malicious probe) cannot turn into a request amplification attack on the IdP.
        self._last_miss_refresh_at = 0.0
        # Serialize cache mutation so concurrent unknown-`kid` requests don't race
        # on the shared key map (and collapse into a single IdP refresh).
        self._lock = asyncio.Lock()

    async def resolve_signing_key(self, token: str) -> Any:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")

        keys = await self._load_keys()
        key = self._select_key(keys, kid)
        if key is not None:
            return key

        # The `kid` isn't in the cached set. Rather than waiting for the TTL to lapse
        # (which delays picking up a rotated key for up to jwks_cache_ttl_seconds),
        # refresh immediately — but at most once per jwks_min_refresh_seconds.
        if kid is not None:
            key = await self._refresh_for_missing_kid(kid)
            if key is not None:
                return key

        raise ValueError("Unable to resolve JWT signing key from JWKS.")

    async def _refresh_for_missing_kid(self, kid: str) -> Any | None:
        async with self._lock:
            # A concurrent request may have refreshed while we waited for the lock;
            # reuse its result rather than hammering the IdP again.
            key = self._select_key(self._keys_by_kid, kid)
            if key is not None:
                return key
            if not self._can_refresh_on_miss():
                return None
            self._last_miss_refresh_at = time.monotonic()
            await self._refresh_locked()
            return self._select_key(self._keys_by_kid, kid)

    def _can_refresh_on_miss(self) -> bool:
        min_interval = max(0, self.settings.jwks_min_refresh_seconds)
        return time.monotonic() - self._last_miss_refresh_at >= min_interval

    @staticmethod
    def _select_key(keys: dict[str, Any], kid: str | None) -> Any | None:
        if kid and kid in keys:
            return keys[kid]
        if not kid and len(keys) == 1:
            return next(iter(keys.values()))
        return None

    async def _load_keys(self) -> dict[str, Any]:
        if self._keys_by_kid and time.monotonic() < self._cache_expires_at:
            return self._keys_by_kid

        async with self._lock:
            # Re-check under the lock: a request that blocked here while another
            # refreshed should reuse that fresh result instead of refetching.
            if self._keys_by_kid and time.monotonic() < self._cache_expires_at:
                return self._keys_by_kid
            await self._refresh_locked()
            return self._keys_by_kid

    async def _refresh_locked(self) -> None:
        """Fetch JWKS and replace the key cache. Caller must hold ``self._lock``."""
        jwks = await self._fetch_jwks()
        loaded: dict[str, Any] = {}
        for idx, jwk in enumerate(jwks.get("keys", [])):
            kid = jwk.get("kid") or f"local-key-{idx}"
            loaded[kid] = RSAAlgorithm.from_jwk(json.dumps(jwk))
        if not loaded:
            raise JWKSUnavailableError("No signing keys found in JWKS.")
        self._keys_by_kid = loaded
        self._cache_expires_at = time.monotonic() + max(1, self.settings.jwks_cache_ttl_seconds)

    async def _fetch_jwks(self) -> dict[str, Any]:
        if self.settings.jwks_local_path:
            try:
                return json.loads(Path(self.settings.jwks_local_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise JWKSUnavailableError(f"Local JWKS unreadable: {exc}") from exc
        if self.settings.jwks_uri:
            timeout = httpx.Timeout(self.settings.http_timeout_seconds)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.get(self.settings.jwks_uri)
                    response.raise_for_status()
                    return response.json()
            except httpx.HTTPError as exc:
                raise JWKSUnavailableError(f"JWKS fetch failed: {exc}") from exc
        raise ValueError("JWKS auth mode requires jwks_uri or jwks_local_path.")


_resolver: JWKSKeyResolver | None = None


def get_jwks_resolver() -> JWKSKeyResolver:
    global _resolver
    if _resolver is None:
        _resolver = JWKSKeyResolver()
    return _resolver


class AuthMiddleware:
    def __init__(self, app):
        self.app = app
        self.settings = get_settings()

    async def __call__(
        self,
        scope,
        receive,
        send,
    ):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request = Request(scope, receive=receive)
        request.state.tenant_id = self.settings.default_tenant_id
        request.state.user_id = "anonymous"
        request.state.roles = []
        request.state.scopes = []
        request.state.is_admin_principal = False
        request.state.admin_auth_via_cookie = False
        request.state.authenticated_via_basic = False

        admin_claims, via_cookie = self._resolve_admin_session(request)
        if admin_claims is not None:
            session_roles = self._session_roles(admin_claims)
            request.state.user_id = str(admin_claims.get("sub", "admin"))
            request.state.tenant_id = str(
                admin_claims.get("tenant_id") or self.settings.default_tenant_id
            )
            request.state.roles = session_roles
            request.state.scopes = []
            # Only sessions that actually carry an admin-tier role count as admin
            # principals. A plain "user" session authenticates but cannot reach the
            # admin control plane (RbacMiddleware still gates /admin and /ui).
            request.state.is_admin_principal = self._has_admin_role(session_roles)
            request.state.admin_auth_via_cookie = via_cookie

        # Optional HTTP Basic on the MCP surface: let an MCP client present
        # username/password directly instead of exchanging it at /auth/token.
        if (
            not request.state.is_admin_principal
            and self.settings.mcp_basic_auth_enabled
            and self._is_mcp_path(request.url.path)
        ):
            basic = self._basic_credentials(request)
            if basic is not None:
                principal = await resolve_login_principal(basic[0], basic[1])
                if principal is None:
                    response = JSONResponse(
                        status_code=401,
                        content={"detail": "Invalid username or password."},
                        headers={"WWW-Authenticate": 'Basic realm="mcp"'},
                    )
                    return await response(scope, receive, send)
                roles = self._normalize_roles(principal.get("roles"))
                request.state.user_id = str(principal.get("email") or "unknown-user")
                request.state.tenant_id = str(
                    principal.get("tenant_id") or self.settings.default_tenant_id
                )
                request.state.roles = roles
                request.state.scopes = []
                request.state.is_admin_principal = self._has_admin_role(roles)
                request.state.authenticated_via_basic = True

        if not request.state.is_admin_principal and not request.state.authenticated_via_basic:
            path = request.url.path
            if (
                self._is_observability_path(path)
                or self._is_public_path(path)
                or self._is_auth_public_path(path)
                or self._is_ui_path(path)
            ):
                await self.app(scope, request.receive, send)
                return

            token = self._bearer_token(request)
            if token is None:
                response = JSONResponse(
                    status_code=401,
                    content={"detail": "Missing bearer token."},
                    headers=self._challenge_headers(request),
                )
                return await response(scope, receive, send)

            try:
                claims = await self._decode_claims(token)
                request.state.tenant_id = claims.get("tenant_id", self.settings.default_tenant_id)
                request.state.user_id = claims.get("sub", "unknown-user")
                request.state.roles = self._normalize_roles(claims.get("roles"))
                claim_scopes = claims.get("groups") or claims.get("scopes") or []
                request.state.scopes = self._normalize_scopes(claim_scopes)
                role_set = set(request.state.roles)
                if "admin" in role_set or self.settings.platform_admin_role in role_set:
                    request.state.is_admin_principal = True
            except Exception as exc:
                response = self._auth_failure_response(exc, request)
                return await response(scope, receive, send)

        await self.app(scope, request.receive, send)

    @staticmethod
    def _bearer_token(request: Request) -> str | None:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        return auth_header.replace("Bearer ", "", 1)

    @staticmethod
    def _is_mcp_path(path: str) -> bool:
        return (
            path == "/rpc" or path.startswith("/rpc/") or path == "/mcp" or path.startswith("/mcp/")
        )

    @staticmethod
    def _basic_credentials(request: Request) -> tuple[str, str] | None:
        header = request.headers.get("authorization", "")
        if header[:6].lower() != "basic ":
            return None
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
        if ":" not in decoded:
            return None
        username, password = decoded.split(":", 1)
        return username, password

    def _oauth_metadata_enabled(self) -> bool:
        return self.settings.oauth_metadata_enabled or self.settings.auth_mode == "jwks"

    def _challenge_headers(self, request: Request) -> dict[str, str]:
        """RFC 9728 ``WWW-Authenticate`` hint pointing at resource metadata.

        Only emitted when OAuth discovery is advertised, so non-OAuth deployments
        keep returning a bare 401.
        """
        if not self._oauth_metadata_enabled():
            return {}
        base = str(request.base_url).rstrip("/")
        resource_metadata = f"{base}/.well-known/oauth-protected-resource"
        return {"WWW-Authenticate": f'Bearer resource_metadata="{resource_metadata}"'}

    def _is_auth_public_path(self, path: str) -> bool:
        """Inbound auth endpoints must be reachable without an existing token."""
        return path in {"/auth/token", "/.well-known/oauth-protected-resource"}

    @staticmethod
    def _session_roles(claims: dict[str, Any]) -> list[str]:
        """Read roles straight from the verified session claim — fail-safe.

        A session that carries no ``roles`` claim gets **no** privileges (not an
        implicit admin). Every token minted by this gateway embeds explicit roles
        (``resolve_login_principal`` always supplies them, including the env
        bootstrap admin), so a missing/empty claim means "authenticated identity,
        no authority" rather than silently escalating to platform-admin.
        """
        raw = claims.get("roles")
        if isinstance(raw, list):
            return [role for role in raw if isinstance(role, str)]
        return []

    def _has_admin_role(self, roles: list[str]) -> bool:
        role_set = set(roles)
        return self.settings.platform_admin_role in role_set or "admin" in role_set

    def _resolve_admin_session(self, request: Request) -> tuple[dict[str, Any] | None, bool]:
        cookie_token = request.cookies.get(ADMIN_SESSION_COOKIE)
        if cookie_token:
            claims = verify_session(cookie_token)
            if claims is not None:
                return claims, True

        bearer_token = self._bearer_token(request)
        if bearer_token:
            claims = verify_session(bearer_token)
            if claims is not None:
                return claims, False

        return None, False

    @staticmethod
    def _is_observability_path(path: str) -> bool:
        """Liveness/readiness probes and metrics must be reachable in every auth mode.

        These endpoints expose only operational status and aggregate counters (no
        tenant data), and infra-level probes/scrapers cannot present a bearer token.
        They stay reachable while the rest of the surface requires authentication;
        restrict them at the network layer (see NETWORK-SECURITY.md).
        """
        return path == "/metrics" or path == "/health" or path.startswith("/health/")

    def _is_public_path(self, path: str) -> bool:
        if not self.settings.admin_ui_enabled:
            return False
        ui_path = self.settings.admin_ui_path
        return (
            path in {f"{ui_path}/login", f"{ui_path}/logout"}
            or path == "/static"
            or path.startswith("/static/")
        )

    def _is_ui_path(self, path: str) -> bool:
        if not self.settings.admin_ui_enabled:
            return False
        ui_path = self.settings.admin_ui_path
        return path == ui_path or path.startswith(f"{ui_path}/")

    def _auth_failure_response(self, exc: Exception, request: Request) -> JSONResponse:
        """Classify an auth failure for observability, then return a response.

        The client-facing body stays deliberately opaque (no leaking of *why* a
        token was rejected), but we record the cause as a metric label and a
        structured log so operators can tell "users sent bad tokens" apart from
        "our IdP is down" — the latter being a server-side 503, not a 401.
        """
        reason = self._classify_auth_failure(exc)
        observe_auth_failure(reason)
        if reason == "jwks_unavailable":
            # The credential may be fine; we just couldn't verify it. Signal a
            # retryable server-side fault rather than blaming the caller.
            logger.error("JWKS unavailable, cannot verify bearer token: %s", exc)
            return JSONResponse(
                status_code=503,
                content={"detail": "Authentication temporarily unavailable."},
            )
        logger.info("Rejected bearer token (%s).", reason)
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid bearer token."},
            headers=self._challenge_headers(request),
        )

    @staticmethod
    def _classify_auth_failure(exc: Exception) -> str:
        if isinstance(exc, JWKSUnavailableError):
            return "jwks_unavailable"
        if isinstance(exc, jwt.ExpiredSignatureError):
            return "expired"
        if isinstance(exc, jwt.InvalidSignatureError):
            return "bad_signature"
        if isinstance(exc, jwt.InvalidAudienceError):
            return "bad_audience"
        if isinstance(exc, jwt.InvalidIssuerError):
            return "bad_issuer"
        if isinstance(exc, jwt.DecodeError):
            return "malformed"
        if isinstance(exc, jwt.InvalidTokenError):
            return "invalid_token"
        # ValueError covers "kid not resolvable" from the resolver; everything
        # else is an unexpected fault worth a distinct bucket.
        if isinstance(exc, ValueError):
            return "unresolved_key"
        return "unexpected"

    async def _decode_claims(self, token: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.settings.jwt_audience:
            kwargs["audience"] = self.settings.jwt_audience
        if self.settings.jwt_issuer:
            kwargs["issuer"] = self.settings.jwt_issuer

        if self.settings.auth_mode == "hs256":
            return jwt.decode(
                token,
                self.settings.jwt_secret,
                algorithms=[self.settings.jwt_algorithm],
                **kwargs,
            )

        key = await get_jwks_resolver().resolve_signing_key(token)
        return jwt.decode(token, key, algorithms=["RS256"], **kwargs)

    @staticmethod
    def _normalize_roles(raw_roles: Any) -> list[str]:
        if isinstance(raw_roles, list):
            return [role for role in raw_roles if isinstance(role, str)]
        if isinstance(raw_roles, str):
            return [role for role in raw_roles.split() if role]
        return []

    @staticmethod
    def _normalize_scopes(raw_scopes: Any) -> list[str]:
        if isinstance(raw_scopes, list):
            return [scope for scope in raw_scopes if isinstance(scope, str)]
        if isinstance(raw_scopes, str):
            return [scope for scope in raw_scopes.split() if scope]
        return []
