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
    # their content (x-text — the content is untrusted, never HTML).
    browser.expect_count('[data-test="transcript-session"]', 1)
    browser.click('[data-test="transcript-session"]')
    browser.expect_count('[data-test="transcript-turn"]', 2)
    modal_text = browser.get_text('[data-test="transcript-modal"]')
    assert "hello from e2e" in modal_text
    assert "hi back from the fixture" in modal_text


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
