# Queryable Encryption Caveats — Hybrid Search, `$rankFusion`, and Auto‑Encryption

> Companion to [`docs/QUERYABLE-ENCRYPTION.md`](docs/QUERYABLE-ENCRYPTION.md) (the setup/ops
> guide). This document is the **"sharp edges" reference**: what breaks when MongoDB
> Queryable Encryption (QE) and Atlas Search / Vector Search / `$rankFusion` meet, *why*
> it breaks, how bad it is, and the exact options to make native hybrid search work.

---

## TL;DR

- **You are not "rank‑fusing an encrypted field."** Catalog search runs on
  `tool_catalog`, whose searched fields (`name`, `description`, `embedding`) are
  **plaintext**. The QE‑encrypted fields live in a *different* collection
  (`routing_registry`) that search never touches.
- The real blocker is **not** the data — it's the **client**. A driver configured for
  **automatic encryption** runs *every* outgoing command through `crypt_shared`'s
  allow‑listed query analysis. That analysis cannot resolve the namespaces inside
  `$rankFusion`'s nested sub‑pipelines, so it errors **before the command ever reaches
  the server** — even on a collection with zero encrypted fields.
- **Single‑stage `$vectorSearch` and `$search` analyze fine** under QE (we run them
  live). Only the **compound `$rankFusion`** trips analysis.
- **The fallback is not the only way — and the gateway now does the better thing.** As of
  this change, catalog search runs through a scoped **`bypass_auto_encryption` client**, so
  **native server‑side `$rankFusion` executes under QE**. The app‑side reciprocal‑rank‑fusion
  (RRF) path is retained purely as a **resilience fallback**.
- **Severity: low** even without the native path. App‑side RRF gives *identical ranking*,
  two round trips instead of one, with no security or correctness impact — the only thing
  it loses is the "single native query" property and server‑emitted `scoreDetails`.

---

## 1. Three different "encryptions" live in this gateway — don't conflate them

Most confusion here comes from collapsing three independent mechanisms into one. They are
not the same and they fail for different reasons.

| # | Mechanism | What it protects | Where | Searchable? |
|---|-----------|------------------|-------|-------------|
| 1 | **Queryable Encryption (QE)** — auto field‑level encryption | Downstream registry secrets: `env`, `command`, `args`, `metadata` | `routing_registry` collection | No — never indexed by Atlas Search |
| 2 | **Atlas Search + Vector Search indexes** on **plaintext** | Tool routing metadata: `name`, `description`, `embedding` (Voyage vectors) | `tool_catalog` collection | Yes — this is where `tools/search` runs |
| 3 | **App‑level secret encryption** (Fernet / per‑tenant DEK) | Embedding provider **API keys at rest** (e.g. `VOYAGE_API_KEY`) | `gateway_config` docs | N/A — opaque blob, never queried |

The QE field set is declared once:

```18:25:database/encryption.py
ROUTING_REGISTRY_ENCRYPTED_FIELDS: dict[str, Any] = {
    "fields": [
        {"path": "env", "bsonType": "object", "keyId": None},
        {"path": "command", "bsonType": "string", "keyId": None},
        {"path": "args", "bsonType": "array", "keyId": None},
        {"path": "metadata", "bsonType": "object", "keyId": None},
    ]
}
```

**Key takeaway:** hybrid search (mechanism 2) never reads encrypted bytes (mechanism 1).
The two only collide because they share **one MongoDB client**, and that client has
auto‑encryption turned on globally.

---

## 2. The actual failure

### Symptom

`tools/search` with `mode=hybrid` returned a JSON‑RPC `-32603`:

```text
[crypt_shared 8.2.0] "analyze_query" failed:
  No resolved namespace provided for tenant_local_dev_… [Error 2, code 9453000]
```

On the originally pinned MongoDB 8.0 it surfaced earlier and differently:

```text
[crypt_shared 8.0.1] "analyze_query" failed:
  Unrecognized pipeline stage name: '$rankFusion' [Error 2, code 40324]
```

### Root cause (two coupled issues)

1. **Version floor.** `$rankFusion` requires **MongoDB 8.1+** (server *and* the QE
   `crypt_shared` library must both understand the stage). The repo originally pinned
   `mongodb-atlas-local:8.0` + `crypt_shared 8.0.1`, which simply don't know the stage.
   → **Fixed** by bumping to MongoDB **8.3** and `crypt_shared` **8.3.2** (see §6).

2. **Auto‑encryption query analysis.** Even on 8.3, an auto‑encryption client must run
   `crypt_shared.analyze_query` on the command first. `$rankFusion` embeds *named
   sub‑pipelines*:

```177:198:services/hybrid_search.py
    return [
        {
            "$rankFusion": {
                "input": {
                    "pipelines": {
                        "vectorPipeline": [{"$vectorSearch": vector_stage}],
                        "fullTextPipeline": full_text_pipeline,
                    }
                },
                "combination": {
                    "weights": {
                        "vectorPipeline": vector_weight,
                        "fullTextPipeline": text_weight,
                    }
                },
                "scoreDetails": include_score_details,
            }
        },
        {"$project": projection},
        {"$sort": {"score": -1}},
        {"$limit": output_limit},
    ]
```

`crypt_shared`'s analyzer can't resolve the collection namespace for those inner
pipelines, so it fails with `code 9453000` **client‑side**. The command never reaches
mongod. Importantly, the *same* `$vectorSearch` and `$search` stages analyze fine when
issued **as their own top‑level pipelines** — which is exactly why `mode=vector` and
`mode=text` work under QE and only `mode=hybrid` (the compound) does not.

### Why this isn't "QE vs Atlas Search"

MongoDB does document a hard rule: **"Queryable Encryption is incompatible with MongoDB
Atlas Search"** ([QE Limitations](https://www.mongodb.com/docs/v7.0/core/queryable-encryption/reference/limitations/)).
That rule is about **indexing encrypted `BinData`** — Atlas Search can't tokenize opaque
ciphertext. We don't do that: `tool_catalog` is plaintext and is the only thing indexed.
So the hard incompatibility does **not** apply here; our problem is purely the
client‑side **command allow‑list**, per
[Supported Operations for QE](https://www.mongodb.com/docs/manual/core/queryable-encryption/reference/supported-operations/):
*"Issuing any other command [stage] through a compatible driver configured for automatic
encryption returns an error."*

---

## 3. What QE genuinely forbids (the real, permanent limits)

These are not workaround‑able; design around them.

- **No Atlas Search / Vector Search indexes on encrypted fields.** Ciphertext is
  un‑tokenizable and un‑embeddable. If a field must be both *semantically searchable* and
  *encrypted at rest*, QE is the wrong tool for that field.
- **Auto‑encryption supports only an allow‑listed set of stages/operators.** Anything off
  the list errors at analysis time. `$rankFusion`/`$scoreFusion` are not on the
  auto‑encryption analyzable path.
- **No cross‑collection stages under auto‑encryption** (`$lookup`/`$graphLookup` to a
  *different* `from` collection, `$out`, `$merge` to others) — they error.
- **`$rankFusion`/`$scoreFusion` sub‑pipelines may contain only**
  `$search`, `$vectorSearch`, `$match`, `$sort`, `$geoNear`
  ([Hybrid Search docs](https://www.mongodb.com/docs/vector-search/hybrid-search/hybrid-search/)).
- **No time‑series collections under QE** — that's why `audit_telemetry` is intentionally
  not QE‑encrypted.
- **Diagnostics are redacted** on encrypted collections (`$collStats`, `$planCacheStats`,
  query log), which complicates perf analysis.
- **Known open bugs** (track before relying on the native path): a query‑analysis bug
  around `$vectorSearch` under QE, and a `mongod` crash when running
  `$rankFusion`/`$scoreFusion` with an **empty** sub‑pipeline. Reasons to keep a graceful
  fallback even after enabling the native path.

---

## 4. Options to run hybrid under QE (ranked)

| Option | Native `$rankFusion`? | Blast radius | Notes |
|--------|----------------------|--------------|-------|
| **A. `bypass_auto_encryption` client for catalog search** *(IMPLEMENTED — default)* | ✅ Yes | Small, well‑precedented | Disables outgoing‑command **analysis/encryption**; **auto‑decryption of responses still works**. `tool_catalog` has nothing to encrypt, so nothing is lost. |
| **B. App‑side RRF fallback** *(retained as the safety net)* | ❌ No (client‑side fuse) | Zero | Identical ranking; 2 round trips; reconstructed `scoreDetails`. Triggers only if native analysis still fails. |
| **C. `bypassQueryAnalysis=true` globally** | ✅ Yes | Large | Forces **explicit** encryption for every `routing_registry` write across the codebase. Invasive; easy to footgun. Avoid. |
| **D. Disable QE** (`QE_ENABLED=false`) | ✅ Yes | Large | Loses encryption‑at‑rest for registry secrets. Not worth it. |

### Why Option A is safe and correct

The driver spec is explicit: `bypassAutoEncryption` disables outgoing‑command
*encryption/analysis*, while **automatic decryption of returned encrypted fields still
happens** ([client‑side‑encryption spec](https://github.com/mongodb/specifications/blob/master/source/client-side-encryption/client-side-encryption.md)).
The gateway already depends on exactly this behavior for the registry watcher:

```111:127:database/encryption.py
def build_watcher_client(settings: Settings | None = None) -> AsyncMongoClient:
    """Client for the registry change-stream watcher under Queryable Encryption.

    The shared app client auto-encrypts, and libmongocrypt forbids the
    cluster-wide ``aggregate``/``$changeStream`` such a watcher needs ("non-collection
    command not supported for auto encryption: aggregate"). ``bypass_auto_encryption``
    skips the command analysis that imposes that restriction, so the cluster-wide
    change stream is allowed — while automatic *decryption* of the encrypted
    ``routing_registry`` fields in each change event still happens. Decryption uses
    the embedded libmongocrypt, so no crypt_shared/mongocryptd is required here.
    """
```

The search‑specific bypass client mirrors this exactly and is now implemented:

```63:82:database/mongo.py
def get_qe_bypass_client() -> AsyncMongoClient:
    """Long-lived client that bypasses QE auto-encryption *query analysis*.
    ...
    Scope: READS / AGGREGATIONS on non-encrypted collections only (e.g.
    ``tool_catalog`` hybrid search). NEVER use it to WRITE ``routing_registry`` —
    those fields must still be auto-encrypted by the shared client. Only call this
    when ``qe_enabled`` is true (it builds QE auto-encryption options).
    """
    global _qe_bypass_client
    if _qe_bypass_client is None:
        _qe_bypass_client = build_watcher_client(get_settings())
    return _qe_bypass_client
```

Call sites pick the right client transparently — bypass under QE, normal client otherwise:

```152:163:database/mongo.py
def get_tenant_database_for_search(tenant_id: str):
    """Tenant DB handle for Atlas Search / Vector Search / ``$rankFusion`` reads.

    Under QE, the shared client's auto-encryption query analysis can't analyze
    ``$rankFusion`` (see get_qe_bypass_client), so catalog search is routed through
    the bypass client. Without QE the normal client is returned unchanged. Safe
    because ``tool_catalog`` holds no encrypted fields.
    """
    if get_settings().qe_enabled:
        return get_qe_bypass_client()[tenant_db_name(tenant_id)]
    return get_tenant_database(tenant_id)
```

The one invariant to preserve: **never use the bypass client to *write* `routing_registry`**
(writes still need auto‑encryption). It is scoped to reads/aggregations on `tool_catalog`.

---

## 5. What the gateway does today (and why)

**Native‑first, with the fallback as a safety net.** The `$rankFusion` aggregate runs
through the search‑appropriate collection (bypass client under QE, normal client
otherwise). If analysis or execution still fails — *both* the "old server"
(`OperationFailure`) and any residual "QE analysis" (`EncryptionError`) cases — it
degrades to app‑side RRF:

```342:366:services/hybrid_search.py
        # Native server-side $rankFusion (the differentiator). Under QE this MUST run
        # through the bypass-auto-encryption client: the shared client's crypt_shared
        # query analysis can't resolve $rankFusion's sub-pipeline namespaces. tool_catalog
        # has no encrypted fields, so the bypass is safe and decryption still works.
        # See QUERYABLE_ENCRYPTION_CAVEATS.md.
        search_collection = get_tenant_database_for_search(resolved_tenant_id)["tool_catalog"]
        try:
            cursor = await search_collection.aggregate(pipeline)
            return await cursor.to_list(length=effective_limit)
        except (OperationFailure, EncryptionError):
            # GA-safe fallback: run both retrievers and fuse with app-side RRF.
            # OperationFailure covers servers without native $rankFusion; EncryptionError
            # covers the residual QE case (e.g. a non-bypass client, or the known
            # empty-sub-pipeline analysis bug). The single-stage $vectorSearch/$search
            # arms used by the app-side path analyze fine under QE, so hybrid still works.
            return await self._search_hybrid_app_side(
                collection=collection,
                query=query,
                query_vector=query_vector,
                effective_limit=effective_limit,
                vector_weight=vector_weight or self.settings.hybrid_vector_weight,
                text_weight=text_weight or self.settings.hybrid_text_weight,
                allowed_scopes=allowed_scopes,
                server=server,
            )
```

The fallback runs the two arms separately (`$vectorSearch`, then `$search`) — both of
which analyze fine under QE — and fuses by **reciprocal rank** (`weight × 1/(60 + rank)`)
in process, reconstructing the `scoreDetails` receipts.

> Design stance: **keep this fallback even though the native path now works.** It protects
> against the open `$rankFusion` bugs (§3) and any future QE/version drift. Native path =
> fast path; app‑side RRF = correctness floor.

### Telling them apart

Native `$rankFusion` emits `scoreDetails` as a server‑built **object**
(`{value, description, details:[…]}`); the app‑side fallback returns a **list** of
per‑pipeline entries. So a one‑line shape check on a hybrid response confirms which path
served it (the native object's `description` reads *"value output by reciprocal rank
fusion algorithm…"*).

---

## 6. Version requirements (and the bump we made)

`$rankFusion` is **8.1+** on both the server and the QE `crypt_shared` lib. The repo now
pins:

- `docker-compose.yml` → `mongodb/mongodb-atlas-local:8.3` (server; FCV `8.3`)
- `Dockerfile` → `MONGODB_CRYPT_VERSION=8.3.2` (the `crypt_shared` library)

Keep these two **aligned on the same minor**. A server that supports `$rankFusion` paired
with an older `crypt_shared` will still fail analysis with
`Unrecognized pipeline stage name: '$rankFusion'`. After a version change, start from a
clean data volume (or bump FCV) so the cluster's feature‑compatibility version actually
permits the stage.

---

## 7. Severity assessment — "how bad is this?"

**Bottom line: mild** — and with the native path now the default, even this mild cost
only applies on the rare occasions the fallback engages. Were the gateway on Option B
alone:

| Dimension | Impact |
|-----------|--------|
| **Result quality** | **None.** App‑side RRF uses the same two retrievers and the same RRF math; identical ordering to native `$rankFusion`. |
| **Latency** | **Minor.** 2 round trips vs 1, plus fusing ~`hybrid_pipeline_limit` (default 20) docs in Python. Negligible for a tool catalog (hundreds–low‑thousands of tools). |
| **Throughput / scale** | **Minor, grows with size.** Native server‑side merge saves a round trip and ships fewer bytes; matters only at high QPS with large candidate sets. |
| **Feature / story** | **Cosmetic.** Lose the "MongoDB does `$rankFusion` natively in one query" headline; `scoreDetails` are reconstructed app‑side rather than server‑emitted. |
| **Security / correctness** | **None.** Nothing decrypts that shouldn't; encrypted fields stay encrypted; no data leaves the boundary. |

When to invest in Option A: you want the **native single‑query demo**, you're scaling the
catalog into the tens of thousands of tools, or hybrid QPS is high enough that the extra
round trip shows up in p99. Otherwise the fallback is fine to ship.

---

## 8. Reproduce & verify

```bash
# Hybrid under QE — now served by NATIVE server-side $rankFusion via the bypass client
curl -s -X POST http://localhost:8000/rpc -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/search",
       "params":{"query":"refund a customer payment","limit":3,"mode":"hybrid"}}' | jq .

# Confirm the NATIVE path (scoreDetails is an OBJECT with a "details" array; the
# app-side fallback would instead return a LIST):
curl -s -X POST http://localhost:8000/rpc -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/search",
       "params":{"query":"refund a customer payment","limit":1,"mode":"hybrid"}}' \
  | jq '.result.items[0].scoreDetails | if type=="object" then "native $rankFusion" else "app-side RRF" end'

# Vector-only and text-only both analyze fine under QE
curl -s -X POST http://localhost:8000/rpc -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"2","method":"tools/search",
       "params":{"query":"cancel my shipment","limit":3,"mode":"vector"}}' | jq .

# QE health
curl -s http://localhost:8000/health/ready | jq '.checks, .qe'
```

A native hybrid response's `scoreDetails` is an object whose `description` reads
*"value output by reciprocal rank fusion algorithm…"*, with a `details` array carrying the
**raw per‑arm scores** (BM25 for `fullTextPipeline`, cosine for `vectorPipeline`) — proof
both retrievers ran and were fused server‑side in a single query.

---

## 9. References

- [QE — Supported Operations](https://www.mongodb.com/docs/manual/core/queryable-encryption/reference/supported-operations/)
- [QE — Limitations](https://www.mongodb.com/docs/v7.0/core/queryable-encryption/reference/limitations/)
- [QE — MongoClient options (`bypassAutoEncryption`, `bypassQueryAnalysis`)](https://www.mongodb.com/docs/v8.3/core/queryable-encryption/reference/qe-options-clients/)
- [Client‑Side Encryption driver spec](https://github.com/mongodb/specifications/blob/master/source/client-side-encryption/client-side-encryption.md)
- [Hybrid Search — `$rankFusion` / `$scoreFusion`](https://www.mongodb.com/docs/vector-search/hybrid-search/hybrid-search/)
- This repo: [`docs/QUERYABLE-ENCRYPTION.md`](docs/QUERYABLE-ENCRYPTION.md), [`VOYAGE-AI.md`](VOYAGE-AI.md), [`services/hybrid_search.py`](services/hybrid_search.py), [`database/encryption.py`](database/encryption.py)
