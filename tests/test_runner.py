from __future__ import annotations

import json
from pathlib import Path

import pytest

from clauster.models import Attribution, InstanceStatus, RemoteControlInstance, WorkingSession
from clauster.runner import NotTrusted, SessionRunner, UnknownProject


def _make_runner(runner_config) -> SessionRunner:
    config, claude_json = runner_config
    return SessionRunner(config, claude_json=claude_json)


async def test_spawn_ready_then_stop(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)

    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.RUNNING
    assert inst.environment_id == "env_01TESTENVAAAAAAAAAAAAAAAA"
    assert inst.starter_session_id == "session_01TESTSTARTERAAAAAAAAAA"
    assert inst.bridge_id == "11111111-2222-3333-4444-555555555555"
    assert inst.url and inst.url.endswith("env_01TESTENVAAAAAAAAAAAAAAAA")
    assert runner.running_count() == 1

    stopped = await runner.stop("alpha")
    assert stopped.status is InstanceStatus.STOPPED
    assert stopped.intentional_stop is True
    assert runner.running_count() == 0


async def test_spawn_unknown_project_rejected(runner_config):
    runner = _make_runner(runner_config)
    with pytest.raises(UnknownProject):
        await runner.spawn("does-not-exist")


async def test_spawn_path_traversal_rejected(runner_config):
    runner = _make_runner(runner_config)
    # Invalid names never reach Popen (spec §9 path-traversal defense).
    for evil in ("../etc", "a/b", "..", "foo bar"):
        with pytest.raises(UnknownProject):
            await runner.spawn(evil)


async def test_spawn_untrusted_refused(runner_config, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, _ = runner_config
    empty_trust = tmp_path / "untrusted.json"
    empty_trust.write_text("{}")
    runner = SessionRunner(config, claude_json=empty_trust)
    with pytest.raises(NotTrusted):
        await runner.spawn("alpha")


async def test_spawn_crash_is_error(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.ERROR


async def test_spawn_no_poll_loop_is_error(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_marker")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.ERROR
    # markers before the poll loop are still captured
    assert inst.environment_id == "env_01TESTENVAAAAAAAAAAAAAAAA"


async def test_spawn_trust_error_is_error(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "trust_error")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.ERROR


async def test_stop_unknown_instance_raises(runner_config):
    runner = _make_runner(runner_config)
    with pytest.raises(UnknownProject):
        await runner.stop("alpha")  # never spawned


def test_external_sessions_by_project(runner_config):
    config, claude_json = runner_config
    runner = _make_runner(runner_config)
    root = config.projects_root

    def session(pid, rel, attribution):
        return WorkingSession(
            pid=pid, cwd=root / rel, kind="interactive",
            started_at=pid, local_uuid=f"uuid-{pid}", attribution=attribution,
        )

    runner._sessions = [
        session(111, "alpha", Attribution.EXTERNAL),   # surfaced
        session(222, "alpha", Attribution.EXTERNAL),   # grouped with the first
        session(333, "beta", Attribution.TRACKED),     # managed -> excluded
        session(444, "nope", Attribution.EXTERNAL),    # not a discovered project -> excluded
    ]

    grouped = runner.external_sessions_by_project()
    assert set(grouped) == {"alpha"}
    assert sorted(s.pid for s in grouped["alpha"]) == [111, 222]


def test_external_sessions_empty_when_none(runner_config):
    runner = _make_runner(runner_config)
    assert runner.external_sessions_by_project() == {}


def test_reconcile_status_transitions():
    def inst(status, intentional=False):
        return RemoteControlInstance(project="x", label="x", status=status, intentional_stop=intentional)

    i = inst(InstanceStatus.RUNNING, intentional=True)
    SessionRunner._reconcile_status(i, alive=False)
    assert i.status is InstanceStatus.STOPPED

    i = inst(InstanceStatus.RUNNING, intentional=False)
    SessionRunner._reconcile_status(i, alive=False)
    assert i.status is InstanceStatus.CRASHED

    i = inst(InstanceStatus.ERROR)
    SessionRunner._reconcile_status(i, alive=True)
    assert i.status is InstanceStatus.RUNNING  # slow-start recovery

    i = inst(InstanceStatus.RUNNING)
    SessionRunner._reconcile_status(i, alive=True)
    assert i.status is InstanceStatus.RUNNING  # unchanged
