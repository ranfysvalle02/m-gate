"""The bundled downstream demo servers (orders, weather) must refuse to verify
tokens against the repo's public dev JWKS outside a local/dev environment: the
private half of that keypair is published in this repo, so anyone could forge
tokens the server would otherwise accept.
"""

from __future__ import annotations

import importlib

import pytest

SERVER_MODULES = ["servers.orders.server", "servers.weather.server"]


@pytest.fixture(params=SERVER_MODULES)
def server_module(request):
    return importlib.import_module(request.param)


def test_guard_rejects_bundled_jwks_in_production(server_module, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="bundled dev JWKS"):
        server_module._guard_bundled_dev_jwks("config/dev-jwks.json")


def test_guard_rejects_bundled_jwks_relative_prefix_in_staging(server_module, monkeypatch):
    # The "./" prefix must still resolve to the bundled file.
    monkeypatch.setenv("ENVIRONMENT", "staging")
    with pytest.raises(RuntimeError, match="bundled dev JWKS"):
        server_module._guard_bundled_dev_jwks("./config/dev-jwks.json")


def test_guard_allows_bundled_jwks_in_development(server_module, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    server_module._guard_bundled_dev_jwks("config/dev-jwks.json")


def test_guard_allows_bundled_jwks_when_environment_unset(server_module, monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    server_module._guard_bundled_dev_jwks("config/dev-jwks.json")


def test_guard_allows_custom_jwks_path_in_production(server_module, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    server_module._guard_bundled_dev_jwks("/etc/secrets/real-jwks.json")
