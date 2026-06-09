from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx


def _parse_env(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"Invalid --env value '{item}', expected KEY=VALUE.")
        key, value = item.split("=", 1)
        parsed[key] = value
    return parsed


def _request(
    *,
    method: str,
    base_url: str,
    path: str,
    token: str | None,
    payload: dict[str, Any] | None = None,
    tenant_id: str | None = None,
) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id
    response = httpx.request(method, url, headers=headers, json=payload, timeout=30.0)
    response.raise_for_status()
    if not response.content:
        return {}
    return response.json()


def _login_for_session_token(
    *,
    base_url: str,
    ui_path: str,
    email: str,
    password: str,
) -> str:
    url = f"{base_url.rstrip('/')}{ui_path.rstrip('/')}/login"
    response = httpx.post(
        url,
        headers={"Accept": "application/json"},
        json={"email": email, "password": password},
        timeout=30.0,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("Login succeeded but no token was returned.")
    return token


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Admin CLI for mdb-mcp-gateway.")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Gateway base URL.")
    parser.add_argument("--token", default=None, help="Bearer token for authenticated mode.")
    parser.add_argument("--ui-path", default="/ui", help="Admin UI path used for login.")
    parser.add_argument(
        "--email",
        default=None,
        help="Admin email for session login (or set ADMIN_EMAIL env var).",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Admin password for session login (or set ADMIN_PASSWORD env var).",
    )

    root_subparsers = parser.add_subparsers(dest="resource", required=True)

    tenant_parser = root_subparsers.add_parser("tenant", help="Manage tenants.")
    tenant_subparsers = tenant_parser.add_subparsers(dest="action", required=True)
    tenant_create = tenant_subparsers.add_parser("create", help="Provision a tenant.")
    tenant_create.add_argument("--tenant-id", required=True, help="Tenant ID to provision.")
    tenant_subparsers.add_parser("list", help="List visible tenants.")

    server_parser = root_subparsers.add_parser("server", help="Manage downstream servers.")
    server_subparsers = server_parser.add_subparsers(dest="action", required=True)

    server_add = server_subparsers.add_parser("add", help="Create or update a downstream server.")
    server_add.add_argument("--tenant-id", default=None, help="Target tenant ID.")
    server_add.add_argument("--server", required=True, help="Server identifier.")
    server_add.add_argument(
        "--transport",
        required=True,
        choices=["streamable_http", "sse", "stdio"],
        help="Downstream MCP transport.",
    )
    server_add.add_argument("--endpoint", default=None, help="Endpoint URL for network transports.")
    server_add.add_argument("--command", default=None, help="Command for stdio transport.")
    server_add.add_argument("--arg", action="append", default=[], help="Repeated stdio arg.")
    server_add.add_argument(
        "--env", action="append", default=[], help="Repeated env var in KEY=VALUE format."
    )
    server_add.add_argument("--cwd", default=None, help="Working directory for stdio transport.")
    server_add.add_argument("--disabled", action="store_true", help="Create server disabled.")
    server_add.add_argument(
        "--metadata",
        default="{}",
        help='JSON object metadata, e.g. \'{"domain":"payments"}\'.',
    )

    server_list = server_subparsers.add_parser("list", help="List servers for a tenant.")
    server_list.add_argument("--tenant-id", default=None, help="Target tenant ID.")

    server_get = server_subparsers.add_parser("get", help="Get one server.")
    server_get.add_argument("--tenant-id", default=None, help="Target tenant ID.")
    server_get.add_argument("--server", required=True, help="Server identifier.")

    server_update = server_subparsers.add_parser("update", help="Patch an existing server.")
    server_update.add_argument("--tenant-id", default=None, help="Target tenant ID.")
    server_update.add_argument("--server", required=True, help="Server identifier.")
    server_update.add_argument(
        "--transport",
        choices=["streamable_http", "sse", "stdio"],
        default=None,
        help="New transport.",
    )
    server_update.add_argument("--endpoint", default=None, help="New endpoint.")
    server_update.add_argument("--command", default=None, help="New stdio command.")
    server_update.add_argument("--arg", action="append", default=None, help="Replace stdio args.")
    server_update.add_argument(
        "--env",
        action="append",
        default=None,
        help="Replace env vars with KEY=VALUE entries.",
    )
    server_update.add_argument("--cwd", default=None, help="New working directory.")
    server_update.add_argument("--enabled", dest="enabled", action="store_true", default=None)
    server_update.add_argument("--disabled", dest="enabled", action="store_false")
    server_update.add_argument("--metadata", default=None, help="Replace metadata JSON object.")

    server_remove = server_subparsers.add_parser("remove", help="Delete a server.")
    server_remove.add_argument("--tenant-id", default=None, help="Target tenant ID.")
    server_remove.add_argument("--server", required=True, help="Server identifier.")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    token = args.token
    if not token:
        email = args.email or os.getenv("ADMIN_EMAIL")
        password = args.password or os.getenv("ADMIN_PASSWORD")
        if email and password:
            token = _login_for_session_token(
                base_url=args.base_url,
                ui_path=args.ui_path,
                email=email,
                password=password,
            )

    try:
        if args.resource == "tenant":
            if args.action == "create":
                result = _request(
                    method="POST",
                    base_url=args.base_url,
                    path="/admin/tenants",
                    token=token,
                    payload={"tenant_id": args.tenant_id},
                    tenant_id=args.tenant_id,
                )
            else:  # list
                result = _request(
                    method="GET",
                    base_url=args.base_url,
                    path="/admin/tenants",
                    token=token,
                )
        else:
            if args.action == "add":
                metadata = json.loads(args.metadata)
                result = _request(
                    method="POST",
                    base_url=args.base_url,
                    path="/admin/servers",
                    token=token,
                    tenant_id=args.tenant_id,
                    payload={
                        "tenant_id": args.tenant_id,
                        "server": args.server,
                        "transport": args.transport,
                        "endpoint": args.endpoint,
                        "command": args.command,
                        "args": args.arg,
                        "env": _parse_env(args.env),
                        "cwd": args.cwd,
                        "enabled": not args.disabled,
                        "metadata": metadata,
                    },
                )
            elif args.action == "list":
                query = f"?tenant_id={args.tenant_id}" if args.tenant_id else ""
                result = _request(
                    method="GET",
                    base_url=args.base_url,
                    path=f"/admin/servers{query}",
                    token=token,
                    tenant_id=args.tenant_id,
                )
            elif args.action == "get":
                query = f"?tenant_id={args.tenant_id}" if args.tenant_id else ""
                result = _request(
                    method="GET",
                    base_url=args.base_url,
                    path=f"/admin/servers/{args.server}{query}",
                    token=token,
                    tenant_id=args.tenant_id,
                )
            elif args.action == "update":
                payload: dict[str, Any] = {"tenant_id": args.tenant_id}
                for field in ["transport", "endpoint", "command", "cwd", "enabled"]:
                    value = getattr(args, field)
                    if value is not None:
                        payload[field] = value
                if args.arg is not None:
                    payload["args"] = args.arg
                if args.env is not None:
                    payload["env"] = _parse_env(args.env)
                if args.metadata is not None:
                    payload["metadata"] = json.loads(args.metadata)
                result = _request(
                    method="PATCH",
                    base_url=args.base_url,
                    path=f"/admin/servers/{args.server}",
                    token=token,
                    tenant_id=args.tenant_id,
                    payload=payload,
                )
            else:  # remove
                query = f"?tenant_id={args.tenant_id}" if args.tenant_id else ""
                result = _request(
                    method="DELETE",
                    base_url=args.base_url,
                    path=f"/admin/servers/{args.server}{query}",
                    token=token,
                    tenant_id=args.tenant_id,
                )
    except (ValueError, httpx.HTTPError) as exc:
        raise SystemExit(str(exc)) from exc

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
