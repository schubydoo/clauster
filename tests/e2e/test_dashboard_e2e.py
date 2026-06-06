"""Browser E2E for the non-bridge dashboard widgets (overflow menu, editor, new-project).

The third slice of the automated browser suite (after ``test_smoke_e2e`` and the
bridge-spawn ``test_bridge_e2e``). These drive real headless Chromium against a
live clauster but, unlike the bridge slice, never spawn a ``claude`` subprocess —
they exercise the dashboard's own widgets: the ``···`` overflow menu, the inline
CLAUDE.md editor (open → edit → save), and the New-project create form (validation
gate + in-place card insertion with no full-page reload). See
``tests/E2E_CHECKLIST.md`` for the full manual list; these port the **CLAUDE.md
editor** and **Create project (empty)** rows.

They use the function-scoped ``bridge_server`` for the flows that mutate the
projects_root on disk (a saved CLAUDE.md, a freshly created project), so a write
in one test never leaks into the next; the read-only validation checks ride the
module-scoped ``open_server``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Page, expect

if TYPE_CHECKING:
    from .conftest import Server

pytestmark = pytest.mark.e2e


# A bridge spawn waits on the fake claude's readiness markers, then the dashboard's
# 4s poll reconciles state — give the trust-and-start status transition headroom.
_STATUS_TIMEOUT = 20_000


def test_overflow_menu_opens_claude_md_editor_and_saves(page: Page, bridge_server: Server) -> None:
    """The ··· overflow menu reveals the CLAUDE.md editor; an edit saves in place.

    Drives the overflow-menu row (Alpine toggles it, no Bootstrap JS) and the
    CLAUDE.md editor row (load → edit → save → ``✓ saved``) against ``beta``, the
    fixture project that already ships a CLAUDE.md. The write path refuses an
    untrusted directory (a 403), so the project is trusted first via the
    trust-on-start gate — which also surfaces the editor's "bridge running" banner.
    """
    page.goto(bridge_server.url)
    card = page.locator('[data-project="beta"]')
    expect(card).to_be_visible()

    # Trust the directory (the only path to trust is the start gate); once RUNNING the
    # write path is allowed. The bridge stays up so the editor shows its running banner.
    card.get_by_role("button", name="Start bridge").click()
    card.get_by_role("checkbox").check()
    card.get_by_role("button", name="Trust & start").click()
    expect(card.get_by_role("status")).to_contain_text("Running", timeout=_STATUS_TIMEOUT)

    # The editor is collapsed until opened from the overflow menu.
    editor = card.locator(".cmd-text")
    expect(editor).to_be_hidden()

    # Open the ··· menu, then click "Edit CLAUDE.md".
    card.get_by_role("button", name="More actions").click()
    card.get_by_role("button", name="Edit CLAUDE.md").click()

    # The textarea loads the on-disk content (the fixture wrote "# beta\n"), and the
    # running bridge raises the "applies to new sessions" banner.
    expect(editor).to_be_visible()
    expect(editor).to_have_value("# beta\n")
    expect(card.get_by_text("A bridge is running")).to_be_visible()

    # Edit and Save; the green "✓ saved" confirmation appears (no reload).
    editor.fill("# beta\n\nedited by e2e\n")
    card.get_by_role("button", name="Save").click()
    expect(card.get_by_text("✓ saved")).to_be_visible()

    # Re-opening the editor shows the persisted edit (close, then reopen). Assert the
    # Cancel button is present first, so an auto-collapse-on-save regression fails here
    # explicitly rather than as an opaque no-op click + later hidden-state assertion.
    expect(card.get_by_role("button", name="Cancel")).to_be_visible()
    card.get_by_role("button", name="Cancel").click()
    expect(editor).to_be_hidden()
    card.get_by_role("button", name="More actions").click()
    card.get_by_role("button", name="Edit CLAUDE.md").click()
    expect(editor).to_have_value("# beta\n\nedited by e2e\n")


def test_new_project_form_validation_and_clone_toggle(page: Page, open_server: str) -> None:
    """The New-project form gates Create on a name and reveals the Git URL on Clone.

    Read-only validation: the submit stays disabled with an empty name, the clone
    radio swaps in the Git-URL input, and the create radio swaps it back out.
    """
    page.goto(open_server)
    page.get_by_role("button", name="+ New project").click()

    name = page.locator('input[x-model="np.name"]')
    expect(name).to_be_visible()

    # Empty name → the submit (Create) is disabled.
    create = page.get_by_role("button", name="Create", exact=True)
    expect(create).to_be_disabled()
    name.fill("scratch-proj")
    expect(create).to_be_enabled()

    # The Git-URL input is clone-only: hidden under "Create empty", shown under "Clone".
    # Click the radio labels by their `for` target (the empty-state CTA shares the
    # "Clone from git URL" text, so match the form's own label, not visible text).
    git_url = page.locator('input[x-model="np.url"]')
    expect(git_url).to_be_hidden()
    page.locator('label[for="np-clone"]').click()
    expect(git_url).to_be_visible()
    # The submit relabels to "Clone" in clone mode.
    expect(page.get_by_role("button", name="Clone", exact=True)).to_be_visible()
    # Switching back to create hides the URL field again.
    page.locator('label[for="np-create"]').click()
    expect(git_url).to_be_hidden()


def test_create_empty_project_inserts_card_in_place(page: Page, bridge_server: Server) -> None:
    """Creating an empty project inserts its card in the grid with no full-page reload.

    Pins a sentinel on ``window`` and asserts it survives the create, proving the
    new card is grafted into the live grid (the checklist's "no full-page reload"
    guarantee) rather than fetched via a navigation.
    """
    page.goto(bridge_server.url)
    expect(page.locator("#project-grid")).to_be_visible()
    # Sentinel: a full-page reload would wipe this.
    page.evaluate("window.__e2e_no_reload = true")

    page.get_by_role("button", name="+ New project").click()
    page.locator('input[x-model="np.name"]').fill("delta")
    page.get_by_role("button", name="Create", exact=True).click()

    # The new card appears in place (the create API call can be slow in CI, so give it
    # the same headroom as the other waitable assertions in this file)...
    new_card = page.locator('[data-project="delta"]')
    expect(new_card).to_be_visible(timeout=_STATUS_TIMEOUT)
    expect(new_card.get_by_role("heading", name="delta")).to_be_visible()
    # ...and the page never navigated (sentinel intact).
    assert page.evaluate("window.__e2e_no_reload") is True, (
        "page navigated during project creation — the window sentinel was wiped"
    )

    # The inserted card is fully interactive without a refresh: an untrusted new
    # project offers Start, which opens the trust prompt rather than spawning.
    new_card.get_by_role("button", name="Start bridge").click()
    expect(new_card.get_by_role("button", name="Trust & start")).to_be_visible()
