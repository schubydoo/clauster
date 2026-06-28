"""Browser E2E for the non-bridge dashboard widgets (CLAUDE.md editor, new-project).

The third slice of the automated browser suite (after ``test_smoke_e2e`` and the
bridge-spawn ``test_bridge_e2e``). These drive real headless Chromium against a
live clauster and exercise the dashboard's own widgets: the inline CLAUDE.md editor
(open → edit → save), and the New-project create form (validation gate + clone
toggle + in-place row insertion with no full-page reload). See
``tests/E2E_CHECKLIST.md`` for the full manual list.

They use the function-scoped ``bridge_server`` for flows that mutate the
projects_root on disk (a saved CLAUDE.md, a freshly created project), so a write in
one test never leaks into the next; the read-only validation checks ride the
module-scoped ``open_server``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e

# A bridge spawn waits on the fake claude's readiness markers, then the dashboard's
# 4s poll reconciles state — give the trust-and-start status transition headroom.
_STATUS_TIMEOUT = 20_000

# The New-project form: its toggle (the only primary button in the Projects zone
# header) and the submit (scoped to the form's own .mt-3 block, so the warn-banner's
# button can't be mistaken for it).
_NEW_PROJECT_TOGGLE = "section.zone-projects .zone-actions button.btn-primary"
_NEW_PROJECT_SUBMIT = 'div[x-show="np.open"] .mt-3 button.btn-primary'


def test_overflow_menu_opens_claude_md_editor_and_saves(
    browser: AgentBrowser, bridge_server: Server
) -> None:
    """The ··· overflow menu reveals the CLAUDE.md editor; an edit saves in place.

    Drives the editor row (load → edit → save → ``✓ saved``) against ``beta``, the
    fixture project that already ships a CLAUDE.md. The write path refuses an
    untrusted directory (a 403), so the project is trusted first via the
    trust-on-start gate — which also surfaces the editor's "session running" banner.
    """
    browser.goto(bridge_server.url)
    browser.expect_visible('[data-project="beta"]')

    # Trust the directory (the only path to trust is the launch gate); once RUNNING the
    # write path is allowed. The bridge stays up so the editor shows its running banner.
    browser.click('[data-project="beta"] .launch-anchor button')
    browser.expect_visible('[data-project="beta"] .launch-pop')
    browser.check('[data-project="beta"] input[name="lm-beta"][value="desktop"]')
    browser.click('[data-project="beta"] .launch-pop button.btn-primary.w-100')
    browser.check('[data-project="beta"] .alert-warning input[type="checkbox"]')
    browser.click('[data-project="beta"] .alert-warning button.btn-warning')
    browser.expect_text("section.zone-active", "Running", timeout_ms=_STATUS_TIMEOUT)

    editor = '[data-project="beta"] textarea.cmd-text'
    block = '[data-project="beta"] .border.rounded'  # the editor block (x-if template)
    # The editor is collapsed until opened from the overflow menu.
    browser.expect_hidden(editor)

    # Open the ··· menu, then click "Edit CLAUDE.md".
    browser.click('[data-project="beta"] .card-menu button[aria-label="More actions"]')
    browser.click('[data-project="beta"] .card-menu .dropdown-item')

    # The textarea loads the on-disk content (the fixture wrote "# beta\n"), and the
    # running bridge raises the "applies to new sessions" banner.
    browser.expect_visible(editor)
    assert browser.get_value(editor) == "# beta"  # agent-browser strips the trailing \n
    browser.expect_text(f"{block} .alert-warning", "A session is running")

    # Edit and Save; the green "saved" confirmation appears (no reload). DES-03 (#694)
    # swapped the leading ✓ glyph for a Tabler check SVG, so assert on the word only.
    browser.fill(editor, "# beta\n\nedited by e2e\n")
    browser.click(f"{block} button.btn-primary")
    browser.expect_text(f"{block} .text-green", "saved")

    # Re-opening the editor shows the persisted edit (Cancel, then reopen). Assert the
    # Cancel button is present first, so an auto-collapse-on-save regression fails here
    # explicitly rather than as an opaque no-op click + later hidden-state assertion.
    cancel = f"{block} .btn-list button:not(.btn-primary)"
    browser.expect_visible(cancel)
    browser.click(cancel)
    browser.expect_hidden(editor)
    browser.click('[data-project="beta"] .card-menu button[aria-label="More actions"]')
    browser.click('[data-project="beta"] .card-menu .dropdown-item')
    # A textarea's *value* (not its DOM text) carries the persisted edit; the reopen
    # re-fetches from disk asynchronously, so poll the value (agent-browser strips the
    # trailing newline). The driver has no expect_value, so poll get_value here.
    browser.expect_visible(editor)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and browser.get_value(editor) != "# beta\n\nedited by e2e":
        time.sleep(0.2)
    assert browser.get_value(editor) == "# beta\n\nedited by e2e"


def test_new_project_form_validation_and_clone_toggle(
    browser: AgentBrowser, open_server: str
) -> None:
    """The New-project form gates Create on a name and reveals the Git URL on Clone.

    Read-only validation: the submit stays disabled with an empty name, the clone
    radio swaps in the Git-URL input, and the create radio swaps it back out.
    """
    browser.goto(open_server)
    browser.click(_NEW_PROJECT_TOGGLE)
    browser.expect_visible("#np-name")

    # Empty name → the submit (Create) is disabled.
    browser.expect_disabled(_NEW_PROJECT_SUBMIT)
    browser.expect_text(_NEW_PROJECT_SUBMIT, "Create")
    browser.fill("#np-name", "scratch-proj")
    browser.expect_enabled(_NEW_PROJECT_SUBMIT)

    # The Git-URL input is clone-only: hidden under "Create empty", shown under "Clone".
    # Click the radio labels by their `for` target.
    browser.expect_hidden("#np-url")
    browser.click('label[for="np-clone"]')
    browser.expect_visible("#np-url")
    # The submit relabels to "Clone" in clone mode.
    browser.expect_text(_NEW_PROJECT_SUBMIT, "Clone")
    # Switching back to create hides the URL field again.
    browser.click('label[for="np-create"]')
    browser.expect_hidden("#np-url")


def test_create_empty_project_inserts_row_in_place(
    browser: AgentBrowser, bridge_server: Server
) -> None:
    """Creating an empty project inserts its row in the grid with no full-page reload.

    Pins a sentinel on ``window`` and asserts it survives the create, proving the new
    row is grafted into the live grid (the checklist's "no full-page reload"
    guarantee) rather than fetched via a navigation.
    """
    browser.goto(bridge_server.url)
    browser.expect_visible("#project-grid")
    # Sentinel: a full-page reload would wipe this.
    browser.eval_js("window.__e2e_no_reload = true")

    browser.click(_NEW_PROJECT_TOGGLE)
    browser.fill("#np-name", "delta")
    browser.click(_NEW_PROJECT_SUBMIT)

    # The new row appears in place (the create API call can be slow in CI, so give it
    # the same headroom as the other waitable assertions)...
    browser.expect_visible('[data-project="delta"]', timeout_ms=_STATUS_TIMEOUT)
    browser.expect_text('[data-project="delta"]', "delta")
    # ...and the page never navigated (sentinel intact).
    assert browser.eval_js("window.__e2e_no_reload") == "true", (
        "page navigated during project creation — the window sentinel was wiped"
    )

    # The inserted row is fully interactive without a refresh: it has its own launch
    # popover, which opens on click.
    browser.click('[data-project="delta"] .launch-anchor button')
    browser.expect_visible('[data-project="delta"] .launch-pop')
