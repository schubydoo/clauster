"""Browser smoke tests for the non-bridge dashboard flows.

This is the first slice of the automated browser suite (see tests/E2E_CHECKLIST.md
for the full manual list still to be ported). It covers flows that need no live
bridge lifecycle — the most stable to automate first: dashboard grid render, the
login flow, and theme persistence.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_dashboard_grid_renders(page: Page, open_server: str) -> None:
    """A loopback dashboard renders one card per discovered project."""
    page.goto(open_server)
    expect(page.locator("#project-grid")).to_be_visible()
    for name in ("alpha", "beta", "gamma"):
        expect(page.get_by_role("heading", name=name)).to_be_visible()


def test_login_page_is_served_when_auth_required(page: Page, auth_server: str) -> None:
    """An unauthenticated request to an auth-enabled server lands on /login."""
    page.goto(auth_server)
    expect(page).to_have_url(f"{auth_server}/login")
    expect(page.locator("#password")).to_be_visible()
    expect(page.get_by_role("button", name="Sign in")).to_be_visible()


def test_login_rejects_wrong_password_then_accepts_correct(
    page: Page, auth_server: str, e2e_password: str
) -> None:
    """A wrong password keeps you on /login; the correct one reaches the dashboard."""
    page.goto(f"{auth_server}/login")
    page.locator("#password").fill("definitely-not-the-password")
    page.get_by_role("button", name="Sign in").click()
    # Still gated: back on the login page with the rejection surfaced, and the
    # dashboard grid not reachable.
    expect(page).to_have_url(re.compile(r"/login"))
    expect(page.locator("#password")).to_be_visible()
    expect(page.get_by_role("alert")).to_contain_text("Incorrect password.")

    page.locator("#password").fill(e2e_password)
    page.get_by_role("button", name="Sign in").click()
    # Authenticated: redirected to the dashboard, which renders the project grid.
    expect(page).to_have_url(f"{auth_server}/")
    expect(page.locator("#project-grid")).to_be_visible()


def test_theme_toggle_persists_across_reload(page: Page, auth_server: str) -> None:
    """Toggling the theme writes localStorage and survives a reload."""
    page.goto(f"{auth_server}/login")
    html = page.locator("html")
    expect(html).to_have_attribute("data-bs-theme", "dark")  # default

    page.locator("#theme-toggle").click()
    expect(html).to_have_attribute("data-bs-theme", "light")

    page.reload()
    expect(html).to_have_attribute("data-bs-theme", "light")  # restored from localStorage
