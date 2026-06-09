"""Tests for the JWKS key resolver and the auth middleware's JWKS/HS256 decode
paths, plus role/scope normalization helpers.
"""

from __future__ import annotations

import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from gateway.middleware.auth import (
    AuthMiddleware,
    JWKSKeyResolver,
    JWKSUnavailableError,
)

# --------------------------------------------------------------------------
# Normalization helpers (pure)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["a", "b"], ["a", "b"]),
        ("a b", ["a", "b"]),
        (None, []),
        ([1, "ok"], ["ok"]),  # non-strings filtered
    ],
)
def test_normalize_roles(raw, expected):
    assert AuthMiddleware._normalize_roles(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["x", "y"], ["x", "y"]),
        ("x y", ["x", "y"]),
        (None, []),
    ],
)
def test_normalize_scopes(raw, expected):
    assert AuthMiddleware._normalize_scopes(raw) == expected


# --------------------------------------------------------------------------
# JWKS resolver, local-file mode
# --------------------------------------------------------------------------


def _rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key


def _public_jwk(private_key, kid: str) -> dict:
    public_numbers = private_key.public_key().public_numbers()

    def _b64(n: int) -> str:
        import base64

        length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64(public_numbers.n),
        "e": _b64(public_numbers.e),
    }


@pytest.fixture
def jwks_setup(tmp_path, monkeypatch):
    key = _rsa_keypair()
    jwk = _public_jwk(key, kid="test-key")
    jwks_file = tmp_path / "jwks.json"
    jwks_file.write_text(json.dumps({"keys": [jwk]}))

    monkeypatch.setenv("AUTH_MODE", "jwks")
    monkeypatch.setenv("JWKS_LOCAL_PATH", str(jwks_file))
    monkeypatch.setenv("JWT_ISSUER", "")
    monkeypatch.setenv("JWT_AUDIENCE", "")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield key
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_jwks_resolver_loads_local_key_and_caches(jwks_setup):
    resolver = JWKSKeyResolver()
    token = jwt.encode({"sub": "u1"}, jwks_setup, algorithm="RS256", headers={"kid": "test-key"})
    signing_key = await resolver.resolve_signing_key(token)
    assert signing_key is not None
    # Cache populated -> a second resolve reuses it.
    assert resolver._keys_by_kid


@pytest.mark.asyncio
async def test_jwks_resolver_rejects_unknown_kid(jwks_setup):
    resolver = JWKSKeyResolver()
    token = jwt.encode({"sub": "u1"}, jwks_setup, algorithm="RS256", headers={"kid": "wrong-kid"})
    with pytest.raises(ValueError, match="Unable to resolve"):
        await resolver.resolve_signing_key(token)


@pytest.mark.asyncio
async def test_jwks_resolver_refreshes_on_unknown_kid_after_rotation(tmp_path, monkeypatch):
    """A rotated-in key must be picked up immediately on the first token that uses
    it, rather than waiting for the cache TTL to expire."""
    key1 = _rsa_keypair()
    jwk1 = _public_jwk(key1, kid="k1")
    jwks_file = tmp_path / "jwks.json"
    jwks_file.write_text(json.dumps({"keys": [jwk1]}))

    monkeypatch.setenv("AUTH_MODE", "jwks")
    monkeypatch.setenv("JWKS_LOCAL_PATH", str(jwks_file))
    from config.settings import get_settings

    get_settings.cache_clear()
    try:
        resolver = JWKSKeyResolver()
        # Warm the cache with the original key.
        token1 = jwt.encode({"sub": "u1"}, key1, algorithm="RS256", headers={"kid": "k1"})
        assert await resolver.resolve_signing_key(token1) is not None

        # Rotate: a new key appears in the JWKS document while the cache (TTL 300s)
        # still only knows k1.
        key2 = _rsa_keypair()
        jwk2 = _public_jwk(key2, kid="k2")
        jwks_file.write_text(json.dumps({"keys": [jwk1, jwk2]}))

        token2 = jwt.encode({"sub": "u2"}, key2, algorithm="RS256", headers={"kid": "k2"})
        # Without the miss-triggered refresh this would raise; now it resolves.
        assert await resolver.resolve_signing_key(token2) is not None
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_jwks_unknown_kid_refresh_is_rate_limited(monkeypatch):
    """A flood of unknown kids must not amplify into repeated JWKS fetches; the
    out-of-band refresh is throttled to once per jwks_min_refresh_seconds."""
    key1 = _rsa_keypair()
    jwk1 = _public_jwk(key1, kid="k1")

    resolver = JWKSKeyResolver()
    fetches = {"count": 0}

    async def counting_fetch():
        fetches["count"] += 1
        return {"keys": [jwk1]}

    resolver._fetch_jwks = counting_fetch  # type: ignore[method-assign]

    token_a = jwt.encode({"sub": "a"}, key1, algorithm="RS256", headers={"kid": "unknown-a"})
    token_b = jwt.encode({"sub": "b"}, key1, algorithm="RS256", headers={"kid": "unknown-b"})

    with pytest.raises(ValueError, match="Unable to resolve"):
        await resolver.resolve_signing_key(token_a)
    # Initial load + one forced refresh on the miss.
    assert fetches["count"] == 2

    with pytest.raises(ValueError, match="Unable to resolve"):
        await resolver.resolve_signing_key(token_b)
    # Cooldown still active -> no additional fetch.
    assert fetches["count"] == 2


@pytest.mark.asyncio
async def test_auth_middleware_accepts_valid_jwks_token(jwks_setup, monkeypatch):
    # Reset the resolver singleton so it picks up the test JWKS path.
    import gateway.middleware.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_resolver", None, raising=False)

    captured = {}

    async def ok_app(scope, receive, send):
        captured["state"] = scope.get("state", {})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    mw = AuthMiddleware(ok_app)
    token = jwt.encode(
        {"sub": "alice", "tenant_id": "acme", "groups": ["weather"]},
        jwks_setup,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/rpc",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 5000),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        sent.append(m)

    await mw(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 200
    assert captured["state"]["user_id"] == "alice"
    assert captured["state"]["scopes"] == ["weather"]


@pytest.mark.asyncio
async def test_auth_middleware_rejects_invalid_signature(jwks_setup, monkeypatch):
    import gateway.middleware.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_resolver", None, raising=False)

    mw = AuthMiddleware(_ok := (lambda *a: None))

    other_key = _rsa_keypair()
    token = jwt.encode(
        {"sub": "mallory"}, other_key, algorithm="RS256", headers={"kid": "test-key"}
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/rpc",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 5000),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        sent.append(m)

    await mw(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 401


# --------------------------------------------------------------------------
# Auth-failure classification & observability
# --------------------------------------------------------------------------


def test_classify_auth_failure_buckets():
    cls = AuthMiddleware._classify_auth_failure
    assert cls(JWKSUnavailableError("down")) == "jwks_unavailable"
    assert cls(jwt.ExpiredSignatureError()) == "expired"
    assert cls(jwt.InvalidSignatureError()) == "bad_signature"
    assert cls(jwt.InvalidAudienceError()) == "bad_audience"
    assert cls(jwt.InvalidIssuerError()) == "bad_issuer"
    assert cls(jwt.DecodeError()) == "malformed"
    assert cls(ValueError("Unable to resolve JWT signing key")) == "unresolved_key"
    assert cls(RuntimeError("boom")) == "unexpected"


def test_auth_failure_response_maps_idp_outage_to_503_and_token_error_to_401():
    mw = AuthMiddleware(lambda *a: None)
    # A server-side IdP outage is retryable -> 503, not a credential rejection.
    assert mw._auth_failure_response(JWKSUnavailableError("down")).status_code == 503
    # A genuinely bad token stays an opaque 401.
    assert mw._auth_failure_response(jwt.ExpiredSignatureError()).status_code == 401


def test_auth_failure_response_emits_metric(monkeypatch):
    import gateway.middleware.auth as auth_mod

    recorded: list[str] = []
    monkeypatch.setattr(auth_mod, "observe_auth_failure", recorded.append)

    mw = AuthMiddleware(lambda *a: None)
    mw._auth_failure_response(jwt.ExpiredSignatureError())
    assert recorded == ["expired"]


@pytest.mark.asyncio
async def test_auth_middleware_returns_503_when_jwks_unreachable(monkeypatch, tmp_path):
    """An IdP/JWKS outage must not be reported to the caller as a 401 — the
    credential may be perfectly valid; we simply can't verify it right now."""
    import gateway.middleware.auth as auth_mod

    monkeypatch.setenv("AUTH_MODE", "jwks")
    # Point at a local path that does not exist -> JWKSUnavailableError on fetch.
    monkeypatch.setenv("JWKS_LOCAL_PATH", str(tmp_path / "does-not-exist.json"))
    monkeypatch.setenv("JWT_ISSUER", "")
    monkeypatch.setenv("JWT_AUDIENCE", "")
    from config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(auth_mod, "_resolver", None, raising=False)
    try:
        key = _rsa_keypair()
        token = jwt.encode({"sub": "u1"}, key, algorithm="RS256", headers={"kid": "k1"})
        mw = AuthMiddleware(lambda *a: None)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/rpc",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "client": ("127.0.0.1", 5000),
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)

        await mw(scope, receive, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        assert status == 503
    finally:
        get_settings.cache_clear()
