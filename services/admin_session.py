from __future__ import annotations

import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from config.settings import get_settings

ADMIN_SESSION_COOKIE = "admin_session"
ADMIN_CSRF_COOKIE = "csrf_token"


def verify_credentials(email: str, password: str) -> bool:
    settings = get_settings()
    expected_email = (settings.admin_email or "").strip().lower()
    expected_password = settings.admin_password
    if not expected_email or not expected_password:
        return False
    provided_email = email.strip().lower()
    return hmac.compare_digest(provided_email, expected_email) and hmac.compare_digest(
        password, expected_password
    )


def mint_session(
    email: str,
    *,
    tenant_id: str | None = None,
    roles: list[str] | None = None,
) -> str:
    """Issue a signed admin-session token.

    ``tenant_id`` and ``roles`` are embedded so the auth middleware can hydrate
    ``request.state`` directly from the verified token. Callers should always pass
    the principal's real roles: the middleware grants **no** authority to a session
    that carries no roles claim (it is authenticated but unprivileged), so an
    empty/omitted ``roles`` is fail-safe rather than an implicit admin.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": email,
        "kind": "admin_session",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.admin_session_ttl_seconds)).timestamp()),
    }
    if tenant_id:
        payload["tenant_id"] = tenant_id
    if roles:
        payload["roles"] = [role for role in roles if isinstance(role, str)]
    return jwt.encode(
        payload, settings.admin_session_secret or settings.jwt_secret, algorithm="HS256"
    )


def mint_bearer_jwt(
    email: str,
    *,
    tenant_id: str,
    roles: list[str] | None = None,
    scopes: list[str] | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Issue a *real* bearer JWT (not an admin-session token).

    Unlike :func:`mint_session`, this signs with ``jwt_secret`` and omits the
    ``kind: admin_session`` marker so the token travels the middleware's bearer
    path (``AuthMiddleware._decode_claims``) rather than the admin-session short
    circuit. That path is the only one that hydrates ``request.state.scopes``, so
    fine-grained scopes survive on the token — which is exactly what a scoped
    MCP client (e.g. a demo account) needs.

    ``iss`` / ``aud`` are stamped only when configured, because the same decode
    path enforces them whenever ``jwt_issuer`` / ``jwt_audience`` are set; adding
    them unconditionally would make a token the gateway then rejects.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    ttl = ttl_seconds if ttl_seconds is not None else settings.admin_session_ttl_seconds
    normalized_scopes = [scope for scope in (scopes or []) if isinstance(scope, str)]
    payload: dict[str, Any] = {
        "sub": email,
        "tenant_id": tenant_id,
        "roles": [role for role in (roles or []) if isinstance(role, str)],
        # The middleware reads ``groups`` first, then ``scopes``; set both so the
        # token is robust regardless of which claim a downstream check consults.
        "groups": normalized_scopes,
        "scopes": normalized_scopes,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }
    if settings.jwt_issuer:
        payload["iss"] = settings.jwt_issuer
    if settings.jwt_audience:
        payload["aud"] = settings.jwt_audience
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_session(token: str) -> dict[str, Any] | None:
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.admin_session_secret or settings.jwt_secret,
            algorithms=["HS256"],
        )
    except jwt.InvalidTokenError:
        return None
    if claims.get("kind") != "admin_session":
        return None
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        return None
    return claims


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)
