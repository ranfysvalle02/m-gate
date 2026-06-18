# Things to Lookout For

This document catalogs critical architectural edge cases, known failure modes, and debugging lessons learned while building the MongoDB MCP Gateway.

## 1. ASGI Middleware and Streaming Endpoints (The Infinite Loop Deadlock)

**Symptom:**
The entire Uvicorn server instantly deadlocks (CPU hits 100%, all requests hang) the moment an MCP client (like Cursor) attempts to connect using the SSE (Server-Sent Events) transport.

**Root Cause:**
This is caused by a lethal interaction between custom ASGI middleware that inspects request bodies and the `sse-starlette` library's client-disconnect detection.

1. **The Guardrail Intercept:** A security middleware (like `GuardrailsMiddleware`) attempts to read the entire request body into memory to scan it for malicious content. To pass the body down the ASGI chain without consuming it permanently, the middleware overrides the low-level `receive` channel with a wrapper that immediately returns the cached body (e.g., `{"type": "http.request", "body": b"", "more_body": False}`).
2. **The Disconnect Monitor:** When a client connects via SSE, the `sse-starlette` library spins up a background background task (`_listen_for_disconnect`) containing a `while True:` loop. This loop continuously awaits the `receive` channel, looking for an `http.disconnect` event to know when the client has dropped the persistent connection.
3. **The Deadlock:** Because the middleware's wrapped `receive` channel returns `{"type": "http.request", ...}` *instantly* instead of yielding back to the event loop to wait for real network bytes, `sse-starlette`'s `while True:` loop spins infinitely. It never pauses, starving the Python `asyncio` event loop and completely locking up the server.

**The Solution:**
Never attempt to buffer or inspect the request body of streaming endpoints (like Server-Sent Events or WebSockets) using global ASGI middleware. 

In `GuardrailsMiddleware`, we explicitly bypass the MCP transport routes because the FastMCP protocol handles its own streaming and chunking natively:

```python
# Guard only the custom JSON-RPC transport surface. FastMCP endpoints (/mcp)
# handle streaming and chunking natively, so we cannot safely buffer them.
if not request.url.path.startswith("/rpc"):
    return await self.app(scope, receive, send)
```

**Takeaway:**
When writing ASGI middleware, if you modify or wrap the `receive` channel, you must perfectly emulate the asynchronous waiting behavior of the underlying server (Uvicorn). If your mock `receive` returns immediately in a loop, you will crash the server.

## 2. MongoDB Queryable Encryption (QE) and `hostInfo`

**Symptom:**
Rate limiting and clock-sync routines hang, eventually throwing a `pymongo.errors.ServerSelectionTimeoutError` when trying to execute `admin.command("hostInfo")`.

**Root Cause:**
When MongoDB Queryable Encryption (QE) is enabled, the primary `AsyncMongoClient` is configured with `auto_encryption_opts`. This requires all commands to pass through `mongocryptd` or `crypt_shared` for query analysis. The encryption layer can sometimes reject or hang on certain administrative or unencrypted commands (like `hostInfo` or complex aggregation stages like `$rankFusion`).

**The Solution:**
Use a dedicated "QE-bypass" client for operations that do not involve encrypted fields. In our architecture, `get_qe_bypass_client()` provides a long-lived client with `bypass_auto_encryption=True`.

```python
# Correct way to fetch hostInfo when QE might be enabled
client = get_qe_bypass_client() if get_settings().qe_enabled else get_client()
host_info = await client.admin.command("hostInfo")
```

**Takeaway:**
Do not assume a MongoDB client configured for Queryable Encryption can run all standard commands natively. Isolate read-only search operations and administrative commands to a bypass client.