from __future__ import annotations

import hashlib
import logging
from pathlib import Path
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
    verify_session,
)
from services.users import resolve_login_principal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory="gateway/templates")

# Shown as the "Effective" date on the public Terms of Use / Privacy Policy pages.
# Bump this whenever the legal documents materially change.
LEGAL_EFFECTIVE_DATE = "June 19, 2026"

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _asset_version() -> str:
    """Cache-busting token derived from static asset mtimes.

    Browsers aggressively cache ``/static/app.js`` and ``/static/styles.css``. Without
    a changing query string the user keeps running a stale bundle after a redeploy --
    e.g. the template calls a method that only exists in the new JS, so Alpine throws
    ``X is not defined``. Recomputed per render (it only stats a handful of small
    files) so any edit is reflected on the very next page load, while unchanged assets
    still hit the browser cache.
    """
    try:
        mtimes = sorted(p.stat().st_mtime_ns for p in _STATIC_DIR.glob("*") if p.is_file())
    except OSError:  # pragma: no cover - defensive: static dir missing
        mtimes = []
    if not mtimes:
        return "0"
    return hashlib.sha1(repr(mtimes).encode("utf-8")).hexdigest()[:12]


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
            "asset_version": _asset_version(),
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
            "asset_version": _asset_version(),
            # Drives the flag-gated "Create an account" link to /ui/register.
            "self_registration_enabled": settings.self_registration_enabled,
        },
    )


@router.get("/register", response_class=HTMLResponse, name="ui_register")
async def ui_register(request: Request) -> Response:
    """Public self-service sign-up page (flag-gated).

    Reachable at ``/ui/register`` without a session — the auth middleware lets all
    ``/ui/*`` paths through, and the page POSTs to the public ``/auth/register``
    endpoint. When self-registration is disabled we redirect to the login page so
    the route never advertises a closed beta.
    """
    settings = get_settings()
    if not settings.self_registration_enabled:
        return RedirectResponse(url=_ui_login_path(), status_code=303)
    if _session_claims(request) is not None:
        return RedirectResponse(url=_ui_home_path(), status_code=303)
    return templates.TemplateResponse(
        request,
        "register.html",
        {
            "ui_path": settings.admin_ui_path,
            "asset_version": _asset_version(),
            "min_password_length": settings.self_registration_min_password_length,
        },
    )


@router.get("/terms", response_class=HTMLResponse, name="ui_terms")
async def ui_terms(request: Request) -> Response:
    """Public Terms of Use page (no session required)."""
    return templates.TemplateResponse(
        request,
        "terms.html",
        {
            "ui_path": get_settings().admin_ui_path,
            "asset_version": _asset_version(),
            "effective_date": LEGAL_EFFECTIVE_DATE,
        },
    )


@router.get("/privacy", response_class=HTMLResponse, name="ui_privacy")
async def ui_privacy(request: Request) -> Response:
    """Public Privacy Policy page (no session required)."""
    return templates.TemplateResponse(
        request,
        "privacy.html",
        {
            "ui_path": get_settings().admin_ui_path,
            "asset_version": _asset_version(),
            "effective_date": LEGAL_EFFECTIVE_DATE,
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

    principal = await resolve_login_principal(email, password)
    if principal is None:
        if wants_json:
            return JSONResponse(status_code=401, content={"detail": "Invalid admin credentials."})
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "ui_path": _ui_home_path(),
                "error": "Invalid email or password.",
                "asset_version": _asset_version(),
                # Keep the "Create an account" link visible after a failed attempt.
                "self_registration_enabled": get_settings().self_registration_enabled,
            },
            status_code=401,
        )

    token = mint_session(
        principal["email"],
        tenant_id=principal["tenant_id"],
        roles=principal["roles"],
    )
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
