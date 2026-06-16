"""Mint a local **RS256** JWT for the gateway's ``AUTH_MODE=jwks`` offline flow.

This signs with the repo's bundled dev RSA private key (`config/dev-private-key.pem`),
which the gateway verifies via the matching local JWKS (`config/dev-jwks.json`). It
is therefore only useful when the gateway runs in JWKS mode:

    AUTH_MODE=jwks JWKS_LOCAL_PATH=./config/dev-jwks.json \\
        JWT_ISSUER=http://localhost:8000 JWT_AUDIENCE=mdb-mcp-gateway

For the **default** ``AUTH_MODE=hs256`` setup (incl. ``docker compose up``), don't
use this script — the gateway signs/verifies with its own ``JWT_SECRET``. Instead
mint a token from the admin console (**Users -> Generate token**) or exchange
credentials at ``POST /auth/token``.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Mint a local RS256 JWT for the gateway's AUTH_MODE=jwks offline flow. "
            "For the default hs256 mode, use the admin console 'Generate token' "
            "button or POST /auth/token instead."
        )
    )
    parser.add_argument("--subject", default="local-user", help="JWT subject (sub claim).")
    parser.add_argument("--tenant-id", default="local-dev", help="tenant_id claim.")
    parser.add_argument(
        "--groups",
        nargs="*",
        default=["weather", "orders", "readonly"],
        help="Groups claim values used for tool scope filtering.",
    )
    parser.add_argument(
        "--roles",
        nargs="*",
        default=["tool:invoke"],
        help="Roles claim values used by RBAC middleware.",
    )
    parser.add_argument("--issuer", default="http://localhost:8000", help="JWT issuer.")
    parser.add_argument("--audience", default="mdb-mcp-gateway", help="JWT audience.")
    parser.add_argument(
        "--ttl-minutes", type=int, default=60, help="Token time-to-live in minutes."
    )
    parser.add_argument(
        "--kid",
        default="dev-local-key-1",
        help="Key id (kid) in JWT header. Must exist in JWKS.",
    )
    parser.add_argument(
        "--private-key",
        default="config/dev-private-key.pem",
        help="Path to RSA private key (PEM).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    private_key = Path(args.private_key).read_text(encoding="utf-8")
    now = datetime.now(UTC)
    payload = {
        "sub": args.subject,
        "tenant_id": args.tenant_id,
        "groups": args.groups,
        "roles": args.roles,
        "iss": args.issuer,
        "aud": args.audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=args.ttl_minutes)).timestamp()),
    }
    token = jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": args.kid})
    print(token)


if __name__ == "__main__":
    main()
