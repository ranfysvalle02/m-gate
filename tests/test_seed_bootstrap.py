import pytest

from database.seed import routing_registry_seed, seed_bootstrap_data


def test_routing_registry_seed_includes_code_demos_and_deepwiki():
    docs = routing_registry_seed("local-dev")
    by_id = {doc["_id"]: doc for doc in docs}

    assert by_id["weather"]["transport"] == "code"
    assert by_id["orders"]["transport"] == "code"
    assert by_id["utilities"]["transport"] == "code"
    assert by_id["analytics"]["transport"] == "code"
    assert by_id["deepwiki"]["transport"] == "streamable_http"
    assert by_id["deepwiki"]["endpoint"] == "https://mcp.deepwiki.com/mcp"


@pytest.mark.asyncio
async def test_seed_bootstrap_data_encrypts_code_tool_source(patch_mongo):
    await seed_bootstrap_data()

    routing = patch_mongo["routing_registry"].docs
    weather = next(doc for doc in routing if doc.get("_id") == "weather")
    orders = next(doc for doc in routing if doc.get("_id") == "orders")
    utilities = next(doc for doc in routing if doc.get("_id") == "utilities")
    analytics = next(doc for doc in routing if doc.get("_id") == "analytics")

    for server_doc in (weather, orders, utilities, analytics):
        assert server_doc["transport"] == "code"
        assert server_doc.get("endpoint") is None
        assert server_doc.get("cwd") is None
        # Encrypted routing fields must be absent (not null/empty) so Queryable
        # Encryption never has to encrypt a null value.
        assert "command" not in server_doc
        assert "env" not in server_doc
        assert "args" not in server_doc
        for tool in server_doc.get("tools") or []:
            raw = tool.get("raw_code")
            assert isinstance(raw, str)
            assert raw.startswith("enc::") or raw.startswith("qe::")

    session_docs = patch_mongo._control_db["session_context"].docs  # type: ignore[attr-defined]
    admin = next(doc for doc in session_docs if doc.get("user_id") == "admin")
    assert "deepwiki" in (admin.get("scopes") or [])
    assert "utilities" in (admin.get("scopes") or [])
    assert "analytics" in (admin.get("scopes") or [])
    assert "server:analytics" in (admin.get("scopes") or [])
