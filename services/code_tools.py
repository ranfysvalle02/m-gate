"""Authoring-time handling for code-backed tools (``transport="code"``).

Phase 2 is *storage only*: user-authored Python is validated with a static safety
lint, encrypted at rest, embedded/indexed for discovery, and surfaced in the
catalog — but it is **never executed** here. A sandboxed runtime arrives in
Phase 3; until then ``tools/call`` against a code tool returns a clear
"execution not enabled" error.

The lint is a defense-in-depth gate, not a security boundary. Real isolation is
the sandbox's job; this just rejects obvious abuse and unpinned dependencies so
junk never reaches the runtime in the first place.
"""

from __future__ import annotations

import ast
import re
from typing import Any

from config.settings import Settings
from services.embedding_config import decrypt_tenant_api_key, encrypt_tenant_api_key

CODE_TRANSPORT = "code"

# A single authored function is small; cap the source so a pasted blob can't
# bloat the registry document or the (future) sandbox image build context.
MAX_RAW_CODE_BYTES = 64 * 1024

ALLOWED_ACTION_TYPES = frozenset({"read", "write", "destructive"})

# Modules that have no business inside a user tool and signal an escape attempt
# or host-level access. (The sandbox will enforce the real boundary; this keeps
# the obvious cases out of storage.)
BANNED_IMPORTS = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "shutil",
        "ctypes",
        "multiprocessing",
        "importlib",
        "marshal",
        "pickle",
        "builtins",
        "resource",
        "signal",
        "pty",
    }
)

# Builtins that execute arbitrary code, reach the import machinery, or touch the
# filesystem/global namespace.
BANNED_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "globals",
        "locals",
        "vars",
        "open",
        "breakpoint",
    }
)

# Dunder attributes used in classic sandbox-escape chains
# (e.g. ``().__class__.__bases__[0].__subclasses__()``).
BANNED_ATTRIBUTES = frozenset(
    {
        "__globals__",
        "__builtins__",
        "__subclasses__",
        "__bases__",
        "__mro__",
        "__code__",
        "__dict__",
        "__import__",
        "__getattribute__",
        "__reduce__",
    }
)

# Pinned PyPI requirement: ``name`` or ``name[extra,...]`` followed by ``==`` and
# a concrete version. Anything with whitespace, URLs, VCS, file/local paths, or a
# non-exact operator (``>=``, ``~=``, ``*``) fails to match and is rejected.
_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"  # distribution name
    r"(?:\[[A-Za-z0-9,._-]+\])?"  # optional extras
    r"==[A-Za-z0-9][A-Za-z0-9.\-_+!]*$"  # exact version pin
)


# At-rest token prefixes produced by the tenant-secret cipher (see
# services/embedding_config.py): Fernet (``enc::``) or per-tenant QE (``qe::``).
_ENCRYPTED_PREFIXES = ("enc::", "qe::")


class CodeToolValidationError(ValueError):
    """A code tool failed the authoring-time safety lint."""


def is_encrypted_token(value: Any) -> bool:
    """True if ``value`` is already an encrypted at-rest token (avoid re-encrypt)."""
    return isinstance(value, str) and value.startswith(_ENCRYPTED_PREFIXES)


def _lint_source(raw_code: str) -> list[str]:
    issues: list[str] = []
    if len(raw_code.encode("utf-8")) > MAX_RAW_CODE_BYTES:
        issues.append(f"Code exceeds the maximum size of {MAX_RAW_CODE_BYTES // 1024} KB.")
    try:
        tree = ast.parse(raw_code)
    except SyntaxError as exc:
        issues.append(f"Code is not valid Python: {exc.msg} (line {exc.lineno}).")
        return issues  # Can't inspect further once parsing failed.

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_IMPORTS:
                    issues.append(f"Importing '{root}' is not allowed.")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_IMPORTS:
                issues.append(f"Importing from '{root}' is not allowed.")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALLS:
                issues.append(f"Calling '{func.id}()' is not allowed.")
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_ATTRIBUTES:
                issues.append(f"Accessing attribute '{node.attr}' is not allowed.")
        elif isinstance(node, ast.Name) and node.id in BANNED_CALLS:
            # Bare reference (e.g. assigning ``f = exec``) is just as dangerous.
            issues.append(f"Referencing '{node.id}' is not allowed.")
    return issues


def _lint_requirements(requirements: list[Any]) -> list[str]:
    issues: list[str] = []
    for requirement in requirements:
        if not isinstance(requirement, str) or not _REQUIREMENT_RE.match(requirement.strip()):
            issues.append(
                f"Requirement '{requirement}' must be a pinned PyPI spec like "
                "'package==1.2.3' (no URLs, VCS, paths, or version ranges)."
            )
    return issues


def lint_code_tool(tool: dict[str, Any]) -> None:
    """Validate one authored code tool, raising ``CodeToolValidationError``.

    Checks: non-empty source within the size cap and parseable Python free of
    obvious import/exec abuse; pinned PyPI requirements; and a recognized
    ``metadata.action_type`` when present.
    """
    name = tool.get("name") or "<unnamed>"
    raw_code = tool.get("raw_code")
    issues: list[str] = []
    if not isinstance(raw_code, str) or not raw_code.strip():
        raise CodeToolValidationError(f"Code tool '{name}' requires non-empty 'raw_code'.")
    issues.extend(_lint_source(raw_code))
    issues.extend(_lint_requirements(list(tool.get("requirements") or [])))

    metadata = tool.get("metadata") or {}
    action_type = metadata.get("action_type")
    if action_type is not None and action_type not in ALLOWED_ACTION_TYPES:
        issues.append(
            f"metadata.action_type '{action_type}' must be one of "
            f"{', '.join(sorted(ALLOWED_ACTION_TYPES))}."
        )
    if issues:
        raise CodeToolValidationError(f"Code tool '{name}' rejected: " + " ".join(issues))


async def encrypt_raw_code(
    tenant_id: str, raw_code: str, settings: Settings | None = None
) -> str | None:
    """Encrypt authored source at rest, reusing the tenant-secret cipher.

    Uses the per-tenant Queryable Encryption DEK when QE is enabled and a
    deployment-wide Fernet key otherwise — the same scheme that protects tenant
    embedding API keys (``database/encryption.py`` via ``services/embedding_config``).
    Returns the encoded token (``qe::`` or ``enc::`` prefixed) or ``None``.
    """
    return await encrypt_tenant_api_key(tenant_id, raw_code, settings)


async def decrypt_raw_code(
    tenant_id: str, stored: str | None, settings: Settings | None = None
) -> str:
    """Reverse :func:`encrypt_raw_code`; returns ``""`` if it cannot be decrypted."""
    return await decrypt_tenant_api_key(tenant_id, stored, settings)
