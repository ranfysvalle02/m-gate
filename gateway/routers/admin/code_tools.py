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
from services.tenant_pip_policy import (
    PipPolicyDecision,
    evaluate_tenant_requirements,
    get_effective_pip_allowlist,
)

from . import _common as c
from ._common import _require_tenant_admin, _resolve_target_tenant, router, settings


def _pip_policy_issues(decision: PipPolicyDecision) -> list[CodeToolValidationIssue]:
    """Turn a blocked-requirement decision into authoring-time error issues.

    Split by who can unblock each package so the author is pointed at the right
    actor (platform operator vs. tenant admin) — the same distinction the runtime
    install error carries.
    """
    issues: list[CodeToolValidationIssue] = []
    for name in decision.blocked_by_global:
        issues.append(
            CodeToolValidationIssue(
                severity="error",
                message=(
                    f"Package '{name}' isn't permitted by the platform pip ceiling "
                    "(SANDBOX_ALLOWED_REQUIREMENTS); a platform operator must allow it."
                ),
            )
        )
    for name in decision.blocked_by_tenant:
        issues.append(
            CodeToolValidationIssue(
                severity="error",
                message=(
                    f"Package '{name}' isn't in this tenant's code-package policy; a "
                    "tenant admin must add it to the tenant's allowed packages."
                ),
            )
        )
    return issues


@router.post("/code-tools/validate", response_model=CodeToolValidateResponse)
async def validate_code_tool_endpoint(
    request: Request, payload: CodeToolValidateRequest
) -> CodeToolValidateResponse:
    """Lint authored Python and check requirements against the tenant pip policy.

    Returns the exact set of issues the save path enforces (the static
    :func:`validate_code_tool` lint plus the effective ``tenant ∩ global_ceiling``
    pip-policy check), so the admin UI can block a broken save before it is
    attempted and surface line-accurate, actor-targeted problems while the author
    types. The one cheap DB read is the cached tenant allowlist.
    """
    _require_tenant_admin(request)
    target_tenant = _resolve_target_tenant(request, payload.tenant_id)
    requirements = [str(req).strip() for req in (payload.requirements or []) if str(req).strip()]
    issues = validate_code_tool(
        {
            "name": payload.name,
            "raw_code": payload.raw_code,
            "requirements": requirements,
            "metadata": {"action_type": payload.action_type},
            "input_schema": payload.input_schema,
        }
    )
    typed_issues = [CodeToolValidationIssue(**issue) for issue in issues]
    decision = await evaluate_tenant_requirements(target_tenant, requirements, settings=settings)
    typed_issues.extend(_pip_policy_issues(decision))
    ok = not any(issue.severity == "error" for issue in typed_issues)
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

    # Fail fast (before spawning the sandbox) when a requirement is outside the
    # effective ``tenant ∩ global_ceiling`` policy, with the same actor-targeted
    # message the runtime install would raise.
    requirements = list(candidate_tool["requirements"])
    decision = await evaluate_tenant_requirements(target_tenant, requirements, settings=settings)
    if not decision.ok:
        return CodeToolTestResponse(ok=False, error=decision.error_message())
    allowed_requirements = (
        tuple(await get_effective_pip_allowlist(target_tenant, settings=settings))
        if requirements
        else ()
    )

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
    proxy_registry = c.get_proxy_registry()
    tool_invoker = proxy_registry.make_tool_invoker(
        tenant_id=target_tenant,
        caller=test_caller,
        call_depth=0,
    )
    # Load the server's secrets so a workbench "Run" can exercise context.http
    # secret injection (auth="ENV_KEY") exactly like production. Fail-soft: an
    # unsaved draft simply has no secrets yet.
    server_env = await proxy_registry.read_server_env(target_tenant, server_name)
    try:
        result = await asyncio.wait_for(
            executor.run(
                ExecRequest(
                    tenant_id=target_tenant,
                    server=server_name,
                    tool=tool_name,
                    raw_code=payload.raw_code,
                    requirements=requirements,
                    arguments=payload.arguments if isinstance(payload.arguments, dict) else {},
                    env=server_env,
                    action_type=payload.action_type,
                    tool_invoker=tool_invoker,
                    allowed_requirements=allowed_requirements,
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
