# Function Runtime Context

This document is for authors building `transport="code"` tools in the gateway.

## What You Get In Function Code

Your function runs in a wasm sandbox and can use:

- Python builtins + standard library (subject to sandbox lint policy)
- `context` runtime helper object

`context` exposes exactly four resources — nothing else lives on it:

- `context.db` — tenant-scoped database access (host-relayed, no DB creds in sandbox)
- `context.env` — per-server encrypted environment values
- `context.tools` — call sibling tools in your tenant namespace (host-relayed, re-authorized)
- `context.http` — opt-in, host-mediated outbound HTTPS (deny-by-default allowlist, SSRF-screened, host-side secret injection)

For BSON ids, use `context.db.ObjectId("...")` (it belongs to the database). For
timestamps, use the standard library: `from datetime import datetime, timezone`.

No legacy `secrets` argument or `SECRETS` global is injected.

## `context.db` Virtual Database

Use collection access either way:

- `context.db["users"]`
- `context.db.users`

Common methods:

- `find_one(query)`
- `find(query, limit=...)`
- `aggregate(pipeline)`
- `count_documents(query)`
- `distinct(field, query={})`
- `insert_one(doc)`
- `insert_many(docs)`
- `update_one(filter, update, upsert=False)`
- `update_many(filter, update, upsert=False)`
- `delete_one(filter)`
- `delete_many(filter)`

### Example

```python
def get_active_user(email: str) -> dict:
    doc = context.db.users.find_one({"email": email, "status": "active"})
    return {"user": doc}
```

### Write results (PyMongo-style)

Write operations return a result object with both attribute and dict access:

| Operation | Fields |
| --- | --- |
| `insert_one` | `.inserted_id` |
| `insert_many` | `.inserted_ids` |
| `update_one` / `update_many` | `.matched_count`, `.modified_count`, `.upserted_id` |
| `delete_one` / `delete_many` | `.deleted_count` |
| all | `.acknowledged` |

```python
def add(target: str) -> dict:
    result = context.db.clicks.insert_one({"target": target})
    return {"click_id": result.inserted_id}  # ObjectId is auto-serialized
```

## `context.env` Virtual Environment

Each MCP server can define encrypted env values from Admin Studio (Secrets tab).
At runtime, code tools on that server can read them via `context.env`.

```python
def current_label() -> dict:
    return {"label": context.env.get("CLICK_LABEL", "anonymous")}
```

Values are encrypted at rest and never returned by admin APIs after write.

## `context.tools` — Calling Other Tools

Your tenant is a **namespace**: it holds servers, and each server holds one or
more tools. `context.tools` lets one tool call another, so you can compose small
single-purpose tools into a workflow instead of duplicating logic.

```python
context.tools["<server>"]["<tool>"](**kwargs)   # works for any name
context.tools.<server>.<tool>(**kwargs)          # attribute style (no hyphens)
context.call("<server>", "<tool>", **kwargs)     # explicit, naming-agnostic
```

Pass arguments **by keyword** (they map to the target function's parameters).
The return value is exactly what the other tool returns (BSON-aware, JSON-safe).

```python
def track_and_report(target: str, source: str = "web") -> dict:
    recorded = context.tools.analytics.track_click(target=target, source=source)
    stats = context.tools.analytics.get_click_stats(limit=5)
    return {"recorded": recorded, "leaderboard": stats.get("top_targets", [])}
```

### How it stays safe

Every cross-tool call is relayed to the host and re-checked before it runs:

- **Re-authorized** against *your* caller's scopes — you can only call tools you
  could already call directly (server scope + tool scopes still apply).
- **Code tools only.** Sibling must be a `transport="code"` server in the same
  tenant; proxied/network tools are not reachable (the sandbox stays isolated).
- **No confirmation-gated tools.** A tool that requires human confirmation
  cannot be called programmatically.
- **Bounded.** Nesting depth (`SANDBOX_TOOL_CALL_MAX_DEPTH`) and a per-invocation
  call budget (`SANDBOX_TOOL_MAX_CALLS_PER_INVOCATION`) prevent runaway or cyclic
  fan-out — a tool that (transitively) calls itself fails closed at the limit.

A denied call raises a clear error inside your function (`forbidden`,
`confirmation_required`, `tool_not_callable`, `tool_call_depth_exceeded`, ...),
which you can catch like any exception.

## Export: run your server outside the gateway

In Admin Studio, **Edit** a `transport="code"` server and click
**Export server (.zip)** to download a self-contained
[FastMCP](https://github.com/jlowin/fastmcp) project. It runs the exact source
you authored, with `context` reconstructed locally:

- `context.db` → a real MongoDB database (`pymongo`); set `MONGODB_URI` /
  `MONGODB_DB`.
- `context.env` → process environment values (see `.env.example` — **secrets are
  never exported**, only key names).
- `context.tools` / `context.call` → sibling tools resolved **in-process**. The
  exporter statically follows your `context.tools` references and bundles the
  whole dependency closure (even tools on other servers in your tenant), so
  `tool_a` calling `tool_b` keeps working. A depth guard
  (`MCP_TOOL_CALL_MAX_DEPTH`) fails cyclic calls closed.

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in MONGODB_URI + any context.env values
python server.py       # stdio MCP server (see README.md for HTTP)
```

Because the runtime mirrors the gateway's return contract (`ObjectId` → string,
`datetime` → ISO-8601 UTC), a tool's output is identical whether it runs in the
sandbox or the exported server.

## Action-Type Security Gating

The server enforces allowed DB operations based on your tool metadata:

- `read`: read operations only
- `write`: read + insert/update
- `destructive`: read + insert/update + delete

This policy is host-enforced. Even if code attempts a disallowed operation, the
bridge rejects it.

## Extended JSON Helpers

The bridge round-trips MongoDB types using Extended JSON automatically. Build
typed queries with `context.db.ObjectId(...)`; native `datetime` values are
serialized for you.

```python
def by_id(user_id: str) -> dict:
    return context.db.users.find_one({"_id": context.db.ObjectId(user_id)}) or {}
```

### BSON-aware return values

You can return MongoDB documents, `ObjectId`s, and `datetime` values straight
out of your function — the sandbox serializes them to JSON for you (`ObjectId`
→ string, `datetime` → ISO-8601 UTC). No manual `str(...)` conversion needed.

> In Admin Studio, the **What is `context`?** button (in the function editor)
> opens a live, copy-paste guide to everything above.

## Click Tracker Example (`context.db` + `context.env`)

```python
from datetime import datetime, timezone


def track_click(target: str, source: str = "web") -> dict:
    item = (target or "").strip()
    if not item:
        raise ValueError("target is required")
    label = context.env.get("CLICK_LABEL", "anonymous")
    context.db.clicks.insert_one(
        {
            "target": item,
            "source": (source or "web").strip() or "web",
            "label": label,
            "created_at": datetime.now(timezone.utc),
        }
    )
    total = context.db.clicks.count_documents({"target": item})
    return {"target": item, "count": int(total), "label": label}
```

## Admin Studio: Explore Database

In the Admin UI function editor, use **Explore Database** to:

- list tenant collections
- sample docs and inferred field types
- run read-only `find`/`aggregate` queries
- copy/insert generated `context.db[...]` snippets

This is the fastest way to discover query shape while authoring tools.

## Important Runtime Notes

- The sandbox has no raw network/sockets. Every external interaction (DB, sibling
  tools, outbound HTTP) is relayed through the trusted host process.
- DB operations are relayed through the host process and scoped to your tenant.
- Bridge behavior is controlled by `SANDBOX_DB_BRIDGE_ENABLED` (enabled in local dev examples, opt-in for production).
- DB limits are controlled by:
  - `SANDBOX_DB_MAX_DOCS`
  - `SANDBOX_DB_QUERY_TIMEOUT_MS`
  - `SANDBOX_DB_MAX_CALLS_PER_INVOCATION`
  - `SANDBOX_DB_MAX_RESULT_BYTES`
- Cross-tool calling (`context.tools`) is controlled by:
  - `SANDBOX_TOOL_BRIDGE_ENABLED` (opt-in, off by default)
  - `SANDBOX_TOOL_CALL_MAX_DEPTH`
  - `SANDBOX_TOOL_MAX_CALLS_PER_INVOCATION`
  - `SANDBOX_TOOL_MAX_RESULT_BYTES`
- Outbound HTTP (`context.http`) is controlled by:
  - `SANDBOX_HTTP_BRIDGE_ENABLED` (opt-in, off by default)
  - the egress allowlist: `EGRESS_GLOBAL_ALLOWLIST` (platform ceiling) intersected
    with the per-tenant egress allowlist. **Always deny-by-default** for code —
    an empty effective allowlist blocks every host, regardless of
    `EGRESS_ALLOWLIST_ENABLED`.
  - `SANDBOX_HTTP_TIMEOUT_MS`, `SANDBOX_HTTP_MAX_CALLS_PER_INVOCATION`,
    `SANDBOX_HTTP_MAX_RESPONSE_BYTES`, `SANDBOX_HTTP_MAX_REQUEST_BYTES`
  - `SANDBOX_HTTP_BREAKER_FAILURES` / `SANDBOX_HTTP_BREAKER_RESET_SECONDS`
  - `SANDBOX_HTTP_MAX_CONCURRENCY_PER_TENANT` / `SANDBOX_HTTP_MAX_GLOBAL_CONCURRENCY`

## `context.http` Outbound HTTP

Opt-in (`SANDBOX_HTTP_BRIDGE_ENABLED=true`). The wasm jail still has no sockets;
each call is relayed to the host and made through the gateway's egress firewall
(SSRF denylist + global ceiling ∩ tenant allowlist + IP pinning, re-validated on
every redirect). **https only.**

```python
def fx(base: str = "USD") -> dict:
    resp = context.http.get("https://api.example.com/rates", params={"base": base})
    if not resp.ok:
        raise ValueError(f"upstream {resp.status}")
    return resp.json()
```

Secrets are injected host-side — pass the *name* of a per-server secret, never
the value:

```python
# attaches "Authorization: Bearer <EXAMPLE_TOKEN>" on the host; the value never
# enters your code, the URL, logs, or the response.
context.http.get("https://api.example.com/me", auth="EXAMPLE_TOKEN")
# custom header: auth={"key": "EXAMPLE_TOKEN", "header": "X-Api-Key", "scheme": ""}
```

Methods follow the tool's `action_type`: `read` tools may use `get`/`head`;
`write`/`destructive` tools may also `post`/`put`/`patch`/`delete` (send a body
with `json=` or `data=`). The response exposes `.status`, `.ok`, `.headers`,
`.text`, `.content`, and `.json()`. Blocked calls raise a typed error
(`egress_blocked`, `http_scheme_forbidden`, `http_method_forbidden`,
`http_auth_unknown_key`, `http_response_too_large`, `http_call_limit`,
`http_timeout`, …) you can `try/except`.

## Authoring Checklist

- Match function name to tool name.
- Keep output JSON-serializable.
- Pin requirements (`package==version`) when needed — but each package must also be
  **allowed for your tenant**. The Functions Studio shows a chip per requirement:
  green = installs, amber "awaiting operator" = in your tenant policy but not yet in the
  platform ceiling, red "not allowed" = ask a tenant admin to add it under **Code
  packages**. A tool with a disallowed package cannot be saved or run. Empty tenant policy
  ⇒ standard library only.
- Choose the narrowest correct `action_type`.
- Use Explore Database to generate and validate query snippets.

