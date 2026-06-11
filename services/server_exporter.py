"""Export a code server as a self-contained, runnable FastMCP project (`.zip`).

Given a tenant + one ``transport="code"`` server, this builds a small Python
project that reproduces the gateway's sandbox ``context`` object so every
authored tool runs **unmodified** outside the gateway:

    context.db      -> a real MongoDB database (pymongo)
    context.env     -> that server's environment values
    context.tools   -> call sibling tools in-process (the tenant namespace)
    context.call(server, tool, **kwargs)

The "smart" part is cross-tool calls: when ``tool_a`` calls ``tool_b`` (even on
another server in the same tenant), we statically resolve the dependency
closure and bundle every reachable code tool, wiring them into an in-process
registry so ``context.tools[...][...]`` resolves locally — exactly how it would
inside the gateway.

Secrets are **never** exported. We emit the *names* of each server's
``context.env`` keys into ``.env.example`` (with empty values) and leave it to
the operator to supply real values at runtime.
"""

from __future__ import annotations

import ast
import io
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime

from config.settings import Settings, get_settings
from database.mongo import get_tenant_database, tenant_db_name
from services.code_tools import CODE_TRANSPORT, decrypt_raw_code

# Versions the gateway itself runs on; the export pins the same major line so an
# operator gets a known-good runtime. Kept in sync with ``requirements.txt``.
FASTMCP_REQUIREMENT = "fastmcp==3.4.2"
PYMONGO_REQUIREMENT = "pymongo==4.17.0"


class ServerExportError(ValueError):
    """The requested server cannot be exported (bad transport, empty, etc.)."""


class ServerExportNotFound(ServerExportError):
    """The requested server does not exist for this tenant."""


@dataclass(frozen=True)
class _Tool:
    server: str
    name: str
    description: str
    raw_code: str
    requirements: tuple[str, ...]
    requires_confirmation: bool


@dataclass
class _BundledServer:
    name: str
    slug: str
    tools: list[_Tool] = field(default_factory=list)
    env_keys: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ServerExport:
    """A finished export: the zip bytes plus a little metadata for the API."""

    filename: str
    content: bytes
    primary_server: str
    tool_count: int
    bundled_servers: tuple[str, ...]
    extra_servers: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Cross-tool reference extraction (best-effort static analysis)
# --------------------------------------------------------------------------- #
def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _accessor(node: ast.AST) -> tuple[ast.AST | None, str | None]:
    """If ``node`` is ``x.<name>`` or ``x["<name>"]``, return ``(x, name)``."""
    if isinstance(node, ast.Attribute):
        return node.value, node.attr
    if isinstance(node, ast.Subscript):
        key = _const_str(node.slice)
        if key is not None:
            return node.value, key
    return None, None


def _is_context_tools(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "tools"
        and isinstance(node.value, ast.Name)
        and node.value.id == "context"
    )


def extract_tool_refs(raw_code: str) -> set[tuple[str, str]]:
    """Find ``(server, tool)`` pairs this code calls via ``context``.

    Recognizes the documented forms with *literal* names:

      - ``context.call("server", "tool", ...)``
      - ``context.tools.server.tool(...)`` / ``context.tools["server"]["tool"]``
      - ``context.tools.server("tool", ...)`` / ``context.tools["server"]("tool")``

    Dynamic names (built from variables) cannot be resolved statically and are
    simply not followed — the README flags this so the operator knows.
    """
    refs: set[tuple[str, str]] = set()
    try:
        tree = ast.parse(raw_code)
    except SyntaxError:
        return refs

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # context.call("server", "tool", ...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "call"
                and isinstance(func.value, ast.Name)
                and func.value.id == "context"
                and len(node.args) >= 2
            ):
                server = _const_str(node.args[0])
                tool = _const_str(node.args[1])
                if server and tool:
                    refs.add((server, tool))
            # context.tools.<server>("tool", ...)
            inner, server = _accessor(func)
            if server is not None and _is_context_tools(inner) and node.args:
                tool = _const_str(node.args[0])
                if tool:
                    refs.add((server, tool))

        # context.tools.<server>.<tool>  /  context.tools["server"]["tool"]
        inner1, tool = _accessor(node)
        if tool is not None:
            inner2, server = _accessor(inner1) if inner1 is not None else (None, None)
            if server is not None and _is_context_tools(inner2):
                refs.add((server, tool))
    return refs


# --------------------------------------------------------------------------- #
# Identifier / name helpers
# --------------------------------------------------------------------------- #
def _slug(name: str) -> str:
    """Turn an arbitrary server/tool name into a safe Python module identifier."""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(name))
    cleaned = cleaned.strip("_") or "tool"
    if cleaned[0].isdigit():
        cleaned = f"s_{cleaned}"
    return cleaned.lower()


def _env_prefix(server: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(server)).strip("_").upper()
    return f"{cleaned}__" if cleaned else ""


def _alias(server_slug: str, tool_slug: str) -> str:
    return f"_{server_slug}__{tool_slug}"


# --------------------------------------------------------------------------- #
# Loading + closure resolution
# --------------------------------------------------------------------------- #
async def _load_code_servers(tenant_id: str) -> dict[str, dict]:
    """Return ``{server_name: server_doc}`` for every code server in the tenant."""
    db = get_tenant_database(tenant_id)
    docs = await db["routing_registry"].find({"transport": CODE_TRANSPORT}).to_list(length=10_000)
    servers: dict[str, dict] = {}
    for doc in docs:
        name = str(doc.get("server") or doc.get("_id") or "").strip()
        if name:
            servers[name] = doc
    return servers


async def _env_keys_for(tenant_id: str, server_name: str) -> list[str]:
    """Names (never values) of the ``context.env`` keys defined for a server."""
    db = get_tenant_database(tenant_id)
    doc = await db["server_secrets"].find_one({"_id": server_name})
    values = doc.get("values") if isinstance(doc, dict) else None
    if not isinstance(values, dict):
        return []
    return sorted(str(key) for key in values if isinstance(key, str))


def _tool_index(server_docs: dict[str, dict]) -> dict[tuple[str, str], tuple[str, dict]]:
    """Map ``(server, tool)`` -> ``(server_name, tool_doc)`` across all code servers."""
    index: dict[tuple[str, str], tuple[str, dict]] = {}
    for server_name, doc in server_docs.items():
        for tool in doc.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            tool_name = str(tool.get("name") or "").strip()
            if tool_name:
                index[(server_name, tool_name)] = (server_name, tool)
    return index


async def _build_tool(tenant_id: str, server: str, tool_doc: dict) -> _Tool | None:
    raw_code = await decrypt_raw_code(tenant_id, tool_doc.get("raw_code"))
    if not raw_code.strip():
        return None
    raw_metadata = tool_doc.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    requirements = tuple(
        str(req).strip() for req in (tool_doc.get("requirements") or []) if str(req).strip()
    )
    return _Tool(
        server=server,
        name=str(tool_doc.get("name") or "").strip(),
        description=str(tool_doc.get("description") or "").strip(),
        raw_code=raw_code,
        requirements=requirements,
        requires_confirmation=bool(metadata.get("requires_confirmation")),
    )


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
async def build_server_export(
    *,
    tenant_id: str,
    server_name: str,
    settings: Settings | None = None,
) -> ServerExport:
    """Build a runnable FastMCP project zip for ``server_name``.

    Bundles the server's tools plus the transitive closure of any sibling code
    tools they call via ``context.tools`` / ``context.call`` so cross-tool
    composition keeps working in-process.
    """
    settings = settings or get_settings()
    server_docs = await _load_code_servers(tenant_id)
    primary_doc = server_docs.get(server_name)
    if primary_doc is None:
        # Distinguish "exists but not a code server" from "missing" for the API.
        db = get_tenant_database(tenant_id)
        any_doc = await db["routing_registry"].find_one({"_id": server_name})
        if any_doc is None:
            raise ServerExportNotFound(f"Server '{server_name}' was not found.")
        raise ServerExportError(
            f"Server '{server_name}' is not a code server. "
            "Export is only available for transport=code servers."
        )

    index = _tool_index(server_docs)

    # BFS over the cross-tool dependency graph, decrypting lazily + caching.
    built: dict[tuple[str, str], _Tool] = {}
    missing_refs: set[tuple[str, str]] = set()
    primary_tool_names = [
        str(t.get("name") or "").strip()
        for t in (primary_doc.get("tools") or [])
        if isinstance(t, dict) and str(t.get("name") or "").strip()
    ]
    queue: list[tuple[str, str]] = [(server_name, name) for name in primary_tool_names]
    while queue:
        key = queue.pop(0)
        if key in built or key in missing_refs:
            continue
        entry = index.get(key)
        if entry is None:
            missing_refs.add(key)
            continue
        owner, tool_doc = entry
        tool = await _build_tool(tenant_id, owner, tool_doc)
        if tool is None:
            missing_refs.add(key)
            continue
        built[key] = tool
        for ref in sorted(extract_tool_refs(tool.raw_code)):
            if ref not in built:
                queue.append(ref)

    primary_tools = [tool for key, tool in built.items() if key[0] == server_name]
    if not primary_tools:
        raise ServerExportError(
            f"Server '{server_name}' has no exportable tools (no decryptable code-tool source)."
        )

    # Group every bundled tool by its owning server, preserving stable slugs.
    bundled: dict[str, _BundledServer] = {}
    used_slugs: set[str] = set()
    for owner in sorted({key[0] for key in built}):
        slug = _slug(owner)
        candidate = slug
        suffix = 2
        while candidate in used_slugs:
            candidate = f"{slug}_{suffix}"
            suffix += 1
        used_slugs.add(candidate)
        bundled[owner] = _BundledServer(name=owner, slug=candidate)
    for key, tool in sorted(built.items()):
        bundled[key[0]].tools.append(tool)
    for owner, server in bundled.items():
        server.env_keys = await _env_keys_for(tenant_id, owner)

    files = _render_project(
        settings=settings,
        tenant_id=tenant_id,
        primary_server=server_name,
        bundled=bundled,
        missing_refs=sorted(missing_refs),
    )

    root = f"{_slug(server_name)}-mcp"
    content = _zip_files(root, files)
    extra_servers = tuple(sorted(name for name in bundled if name != server_name))
    return ServerExport(
        filename=f"{root}.zip",
        content=content,
        primary_server=server_name,
        tool_count=sum(len(s.tools) for s in bundled.values()),
        bundled_servers=tuple(sorted(bundled)),
        extra_servers=extra_servers,
    )


# --------------------------------------------------------------------------- #
# Project rendering
# --------------------------------------------------------------------------- #
def _zip_files(root: str, files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    fixed_date = (2026, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            info = zipfile.ZipInfo(f"{root}/{path}", date_time=fixed_date)
            # Make shell helpers executable; everything else stays 0644.
            mode = 0o755 if path.endswith(".sh") else 0o644
            info.external_attr = mode << 16
            zf.writestr(info, files[path])
    return buffer.getvalue()


def _render_project(
    *,
    settings: Settings,
    tenant_id: str,
    primary_server: str,
    bundled: dict[str, _BundledServer],
    missing_refs: list[tuple[str, str]],
) -> dict[str, str]:
    files: dict[str, str] = {}

    # --- mcp_context runtime package (static) ---
    files["mcp_context/__init__.py"] = _RUNTIME_INIT_PY
    files["mcp_context/runtime.py"] = _RUNTIME_PY

    # --- per-server tool packages ---
    files["tools/__init__.py"] = ""
    for server in bundled.values():
        files[f"tools/{server.slug}/__init__.py"] = ""
        for tool in server.tools:
            module = _slug(tool.name)
            files[f"tools/{server.slug}/{module}.py"] = _render_tool_module(server, tool)

    files["server.py"] = _render_server_py(primary_server, bundled)
    files["requirements.txt"] = _render_requirements(bundled)
    files[".env.example"] = _render_env_example(tenant_id, bundled)
    files[".gitignore"] = ".env\n__pycache__/\n*.pyc\n.venv/\nvenv/\n"
    files[".python-version"] = "3.12\n"
    files["run.sh"] = _render_run_sh()
    files["README.md"] = _render_readme(primary_server, bundled, missing_refs)
    return files


def _render_run_sh() -> str:
    return (
        "#!/usr/bin/env bash\n"
        "# One-shot launcher: create a venv, install deps, and start the server.\n"
        "# Re-runs are fast — the venv and installed deps are reused.\n"
        "set -euo pipefail\n"
        'cd "$(dirname "$0")"\n'
        "\n"
        "if [ ! -d .venv ]; then\n"
        "  python3 -m venv .venv\n"
        "fi\n"
        "source .venv/bin/activate\n"
        "pip install --quiet --upgrade pip\n"
        "pip install --quiet -r requirements.txt\n"
        "\n"
        "# Load .env if present (export every KEY=VALUE line).\n"
        "if [ -f .env ]; then\n"
        "  set -a; source .env; set +a\n"
        "fi\n"
        "\n"
        "exec python server.py\n"
    )


def _render_tool_module(server: _BundledServer, tool: _Tool) -> str:
    keys = ", ".join(repr(key) for key in server.env_keys)
    header = '"""'
    title = f"{server.name}.{tool.name} — exported from mdb-mcp-gateway."
    desc = tool.description or "(no description)"
    return (
        f"{header}{title}\n\n{desc}\n{header}\n\n"
        "from mcp_context import build_context\n\n"
        f"context = build_context({server.name!r}, [{keys}])\n\n\n"
        f"{tool.raw_code.rstrip()}\n"
    )


def _render_server_py(primary_server: str, bundled: dict[str, _BundledServer]) -> str:
    lines: list[str] = []
    lines.append('"""')
    lines.append(f"{primary_server} — MCP server exported from mdb-mcp-gateway.")
    lines.append("")
    lines.append("Run it over stdio (default) or HTTP — see README.md. Every tool below")
    lines.append("runs the exact source authored in the gateway, with a reconstructed")
    lines.append("`context` (context.db / context.env / context.tools).")
    lines.append('"""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("from fastmcp import FastMCP")
    lines.append("from fastmcp.tools import Tool")
    lines.append("")
    lines.append("from mcp_context import jsonify_tool, register_tool")
    lines.append("")

    # Imports (grouped by server for readability).
    for server in bundled.values():
        for tool in server.tools:
            module = _slug(tool.name)
            alias = _alias(server.slug, module)
            lines.append(f"from tools.{server.slug}.{module} import {tool.name} as {alias}")
    lines.append("")
    lines.append("")
    lines.append(f"mcp = FastMCP({primary_server!r})")
    lines.append("")
    lines.append("# Register every bundled tool so context.tools / context.call resolve")
    lines.append("# in-process — this is what makes one tool calling another keep working.")
    for server in bundled.values():
        for tool in server.tools:
            alias = _alias(server.slug, _slug(tool.name))
            lines.append(f"register_tool({server.name!r}, {tool.name!r}, {alias})")
    lines.append("")
    lines.append(
        f"# Expose the {primary_server} server's tools over MCP (returns are JSON-normalized)."
    )
    primary = bundled[primary_server]
    for tool in primary.tools:
        alias = _alias(primary.slug, _slug(tool.name))
        desc = _py_str(tool.description or tool.name)
        lines.append("mcp.add_tool(")
        lines.append("    Tool.from_function(")
        lines.append(f"        jsonify_tool({alias}),")
        lines.append(f"        name={tool.name!r},")
        lines.append(f"        description={desc},")
        lines.append("        output_schema=None,")
        lines.append("    )")
        lines.append(")")
    lines.append("")
    lines.append("")
    lines.append('if __name__ == "__main__":')
    lines.append("    import os")
    lines.append("")
    lines.append('    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()')
    lines.append('    if transport in {"http", "streamable-http"}:')
    lines.append("        mcp.run(")
    lines.append('            transport="http",')
    lines.append('            host=os.environ.get("MCP_HOST", "127.0.0.1"),')
    lines.append('            port=int(os.environ.get("MCP_PORT", "8000")),')
    lines.append("        )")
    lines.append("    else:")
    lines.append("        mcp.run()")
    lines.append("")
    return "\n".join(lines)


def _py_str(value: str) -> str:
    """Render a string as a Python literal, preferring triple-quotes for prose."""
    if "\n" in value or '"' in value:
        safe = value.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
        return f'"""{safe}"""'
    return repr(value)


def _render_requirements(bundled: dict[str, _BundledServer]) -> str:
    extras: set[str] = set()
    for server in bundled.values():
        for tool in server.tools:
            extras.update(tool.requirements)
    lines = [
        "# Exported by mdb-mcp-gateway. Pinned to the gateway's runtime line.",
        FASTMCP_REQUIREMENT,
        PYMONGO_REQUIREMENT,
    ]
    if extras:
        lines.append("")
        lines.append("# Declared by the exported tools:")
        lines.extend(sorted(extras))
    return "\n".join(lines) + "\n"


def _render_env_example(tenant_id: str, bundled: dict[str, _BundledServer]) -> str:
    lines = [
        "# --- context.db: MongoDB connection -------------------------------------",
        "# Required for any tool that uses context.db.",
        "MONGODB_URI=mongodb://localhost:27017",
        "# Database name. The gateway tenant database was:",
        f"MONGODB_DB={tenant_db_name(tenant_id)}",
        "",
        "# --- context.tools: cross-tool call safety ------------------------------",
        "# Max nesting depth for context.tools calls (guards against cycles).",
        "MCP_TOOL_CALL_MAX_DEPTH=5",
        "",
        "# --- transport: how the server is served --------------------------------",
        "# Default is stdio (what MCP clients use). Set http to serve over HTTP.",
        "MCP_TRANSPORT=stdio",
        "# Only used when MCP_TRANSPORT=http:",
        "MCP_HOST=127.0.0.1",
        "MCP_PORT=8000",
    ]
    has_env = any(server.env_keys for server in bundled.values())
    if has_env:
        lines.append("")
        lines.append("# --- context.env: per-server values -------------------------------------")
        lines.append("# Fill in the real values. NEVER commit this file with secrets.")
        lines.append("# Keys are namespaced <SERVER>__<KEY>; for a single server you may also")
        lines.append("# set the bare <KEY> (the prefixed form always wins).")
        for server in bundled.values():
            if not server.env_keys:
                continue
            lines.append("")
            lines.append(f"# Server: {server.name}")
            prefix = _env_prefix(server.name)
            for key in server.env_keys:
                lines.append(f"{prefix}{key}=")
    return "\n".join(lines) + "\n"


def _render_readme(
    primary_server: str,
    bundled: dict[str, _BundledServer],
    missing_refs: list[tuple[str, str]],
) -> str:
    primary = bundled[primary_server]
    extra = [server for name, server in bundled.items() if name != primary_server]
    generated = datetime.now(UTC).strftime("%Y-%m-%d")

    lines: list[str] = []
    lines.append(f"# {primary_server} — exported MCP server")
    lines.append("")
    lines.append(
        f"A standalone [FastMCP](https://github.com/jlowin/fastmcp) server generated "
        f"from the **{primary_server}** server in mdb-mcp-gateway (on {generated})."
    )
    lines.append("")
    lines.append(
        "Every tool runs the exact Python you authored in the gateway. The sandbox "
        "`context` object is reconstructed locally so your code runs unchanged:"
    )
    lines.append("")
    lines.append("- `context.db` — a real MongoDB database (via `pymongo`)")
    lines.append("- `context.env` — this server's environment values")
    lines.append("- `context.tools` / `context.call` — call sibling tools in-process")
    lines.append("")
    lines.append("## Tools")
    lines.append("")
    for tool in primary.tools:
        desc = tool.description or "(no description)"
        flag = " _(was confirmation-gated in the gateway)_" if tool.requires_confirmation else ""
        lines.append(f"- **`{tool.name}`** — {desc}{flag}")
    lines.append("")
    if extra:
        lines.append("### Bundled dependencies (cross-tool calls)")
        lines.append("")
        lines.append(
            "These tools are **not** exposed over MCP, but are bundled because the "
            "tools above call them via `context.tools` / `context.call`:"
        )
        lines.append("")
        for server in extra:
            names = ", ".join(f"`{tool.name}`" for tool in server.tools)
            lines.append(f"- `{server.name}`: {names}")
        lines.append("")
    root = f"{_slug(primary_server)}-mcp"

    lines.append("## Quickstart")
    lines.append("")
    lines.append(
        "One command — sets up a venv, installs deps, loads `.env`, and serves over stdio:"
    )
    lines.append("")
    lines.append("```bash")
    lines.append("cp .env.example .env   # then fill in real values")
    lines.append("./run.sh")
    lines.append("```")
    lines.append("")
    lines.append("<details><summary>Prefer to run it by hand?</summary>")
    lines.append("")
    lines.append("```bash")
    lines.append("python -m venv .venv && source .venv/bin/activate")
    lines.append("pip install -r requirements.txt")
    lines.append("cp .env.example .env            # then fill in real values")
    lines.append("set -a && . ./.env && set +a    # load env into your shell")
    lines.append("python server.py                # runs over stdio (for MCP clients)")
    lines.append("```")
    lines.append("")
    lines.append("</details>")
    lines.append("")
    lines.append("## Connect it to your MCP client")
    lines.append("")
    lines.append(
        f"Point any MCP client at this server over stdio. Replace `/ABSOLUTE/PATH/TO/{root}` "
        "with wherever you unzipped this project."
    )
    lines.append("")
    lines.append(
        "**Cursor** (`.cursor/mcp.json`) or **Claude Desktop** (`claude_desktop_config.json`):"
    )
    lines.append("")
    lines.append("```json")
    lines.append("{")
    lines.append('  "mcpServers": {')
    lines.append(f'    "{primary_server}": {{')
    lines.append(f'      "command": "/ABSOLUTE/PATH/TO/{root}/run.sh"')
    lines.append("    }")
    lines.append("  }")
    lines.append("}")
    lines.append("```")
    lines.append("")
    lines.append(
        "`run.sh` provisions the venv on first launch and loads `.env`, so that line is all "
        "the client needs. Prefer an explicit interpreter? Use the venv's Python directly:"
    )
    lines.append("")
    lines.append("```json")
    lines.append("{")
    lines.append('  "mcpServers": {')
    lines.append(f'    "{primary_server}": {{')
    lines.append(f'      "command": "/ABSOLUTE/PATH/TO/{root}/.venv/bin/python",')
    lines.append('      "args": ["server.py"],')
    lines.append(f'      "cwd": "/ABSOLUTE/PATH/TO/{root}"')
    lines.append("    }")
    lines.append("  }")
    lines.append("}")
    lines.append("```")
    lines.append("")
    lines.append("## Run over HTTP instead")
    lines.append("")
    lines.append("No code edits — just set environment variables:")
    lines.append("")
    lines.append("```bash")
    lines.append("MCP_TRANSPORT=http MCP_HOST=127.0.0.1 MCP_PORT=8000 python server.py")
    lines.append("```")
    lines.append("")
    lines.append("## What's inside")
    lines.append("")
    lines.append("```")
    lines.append(f"{root}/")
    lines.append("├── server.py          # FastMCP entrypoint: registers + exposes the tools")
    lines.append("├── run.sh             # venv + install + run (loads .env)")
    lines.append("├── requirements.txt")
    lines.append("├── .env.example       # copy to .env and fill in")
    lines.append("├── .python-version")
    lines.append("├── mcp_context/       # reconstructs context.db / context.env / context.tools")
    lines.append("└── tools/             # the authored source for every bundled tool")
    servers = list(bundled.values())
    width = max((len(s.slug) for s in servers), default=0) + 1
    for idx, server in enumerate(servers):
        connector = "└──" if idx == len(servers) - 1 else "├──"
        entry = f"{server.slug}/".ljust(width + 1)
        lines.append(f"    {connector} {entry} # {server.name}")
    lines.append("```")
    lines.append("")
    lines.append("## Configuration")
    lines.append("")
    lines.append("See `.env.example`. At minimum, tools that touch `context.db` need")
    lines.append("`MONGODB_URI` (and usually `MONGODB_DB`). `context.env` values are")
    lines.append("listed there with empty placeholders — **secrets are never exported**.")
    lines.append("")
    if missing_refs:
        lines.append("## Heads up: unresolved tool calls")
        lines.append("")
        lines.append(
            "Static analysis found `context.tools` / `context.call` references that "
            "could not be bundled (the target was not a code tool in this tenant, or "
            "the name was computed at runtime). Calling these will raise at runtime:"
        )
        lines.append("")
        for ref_server, ref_tool in missing_refs:
            lines.append(f"- `{ref_server}.{ref_tool}`")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_Generated by mdb-mcp-gateway. Re-export to pick up changes._")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Static runtime shipped inside every export (mcp_context/)
# --------------------------------------------------------------------------- #
_RUNTIME_INIT_PY = (
    '"""Reconstructed gateway `context` runtime for the exported server."""\n\n'
    "from mcp_context.runtime import (\n"
    "    Context,\n"
    "    ObjectId,\n"
    "    WriteResult,\n"
    "    build_context,\n"
    "    jsonify,\n"
    "    jsonify_tool,\n"
    "    register_tool,\n"
    ")\n\n"
    "__all__ = [\n"
    '    "Context",\n'
    '    "ObjectId",\n'
    '    "WriteResult",\n'
    '    "build_context",\n'
    '    "jsonify",\n'
    '    "jsonify_tool",\n'
    '    "register_tool",\n'
    "]\n"
)


_RUNTIME_PY = r'''"""Standalone runtime that reproduces the gateway sandbox `context` object.

Generated by mdb-mcp-gateway's "Export server" feature. You normally do not
need to edit this file. It lets every authored tool run unmodified:

    context.db      -> a real MongoDB database (pymongo)
    context.env     -> this server's environment values
    context.tools   -> call sibling tools in-process (the tenant namespace)
    context.call(server, tool, **kwargs)
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import os
from datetime import date, datetime, timezone
from typing import Any, Callable

from bson import ObjectId
from bson.errors import InvalidId

__all__ = [
    "Context",
    "ObjectId",
    "WriteResult",
    "build_context",
    "jsonify",
    "jsonify_tool",
    "register_tool",
]


# --------------------------------------------------------------------------- #
# JSON normalization (mirrors the gateway: ObjectId -> str, datetime -> ISO-Z)
# --------------------------------------------------------------------------- #
class WriteResult:
    """PyMongo-flavored write result: attribute + dict access, JSON-safe."""

    acknowledged = True

    def __init__(self, data):
        self._data = dict(data or {})

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def __iter__(self):
        return iter(self._data)

    def __bool__(self):
        return True

    def to_dict(self):
        return dict(self._data)

    def __repr__(self):
        return f"WriteResult({self._data!r})"


def jsonify(value: Any) -> Any:
    """Recursively coerce Mongo/BSON values into JSON-native types.

    Matches the gateway return contract so output is identical whether a tool
    runs in the sandbox or here: ObjectId -> str, datetime -> ISO-8601 UTC,
    WriteResult -> dict.
    """
    if isinstance(value, WriteResult):
        return jsonify(value.to_dict())
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonify(v) for v in value]
    return value


def jsonify_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool so its return is JSON-normalized, preserving its signature
    (so FastMCP still derives the correct input schema)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return jsonify(fn(*args, **kwargs))

    try:
        wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        pass
    return wrapper


# --------------------------------------------------------------------------- #
# context.db (real MongoDB via pymongo)
# --------------------------------------------------------------------------- #
class _Collection:
    def __init__(self, collection):
        self._c = collection

    def find_one(self, query=None, **kwargs):
        return self._c.find_one(query or {}, **kwargs)

    def find(self, query=None, **kwargs):
        # Materialized to a list to mirror the gateway bridge (JSON-relayed).
        return list(self._c.find(query or {}, **kwargs))

    def aggregate(self, pipeline, **kwargs):
        return list(self._c.aggregate(pipeline, **kwargs))

    def count_documents(self, query=None, **kwargs):
        return self._c.count_documents(query or {}, **kwargs)

    def distinct(self, field, query=None, **kwargs):
        return self._c.distinct(field, query or {}, **kwargs)

    def insert_one(self, doc, **kwargs):
        r = self._c.insert_one(doc, **kwargs)
        return WriteResult({"inserted_id": r.inserted_id, "acknowledged": r.acknowledged})

    def insert_many(self, docs, **kwargs):
        r = self._c.insert_many(docs, **kwargs)
        return WriteResult(
            {"inserted_ids": list(r.inserted_ids), "acknowledged": r.acknowledged}
        )

    def update_one(self, filt, update, **kwargs):
        r = self._c.update_one(filt, update, **kwargs)
        return WriteResult(
            {
                "matched_count": r.matched_count,
                "modified_count": r.modified_count,
                "upserted_id": r.upserted_id,
                "acknowledged": r.acknowledged,
            }
        )

    def update_many(self, filt, update, **kwargs):
        r = self._c.update_many(filt, update, **kwargs)
        return WriteResult(
            {
                "matched_count": r.matched_count,
                "modified_count": r.modified_count,
                "upserted_id": r.upserted_id,
                "acknowledged": r.acknowledged,
            }
        )

    def delete_one(self, filt, **kwargs):
        r = self._c.delete_one(filt, **kwargs)
        return WriteResult({"deleted_count": r.deleted_count, "acknowledged": r.acknowledged})

    def delete_many(self, filt, **kwargs):
        r = self._c.delete_many(filt, **kwargs)
        return WriteResult({"deleted_count": r.deleted_count, "acknowledged": r.acknowledged})


class _Database:
    """Lazily-connected tenant database; connects on first use via MONGODB_URI."""

    def __init__(self):
        self._db = None

    @staticmethod
    def ObjectId(value):
        try:
            return ObjectId(str(value))
        except (InvalidId, TypeError) as exc:
            raise ValueError(f"Invalid ObjectId: {value!r}") from exc

    def _database(self):
        if self._db is None:
            uri = os.environ.get("MONGODB_URI")
            if not uri:
                raise RuntimeError(
                    "context.db needs a MongoDB connection. Set MONGODB_URI "
                    "(and optionally MONGODB_DB) in your environment."
                )
            from pymongo import MongoClient

            client = MongoClient(uri)
            db_name = os.environ.get("MONGODB_DB")
            if db_name:
                self._db = client[db_name]
            else:
                try:
                    self._db = client.get_default_database()
                except Exception as exc:
                    raise RuntimeError(
                        "Set MONGODB_DB, or include a default database in MONGODB_URI."
                    ) from exc
                if self._db is None:
                    raise RuntimeError(
                        "Set MONGODB_DB, or include a default database in MONGODB_URI."
                    )
        return self._db

    def __getitem__(self, name):
        return _Collection(self._database()[name])

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _Collection(self._database()[name])


# --------------------------------------------------------------------------- #
# context.tools (in-process sibling calls; the tenant is the namespace)
# --------------------------------------------------------------------------- #
_REGISTRY = {}
_DEPTH = contextvars.ContextVar("mcp_tool_call_depth", default=0)


def register_tool(server, tool, fn):
    _REGISTRY[(str(server), str(tool))] = fn


def _max_depth():
    try:
        return max(1, int(os.environ.get("MCP_TOOL_CALL_MAX_DEPTH", "5")))
    except ValueError:
        return 5


class _ToolCallable:
    def __init__(self, server, tool):
        self._server = str(server)
        self._tool = str(tool)

    def __call__(self, **kwargs):
        fn = _REGISTRY.get((self._server, self._tool))
        if fn is None:
            raise RuntimeError(
                f"context.tools: no bundled tool '{self._server}.{self._tool}'. "
                "Only tools exported with this server are callable."
            )
        depth = _DEPTH.get()
        limit = _max_depth()
        if depth >= limit:
            raise RuntimeError(
                f"context.tools: max call depth ({limit}) exceeded calling "
                f"'{self._server}.{self._tool}' (cyclic tool calls?)."
            )
        token = _DEPTH.set(depth + 1)
        try:
            return jsonify(fn(**kwargs))
        finally:
            _DEPTH.reset(token)

    def __repr__(self):
        return f"ToolCallable({self._server}.{self._tool})"


class _ServerToolsProxy:
    def __init__(self, server):
        self._server = str(server)

    def __getitem__(self, tool):
        return _ToolCallable(self._server, tool)

    def __getattr__(self, tool):
        if tool.startswith("_"):
            raise AttributeError(tool)
        return _ToolCallable(self._server, tool)

    def __call__(self, tool, **kwargs):
        return _ToolCallable(self._server, tool)(**kwargs)


class _ToolsProxy:
    def __getitem__(self, server):
        return _ServerToolsProxy(server)

    def __getattr__(self, server):
        if server.startswith("_"):
            raise AttributeError(server)
        return _ServerToolsProxy(server)


# --------------------------------------------------------------------------- #
# context.env (per-server; prefixed env wins, bare key is a fallback)
# --------------------------------------------------------------------------- #
class _ServerEnv:
    """Mapping over this server's environment values.

    Reads `<SERVER>__<KEY>` first (so multiple bundled servers keep distinct
    values) and falls back to a bare `<KEY>`.
    """

    def __init__(self, prefix, known_keys):
        self._prefix = prefix
        self._known = list(dict.fromkeys(known_keys or []))

    def _resolve(self, key):
        if self._prefix:
            prefixed = os.environ.get(f"{self._prefix}{key}")
            if prefixed is not None:
                return prefixed
        return os.environ.get(key)

    def get(self, key, default=None):
        value = self._resolve(key)
        return default if value is None else value

    def __getitem__(self, key):
        value = self._resolve(key)
        if value is None:
            raise KeyError(key)
        return value

    def __contains__(self, key):
        return self._resolve(key) is not None

    def keys(self):
        return list(self._known)

    def __iter__(self):
        return iter(self._known)

    def items(self):
        for key in self._known:
            value = self._resolve(key)
            if value is not None:
                yield key, value

    def to_dict(self):
        return dict(self.items())


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #
_DB_SINGLETON = _Database()


class Context:
    def __init__(self, server, env_keys):
        self.db = _DB_SINGLETON
        self.tools = _ToolsProxy()
        cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(server)).strip("_").upper()
        prefix = f"{cleaned}__" if cleaned else ""
        self.env = _ServerEnv(prefix, list(env_keys or []))

    def call(self, server, tool, **kwargs):
        return _ToolCallable(server, tool)(**kwargs)


def build_context(server, env_keys=None):
    return Context(server, list(env_keys or []))
'''
