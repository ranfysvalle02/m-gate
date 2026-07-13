"""Capture Admin Console screenshots for docs / design review.

Drives a real Chromium session through the redesigned admin UI and writes PNGs
into ``screenshots/``. Assumes the stack is already running (``docker compose up
-d gateway``) and reachable at ``BASE_URL`` with the seeded demo admin login.

Usage:
    python -m playwright install chromium   # one-time
    python scripts/ui_screenshots.py

Environment:
    GATEWAY_BASE_URL   default http://localhost:8000
    GATEWAY_ADMIN_EMAIL / GATEWAY_ADMIN_PASSWORD   default demo@demo.com / demo
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

BASE_URL = os.environ.get("GATEWAY_BASE_URL", "http://localhost:8000").rstrip("/")
EMAIL = os.environ.get("GATEWAY_ADMIN_EMAIL", "demo@demo.com")
PASSWORD = os.environ.get("GATEWAY_ADMIN_PASSWORD", "demo")

OUT_DIR = Path(__file__).resolve().parent.parent / "screenshots"
VIEWPORT = {"width": 1440, "height": 960}


def _shot(page: Page, name: str, *, full_page: bool = False) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    page.screenshot(path=str(path), full_page=full_page)
    print(f"  saved {path.relative_to(OUT_DIR.parent)}")


def _login(page: Page) -> None:
    page.goto(f"{BASE_URL}/ui/login", wait_until="networkidle")
    page.wait_for_selector(".auth-steps", timeout=15_000)
    _shot(page, "01-login.png")
    page.fill("#email", EMAIL)
    page.fill("#password", PASSWORD)
    page.click("button.auth-submit")
    # Land on the SPA overview.
    page.wait_for_selector(".overview-hero", timeout=20_000)
    page.wait_for_timeout(1200)  # let Alpine hydrate + the map render


def _set_view(page: Page, mode: str) -> None:
    """Force Simple or Advanced regardless of the persisted localStorage value."""
    label = "Simple" if mode == "simple" else "Advanced"
    btn = page.locator(".view-toggle-btn", has_text=label)
    classes = btn.get_attribute("class") or ""
    if "view-toggle-btn-active" not in classes:
        btn.click()
        page.wait_for_timeout(900)


def _goto_section(page: Page, key: str, label: str) -> None:
    page.locator(".nav-link", has_text=label).first.click()
    page.wait_for_timeout(900)


def capture() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,
            color_scheme="dark",
        )
        page = context.new_page()

        print(f"Driving {BASE_URL} as {EMAIL} ...")
        _login(page)

        # --- Overview, Simple (guided default) ---
        _set_view(page, "simple")
        _shot(page, "02-overview-simple.png", full_page=True)

        # Sidebar close-up: the grouped, collapsible navigation.
        page.locator(".sidebar-shell").screenshot(path=str(OUT_DIR / "03-grouped-nav.png"))
        print("  saved screenshots/03-grouped-nav.png")

        # --- Overview, Advanced (full density: stats + charts) ---
        _set_view(page, "advanced")
        page.wait_for_timeout(1200)  # allow charts to (re)render while visible
        _shot(page, "04-overview-advanced.png", full_page=True)

        # --- Servers & tools: upstream / downstream lanes ---
        _set_view(page, "simple")
        _goto_section(page, "servers", "Servers")
        try:
            page.wait_for_selector(".server-lanes", timeout=8_000)
        except PWTimeout:
            print("  ! server-lanes not found (composer may have auto-opened)")
        _shot(page, "05-servers-lanes.png", full_page=True)

        # Expand the first server card to reveal its tools (with action badges).
        toggles = page.locator(".server-card-toggle")
        if toggles.count() > 0:
            toggles.first.click()
            page.wait_for_timeout(700)
            _shot(page, "06-server-tools-drilldown.png", full_page=True)
        else:
            print("  ! no server cards to expand")

        # --- Advanced servers view: raw transport/target/origin meta ---
        _set_view(page, "advanced")
        page.wait_for_timeout(500)
        _shot(page, "07-servers-advanced.png", full_page=True)

        # --- Light theme flourish on the overview ---
        _goto_section(page, "dashboard", "Overview")
        _set_view(page, "simple")
        page.click("button.theme-toggle")
        page.wait_for_timeout(900)
        _shot(page, "08-overview-light.png", full_page=True)

        context.close()
        browser.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(capture())
    except PWTimeout as exc:  # pragma: no cover - operational script
        print(f"Timed out driving the UI: {exc}", file=sys.stderr)
        sys.exit(1)
