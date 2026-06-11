"""Tests for the FastMCP server export (`services/server_exporter.py`).

Two layers:

1. Unit tests over the in-memory fakes: dependency-closure resolution, error
   cases, and the shape/content of the generated project (incl. that secrets
   are never exported).
2. A subprocess end-to-end test that unzips a generated project, imports it
   with the real FastMCP, and proves a tool calling a *sibling* tool (on
   another server) resolves in-process — the headline "smart enough" behavior.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest
from fakes import FakeDatabase

from services import server_exporter
from services.server_exporter import (
    ServerExportError,
    ServerExportNotFound,
    build_server_export,
    extract_tool_refs,
)

TENANT = "acme"

TRACK_CLICK = textwrap.dedent(
    """
    def track_click(target: str) -> dict:
        return {"ok": True, "target": target}
    """
).strip()

# report() composes a sibling tool on the SAME server and one on ANOTHER server.
REPORT = textwrap.dedent(
    """
    def report(target: str) -> dict:
        clicked = context.tools.analytics.track_click(target=target)
        helped = context.tools["utils"]["helper"](value=target)
        return {"clicked": clicked, "helped": helped}
    """
).strip()

HELPER = textwrap.dedent(
    """
    def helper(value: str) -> dict:
        return {"echoed": value, "length": len(value)}
    """
).strip()


def _server_doc(name: str, tools: list[dict]) -> dict:
    return {
        "_id": name,
        "server": name,
        "tenant_id": TENANT,
        "transport": "code",
        "enabled": True,
        "tools": tools,
    }


def _tool(name: str, raw_code: str, **extra) -> dict:
    doc = {
        "name": name,
        "description": extra.get("description", f"{name} tool"),
        "raw_code": raw_code,
        "requirements": extra.get("requirements", []),
        "metadata": {"action_type": extra.get("action_type", "read"), "transport": "code"},
        "input_schema": {},
        "scopes": [],
    }
    if extra.get("requires_confirmation"):
        doc["metadata"]["requires_confirmation"] = True
    return doc


@pytest.fixture
def seeded(monkeypatch):
    """A tenant DB with analytics{track_click, report} + utils{helper}, plus a
    proxied (non-code) server and analytics secrets. Returns the FakeDatabase."""
    db = FakeDatabase()
    db["routing_registry"].docs.extend(
        [
            _server_doc(
                "analytics",
                [
                    _tool("track_click", TRACK_CLICK, requirements=["httpx==0.27.0"]),
                    _tool("report", REPORT),
                ],
            ),
            _server_doc("utils", [_tool("helper", HELPER, requirements=["httpx==0.27.0"])]),
            # A proxied server that must never be treated as exportable.
            {
                "_id": "weather",
                "server": "weather",
                "tenant_id": TENANT,
                "transport": "streamable_http",
                "endpoint": "https://example.test/mcp",
                "tools": [{"name": "forecast"}],
            },
        ]
    )
    db["server_secrets"].docs.append(
        {"_id": "analytics", "values": {"CLICK_LABEL": "enc::secret", "API_KEY": "enc::secret2"}}
    )

    async def _identity(_tenant, stored, *_a, **_k):
        return stored or ""

    monkeypatch.setattr(server_exporter, "get_tenant_database", lambda _tenant: db)
    monkeypatch.setattr(server_exporter, "decrypt_raw_code", _identity)
    return db


def _zip_names(content: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return set(zf.namelist())


def _read(content: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return zf.read(name).decode("utf-8")


# --------------------------------------------------------------------------- #
# extract_tool_refs
# --------------------------------------------------------------------------- #
def test_extract_tool_refs_handles_all_documented_forms():
    code = textwrap.dedent(
        """
        def t(x):
            a = context.tools.analytics.track_click(target=x)
            b = context.tools["my-funcs"]["helper"]()
            c = context.call("svc", "do_it", n=1)
            d = context.tools.analytics("get_stats", limit=2)
            return [a, b, c, d]
        """
    )
    assert extract_tool_refs(code) == {
        ("analytics", "track_click"),
        ("my-funcs", "helper"),
        ("svc", "do_it"),
        ("analytics", "get_stats"),
    }


def test_extract_tool_refs_ignores_dynamic_names():
    code = "def t(s, name):\n    return context.tools[s][name]()\n"
    assert extract_tool_refs(code) == set()


# --------------------------------------------------------------------------- #
# build_server_export — closure + metadata
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_export_bundles_cross_tool_dependency_closure(seeded):
    export = await build_server_export(tenant_id=TENANT, server_name="analytics")

    assert export.filename == "analytics-mcp.zip"
    assert export.primary_server == "analytics"
    # analytics{track_click, report} + the utils.helper dependency.
    assert export.tool_count == 3
    assert export.bundled_servers == ("analytics", "utils")
    assert export.extra_servers == ("utils",)

    names = _zip_names(export.content)
    assert "analytics-mcp/server.py" in names
    assert "analytics-mcp/mcp_context/runtime.py" in names
    assert "analytics-mcp/tools/analytics/track_click.py" in names
    assert "analytics-mcp/tools/analytics/report.py" in names
    assert "analytics-mcp/tools/utils/helper.py" in names


@pytest.mark.asyncio
async def test_server_py_exposes_only_primary_tools_but_registers_all(seeded):
    export = await build_server_export(tenant_id=TENANT, server_name="analytics")
    server_py = _read(export.content, "analytics-mcp/server.py")

    # All three bundled tools are registered for cross-tool resolution.
    assert "register_tool('analytics', 'track_click'" in server_py
    assert "register_tool('analytics', 'report'" in server_py
    assert "register_tool('utils', 'helper'" in server_py

    # Only analytics' own tools are exposed as MCP tools.
    assert "name='track_click'" in server_py
    assert "name='report'" in server_py
    assert "name='helper'" not in server_py


@pytest.mark.asyncio
async def test_tool_module_embeds_author_source_with_context(seeded):
    export = await build_server_export(tenant_id=TENANT, server_name="analytics")
    module = _read(export.content, "analytics-mcp/tools/analytics/track_click.py")
    assert "build_context('analytics'" in module
    assert "def track_click(target: str) -> dict:" in module


@pytest.mark.asyncio
async def test_requirements_union_with_pinned_runtime(seeded):
    export = await build_server_export(tenant_id=TENANT, server_name="analytics")
    requirements = _read(export.content, "analytics-mcp/requirements.txt")
    assert "fastmcp==3.4.2" in requirements
    assert "pymongo==4.17.0" in requirements
    assert "httpx==0.27.0" in requirements


@pytest.mark.asyncio
async def test_env_example_lists_keys_but_never_values(seeded):
    export = await build_server_export(tenant_id=TENANT, server_name="analytics")
    env_example = _read(export.content, "analytics-mcp/.env.example")
    # Key names are namespaced per server; values are blank placeholders.
    assert "ANALYTICS__CLICK_LABEL=" in env_example
    assert "ANALYTICS__API_KEY=" in env_example
    # The encrypted secret material is never present.
    assert "enc::secret" not in env_example
    assert "MONGODB_URI=" in env_example


@pytest.mark.asyncio
async def test_export_ships_runnable_scaffolding(seeded):
    export = await build_server_export(tenant_id=TENANT, server_name="analytics")
    names = _zip_names(export.content)
    assert "analytics-mcp/run.sh" in names
    assert "analytics-mcp/.python-version" in names

    run_sh = _read(export.content, "analytics-mcp/run.sh")
    assert run_sh.startswith("#!/usr/bin/env bash")
    assert "pip install" in run_sh and "python server.py" in run_sh

    # run.sh must be marked executable inside the archive (0o755 high bits).
    with zipfile.ZipFile(io.BytesIO(export.content)) as zf:
        mode = zf.getinfo("analytics-mcp/run.sh").external_attr >> 16
        assert mode & 0o111, f"run.sh not executable: {oct(mode)}"
        readme_mode = zf.getinfo("analytics-mcp/README.md").external_attr >> 16
        assert not (readme_mode & 0o111)


@pytest.mark.asyncio
async def test_server_py_transport_is_env_driven(seeded):
    export = await build_server_export(tenant_id=TENANT, server_name="analytics")
    server_py = _read(export.content, "analytics-mcp/server.py")
    assert 'os.environ.get("MCP_TRANSPORT"' in server_py
    assert 'transport="http"' in server_py
    assert 'os.environ.get("MCP_PORT"' in server_py


@pytest.mark.asyncio
async def test_env_example_documents_transport(seeded):
    export = await build_server_export(tenant_id=TENANT, server_name="analytics")
    env_example = _read(export.content, "analytics-mcp/.env.example")
    assert "MCP_TRANSPORT=stdio" in env_example
    assert "MCP_HOST=127.0.0.1" in env_example
    assert "MCP_PORT=8000" in env_example


@pytest.mark.asyncio
async def test_readme_has_paste_ready_mcp_client_config(seeded):
    export = await build_server_export(tenant_id=TENANT, server_name="analytics")
    readme = _read(export.content, "analytics-mcp/README.md")
    # A copy-paste mcpServers block keyed by the primary server name, plus the
    # one-command launcher and an env-driven HTTP recipe.
    assert '"mcpServers"' in readme
    assert '"analytics"' in readme
    assert "analytics-mcp/run.sh" in readme
    assert "MCP_TRANSPORT=http" in readme
    assert "./run.sh" in readme


@pytest.mark.asyncio
async def test_export_rejects_non_code_server(seeded):
    with pytest.raises(ServerExportError):
        await build_server_export(tenant_id=TENANT, server_name="weather")


@pytest.mark.asyncio
async def test_export_missing_server_raises_not_found(seeded):
    with pytest.raises(ServerExportNotFound):
        await build_server_export(tenant_id=TENANT, server_name="ghost")


@pytest.mark.asyncio
async def test_export_without_tools_raises(seeded):
    seeded["routing_registry"].docs.append(_server_doc("empty", []))
    with pytest.raises(ServerExportError):
        await build_server_export(tenant_id=TENANT, server_name="empty")


@pytest.mark.asyncio
async def test_unresolved_refs_are_reported_in_readme(seeded, monkeypatch):
    # A code tool that calls a non-existent sibling: bundled, but flagged.
    seeded["routing_registry"].docs.append(
        _server_doc(
            "lonely",
            [_tool("caller", "def caller():\n    return context.call('nope', 'missing')\n")],
        )
    )
    export = await build_server_export(tenant_id=TENANT, server_name="lonely")
    readme = _read(export.content, "lonely-mcp/README.md")
    assert "nope.missing" in readme


# --------------------------------------------------------------------------- #
# End-to-end: the generated project actually runs standalone.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_generated_project_runs_cross_tool_calls(seeded, tmp_path):
    export = await build_server_export(tenant_id=TENANT, server_name="analytics")
    with zipfile.ZipFile(io.BytesIO(export.content)) as zf:
        zf.extractall(tmp_path)
    root = tmp_path / "analytics-mcp"

    # Driver: import the generated server (validates FastMCP registration), then
    # invoke report() which fans out to a same-server and a cross-server tool.
    driver = tmp_path / "_drive.py"
    driver.write_text(
        textwrap.dedent(
            f"""
            import json, sys
            sys.path.insert(0, {str(root)!r})
            import server  # noqa: F401  (registers tools, builds the FastMCP app)
            from tools.analytics.report import report
            print("RESULT:" + json.dumps(report(target="hello")))
            """
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, str(driver)],
        capture_output=True,
        text=True,
        cwd=str(root),
        timeout=60,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
    result = json.loads(line[len("RESULT:") :])
    assert result == {
        "clicked": {"ok": True, "target": "hello"},
        "helped": {"echoed": "hello", "length": 5},
    }


@pytest.mark.asyncio
async def test_generated_project_imports_are_valid_python(seeded):
    """Every generated .py file must at least parse (catches template drift)."""
    export = await build_server_export(tenant_id=TENANT, server_name="analytics")
    with zipfile.ZipFile(io.BytesIO(export.content)) as zf:
        for name in zf.namelist():
            if name.endswith(".py"):
                compile(zf.read(name).decode("utf-8"), name, "exec")
    assert Path  # keep import used
