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
    alpha_badge = '[data-project="alpha"] [x-text="usageLabel(\'alpha\')"]'
    browser.expect_visible(alpha_badge)
    assert "≈" in browser.get_text(alpha_badge)

    # beta has no transcripts -> usageLabel('beta') is null -> the badge stays hidden.
    browser.expect_hidden('[data-project="beta"] [x-text="usageLabel(\'beta\')"]')


def test_external_session_indicator_shows_for_unmanaged_session(
    browser: AgentBrowser, external_session_server: Server
) -> None:
    """A session Clauster didn't start (EXTERNAL) surfaces its distinct indicator."""
    browser.goto(external_session_server.url)
    browser.expect_visible('[data-project="alpha"]')

    # The agents --json cross-check (polled every 1s) attributes the injected session to
    # alpha as EXTERNAL -> hasExternal('alpha') flips true and the indicator renders.
    indicator = '[data-project="alpha"] [x-show="hasExternal(\'alpha\')"]'
    browser.expect_visible(indicator, timeout_ms=_BANNER_TIMEOUT)
    assert "External session active" in browser.get_text(indicator)

    # beta has no external session -> its indicator stays hidden.
    browser.expect_hidden('[data-project="beta"] [x-show="hasExternal(\'beta\')"]')


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
