from __future__ import annotations

import pytest

from services.code_tools import (
    MAX_RAW_CODE_BYTES,
    CodeToolValidationError,
    decrypt_raw_code,
    encrypt_raw_code,
    is_encrypted_token,
    lint_code_tool,
    suggest_input_schema,
    validate_code_tool,
)

_GOOD_CODE = "def add(a: int, b: int) -> int:\n    return a + b\n"


def _tool(**overrides):
    base = {
        "name": "add",
        "description": "Add two numbers",
        "raw_code": _GOOD_CODE,
        "requirements": [],
        "metadata": {"action_type": "read"},
    }
    base.update(overrides)
    return base


def test_lint_accepts_clean_function():
    lint_code_tool(_tool())  # should not raise


def test_lint_requires_non_empty_code():
    with pytest.raises(CodeToolValidationError, match="non-empty"):
        lint_code_tool(_tool(raw_code="   "))


def test_lint_rejects_oversized_code():
    big = "x = 1\n" * (MAX_RAW_CODE_BYTES // 5)
    with pytest.raises(CodeToolValidationError, match="maximum size"):
        lint_code_tool(_tool(raw_code=big))


def test_lint_rejects_syntax_error():
    with pytest.raises(CodeToolValidationError, match="not valid Python"):
        lint_code_tool(_tool(raw_code="def broken(:\n    pass\n"))


@pytest.mark.parametrize(
    "code",
    [
        "import os\n\ndef f():\n    return os.getcwd()\n",
        "from subprocess import run\n\ndef f():\n    return run(['ls'])\n",
        "import socket\n\ndef f():\n    return socket.socket()\n",
    ],
)
def test_lint_rejects_banned_imports(code):
    with pytest.raises(CodeToolValidationError, match="not allowed"):
        lint_code_tool(_tool(raw_code=code))


@pytest.mark.parametrize(
    "code",
    [
        "def f():\n    return eval('1+1')\n",
        "def f():\n    return exec('x=1')\n",
        "def f():\n    return open('/etc/passwd').read()\n",
    ],
)
def test_lint_rejects_dangerous_calls(code):
    with pytest.raises(CodeToolValidationError, match="not allowed"):
        lint_code_tool(_tool(raw_code=code))


def test_lint_rejects_dunder_escape():
    code = "def f():\n    return ().__class__.__bases__\n"
    with pytest.raises(CodeToolValidationError, match="not allowed"):
        lint_code_tool(_tool(raw_code=code))


def test_lint_accepts_pinned_requirements():
    lint_code_tool(_tool(requirements=["httpx==0.27.0", "pydantic[email]==2.6.1"]))


@pytest.mark.parametrize(
    "requirement",
    [
        "httpx",  # unpinned
        "httpx>=0.27.0",  # range, not exact
        "httpx==*",  # wildcard
        "requests @ git+https://example.com/requests.git",  # VCS
        "./local-pkg",  # local path
        "file:///tmp/pkg",  # file URL
        "evil package==1.0",  # whitespace
    ],
)
def test_lint_rejects_bad_requirements(requirement):
    with pytest.raises(CodeToolValidationError, match="pinned PyPI spec"):
        lint_code_tool(_tool(requirements=[requirement]))


def test_lint_rejects_unknown_action_type():
    with pytest.raises(CodeToolValidationError, match="action_type"):
        lint_code_tool(_tool(metadata={"action_type": "nuke"}))


def test_lint_rejects_function_name_mismatch():
    # Tool named "add" but the source defines "subtract" -> the runner's
    # namespace.get("add") would fail at call time, so save must reject it.
    code = "def subtract(a: int, b: int) -> int:\n    return a - b\n"
    with pytest.raises(CodeToolValidationError, match="must match the tool name"):
        lint_code_tool(_tool(name="add", raw_code=code))


def test_lint_accepts_top_level_lambda_binding():
    # A top-level binding (not only a def) also satisfies the runner contract.
    code = "add = lambda a, b: a + b\n"
    lint_code_tool(_tool(name="add", raw_code=code))  # should not raise


def test_validate_returns_empty_for_clean_tool():
    assert validate_code_tool(_tool()) == []


def test_validate_reports_severity_and_line_numbers():
    code = "def add(a, b):\n    import os\n    return os.getcwd()\n"
    issues = validate_code_tool(_tool(raw_code=code))
    assert any(i["severity"] == "error" for i in issues)
    banned = next(i for i in issues if "not allowed" in i["message"])
    assert banned["line"] == 2


def test_validate_reports_syntax_error_line():
    issues = validate_code_tool(_tool(raw_code="def broken(:\n    pass\n"))
    assert len(issues) == 1
    assert issues[0]["severity"] == "error"
    assert issues[0]["line"] == 1


def test_validate_never_raises_on_empty_code():
    issues = validate_code_tool(_tool(raw_code=""))
    assert any("non-empty" in i["message"] for i in issues)


def test_validate_warns_on_signature_schema_drift():
    code = "def add(a):\n    return a\n"
    schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    issues = validate_code_tool(_tool(raw_code=code, input_schema=schema))
    warnings = [i for i in issues if i["severity"] == "warning"]
    assert any("Input schema requires 'city'" in i["message"] for i in warnings)
    assert any("Parameter 'a'" in i["message"] for i in warnings)
    # Warnings are advisory: the hard lint gate must NOT raise on them.
    lint_code_tool(_tool(raw_code=code, input_schema=schema))


def test_validate_no_signature_warnings_when_aligned():
    code = "def add(a, b):\n    return a + b\n"
    schema = {"type": "object", "properties": {"a": {}, "b": {}}, "required": ["a", "b"]}
    issues = validate_code_tool(_tool(raw_code=code, input_schema=schema))
    assert [i for i in issues if i["severity"] == "warning"] == []


def test_validate_kwargs_suppresses_missing_param_warning():
    code = "def add(a, **kwargs):\n    return a\n"
    schema = {"type": "object", "properties": {"a": {}, "city": {}}, "required": ["a", "city"]}
    issues = validate_code_tool(_tool(raw_code=code, input_schema=schema))
    assert not any("no such parameter" in i["message"] for i in issues)


def test_validate_ignores_empty_schema():
    code = "def add(a):\n    return a\n"
    issues = validate_code_tool(_tool(raw_code=code, input_schema={}))
    assert [i for i in issues if i["severity"] == "warning"] == []


def test_suggest_schema_infers_types_and_required():
    code = (
        "def fetch(city: str, days: int = 3, verbose: bool = False) -> dict:\n"
        "    return {}\n"
    )
    schema = suggest_input_schema(code, "fetch")
    assert schema == {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "days": {"type": "integer"},
            "verbose": {"type": "boolean"},
        },
        "required": ["city"],
    }


def test_suggest_schema_unwraps_optional_params():
    code = (
        "from typing import Optional\n"
        "def f(name: str, nick: Optional[str] = None, secrets: dict = {}) -> dict:\n"
        "    return {}\n"
    )
    schema = suggest_input_schema(code, "f")
    assert schema["properties"]["secrets"] == {"type": "object"}
    assert schema["properties"]["nick"] == {"type": "string"}
    assert schema["required"] == ["name"]  # Optional -> not required


def test_suggest_schema_returns_none_without_params_or_function():
    assert suggest_input_schema("def f():\n    return 1\n", "f") is None
    assert suggest_input_schema("x = 1\n", "f") is None
    assert suggest_input_schema("def broken(:\n", "f") is None


@pytest.mark.asyncio
async def test_encrypt_decrypt_roundtrip():
    token = await encrypt_raw_code("tenant-x", _GOOD_CODE)
    assert token is not None
    assert is_encrypted_token(token)
    assert _GOOD_CODE not in token  # ciphertext, not plaintext
    restored = await decrypt_raw_code("tenant-x", token)
    assert restored == _GOOD_CODE


@pytest.mark.asyncio
async def test_encrypt_empty_returns_none():
    assert await encrypt_raw_code("tenant-x", "") is None


def test_is_encrypted_token_detects_prefixes():
    assert is_encrypted_token("enc::abc")
    assert is_encrypted_token("qe::abc")
    assert not is_encrypted_token("def f(): pass")
    assert not is_encrypted_token(None)
