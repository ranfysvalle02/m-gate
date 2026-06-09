from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from config.settings import get_settings
from database.mongo import get_control_database
from services.admin_session import ADMIN_CSRF_COOKIE


class RbacMiddleware:
    def __init__(self, app):
        self.app = app
        self.settings = get_settings()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        request = Request(scope, receive=receive)
        path = request.url.path

        if self._requires_admin_principal(path):
            if not getattr(request.state, "is_admin_principal", False):
                response: Response
                if path.startswith("/admin"):
                    response = JSONResponse(
                        status_code=401,
                        content={"detail": "Admin authentication required."},
                    )
                else:
                    response = RedirectResponse(url=self._ui_login_path(), status_code=303)
                return await response(scope, request.receive, send)
            if self._requires_csrf(request):
                response = JSONResponse(
                    status_code=403, content={"detail": "CSRF validation failed."}
                )
                return await response(scope, request.receive, send)

        roles = set(getattr(request.state, "roles", []))
        if self.settings.auth_mode != "disabled" and path.startswith("/rpc"):
            tenant_id = getattr(request.state, "tenant_id", self.settings.default_tenant_id)
            user_id = getattr(request.state, "user_id", "unknown-user")
            session = await get_control_database()["session_context"].find_one(
                {"tenant_id": tenant_id, "user_id": user_id}
            )
            if session and isinstance(session.get("roles"), list):
                roles.update(session["roles"])
            request.state.roles = sorted(roles)

        if path.startswith("/rpc"):
            if "admin" not in roles and "tool:invoke" not in roles:
                response = JSONResponse(
                    status_code=403, content={"detail": "Insufficient permissions."}
                )
                return await response(scope, request.receive, send)

        await self.app(scope, request.receive, send)

    def _ui_path(self) -> str:
        return self.settings.admin_ui_path

    def _ui_login_path(self) -> str:
        return f"{self._ui_path()}/login"

    def _ui_logout_path(self) -> str:
        return f"{self._ui_path()}/logout"

    def _is_ui_path(self, path: str) -> bool:
        ui_path = self._ui_path()
        return path == ui_path or path.startswith(f"{ui_path}/")

    def _requires_admin_principal(self, path: str) -> bool:
        if not self.settings.admin_ui_enabled:
            return path.startswith("/admin")
        if path.startswith("/admin"):
            return True
        if not self._is_ui_path(path):
            return False
        return path not in {self._ui_login_path(), self._ui_logout_path()}

    @staticmethod
    def _unsafe_method(request: Request) -> bool:
        return request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}

    def _requires_csrf(self, request: Request) -> bool:
        if not request.url.path.startswith("/admin"):
            return False
        if not self._unsafe_method(request):
            return False
        if not getattr(request.state, "admin_auth_via_cookie", False):
            return False
        header = request.headers.get("x-csrf-token")
        cookie = request.cookies.get(ADMIN_CSRF_COOKIE)
        if not header or not cookie:
            return True
        return not hmac.compare_digest(header, cookie)
