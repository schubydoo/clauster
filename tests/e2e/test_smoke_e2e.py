"""Browser smoke tests for the non-bridge dashboard flows.

The first slice of the automated browser suite (see tests/E2E_CHECKLIST.md for the
full manual list still to be ported). It covers flows that need no live bridge
lifecycle — the most stable to automate first: dashboard grid render, the login
flow, and theme persistence. Driven through the ``agent-browser`` CLI via the
``browser`` fixture (see ``_driver.AgentBrowser``).
"""

from __future__ import annotations

import re

import pytest
from _driver import AgentBrowser

pytestmark = pytest.mark.e2e


def test_dashboard_grid_renders(browser: AgentBrowser, open_server: str) -> None:
    """A loopback dashboard renders one row per discovered project."""
    browser.goto(open_server)
    browser.expect_visible("#project-grid")
    # The two-zone redesign renders each project as a `[data-project]` row whose name
    # shows in a span (no longer a heading); assert the row is present with its name.
    for name in ("alpha", "beta", "gamma"):
        browser.expect_text(f'[data-project="{name}"]', name)


def test_login_page_is_served_when_auth_required(browser: AgentBrowser, auth_server: str) -> None:
    """An unauthenticated request to an auth-enabled server lands on /login."""
    browser.goto(auth_server)
    browser.expect_url(f"{auth_server}/login")
    browser.expect_visible("#password")
    browser.expect_role_visible("button", "Sign in")


def test_login_rejects_wrong_password_then_accepts_correct(
    browser: AgentBrowser, auth_server: str, e2e_password: str
) -> None:
    """A wrong password keeps you on /login; the correct one reaches the dashboard."""
    browser.goto(f"{auth_server}/login")
    browser.fill("#password", "definitely-not-the-password")
    browser.click('button[type="submit"]')  # real CDP click submits the form
    # Still gated: back on the login page with the rejection surfaced, and the
    # dashboard grid not reachable.
    browser.expect_url(re.compile(r"/login"))
    browser.expect_visible("#password")
    browser.expect_text('[role="alert"]', "Incorrect password.")

    browser.fill("#password", e2e_password)
    browser.click('button[type="submit"]')
    # Authenticated: redirected to the dashboard, which renders the project grid.
    browser.expect_url(f"{auth_server}/")
    browser.expect_visible("#project-grid")


def test_theme_toggle_persists_across_reload(browser: AgentBrowser, auth_server: str) -> None:
    """Toggling the theme writes localStorage and survives a reload."""
    browser.goto(f"{auth_server}/login")
    browser.expect_attr("html", "data-bs-theme", "dark")  # default

    browser.click("#theme-toggle")
    browser.expect_attr("html", "data-bs-theme", "light")

    browser.reload()
    browser.expect_attr("html", "data-bs-theme", "light")  # restored from localStorage
