"""Just-in-time downstream credential brokering.

The gateway brokers a transport credential per ``(tenant, server)`` according to
``metadata.auth.scheme``:

- ``jwt`` (default): short-lived RS256 workload identity minted by the gateway
- ``none``: no injected transport credential (the downstream server, or the
  tenant, owns its own authentication)

Design notes:
- **Workload identity, not the end-user's token.** End-user authorization is
  enforced upstream in the gateway (``AuthorizationService``) *before* a
  downstream call is made. The downstream credential therefore carries the
  tenant identity, not volatile per-caller claims. This is deliberate: the proxy
  keeps a single warm client per ``(tenant, server)`` shared across every caller
  (see ``services/proxy_registry.py``), so a token shared by that pool cannot
  faithfully represent any one caller. Caller identity stays in gateway audit
  logs and traces.
- **Third-party downstream auth stays out of the gateway.** Anything beyond a
  workload identity (vendor API keys, basic auth, OAuth client credentials) is
  intentionally *not* brokered here. Such credentials belong to the downstream
  service or the tenant, not to the gateway's per-server config. Use
  ``scheme=none`` and let the downstream/tenant present its own credential.
- **TTL + cache contract.** Credentials are cached per ``(tenant, server)`` and
  reused until they enter the refresh-skew window before expiry. JWT uses real
  TTL/rotation; ``none`` uses a long-lived expiry so it stays warm until an
  explicit invalidation (for example, a server config change).
- **Secrets never logged.** The signed token is returned only inside the
  ``MintedCredential`` and is never written to logs, telemetry, or spans.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
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


def _auth_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    auth_meta = (metadata or {}).get("auth")
    return auth_meta if isinstance(auth_meta, dict) else {}


def resolve_auth_scheme(metadata: dict[str, Any] | None) -> str:
    """Resolve ``metadata.auth.scheme`` to a supported downstream scheme name.

    Defaults to ``jwt`` (the gateway's workload identity) when unset. Only
    ``jwt`` and ``none`` are supported; any other value resolves to its
    normalized form and is rejected at mint time.
    """
    raw = _auth_metadata(metadata).get("scheme")
    if not isinstance(raw, str) or not raw.strip():
        return "jwt"
    return raw.strip().lower().replace("-", "_")


class DownstreamAuthStrategy(Protocol):
    async def build(
        self,
        *,
        server_name: str,
        tenant_id: str,
        metadata: dict[str, Any] | None,
        now: datetime,
    ) -> MintedCredential: ...


class WorkloadJwtStrategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def build(
        self,
        *,
        server_name: str,
        tenant_id: str,
        metadata: dict[str, Any] | None,
        now: datetime,
    ) -> MintedCredential:
        if not self.settings.downstream_jwt_enabled:
            raise ValueError("Downstream JWT brokering is disabled.")
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
        auth_meta = _auth_metadata(metadata)
        audience = auth_meta.get("audience")
        if isinstance(audience, str) and audience.strip():
            return audience.strip()
        return server_name


class NoneAuthStrategy:
    @staticmethod
    def _static_expiry(now: datetime) -> datetime:
        return now + timedelta(days=365 * 100)

    async def build(
        self,
        *,
        server_name: str,
        tenant_id: str,
        metadata: dict[str, Any] | None,
        now: datetime,
    ) -> MintedCredential:
        del server_name, tenant_id, metadata
        return MintedCredential(
            headers={},
            env={},
            expires_at=self._static_expiry(now),
            token_id=str(uuid4()),
        )


class JwtCredentialBroker:
    """Mint and cache short-lived downstream workload-identity credentials."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._cache: dict[tuple[str, str], MintedCredential] = {}
        self._lock = asyncio.Lock()
        self._strategies: dict[str, DownstreamAuthStrategy] = {
            "jwt": WorkloadJwtStrategy(self.settings),
            "none": NoneAuthStrategy(),
        }

    async def mint(
        self,
        server_name: str,
        *,
        tenant_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> MintedCredential:
        """Return a valid credential for ``(tenant_id, server_name)``, minting if needed."""
        key = (tenant_id, server_name)
        now = datetime.now(UTC)
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and not self.near_expiry(cached, now):
                return cached
            minted = await self._mint_fresh(
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

    async def invalidate(self, server_name: str, *, tenant_id: str | None = None) -> None:
        async with self._lock:
            if tenant_id is not None:
                self._cache.pop((tenant_id, server_name), None)
                return
            stale = [key for key in self._cache if key[1] == server_name]
            for key in stale:
                self._cache.pop(key, None)

    async def _mint_fresh(
        self,
        *,
        server_name: str,
        tenant_id: str,
        metadata: dict[str, Any] | None,
        now: datetime,
    ) -> MintedCredential:
        scheme = resolve_auth_scheme(metadata)
        strategy = self._strategies.get(scheme)
        if strategy is None:
            raise ValueError(
                f"Unsupported downstream auth scheme '{scheme}'. Supported: jwt, none."
            )
        return await strategy.build(
            server_name=server_name,
            tenant_id=tenant_id,
            metadata=metadata,
            now=now,
        )


_credential_broker: JwtCredentialBroker | None = None


def get_credential_broker() -> JwtCredentialBroker:
    global _credential_broker
    if _credential_broker is None:
        _credential_broker = JwtCredentialBroker()
    return _credential_broker
