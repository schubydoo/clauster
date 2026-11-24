"""A session that outlives its crashed standard bridge belongs to the crashed row (TASK-562)."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from clauster import inspector, mcp_server, procutil
from clauster.app import create_app
from clauster.models import Attribution, InstanceStatus, RemoteControlInstance, WorkingSession
from clauster.routes import projects as project_routes
from clauster.runner import SessionRunner

SESSION_ID = "cse_01VxAGVrNRB2vvCQwQziTr4a"
LOG_DIR = Path("/logs")
BRIDGE_LOG = LOG_DIR / "alpha-1795127464176-1.log"
CHILD_LOG = LOG_DIR / f"alpha-1795127464176-1-{SESSION_ID}.log"


def _session(pid=500, cwd="/projects/alpha", attribution=Attribution.EXTERNAL, parent=None):
    return WorkingSession(
        pid=pid,
        cwd=Path(cwd),
        kind="interactive",
        started_at=1795127464176,
        local_uuid=f"uuid-{pid}",
        attribution=attribution,
        parent_instance=parent,
    )


@pytest.fixture
def children(monkeypatch):
    """Serve fake ``(session id, debug file)`` reads, with no live bridge above any pid."""
    table: dict[int, tuple[str, Path] | None] = {500: (SESSION_ID, CHILD_LOG)}
    monkeypatch.setattr(procutil, "session_child_debug_file", table.get)
    monkeypatch.setattr(procutil, "bridge_ancestor", lambda pid: None)
    return table


# ----- reading the child's argv ------------------------------------------------------------


class _Proc:
    def __init__(self, argv):
        self._argv = argv

    def cmdline(self):
        return self._argv


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["claude", "--sdk-url", "u", "--session-id", "s", "--debug-file", "/l/a.log"],
            ("s", Path("/l/a.log")),
        ),
        # A hand-run session with a debug file is not a bridge child.
        (["claude", "--session-id", "s", "--debug-file", "/l/a.log"], None),
        (["claude", "--sdk-url", "u", "--debug-file", "/l/a.log"], None),
        (["claude", "--sdk-url", "u", "--session-id", "s"], None),
        (["claude", "--sdk-url", "u", "--session-id", "s", "--debug-file"], None),
        (["claude", "--sdk-url", "u", "--session-id", "", "--debug-file", "/l/a.log"], None),
    ],
)
def test_session_child_debug_file_reads_only_a_complete_child_argv(monkeypatch, argv, expected):
    monkeypatch.setattr(procutil.psutil, "Process", lambda pid: _Proc(argv))
    assert procutil.session_child_debug_file(500) == expected


@pytest.mark.parametrize(
    "error", [psutil.NoSuchProcess(1), psutil.AccessDenied(1), psutil.ZombieProcess(1), OSError()]
)
def test_session_child_debug_file_fails_closed_on_an_unreadable_process(monkeypatch, error):
    def _raise(pid):
        raise error

    monkeypatch.setattr(procutil.psutil, "Process", _raise)
    assert procutil.session_child_debug_file(500) is None


# ----- the link: the debug-file name, nothing else ------------------------------------------


def test_link_uses_the_debug_file_stem(children):
    assert inspector.link_orphans([_session()], {"row": BRIDGE_LOG}) == {500: "row"}


def test_link_uses_the_private_log_when_redaction_splits_it(children):
    raw = LOG_DIR / "alpha-1795127464176-1.raw.log"
    children[500] = (SESSION_ID, LOG_DIR / f"alpha-1795127464176-1.raw-{SESSION_ID}.log")
    assert inspector.link_orphans([_session()], {"row": raw}) == {500: "row"}


@pytest.mark.parametrize(
    "child_log",
    [
        # Next spawn counter, which a prefix match would accept.
        LOG_DIR / f"alpha-1795127464176-10-{SESSION_ID}.log",
        # A sibling project whose name extends this bridge's stem.
        LOG_DIR / f"alpha-1795127464176-1-2-{SESSION_ID}.log",
        # Same name in another directory.
        Path("/elsewhere") / CHILD_LOG.name,
        # A different session id than the child's own --session-id.
        LOG_DIR / "alpha-1795127464176-1-cse_other.log",
        # The bridge's own file, with no session id.
        BRIDGE_LOG,
        # An extension the bridge never writes.
        LOG_DIR / f"alpha-1795127464176-1-{SESSION_ID}.txt",
        # Degenerate names from an argv: no stem left, and no extension.
        LOG_DIR / f"-{SESSION_ID}",
        Path(f"-{SESSION_ID}"),
    ],
)
def test_no_link_when_the_stem_differs(children, child_log):
    children[500] = (SESSION_ID, child_log)
    assert inspector.link_orphans([_session()], {"row": BRIDGE_LOG}) == {}


def test_no_link_from_cwd_alone(children):
    # A hand-run `claude` in the crashed bridge's own folder.
    children[500] = None
    assert inspector.link_orphans([_session()], {"row": BRIDGE_LOG}) == {}


def test_no_link_when_two_rows_claim_one_log(children):
    assert inspector.link_orphans([_session()], {"a": BRIDGE_LOG, "b": BRIDGE_LOG}) == {}


def test_no_link_while_a_live_bridge_is_above_the_session(children, monkeypatch):
    monkeypatch.setattr(procutil, "bridge_ancestor", lambda pid: 77)
    assert inspector.link_orphans([_session()], {"row": BRIDGE_LOG}) == {}


def test_link_takes_a_session_a_worktree_row_claimed_by_containment(children):
    session = _session(attribution=Attribution.TRACKED, parent="row")
    assert inspector.link_orphans([session], {"row": BRIDGE_LOG}) == {500: "row"}


@pytest.mark.parametrize(
    ("attribution", "parent"),
    [
        (Attribution.TRACKED, "live-sibling"),
        (Attribution.HOSTED, "hosted"),
        (Attribution.UNTRACKED, None),
    ],
)
def test_link_leaves_other_attributions_alone(children, attribution, parent):
    session = _session(attribution=attribution, parent=parent)
    assert inspector.link_orphans([session], {"row": BRIDGE_LOG}) == {}


# ----- the poll, the runner views and the route ---------------------------------------------


def _runner(runner_config, monkeypatch, working, *, status=InstanceStatus.RUNNING, log=BRIDGE_LOG):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=status)
    inst.bridge_pid = 4242
    inst.bridge_debug_log_path = log
    inst.bridge_raw_log_path = log
    runner._instances[inst.instance_id] = inst
    monkeypatch.setattr("clauster.runner.procutil.reap_if_exited", lambda *a, **k: None)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    monkeypatch.setattr("clauster.poll_loop.inspector.list_working_sessions", working)
    monkeypatch.setattr(runner._record, "_emit_lifecycle", lambda *a, **k: None)
    return runner, inst


def _alpha_session(runner):
    return _session(cwd=str(runner._discovered()["alpha"].path))


async def test_crashed_bridge_with_a_surviving_child(runner_config, monkeypatch, children):
    holder = {}
    runner, inst = _runner(runner_config, monkeypatch, lambda *a, **k: [holder["s"]])
    holder["s"] = _alpha_session(runner)

    await runner.poll_once()

    assert inst.status is InstanceStatus.CRASHED
    assert holder["s"].attribution is Attribution.ORPHANED
    assert holder["s"].parent_instance == inst.instance_id
    assert runner.orphaned_sessions_by_instance() == {inst.instance_id: [holder["s"]]}
    assert runner.external_sessions_by_project() == {}
    assert runner.tracked_sessions_by_instance() == {}
    assert runner.has_orphaned_session("alpha") is True
    assert project_routes._bridge_running(runner, "alpha") is True


async def test_crashed_bridge_with_no_surviving_child(runner_config, monkeypatch, children):
    runner, inst = _runner(runner_config, monkeypatch, lambda *a, **k: [])

    await runner.poll_once()

    assert inst.status is InstanceStatus.CRASHED
    assert runner.orphaned_sessions_by_instance() == {}
    assert runner.has_orphaned_session("alpha") is False
    assert project_routes._bridge_running(runner, "alpha") is False


async def test_hand_run_session_beside_a_crashed_bridge_stays_external(
    runner_config, monkeypatch, children
):
    holder = {}
    runner, _inst = _runner(runner_config, monkeypatch, lambda *a, **k: [holder["s"]])
    holder["s"] = _alpha_session(runner)
    children[500] = None

    await runner.poll_once()

    assert holder["s"].attribution is Attribution.EXTERNAL
    assert runner.orphaned_sessions_by_instance() == {}
    assert list(runner.external_sessions_by_project()) == ["alpha"]


@pytest.mark.parametrize(
    "change",
    [
        lambda inst: setattr(inst, "resume_mode", "pty"),
        lambda inst: setattr(inst, "intentional_stop", True),
        lambda inst: (
            setattr(inst, "bridge_debug_log_path", None),
            setattr(inst, "bridge_raw_log_path", None),
        ),
    ],
    ids=["pty-row", "deliberate-stop", "no-recorded-log"],
)
async def test_no_link_without_a_crashed_standard_row_and_its_log(
    runner_config, monkeypatch, children, change
):
    holder = {}
    runner, inst = _runner(runner_config, monkeypatch, lambda *a, **k: [holder["s"]])
    holder["s"] = _alpha_session(runner)
    change(inst)

    await runner.poll_once()

    assert holder["s"].attribution is Attribution.EXTERNAL
    assert list(runner.external_sessions_by_project()) == ["alpha"]


async def test_link_is_dropped_when_a_resume_lands_during_the_read(
    runner_config, monkeypatch, children
):
    holder = {}
    runner, inst = _runner(runner_config, monkeypatch, lambda *a, **k: [holder["s"]])
    holder["s"] = _alpha_session(runner)
    real = inspector.link_orphans

    def _resume_meanwhile(sessions, logs):
        links = real(sessions, logs)
        inst.status = InstanceStatus.STARTING
        return links

    monkeypatch.setattr("clauster.poll_loop.inspector.link_orphans", _resume_meanwhile)

    await runner.poll_once()

    assert holder["s"].attribution is Attribution.EXTERNAL


async def test_row_that_left_crashed_shows_the_old_process_as_external_again(
    runner_config, monkeypatch, children
):
    holder = {}
    runner, inst = _runner(runner_config, monkeypatch, lambda *a, **k: [holder["s"]])
    holder["s"] = _alpha_session(runner)
    await runner.poll_once()
    assert runner.external_sessions_by_project() == {}

    inst.status = InstanceStatus.STARTING  # a Resume, before the next poll

    assert runner.orphaned_sessions_by_instance() == {}
    assert list(runner.external_sessions_by_project()) == ["alpha"]


async def test_failed_cross_check_withdraws_the_still_running_claim(
    runner_config, monkeypatch, children
):
    holder = {}
    runner, _inst = _runner(runner_config, monkeypatch, lambda *a, **k: [holder["s"]])
    holder["s"] = _alpha_session(runner)
    await runner.poll_once()
    assert holder["s"].attribution is Attribution.ORPHANED

    def _fail(*a, **k):
        raise subprocess.SubprocessError("agents --json failed")

    monkeypatch.setattr("clauster.poll_loop.inspector.list_working_sessions", _fail)
    await runner.poll_once()

    assert holder["s"].attribution is Attribution.EXTERNAL
    assert holder["s"].parent_instance is None
    assert runner.orphaned_sessions_by_instance() == {}


def test_clear_orphan_links_touches_only_orphaned_sessions():
    orphan = _session(pid=1, attribution=Attribution.ORPHANED, parent="row")
    tracked = _session(pid=2, attribution=Attribution.TRACKED, parent="live")
    inspector.clear_orphan_links([orphan, tracked])
    assert (orphan.attribution, orphan.parent_instance) == (Attribution.EXTERNAL, None)
    assert (tracked.attribution, tracked.parent_instance) == (Attribution.TRACKED, "live")


async def test_poll_never_signals_the_surviving_process(runner_config, monkeypatch, children):
    holder = {}
    runner, _inst = _runner(runner_config, monkeypatch, lambda *a, **k: [holder["s"]])
    holder["s"] = _alpha_session(runner)
    signals = []
    monkeypatch.setattr(procutil.os, "kill", lambda *a: signals.append(a))
    monkeypatch.setattr(psutil.Process, "send_signal", lambda *a: signals.append(a))
    monkeypatch.setattr(psutil.Process, "terminate", lambda *a: signals.append(a))
    monkeypatch.setattr(psutil.Process, "kill", lambda *a: signals.append(a))

    await runner.poll_once()

    assert holder["s"].attribution is Attribution.ORPHANED
    assert signals == []


def test_orphaned_route_returns_the_sessions_by_row(runner_config, monkeypatch):
    config, _claude_json = runner_config
    app = create_app(config)
    runner = app.state.runner
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.CRASHED)
    runner._instances[inst.instance_id] = inst
    runner._sessions = [
        _session(attribution=Attribution.ORPHANED, parent=inst.instance_id),
    ]
    body = TestClient(app).get("/api/sessions/orphaned").json()
    assert [s["pid"] for s in body[inst.instance_id]] == [500]
    assert body[inst.instance_id][0]["attribution"] == "orphaned"


def test_dashboard_carries_the_crashed_row_copy(runner_config):
    config, _claude_json = runner_config
    page = TestClient(create_app(config)).get("/").text
    assert 'data-test="bridge-survivor-note"' in page
    assert "/api/sessions/orphaned" in page
    assert "Its session is still running" in page
    assert "Resume reattaches a session in a new process." in page
    assert "shell commands and subagents in the old process end" in page
    # No "Resume to continue" beside a warning that Resume restarts the running session.
    assert '(this.survivingSessions(name).length ? "." : tail)' in page


def test_mcp_lists_a_surviving_session_once_under_its_row(runner_config, monkeypatch):
    config, _claude_json = runner_config
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.CRASHED)
    orphan = _session(attribution=Attribution.ORPHANED, parent=inst.instance_id)

    async def _rediscover(self, **kwargs):
        self._instances[inst.instance_id] = inst
        self._sessions = [orphan]

    async def _poll(self, **kwargs):
        return None

    monkeypatch.setattr(SessionRunner, "rediscover", _rediscover)
    monkeypatch.setattr(SessionRunner, "poll_once", _poll)
    monkeypatch.setattr("clauster.supervisor.list_background_jobs", lambda *a, **k: [])

    sessions = asyncio.run(mcp_server.gather_sessions(config))

    found = [s for s in sessions if s["id"] == "uuid-500"]
    assert [(s["kind"], s["project"]) for s in found] == [("orphaned-session", "alpha")]
    assert found[0]["parent_instance"] == inst.instance_id
