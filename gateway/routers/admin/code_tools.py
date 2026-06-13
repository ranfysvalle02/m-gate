"""Authoring-time linting and sandboxed test-runs for code-backed tools."""

from __future__ import annotations

import asyncio

from fastapi import Request

from models.admin import (
    CodeToolTestRequest,
    CodeToolTestResponse,
    CodeToolValidateRequest,
    CodeToolValidateResponse,
    CodeToolValidationIssue,
)
from services.code_tools import (
    CodeToolValidationError,
    lint_code_tool,
    suggest_input_schema,
    validate_code_tool,
)
from services.credential_broker import CallerIdentity
from services.sandbox_executor import (
    ExecRequest,
    SandboxError,
    SandboxProtocolError,
    SandboxTimeoutError,
)

from . import _common as c
from ._common import _require_tenant_admin, _resolve_target_tenant, router, settings


@router.post("/code-tools/validate", response_model=CodeToolValidateResponse)
async def validate_code_tool_endpoint(
    request: Request, payload: CodeToolValidateRequest
) -> CodeToolValidateResponse:
    """Lint authored Python without executing it.

    Returns the exact set of issues the save path enforces (same
    :func:`validate_code_tool`), so the admin UI can block a broken save before it
    is attempted and surface line-accurate problems while the author types. Pure
    and cheap: no DB access, no sandbox spawn.
    """
    _require_tenant_admin(request)
    issues = validate_code_tool(
        {
            "name": payload.name,
            "raw_code": payload.raw_code,
            "requirements": [
                str(req).strip() for req in (payload.requirements or []) if str(req).strip()
            ],
            "metadata": {"action_type": payload.action_type},
            "input_schema": payload.input_schema,
        }
    )
    ok = not any(issue["severity"] == "error" for issue in issues)
    typed_issues = [CodeToolValidationIssue(**issue) for issue in issues]
    return CodeToolValidateResponse(
        ok=ok,
        issues=typed_issues,
        suggested_schema=suggest_input_schema(payload.raw_code, payload.name),
    )


@router.post(
    "/servers/{server_name}/tools/{tool_name}/test",
    response_model=CodeToolTestResponse,
)
async def test_code_tool(
    request: Request,
    server_name: str,
    tool_name: str,
    payload: CodeToolTestRequest,
) -> CodeToolTestResponse:
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    candidate_tool = {
        "server": server_name,
        "name": tool_name,
        "description": "sandbox test run",
        "raw_code": payload.raw_code,
        "requirements": [
            str(req).strip() for req in (payload.requirements or []) if str(req).strip()
        ],
        "metadata": {
            "action_type": payload.action_type,
            "requires_confirmation": bool(payload.requires_confirmation),
        },
        "input_schema": {},
        "scopes": [],
    }
    try:
        lint_code_tool(candidate_tool)
    except CodeToolValidationError as exc:
        return CodeToolTestResponse(ok=False, error=str(exc))

    timeout_seconds = settings.sandbox_wall_timeout_ms / 1000
    executor = c.get_executor()
    # Let the workbench "Run" exercise context.tools just like production: an
    # admin caller can reach sibling code tools, still re-authorized + restricted
    # to code servers + no confirmation-gated tools by the shared invoker.
    test_caller = CallerIdentity(
        user_id=str(getattr(request.state, "user_id", "") or "admin-test"),
        scopes=["server:*"],
        roles=["admin"],
    )
    tool_invoker = c.get_proxy_registry().make_tool_invoker(
        tenant_id=target_tenant,
        caller=test_caller,
        call_depth=0,
    )
    try:
        result = await asyncio.wait_for(
            executor.run(
                ExecRequest(
                    tenant_id=target_tenant,
                    server=server_name,
                    tool=tool_name,
                    raw_code=payload.raw_code,
                    requirements=list(candidate_tool["requirements"]),
                    arguments=payload.arguments if isinstance(payload.arguments, dict) else {},
                    env={},
                    action_type=payload.action_type,
                    tool_invoker=tool_invoker,
                )
            ),
            timeout=timeout_seconds,
        )
        return CodeToolTestResponse(ok=True, result=result.payload, elapsed_ms=result.elapsed_ms)
    except TimeoutError:
        return CodeToolTestResponse(
            ok=False,
            error=(
                f"Sandbox test exceeded {settings.sandbox_wall_timeout_ms}ms timeout. "
                "Optimize your function or inputs."
            ),
        )
    except SandboxTimeoutError as exc:
        return CodeToolTestResponse(ok=False, error=str(exc))
    except (SandboxProtocolError, SandboxError, ValueError) as exc:
        return CodeToolTestResponse(ok=False, error=str(exc))
