from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import get_settings
from gateway.middleware.auth import AuthMiddleware
from gateway.middleware.rbac import RbacMiddleware
from gateway.routers.ui import router as ui_router


def _build_ui_app() -> FastAPI:
    app = FastAPI()
    settings = get_settings()
    app.add_middleware(RbacMiddleware)
    app.add_middleware(AuthMiddleware)
    app.include_router(ui_router, prefix=settings.admin_ui_path)
    return app


def test_ui_login_page_public_under_hs256(monkeypatch, reset_settings):
    monkeypatch.setenv("AUTH_MODE", "hs256")
    monkeypatch.setenv("JWT_SECRET", "super-secret-for-tests")
    monkeypatch.setenv("ADMIN_EMAIL", "demo@demo.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "demo-password")
    client = TestClient(_build_ui_app())
    response = client.get("/ui/login")
    assert response.status_code == 200
    assert "Admin Login" in response.text


def test_ui_home_redirects_to_login_without_session(monkeypatch, reset_settings):
    monkeypatch.setenv("AUTH_MODE", "hs256")
    monkeypatch.setenv("JWT_SECRET", "super-secret-for-tests")
    monkeypatch.setenv("ADMIN_EMAIL", "demo@demo.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "demo-password")
    client = TestClient(_build_ui_app())
    response = client.get("/ui/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/ui/login")


def test_ui_login_sets_session_cookie(monkeypatch, reset_settings):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    monkeypatch.setenv("ADMIN_EMAIL", "demo@demo.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "demo-password")
    client = TestClient(_build_ui_app())
    login = client.post(
        "/ui/login",
        data={"email": "demo@demo.com", "password": "demo-password"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert "admin_session=" in login.headers.get("set-cookie", "")
    home = client.get("/ui/")
    assert home.status_code == 200
    assert "Admin Console" in home.text


def test_create_app_omits_ui_routes_when_disabled(monkeypatch, reset_settings):
    monkeypatch.setenv("ADMIN_UI_ENABLED", "false")
    from gateway.app import create_app

    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/ui/" not in paths
    assert "/ui/login" not in paths
