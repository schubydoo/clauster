"""Browser E2E for the per-session CPU/RAM figure on a bridge's live-session rows.

Backlog TASK-342. A standard bridge runs each session as its own child process, so the
metrics sampler can split the bridge figure per session. This drives that against a
real (fake-``claude``) server whose bridge starts a real child process:

* the session whose ``agents --json`` pid is that child shows its own figure;
* a session whose pid is the bridge process itself shows NO figure, because the
  sampler cannot place it inside the bridge's tree, and the row says nothing rather
  than guess.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import TYPE_CHECKING

import pytest
from _helpers import STATUS_TIMEOUT, open_desktop_launch, trust_and_start

if TYPE_CHECKING:
    from pathlib import Path

    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e

_CHILD_SESSION = "34200000-0000-4000-8000-000000000342"
_BRIDGE_SESSION = "34300000-0000-4000-8000-000000000343"


def _bridge_pid(server: Server, project: str) -> int:
    """Return the ``bridge_pid`` of ``project``'s running bridge, read from the API."""
    with urllib.request.urlopen(server.url + "/api/instances", timeout=10) as res:  # noqa: S310
        rows = json.load(res)
    pids = [r["bridge_pid"] for r in rows if r["project"] == project and r.get("bridge_pid")]
    assert len(pids) == 1, rows
    return pids[0]


def _session_child_pid(server: Server) -> int:
    """Wait for the fake bridge to write its session child's pid, and return it."""
    pid_file = server.state_dir.parent / "session-child.pid"
    deadline = time.monotonic() + STATUS_TIMEOUT / 1000
    while not pid_file.exists():
        assert time.monotonic() < deadline, "the fake bridge never started its session child"
        time.sleep(0.1)
    return int(pid_file.read_text(encoding="utf-8"))


def test_bridge_session_row_shows_its_own_cpu_and_ram(
    browser: AgentBrowser, session_child_server: Server, mutable_projects_tree: Path
) -> None:
    """Only the session placed inside the bridge's process tree gets a figure."""
    server = session_child_server
    gamma = (mutable_projects_tree / "gamma").resolve()
    browser.goto(server.url)
    browser.expect_visible('[data-project="gamma"]')
    open_desktop_launch(browser, "gamma")
    trust_and_start(browser, "gamma")
    browser.expect_text("section.zone-active", "Running", timeout_ms=STATUS_TIMEOUT)

    def session(pid: int, uuid: str, started: int) -> dict:
        return {
            "pid": pid,
            "cwd": str(gamma),
            "kind": "interactive",
            "state": "running",
            "startedAt": started,
            "sessionId": uuid,
        }

    # Both pids are the bridge's own (the bridge, and its child), so reconcile attributes
    # both sessions TRACKED and lists both rows. startedAt orders the rows.
    agents = [
        session(_session_child_pid(server), _CHILD_SESSION, 1735689600000),
        session(_bridge_pid(server, "gamma"), _BRIDGE_SESSION, 1735689700000),
    ]
    (server.state_dir.parent / "agents.json").write_text(json.dumps(agents), encoding="utf-8")

    toggle = 'section.zone-active [data-test="bridge-sessions-toggle"]'
    rows = 'section.zone-active [data-test="bridge-session-row"]'
    browser.expect_text(toggle, "2 live sessions", timeout_ms=STATUS_TIMEOUT)
    browser.click(toggle)
    # The expanded list pushes the rows below it down: wait for it before reading.
    browser.expect_count(rows, 2, timeout_ms=STATUS_TIMEOUT)
    browser.expect_visible(f"{rows}:nth-of-type(2)", timeout_ms=STATUS_TIMEOUT)

    # Rows by position (the `<template x-for>` sibling is another element type, so
    # nth-of-type counts the rows only). Pin which session each position holds, so a
    # change to the server's row order fails here rather than swapping the assertions.
    child_chip = f'{rows}:nth-of-type(1) [data-test="bridge-session-metrics"]'
    bridge_chip = f'{rows}:nth-of-type(2) [data-test="bridge-session-metrics"]'
    browser.expect_text(f"{rows}:nth-of-type(1) code", "34200000")
    browser.expect_text(f"{rows}:nth-of-type(2) code", "34300000")
    browser.expect_visible(child_chip, timeout_ms=STATUS_TIMEOUT)
    browser.expect_text(child_chip, "% CPU · ", timeout_ms=STATUS_TIMEOUT)
    assert "part of the bridge figure" in (browser.get_attr(child_chip, "title") or "")
    # Keep this AFTER the child chip's wait: expect_hidden also passes for a chip whose
    # metrics never arrived. Here the same batch that showed the child chip carried this
    # row too, so hidden means the bridge-pid session got no figure.
    browser.expect_hidden(bridge_chip)
    # The bridge row keeps its own total.
    browser.expect_visible('section.zone-active [data-test="metrics-chip"]')

    # At phone width the extra chip must wrap, not push the Open link out of the row
    # (the list clips its overflow, so a clipped link would just vanish). The fixture's
    # idle session reads only a few characters, so the script widens the chip text to a
    # busy session's figure first, and measures in the same task, before Alpine's next
    # metrics poll can repaint it.
    browser.set_viewport(320, 2000)
    try:
        fits = browser.eval_json(
            "(() => { const row = document.querySelector("
            "'section.zone-active [data-test=\"bridge-session-row\"]');"
            " row.querySelector('[data-test=\"bridge-session-metrics\"]').textContent ="
            " '1234% CPU · 12345 MB';"
            " const box = row.getBoundingClientRect();"
            " const sel = 'a, button, [data-test=\"bridge-session-metrics\"]';"
            " return [...row.querySelectorAll(sel)]"
            ".filter((el) => el.offsetParent !== null)"
            ".every((el) => { const r = el.getBoundingClientRect();"
            " return r.left >= box.left - 0.5 && r.right <= box.right + 0.5; }); })()"
        )
        assert fits is True
    finally:
        browser.set_viewport(1280, 2000)
