# Function Runtime Context

This document is for authors building `transport="code"` tools in the gateway.

## What You Get In Function Code

Your function runs in a wasm sandbox and can use:

- Python builtins + standard library (subject to sandbox lint policy)
- `context` runtime helper object

Key runtime helpers:

- `context.db` for tenant-scoped database access (host-relayed, no DB creds in sandbox)
- `context.env` for per-server encrypted environment values
- `context.ObjectId(...)` and `context.utcnow()` for BSON/time helpers

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

## `context.env` Virtual Environment

Each MCP server can define encrypted env values from Admin Studio (Secrets tab).
At runtime, code tools on that server can read them via `context.env`.

```python
def current_label() -> dict:
    return {"label": context.env.get("CLICK_LABEL", "anonymous")}
```

Values are encrypted at rest and never returned by admin APIs after write.

## Action-Type Security Gating

The server enforces allowed DB operations based on your tool metadata:

- `read`: read operations only
- `write`: read + insert/update
- `destructive`: read + insert/update + delete

This policy is host-enforced. Even if code attempts a disallowed operation, the
bridge rejects it.

## Extended JSON Helpers

The bridge round-trips MongoDB types using Extended JSON.

Helpers available in context:

- `context.ObjectId("...")`
- `context.utcnow()`

Example:

```python
def by_id(user_id: str) -> dict:
    return context.db.users.find_one({"_id": context.ObjectId(user_id)}) or {}
```

## Click Tracker Example (`context.db` + `context.env`)

```python
def track_click(target: str, source: str = "web") -> dict:
    item = (target or "").strip()
    if not item:
        raise ValueError("target is required")
    label = context.env.get("CLICK_LABEL", "anonymous")
    created_at = context.utcnow()
    context.db.clicks.insert_one(
        {"target": item, "source": (source or "web").strip() or "web", "label": label, "created_at": created_at}
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

- The sandbox is network-isolated.
- DB operations are relayed through the host process and scoped to your tenant.
- Bridge behavior is controlled by `SANDBOX_DB_BRIDGE_ENABLED` (enabled in local dev examples, opt-in for production).
- Limits are controlled by:
  - `SANDBOX_DB_MAX_DOCS`
  - `SANDBOX_DB_QUERY_TIMEOUT_MS`
  - `SANDBOX_DB_MAX_CALLS_PER_INVOCATION`
  - `SANDBOX_DB_MAX_RESULT_BYTES`

## Authoring Checklist

- Match function name to tool name.
- Keep output JSON-serializable.
- Pin requirements (`package==version`) when needed.
- Choose the narrowest correct `action_type`.
- Use Explore Database to generate and validate query snippets.

