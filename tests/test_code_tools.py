from __future__ import annotations

import pytest

from services.code_tools import (
    MAX_RAW_CODE_BYTES,
    CodeToolValidationError,
    decrypt_raw_code,
    encrypt_raw_code,
    is_encrypted_token,
    lint_code_tool,
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
