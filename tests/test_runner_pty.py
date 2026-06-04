"""Runner wiring for pty / true-resume mode (`claude.resume_mode: pty`)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from clauster import procutil
from clauster.config import ClausterConfig
from clauster.models import InstanceStatus
from clauster.runner import SessionRunner

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="pty mode is POSIX-only")


def _pty_runner(runner_config) -> tuple[SessionRunner, Path]:
    config, claude_json = runner_config
    pty_config = ClausterConfig(
        projects_root=config.projects_root,
        state_dir=config.state_dir,
        claude={"binary": config.claude.binary, "resume_mode": "pty"},
    )
    return SessionRunner(pty_config, claude_json=claude_json), claude_json


# ----- pure unit: argv + status + signalling --------------------------------


def test_build_pty_bridge_argv_uses_flag_form(runner_config) -> None:
    runner, _ = _pty_runner(runner_config)
    argv = runner._build_pty_bridge_argv(Path("/tmp/x.log"), "alpha", "default", resume=False)
    assert argv[1] == "--remote-control"  # flag form, not the `remote-control` subcommand
    assert "alpha" in argv
    assert "--permission-mode" in argv and "default" in argv
    assert "--spawn" not in argv  # single-session: no spawn/capacity
    assert "--continue" not in argv


def test_build_pty_bridge_argv_resume_adds_continue(runner_config) -> None:
    runner, _ = _pty_runner(runner_config)
    argv = runner._build_pty_bridge_argv(Path("/tmp/x.log"), "alpha", "plan", resume=True)
    assert "--continue" in argv  # this is what restores prior context on restart


def test_is_pty_mode_gated_on_config_and_platform(runner_config) -> None:
    pty_runner, _ = _pty_runner(runner_config)
    std_runner = SessionRunner(runner_config[0], claude_json=runner_config[1])
    assert std_runner._is_pty_mode() is False
    assert pty_runner._is_pty_mode() is (sys.platform != "win32")


def test_is_pty_mode_honors_prior_instance_over_config(runner_config) -> None:
    """A resume follows the bridge's *recorded* mode, not the live config.

    The config only seeds brand-new bridges; once a bridge has launched its mode
    is fixed on the instance, so stop() and resume() can never disagree about the
    same bridge after the config is edited underneath it.
    """
    from clauster.models import RemoteControlInstance

    std_runner = SessionRunner(runner_config[0], claude_json=runner_config[1])
    pty_runner, _ = _pty_runner(runner_config)
    pty_inst = RemoteControlInstance(project="a", label="a", resume_mode="pty")
    std_inst = RemoteControlInstance(project="b", label="b", resume_mode="standard")

    # config=standard but the bridge was launched pty -> resume stays pty (POSIX)
    assert std_runner._is_pty_mode(pty_inst) is (sys.platform != "win32")
    # config=pty but the bridge was launched standard -> resume stays standard
    assert pty_runner._is_pty_mode(std_inst) is False
    # no prior (a brand-new spawn) still follows the config default
    assert std_runner._is_pty_mode() is False
    assert pty_runner._is_pty_mode() is (sys.platform != "win32")


def test_is_pty_mode_explicit_request_wins(runner_config) -> None:
    """The per-launch picker (an explicit requested mode) overrides config and prior."""
    from clauster.models import RemoteControlInstance

    std_runner = SessionRunner(runner_config[0], claude_json=runner_config[1])
    pty_runner, _ = _pty_runner(runner_config)
    prior_std = RemoteControlInstance(project="a", label="a", resume_mode="standard")

    # explicit pty wins over a standard config (POSIX)
    assert std_runner._is_pty_mode(requested="pty") is (sys.platform != "win32")
    # explicit standard wins over a pty config
    assert pty_runner._is_pty_mode(requested="standard") is False
    # an explicit request also wins over a prior instance's recorded mode
    assert std_runner._is_pty_mode(prior_std, requested="pty") is (sys.platform != "win32")


def test_is_pty_mode_false_on_windows(runner_config, monkeypatch) -> None:
    """pty is POSIX-only: on Windows the guard returns False no matter what —
    config, an explicit request, or a prior pty instance all fall back to standard.
    """
    from clauster.models import RemoteControlInstance

    monkeypatch.setattr("clauster.runner.sys.platform", "win32")
    pty_runner, _ = _pty_runner(runner_config)  # config resume_mode == "pty"
    prior_pty = RemoteControlInstance(project="a", label="a", resume_mode="pty")

    assert pty_runner._is_pty_mode() is False  # config says pty, Windows overrides
    assert pty_runner._is_pty_mode(requested="pty") is False  # explicit pty too
    assert pty_runner._is_pty_mode(prior_pty) is False  # a recorded pty bridge too


async def test_spawn_rejects_invalid_resume_mode(runner_config) -> None:
    """A bad per-launch resume_mode is rejected before launch (no fake claude needed)."""
    from clauster.runner import InvalidSpawnOption

    runner = SessionRunner(runner_config[0], claude_json=runner_config[1])
    with pytest.raises(InvalidSpawnOption):
        await runner.spawn("alpha", resume_mode="bogus")


@_POSIX_ONLY
async def test_spawn_explicit_pty_overrides_standard_config(runner_config) -> None:
    """The picker can choose pty even when the config default is standard."""
    runner = SessionRunner(runner_config[0], claude_json=runner_config[1])  # config=standard
    inst = await runner.spawn("alpha", resume_mode="pty")
    try:
        assert inst.resume_mode == "pty"
        assert inst.status is InstanceStatus.RUNNING
        assert inst.url is not None and "/code/session_" in inst.url
        assert isinstance(inst.keeper_pid, int)
    finally:
        await runner.stop("alpha")


class _FakeProc:
    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def poll(self):  # noqa: ANN201 — mimic subprocess.Popen.poll
        return None if self._alive else 0


def test_apply_pty_info_status_transitions(runner_config) -> None:
    runner, _ = _pty_runner(runner_config)
    from clauster.models import RemoteControlInstance

    # connect_url + keeper alive -> RUNNING, and the deep link fields are set.
    inst = RemoteControlInstance(project="alpha", label="alpha")
    runner._apply_pty_info(
        inst,
        {
            "bridge_pid": 4321,
            "bridge_proc_start": 123.0,
            "connect_url": "https://claude.ai/code/session_01ABC",
            "session_id": "session_01ABC",
            "state": "ready",
        },
        _FakeProc(alive=True),
    )
    assert inst.status is InstanceStatus.RUNNING
    assert inst.bridge_pid == 4321
    assert inst.bridge_proc_start == 123.0
    assert inst.url == "https://claude.ai/code/session_01ABC"
    assert inst.starter_session_id == "session_01ABC"

    # keeper dead before a url -> ERROR
    err = RemoteControlInstance(project="beta", label="beta")
    runner._apply_pty_info(err, {"state": "error", "error": "boom"}, _FakeProc(alive=False))
    assert err.status is InstanceStatus.ERROR

    # alive, not yet registered -> STARTING (startup-watch will promote)
    starting = RemoteControlInstance(project="gamma", label="gamma")
    runner._apply_pty_info(starting, {"bridge_pid": 9, "state": "starting"}, _FakeProc(alive=True))
    assert starting.status is InstanceStatus.STARTING


def test_signal_stop_twice_sends_two_signals(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("clauster.runner.os.kill", lambda pid, sig: calls.append(pid))
    monkeypatch.setattr("clauster.runner.time.sleep", lambda _s: None)

    SessionRunner._signal_stop(1234, twice=False)
    assert calls == [1234]
    calls.clear()
    SessionRunner._signal_stop(1234, twice=True)
    assert calls == [1234, 1234]  # the flag-form TUI needs the confirming second press


# ----- integration: real keeper + fake claude flag form ---------------------


@_POSIX_ONLY
async def test_spawn_pty_reaches_running_then_stops(runner_config) -> None:
    runner, _ = _pty_runner(runner_config)
    inst = await runner.spawn("alpha")
    try:
        assert inst.resume_mode == "pty"
        assert inst.status is InstanceStatus.RUNNING
        assert inst.url is not None and "/code/session_" in inst.url
        assert isinstance(inst.bridge_pid, int)
        assert isinstance(inst.keeper_pid, int)
        assert inst.bridge_pid != inst.keeper_pid  # bridge is the keeper's child
        # the keeper-tracked bridge passes the same liveness check as a subcommand bridge
        assert procutil.is_live_bridge(inst.bridge_pid, inst.bridge_proc_start) is True
    finally:
        stopped = await runner.stop("alpha")
    assert stopped.status is InstanceStatus.STOPPED
    # both the bridge and its keeper are gone after stop
    assert procutil.proc_create_time(inst.bridge_pid) is None
    assert procutil.proc_create_time(inst.keeper_pid) is None


@_POSIX_ONLY
async def test_spawn_pty_no_url_does_not_falsely_run(runner_config, monkeypatch) -> None:
    """A flag-form bridge that never prints a URL must not be reported RUNNING."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "pty_no_url")
    runner, _ = _pty_runner(runner_config)
    inst = await runner.spawn("alpha")
    try:
        assert inst.status is not InstanceStatus.RUNNING  # STARTING (watch will ERROR it)
    finally:
        await runner.stop("alpha")


@_POSIX_ONLY
async def test_spawn_pty_promoted_by_startup_watch(runner_config, monkeypatch) -> None:
    """A pty bridge slow to print its URL is promoted to RUNNING by the startup-watch."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "pty_slow")
    monkeypatch.setattr("clauster.runner._READY_TIMEOUT", 0.3)
    monkeypatch.setattr("clauster.runner._STARTUP_WATCH_INTERVAL", 0.25)
    runner, _ = _pty_runner(runner_config)
    inst = await runner.spawn("alpha")
    try:
        assert inst.status is InstanceStatus.STARTING  # too slow for the synchronous wait
        for _ in range(50):  # the background watch reads the sidecar and promotes it
            if inst.status is InstanceStatus.RUNNING:
                break
            await asyncio.sleep(0.1)
        assert inst.status is InstanceStatus.RUNNING
        assert inst.url is not None and "/code/session_" in inst.url
    finally:
        await runner.stop("alpha")


async def test_stop_cleans_keeper_when_bridge_pid_absent(runner_config, monkeypatch) -> None:
    """Stopping a pty bridge whose bridge pid is already gone still reaps the keeper."""
    from clauster.models import RemoteControlInstance

    runner, _ = _pty_runner(runner_config)
    inst = RemoteControlInstance(
        project="alpha",
        label="alpha",
        resume_mode="pty",
        keeper_pid=4242,
        bridge_pid=None,
        status=InstanceStatus.RUNNING,
    )
    runner._instances["alpha"] = inst
    cleaned: list[int] = []
    monkeypatch.setattr(runner, "_cleanup_keeper", lambda pid: cleaned.append(pid))

    stopped = await runner.stop("alpha")
    assert cleaned == [4242]
    assert stopped.status is InstanceStatus.STOPPED


async def test_rediscover_restores_persisted_resume_mode(runner_config, monkeypatch) -> None:
    """A pty bridge rediscovered after a restart keeps its persisted resume_mode."""
    from clauster.state import StateStore

    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {"alpha": {"label": "alpha", "intentional_stop": True, "resume_mode": "pty"}}
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
    assert runner.get_instance("alpha").resume_mode == "pty"


async def test_rediscover_recovers_keeper_pid_from_sidecar(runner_config, monkeypatch) -> None:
    """A rediscovered pty bridge recovers its keeper pid from the matching sidecar.

    The log path is timestamped (not derivable after a restart), so the sidecar is
    located by globbing the log dir and matching ``bridge_pid``. Without this, the
    keeper would be unmanaged and leak on stop.
    """
    from clauster.state import StateStore

    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {"alpha": {"label": "alpha", "intentional_stop": True, "resume_mode": "pty"}}
    )
    # A sidecar the keeper would have written, naming the live bridge pid (4242)
    # and its proc-start (12345.0 — what the patched jiffies_to_epoch below yields
    # for the pointer's "1000").
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "alpha-1700000000000.keeper.json").write_text(
        json.dumps({"keeper_pid": 9999, "bridge_pid": 4242, "bridge_proc_start": 12345.0})
    )
    # A stale sidecar that RECYCLED the same pid but has a different proc-start must
    # be rejected (PID-reuse defense), even though its bridge_pid matches.
    (log_dir / "alpha-1699999999999.keeper.json").write_text(
        json.dumps({"keeper_pid": 1111, "bridge_pid": 4242, "bridge_proc_start": 88888.0})
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
    assert inst.resume_mode == "pty"
    assert inst.keeper_pid == 9999


def test_cleanup_keeper_forces_a_lingering_keeper(runner_config, monkeypatch) -> None:
    """If the keeper outlives its bridge, _cleanup_keeper force-kills then reaps it."""
    runner, _ = _pty_runner(runner_config)
    monkeypatch.setattr("clauster.runner.time.sleep", lambda _s: None)
    monkeypatch.setattr(procutil, "reap_if_exited", lambda pid: None)
    monkeypatch.setattr(procutil, "proc_create_time", lambda pid: 1.0)  # always "alive"
    forced: list[int] = []
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: forced.append(pid))

    runner._cleanup_keeper(777)
    assert forced == [777]
