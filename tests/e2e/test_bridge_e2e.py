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
"""

# TODO(redesign): the two-zone dashboard removed the old project card and its
# "Start bridge" button — launching is now the per-project "Run Claude here"
# popover (mode "In claude.ai / Desktop" routes to the bridge spawn). The
# Playwright selectors below (get_by_role("button", name="Start bridge"), .card
# lookups) need re-targeting to the new row + launch-popover markup. Out of scope
# for the unit-suite green-up; this opt-in suite is excluded from the default/CI run.

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Page, expect

if TYPE_CHECKING:
    from pathlib import Path

    from .conftest import Server

pytestmark = pytest.mark.e2e

# A bridge spawn waits on the fake claude writing its readiness markers, then the
# dashboard's 4s poll reconciles state — give status transitions generous headroom.
_STATUS_TIMEOUT = 20_000


def _read_launch_argv(state_dir: Path, project: str) -> list[str]:
    """Return the argv the fake ``claude`` was spawned with for ``project``.

    The fake writes its argv to ``<debug-file>.argv.json`` beside the bridge log
    (``state_dir/logs/<project>-<ms>.log``), so the test can assert the spawn /
    permission flags the UI passed through.
    """
    argv_files = sorted((state_dir / "logs").glob(f"{project}-*.log.argv.json"))
    assert argv_files, f"no launch argv recorded for {project} under {state_dir / 'logs'}"
    return json.loads(argv_files[-1].read_text())


def test_trust_on_start_starts_then_stops_bridge(page: Page, bridge_server: Server) -> None:
    """An untrusted project: Start prompts for trust (gated on the checkbox), then
    trusts in place and spawns a running bridge; Stop ends it and leaves it resumable."""
    page.goto(bridge_server.url)
    card = page.locator('[data-project="gamma"]')
    expect(card).to_be_visible()

    # Untrusted: the green shield by the name is hidden (the <use> stays in the DOM,
    # gated by x-show/x-cloak), and Start opens the trust prompt rather than spawning.
    shield = card.locator('svg:has(use[href="#ic-trust"])')
    expect(shield).to_be_hidden()
    card.get_by_role("button", name="Start bridge").click()

    # The "Trust & start" action stays disabled until the trust checkbox is ticked.
    trust_start = card.get_by_role("button", name="Trust & start")
    expect(trust_start).to_be_visible()
    expect(trust_start).to_be_disabled()
    card.get_by_role("checkbox").check()
    expect(trust_start).to_be_enabled()
    trust_start.click()

    # Trusted in place (green shield appears) and the bridge reaches RUNNING with a
    # session deep link — no full-page reload.
    expect(shield).to_be_visible()
    status = card.get_by_role("status")
    expect(status).to_contain_text("Running", timeout=_STATUS_TIMEOUT)
    expect(card.get_by_role("button", name="Stop bridge")).to_be_visible()
    expect(card.get_by_role("link", name="Open session in Claude")).to_be_visible()

    # Stop ends the bridge. A stopped standard bridge keeps its environment, so the
    # card offers Resume (not a fresh Start) — the resumable affordance.
    card.get_by_role("button", name="Stop bridge").click()
    expect(status).to_contain_text("Stopped", timeout=_STATUS_TIMEOUT)
    expect(card.get_by_role("button", name="Stop bridge")).to_be_hidden()
    expect(card.get_by_role("button", name="Resume")).to_be_visible()


def test_spawn_options_pass_through_to_bridge_argv(page: Page, bridge_server: Server) -> None:
    """The Options panel exposes spawn-mode / permission-mode / Mode pickers, and the
    chosen spawn + permission values reach the spawned bridge's argv."""
    page.goto(bridge_server.url)
    # alpha is a git repo, so the worktree spawn option and the (POSIX-only) Mode
    # picker both render.
    card = page.locator('[data-project="alpha"]')
    expect(card).to_be_visible()

    card.get_by_role("button", name="Options").click()
    spawn = card.locator("select[x-model=\"spawnMode['alpha']\"]")
    perm = card.locator("select[x-model=\"permMode['alpha']\"]")
    mode = card.locator("select[x-model=\"resumeMode['alpha']\"]")
    expect(spawn).to_be_visible()
    expect(perm).to_be_visible()
    # Mode picker (standard / pty) is POSIX-only; the E2E host is Linux.
    expect(mode).to_be_visible()
    expect(mode.locator("option")).to_have_count(2)

    spawn.select_option("session")
    perm.select_option("plan")

    # Trust + start, then verify the picked options reached the bridge argv.
    card.get_by_role("button", name="Start bridge").click()
    card.get_by_role("checkbox").check()
    card.get_by_role("button", name="Trust & start").click()
    expect(card.get_by_role("status")).to_contain_text("Running", timeout=_STATUS_TIMEOUT)

    argv = _read_launch_argv(bridge_server.state_dir, "alpha")
    assert argv[argv.index("--spawn") + 1] == "session"
    assert argv[argv.index("--permission-mode") + 1] == "plan"


@pytest.mark.skipif(sys.platform == "win32", reason="pty mode is POSIX-only")
def test_pty_mode_start_then_resume_adds_continue(page: Page, bridge_server_pty: Server) -> None:
    """In pty (true-resume) mode the bridge comes up under a PTY keeper with the
    ↻ true-resume badge; the fresh Start carries no ``--continue`` while Resume,
    after a Stop, re-spawns the flag form WITH ``--continue`` (the resume signal)."""
    page.goto(bridge_server_pty.url)
    card = page.locator('[data-project="gamma"]')
    expect(card).to_be_visible()

    # Trust-on-start (same gate as standard mode), then the pty bridge reaches RUNNING.
    card.get_by_role("button", name="Start bridge").click()
    card.get_by_role("checkbox").check()
    card.get_by_role("button", name="Trust & start").click()
    status = card.get_by_role("status")
    expect(status).to_contain_text("Running", timeout=_STATUS_TIMEOUT)
    # The purple ↻ true-resume badge marks a pty (single-session) bridge (the span,
    # not the Mode picker's "pty (true-resume)" <option>).
    expect(card.locator("span.text-purple", has_text="true-resume")).to_be_visible()

    # The fresh start uses the flag form (not the subcommand) and no --continue.
    start_argv = _read_launch_argv(bridge_server_pty.state_dir, "gamma")
    assert "--remote-control" in start_argv  # the flag form...
    assert "remote-control" not in start_argv  # ...never the subcommand form
    assert "--continue" not in start_argv

    # Stop leaves a resumable pty card; Resume re-spawns the flag form with --continue.
    card.get_by_role("button", name="Stop bridge").click()
    expect(status).to_contain_text("Stopped", timeout=_STATUS_TIMEOUT)
    resume = card.get_by_role("button", name="Resume")
    expect(resume).to_be_visible()
    resume.click()
    expect(status).to_contain_text("Running", timeout=_STATUS_TIMEOUT)

    resume_argv = _read_launch_argv(bridge_server_pty.state_dir, "gamma")
    assert "--remote-control" in resume_argv  # resume stays on the flag form...
    assert "remote-control" not in resume_argv  # ...never the subcommand form
    assert "--continue" in resume_argv  # true-resume restores the prior session
