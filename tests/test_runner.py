from __future__ import annotations

import subprocess
from typing import cast

import pytest

from clauster import bridge_log
from clauster.models import (
    Attribution,
    InstanceStatus,
    RemoteControlInstance,
    WorkingSession,
)
from clauster.runner import NotTrusted, SessionRunner, UnknownProject
from clauster.state import StateStore


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


async def test_stop_signals_graceful_shutdown(runner_config, monkeypatch):
    # The bridge must receive the graceful stop signal (SIGINT on POSIX,
    # CTRL_BREAK on Windows) and log its shutdown marker before exiting — proves
    # stop() is graceful cross-platform, not a hard kill.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    log_path = inst.bridge_debug_log_path
    assert log_path is not None

    await runner.stop("alpha")
    assert "[bridge:shutdown]" in log_path.read_text()


async def test_stop_force_kills_when_signal_ignored(runner_config, monkeypatch):
    # If the bridge never clears the liveness check (ignored the signal, or a
    # wrapper process lingers), _await_exit exhausts its grace loop and force-kills.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    pid = inst.bridge_pid

    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)

    killed: list[int] = []
    from clauster import procutil

    real_force = procutil.force_kill_tree
    monkeypatch.setattr(
        "clauster.runner.procutil.force_kill_tree",
        lambda p: (killed.append(p), real_force(p))[0],
    )

    async def _nosleep(_seconds):
        return None

    monkeypatch.setattr("clauster.runner.asyncio.sleep", _nosleep)

    stopped = await runner.stop("alpha")
    assert stopped.status is InstanceStatus.STOPPED
    assert killed == [pid]  # force-kill fallback fired


async def test_spawn_unresolvable_binary_is_error(runner_config):
    # A claude binary that doesn't resolve must fail the instance to ERROR, not
    # leave it stuck in STARTING or raise out of spawn().
    config, claude_json = runner_config
    config.claude.binary = "definitely-not-a-real-claude-xyz"
    runner = SessionRunner(config, claude_json=claude_json)
    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.ERROR


async def test_spawn_unknown_project_rejected(runner_config):
    runner = _make_runner(runner_config)
    with pytest.raises(UnknownProject):
        await runner.spawn("does-not-exist")


async def test_spawn_is_idempotent_while_running(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    first = await runner.spawn("alpha")
    assert first.status is InstanceStatus.RUNNING
    second = await runner.spawn("alpha")  # already running -> returns the same instance
    assert second is first
    assert runner.running_count() == 1
    await runner.stop("alpha")


async def test_stop_instance_without_pid_marks_stopped(runner_config):
    runner = _make_runner(runner_config)
    runner._instances["alpha"] = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_pid=None,
    )
    inst = await runner.stop("alpha")
    assert inst.status is InstanceStatus.STOPPED and inst.intentional_stop is True


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


def test_apply_markers_status_branches(runner_config):
    """A slow-but-alive bridge stays STARTING (not a false 'Failed to start');
    only a dead proc or a trust rejection is a terminal ERROR."""
    runner = _make_runner(runner_config)

    def proc(alive: bool) -> subprocess.Popen:
        class _Proc:
            def poll(self):
                return None if alive else 0

        return cast(subprocess.Popen, _Proc())

    def fresh():
        return RemoteControlInstance(project="x", label="x", status=InstanceStatus.STARTING)

    ready = bridge_log.BridgeMarkers(poll_loop_started=True, environment_id="env_x")
    assert ready.is_ready

    # ready + alive -> RUNNING
    i = fresh()
    runner._apply_markers(i, ready, proc(alive=True))
    assert i.status is InstanceStatus.RUNNING

    # alive but no ready marker yet -> stays STARTING (slow start, not a failure)
    i = fresh()
    runner._apply_markers(i, bridge_log.BridgeMarkers(), proc(alive=True))
    assert i.status is InstanceStatus.STARTING

    # exited before readiness -> ERROR (genuine, terminal)
    i = fresh()
    runner._apply_markers(i, bridge_log.BridgeMarkers(), proc(alive=False))
    assert i.status is InstanceStatus.ERROR

    # trust rejected even while alive -> ERROR
    i = fresh()
    runner._apply_markers(i, bridge_log.BridgeMarkers(trust_error=True), proc(alive=True))
    assert i.status is InstanceStatus.ERROR


async def test_watch_startup_alive_unregistered_becomes_error(runner_config, monkeypatch):
    """Regression: a bridge that launches but never registers an environment
    (e.g. it can't authenticate to the controller) stays alive yet uncontrollable.
    It must never be reported RUNNING — it stays STARTING, then fails to ERROR."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")  # alive, never registers
    monkeypatch.setattr("clauster.runner._READY_TIMEOUT", 0.2)
    monkeypatch.setattr("clauster.runner._STARTUP_WATCH_INTERVAL", 0.05)
    config, claude_json = runner_config
    config.claude.startup_grace_seconds = 0.3  # tiny grace so the test is fast
    runner = SessionRunner(config, claude_json=claude_json)

    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.STARTING  # NOT a false RUNNING
    assert inst.url is None and inst.environment_id is None
    watch = runner._startup_watches["alpha"]

    await watch
    assert inst.status is InstanceStatus.ERROR  # honest: alive but never usable
    assert inst.url is None and inst.environment_id is None
    assert runner.running_count() == 0

    await runner.stop("alpha")  # clean up the still-idling fake bridge


async def test_watch_startup_promotes_on_late_registration(runner_config, monkeypatch):
    """A genuinely slow bridge that registers *after* the synchronous readiness
    wait is promoted to RUNNING by the watch — but only once it actually has an
    environment, never on liveness alone."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    monkeypatch.setenv("FAKE_CLAUDE_SLOW", "0.5")  # registers ~0.5s in, after the wait
    monkeypatch.setattr("clauster.runner._READY_TIMEOUT", 0.2)
    monkeypatch.setattr("clauster.runner._STARTUP_WATCH_INTERVAL", 0.05)
    config, claude_json = runner_config
    config.claude.startup_grace_seconds = 30
    runner = SessionRunner(config, claude_json=claude_json)

    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.STARTING  # not ready within the 0.2s wait
    assert inst.url is None
    watch = runner._startup_watches["alpha"]

    await watch
    assert inst.status is InstanceStatus.RUNNING
    assert inst.environment_id == "env_01TESTENVAAAAAAAAAAAAAAAA"
    assert inst.url and inst.url.endswith("env_01TESTENVAAAAAAAAAAAAAAAA")

    await runner.stop("alpha")


async def test_watch_startup_marks_crashed_if_bridge_dies(runner_config, monkeypatch):
    """If a STARTING bridge dies before registering, the watch defers to the same
    rule as the poll loop: an unintended same-dir exit is CRASHED."""
    import os
    import signal as _signal

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")
    monkeypatch.setattr("clauster.runner._READY_TIMEOUT", 0.2)
    monkeypatch.setattr("clauster.runner._STARTUP_WATCH_INTERVAL", 0.05)
    config, claude_json = runner_config
    config.claude.startup_grace_seconds = 30  # long; we kill it well before grace
    runner = SessionRunner(config, claude_json=claude_json)

    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.STARTING
    watch = runner._startup_watches["alpha"]

    assert inst.bridge_pid is not None
    os.kill(inst.bridge_pid, _signal.SIGKILL)  # die during startup
    await watch
    assert inst.status is InstanceStatus.CRASHED


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
            pid=pid,
            cwd=root / rel,
            kind="interactive",
            started_at=pid,
            local_uuid=f"uuid-{pid}",
            attribution=attribution,
        )

    runner._sessions = [
        session(111, "alpha", Attribution.EXTERNAL),  # surfaced
        session(222, "alpha", Attribution.EXTERNAL),  # grouped with the first
        session(333, "beta", Attribution.TRACKED),  # managed -> excluded
        session(444, "nope", Attribution.EXTERNAL),  # not a discovered project -> excluded
    ]

    grouped = runner.external_sessions_by_project()
    assert set(grouped) == {"alpha"}
    assert sorted(s.pid for s in grouped["alpha"]) == [111, 222]


def test_external_sessions_empty_when_none(runner_config):
    runner = _make_runner(runner_config)
    assert runner.external_sessions_by_project() == {}


async def test_rediscover_overlays_persisted_state(runner_config, monkeypatch):
    config, claude_json = runner_config
    # alpha was intentionally stopped with a custom label; zeta is stale/persisted.
    StateStore(config.state_dir).save(
        {
            "alpha": {
                "label": "my-alpha",
                "intentional_stop": True,
                "spawn_mode": "same-dir",
            },
            "zeta": {
                "label": "zeta",
                "intentional_stop": True,
                "spawn_mode": "same-dir",
            },
        }
    )
    runner = SessionRunner(config, claude_json=claude_json)

    class FakePtr:
        pid, proc_start, environment_id, session_id = 4242, "1000", "env_x", "session_x"

    # Only alpha has a live bridge; beta/gamma/zeta resolve to no pointer.
    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.pointers.is_live", lambda ptr: True)
    monkeypatch.setattr("clauster.procutil.jiffies_to_epoch", lambda j: 12345.0)

    await runner.rediscover()
    insts = {i.project: i for i in runner.list_instances()}

    assert set(insts) == {"alpha"}  # no phantom from a persisted-but-dead entry
    assert insts["alpha"].label == "my-alpha"  # persisted label overlaid
    assert insts["alpha"].intentional_stop is False  # a live bridge is not "stopped"


async def test_rediscover_tolerates_invalid_persisted_mode(runner_config, monkeypatch):
    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {
            "alpha": {
                "label": "alpha",
                "intentional_stop": True,
                "spawn_mode": "BOGUS",
                "permission_mode": "NOPE",
            },
        }
    )
    runner = SessionRunner(config, claude_json=claude_json)

    class FakePtr:
        pid, proc_start, environment_id, session_id = 4242, "1000", "env_x", "session_x"

    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.pointers.is_live", lambda ptr: True)
    monkeypatch.setattr("clauster.procutil.jiffies_to_epoch", lambda j: 12345.0)

    await runner.rediscover()
    inst = runner.get_instance("alpha")
    assert inst is not None  # didn't crash on the bad persisted modes
    assert inst.spawn_mode == "same-dir" and inst.permission_mode == "default"  # fell back


async def test_rediscover_tolerates_unparseable_proc_start(runner_config, monkeypatch):
    # A garbled/future-format pointer procStart must not crash startup with a bare
    # int() ValueError; it degrades to bridge_proc_start=None (cmdline-only
    # liveness), mirroring procutil.is_live_bridge so the two paths can't disagree.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)

    class FakePtr:
        pid, proc_start, environment_id, session_id = 4242, "not-a-number", "env_x", "session_x"

    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.pointers.is_live", lambda ptr: True)

    await runner.rediscover()  # must not raise
    inst = runner.get_instance("alpha")
    assert inst is not None  # rediscovered despite the unparseable procStart
    assert inst.bridge_proc_start is None  # degraded, not crashed


def test_reconcile_status_transitions():
    def inst(status, intentional=False):
        return RemoteControlInstance(
            project="x", label="x", status=status, intentional_stop=intentional
        )

    i = inst(InstanceStatus.RUNNING, intentional=True)
    SessionRunner._reconcile_status(i, alive=False)
    assert i.status is InstanceStatus.STOPPED

    i = inst(InstanceStatus.RUNNING, intentional=False)
    SessionRunner._reconcile_status(i, alive=False)
    assert i.status is InstanceStatus.CRASHED

    i = inst(InstanceStatus.STARTING)
    SessionRunner._reconcile_status(i, alive=True)
    # Liveness alone must NOT promote: a bridge can be alive yet never have
    # registered an environment (then it is unusable). Promotion is the
    # startup-watch's job, gated on a real environment registration.
    assert i.status is InstanceStatus.STARTING

    i = inst(InstanceStatus.STARTING, intentional=False)
    SessionRunner._reconcile_status(i, alive=False)
    assert i.status is InstanceStatus.CRASHED  # died during startup

    i = inst(InstanceStatus.RUNNING)
    SessionRunner._reconcile_status(i, alive=True)
    assert i.status is InstanceStatus.RUNNING  # unchanged
