"""Fail-soft seeding of a starter "utilities" server for brand-new tenants.

A freshly self-registered tenant has an empty catalog, so its ``/mcp`` endpoint
exposes no tools on first connect — a confusing first impression. This module
installs a single stdlib-only, read-only function (``word_count``) so there is
always something to discover and call.

Design constraints:

* One tool only — keeps the seed inside the ``unconfirmed`` 1-server / 1-tool cap
  (:mod:`services.account_tier`) so the owner can still edit it without tripping
  the tier guard.
* ``origin="tenant"`` — the seeded server belongs to the registrant, who may edit
  or delete it (platform-origin servers are locked to platform admins).
* Fully fail-soft — embeddings are generated synchronously on mount and may be
  unavailable in a barebones deployment; a failure here must never break the
  sign-up that calls :func:`seed_starter_server`.
"""

from __future__ import annotations

import logging
from typing import Any

from config.settings import Settings, get_settings
from database.mongo import get_tenant_database
from services.code_tools import CODE_TRANSPORT, encrypt_raw_code, lint_code_tool
from services.proxy_registry import get_proxy_registry

logger = logging.getLogger(__name__)

STARTER_SERVER_NAME = "utilities"

# Kept deliberately tiny and dependency-free: pure standard library, no context
# capabilities, so it validates and runs in any deployment regardless of which
# sandbox bridges are enabled.
_WORD_COUNT_CODE = '''def word_count(text: str) -> dict:
    """Count the words and characters in the given text."""
    words = [w for w in text.split() if w]
    return {"words": len(words), "characters": len(text)}
'''


def _starter_tool() -> dict[str, Any]:
    return {
        "server": STARTER_SERVER_NAME,
        "name": "word_count",
        "description": "Count the words and characters in a string.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to analyze"}},
            "required": ["text"],
        },
        "scopes": ["utilities", "readonly"],
        "raw_code": _WORD_COUNT_CODE,
        "requirements": [],
        "metadata": {"action_type": "read"},
    }


async def seed_starter_server(tenant_id: str, *, settings: Settings | None = None) -> bool:
    """Best-effort install of a 1-tool ``utilities`` server for a new tenant.

    Returns ``True`` when the server was seeded, ``False`` otherwise. Never raises:
    a seeding/embedding failure is logged and swallowed so the caller's sign-up
    still succeeds.
    """
    settings = settings or get_settings()
    registry = get_proxy_registry()
    try:
        tool = _starter_tool()
        # Defensive: never seed code that wouldn't pass the normal save lint.
        lint_code_tool(tool)
        tool["raw_code"] = await encrypt_raw_code(tenant_id, tool["raw_code"], settings)
        tool["embedding"] = []
        doc: dict[str, Any] = {
            "_id": STARTER_SERVER_NAME,
            "tenant_id": tenant_id,
            "server": STARTER_SERVER_NAME,
            "transport": CODE_TRANSPORT,
            "origin": "tenant",
            "enabled": True,
            "metadata": {"domain": "utility", "runtime": "wasm"},
            "tools": [tool],
        }
        # Mount first: this is the embedding-dependent step (it writes tool_catalog
        # with a generated vector). Only persist the routing doc once it succeeds so
        # a failure leaves the tenant clean rather than with an orphaned server.
        await registry.mount_or_update(doc)
        await get_tenant_database(tenant_id)["routing_registry"].replace_one(
            {"_id": doc["_id"]}, doc, upsert=True
        )
        logger.info("Seeded starter '%s' server for tenant=%s", STARTER_SERVER_NAME, tenant_id)
        return True
    except Exception:
        logger.warning(
            "Starter seed skipped for tenant=%s (non-fatal).", tenant_id, exc_info=True
        )
        # Best-effort cleanup of a partially-mounted server so nothing dangles.
        try:
            await registry.unmount(STARTER_SERVER_NAME, tenant_id=tenant_id)
        except Exception:
            pass
        return False
