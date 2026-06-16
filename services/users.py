from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from config.settings import get_settings
from database.mongo import get_control_database, get_tenant_database
from services.admin_session import verify_credentials
from services.passwords import hash_password, verify_password

logger = logging.getLogger(__name__)

# Users live in the control DB (not a tenant DB): authentication happens before a
# tenant is resolved, and a platform-admin spans every tenant.
USERS_COLLECTION = "users"
SESSION_CONTEXT_COLLECTION = "session_context"

# A demo account must be able to BOTH discover and invoke tools. The RBAC gate
# (gateway/middleware/rbac.py) needs `tool:invoke`; the scope filter
# (services/hybrid_search.py) and per-call authorization (services/authorization.py)
# need a `server:*`/`server:<name>` scope plus a matching tool scope. So a demo
# carries `tool:invoke` and catalog-derived scopes (see derive_demo_scopes).
DEMO_USER_ROLES = ["user", "tool:invoke"]


async def derive_demo_scopes(tenant_id: str) -> list[str]:
    """Compute scopes that let a demo user see and call *everything* in a tenant.

    `server:*` grants visibility/authorization across all servers (current and
    future), and every distinct tool scope already present in the tenant's
    ``tool_catalog`` is granted so scope-gated tools are actually invocable. This
    is catalog-aware on purpose: a demo created in any tenant "just works" against
    whatever tools that tenant has, with no hand-maintained scope list.
    """
    collection = get_tenant_database(tenant_id)["tool_catalog"]
    docs = await collection.find({}, {"scopes": 1, "_id": 0}).to_list(length=10_000)
    tool_scopes = sorted(
        {
            scope
            for doc in docs
            for scope in (doc.get("scopes") or [])
            if isinstance(scope, str) and scope and not scope.startswith("server:")
        }
    )
    return ["server:*", *tool_scopes]


class UserError(ValueError):
    """Base error for user-management operations."""


class UserAlreadyExists(UserError):
    """A user with the given email already exists."""


class UserNotFound(UserError):
    """No user matched the given identifier."""


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _clean_str_list(values: Iterable[Any] | None) -> list[str]:
    if not values:
        return []
    return [value for value in values if isinstance(value, str)]


def public_user(doc: dict[str, Any]) -> dict[str, Any]:
    """Project a stored user document to its API-safe shape (no password hash)."""
    return {
        "id": str(doc.get("_id", "")),
        "tenant_id": str(doc.get("tenant_id", "")),
        "email": str(doc.get("email", "")),
        "roles": _clean_str_list(doc.get("roles")),
        "scopes": _clean_str_list(doc.get("scopes")),
        "status": str(doc.get("status", "active")),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "created_by": doc.get("created_by"),
    }


def _users_collection():
    return get_control_database()[USERS_COLLECTION]


async def get_user_raw(user_id: str) -> dict[str, Any] | None:
    """Return the raw stored document (including password hash) or ``None``."""
    return await _users_collection().find_one({"_id": user_id})


async def find_user_by_email(email: str) -> dict[str, Any] | None:
    return await _users_collection().find_one({"email": normalize_email(email)})


async def list_users(*, tenant_id: str | None = None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if tenant_id:
        query["tenant_id"] = tenant_id
    docs = await _users_collection().find(query).to_list(length=10_000)
    docs.sort(key=lambda doc: str(doc.get("email", "")))
    return [public_user(doc) for doc in docs]


async def create_user(
    *,
    email: str,
    password: str,
    tenant_id: str,
    roles: Iterable[str],
    scopes: Iterable[str] | None = None,
    status: str = "active",
    created_by: str | None = None,
) -> dict[str, Any]:
    """Create a user, enforcing globally-unique email.

    Email uniqueness is enforced at the application layer (in addition to the
    unique index) because the login form is keyed by email alone, so an address
    must resolve to exactly one account regardless of tenant.
    """
    collection = _users_collection()
    normalized = normalize_email(email)
    if not normalized:
        raise UserError("Email is required.")
    existing = await collection.find_one({"email": normalized})
    if existing is not None:
        raise UserAlreadyExists(f"A user with email '{normalized}' already exists.")
    now = datetime.now(UTC)
    doc = {
        "_id": uuid.uuid4().hex,
        "tenant_id": tenant_id,
        "email": normalized,
        "password_hash": hash_password(password),
        "roles": _clean_str_list(roles),
        "scopes": _clean_str_list(scopes),
        "status": status,
        "created_at": now,
        "updated_at": now,
        "created_by": created_by,
    }
    await collection.insert_one(doc)
    return public_user(doc)


async def update_user(
    user_id: str,
    *,
    password: str | None = None,
    roles: Iterable[str] | None = None,
    scopes: Iterable[str] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    collection = _users_collection()
    updates: dict[str, Any] = {"updated_at": datetime.now(UTC)}
    if password is not None:
        updates["password_hash"] = hash_password(password)
    if roles is not None:
        updates["roles"] = _clean_str_list(roles)
    if scopes is not None:
        updates["scopes"] = _clean_str_list(scopes)
    if status is not None:
        updates["status"] = status
    result = await collection.update_one({"_id": user_id}, {"$set": updates})
    if getattr(result, "matched_count", 0) == 0:
        raise UserNotFound(f"User '{user_id}' not found.")
    doc = await collection.find_one({"_id": user_id})
    if doc is None:  # pragma: no cover - lost race between update and re-read
        raise UserNotFound(f"User '{user_id}' not found.")
    return public_user(doc)


async def delete_user(user_id: str) -> bool:
    collection = _users_collection()
    doc = await collection.find_one({"_id": user_id})
    if doc is None:
        return False
    await collection.delete_one({"_id": user_id})
    await _clear_session_context(
        tenant_id=str(doc.get("tenant_id", "")),
        user_id=str(doc.get("email", "")),
    )
    return True


async def authenticate(email: str, password: str) -> dict[str, Any] | None:
    """Return the public user when credentials are valid and the account active."""
    doc = await find_user_by_email(email)
    if doc is None:
        return None
    if str(doc.get("status", "active")) != "active":
        return None
    if not verify_password(password, str(doc.get("password_hash", ""))):
        return None
    return public_user(doc)


async def resolve_login_principal(email: str, password: str) -> dict[str, Any] | None:
    """Resolve credentials to a login principal ``{email, tenant_id, roles}``.

    Single source of truth shared by the admin UI login and the ``/auth/token``
    endpoint. Managed users (control-DB ``users`` collection) take precedence;
    the env ``ADMIN_EMAIL``/``ADMIN_PASSWORD`` pair survives only as a bootstrap
    superuser. The DB lookup is best-effort: before the control plane is
    provisioned (or in unit tests without a database) it is skipped so the
    bootstrap admin can still sign in.
    """
    settings = get_settings()
    try:
        user = await authenticate(email, password)
    except Exception as exc:  # pragma: no cover - defensive: DB not yet reachable
        logger.warning("User store unavailable during login, using bootstrap admin only: %s", exc)
        user = None
    if user is not None:
        return {
            "email": user["email"],
            "tenant_id": user["tenant_id"],
            "roles": user["roles"],
        }
    if verify_credentials(email, password):
        return {
            "email": normalize_email(email),
            "tenant_id": settings.default_tenant_id,
            "roles": [settings.platform_admin_role, "admin"],
        }
    return None


async def sync_session_context(user: dict[str, Any]) -> None:
    """Mirror a user's roles/scopes into the ``session_context`` control doc.

    ``gateway/middleware/rbac.py`` reads ``session_context`` to hydrate roles for
    JWT principals on ``/rpc``. Keeping it in lockstep with the user record closes
    the loop so a user managed in the console is authorized consistently whether
    they hit the admin API (session cookie) or ``/rpc`` (bearer token with the
    same ``sub``/``tenant_id``). Written without ``expires_at`` so the TTL index
    leaves operator-managed entries in place.
    """
    tenant_id = str(user.get("tenant_id", ""))
    user_id = str(user.get("email", ""))
    if not tenant_id or not user_id:
        return
    await get_control_database()[SESSION_CONTEXT_COLLECTION].update_one(
        {"tenant_id": tenant_id, "user_id": user_id},
        {
            "$set": {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "roles": _clean_str_list(user.get("roles")),
                "scopes": _clean_str_list(user.get("scopes")),
                # Mirror status so the /rpc RBAC gate can revoke a disabled user on
                # their next request without an extra control-plane read.
                "status": str(user.get("status", "active")) or "active",
                "updated_at": datetime.now(UTC),
            }
        },
        upsert=True,
    )


async def _clear_session_context(*, tenant_id: str, user_id: str) -> None:
    if not tenant_id or not user_id:
        return
    await get_control_database()[SESSION_CONTEXT_COLLECTION].delete_one(
        {"tenant_id": tenant_id, "user_id": user_id}
    )
