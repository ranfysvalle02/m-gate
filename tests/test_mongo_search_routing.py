"""Unit tests for the QE-aware search client routing in database.mongo.

Under Queryable Encryption, catalog search ($rankFusion) must run through a
bypass-auto-encryption client; without QE it uses the normal client. These tests
pin that routing and the lazy/cached construction of the bypass client without
needing a live MongoDB.
"""

from __future__ import annotations

from types import SimpleNamespace

from fakes import FakeMongoClient

import database.mongo as mongo
from config.settings import Settings


def test_search_routing_uses_normal_client_when_qe_disabled(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(mongo, "get_settings", lambda: Settings(qe_enabled=False))
    monkeypatch.setattr(mongo, "get_tenant_database", lambda tenant_id: sentinel)

    def _must_not_build():
        raise AssertionError("bypass client must not be used when QE is disabled")

    monkeypatch.setattr(mongo, "get_qe_bypass_client", _must_not_build)

    assert mongo.get_tenant_database_for_search("tenant-a") is sentinel


def test_search_routing_uses_bypass_client_when_qe_enabled(monkeypatch):
    # A real Settings(qe_enabled=True) trips the KMS validator, so stub the bits
    # the routing helper actually reads.
    stub = SimpleNamespace(qe_enabled=True, tenant_db_prefix="tenant_")
    monkeypatch.setattr(mongo, "get_settings", lambda: stub)
    fake_client = FakeMongoClient()
    monkeypatch.setattr(mongo, "get_qe_bypass_client", lambda: fake_client)

    db = mongo.get_tenant_database_for_search("tenant-a")

    # Routed through the bypass client, addressing the same tenant DB.
    assert db is fake_client[mongo.tenant_db_name("tenant-a")]


def test_qe_bypass_client_is_lazy_and_cached(monkeypatch):
    builds: list[object] = []
    sentinel = object()

    def _build(settings=None):
        builds.append(settings)
        return sentinel

    monkeypatch.setattr(mongo, "build_watcher_client", _build)
    monkeypatch.setattr(
        mongo, "get_settings", lambda: SimpleNamespace(qe_enabled=True, tenant_db_prefix="tenant_")
    )
    # Ensure a clean slate; monkeypatch restores the original (None) afterwards.
    monkeypatch.setattr(mongo, "_qe_bypass_client", None, raising=False)

    first = mongo.get_qe_bypass_client()
    second = mongo.get_qe_bypass_client()

    assert first is sentinel
    assert second is sentinel
    assert len(builds) == 1  # built once, then cached for the process lifetime
