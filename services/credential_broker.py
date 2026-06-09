"""Just-in-time downstream credential brokering.

The gateway never hands a long-lived secret to a downstream MCP server. Instead,
for each ``(tenant, server)`` it mints a short-lived RS256 JWT — a service-to-
service *workload identity* asserting "the gateway, acting for this tenant, is
calling this server" — and injects it as a transport credential (an
``Authorization: Bearer`` header for HTTP/SSE, an env var for stdio).

Design notes:
- **Workload identity, not the end-user's token.** End-user authorization is
  enforced upstream in the gateway (``AuthorizationService``) *before* a
  downstream call is made. The downstream credential therefore carries the
  tenant identity, not volatile per-caller claims. This is deliberate: the proxy
  keeps a single warm client per ``(tenant, server)`` shared across every caller
  (see ``services/proxy_registry.py``), so a token shared by that pool cannot
  faithfully represent any one caller. Caller identity stays in gateway audit
  logs and traces.
- **Short TTL + cache.** Tokens are cached per ``(tenant, server)`` and reused
  until they enter the refresh-skew window before expiry, at which point the next
  request mints a fresh one. The proxy pool checks the same ``near_expiry`` skew
  to decide when to reconnect, so the two layers agree on rotation timing.
- **Secrets never logged.** The signed token is returned only inside the
  ``MintedCredential`` and is never written to logs, telemetry, or spans.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt

from config.settings import Settings, get_settings


@dataclass(frozen=True)
class CallerIdentity:
    """End-user identity resolved by the gateway, used for audit/observability."""

    user_id: str
    scopes: list[str]
    roles: list[str]


@dataclass(frozen=True)
class MintedCredential:
    headers: dict[str, str]
    env: dict[str, str]
    expires_at: datetime
    token_id: str


class JwtCredentialBroker:
    """Mint and cache short-lived downstream workload-identity credentials."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._cache: dict[tuple[str, str], MintedCredential] = {}
        self._lock = asyncio.Lock()

    async def mint(
        self,
        server_name: str,
        *,
        tenant_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> MintedCredential:
        """Return a valid credential for ``(tenant_id, server_name)``, minting if needed."""
        if not self.settings.downstream_jwt_enabled:
            raise ValueError("Downstream JWT brokering is disabled.")
        key = (tenant_id, server_name)
        now = datetime.now(UTC)
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and not self.near_expiry(cached, now):
                return cached
            minted = self._mint_fresh(
                server_name=server_name,
                tenant_id=tenant_id,
                metadata=metadata,
                now=now,
            )
            self._cache[key] = minted
            return minted

    def near_expiry(self, credential: MintedCredential, now: datetime | None = None) -> bool:
        """True when ``credential`` is within the refresh-skew window of expiry."""
        now = now or datetime.now(UTC)
        skew = timedelta(seconds=max(0, self.settings.downstream_token_refresh_skew_seconds))
        return credential.expires_at <= now + skew

    def clear_cache(self) -> None:
        self._cache.clear()

    def _mint_fresh(
        self,
        *,
        server_name: str,
        tenant_id: str,
        metadata: dict[str, Any] | None,
        now: datetime,
    ) -> MintedCredential:
        expires_at = now + timedelta(seconds=max(1, self.settings.downstream_token_ttl_seconds))
        token_id = str(uuid4())
        payload = {
            "iss": self.settings.downstream_jwt_issuer,
            "aud": self._audience_for(server_name, metadata),
            "sub": f"tenant:{tenant_id}:gateway",
            "tenant_id": tenant_id,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": token_id,
        }
        jwt_headers = (
            {"kid": self.settings.downstream_jwt_kid} if self.settings.downstream_jwt_kid else {}
        )
        token = jwt.encode(
            payload,
            self._signing_key(),
            algorithm=self.settings.downstream_jwt_algorithm,
            headers=jwt_headers,
        )
        auth_header = self.settings.downstream_auth_header.strip() or "Authorization"
        token_env_var = self.settings.downstream_token_env_var.strip() or "MCP_DOWNSTREAM_TOKEN"
        return MintedCredential(
            headers={auth_header: f"Bearer {token}"},
            env={token_env_var: token},
            expires_at=expires_at,
            token_id=token_id,
        )

    def _signing_key(self) -> str:
        key = self.settings.downstream_jwt_private_key
        if key:
            return key
        raise ValueError(
            "Missing downstream JWT private key; configure DOWNSTREAM_JWT_PRIVATE_KEY_FILE or "
            "DOWNSTREAM_JWT_PRIVATE_KEY."
        )

    @staticmethod
    def _audience_for(server_name: str, metadata: dict[str, Any] | None) -> str:
        auth_meta = (metadata or {}).get("auth")
        if isinstance(auth_meta, dict):
            audience = auth_meta.get("audience")
            if isinstance(audience, str) and audience.strip():
                return audience.strip()
        return server_name


_credential_broker: JwtCredentialBroker | None = None


def get_credential_broker() -> JwtCredentialBroker:
    global _credential_broker
    if _credential_broker is None:
        _credential_broker = JwtCredentialBroker()
    return _credential_broker
