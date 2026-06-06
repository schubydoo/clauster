"""Browser E2E for the card's secondary controls (overflow menu, session-link controls).

A follow-on slice to ``test_smoke_e2e`` (non-bridge flows) and ``test_bridge_e2e``
(bridge lifecycle). These port checklist rows that exercise the smaller card
*controls* rather than the spawn lifecycle itself:

* the **··· overflow menu** open / close (Alpine-driven, no Bootstrap JS) and its
  always-present **Edit CLAUDE.md** item — drivable with no live bridge;
* the running-bridge **session-link controls** — the **Open in Claude** deep
  link plus the **QR** show / hide toggle and the **copy** button on the same
  row — which need a real (fake-``claude``) bridge to surface a session URL.

See ``tests/E2E_CHECKLIST.md`` for the full manual list; the rows covered here
are marked ``[auto]``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Page, expect

if TYPE_CHECKING:
    from .conftest import Server

pytestmark = pytest.mark.e2e

# A bridge spawn waits on the fake claude writing its readiness markers, then the
# dashboard's poll reconciles state — give status transitions generous headroom
# (matches test_bridge_e2e).
_STATUS_TIMEOUT = 20_000


def test_overflow_menu_opens_and_closes(page: Page, open_server: str) -> None:
    """The ··· menu toggles open (revealing Edit CLAUDE.md) and closes on outside click.

    Needs no bridge: the menu is Alpine-driven and present on every card.
    """
    page.goto(open_server)
    card = page.locator('[data-project="beta"]')
    expect(card).to_be_visible()

    trigger = card.get_by_role("button", name="More actions")
    menu_item = card.get_by_role("button", name="Edit CLAUDE.md")

    # Closed by default (x-cloak/x-show keeps the item hidden until the menu opens).
    expect(trigger).to_have_attribute("aria-expanded", "false")
    expect(menu_item).to_be_hidden()

    # Open: aria-expanded flips and the Edit CLAUDE.md item is revealed.
    trigger.click()
    expect(trigger).to_have_attribute("aria-expanded", "true")
    expect(menu_item).to_be_visible()

    # @click.outside closes it again — click elsewhere on the page body.
    page.locator("body").click(position={"x": 1, "y": 1})
    expect(trigger).to_have_attribute("aria-expanded", "false")
    expect(menu_item).to_be_hidden()


def test_overflow_menu_toggles_claude_md_editor(page: Page, open_server: str) -> None:
    """Choosing Edit CLAUDE.md from the ··· menu opens the inline editor and closes the menu."""
    page.goto(open_server)
    card = page.locator('[data-project="beta"]')
    expect(card).to_be_visible()

    card.get_by_role("button", name="More actions").click()
    editor = card.locator("textarea.cmd-text")
    expect(editor).to_be_hidden()

    card.get_by_role("button", name="Edit CLAUDE.md").click()
    # The editor textarea appears and the menu closes itself (menu = false on click).
    expect(editor).to_be_visible()
    expect(card.get_by_role("button", name="Edit CLAUDE.md")).to_be_hidden()


def _start_bridge(page: Page, card) -> None:
    """Trust-on-start a fresh bridge and wait for RUNNING (mirrors test_bridge_e2e)."""
    card.get_by_role("button", name="Start bridge").click()
    card.get_by_role("checkbox").check()
    card.get_by_role("button", name="Trust & start").click()
    expect(card.get_by_role("status")).to_contain_text("Running", timeout=_STATUS_TIMEOUT)


def test_running_bridge_session_link_controls(page: Page, bridge_server: Server) -> None:
    """A running bridge surfaces the Open-in-Claude link plus copy and a QR show/hide toggle."""
    page.goto(bridge_server.url)
    card = page.locator('[data-project="gamma"]')
    expect(card).to_be_visible()

    _start_bridge(page, card)

    # The deep link and its sibling controls render once a session URL is known.
    # Scope copy to the session-link row by title (a second "copy" exists for the
    # optional New-session/env link below it).
    expect(card.get_by_role("link", name="Open session in Claude")).to_be_visible()
    copy_btn = card.locator('button[title="Copy the session link to the clipboard."]')
    qr_btn = card.get_by_role("button", name="QR", exact=True)
    expect(copy_btn).to_be_visible()
    expect(qr_btn).to_be_visible()

    # QR is hidden until toggled; the QR image lives behind an x-if template.
    qr_img = card.get_by_role("img", name="QR code for the session deep link")
    expect(qr_img).to_be_hidden()

    # Show: the button relabels to "hide QR" and the QR image renders.
    qr_btn.click()
    expect(card.get_by_role("button", name="hide QR")).to_be_visible()
    expect(qr_img).to_be_visible()

    # Hide again: the image is removed and the button reverts to "QR".
    card.get_by_role("button", name="hide QR").click()
    expect(qr_img).to_be_hidden()
    expect(card.get_by_role("button", name="QR")).to_be_visible()


def test_running_bridge_copy_session_link_toasts(page: Page, bridge_server: Server) -> None:
    """Clicking copy on a running bridge surfaces a confirmation toast (never silent)."""
    page.goto(bridge_server.url)
    card = page.locator('[data-project="gamma"]')
    expect(card).to_be_visible()

    # Grant clipboard so the copy() path can reach navigator.clipboard cleanly; even
    # if it can't, the fallback execCommand path still toasts, so a toast must appear.
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])

    _start_bridge(page, card)

    card.locator('button[title="Copy the session link to the clipboard."]').click()
    # copy() toasts "Link copied" (or a manual-copy fallback) — assert a toast surfaces.
    expect(page.get_by_text("Link copied")).to_be_visible()
