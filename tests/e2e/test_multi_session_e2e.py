"""Browser E2E for the multi-session dashboard (#779).

A project can now run one standard (Server Mode) bridge plus N interactive (pty)
sessions concurrently, and the Active zone renders ONE row per session (keyed by
``rk`` = instance_id, not by project). This drives the full flow against a real
(fake-``claude``) server:

* launching an Interactive Session without a worktree shows the amber collision
  hint in the popover (``pty-collision-hint``) and, after the spawn, the server's
  "without a worktree" advisory as a warning toast (#778);
* a pty spawn surfaces an explicit pending stub (``pty-pending-row``) until its
  live row lands;
* two interactive sessions plus the standard bridge for the SAME project show as
  three concurrent rows, chipped "interactive" (pty rows) and "worktree" (the
  isolated one);
* stopping one pty session leaves the other pty + standard rows running and parks
  the stopped one in Recent as resumable; Resume revives the SAME identity (the
  Recent row empties instead of leaving a stale stopped twin) back to three rows.

See ``tests/E2E_CHECKLIST.md`` for the full manual list.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from _helpers import (
    RECENT_TOGGLE,
    STATUS_TIMEOUT,
    click_run,
    open_desktop_launch,
    read_launch_argv,
    trust_and_start,
)

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform == "win32", reason="pty (Interactive Session) is POSIX-only"),
]

# The live Active list. The DIRECT-child `>` scoping to the section's card is
# load-bearing: the collapsed Recent group nests its own `.card`, so a descendant
# `.card` would also count stopped (hidden) rows. x-for renders one div per live
# session, so the DOM count equals the session count (the pty pending stub is also
# a .sess-row here while a spawn is in flight — gate on it being hidden before
# asserting shapes).
_ACTIVE_CARD = "section.zone-active > .card"
_ACTIVE_ROWS = f"{_ACTIVE_CARD} .list-group > div.sess-row"
_RECENT_GROUP = 'section.zone-active div[x-show="hasRecent()"]'
_PENDING_ROW = '[data-test="pty-pending-row"]'
_COLLISION_HINT = '[data-project="alpha"] [data-test="pty-collision-hint"]'


def _pick_mode_and_spawn(browser: AgentBrowser, project: str, mode: str, spawn: str) -> None:
    """Expand "More options" and pick the Mode (standard|pty) + Spawn for this launch.

    The popover collapses More options on every reopen (mopen resets on dismiss),
    so each launch expands it afresh; the select VALUES persist per project.
    """
    browser.click(f'[data-project="{project}"] [data-test="launch-more-options"]')
    browser.expect_visible(f"#resume-{project}")
    browser.select(f"#resume-{project}", mode)
    browser.select(f"#spawn-{project}", spawn)


def _session_shape(browser: AgentBrowser) -> dict[str, int]:
    """One-shot shape of the VISIBLE Active-list rows: count + per-row chip visibility.

    The "interactive" / "worktree" chips are x-show'd spans that exist in every
    bridge row's DOM, so a CSS count can't distinguish shown from hidden — resolve
    visibility in-page instead. One-shot by design: callers gate on a polled
    ``expect_count`` / ``expect_text`` first (mirrors the suite's polled-gate →
    one-shot-read pattern for the desktop badge).
    """
    # Interpolate the module's own row selector (not a hand-kept copy) so the JS and
    # the Python-side expect_count() can never drift apart.
    return browser.eval_json(
        f"""(() => {{
          const vis = (el) => !!el && el.offsetParent !== null;
          const rows = Array.from(document.querySelectorAll(
            {_ACTIVE_ROWS!r})).filter(vis);
          const chip = (sel) => rows.filter(
            (r) => Array.from(r.querySelectorAll(sel)).some(vis)).length;
          return {{
            rows: rows.length,
            interactive: chip('[data-test="interactive-chip"]'),
            worktree: chip('[data-test="worktree-chip"]'),
          }};
        }})()"""
    )


def test_two_interactive_sessions_plus_standard_bridge_stop_and_resume(
    browser: AgentBrowser, multi_session_server: Server
) -> None:
    """Three concurrent sessions for ONE project (2 pty + 1 standard), each its own row;
    stopping one pty row leaves the other two running and Resume revives the same one."""
    browser.goto(multi_session_server.url)
    # alpha is a git repo, so the worktree spawn option and the Mode picker render.
    browser.expect_visible('[data-project="alpha"]')

    # --- 1: Interactive Session, Spawn=same-dir → collision hint + warning toast ---
    open_desktop_launch(browser, "alpha")
    _pick_mode_and_spawn(browser, "alpha", "pty", "same-dir")
    # The amber no-worktree collision hint shows BEFORE launching (warn, never block).
    browser.expect_visible(_COLLISION_HINT)
    trust_and_start(browser, "alpha")
    # The pending stub bridges the gap until the spawn POST mints the session row
    # (the fixture's pty_slow fake keeps that window open long enough to observe).
    browser.expect_visible(_PENDING_ROW, timeout_ms=STATUS_TIMEOUT)
    # The launch proceeded anyway and the server echoed the same advisory (#778)
    # as a warning toast.
    browser.expect_text(
        ".toast-stack .alert-warning", "without a worktree", timeout_ms=STATUS_TIMEOUT
    )
    browser.expect_text(_ACTIVE_CARD, "Running", timeout_ms=STATUS_TIMEOUT)
    browser.expect_count(_ACTIVE_ROWS, 1, timeout_ms=STATUS_TIMEOUT)
    browser.expect_hidden(_PENDING_ROW)
    # The one live row is a pty session (visible "interactive" chip), not isolated.
    assert _session_shape(browser) == {"rows": 1, "interactive": 1, "worktree": 0}

    # --- 2: second Interactive Session for the SAME project, Spawn=worktree ---
    open_desktop_launch(browser, "alpha")
    _pick_mode_and_spawn(browser, "alpha", "pty", "worktree")
    # Isolated spawn → no collision hint for this launch.
    browser.expect_hidden(_COLLISION_HINT)
    click_run(browser, "alpha")  # alpha is trusted now — no gate
    browser.expect_count(_ACTIVE_ROWS, 2, timeout_ms=STATUS_TIMEOUT)
    browser.expect_hidden(_PENDING_ROW, timeout_ms=STATUS_TIMEOUT)
    # The worktree chip lands on the new row (polled: the row may still be starting).
    browser.expect_text(_ACTIVE_CARD, "worktree", timeout_ms=STATUS_TIMEOUT)
    # Two DISTINCT pty rows for one project, exactly one isolated in a worktree.
    assert _session_shape(browser) == {"rows": 2, "interactive": 2, "worktree": 1}

    # --- 3: the standard (Server Mode) bridge beside both interactive sessions ---
    open_desktop_launch(browser, "alpha")
    _pick_mode_and_spawn(browser, "alpha", "standard", "same-dir")
    click_run(browser, "alpha")
    browser.expect_count(_ACTIVE_ROWS, 3, timeout_ms=STATUS_TIMEOUT)
    # 3 concurrent rows: standard (no interactive chip) + the two pty sessions.
    assert _session_shape(browser) == {"rows": 3, "interactive": 2, "worktree": 1}

    # --- 4: stop ONE pty row via its own Stop button ---
    # Select the same-dir Interactive Session by its own row attributes (the rows carry
    # data-resume-mode / data-spawn-mode) instead of a position — row order depends on
    # activeBridges() concatenation and would silently shift if a hosted/detached row
    # ever rendered above it.
    browser.click(
        f'{_ACTIVE_ROWS}[data-resume-mode="pty"][data-spawn-mode="same-dir"]'
        ' [data-test="stop-session"]'
    )
    browser.expect_count(_ACTIVE_ROWS, 2, timeout_ms=STATUS_TIMEOUT)
    # The worktree pty and the standard bridge survive — we stopped the same-dir pty.
    assert _session_shape(browser) == {"rows": 2, "interactive": 1, "worktree": 1}
    # The stopped session parks in Recent as resumable (and it is the interactive one).
    browser.expect_visible(RECENT_TOGGLE, timeout_ms=STATUS_TIMEOUT)
    browser.click(RECENT_TOGGLE)
    browser.expect_text(_RECENT_GROUP, "Stopped", timeout_ms=STATUS_TIMEOUT)
    browser.expect_text(_RECENT_GROUP, "interactive")
    resume_btn = f'{_RECENT_GROUP} [data-test="resume-session"]'
    browser.expect_visible(resume_btn)

    # --- 5: Resume it from Recent → back to 3 concurrent rows, same identity ---
    browser.click(resume_btn)
    browser.expect_count(_ACTIVE_ROWS, 3, timeout_ms=STATUS_TIMEOUT)
    browser.expect_hidden(_PENDING_ROW, timeout_ms=STATUS_TIMEOUT)
    assert _session_shape(browser) == {"rows": 3, "interactive": 2, "worktree": 1}
    # Same-identity revival: the Recent group emptied (hasRecent() is false) — a
    # resume that minted a NEW instance would have left the stopped twin behind.
    browser.expect_hidden(RECENT_TOGGLE, timeout_ms=STATUS_TIMEOUT)
    # And it was a true pty resume: the newest alpha spawn is the flag form with
    # --continue (restores the prior conversation), not a fresh start.
    resume_argv = read_launch_argv(multi_session_server.state_dir, "alpha")
    assert "--remote-control" in resume_argv
    assert "--continue" in resume_argv
