"""Curated, capability-aware seeding of a demo workspace's tool pack + data.

A demo workspace (see :mod:`services.demo_workspace`) is meant to *shine on the
first call* in front of a prospect. This module installs a hand-picked set of
runnable code tools and the sample data they read, choosing the pack to match
the host's live sandbox capabilities so **every seeded tool actually runs**:

* ``utilities.word_count`` — pure standard library; always seeded, runs anywhere.
* ``analytics.*`` (``get_click_stats`` read + ``track_click`` write) and a
  pre-seeded ``clicks`` collection — only when the DB bridge is enabled, so the
  leaderboard returns real numbers immediately.
* ``analytics.track_and_report`` — composes sibling tools via ``context.tools``;
  added only when BOTH the DB and tool bridges are enabled.
* ``directory.find_user`` + a pre-seeded ``users`` collection (including the very
  id the gallery example uses) — only when the DB bridge is enabled.

Design constraints (mirrors :mod:`services.starter_seed`):

* Fully fail-soft and *partial-success friendly*: each server is mounted
  independently and a failure on one (e.g. a transient embedding hiccup) never
  aborts the others, so a demo always comes up with at least the stdlib utility.
* ``origin="tenant"`` so the demo's recipient (a tenant-admin of the isolated
  demo tenant) can edit, run, and extend every seeded function.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId

from config.settings import Settings, get_settings
from database.mongo import get_tenant_database
from services.code_tools import CODE_TRANSPORT, encrypt_raw_code, lint_code_tool
from services.proxy_registry import get_proxy_registry

logger = logging.getLogger(__name__)

# A stable id so the gallery's "look up by id" example (which pre-fills this exact
# value) returns a real document the instant the demo opens.
_SAMPLE_USER_ID = "656f1f77bcf86cd799439011"


@dataclass
class DemoSeedResult:
    """What actually landed in the demo tenant, for the API response + logs."""

    servers: list[str] = field(default_factory=list)
    tools: int = 0
    bridges: dict[str, bool] = field(default_factory=dict)


_WORD_COUNT_CODE = '''def word_count(text: str) -> dict:
    """Count the words and characters in the given text."""
    words = [w for w in text.split() if w]
    return {"words": len(words), "characters": len(text)}
'''

_GET_STATS_CODE = '''def get_click_stats(limit: int = 5) -> dict:
    """Return the most-clicked targets, highest first."""
    cap = max(1, min(int(limit or 5), 25))
    rows = context.db.clicks.aggregate(
        [
            {"$group": {"_id": "$target", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": cap},
        ]
    )
    top = [{"target": row.get("_id"), "count": row.get("count", 0)} for row in rows]
    return {"top_targets": top, "source": "sandbox-code"}
'''

_TRACK_CLICK_CODE = '''from datetime import datetime, timezone


def track_click(target: str, source: str = "web") -> dict:
    """Record a click, then return the running total for that target."""
    item = (target or "").strip()
    if not item:
        raise ValueError("target is required")
    result = context.db.clicks.insert_one(
        {
            "target": item,
            "source": (source or "web").strip() or "web",
            "created_at": datetime.now(timezone.utc),
        }
    )
    total = context.db.clicks.count_documents({"target": item})
    return {"target": item, "count": int(total), "click_id": result.inserted_id}
'''

_TRACK_AND_REPORT_CODE = '''def track_and_report(target: str, source: str = "web") -> dict:
    """Record a click via a sibling tool, then return the leaderboard.

    The tenant is a namespace: context.tools.<server>.<tool>(**kwargs) calls a
    sibling tool. Each hop is re-authorized as you and runs in its own sandbox.
    """
    recorded = context.tools.analytics.track_click(target=target, source=source)
    stats = context.tools.analytics.get_click_stats(limit=5)
    return {"recorded": recorded, "leaderboard": stats.get("top_targets", [])}
'''

_FIND_USER_CODE = '''def find_user(user_id: str) -> dict:
    """Look up a single user document by its 24-char hex id."""
    # context.db.ObjectId builds the BSON id — it lives on the db handle.
    found = context.db.users.find_one({"_id": context.db.ObjectId(user_id)})
    return found or {}
'''


def _tool(
    *,
    server: str,
    name: str,
    description: str,
    code: str,
    action_type: str,
    input_schema: dict[str, Any],
    scopes: list[str],
) -> dict[str, Any]:
    return {
        "server": server,
        "name": name,
        "description": description,
        "input_schema": input_schema,
        "scopes": scopes,
        "raw_code": code,
        "requirements": [],
        "metadata": {"action_type": action_type},
    }


def _utilities_tools() -> list[dict[str, Any]]:
    return [
        _tool(
            server="utilities",
            name="word_count",
            description="Count the words and characters in a string.",
            code=_WORD_COUNT_CODE,
            action_type="read",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Text to analyze"}},
                "required": ["text"],
            },
            scopes=["utilities", "readonly"],
        )
    ]


def _analytics_tools(*, include_compose: bool) -> list[dict[str, Any]]:
    tools = [
        _tool(
            server="analytics",
            name="get_click_stats",
            description="Return the most-clicked targets, highest first.",
            code=_GET_STATS_CODE,
            action_type="read",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "How many targets to return (1-25)",
                        "default": 5,
                    }
                },
            },
            scopes=["analytics", "readonly"],
        ),
        _tool(
            server="analytics",
            name="track_click",
            description="Record a click and return the running total for that target.",
            code=_TRACK_CLICK_CODE,
            action_type="write",
            input_schema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "What was clicked"},
                    "source": {
                        "type": "string",
                        "description": "Attribution source",
                        "default": "web",
                    },
                },
                "required": ["target"],
            },
            scopes=["analytics"],
        ),
    ]
    if include_compose:
        tools.append(
            _tool(
                server="analytics",
                name="track_and_report",
                description="Record a click via a sibling tool, then return the leaderboard.",
                code=_TRACK_AND_REPORT_CODE,
                action_type="write",
                input_schema={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "What was clicked"},
                        "source": {
                            "type": "string",
                            "description": "Attribution source",
                            "default": "web",
                        },
                    },
                    "required": ["target"],
                },
                scopes=["analytics"],
            )
        )
    return tools


def _directory_tools() -> list[dict[str, Any]]:
    return [
        _tool(
            server="directory",
            name="find_user",
            description="Look up a single user document by its string id.",
            code=_FIND_USER_CODE,
            action_type="read",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "The user's _id as a 24-char hex string",
                    }
                },
                "required": ["user_id"],
            },
            scopes=["directory", "readonly"],
        )
    ]


async def _seed_server(
    tenant_id: str,
    *,
    server: str,
    tools: list[dict[str, Any]],
    metadata: dict[str, Any],
    settings: Settings,
) -> bool:
    """Mount one code server and persist its routing doc. Best-effort, returns success.

    Lints each function on plaintext, encrypts the source at rest, then mounts
    (which writes ``tool_catalog`` with embeddings) before persisting the routing
    registry doc — the same ordering as :func:`services.starter_seed`, so a mount
    failure leaves no orphaned routing row.
    """
    registry = get_proxy_registry()
    try:
        prepared: list[dict[str, Any]] = []
        for tool in tools:
            tool = dict(tool)
            lint_code_tool(tool)
            tool["raw_code"] = await encrypt_raw_code(tenant_id, tool["raw_code"], settings)
            tool["embedding"] = []
            prepared.append(tool)
        doc: dict[str, Any] = {
            "_id": server,
            "tenant_id": tenant_id,
            "server": server,
            "transport": CODE_TRANSPORT,
            "origin": "tenant",
            "enabled": True,
            "metadata": metadata,
            "tools": prepared,
        }
        await registry.mount_or_update(doc)
        await get_tenant_database(tenant_id)["routing_registry"].replace_one(
            {"_id": doc["_id"]}, doc, upsert=True
        )
        return True
    except Exception:
        logger.warning(
            "Demo seed: server '%s' failed to mount for tenant=%s (non-fatal); "
            "continuing with the rest of the pack.",
            server,
            tenant_id,
            exc_info=True,
        )
        try:
            await registry.unmount(server, tenant_id=tenant_id)
        except Exception:
            pass
        return False


async def _seed_sample_clicks(tenant_id: str) -> None:
    """Pre-seed a small, skewed ``clicks` set so the leaderboard is non-trivial."""
    collection = get_tenant_database(tenant_id)["clicks"]
    if await collection.count_documents({}) > 0:
        return
    now = datetime.now(UTC)
    weights = {"pricing": 6, "docs": 4, "home": 3, "blog": 2, "careers": 1}
    docs: list[dict[str, Any]] = []
    for index, (target, count) in enumerate(weights.items()):
        for _ in range(count):
            docs.append(
                {
                    "target": target,
                    "source": "web",
                    "created_at": now - timedelta(minutes=index),
                }
            )
    if docs:
        await collection.insert_many(docs)


async def _seed_sample_users(tenant_id: str) -> None:
    """Pre-seed a tiny ``users`` set, including the gallery example's exact id."""
    collection = get_tenant_database(tenant_id)["users"]
    if await collection.count_documents({}) > 0:
        return
    await collection.insert_many(
        [
            {
                "_id": ObjectId(_SAMPLE_USER_ID),
                "name": "Ada Lovelace",
                "email": "ada@example.com",
                "plan": "enterprise",
            },
            {"name": "Grace Hopper", "email": "grace@example.com", "plan": "team"},
            {"name": "Alan Turing", "email": "alan@example.com", "plan": "free"},
        ]
    )


async def seed_demo_pack(tenant_id: str, *, settings: Settings | None = None) -> DemoSeedResult:
    """Install the capability-aware demo pack + sample data into ``tenant_id``.

    Never raises: a seeding hiccup degrades the pack rather than failing the demo
    provision that calls it. Returns a :class:`DemoSeedResult` describing exactly
    what landed so the API can report it and the caller can derive demo scopes.
    """
    settings = settings or get_settings()
    db_enabled = bool(settings.sandbox_db_bridge_enabled)
    tool_enabled = bool(settings.sandbox_tool_bridge_enabled)
    http_enabled = bool(settings.sandbox_http_bridge_enabled)
    result = DemoSeedResult(
        bridges={"db": db_enabled, "tools": tool_enabled, "http": http_enabled}
    )

    # 1. Stdlib utility — always runs, no bridges required.
    utilities = _utilities_tools()
    if await _seed_server(
        tenant_id,
        server="utilities",
        tools=utilities,
        metadata={"domain": "utility", "runtime": "wasm"},
        settings=settings,
    ):
        result.servers.append("utilities")
        result.tools += len(utilities)

    # 2. DB-backed packs — only when the read can actually reach the tenant DB.
    if db_enabled:
        try:
            await _seed_sample_clicks(tenant_id)
            await _seed_sample_users(tenant_id)
        except Exception:
            logger.warning(
                "Demo seed: sample data insert failed for tenant=%s (non-fatal).",
                tenant_id,
                exc_info=True,
            )

        analytics = _analytics_tools(include_compose=tool_enabled)
        if await _seed_server(
            tenant_id,
            server="analytics",
            tools=analytics,
            metadata={"domain": "analytics", "runtime": "wasm"},
            settings=settings,
        ):
            result.servers.append("analytics")
            result.tools += len(analytics)

        directory = _directory_tools()
        if await _seed_server(
            tenant_id,
            server="directory",
            tools=directory,
            metadata={"domain": "directory", "runtime": "wasm"},
            settings=settings,
        ):
            result.servers.append("directory")
            result.tools += len(directory)

    logger.info(
        "Demo pack seeded for tenant=%s: servers=%s tools=%d bridges=%s",
        tenant_id,
        result.servers,
        result.tools,
        result.bridges,
    )
    return result
