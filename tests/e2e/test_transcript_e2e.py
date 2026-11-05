"""Browser E2E for the read-only transcript viewer (#431 — gap fill from the #763 audit).

The transcript trigger, modal, session list, and turn rendering shipped with full
``data-test`` hooks but had zero E2E coverage — the button had never been clicked in
an automated browser. These drive the whole journey against a real server:

* a project WITH a seeded Claude-format transcript lists the session and renders its
  turns (user + assistant, redacted server-side, ``x-text``-only);
* a project WITHOUT transcripts shows the explicit empty state, not a blank modal.

Seeding rides ``usage_server``'s isolated HOME: transcripts live under
``$HOME/.claude/projects/<sanitized-cwd>/`` and are read on demand at click time, so
the test writes its session file directly before opening the modal.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from _helpers import STATUS_TIMEOUT

if TYPE_CHECKING:
    from pathlib import Path

    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e


def _seed_session(home: Path, project_path: Path, name: str) -> None:
    """Write one Claude-format transcript with a user + assistant turn for ``project_path``."""
    from clauster.pointers import sanitize_cwd  # pure cwd→dirname mapping

    tdir = home / ".claude" / "projects" / sanitize_cwd(project_path)
    tdir.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "timestamp": "2026-07-16T00:00:00Z",
            "message": {"role": "user", "content": "hello from e2e"},
        },
        {
            "timestamp": "2026-07-16T00:00:05Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": "hi back from the fixture",
            },
        },
    ]
    (tdir / name).write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def test_transcript_modal_lists_session_and_renders_turns(
    browser: AgentBrowser, usage_server: Server, mutable_projects_tree: Path
) -> None:
    """The Transcript trigger opens the modal, lists the session, and renders its turns."""
    # beta has no fixture-seeded transcript — seed exactly one session for it so the
    # list content is deterministic (alpha carries the usage fixture's cost-only seed).
    home = usage_server.state_dir.parent / "home"
    _seed_session(home, (mutable_projects_tree / "beta").resolve(), "sess-e2e.jsonl")

    browser.goto(usage_server.url)
    browser.expect_visible('[data-project="beta"]')

    browser.click('[data-project="beta"] [data-test="transcript-trigger"]')
    browser.expect_visible('[data-test="transcript-modal"]')

    # Exactly the one seeded session is offered; picking it renders both turns with
    # their content (x-text — the content is untrusted, never HTML). Both steps fetch
    # from the server — give them bridge-transition headroom on a loaded CI runner.
    browser.expect_count('[data-test="transcript-session"]', 1, timeout_ms=STATUS_TIMEOUT)
    browser.click('[data-test="transcript-session"]')
    browser.expect_count('[data-test="transcript-turn"]', 2, timeout_ms=STATUS_TIMEOUT)
    # The turns exist in the DOM as soon as the fetch lands, but the turns view is an
    # x-show whose reveal applies on a later animation frame — seconds away on a starved
    # runner — so wait for the VISIBLE text rather than reading it once (get_text is
    # innerText: it returns the still-shown picker until that frame arrives).
    modal = '[data-test="transcript-modal"]'
    browser.expect_text(modal, "hello from e2e", timeout_ms=STATUS_TIMEOUT)
    browser.expect_text(modal, "hi back from the fixture", timeout_ms=STATUS_TIMEOUT)


def test_transcript_modal_shows_empty_state_without_transcripts(
    browser: AgentBrowser, usage_server: Server
) -> None:
    """A project with no transcripts gets the explicit empty state, not a blank modal."""
    browser.goto(usage_server.url)
    browser.expect_visible('[data-project="gamma"]')

    browser.click('[data-project="gamma"] [data-test="transcript-trigger"]')
    browser.expect_visible('[data-test="transcript-modal"]')
    browser.expect_visible('[data-test="transcript-empty"]')
    browser.expect_text('[data-test="transcript-empty"]', "No transcripts for this project yet.")


def _seed_long_session(home: Path, project_path: Path, name: str, turns: int) -> None:
    """Write a ``turns``-long transcript: even turns say ``even``, every tenth ``needle``."""
    from clauster.pointers import sanitize_cwd  # pure cwd→dirname mapping

    tdir = home / ".claude" / "projects" / sanitize_cwd(project_path)
    tdir.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "timestamp": f"2026-07-16T00:{i // 60:02d}:{i % 60:02d}Z",
            "message": {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"turn {i}"
                + (" even" if i % 2 == 0 else "")
                + (" needle" if i % 10 == 0 else ""),
            },
        }
        for i in range(turns)
    ]
    (tdir / name).write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def test_transcript_position_indicator_tracks_paging_search_and_sort(
    browser: AgentBrowser, usage_server: Server, mutable_projects_tree: Path
) -> None:
    """The paged viewer shows "showing X of Y turns" and what Load more has left to fetch.

    The read-only viewer used to page blind (backlog TASK-268). The position line and
    the Load more label must stay honest after each Load more, under a search (``total``
    is the post-filter count), and after the sort flips (the turns left become newer).
    """
    home = usage_server.state_dir.parent / "home"
    _seed_long_session(home, (mutable_projects_tree / "beta").resolve(), "sess-268.jsonl", 120)

    browser.goto(usage_server.url)
    browser.expect_visible('[data-project="beta"]')
    browser.click('[data-project="beta"] [data-test="transcript-trigger"]')
    browser.expect_visible('[data-test="transcript-modal"]')
    browser.expect_count('[data-test="transcript-session"]', 1, timeout_ms=STATUS_TIMEOUT)
    browser.click('[data-test="transcript-session"]')

    position = '[data-test="transcript-position"]'
    more = '[data-test="transcript-load-more"]'
    browser.expect_text(position, "showing 50 of 120 turns", timeout_ms=STATUS_TIMEOUT)
    browser.expect_attr(position, "aria-live", "polite")
    browser.expect_text(more, "Load more (70 older)", timeout_ms=STATUS_TIMEOUT)

    # Each Load more appends below the list and moves the button down, so wait for the
    # new position text (the layout settled) before the next click.
    browser.scroll_into_view(more)
    browser.click(more)
    browser.expect_text(position, "showing 100 of 120 turns", timeout_ms=STATUS_TIMEOUT)
    browser.expect_text(more, "Load more (20 older)", timeout_ms=STATUS_TIMEOUT)
    browser.scroll_into_view(more)
    browser.click(more)
    browser.expect_text(position, "showing 120 of 120 turns", timeout_ms=STATUS_TIMEOUT)
    browser.expect_hidden(more, timeout_ms=STATUS_TIMEOUT)

    # A search counts only the matching turns (12 of the 120 carry "needle").
    search = '[data-test="transcript-search"]'
    browser.scroll_into_view(search)
    browser.fill(search, "needle")
    browser.expect_text(position, "showing 12 of 12 matching turns", timeout_ms=STATUS_TIMEOUT)
    browser.expect_hidden(more, timeout_ms=STATUS_TIMEOUT)

    # A search with no hits says so in the live region, not only in the visual empty state.
    browser.fill(search, "zzz")
    browser.expect_visible('[data-test="transcript-no-matches"]', timeout_ms=STATUS_TIMEOUT)
    browser.expect_text(position, "0 matching turns", timeout_ms=STATUS_TIMEOUT)

    # Load more continues the search on screen, even when the box now holds a term that
    # is too short to search: the 60 "even" turns stay 60, never the unfiltered 120.
    browser.fill(search, "even")
    browser.expect_text(position, "showing 50 of 60 matching turns", timeout_ms=STATUS_TIMEOUT)
    browser.expect_text(more, "Load more (10 older)", timeout_ms=STATUS_TIMEOUT)
    browser.fill(search, "e")
    # The "keep typing" hint opens above the list and moves the button: wait for it.
    browser.expect_visible('[data-test="transcript-query-short"]', timeout_ms=STATUS_TIMEOUT)
    browser.scroll_into_view(more)
    browser.click(more)
    browser.expect_text(position, "showing 60 of 60 matching turns", timeout_ms=STATUS_TIMEOUT)
    browser.expect_hidden(more, timeout_ms=STATUS_TIMEOUT)

    browser.scroll_into_view(search)
    browser.click('[data-test="transcript-search-clear"]')
    browser.expect_text(position, "showing 50 of 120 turns", timeout_ms=STATUS_TIMEOUT)

    # Oldest first: the turns that Load more has left are now the newer ones.
    browser.expect_enabled('[data-test="transcript-order-toggle"]', timeout_ms=STATUS_TIMEOUT)
    browser.click('[data-test="transcript-order-toggle"]')
    browser.expect_text(more, "Load more (70 newer)", timeout_ms=STATUS_TIMEOUT)
    browser.expect_text(position, "showing 50 of 120 turns", timeout_ms=STATUS_TIMEOUT)
