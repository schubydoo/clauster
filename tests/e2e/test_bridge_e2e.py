"""Browser E2E for the bridge-spawn flows (trust-on-start, lifecycle, spawn options).

The second slice of the automated browser suite (the first, ``test_smoke_e2e``,
covered the non-bridge flows). These drive a real headless Chromium against a
clauster whose ``claude`` binary is the fake bridge in ``tests/fixtures/fake_claude``
— so an actual bridge subprocess is spawned, reaches RUNNING, and is stopped, all
through the dashboard UI. See ``tests/E2E_CHECKLIST.md`` for the full manual list;
these port the **Start / Stop**, **Trust-on-Start**, and **Spawn controls** rows.

These cover the *standard* (subcommand) mode; the gated pty true-resume flow stays
manual for now (it brings up a PTY keeper subprocess — a heavier moving part to
automate reliably).
"""

from __future__ import annotations

import json
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
