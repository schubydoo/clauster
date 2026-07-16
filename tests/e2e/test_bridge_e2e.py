"""Browser E2E for the bridge-spawn flows (trust-on-start, lifecycle, spawn options).

The second slice of the automated browser suite (the first, ``test_smoke_e2e``,
covered the non-bridge flows). These drive a real headless Chromium against a
clauster whose ``claude`` binary is the fake bridge in ``tests/fixtures/fake_claude``
— so an actual bridge subprocess is spawned, reaches RUNNING, and is stopped, all
through the dashboard UI. See ``tests/E2E_CHECKLIST.md`` for the full manual list;
these port the **Start / Stop**, **Trust-on-Start**, and **Spawn controls** rows.

These cover the *standard* (subcommand) mode plus the gated **pty true-resume**
lifecycle (a real PTY keeper subprocess + the ``--continue`` resume signal). The
pty Resume *content* check — that the prior conversation is actually restored —
stays manual, since the fake bridge has no conversation to restore.

Two-zone DOM: launching is the per-project "Run Claude here" popover (mode
``desktop`` routes to the bridge spawn); a running/stopped bridge surfaces in the
Active-sessions zone, with stopped bridges in the collapsed "Recent / resumable"
group.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from _helpers import (
    RECENT_TOGGLE,
    RESUME_BUTTON,
    STATUS_TIMEOUT,
    STOP_BUTTON,
    open_desktop_launch,
    read_launch_argv,
    trust_and_start,
)

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e


def test_trust_on_start_starts_then_stops_bridge(
    browser: AgentBrowser, bridge_server: Server
) -> None:
    """An untrusted project: Run prompts for trust (gated on the checkbox), then
    trusts in place and spawns a running bridge; Stop ends it and leaves it resumable."""
    browser.goto(bridge_server.url)
    browser.expect_visible('[data-project="gamma"]')

    # Untrusted: the green "trusted" shield by the name is hidden (the <use> stays in
    # the DOM, gated by x-show/x-cloak), and Run opens the trust prompt rather than
    # spawning.
    trusted_shield = '[data-project="gamma"] svg[aria-label="Directory trusted"]'
    browser.expect_hidden(trusted_shield)

    open_desktop_launch(browser, "gamma")
    trust_and_start(browser, "gamma")

    # Trusted in place (green shield appears) and the bridge reaches RUNNING with a
    # session deep link in the Active zone — no full-page reload.
    browser.expect_visible(trusted_shield)
    browser.expect_text("section.zone-active", "Running", timeout_ms=STATUS_TIMEOUT)
    # The "desktop" mode badge confirms this is the bridge row (not another session type).
    # .mode-badge uppercases its text via CSS, so get_text() reports "DESKTOP" — assert
    # case-insensitively (mirrors test_observability_e2e). The "Running" wait above already
    # gated on the row being rendered and the static badge stamps with it, so a one-shot
    # read is race-free — the prior timeout-bump "de-flake" never matched at all.
    assert "desktop" in browser.get_text("section.zone-active").lower()
    browser.expect_role_visible("link", "Open in Claude")
    stop_btn = STOP_BUTTON
    browser.expect_visible(stop_btn)

    # Stop ends the bridge. A stopped standard bridge keeps its environment, so it
    # moves to "Recent / resumable" and offers Resume (not a fresh Start).
    browser.click(stop_btn)
    browser.expect_hidden(stop_btn, timeout_ms=STATUS_TIMEOUT)
    # The Recent group is collapsed; expand it by its toggle (its text carries the count).
    browser.expect_visible(RECENT_TOGGLE, timeout_ms=STATUS_TIMEOUT)
    browser.click(RECENT_TOGGLE)
    browser.expect_text("section.zone-active", "Stopped", timeout_ms=STATUS_TIMEOUT)
    browser.expect_visible(RESUME_BUTTON)
    browser.expect_role_visible("button", "Resume")


def test_spawn_options_pass_through_to_bridge_argv(
    browser: AgentBrowser, bridge_server: Server
) -> None:
    """The "More options" panel exposes spawn-mode / Mode pickers (and Permissions), and
    the chosen spawn + permission values reach the spawned bridge's argv."""
    browser.goto(bridge_server.url)
    # alpha is a git repo, so the worktree spawn option and the (POSIX-only) Mode
    # picker both render.
    browser.expect_visible('[data-project="alpha"]')

    open_desktop_launch(browser, "alpha")
    # Permissions live in the popover; the spawn + Mode pickers sit behind the
    # "More options" disclosure.
    browser.select("#perm-alpha", "plan")
    browser.click('[data-project="alpha"] [data-test="launch-more-options"]')
    browser.expect_visible("#spawn-alpha")
    browser.expect_visible("#resume-alpha")
    # Mode picker (standard / pty) is POSIX-only; the E2E host is Linux.
    browser.expect_count("#resume-alpha option", 2)
    browser.select("#spawn-alpha", "session")

    # Trust + start, then verify the picked options reached the bridge argv.
    trust_and_start(browser, "alpha")
    browser.expect_text("section.zone-active", "Running", timeout_ms=STATUS_TIMEOUT)

    argv = read_launch_argv(bridge_server.state_dir, "alpha")
    assert argv[argv.index("--spawn") + 1] == "session"
    assert argv[argv.index("--permission-mode") + 1] == "plan"


@pytest.mark.skipif(sys.platform == "win32", reason="pty mode is POSIX-only")
def test_pty_mode_start_then_resume_adds_continue(
    browser: AgentBrowser, bridge_server_pty: Server
) -> None:
    """In pty (true-resume) mode the bridge comes up under a PTY keeper; the fresh
    Start carries no ``--continue`` while Resume, after a Stop, re-spawns the flag form
    WITH ``--continue`` (the resume signal)."""
    browser.goto(bridge_server_pty.url)
    browser.expect_visible('[data-project="gamma"]')

    # Trust-on-start (same gate as standard mode), then the pty bridge reaches RUNNING.
    open_desktop_launch(browser, "gamma")
    trust_and_start(browser, "gamma")
    browser.expect_text("section.zone-active", "Running", timeout_ms=STATUS_TIMEOUT)

    # The fresh start uses the flag form (not the subcommand) and no --continue.
    start_argv = read_launch_argv(bridge_server_pty.state_dir, "gamma")
    assert "--remote-control" in start_argv  # the flag form...
    assert "remote-control" not in start_argv  # ...never the subcommand form
    assert "--continue" not in start_argv

    # Stop leaves a resumable pty bridge in Recent; Resume re-spawns the flag form with
    # --continue.
    browser.click(STOP_BUTTON)
    browser.expect_visible(RECENT_TOGGLE, timeout_ms=STATUS_TIMEOUT)
    browser.click(RECENT_TOGGLE)
    browser.expect_text("section.zone-active", "Stopped", timeout_ms=STATUS_TIMEOUT)
    resume = RESUME_BUTTON
    browser.expect_visible(resume)
    browser.click(resume)
    browser.expect_text("section.zone-active", "Running", timeout_ms=STATUS_TIMEOUT)

    resume_argv = read_launch_argv(bridge_server_pty.state_dir, "gamma")
    assert "--remote-control" in resume_argv  # resume stays on the flag form...
    assert "remote-control" not in resume_argv  # ...never the subcommand form
    assert "--continue" in resume_argv  # true-resume restores the prior session


def test_background_launch_option_labeled_and_reveals_subfields(
    browser: AgentBrowser, open_server: str
) -> None:
    """The detached/background launch mode reads "Background" (UX-04, #567) and choosing
    it reveals its sub-fields — no spawn, so a read-only server is enough.

    Guards the rename's only surface with no other test: the launch picker. The radio's
    internal value stays ``detached`` (the routing token), only the visible label moved
    Fire-and-forget → Background, and the ``lmode === 'detached'`` sub-UI (the claude.ai
    opt-in + the optional first-prompt box) gates on that unchanged token.
    """
    browser.goto(open_server)
    browser.expect_visible('[data-project="alpha"]')

    # Open the per-project launch popover.
    browser.click('[data-project="alpha"] [data-test="run-launch"]')
    browser.expect_visible('[data-project="alpha"] .launch-pop')

    # The picker offers the mode under the unchanged internal value "detached".
    bg_radio = '[data-project="alpha"] input[name="lm-alpha"][value="detached"]'
    browser.expect_visible(bg_radio)

    # Default mode is desktop, so the detached sub-fields start hidden.
    first_prompt = "#lprompt-alpha"
    cloud_optin = '[data-project="alpha"] [data-test="launch-cloud-optin"]'
    browser.expect_hidden(first_prompt)

    # Selecting it marks its option (.is-sel) and reveals the sub-fields. Assert the
    # SELECTED option's own label reads "Background" (not just that the word appears
    # somewhere in the popover), so a future relabel of *this* option is caught.
    browser.check(bg_radio)
    selected_label = '[data-project="alpha"] label.launch-opt.is-sel .fw-medium'
    browser.expect_text(selected_label, "Background")
    browser.expect_visible(first_prompt)
    browser.expect_visible(cloud_optin)
