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

import json
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e

# A bridge spawn waits on the fake claude writing its readiness markers, then the
# dashboard's 4s poll reconciles state — give status transitions generous headroom.
_STATUS_TIMEOUT = 20_000

# Stopped bridges live in the collapsed "Recent / resumable" group; this is its
# expand toggle (the only button under the hasRecent() container).
_RECENT_TOGGLE = 'section.zone-active div[x-show="hasRecent()"] > button'


def _read_launch_argv(state_dir: Path, project: str) -> list[str]:
    """Return the argv the fake ``claude`` was spawned with for ``project``.

    The fake writes its argv to ``<debug-file>.argv.json`` beside the bridge log
    (``state_dir/logs/<project>-<ms>.log``), so the test can assert the spawn /
    permission flags the UI passed through.
    """
    argv_files = sorted((state_dir / "logs").glob(f"{project}-*.log.argv.json"))
    assert argv_files, f"no launch argv recorded for {project} under {state_dir / 'logs'}"
    return json.loads(argv_files[-1].read_text())


def _open_desktop_launch(browser: AgentBrowser, project: str) -> None:
    """Open the project's launch popover and select the desktop (bridge) mode."""
    browser.click(f'[data-project="{project}"] .launch-anchor button')
    browser.expect_visible(f'[data-project="{project}"] .launch-pop')
    browser.check(f'[data-project="{project}"] input[name="lm-{project}"][value="desktop"]')


def _trust_and_start(browser: AgentBrowser, project: str) -> None:
    """Click Run, then satisfy the trust gate (checkbox enables Trust & start)."""
    browser.click(f'[data-project="{project}"] .launch-pop button.btn-primary.w-100')
    trust_start = f'[data-project="{project}"] .alert-warning button.btn-warning'
    browser.expect_visible(trust_start)
    browser.expect_disabled(trust_start)
    browser.check(f'[data-project="{project}"] .alert-warning input[type="checkbox"]')
    browser.expect_enabled(trust_start)
    browser.click(trust_start)


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

    _open_desktop_launch(browser, "gamma")
    _trust_and_start(browser, "gamma")

    # Trusted in place (green shield appears) and the bridge reaches RUNNING with a
    # session deep link in the Active zone — no full-page reload.
    browser.expect_visible(trusted_shield)
    browser.expect_text("section.zone-active", "Running", timeout_ms=_STATUS_TIMEOUT)
    # The "desktop" mode badge confirms this is the bridge row (not another session type).
    # .mode-badge uppercases its text via CSS, so get_text() reports "DESKTOP" — assert
    # case-insensitively (mirrors test_observability_e2e). The "Running" wait above already
    # gated on the row being rendered and the static badge stamps with it, so a one-shot
    # read is race-free — the prior timeout-bump "de-flake" never matched at all.
    assert "desktop" in browser.get_text("section.zone-active").lower()
    browser.expect_role_visible("link", "Open in Claude")
    stop_btn = "section.zone-active .btn-outline-danger"
    browser.expect_visible(stop_btn)

    # Stop ends the bridge. A stopped standard bridge keeps its environment, so it
    # moves to "Recent / resumable" and offers Resume (not a fresh Start).
    browser.click(stop_btn)
    browser.expect_hidden(stop_btn, timeout_ms=_STATUS_TIMEOUT)
    # The Recent group is collapsed; expand it by its toggle (its text carries the count).
    browser.expect_visible(_RECENT_TOGGLE, timeout_ms=_STATUS_TIMEOUT)
    browser.click(_RECENT_TOGGLE)
    browser.expect_text("section.zone-active", "Stopped", timeout_ms=_STATUS_TIMEOUT)
    browser.expect_visible("section.zone-active .btn-success")
    browser.expect_role_visible("button", "Resume")


def test_spawn_options_pass_through_to_bridge_argv(
    browser: AgentBrowser, bridge_server: Server
) -> None:
    """The Advanced panel exposes spawn-mode / Mode pickers (and Permissions), and the
    chosen spawn + permission values reach the spawned bridge's argv."""
    browser.goto(bridge_server.url)
    # alpha is a git repo, so the worktree spawn option and the (POSIX-only) Mode
    # picker both render.
    browser.expect_visible('[data-project="alpha"]')

    _open_desktop_launch(browser, "alpha")
    # Permissions live in the popover; the spawn + Mode pickers behind Advanced (the
    # only ghost-secondary button in the popover).
    browser.select("#perm-alpha", "plan")
    browser.click('[data-project="alpha"] .launch-pop button.btn-ghost-secondary')
    browser.expect_visible("#spawn-alpha")
    browser.expect_visible("#resume-alpha")
    # Mode picker (standard / pty) is POSIX-only; the E2E host is Linux.
    browser.expect_count("#resume-alpha option", 2)
    browser.select("#spawn-alpha", "session")

    # Trust + start, then verify the picked options reached the bridge argv.
    _trust_and_start(browser, "alpha")
    browser.expect_text("section.zone-active", "Running", timeout_ms=_STATUS_TIMEOUT)

    argv = _read_launch_argv(bridge_server.state_dir, "alpha")
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
    _open_desktop_launch(browser, "gamma")
    _trust_and_start(browser, "gamma")
    browser.expect_text("section.zone-active", "Running", timeout_ms=_STATUS_TIMEOUT)

    # The fresh start uses the flag form (not the subcommand) and no --continue.
    start_argv = _read_launch_argv(bridge_server_pty.state_dir, "gamma")
    assert "--remote-control" in start_argv  # the flag form...
    assert "remote-control" not in start_argv  # ...never the subcommand form
    assert "--continue" not in start_argv

    # Stop leaves a resumable pty bridge in Recent; Resume re-spawns the flag form with
    # --continue.
    browser.click("section.zone-active .btn-outline-danger")
    browser.expect_visible(_RECENT_TOGGLE, timeout_ms=_STATUS_TIMEOUT)
    browser.click(_RECENT_TOGGLE)
    browser.expect_text("section.zone-active", "Stopped", timeout_ms=_STATUS_TIMEOUT)
    resume = "section.zone-active .btn-success"
    browser.expect_visible(resume)
    browser.click(resume)
    browser.expect_text("section.zone-active", "Running", timeout_ms=_STATUS_TIMEOUT)

    resume_argv = _read_launch_argv(bridge_server_pty.state_dir, "gamma")
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
    browser.click('[data-project="alpha"] .launch-anchor button')
    browser.expect_visible('[data-project="alpha"] .launch-pop')

    # The picker offers the mode under the unchanged internal value "detached".
    bg_radio = '[data-project="alpha"] input[name="lm-alpha"][value="detached"]'
    browser.expect_visible(bg_radio)

    # Default mode is desktop, so the detached sub-fields start hidden.
    first_prompt = "#lprompt-alpha"
    cloud_optin = '[data-project="alpha"] input[x-model="lcloud"]'
    browser.expect_hidden(first_prompt)

    # Selecting it marks its option (.is-sel) and reveals the sub-fields. Assert the
    # SELECTED option's own label reads "Background" (not just that the word appears
    # somewhere in the popover), so a future relabel of *this* option is caught.
    browser.check(bg_radio)
    selected_label = '[data-project="alpha"] label.launch-opt.is-sel .fw-medium'
    browser.expect_text(selected_label, "Background")
    browser.expect_visible(first_prompt)
    browser.expect_visible(cloud_optin)
