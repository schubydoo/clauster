"""Browser E2E for icon rendering and accessibility (the TS-1 maturity slice).

A follow-on slice to the earlier browser tests (``test_smoke_e2e`` non-bridge
flows, ``test_bridge_e2e`` lifecycle, ``test_controls_e2e`` controls). These port
two checklist rows that need no live bridge — they run against the read-only
``open_server`` / ``auth_server``:

* **Action-button icons render** — the dashboard's icons are Tabler sprite
  symbols referenced via ``<use href="#ic-…">``. An unresolved sprite ref renders
  a zero-size ``<svg>`` (the icon silently vanishes), so a representative set of
  always-present controls is checked to have a non-zero rendered box.
* **axe-core a11y smoke** — the vendored, network-free axe-core (registered as a
  page init script by :class:`_driver.AgentBrowser`) runs in-page against the
  dashboard and the login page; the suite asserts **zero serious/critical**
  WCAG 2 A/AA violations.

See ``tests/E2E_CHECKLIST.md`` for the full manual list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _driver import AgentBrowser

pytestmark = pytest.mark.e2e

# Run axe over only the WCAG 2.0/2.1 A + AA rule tags and fail on the two highest
# impact levels — the ones that are unambiguous defects (a missing accessible name,
# an unlabelled control, an alt-less image), not the noisier "moderate"/"minor"
# advisories that depend on design intent.
_AXE_RUN = (
    "(async () => {"
    "  const r = await axe.run(document, { runOnly: ['wcag2a', 'wcag2aa'] });"
    "  return r.violations"
    "    .filter(v => v.impact === 'serious' || v.impact === 'critical')"
    "    .map(v => ({ id: v.id, impact: v.impact, help: v.help, n: v.nodes.length }));"
    "})()"
)


def _icon_box(browser: AgentBrowser, selector: str) -> dict:
    """Return the rendered bounding box ({w, h}) of the first ``selector`` element.

    Measures the icon ``<svg>`` itself: a sprite ``<use href="#…">`` that fails to
    resolve produces a zero-size box, which is exactly the "broken icon" regression
    this guards against.
    """
    return browser.eval_json(
        "(() => {"
        f"  const els = document.querySelectorAll({selector!r});"
        # Pick the first *displayed* match: a control like the theme toggle holds both a
        # sun and a moon icon, only one shown for the current theme (the other is
        # display:none). Measuring the displayed one is what proves the sprite resolved.
        "  for (const el of els) {"
        "    if (getComputedStyle(el).display === 'none') continue;"
        "    const r = el.getBoundingClientRect();"
        "    return { w: r.width, h: r.height };"
        "  }"
        "  return { w: 0, h: 0, none_displayed: true };"
        "})()"
    )


def test_action_button_icons_render(browser: AgentBrowser, open_server: str) -> None:
    """Always-present action-button icons render with a non-zero box (no broken sprite refs).

    Covers a representative set of controls present on every page load without a live
    bridge: the per-project "Run Claude here" play icon, the ··· overflow icon, and
    the theme-toggle sun/moon icons.
    """
    browser.goto(open_server)
    browser.expect_visible('[data-project="alpha"]')

    # The theme toggle holds both a sun + moon icon (only the one for the current theme is
    # displayed); ``_icon_box`` measures the displayed one. The play + overflow icons are
    # each a single always-displayed sprite.
    selectors = {
        "play": '[data-project="alpha"] .launch-anchor button svg.ico',
        "overflow": '[data-project="alpha"] .card-menu button svg.ico',
        "theme": "#theme-toggle svg",
    }
    for label, selector in selectors.items():
        box = _icon_box(browser, selector)
        assert box.get("w", 0) > 0 and box.get("h", 0) > 0, (
            f"{label} icon ({selector}) rendered zero-size {box} — a broken <use> sprite ref?"
        )


def test_dashboard_has_no_serious_a11y_violations(browser: AgentBrowser, open_server: str) -> None:
    """axe-core finds zero serious/critical WCAG 2 A/AA violations on the dashboard."""
    browser.goto(open_server)
    browser.expect_visible("#project-grid")
    # Sanity: the init-script injection actually put axe on the page.
    assert browser.eval_js("typeof window.axe") == '"object"', (
        "axe-core was not injected — check AGENT_BROWSER_INIT_SCRIPTS / the vendored axe.min.js"
    )

    violations = browser.eval_json(_AXE_RUN)
    assert violations == [], f"dashboard a11y violations (serious/critical): {violations}"


# axe rules for the page's landmark/heading structure. These are "moderate" impact (so the
# serious/critical gate above doesn't cover them), but they were the only real a11y gap on the
# dashboard (#958 P4): no <main> landmark, no <h1>, content outside landmarks (region). Pinned
# separately so a future refactor that drops the <main>/<h1> re-breaks a test, not just a scan.
_AXE_LANDMARKS = (
    "(async () => {"
    "  const r = await axe.run(document, "
    "    { runOnly: ['landmark-one-main', 'page-has-heading-one', 'region'] });"
    "  return r.violations.map(v => ({ id: v.id, help: v.help, n: v.nodes.length }));"
    "})()"
)


def test_dashboard_has_main_landmark_and_h1(browser: AgentBrowser, open_server: str) -> None:
    """The dashboard exposes one <main> landmark + an <h1>, so all content sits in a landmark."""
    browser.goto(open_server)
    browser.expect_visible("#project-grid")
    violations = browser.eval_json(_AXE_LANDMARKS)
    assert violations == [], f"dashboard landmark/heading a11y violations: {violations}"


def test_login_page_has_no_serious_a11y_violations(
    browser: AgentBrowser, auth_server: str
) -> None:
    """axe-core finds zero serious/critical WCAG 2 A/AA violations on the login page."""
    browser.goto(f"{auth_server}/login")
    browser.expect_visible("#password")
    assert browser.eval_js("typeof window.axe") == '"object"', (
        "axe-core was not injected — check AGENT_BROWSER_INIT_SCRIPTS / the vendored axe.min.js"
    )

    violations = browser.eval_json(_AXE_RUN)
    assert violations == [], f"login a11y violations (serious/critical): {violations}"
