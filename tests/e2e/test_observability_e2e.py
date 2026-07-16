"""Browser E2E for the remaining portable observability/resilience rows (#312).

Ports three ``tests/E2E_CHECKLIST.md`` rows into the agent-browser suite — each needs
real state the route/unit tests can't stage:

* **Per-project cost badge** — lazy-loads from ``/api/projects/<name>/usage`` after first
  paint. The ``usage_server`` fixture seeds one priced transcript for ``alpha`` (badge
  shows ``≈``-prefixed cost) and leaves ``beta`` blank (no badge).
* **External-session indicator** — a ``claude agents --json`` session in a managed dir
  that Clauster didn't start is attributed EXTERNAL and shown as "External session
  active". The ``external_session_server`` fixture injects it via ``FAKE_CLAUDE_AGENTS``.
* **Connection-lost banner** — after ~2 failed ``/api/instances`` polls the dashboard
  shows a "Lost connection … Retrying" banner. We trigger it for real by killing the
  server subprocess mid-session (the browser keeps polling). Per #312 this asserts the
  banner *appears*; "clears on return" and the 401→/login bounce stay manual.

See ``tests/E2E_CHECKLIST.md`` for the full manual list.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e

# Killing the server then waiting out 2 failed polls (4s cadence) needs headroom.
_BANNER_TIMEOUT = 20_000


def test_cost_badge_shows_for_seeded_project_only(
    browser: AgentBrowser, usage_server: Server
) -> None:
    """The cost badge renders for a project with usage and stays hidden for a blank one."""
    browser.goto(usage_server.url)
    browser.expect_visible('[data-project="alpha"]')

    # alpha has a seeded transcript -> the lazy-loaded badge appears with a cost figure.
    # Cost mode always prefixes the amount with "≈" (currency-symbol-agnostic).
    alpha_badge = '[data-project="alpha"] [data-test="usage-badge"]'
    browser.expect_visible(alpha_badge)
    assert "≈" in browser.get_text(alpha_badge)

    # beta has no transcripts -> usageLabel('beta') is null -> the badge stays hidden.
    browser.expect_hidden('[data-project="beta"] [data-test="usage-badge"]')


def test_preflight_pill_surfaces_for_unready_project(
    browser: AgentBrowser, usage_server: Server
) -> None:
    """A project with a preflight warning (untrusted dir) shows the attention pill + detail.

    The E2E projects start untrusted (HOME-isolated, trust-on-start), so the per-project
    preflight reports a WARN and the pill renders; expanding it lists the specific check.
    """
    browser.goto(usage_server.url)
    browser.expect_visible('[data-project="alpha"]')

    pill = '[data-project="alpha"] [data-test="preflight-pill"]'
    browser.expect_visible(pill)
    # The pill reads "N check(s)" beside a Tabler alert icon — UX-07 (#560) replaced the
    # "preflight" jargon with the scoped "readiness checks" vocabulary, and DES-03 (#704)
    # swapped the old "⚠" text glyph for the aria-hidden SVG icon; the data-test hook +
    # internal name stay "preflight".
    assert "check" in browser.get_text(pill).lower()

    # Collapsed until clicked, then the specific warning check(s) appear.
    detail = '[data-project="alpha"] [data-test="preflight-detail"]'
    browser.expect_hidden(detail)
    browser.click(pill)
    browser.expect_visible(detail)
    # The rendered detail names the failing check (the untrusted-workspace warning here),
    # so a regression in the rendered check content fails — not just the expand interaction.
    assert "trust" in browser.get_text(detail).lower()


def test_external_session_indicator_shows_for_unmanaged_session(
    browser: AgentBrowser, external_session_server: Server
) -> None:
    """A session Clauster didn't start (EXTERNAL) surfaces its distinct indicator."""
    browser.goto(external_session_server.url)
    browser.expect_visible('[data-project="alpha"]')

    # The agents --json cross-check (polled every 1s) attributes the injected session to
    # alpha as EXTERNAL -> hasExternal('alpha') flips true and the indicator renders.
    indicator = '[data-project="alpha"] [data-test="external-indicator"]'
    browser.expect_visible(indicator, timeout_ms=_BANNER_TIMEOUT)
    assert "External session active" in browser.get_text(indicator)

    # beta has no external session -> its indicator stays hidden.
    browser.expect_hidden('[data-project="beta"] [data-test="external-indicator"]')


def test_external_session_rich_display_in_active_zone_and_project_detail(
    browser: AgentBrowser, external_session_server: Server
) -> None:
    """FE-4 (#300): the unmanaged session gets a read-only Active-zone row + expandable detail.

    The seeded EXTERNAL session (pid 999999 in ``alpha``) must surface on both surfaces the
    user chose: a first-class read-only row in the Active zone, and an expandable per-session
    detail under the project-row note. Adoption controls are intentionally absent — observe-only.
    """
    browser.goto(external_session_server.url)
    browser.expect_visible('[data-project="alpha"]')

    # Active zone: an external session counts as live, so a read-only row renders carrying the
    # project, an "external" mode badge, and the real pid. No Stop/Resume button (unmanaged).
    row = '[data-test="external-row"]'
    browser.expect_visible(row, timeout_ms=_BANNER_TIMEOUT)
    # .mode-badge uppercases its text via CSS, so compare case-insensitively.
    row_text = browser.get_text(row).lower()
    assert "external" in row_text
    assert "alpha" in row_text
    assert "pid 999999" in row_text
    assert "unmanaged" in row_text

    # Observe-only contract: an unmanaged session exposes NO lifecycle controls. The macro
    # renders no <button> at all, so this guards against a future edit slipping one in.
    browser.expect_hidden('[data-test="external-row"] button')

    # The 'external' source filter is offered alongside the other run-location filters.
    filters = browser.get_text('[aria-label="Filter sessions by where they run"]').lower()
    assert "external" in filters

    # Project-row note: detail is collapsed until the toggle is clicked, then shows the session's
    # state + pid inline. The toggle is a real <button> (aria-expanded) for keyboard/SR users.
    detail = '[data-project="alpha"] [data-test="external-detail"]'
    browser.expect_hidden(detail)
    browser.click('[data-project="alpha"] [data-test="external-toggle"]')
    browser.expect_visible(detail)
    assert "pid 999999" in browser.get_text(detail)


def test_connection_lost_banner_appears_when_server_dies(
    browser: AgentBrowser, bridge_server: Server
) -> None:
    """Killing the server mid-session surfaces the retry banner after the polls fail.

    The dashboard polls ``/api/instances`` every 4s; after two failures (here, the
    server process is killed so the fetch is refused) ``connLost`` flips and the banner
    renders. Asserts the banner appears (per #312, recovery + the 401 bounce stay manual).
    """
    browser.goto(bridge_server.url)
    browser.expect_visible('[data-project="alpha"]')

    # The banner is gated on connLost (top-level, distinct from the per-hosted-session
    # one) and is hidden while the server is healthy.
    banner = '[x-show="connLost"] .alert-warning'
    browser.expect_hidden(banner)

    # Kill the server; the browser keeps polling and the fetches now fail.
    assert bridge_server.proc is not None
    bridge_server.proc.kill()

    browser.expect_visible(banner, timeout_ms=_BANNER_TIMEOUT)
    assert "Lost connection" in browser.get_text(banner)


@pytest.mark.skipif(sys.platform != "linux", reason="/proc/<pid>/stat staging is Linux-only")
def test_adopt_external_standard_bridge_becomes_managed(
    browser: AgentBrowser, adoptable_external_server: Server
) -> None:
    """Adopting a live external *standard* bridge promotes it to a managed row (#330).

    The fixture stages the real adoption gate — a live subcommand-form fake bridge
    plus a matching ``bridge-pointer.json`` — so ``/api/sessions/adoptable`` offers
    ``alpha`` and the Manage affordance renders. Clicking it (its ``window.confirm``
    is auto-accepted by the suite's dialog shim) POSTs ``/adopt`` and the session
    gains the first-class managed row: Running status + the Stop control.
    """
    browser.goto(adoptable_external_server.url)
    browser.expect_visible('[data-project="alpha"]')

    # The agents-json cross-check attributes the session EXTERNAL, and the adoptable
    # poll (pointer + live standard-cmdline checks) arms the Manage button.
    browser.expect_visible(
        '[data-project="alpha"] [data-test="external-indicator"]', timeout_ms=_BANNER_TIMEOUT
    )
    adopt_btn = '[data-project="alpha"] [data-test="adopt-btn"]'
    browser.expect_visible(adopt_btn, timeout_ms=_BANNER_TIMEOUT)

    browser.click(adopt_btn)

    # The adopted session is now managed: a Running row with a Stop control appears in
    # the Active zone (the whole point of adoption — lifecycle controls without restart).
    browser.expect_text("section.zone-active", "Running", timeout_ms=_BANNER_TIMEOUT)
    browser.expect_visible(
        'section.zone-active [data-test="stop-session"]', timeout_ms=_BANNER_TIMEOUT
    )
    # And the Manage affordance retires for this project (no longer adoptable). The
    # retire path needs TWO polls to settle server-side (the 1s agents-json cross-check
    # re-attributing the session managed, then the 4s adoptable refresh) — under CI
    # load that chain can exceed the usual 20s, so give it double headroom.
    browser.expect_hidden(adopt_btn, timeout_ms=2 * _BANNER_TIMEOUT)
