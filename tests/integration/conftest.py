"""Fixtures for the integration tier.

Unlike the unit suite (which uses in-memory fakes), these tests run against a
REAL MongoDB Atlas Local engine — the only thing that can actually execute
``$rankFusion`` / ``$vectorSearch`` / ``$search`` — plus a real embedding
provider (Ollama).

Engine provisioning is airtight by construction:

  * By default the session **owns its own** ``mongodb/mongodb-atlas-local``
    container via testcontainers, pinned to a fixed tag (never ``:latest`` /
    ``:preview``, which move underneath you). It is verified to be genuinely
    Atlas (search-index management responds), bootstrapped into an **isolated
    database**, and torn down at the end. It never touches a shared cluster or
    pre-existing data.
  * If ``INTEGRATION_MONGODB_URI`` is set (CI service container, or an existing
    local stack), that engine is used instead — but it is still *verified* to be
    Atlas, and a unique throwaway database name is used so the suite is
    self-contained either way.

If Docker (and testcontainers) is unavailable AND no URI override is given, the
whole tier ``skip``s cleanly, so it is a no-op on a bare laptop but a hard gate
wherever the engine can be provisioned. Ollama is probed the same way.

Run it with::

    ollama pull nomic-embed-text          # embedding model (host Ollama)
    pytest -m "integration or load"        # testcontainers starts Atlas for you

…or point at your own stack::

    docker compose up -d mongodb
    INTEGRATION_MONGODB_URI=mongodb://localhost:27017/?directConnection=true \
        pytest -m "integration or load"
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import NoReturn

import httpx
import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Pinned Atlas Local image. A fixed minor tag keeps the engine deterministic;
# bump deliberately, never float on :latest/:preview.
ATLAS_LOCAL_IMAGE = os.environ.get("INTEGRATION_ATLAS_IMAGE", "mongodb/mongodb-atlas-local:8.0")
# A unique DB per run so the suite is fully self-contained and never collides
# with application data on a shared cluster.
INTEGRATION_DB_NAME = f"itest_{uuid.uuid4().hex[:10]}"

# Ollama defaults to the host daemon's published port. Overridable for CI.
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")


def _unavailable(reason: str) -> NoReturn:
    """Skip the integration tier — unless INTEGRATION_STRICT=1 is set.

    On a bare laptop (no Docker / no Ollama) the tier should skip cleanly so the
    inner loop stays fast. But ``make ci`` sets ``INTEGRATION_STRICT=1`` to turn
    a missing engine into a HARD FAILURE: the whole point of ``make ci`` is to
    reproduce the cloud gate locally, and a silently-skipped tier is just
    another way to "miss the failure" until it shows up in CI.
    """
    if os.environ.get("INTEGRATION_STRICT") == "1":
        pytest.fail(
            f"INTEGRATION_STRICT=1 but the integration tier cannot run: {reason}",
            pytrace=False,
        )
    pytest.skip(reason, allow_module_level=True)


def _verify_atlas_search(uri: str, *, timeout_ms: int = 2500) -> None:
    """Raise if ``uri`` is not a reachable, *fully search-capable* Atlas engine.

    Reachability + ``list_search_indexes`` proves it is Atlas (a plain ``mongod``
    raises). But on a freshly-started container the ``mongot`` search runner
    comes up *after* ``mongod`` accepts connections, so index *creation* can
    still fail with "Error connecting to Search Index Management service"
    (code 125) for a few seconds. We therefore verify the full write path:
    create a throwaway search index and drop it. Only when that succeeds is the
    engine truly ready for the suite's DDL.
    """
    from pymongo import MongoClient
    from pymongo.operations import SearchIndexModel

    client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms, directConnection=True)
    try:
        client.admin.command("ping")
        coll = client[INTEGRATION_DB_NAME]["__readiness_probe__"]
        list(coll.list_search_indexes())  # Atlas discriminator (read path)
        # Write path: a real index create/drop exercises the mongot service.
        coll.insert_one({"_probe": 1})
        coll.create_search_index(
            model=SearchIndexModel(
                name="__readiness__",
                type="search",
                definition={"mappings": {"dynamic": True}},
            )
        )
        coll.drop()  # cleans up the probe collection + its index
    finally:
        client.close()


def _probe_ollama(base_url: str, model: str) -> str | None:
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=2.5)
        resp.raise_for_status()
        names = {m.get("name", "") for m in resp.json().get("models", [])}
        # Ollama reports e.g. "nomic-embed-text:latest"; match on the base name.
        if any(n.split(":")[0] == model.split(":")[0] for n in names):
            return None
        return f"Ollama model '{model}' not pulled (have: {sorted(names)})"
    except Exception as exc:  # noqa: BLE001
        return f"Ollama not reachable at {base_url}: {exc}"


def _wait_for_atlas_search(uri: str, attempts: int = 60) -> str | None:
    """Poll until the engine answers AND search-index management is live.

    Returns None on success, or a human-readable reason string on timeout.
    """
    last = "unknown error"
    for _ in range(attempts):
        try:
            _verify_atlas_search(uri, timeout_ms=1500)
            return None
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:160]
            import time

            time.sleep(1)
    return last


@pytest.fixture(scope="session")
def atlas_uri():
    """Resolve a verified Atlas Local URI, owning a container unless overridden.

    Resolution order:
      1. INTEGRATION_MONGODB_URI env var -> verify it is Atlas, use it.
      2. testcontainers -> start a pinned atlas-local image, verify, tear down.
      3. Otherwise -> skip the whole tier.
    """
    override = os.environ.get("INTEGRATION_MONGODB_URI")
    if override:
        reason = _wait_for_atlas_search(override)
        if reason:
            _unavailable(
                f"INTEGRATION_MONGODB_URI={override} is not a search-capable Atlas engine: {reason}"
            )
        yield override
        return

    try:
        from testcontainers.core.container import DockerContainer
    except Exception as exc:  # noqa: BLE001
        _unavailable(
            "No INTEGRATION_MONGODB_URI set and testcontainers is unavailable "
            f"({exc}); cannot provision Atlas Local."
        )

    try:
        container = DockerContainer(ATLAS_LOCAL_IMAGE).with_exposed_ports(27017)
        container.start()
    except Exception as exc:  # noqa: BLE001 - Docker not running, image pull blocked, etc.
        _unavailable(
            f"Could not start {ATLAS_LOCAL_IMAGE} via testcontainers ({exc}); is Docker running?"
        )

    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(27017)
        uri = f"mongodb://{host}:{port}/?directConnection=true"
        reason = _wait_for_atlas_search(uri)
        if reason:
            _unavailable(f"Atlas Local container did not become search-ready: {reason}")
        yield uri
    finally:
        container.stop()


@pytest.fixture(scope="session")
def settings(atlas_uri):
    """Application settings pointed at the owned engine + isolated DB.

    Set before get_settings() is first resolved here, and the cache is cleared
    so the whole integration tier observes this configuration.
    """
    os.environ["MONGODB_URI"] = atlas_uri
    os.environ["MONGODB_DB_NAME"] = INTEGRATION_DB_NAME
    from config.settings import get_settings

    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        get_settings.cache_clear()
        # Drop the whole isolated database so nothing lingers on a shared
        # cluster (the owned-container path is torn down regardless, but the
        # INTEGRATION_MONGODB_URI override path must leave no trace).
        try:
            from pymongo import MongoClient

            client = MongoClient(atlas_uri, serverSelectionTimeoutMS=3000, directConnection=True)
            client.drop_database(INTEGRATION_DB_NAME)
            client.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


@pytest.fixture(scope="session")
def require_ollama(settings):
    reason = _probe_ollama(settings.ollama_base_url, settings.ollama_model)
    if reason:
        _unavailable(reason)
    return settings.ollama_base_url


async def _wait_for_search_results(settings, attempts: int = 60) -> None:
    """Block until the seeded catalog is actually searchable.

    Creating a search index returns before it is queryable, and newly-upserted
    documents are reflected in $search a few seconds later. Polling a real
    hybrid query is the only reliable readiness signal.
    """
    import asyncio

    from services.embeddings import get_embedding_service
    from services.hybrid_search import HybridSearchService

    search = HybridSearchService(
        settings=settings, embedding_service=get_embedding_service(settings)
    )
    for _ in range(attempts):
        try:
            results = await search.search_tools(query="weather forecast", mode="hybrid", limit=5)
            if results:
                return
        except Exception:  # noqa: BLE001 - index still warming up
            pass
        await asyncio.sleep(1)
    raise RuntimeError(
        "Seeded catalog never became searchable; the Atlas search index did not "
        "warm up within the timeout."
    )


async def _run_bootstrap(settings) -> None:
    """Provision the isolated DB via the application's real bootstrap path.

    Exercises the actual collection + index DDL and seeding code — creating
    tool_catalog before its search index, and the semantic_cache vector index
    with its tenant_id filter — so the tests cover the real startup sequence.
    """
    from database.mongo import connect_to_mongo, disconnect_from_mongo
    from database.seed import routing_registry_seed, seed_bootstrap_data
    from services.embeddings import get_embedding_service
    from services.proxy_registry import InMemoryFastMCPRegistry
    from services.tenant_provisioner import ensure_control_plane_indexes, provision_tenant

    await connect_to_mongo(settings)
    try:
        await ensure_control_plane_indexes()
        await provision_tenant(settings.default_tenant_id, wait_for_queryable_indexes=True)
        await seed_bootstrap_data()
        registry = InMemoryFastMCPRegistry(embedding_service=get_embedding_service(settings))
        for doc in routing_registry_seed(settings.default_tenant_id):
            await registry.mount_or_update(doc)
        # The search index becomes queryable asynchronously, and freshly-upserted
        # documents take a beat to be reflected in $search/$vectorSearch. Block
        # until a hybrid query actually returns results so tests never race the
        # index. (mongot indexes the seeded catalog within a few seconds.)
        await _wait_for_search_results(settings)
    finally:
        # Fully disconnect so no loop-bound AsyncMongoClient survives into the
        # per-test event loops (AsyncMongoClient is bound to its creation loop).
        await disconnect_from_mongo()


@pytest.fixture(scope="session")
def bootstrapped(settings, require_ollama):
    """Run the one-time bootstrap on its own event loop, once per session.

    A sync, session-scoped fixture using ``asyncio.run`` keeps the expensive
    index DDL out of every test while leaving NO client bound to a session loop
    — each test then connects on its own function-scoped loop via ``live_db``.
    """
    import asyncio

    asyncio.run(_run_bootstrap(settings))
    return INTEGRATION_DB_NAME


@pytest_asyncio.fixture
async def live_db(bootstrapped, settings):
    """Connect on the test's own event loop; disconnect after.

    Depends on ``bootstrapped`` so indexes + seed exist, but owns its own
    connection lifecycle so the loop-bound async client never crosses loops.
    """
    from database.mongo import connect_to_mongo, disconnect_from_mongo, get_tenant_database

    await connect_to_mongo(settings)
    try:
        yield get_tenant_database(settings.default_tenant_id)
    finally:
        await disconnect_from_mongo()


@pytest_asyncio.fixture
async def live_embeddings(bootstrapped, settings):
    from services.embeddings import get_embedding_service

    return get_embedding_service(settings)


@pytest_asyncio.fixture
async def live_search(live_db, live_embeddings, settings):
    # Depends on live_db so a connection is active on this test's event loop.
    from services.hybrid_search import HybridSearchService

    return HybridSearchService(settings=settings, embedding_service=live_embeddings)
