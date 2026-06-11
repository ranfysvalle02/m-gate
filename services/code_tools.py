"""Authoring-time handling for code-backed tools (``transport="code"``).

User-authored Python is validated with a static safety lint, encrypted at rest,
embedded/indexed for discovery, and surfaced in the catalog. Runtime execution
happens separately in the wasm sandbox (``services/sandbox_executor.py`` /
``services/sandbox_worker.py``), and can be disabled via
``CODE_TOOL_EXECUTION_ENABLED``.

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
# bloat the registry document or the sandbox job payload.
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


def _issue(message: str, *, severity: str = "error", line: int | None = None) -> dict[str, Any]:
    return {"severity": severity, "message": message, "line": line}


def _top_level_bindings(tree: ast.Module) -> set[str]:
    """Names bound at module top level.

    Mirrors what the sandbox runner can resolve: after ``exec(raw_code, ns, ns)``
    it does ``ns.get(tool_name)`` and calls it, so any top-level def/class/assign
    with that name is callable. We use this to predict — at authoring time — the
    "function name must match the tool name" requirement the runner enforces.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _function_params(func: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[set[str], set[str], bool]:
    """(all named params, required params, accepts **kwargs) for a function def.
    """
    args = func.args
    positional = list(args.posonlyargs) + list(args.args)
    num_defaults = len(args.defaults)
    params: set[str] = set()
    required: set[str] = set()
    for index, arg in enumerate(positional):
        params.add(arg.arg)
        if index < len(positional) - num_defaults:
            required.add(arg.arg)
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
        params.add(arg.arg)
        if default is None:
            required.add(arg.arg)
    return params, required, args.kwarg is not None


def _signature_issues(
    tree: ast.Module, tool_name: str, input_schema: Any
) -> list[dict[str, Any]]:
    """Advisory checks that the function signature matches the declared schema.

    The runner calls ``func(**arguments)`` where ``arguments`` is built by the
    caller from ``input_schema``. A drift between the two fails at call time, so
    we surface it at authoring time — as *warnings* (never blocking the save),
    because a broader signature or schema is sometimes intentional.
    """
    if not tool_name or not isinstance(input_schema, dict):
        return []
    properties = input_schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return []  # No meaningful schema to compare against yet.
    required_fields = [f for f in (input_schema.get("required") or []) if isinstance(f, str)]

    func: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == tool_name:
            func = node
            break
    if func is None:
        return []  # Name mismatch is reported as an error elsewhere.

    params, required_params, accepts_kwargs = _function_params(func)
    prop_names = set(properties.keys())
    issues: list[dict[str, Any]] = []

    if not accepts_kwargs:
        for field in required_fields:
            if field not in params:
                issues.append(
                    _issue(
                        f"Input schema requires '{field}', but {tool_name}() has no such "
                        "parameter — add it or accept **kwargs.",
                        severity="warning",
                        line=func.lineno,
                    )
                )
    for param in sorted(required_params):
        if param not in prop_names:
            issues.append(
                _issue(
                    f"Parameter '{param}' isn't described in the input schema, so callers "
                    "won't know to provide it.",
                    severity="warning",
                    line=func.lineno,
                )
            )
    return issues


# Python annotation (base name) -> JSON Schema type. Unknown/absent -> "string".
_ANNOTATION_JSON_TYPES = {
    "str": "string",
    "bytes": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "dict": "object",
    "Dict": "object",
    "Mapping": "object",
    "list": "array",
    "List": "array",
    "tuple": "array",
    "Tuple": "array",
    "set": "array",
    "Set": "array",
    "Sequence": "array",
}


def _annotation_name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _annotation_name(node.value)
    if isinstance(node, ast.Constant) and node.value is None:
        return "None"
    return ""


def _annotation_info(node: ast.AST | None) -> tuple[str, bool]:
    """Infer ``(json_type, is_optional)`` from a parameter annotation.

    Unwraps ``Optional[X]`` and ``X | None`` so the JSON type reflects the inner
    type while flagging the parameter as optional.
    """
    if node is None:
        return "string", False
    if isinstance(node, ast.Subscript):
        base = _annotation_name(node.value)
        if base == "Optional":
            inner_type, _ = _annotation_info(node.slice)
            return inner_type, True
        return _ANNOTATION_JSON_TYPES.get(base, "string"), False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left_type, left_opt = _annotation_info(node.left)
        right_name = _annotation_name(node.right)
        if right_name == "None":
            return left_type, True
        right_type, right_opt = _annotation_info(node.right)
        # ``A | None`` is the common case; otherwise keep the left-hand type.
        return left_type, left_opt or right_opt
    return _ANNOTATION_JSON_TYPES.get(_annotation_name(node), "string"), False


def _find_function(
    tree: ast.Module, tool_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    fallback = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if tool_name and node.name == tool_name:
                return node
            if fallback is None:
                fallback = node
    return None if tool_name else fallback


def suggest_input_schema(raw_code: str, tool_name: str) -> dict[str, Any] | None:
    """Derive a JSON-Schema ``input_schema`` from a function's signature.

    Maps each parameter to a property whose type is inferred from its annotation,
    and marks annotated-required params (no
    default, not ``Optional``) as ``required``. Returns ``None`` when there's no
    matching function or it takes no inputs — i.e. nothing to suggest.
    """
    if not isinstance(raw_code, str) or not raw_code.strip():
        return None
    try:
        tree = ast.parse(raw_code)
    except SyntaxError:
        return None
    func = _find_function(tree, tool_name)
    if func is None:
        return None

    args = func.args
    positional = list(args.posonlyargs) + list(args.args)
    num_defaults = len(args.defaults)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for index, arg in enumerate(positional):
        json_type, optional = _annotation_info(arg.annotation)
        properties[arg.arg] = {"type": json_type}
        has_default = index >= len(positional) - num_defaults
        if not has_default and not optional:
            required.append(arg.arg)
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
        json_type, optional = _annotation_info(arg.annotation)
        properties[arg.arg] = {"type": json_type}
        if default is None and not optional:
            required.append(arg.arg)

    if not properties:
        return None
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _source_issues(
    raw_code: str, tool_name: str, input_schema: Any = None
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if len(raw_code.encode("utf-8")) > MAX_RAW_CODE_BYTES:
        issues.append(_issue(f"Code exceeds the maximum size of {MAX_RAW_CODE_BYTES // 1024} KB."))
    try:
        tree = ast.parse(raw_code)
    except SyntaxError as exc:
        issues.append(
            _issue(f"Code is not valid Python: {exc.msg} (line {exc.lineno}).", line=exc.lineno)
        )
        return issues  # Can't inspect further once parsing failed.

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_IMPORTS:
                    issues.append(
                        _issue(f"Importing '{root}' is not allowed.", line=node.lineno)
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_IMPORTS:
                issues.append(
                    _issue(f"Importing from '{root}' is not allowed.", line=node.lineno)
                )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALLS:
                issues.append(_issue(f"Calling '{func.id}()' is not allowed.", line=node.lineno))
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_ATTRIBUTES:
                issues.append(
                    _issue(f"Accessing attribute '{node.attr}' is not allowed.", line=node.lineno)
                )
        elif isinstance(node, ast.Name) and node.id in BANNED_CALLS:
            # Bare reference (e.g. assigning ``f = exec``) is just as dangerous.
            issues.append(_issue(f"Referencing '{node.id}' is not allowed.", line=node.lineno))

    # The runner calls ``namespace.get(tool_name)``; without a matching top-level
    # binding every invocation fails at runtime with "is not callable". Catch the
    # mismatch here so it can never reach storage.
    if tool_name and tool_name not in _top_level_bindings(tree):
        issues.append(
            _issue(
                f"Define a top-level function named '{tool_name}' — the function name "
                "must match the tool name."
            )
        )
    else:
        issues.extend(_signature_issues(tree, tool_name, input_schema))
    return issues


def _requirement_issues(requirements: list[Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for requirement in requirements:
        if not isinstance(requirement, str) or not _REQUIREMENT_RE.match(requirement.strip()):
            issues.append(
                _issue(
                    f"Requirement '{requirement}' must be a pinned PyPI spec like "
                    "'package==1.2.3' (no URLs, VCS, paths, or version ranges)."
                )
            )
    return issues


def validate_code_tool(tool: dict[str, Any]) -> list[dict[str, Any]]:
    """Structured authoring-time validation; returns issue dicts (never raises).

    Each issue is ``{"severity": "error" | "warning", "message": str, "line": int | None}``.
    An empty list means :func:`lint_code_tool` will accept the tool and the sandbox
    runner can call it. This is the single source of truth shared by the save path,
    the ``/admin/code-tools/validate`` endpoint, and the admin UI — so what the
    author sees while typing is exactly what the server enforces on save.
    """
    tool_name = (tool.get("name") or "").strip()
    issues: list[dict[str, Any]] = []
    raw_code = tool.get("raw_code")
    if not isinstance(raw_code, str) or not raw_code.strip():
        issues.append(_issue("Function requires non-empty 'raw_code'."))
    else:
        issues.extend(_source_issues(raw_code, tool_name, tool.get("input_schema")))
    issues.extend(_requirement_issues(list(tool.get("requirements") or [])))

    metadata = tool.get("metadata") or {}
    action_type = metadata.get("action_type")
    if action_type is not None and action_type not in ALLOWED_ACTION_TYPES:
        issues.append(
            _issue(
                f"metadata.action_type '{action_type}' must be one of "
                f"{', '.join(sorted(ALLOWED_ACTION_TYPES))}."
            )
        )
    return issues


def lint_code_tool(tool: dict[str, Any]) -> None:
    """Validate one authored code tool, raising ``CodeToolValidationError``.

    Thin wrapper over :func:`validate_code_tool`: raises if any error-severity
    issue is present, joining their messages into one rejection string.
    """
    name = tool.get("name") or "<unnamed>"
    errors = [issue["message"] for issue in validate_code_tool(tool) if issue["severity"] == "error"]
    if errors:
        raise CodeToolValidationError(f"Code tool '{name}' rejected: " + " ".join(errors))


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
