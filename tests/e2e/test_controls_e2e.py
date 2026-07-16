"""Browser E2E for the secondary controls (overflow menu, running-session controls).

A follow-on slice to ``test_smoke_e2e`` (non-bridge flows) and ``test_bridge_e2e``
(bridge lifecycle). These port checklist rows that exercise the smaller *controls*
rather than the spawn lifecycle itself:

* the **··· overflow menu** open / close (Alpine-driven, no Bootstrap JS) and its
  always-present **Edit CLAUDE.md** item, which lives on the project row — drivable
  with no live bridge;
* the running-bridge **session controls** in the Active-sessions zone — the
  **Open in Claude** deep link, the **QR** show / hide toggle, and the **Logs**
  panel toggle — which need a real (fake-``claude``) bridge to surface a session.

The two-zone redesign dropped the per-card "copy session link" button (the
``copy()`` toast path it drove is no longer reachable from the UI), so the old
copy-toast row is replaced here by the Logs-panel toggle — the other always-present
control on a running row. See ``tests/E2E_CHECKLIST.md`` for the manual list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _helpers import STATUS_TIMEOUT, open_desktop_launch, trust_and_start

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e

# Per-project overflow (···) menu: the trigger button and its single dropdown item.
_MENU_TRIGGER = '.card-menu button[aria-label="More actions"]'
_MENU_ITEM = ".card-menu .dropdown-item"  # the Edit/Close CLAUDE.md toggle


def _start_bridge(browser: AgentBrowser, project: str) -> None:
    """Trust-on-start a fresh bridge and wait for RUNNING."""
    open_desktop_launch(browser, project)
    trust_and_start(browser, project)
    browser.expect_text("section.zone-active", "Running", timeout_ms=STATUS_TIMEOUT)


def test_overflow_menu_opens_and_closes(browser: AgentBrowser, open_server: str) -> None:
    """The ··· menu toggles open (revealing Edit CLAUDE.md) and closes on outside click.

    Needs no bridge: the menu is Alpine-driven and present on every project row.
    """
    browser.goto(open_server)
    browser.expect_visible('[data-project="beta"]')
    trigger = f'[data-project="beta"] {_MENU_TRIGGER}'
    item = f'[data-project="beta"] {_MENU_ITEM}'

    # Closed by default (x-cloak/x-show keeps the item hidden until the menu opens).
    browser.expect_attr(trigger, "aria-expanded", "false")
    browser.expect_hidden(item)

    # Open: aria-expanded flips and the Edit CLAUDE.md item is revealed.
    browser.click(trigger)
    browser.expect_attr(trigger, "aria-expanded", "true")
    browser.expect_visible(item)
    browser.expect_text(item, "Edit CLAUDE.md")

    # @click.outside closes it again — click elsewhere on the page body.
    browser.eval_js("document.body.click()")
    browser.expect_attr(trigger, "aria-expanded", "false")
    browser.expect_hidden(item)


def test_overflow_menu_toggles_claude_md_editor(browser: AgentBrowser, open_server: str) -> None:
    """Choosing Edit CLAUDE.md from the ··· menu opens the inline editor and closes the menu."""
    browser.goto(open_server)
    browser.expect_visible('[data-project="beta"]')
    editor = '[data-project="beta"] textarea.cmd-text'
    item = f'[data-project="beta"] {_MENU_ITEM}'

    browser.click(f'[data-project="beta"] {_MENU_TRIGGER}')
    browser.expect_hidden(editor)

    browser.click(item)
    # The editor textarea appears (after an on-open content fetch — slow-CI headroom)
    # and the menu closes itself (menu = false on click).
    browser.expect_visible(editor, timeout_ms=STATUS_TIMEOUT)
    browser.expect_hidden(item)


def test_running_bridge_session_link_and_qr(browser: AgentBrowser, bridge_server: Server) -> None:
    """A running bridge surfaces the Open-in-Claude deep link plus a QR show/hide toggle."""
    browser.goto(bridge_server.url)
    browser.expect_visible('[data-project="gamma"]')
    _start_bridge(browser, "gamma")

    # The deep link renders once a session URL is known; "Open in Claude" is a link role.
    browser.expect_role_visible("link", "Open in Claude")

    qr_btn = 'section.zone-active button[title="Show a QR code to open on your phone"]'
    qr_img = 'section.zone-active img[alt="QR code for the session deep link"]'
    browser.expect_visible(qr_btn)
    # QR is hidden until toggled; the QR image lives behind an x-if template.
    browser.expect_hidden(qr_img)

    # Show: the QR image renders.
    browser.click(qr_btn)
    browser.expect_visible(qr_img)

    # Hide again: the image is removed from the DOM.
    browser.click(qr_btn)
    browser.expect_hidden(qr_img)


def test_running_bridge_logs_toggle(browser: AgentBrowser, bridge_server: Server) -> None:
    """A running bridge's Logs control toggles the live-tail panel open and closed."""
    browser.goto(bridge_server.url)
    browser.expect_visible('[data-project="gamma"]')
    _start_bridge(browser, "gamma")

    logs_btn = 'section.zone-active [data-test="bridge-logs-toggle"]'
    log_view = "section.zone-active pre.log-view"
    browser.expect_text(logs_btn, "Logs")
    browser.expect_hidden(log_view)

    # Open: the button relabels to "Hide logs" and the live-tail <pre> renders.
    browser.click(logs_btn)
    browser.expect_text(logs_btn, "Hide logs")
    browser.expect_visible(log_view)

    # Close: the panel is removed and the button reverts to "Logs".
    browser.click(logs_btn)
    browser.expect_text(logs_btn, "Logs")
    browser.expect_hidden(log_view)
