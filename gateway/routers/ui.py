from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from config.settings import get_settings
from services.admin_session import (
    ADMIN_CSRF_COOKIE,
    ADMIN_SESSION_COOKIE,
    generate_csrf_token,
    mint_session,
    verify_credentials,
    verify_session,
)

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory="gateway/templates")


def _wants_json_response(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept


def _session_claims(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(ADMIN_SESSION_COOKIE)
    if not token:
        return None
    return verify_session(token)


def _ui_home_path() -> str:
    return get_settings().admin_ui_path


def _ui_login_path() -> str:
    return f"{_ui_home_path()}/login"


@router.get("/", response_class=HTMLResponse, name="ui_home")
async def ui_home(request: Request) -> Response:
    claims = _session_claims(request)
    if claims is None:
        return RedirectResponse(url=_ui_login_path(), status_code=303)
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "ui_path": settings.admin_ui_path,
            "default_tenant_id": settings.default_tenant_id,
            "csrf_cookie_name": ADMIN_CSRF_COOKIE,
            "logged_in_email": str(claims.get("sub", "")),
        },
    )


@router.get("/login", response_class=HTMLResponse, name="ui_login")
async def ui_login(request: Request) -> Response:
    if _session_claims(request) is not None:
        return RedirectResponse(url=_ui_home_path(), status_code=303)
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "ui_path": settings.admin_ui_path,
            "error": None,
        },
    )


@router.post("/login", name="ui_login_post")
async def ui_login_post(request: Request) -> Response:
    wants_json = _wants_json_response(request)
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        payload = await request.json()
        email = str(payload.get("email", ""))
        password = str(payload.get("password", ""))
    else:
        body = (await request.body()).decode("utf-8")
        parsed = parse_qs(body, keep_blank_values=True)
        email = parsed.get("email", [""])[0]
        password = parsed.get("password", [""])[0]

    if not verify_credentials(email, password):
        if wants_json:
            return JSONResponse(status_code=401, content={"detail": "Invalid admin credentials."})
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "ui_path": _ui_home_path(),
                "error": "Invalid email or password.",
            },
            status_code=401,
        )

    token = mint_session(email)
    if wants_json:
        return JSONResponse(content={"token": token})

    settings = get_settings()
    csrf_token = generate_csrf_token()
    response = RedirectResponse(url=settings.admin_ui_path, status_code=303)
    secure_cookie = request.url.scheme == "https"
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=token,
        max_age=settings.admin_session_ttl_seconds,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=ADMIN_CSRF_COOKIE,
        value=csrf_token,
        max_age=settings.admin_session_ttl_seconds,
        httponly=False,
        secure=secure_cookie,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout", name="ui_logout")
async def ui_logout(request: Request) -> Response:
    response: Response
    if _wants_json_response(request):
        response = JSONResponse(content={"ok": True})
    else:
        response = RedirectResponse(url=_ui_login_path(), status_code=303)
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
    response.delete_cookie(ADMIN_CSRF_COOKIE, path="/")
    return response
