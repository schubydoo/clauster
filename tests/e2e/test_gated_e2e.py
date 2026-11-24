"""Browser E2E for the two opt-in safety gates: bypassPermissions + ghost-reaper UI.

These port the **bypassPermissions footgun** and **Ghost-environment reaper UI** rows
from ``tests/E2E_CHECKLIST.md``. Both are pure render/route gating — no ``claude``
bridge is ever spawned:

* **bypassPermissions** — the dangerous ``bypassPermissions`` permission option must
  render *only* for a project that opts in via
  ``projects.<name>.allow_bypass_permissions: true``, and selecting it must force a
  typed-name confirmation before a session can start. We drive the gate up to (and
  including) the typed-confirm button staying disabled for a wrong name, then stop —
  never reaching a real spawn.
* **ghost-reaper** — the dashboard reaper panel and its ``/api/environments/ghosts``
  endpoint must appear only when ``reaper.ui_enabled: true``; with the flag unset the
  whole Maintenance block is absent and the endpoint 404s.

The OFF cases ride the shared, read-only ``open_server``; the ON cases use the
``reaper_server`` (read-only) and ``bypass_server`` (function-scoped — its typed
confirm trusts ``gamma``, a mutation that must not leak between tests).
"""

from __future__ import annotations

import http.client
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest
from _helpers import STATUS_TIMEOUT, open_desktop_launch, trust_and_start

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e

# The footgun permission option, scoped per-row by a `[data-project=...] #perm-<name>`
# prefix. It is gated server-side by Jinja, so a count holds regardless of whether the
# launch popover is open.
_BYPASS_OPTION = 'option[value="bypassPermissions"]'

# The Maintenance block only renders when reaper.ui_enabled is set; its toggle
# reveals the panel whose <h3> title is "Clean up leftover environments".
_REAPER_BLOCK = '[data-test="reaper-panel"]'
_REAPER_TOGGLE = '[data-test="reaper-toggle"]'
_REAPER_TITLE = f"{_REAPER_BLOCK} h3.card-title"


def _ghosts_status(base_url: str) -> int:
    """GET ``/api/environments/ghosts`` and return its HTTP status (loopback, no auth)."""
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        conn.request("GET", "/api/environments/ghosts")
        return conn.getresponse().status
    finally:
        conn.close()


def test_bypass_option_only_renders_for_opted_in_project(
    browser: AgentBrowser, bypass_server: Server
) -> None:
    """The bypassPermissions option renders only for the project that opts in.

    ``gamma`` carries ``allow_bypass_permissions: true``; ``alpha``/``beta`` do not, so
    the footgun option is absent from their permission pickers — it cannot be forced
    from the client for a project without the ceiling.
    """
    browser.goto(bypass_server.url)
    browser.expect_count(f'[data-project="gamma"] #perm-gamma {_BYPASS_OPTION}', 1)
    browser.expect_count(f'[data-project="alpha"] #perm-alpha {_BYPASS_OPTION}', 0)
    browser.expect_count(f'[data-project="beta"] #perm-beta {_BYPASS_OPTION}', 0)


def test_bypass_option_absent_when_no_project_opts_in(
    browser: AgentBrowser, open_server: str
) -> None:
    """With no project opting in, the bypassPermissions option renders nowhere."""
    browser.goto(open_server)
    browser.expect_visible("#project-grid")
    browser.expect_count(_BYPASS_OPTION, 0)


def test_bypass_requires_typed_confirmation_before_spawn(
    browser: AgentBrowser, bypass_server: Server
) -> None:
    """Selecting bypassPermissions forces a typed-name confirm; a wrong name can't proceed.

    The full gate, without ever spawning: open the desktop launch, pick the footgun
    permission, Run (untrusted dir → the trust prompt shows first), Trust & start
    (re-routes through ``start()`` so the bypass gate still applies → the typed confirm
    appears *without* spawning). The "Start with bypass" button stays disabled until the
    typed name matches exactly, so a wrong name can never reach a spawn.
    """
    browser.goto(bypass_server.url)
    browser.expect_visible('[data-project="gamma"]')

    # Open the desktop launch and choose the footgun permission, then Run. The dir is
    # untrusted, so the trust prompt appears first (the order is trust → bypass); Trust &
    # start re-enters start(): now trusted + bypass selected → the typed confirm appears
    # instead of a spawn. Target the bypass block by its own data-test hook — a bare
    # `.alert-danger` matches FOUR blocks in the row (bypass-confirm, mcp-approval-block,
    # inline-error, the spawn-failure detail) and only resolves right by DOM-order luck.
    open_desktop_launch(browser, "gamma")
    browser.select("#perm-gamma", "bypassPermissions")
    trust_and_start(browser, "gamma")
    confirm = '[data-project="gamma"] [data-test="bypass-confirm"]'
    browser.expect_visible(confirm, timeout_ms=STATUS_TIMEOUT)
    browser.expect_text(confirm, "Type the project name to confirm")

    # A wrong name keeps "Start with bypass" disabled — nothing can spawn, gate stays up.
    start_bypass = f'{confirm} [data-test="bypass-start-go"]'
    browser.fill(f'{confirm} input[type="text"]', "not-gamma")
    browser.expect_disabled(start_bypass)
    browser.expect_visible(confirm)
    # Only the exact name unlocks it (we stop here — never click through to a spawn).
    browser.fill(f'{confirm} input[type="text"]', "gamma")
    browser.expect_enabled(start_bypass)


def test_gated_default_mode_starts_at_ask_where_the_ceiling_is_off(
    browser: AgentBrowser, inherit_default_server: Server
) -> None:
    """A row whose default mode is gated for it starts at Ask each time and says why.

    TASK-480: with ``instance_defaults.permission_mode: inherit``, ``alpha`` has no
    inherit option, so its select used to show blank and Run sent a mode the server
    refuses. It now starts at ``default``, and a hint names the reason.
    """
    browser.goto(inherit_default_server.url)
    browser.expect_visible('[data-project="alpha"]')
    open_desktop_launch(browser, "alpha")
    browser.expect_value("#perm-alpha", "default")
    note = '[data-project="alpha"] [data-test="perm-default-gated"]'
    browser.expect_visible(note)
    browser.expect_text(note, "needs allow_bypass_permissions for this project")
    browser.expect_text('[data-project="alpha"] [data-test="launch-run-go"]', "Ask each time")


def test_inherit_requires_typed_confirmation_before_spawn(
    browser: AgentBrowser, inherit_default_server: Server
) -> None:
    """Where the ceiling allows inherit, it takes the same typed confirm as bypass.

    TASK-480: with no flag passed, a bypassPermissions default in a settings file takes
    effect, so ``gamma`` (the ceiling is on) starts at inherit and Run asks for the typed
    project name. We stop before a spawn, as the bypass test does.
    """
    browser.goto(inherit_default_server.url)
    browser.expect_visible('[data-project="gamma"]')
    open_desktop_launch(browser, "gamma")
    browser.expect_value("#perm-gamma", "inherit")
    browser.expect_count('[data-project="gamma"] [data-test="perm-default-gated"]', 0)
    trust_and_start(browser, "gamma")
    confirm = '[data-project="gamma"] [data-test="bypass-confirm"]'
    browser.expect_visible(confirm, timeout_ms=STATUS_TIMEOUT)
    browser.expect_visible(f'{confirm} [data-test="inherit-confirm-text"]')
    browser.expect_text(confirm, "If that is bypassPermissions, they run tools without asking.")
    # The gate starts the mode Run took, so the picker is locked while the gate is open.
    browser.expect_disabled("#perm-gamma")
    start_go = f'{confirm} [data-test="bypass-start-go"]'
    browser.expect_text(start_go, "Start with no forced mode")
    browser.fill(f'{confirm} input[type="text"]', "not-gamma")
    browser.expect_disabled(start_go)
    browser.fill(f'{confirm} input[type="text"]', "gamma")
    browser.expect_enabled(start_go)


def test_leaving_desktop_moves_a_bypass_default_to_ask(
    browser: AgentBrowser, bypass_default_server: str
) -> None:
    """With a bypassPermissions default, leaving Desktop lands on Ask each time.

    bypassPermissions is Desktop-only, so the mode switch moves the picker off it. The
    target used to be the configured default, which is the mode it had just left.
    """
    browser.goto(bypass_default_server)
    browser.expect_visible('[data-project="gamma"]')
    open_desktop_launch(browser, "gamma")
    browser.expect_value("#perm-gamma", "bypassPermissions")
    browser.check('[data-project="gamma"] input[name="lm-gamma"][value="detached"]')
    browser.expect_value("#perm-gamma", "default")
    browser.expect_text('[data-project="gamma"] [data-test="launch-run-go"]', "Ask each time")


def test_reaper_panel_present_when_enabled(browser: AgentBrowser, reaper_server: str) -> None:
    """With ``reaper.ui_enabled: true``, the Maintenance panel reveals the reaper title."""
    browser.goto(reaper_server)
    browser.expect_visible(_REAPER_TOGGLE)
    # The title is collapsed behind the Maintenance toggle; open it.
    browser.click(_REAPER_TOGGLE)
    browser.expect_text(_REAPER_TITLE, "Clean up leftover environments")
    # The gate is open: the endpoint no longer 404s (it fails later on absent cloud
    # credentials, which is out of scope for this gating row).
    assert _ghosts_status(reaper_server) != 404


def test_reaper_panel_and_endpoint_absent_when_disabled(
    browser: AgentBrowser, open_server: str
) -> None:
    """With the reaper flag unset, the Maintenance block is absent and the endpoint 404s."""
    browser.goto(open_server)
    browser.expect_visible("#project-grid")
    browser.expect_count(_REAPER_BLOCK, 0)
    assert _ghosts_status(open_server) == 404
