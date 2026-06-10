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
    ``request.state`` directly from the verified token. They are optional: a token
    minted without them (legacy callers, the env bootstrap admin) is treated as a
    full platform admin for backward compatibility.
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
