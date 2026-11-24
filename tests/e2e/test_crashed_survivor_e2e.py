"""Browser E2E: a session that outlives its crashed standard bridge (backlog TASK-562).

Drives a real (fake-``claude``) bridge, starts a process with the argv of its session
child, and kills the bridge. The dashboard must show that session on the crashed row with
the Resume warning, and not as a separate External card. When the session ends, the row
must fall back to the plain crashed text.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
import pytest
from _helpers import STATUS_TIMEOUT, open_desktop_launch, trust_and_start

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e

_SESSION_ID = "cse_e2esurvivor562"
_NOTE = '[data-test="bridge-survivor-note"]'
_EXTERNAL = '[data-project="gamma"] [data-test="external-indicator"]'


def _bridge(server: Server) -> dict:
    """Return the fake bridge row of ``gamma``, read from the API."""
    with urllib.request.urlopen(server.url + "/api/instances", timeout=10) as res:  # noqa: S310
        rows = [r for r in json.load(res) if r["project"] == "gamma" and r.get("bridge_pid")]
    assert len(rows) == 1, rows
    return rows[0]


def test_crashed_row_shows_the_surviving_session_and_what_resume_does(
    browser: AgentBrowser, tracked_session_server: Server, mutable_projects_tree: Path
) -> None:
    """The survivor moves onto the crashed row, and leaves it when it exits."""
    server = tracked_session_server
    gamma = (mutable_projects_tree / "gamma").resolve()
    browser.goto(server.url)
    browser.expect_visible('[data-project="gamma"]')
    open_desktop_launch(browser, "gamma")
    trust_and_start(browser, "gamma")
    browser.expect_text("section.zone-active", "Running", timeout_ms=STATUS_TIMEOUT)

    row = _bridge(server)
    log = Path(row["bridge_raw_log_path"])
    survivor = subprocess.Popen(  # noqa: S603 - fixed argv, this interpreter
        [
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            "--print",
            "--sdk-url",
            "https://example.invalid/e2e",
            "--session-id",
            _SESSION_ID,
            "--debug-file",
            str(log.with_name(f"{log.stem}-{_SESSION_ID}{log.suffix}")),
        ],
        cwd=gamma,
    )
    agents = server.state_dir.parent / "agents.json"
    try:
        agents.write_text(
            json.dumps(
                [
                    {
                        "pid": survivor.pid,
                        "cwd": str(gamma),
                        "kind": "interactive",
                        "state": "running",
                        "startedAt": int(time.time() * 1000),
                        "sessionId": "56200000-0000-4000-8000-000000000562",
                    }
                ]
            ),
            encoding="utf-8",
        )
        psutil.Process(row["bridge_pid"]).kill()

        browser.expect_visible(_NOTE, timeout_ms=STATUS_TIMEOUT)
        note = browser.get_text(_NOTE)
        assert f"still running (pid {survivor.pid})" in note
        assert "Resume reattaches" in note
        assert "shell commands and subagents" in note
        browser.expect_hidden(_EXTERNAL)

        survivor.kill()
        survivor.wait(timeout=5)
        agents.write_text("[]", encoding="utf-8")

        browser.expect_hidden(_NOTE, timeout_ms=STATUS_TIMEOUT)
        browser.expect_text("section.zone-active", "exited unexpectedly — Resume to continue")
    finally:
        survivor.kill()
        survivor.wait(timeout=5)
