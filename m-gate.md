# The Shape of a Request

> A story about the Model Context Protocol — from the oldest idea in computing to the database that was already shaped to hold it.

Somewhere between an agent's first useful thought and the tool it needed to act on, a quiet inevitability took root.

The agent can reason. It can plan. It can write you a sonnet about your quarterly burn rate. But it cannot *do* anything — not check the weather, not look up an order, not file the ticket — until it asks something else to act on its behalf. And the moment it asks, a question older than the cloud, older than the web, older than the relational database itself comes back into focus:

**What is the shape of a request?**

---

## Chapter I — The Contract

### request in, response out

Strip the Model Context Protocol of every acronym and launch-day adjective and you are left with the most ancient pattern in all of computing: one side asks, the other side answers. A client sends a request. A server returns a response. That's it. That's the whole foundation.

Imagine you're building an AI agent. It's good — genuinely good — at deciding *what* to do. But it lives in a sealed room. To reach the weather service, the order database, the internal wiki, it needs a doorway. Before MCP, every doorway was hand-carved: a bespoke function-calling schema here, a custom HTTP wrapper there, a brittle prompt that begged the model to emit the right JSON. Every new tool was a small integration project. Every new model was a rewrite.

MCP rejected the bespoke door. It said: there will be *one* handshake, and everyone uses it. An agent connects and says `initialize` — *who are you, what can you do?* The server answers with its capabilities. The agent says `tools/list` — *show me the menu.* The server returns the catalog. The agent picks one and says `tools/call` — *do this, with these arguments.* The server does it and returns the result.

Three verbs. A menu, a selection, an answer. When Anthropic introduced MCP in late 2024 and the ecosystem detonated through 2025 — hundreds of servers, every major model adopting it — developers didn't flock to it because it was clever. They flocked to it because the friction of connecting a mind to the world finally *vanished*. The handshake was the same whether the tool was a GitHub server or a coffee machine.

But a contract is only a promise. A promise needs a *form* — something you can put on a wire, parse on the other end, and never misunderstand. The handshake needed a shape.

---

## Chapter II — The Shape

### {jsonrpc: "2.0"}

Here the protocol made a choice that would echo all the way down to the storage layer, though almost nobody noticed at the time.

It could have chosen rows and columns — a tidy, tabular request format. It could have chosen a bespoke binary frame, fast and opaque. It could have leaned on the heavyweight RPC machinery the enterprise world already had. Instead, MCP reached for the format the entire web already spoke fluently: **JSON-RPC 2.0**. A request is a small, self-describing envelope. A method, an id, and a `params` object whose shape depends entirely on what you're asking for.

Here is an agent asking a weather tool to do its job:

```json
{
  "jsonrpc": "2.0",
  "id": "call-1",
  "method": "tools/call",
  "params": {
    "server": "weather",
    "name": "get_current_weather",
    "arguments": { "city": "Montreal", "unit": "celsius" }
  }
}
```

Look at `arguments`. It's nested. It's free-form. The *next* tool's arguments will look nothing like this one — an order lookup wants an order id, a wiki search wants a query string, a deploy tool wants a whole config object with a dozen fields. The tool definitions the server hands back in `tools/list` are themselves nested JSON Schemas, schemas-within-schemas describing inputs you've never seen. An error isn't a status code; it's `{ "code": -32603, "message": "...", "data": { ... } }` — structured, nested, self-describing.

Now try to put this in a table.

You feel the wall immediately. `arguments` has no fixed columns — every tool would demand its own. You could flatten it into a hundred sparse columns and migrate the schema every time a new server appears. You could surrender and stuff the whole thing into a `JSONB` blob your relational engine treats as an opaque guest. Either way you're fighting the storage layer to accept the way the protocol actually works. The shape *resists* rows and columns. It always will, because the shape was never tabular.

But the shape is familiar. You've seen it before. Nested, flexible, self-describing, deep. That envelope on the wire — it isn't really a message at all.

It's a document.

---

## Chapter III — The Perfect Fit

### the shape in the protocol matched the shape in the database

This is the quiet inevitability, the hinge the whole story turns on.

A JSON-RPC message does not need to be *translated* into storage. It does not need an ORM to marshal it, a migration to accommodate it, or a flattening pass to file it into columns. It is already the native unit of a document database. You store the request as it arrived. You store the tool definition exactly as the server advertised it. You store the response exactly as it came back. The same shape on the wire, at rest.

The tool catalog the gateway searches — the menu of everything every server can do — is just a collection of documents that look like this:

```json
{
  "name": "get_current_weather",
  "server": "weather",
  "description": "Get the current weather for a city",
  "inputSchema": { "city": "string", "unit": "string" },
  "scopes": ["weather", "readonly"],
  "embedding": [0.0231, -0.0142, 0.0087, "… 768 dims"]
}
```

Set that beside the `tools/call` envelope from the last chapter. They are the *same kind of object* — nested, self-describing, free to differ from their neighbors. There is no impedance mismatch to bridge because there is no mismatch. The protocol speaks JSON; the document speaks JSON; the conversation needs no interpreter.

And watch what that buys you. A new MCP server arrives tomorrow with a twelve-field configuration object and a tool whose arguments are shaped like nothing in your catalog. In the tabular world that's a migration: write it, test it against a million rows, hold your breath, redeploy. In the document world you just — drop it in. No `ALTER TABLE`. No ORM ceremony. No schema meeting. The registry of servers, the catalog of tools, the ephemeral session state, the audit record of every call ever made — all of them are documents, because all of them were *born* as JSON the moment they crossed the wire.

The friction is gone for the same reason it was gone in the handshake: the data in the protocol finally matched the data in the database.

So now the requests are documents. The tools are documents. They sit, by the thousand, in one collection. Which raises the question that turns a store into a gateway: when an agent needs *one* tool out of two hundred, how does it find the right one?

---

## Chapter IV — The Meaning

### searched by intent, found by name

Here is where the document model stops merely *holding* the protocol and starts making it smarter.

Picture your agent again, but successful now. It has two hundred tools across a dozen servers. The naive move is to hand the model the entire menu on every single turn — all two hundred definitions, every nested schema, dumped into the context window before the agent does one useful thing. That's twenty thousand tokens of throat-clearing per request. It's slow, it's expensive, and it drowns the model in options it will never use.

The fix is to stop treating tool selection as a *list* and start treating it as a *search*. Embed every tool's description into a vector — a coordinate in meaning-space. When the agent says *"I need to check if it's going to rain,"* turn that intent into a vector too, and ask the catalog for its nearest neighbors. Hand back the five tools that actually matter. Route by meaning. The token bill collapses by an order of magnitude, and you never touched a single tool's text.

But semantic search has a blind spot, and it's a cruel one. Vectors are brilliant at *intent* and clumsy with *literals*. Ask a purely semantic router to *"call `get_current_weather`"* or *"look up order `A-417`"* and the embedding can sail right past the exact token you typed — cosine similarity rewards meaning, not spelling. The instant your tools have identifier-shaped names and your users quote error codes and IDs (they always do), pure semantic routing starts quietly returning the *plausible* tool instead of the *right* one.

So you need the other half: lexical search, BM25, the keyword engine that nails exact tokens and is gloriously blind to intent. Run both. Fuse the rankings. Now keyword precision and semantic recall cover for each other's failures — this is **hybrid search**, and it is the difference between a router that usually works and one you can trust.

And here is where every other architecture flinches. Built the conventional way, hybrid search means standing up a vector database *and* a search engine *and* a sync pipeline to keep the two stores agreeing on what exists — three systems, in uneasy lockstep, to answer one question. The moment a write lands in one store but not the other, your keyword index and your vector index disagree about reality, and your "right tool, ranked" promise develops holes.

The document model never had to flinch, because it already saw what an embedding *is*. A vector is just an array of floats. Native JSON. It doesn't belong in a separate silo any more than a city name does — it belongs in the same document as the description it describes, beside the scopes and the schema. The text index and the vector index read the *same* documents. And a single query fuses both arms server-side, in one round trip:

```js
db.tool_catalog.aggregate([
  { $rankFusion: {
      input: { pipelines: {
        vectorPipeline:   [ { $vectorSearch: { index: "hybrid-vector-search", path: "embedding",
                                                queryVector: embed(query), numCandidates: 100, limit: 20 } } ],
        fullTextPipeline: [ { $search: { index: "hybrid-full-text-search",
                                         text: { query: query, path: ["name", "description", "server"] } } },
                            { $limit: 20 } ]
      } },
      combination: { weights: { vectorPipeline: 0.5, fullTextPipeline: 0.5 } }
  } },
  { $project: { name: 1, description: 1, score: { $meta: "score" } } },
  { $limit: 5 }
])
```

One stage. One collection. Reciprocal Rank Fusion merging meaning and spelling by rank position, so an unbounded keyword score never has to be reconciled against a bounded cosine one. No second store. No client-side merge. No sync job reconciling two indexes that drift apart at 3 a.m. The intent, the literal, the metadata that says who's allowed to call it — all evaluated together, because they were all just fields on the same document.

Meaning didn't require a new database. It required the one whose unit of storage was already the unit of the protocol.

---

## Chapter V — One Backend

### registry, config, search, memory — one document, viewed four ways

Step back far enough and the gateway between an agent and its tools needs exactly four things. It needs a **registry** — what servers and tools exist, and what their interfaces look like. It needs **config** — the policies, the scopes, the routing rules. It needs **search** — the means to pull the right subset out on demand. And it needs **memory** — the telemetry, the audit trail, the record of every decision so the system can learn from itself.

The tabular world makes that four systems: a relational database for the registry, a key-value store for config, a search engine plus a vector database for retrieval, a time-series warehouse for analytics. Four engines, four scaling models, four backup strategies, four mental models — held together by sync jobs that can always drift. The cost was never any one of them. The cost was the connective tissue between them.

But a tool's registry entry, the policy attached to it, the indexes you search it by, and the telemetry it emits are not four kinds of data. They are one document, viewed four ways. The fragmentation was never inherent to the problem — it was an artifact of storing one shape across four engines that each understood only a slice of it.

Collapse them back into the model they always shared and the payoff isn't a feature you bought — it's surface area you no longer have to operate. Scope becomes a filter on the *same* hybrid query that does the routing — a tool a caller isn't entitled to never becomes a candidate. And because that entitlement is *data* — checked in the query, never whispered to the model and trusted to its good intentions — no amount of clever prompting talks an untrusted agent past it; the boundary lives in the document, where it cannot be argued with. Keeping the catalog fresh becomes a bulk upsert into the *same* collection. The audit trail stops being mere forensics and becomes a labeled dataset: every query, every tool returned, every call's outcome — the raw material to make the next routing decision better, sitting in the same database you already query. Same documents, same engine, the whole way up.

The protocol was a contract. The contract took the shape of JSON. And a store whose native unit *is* JSON was never going to be a compromise. It was always going to be the obvious home.

---

## The Unbroken Thread

A tool call is a document. A tool definition is a document. An input schema is nested JSON. An embedding is an array of floats — native JSON. A scope is a field. An error is a structured object. An audit record is a document in motion. Every single layer of the Model Context Protocol — from the first `initialize` handshake to the last fused search result — is JSON, and JSON is the document, natively, in a way that rows and columns can only ever approximate.

The relational world can bolt on `JSONB` columns and clever bridges and call it parity. But the document was never a feature retrofitted onto the protocol's storage. It *was* the storage. MCP didn't have to bend to fit the database, and the database didn't have to bend to fit MCP. They were the same shape from the start.

What is the shape of a request? It is the shape of a document.
And the place a document was always meant to live is the database that speaks it as a first language.

---
