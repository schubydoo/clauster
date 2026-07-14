"""Runner wiring for pty / true-resume mode (`claude.launch_mode: pty`)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path

import pytest

from clauster import procutil
from clauster.config import ClausterConfig
from clauster.db.persistence import Persistence
from clauster.models import InstanceStatus
from clauster.runner import SessionRunner

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="pty mode is POSIX-only")

# Fixed instance_id UUID for seeding StateStore (keyed by instance_id since #777).


@contextlib.contextmanager
def _db_persistence(state_dir):
    """Yield a ``Persistence`` on ``state_dir``, disposing its engine on exit."""
    state_dir.mkdir(parents=True, exist_ok=True)
    persistence = Persistence(state_dir)
    try:
        yield persistence
    finally:
        persistence.dispose()


def _db_save(state_dir, records):
    """Seed ``records`` into a DB-backed StateStore on ``state_dir``, then dispose."""
    with _db_persistence(state_dir) as persistence:
        persistence.state_store().save(records)


def _pty_runner(runner_config) -> tuple[SessionRunner, Path]:
    config, claude_json = runner_config
    pty_config = ClausterConfig(
        projects_root=config.projects_root,
        state_dir=config.state_dir,
        claude={"binary": config.claude.binary, "launch_mode": "pty"},
    )
    return SessionRunner(pty_config, claude_json=claude_json), claude_json


# ----- live-screen tap wiring (#534) ----------------------------------------


def test_keeper_launch_cmd_includes_screen_sidecar_only_when_given() -> None:
    """The `--screen-sidecar` flag is passed to the keeper only when a path is supplied (#534)."""
    off = SessionRunner._keeper_launch_cmd(Path("/s.json"), Path("/cwd"), ["claude", "x"])
    assert "--screen-sidecar" not in off
    on = SessionRunner._keeper_launch_cmd(
        Path("/s.json"), Path("/cwd"), ["claude", "x"], Path("/s.screen.json")
    )
    # str(Path(...)) so the expected value is OS-portable (backslash path on Windows CI).
    assert on[on.index("--screen-sidecar") + 1] == str(Path("/s.screen.json"))
    # the bridge argv stays intact after the `--` separator in both cases
    assert on[on.index("--") + 1 :] == ["claude", "x"]
    assert off[off.index("--") + 1 :] == ["claude", "x"]


def test_keeper_launch_cmd_uses_module_form_when_not_frozen(monkeypatch) -> None:
    """A source/venv install runs the keeper as `<python> -m clauster.pty_keeper …`."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    cmd = SessionRunner._keeper_launch_cmd(Path("/s.json"), Path("/cwd"), ["claude", "x"])
    assert cmd[:3] == [sys.executable, "-m", "clauster.pty_keeper"]
    assert procutil.KEEPER_SUBCOMMAND not in cmd


def test_keeper_launch_cmd_uses_subcommand_when_frozen(monkeypatch) -> None:
    """A frozen (PyInstaller) build re-invokes itself: `<exe> __pty-keeper__ …`, never `-m`.

    Under a one-file binary ``sys.executable`` IS the clauster binary, so the `-m` form
    would make clauster's argparse reject the keeper — the exact failure this guards.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    # In a real frozen build sys.executable is the clauster binary, not the test's python.
    monkeypatch.setattr(sys, "executable", "/opt/clauster/clauster")
    cmd = SessionRunner._keeper_launch_cmd(Path("/s.json"), Path("/cwd"), ["claude", "x"])
    assert cmd[:2] == ["/opt/clauster/clauster", procutil.KEEPER_SUBCOMMAND]
    assert "-m" not in cmd
    # the bridge argv still survives intact after the `--` separator
    assert cmd[cmd.index("--") + 1 :] == ["claude", "x"]
    # and procutil recognizes the very command this produces as a keeper (round-trip)
    assert procutil.is_keeper_cmdline(cmd)


def test_screen_sidecar_path_sits_beside_the_keeper_sidecar() -> None:
    """The screen sidecar is `<stem>.screen.json`, beside the keeper discovery JSON (#534)."""
    log = Path("/logs/alpha-123-0.log")
    assert SessionRunner._screen_sidecar_path_for(log).name == "alpha-123-0.screen.json"


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


def test_build_pty_bridge_argv_never_adds_verbose(runner_config) -> None:
    """The verbose toggle is standard-only — the pty flag-form keeper never gets --verbose."""
    runner, claude_json = _pty_runner(runner_config)
    runner._config.instance_defaults.verbose = True
    argv = runner._build_pty_bridge_argv(Path("/tmp/x.log"), "alpha", "default", resume=False)
    assert "--verbose" not in argv  # would corrupt the live-screen tap / PTY render


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
        await runner.stop(inst.instance_id)


@_POSIX_ONLY
async def test_spawn_pty_ignores_custom_name(runner_config) -> None:
    """#780 disposition: pty (the flag form) has no --name equivalent.

    ``label`` (and the positional name actually passed to ``--remote-control``)
    stay the project name, unlike a standard bridge where the same
    ``custom_name`` becomes ``--name`` (see
    test_spawn_controls.test_spawn_custom_name_reaches_argv_and_label).
    """
    runner = SessionRunner(runner_config[0], claude_json=runner_config[1])  # config=standard
    inst = await runner.spawn("alpha", resume_mode="pty", custom_name="My Custom Bridge")
    try:
        assert inst.resume_mode == "pty"
        assert inst.label == "alpha"
        argv = json.loads(Path(str(inst.bridge_debug_log_path) + ".argv.json").read_text())
        assert argv[argv.index("--remote-control") + 1] == "alpha"
    finally:
        await runner.stop(inst.instance_id)


@_POSIX_ONLY
async def test_spawn_pty_ignores_sandbox(runner_config) -> None:
    """#780 is server-mode: a pty bridge records "default" and gets no sandbox flag.

    Mirrors :func:`test_spawn_pty_ignores_custom_name` — the sandbox toggle is only
    wired into the standard subcommand argv, never the flag-form pty bridge.
    """
    runner = SessionRunner(runner_config[0], claude_json=runner_config[1])  # config=standard
    inst = await runner.spawn("alpha", resume_mode="pty", sandbox="on")
    try:
        assert inst.resume_mode == "pty"
        assert inst.sandbox_mode == "default"
        argv = json.loads(Path(str(inst.bridge_debug_log_path) + ".argv.json").read_text())
        assert "--sandbox" not in argv
        assert "--no-sandbox" not in argv
    finally:
        await runner.stop(inst.instance_id)


@_POSIX_ONLY
async def test_spawn_pty_launch_failure_sets_error(runner_config, tmp_path, monkeypatch) -> None:
    """A keeper that fails to launch (OSError) marks the instance ERROR and persists it."""
    from clauster.models import Project, RemoteControlInstance

    runner, _ = _pty_runner(runner_config)

    persisted = []

    async def _spy_persist() -> None:
        persisted.append(True)

    monkeypatch.setattr(runner, "_persist", _spy_persist)

    def boom(*a, **k):
        raise OSError("openpty: too many open files")

    monkeypatch.setattr(runner, "_popen_keeper", boom)

    proj = Project(name="alpha", path=runner_config[0].projects_root / "alpha")
    inst = RemoteControlInstance(project="alpha", label="alpha", resume_mode="pty")
    log_path = tmp_path / "alpha.log"

    out = await runner._spawn_pty(inst, proj, "alpha", log_path, "default", resume=False)

    assert out.status is InstanceStatus.ERROR
    assert persisted == [True]  # the ERROR state was persisted, not swallowed


@_POSIX_ONLY
async def test_spawn_pty_screen_sidecar_gated_on_config(
    runner_config, tmp_path, monkeypatch
) -> None:
    """The keeper receives a screen sidecar only when claude.pty_screen_enabled is on (#534)."""
    from clauster.models import Project, RemoteControlInstance

    captured: dict = {}

    def _capture(cwd, sidecar, bridge_argv, screen_sidecar=None):
        captured["screen_sidecar"] = screen_sidecar
        raise OSError("captured — stop before the real keeper launch")

    async def _noop_persist() -> None:
        pass

    proj = Project(name="alpha", path=runner_config[0].projects_root / "alpha")
    log_path = tmp_path / "alpha.log"

    def _run(runner) -> None:
        monkeypatch.setattr(runner, "_popen_keeper", _capture)
        monkeypatch.setattr(runner, "_persist", _noop_persist)
        inst = RemoteControlInstance(project="alpha", label="alpha", resume_mode="pty")
        return runner._spawn_pty(inst, proj, "alpha", log_path, "default", resume=False)

    # flag OFF (default) -> no screen sidecar handed to the keeper
    off_runner, _ = _pty_runner(runner_config)
    await _run(off_runner)
    assert captured["screen_sidecar"] is None

    # flag ON -> a `.screen.json` sidecar beside the keeper sidecar
    captured.clear()
    cfg = runner_config[0]
    on_cfg = ClausterConfig(
        projects_root=cfg.projects_root,
        state_dir=cfg.state_dir,
        claude={"binary": cfg.claude.binary, "launch_mode": "pty", "pty_screen_enabled": True},
    )
    on_runner = SessionRunner(on_cfg, claude_json=runner_config[1])
    await _run(on_runner)
    assert captured["screen_sidecar"] is not None
    assert captured["screen_sidecar"].name.endswith(".screen.json")


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


def test_apply_pty_info_ready_without_url_is_running(runner_config) -> None:
    # A --continue resume reaches state "ready" with NO connect_url (it reconnected
    # without re-printing the URL). A live keeper+bridge must read RUNNING, not ERROR.
    from clauster.models import RemoteControlInstance

    runner, _ = _pty_runner(runner_config)
    inst = RemoteControlInstance(project="alpha", label="alpha")
    runner._apply_pty_info(inst, {"bridge_pid": 7, "state": "ready"}, _FakeProc(alive=True))
    assert inst.status is InstanceStatus.RUNNING
    assert inst.url is None  # no deep link captured on a URL-less resume (expected)


def test_await_ready_pty_returns_on_ready_without_url(runner_config, tmp_path) -> None:
    runner, _ = _pty_runner(runner_config)
    sidecar = tmp_path / "sc.json"
    sidecar.write_text(json.dumps({"state": "ready", "bridge_pid": 7}))
    info = runner._await_ready_pty(sidecar, _FakeProc(alive=True))
    assert info.get("state") == "ready"  # returns promptly on "ready", not only connect_url


def test_await_ready_pty_returns_when_keeper_exits_before_ready(runner_config, tmp_path) -> None:
    # A keeper (and thus bridge) that dies before publishing a connect URL must NOT block
    # _await_ready_pty until _READY_TIMEOUT — the dead-proc arm returns the last snapshot at
    # once. Drive a dead _FakeProc with a sidecar still "starting" (no connect_url) and assert
    # it returns that snapshot promptly, well under the 15s readiness deadline.
    import time

    runner, _ = _pty_runner(runner_config)
    sidecar = tmp_path / "sc.json"
    sidecar.write_text(json.dumps({"state": "starting", "bridge_pid": 11}))

    start = time.monotonic()
    info = runner._await_ready_pty(sidecar, _FakeProc(alive=False))
    elapsed = time.monotonic() - start

    assert info.get("state") == "starting"  # last snapshot, not a synthesized ready/error
    assert info.get("connect_url") is None
    assert elapsed < 1.0  # returned on the keeper-dead arm, did not block to _READY_TIMEOUT


def test_keeper_pid_skips_sidecar_with_mismatched_proc_start(runner_config) -> None:
    # PID-reuse defense: a sidecar whose bridge_pid matches but whose bridge_proc_start drifts
    # beyond _PROC_START_TOLERANCE is a stale entry that merely recycled the pid — it must be
    # rejected so stop()/poll_once never reap an unrelated process tree. With only that sidecar
    # present, the lookup falls through to None.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    (runner._log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps(
            {
                "keeper_pid": 5555,
                "bridge_pid": 4242,
                "bridge_proc_start": 100.0,
                "state": "ready",
            }
        )
    )

    # same pid, but proc-start off by far more than _PROC_START_TOLERANCE (2.0s)
    assert runner._recover_keeper_pid("alpha", bridge_pid=4242, bridge_proc_start=1000.0) is None
    # the in-tolerance lookup still resolves the keeper, proving the skip is the proc-start guard
    assert runner._recover_keeper_pid("alpha", bridge_pid=4242, bridge_proc_start=100.5) == 5555


def test_recover_keeper_pid_returns_none_when_keeper_pid_missing(runner_config) -> None:
    # A matched sidecar (pid + proc-start) that carries no usable keeper_pid yields None rather
    # than a bogus pid — the runner must never hand stop()/poll_once a non-int to signal.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    (runner._log_dir / "beta-1700000000000-0.keeper.json").write_text(
        json.dumps({"keeper_pid": None, "bridge_pid": 4242, "bridge_proc_start": 100.0})
    )

    assert runner._recover_keeper_pid("beta", bridge_pid=4242, bridge_proc_start=100.0) is None


def _states_until_keeper_ready(tmp_path, monkeypatch, argv) -> list:
    """Run ``run_keeper`` until it publishes "ready", then reap the bridge so it returns.

    Deterministic — the bridge BLOCKS until killed, so "ready" (published once the URL
    timeout lapses while the bridge is alive) is always observed before the bridge can
    exit. There is no wall-clock margin to lose under load: the old form let a short-lived
    `sleep(0.6)` bridge race the keeper's ~0.5s select loop, which flaked the suite under
    contention. run_keeper stays on the MAIN thread (it calls ``signal.signal``); a watcher
    thread reaps the bridge by the pid the sidecar publishes once "ready" lands.
    """
    import contextlib
    import os
    import signal as _signal
    import threading

    from clauster import pty_keeper

    states: list = []
    pid_box: list = []
    ready = threading.Event()

    def _capture(_sc, base):
        states.append(base.get("state"))
        if base.get("bridge_pid"):
            pid_box[:] = [base["bridge_pid"]]
        if base.get("state") == "ready":
            ready.set()

    monkeypatch.setattr(pty_keeper, "_write_sidecar", _capture)
    monkeypatch.setattr(pty_keeper, "_URL_TIMEOUT", 0.2)

    def _reap_when_ready() -> None:
        ready.wait(timeout=10)  # fires in ~0.5s in practice; bound the wait either way
        if pid_box:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid_box[0], _signal.SIGKILL)

    watcher = threading.Thread(target=_reap_when_ready, daemon=True)
    watcher.start()
    prev = _signal.getsignal(_signal.SIGHUP)
    try:
        pty_keeper.run_keeper(argv, tmp_path / "sc.json")
    finally:
        _signal.signal(_signal.SIGHUP, prev)  # keeper sets SIG_IGN; restore for other tests
    watcher.join(timeout=5)
    return states


@_POSIX_ONLY
def test_keeper_marks_ready_on_resume_without_url(tmp_path, monkeypatch) -> None:
    # A --continue resume that never prints a connect URL must still reach "ready"
    # (it reconnected) instead of being stuck "starting" -> a false ERROR upstream.
    argv = [sys.executable, "-c", "import time; time.sleep(30)", "--continue"]
    states = _states_until_keeper_ready(tmp_path, monkeypatch, argv)
    assert "ready" in states  # marked ready on the resume timeout, not stuck "starting"


@_POSIX_ONLY
def test_keeper_fresh_start_no_url_becomes_ready(tmp_path, monkeypatch) -> None:
    # A FRESH start (no --continue) that stays alive past the URL timeout without a URL
    # must reach "ready", not stay "starting". Newer claude builds connect without ever
    # printing the claude.ai/code/session_… line, so gating readiness on the URL would
    # leave a healthy bridge stuck -> upstream false-ERROR -> the "external session
    # active" misclassification. Mirrors the resume case above.
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    states = _states_until_keeper_ready(tmp_path, monkeypatch, argv)
    assert "ready" in states  # promoted on liveness once it survives the URL timeout


def test_keeper_read_loop_uses_poll_not_select() -> None:
    # Regression: the keeper read loop must use poll(), not select.select(), which raises
    # "filedescriptor out of range" once the PTY master fd >= FD_SETSIZE (1024) — crashing a
    # long-lived keeper (and flaking the -n0 full suite as fds accumulated).
    import inspect

    from clauster import pty_keeper

    src = inspect.getsource(pty_keeper.run_keeper)
    assert "select.select(" not in src, "keeper read loop uses select() (FD_SETSIZE-limited)"
    # Assert on poller.poll( specifically — a bare ".poll(" is already satisfied by the
    # run_keeper proc.poll() liveness calls, so it would not catch a regression to select().
    assert "poller.poll(" in src, "keeper read loop must wait on poller.poll(), not select()"


def test_signal_stop_twice_sends_two_signals(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("clauster.runner.os.kill", lambda pid, sig: calls.append(pid))
    monkeypatch.setattr("clauster.runner.time.sleep", lambda _s: None)

    SessionRunner._signal_stop(1234, twice=False)
    assert calls == [1234]
    calls.clear()
    SessionRunner._signal_stop(1234, twice=True)
    assert calls == [1234, 1234]  # the flag-form TUI needs the confirming second press


def test_signal_stop_swallows_oserror(monkeypatch, caplog) -> None:
    # runner.py 2379-2382: a stop signal to an already-exited/unsignalable pid must be a
    # no-op (logged at DEBUG), never raised out of stop(). Force os.kill to raise OSError
    # so the handler runs on every OS (Windows os.kill can't be exercised with a real pid).
    def boom(_pid: int, _sig: int) -> None:
        raise OSError("not signalable")

    monkeypatch.setattr("clauster.runner.os.kill", boom)
    with caplog.at_level("DEBUG", logger="clauster.runner"):
        SessionRunner._signal_stop(1234)  # must not raise
    assert any("was a no-op" in r.getMessage() for r in caplog.records)


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
        stopped = await runner.stop(inst.instance_id)
    assert stopped.status is InstanceStatus.STOPPED
    # both the bridge and its keeper are gone after stop
    assert procutil.proc_create_time(inst.bridge_pid) is None
    assert procutil.proc_create_time(inst.keeper_pid) is None


@_POSIX_ONLY
async def test_spawn_pty_no_url_does_not_falsely_run(runner_config, monkeypatch) -> None:
    """A flag-form bridge that never prints a URL must not be reported RUNNING."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "pty_no_url")
    # The behaviour under test (never-RUNNING without a URL) is independent of the
    # ready-wait DURATION, so cap it instead of polling the real 15s _READY_TIMEOUT —
    # this is the single slowest test in the suite (~15s) and the floor under the
    # parallel run. _await_ready reads these module globals at call time.
    monkeypatch.setattr("clauster.runner._READY_TIMEOUT", 0.5)
    monkeypatch.setattr("clauster.runner._READY_POLL_INTERVAL", 0.05)
    runner, _ = _pty_runner(runner_config)
    inst = await runner.spawn("alpha")
    try:
        assert inst.status is not InstanceStatus.RUNNING  # STARTING (watch will ERROR it)
    finally:
        await runner.stop(inst.instance_id)


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
        await runner.stop(inst.instance_id)


@_POSIX_ONLY
async def test_resume_pty_while_standard_live_returns_pty_not_standard(runner_config) -> None:
    """Resuming a STOPPED pty session must revive the pty — never a live standard bridge.

    #777 allows a standard bridge and pty sessions to coexist in one project. The mode
    resolution + pty idempotency must key on the SPECIFIC instance being resumed, not a
    mode-agnostic "first live instance for the project" scan — otherwise a resume of the
    stopped pty silently hands back the running standard bridge (Greptile P1).
    """
    runner, _ = _pty_runner(runner_config)  # config default = pty
    # A live standard bridge for the project (explicit picker overrides the pty default).
    standard = await runner.spawn("alpha", resume_mode="standard")
    assert standard.resume_mode == "standard" and standard.status is InstanceStatus.RUNNING
    # A pty session for the SAME project, then stop it -> a resumable STOPPED pty card.
    pty = await runner.spawn("alpha", resume_mode="pty")
    assert pty.resume_mode == "pty" and pty.instance_id != standard.instance_id
    stopped = await runner.stop(pty.instance_id)
    assert stopped.status is InstanceStatus.STOPPED
    # The standard bridge is still live at this point (independent axis).
    assert runner.get_instance(standard.instance_id).status is InstanceStatus.RUNNING
    try:
        resumed = await runner.resume(pty.instance_id)  # resume the STOPPED pty
        # The invariant: the resume comes back as a PTY bridge, and is NOT the live
        # standard bridge handed back. (The pre-fix bug returned `standard` verbatim.)
        assert resumed.resume_mode == "pty"
        assert resumed is not standard
        assert resumed.instance_id != standard.instance_id
        assert resumed.status is InstanceStatus.RUNNING
        # The live standard bridge is untouched by the pty resume.
        assert runner.get_instance(standard.instance_id).status is InstanceStatus.RUNNING
    finally:
        await runner.stop(resumed.instance_id)
        await runner.stop(standard.instance_id)


@_POSIX_ONLY
async def test_resume_standard_only_still_returns_standard(runner_config) -> None:
    """Mirror of the P1 case: resume while only a standard bridge exists resolves standard.

    Guards against an over-correction — the mode-aware fix must not misfire the other way
    (e.g. treat a standard resume as pty) when no pty session exists for the project.
    """
    runner, _ = _pty_runner(runner_config)
    standard = await runner.spawn("alpha", resume_mode="standard")
    stopped = await runner.stop(standard.instance_id)
    assert stopped.status is InstanceStatus.STOPPED
    try:
        resumed = await runner.resume(standard.instance_id)
        assert resumed.resume_mode == "standard"
        assert resumed.status is InstanceStatus.RUNNING
    finally:
        await runner.stop(resumed.instance_id)


@_POSIX_ONLY
async def test_resume_already_live_pty_is_idempotent(runner_config) -> None:
    """Resuming an already-RUNNING pty returns that same instance (no second bridge).

    Exercises the pty idempotency branch keyed on resume_target: a resume of a live
    session must hand back the exact instance, not spawn a duplicate keeper.
    """
    runner, _ = _pty_runner(runner_config)
    pty = await runner.spawn("alpha", resume_mode="pty")
    assert pty.status is InstanceStatus.RUNNING
    try:
        again = await runner.resume(pty.instance_id)  # still live -> idempotent return
        assert again is pty
        assert again.instance_id == pty.instance_id
        assert runner.running_count() == 1  # no duplicate bridge
    finally:
        await runner.stop(pty.instance_id)


@_POSIX_ONLY
async def test_spawn_pty_with_worktree_skips_collision_warning(runner_config, caplog) -> None:
    """A pty spawn WITH a worktree isolates each session, so no no-worktree warning fires."""
    import logging

    runner, _ = _pty_runner(runner_config)  # "alpha" is a git repo -> worktree allowed
    with caplog.at_level(logging.WARNING, logger="clauster.runner"):
        inst = await runner.spawn("alpha", resume_mode="pty", spawn_mode="worktree")
    try:
        assert inst.spawn_mode == "worktree" and inst.resume_mode == "pty"
        assert not any("without a worktree" in r.message for r in caplog.records)
    finally:
        await runner.stop(inst.instance_id)


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
    runner._instances[inst.instance_id] = inst
    cleaned: list[int] = []
    monkeypatch.setattr(runner, "_cleanup_keeper", lambda pid: cleaned.append(pid))

    stopped = await runner.stop(inst.instance_id)
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
    assert runner.get_instance_for_project("alpha").resume_mode == "pty"


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
    inst = runner.get_instance_for_project("alpha")
    assert inst.resume_mode == "pty"
    assert inst.keeper_pid == 9999


async def test_rediscover_reattaches_live_pty_keeper_without_pointer(
    runner_config, monkeypatch
) -> None:
    """A self-spawned pty bridge with no pointer but a live keeper reattaches as RUNNING.

    The flag-form bridge writes no Anthropic ``bridge-pointer.json``, so rediscover keys
    on the keeper sidecar instead: a live keeper (``is_keeper_process``) holding a ready,
    live bridge is rebuilt as a managed RUNNING instance — not orphaned behind a STOPPED
    card while the detached keeper leaks.
    """
    from clauster.state import StateStore

    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {"alpha": {"label": "alpha", "resume_mode": "pty", "intentional_stop": False}}
    )
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps(
            {
                "keeper_pid": 9999,
                "bridge_pid": 4242,
                "bridge_proc_start": 12345.0,
                "session_id": "session_x",
                "connect_url": "https://claude.ai/code/session_x",
                "state": "ready",
            }
        )
    )
    # The bridge's parse-source the WS reads — present, as a live bridge's would be.
    (log_dir / "alpha-1700000000000-0.log").write_text("bridge output\n")
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: None)
    monkeypatch.setattr("clauster.procutil.is_keeper_process", lambda pid: pid == 9999)
    monkeypatch.setattr("clauster.procutil.is_live_bridge", lambda pid, start, **k: pid == 4242)

    await runner.rediscover()

    inst = runner.get_instance_for_project("alpha")
    assert inst.status is InstanceStatus.RUNNING  # re-managed, not orphaned
    assert inst.resume_mode == "pty"
    assert inst.keeper_pid == 9999  # so stop()/poll_once own the survivor
    assert inst.bridge_pid == 4242
    assert inst.intentional_stop is False
    assert inst.url == "https://claude.ai/code/session_x"
    # Re-bind the live tail: the bridge's log path is derived from the matched sidecar's
    # shared spawn-set stem, so `/ws/bridge-log` resolves a real path after a reattach
    # instead of 1008-ing the tail to death (#584).
    assert inst.bridge_debug_log_path == log_dir / "alpha-1700000000000-0.log"
    assert inst.bridge_raw_log_path == log_dir / "alpha-1700000000000-0.log"


async def test_rediscover_pty_missing_log_leaves_tail_unbound(runner_config, monkeypatch):
    """If retention pruned the reattached pty bridge's log set, the tail source is left None
    (not a dangling path), so `/ws/bridge-log` 1008s and the operator sees the "disconnected"
    banner — a prompt to act — rather than a silently-empty live panel (#584 review)."""
    from clauster.state import StateStore

    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {"alpha": {"label": "alpha", "resume_mode": "pty", "intentional_stop": False}}
    )
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps(
            {
                "keeper_pid": 9999,
                "bridge_pid": 4242,
                "bridge_proc_start": 12345.0,
                "state": "ready",
            }
        )
    )
    # Note: the `<stem>.log` parse-source is deliberately absent (pruned).
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: None)
    monkeypatch.setattr("clauster.procutil.is_keeper_process", lambda pid: pid == 9999)
    monkeypatch.setattr("clauster.procutil.is_live_bridge", lambda pid, start, **k: pid == 4242)

    await runner.rediscover()

    inst = runner.get_instance_for_project("alpha")
    assert inst.status is InstanceStatus.RUNNING  # still re-managed (Stop/observe restored)
    assert inst.bridge_debug_log_path is None  # no dangling path → WS 1008s, banner shows
    assert inst.bridge_raw_log_path is None


async def test_rediscover_pty_dead_keeper_falls_back_to_stopped(
    runner_config, monkeypatch
) -> None:
    """A pty sidecar whose keeper pid is no longer a keeper (dead/recycled) → STOPPED.

    The liveness guard fails closed: a stale sidecar must not reattach an unrelated
    process, so rediscover resurrects the resumable STOPPED card instead.
    """
    from clauster.state import StateStore

    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {"alpha": {"label": "alpha", "resume_mode": "pty", "intentional_stop": False}}
    )
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps(
            {
                "keeper_pid": 9999,
                "bridge_pid": 4242,
                "bridge_proc_start": 12345.0,
                "state": "ready",
            }
        )
    )
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: None)
    monkeypatch.setattr("clauster.procutil.is_keeper_process", lambda pid: False)

    await runner.rediscover()

    inst = runner.get_instance_for_project("alpha")
    assert inst.status is InstanceStatus.STOPPED
    assert inst.resume_mode == "pty"  # resume affordance preserved
    assert inst.keeper_pid is None


async def test_rediscover_pty_unready_sidecar_falls_back_to_stopped(
    runner_config, monkeypatch
) -> None:
    """A pty keeper still mid-startup (sidecar ``state != "ready"``) is not reattached.

    Only a ``"ready"`` keeper reattaches as RUNNING even when the process looks alive;
    a ``"starting"`` one falls back to STOPPED (the orphan sweep can reap a stuck one).
    """
    from clauster.state import StateStore

    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {"alpha": {"label": "alpha", "resume_mode": "pty", "intentional_stop": False}}
    )
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps(
            {
                "keeper_pid": 9999,
                "bridge_pid": 4242,
                "bridge_proc_start": 12345.0,
                "state": "starting",
            }
        )
    )
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: None)
    monkeypatch.setattr("clauster.procutil.is_keeper_process", lambda pid: True)
    monkeypatch.setattr("clauster.procutil.is_live_bridge", lambda pid, start, **k: True)

    await runner.rediscover()

    assert runner.get_instance_for_project("alpha").status is InstanceStatus.STOPPED


async def test_rediscover_pty_reattach_rejects_stale_bridge_pid(
    runner_config, monkeypatch
) -> None:
    """Reattach fails closed when the bridge pid no longer matches (PID reuse)."""
    from clauster.state import StateStore

    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {"alpha": {"label": "alpha", "resume_mode": "pty", "intentional_stop": False}}
    )
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps(
            {
                "keeper_pid": 9999,
                "bridge_pid": 4242,
                "bridge_proc_start": 12345.0,
                "state": "ready",
            }
        )
    )
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: None)
    monkeypatch.setattr("clauster.procutil.is_keeper_process", lambda pid: True)
    monkeypatch.setattr("clauster.procutil.is_live_bridge", lambda pid, start, **k: False)

    await runner.rediscover()

    assert runner.get_instance_for_project("alpha").status is InstanceStatus.STOPPED


async def test_rediscover_pty_reattach_skips_malformed_sidecar_pids(
    runner_config, monkeypatch
) -> None:
    """A ready sidecar with a non-integer keeper/bridge pid is skipped (falls to STOPPED)."""
    from clauster.state import StateStore

    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {"alpha": {"label": "alpha", "resume_mode": "pty", "intentional_stop": False}}
    )
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps({"keeper_pid": 9999, "bridge_pid": None, "state": "ready"})
    )
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: None)
    monkeypatch.setattr("clauster.procutil.is_keeper_process", lambda pid: True)

    await runner.rediscover()

    assert runner.get_instance_for_project("alpha").status is InstanceStatus.STOPPED


async def test_rediscover_pty_reattach_without_proc_start_uses_cmdline_liveness(
    runner_config, monkeypatch
) -> None:
    """A ready sidecar missing bridge_proc_start still reattaches via cmdline+alive.

    Documents the intentional PID-guard degradation (mirrors is_live_process and
    _recover_keeper_pid): with no recorded proc-start the bridge is matched by pid +
    cmdline only, and the instance carries bridge_proc_start=None rather than refusing
    to reattach a live keeper. The keeper side is still gated by is_keeper_process.
    """
    from clauster.state import StateStore

    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {"alpha": {"label": "alpha", "resume_mode": "pty", "intentional_stop": False}}
    )
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # sidecar with NO bridge_proc_start field
    (log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps({"keeper_pid": 9999, "bridge_pid": 4242, "state": "ready"})
    )
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: None)
    monkeypatch.setattr("clauster.procutil.is_keeper_process", lambda pid: True)
    # returns True ONLY when proc_start arrived as None -> proves the degraded path was taken
    monkeypatch.setattr("clauster.procutil.is_live_bridge", lambda pid, start, **k: start is None)

    await runner.rediscover()

    inst = runner.get_instance_for_project("alpha")
    assert inst.status is InstanceStatus.RUNNING
    assert inst.keeper_pid == 9999 and inst.bridge_pid == 4242
    assert inst.bridge_proc_start is None  # matched by cmdline+alive, no proc-start recorded


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


def test_backfill_starter_session_from_debug_file_on_resume(runner_config, tmp_path) -> None:
    # pty true-resume: no pointer and the keeper captured no connect URL — recover the
    # session the bridge resumed from its --debug-file so the "Open session" deep link
    # works (session_url is computed from starter_session_id).
    from clauster.models import RemoteControlInstance

    runner, _ = _pty_runner(runner_config)
    log = tmp_path / "bridge.log"
    log.write_text("[DEBUG] [remote-bridge] Unarchive session_01RESUMEDXYZABC status=409\n")
    inst = RemoteControlInstance(
        project="alpha", label="alpha", resume_mode="pty", bridge_debug_log_path=log
    )
    runner._backfill_starter_session(inst, tmp_path / "noproj")  # no pointer at this path
    assert inst.starter_session_id == "session_01RESUMEDXYZABC"
    assert inst.session_url == "https://claude.ai/code/session_01RESUMEDXYZABC?from=cli"


# ----- spawn_detailed outcome reporting (#778) --------------------------------------


async def test_spawn_detailed_standard_cap_reports_reused(runner_config) -> None:
    """The standard-singleton cap reports created=False + the cap reason (#778).

    The first spawn reports created=True; the second comes back created=False with
    the SAME instance and a reason naming the one-per-project cap — the signal the
    API turns into a 200-with-reason instead of a silent no-op.
    """
    runner, _ = _pty_runner(runner_config)
    first = await runner.spawn_detailed("alpha", resume_mode="standard")
    assert first.created is True and first.reason is None and first.warnings == []
    try:
        second = await runner.spawn_detailed("alpha", resume_mode="standard")
        assert second.created is False
        assert second.instance is first.instance
        assert "capped at one per project" in second.reason
        assert runner.running_count() == 1  # nothing new was launched
    finally:
        await runner.stop(first.instance.instance_id)


@_POSIX_ONLY
async def test_spawn_detailed_pty_no_worktree_surfaces_warning(runner_config) -> None:
    """A pty spawn without a worktree carries the collision advisory on the outcome (#778).

    The warning is non-blocking (the session still launches, created=True); a spawn
    WITH a worktree carries no advisory. This is what the API surfaces as warnings[].
    """
    runner, _ = _pty_runner(runner_config)
    bare = await runner.spawn_detailed("alpha", resume_mode="pty")
    try:
        assert bare.created is True
        assert any("without a worktree" in w for w in bare.warnings)
        assert bare.instance.status is InstanceStatus.RUNNING  # warn, never block
    finally:
        await runner.stop(bare.instance.instance_id)
    isolated = await runner.spawn_detailed("alpha", resume_mode="pty", spawn_mode="worktree")
    try:
        assert isolated.created is True and isolated.warnings == []
    finally:
        await runner.stop(isolated.instance.instance_id)


@_POSIX_ONLY
async def test_spawn_detailed_resume_of_live_pty_reports_reused(runner_config) -> None:
    """An idempotent resume of an already-live pty session reports created=False (#778)."""
    runner, _ = _pty_runner(runner_config)
    pty = await runner.spawn("alpha", resume_mode="pty", spawn_mode="worktree")
    assert pty.status is InstanceStatus.RUNNING
    try:
        again = await runner.spawn_detailed(
            "alpha", resume_mode="pty", resume=True, resume_target=pty
        )
        assert again.created is False
        assert again.instance is pty
        assert "already" in again.reason
        assert runner.running_count() == 1  # no duplicate keeper
    finally:
        await runner.stop(pty.instance_id)


# ----- resume revives the SAME instance identity (#779 precursor) -------------------


@_POSIX_ONLY
async def test_resume_pty_revives_same_instance_identity(runner_config) -> None:
    """A pty stop→resume keeps the instance_id and REPLACES the registry row.

    Pre-fix, resume minted a fresh id and the old STOPPED row lingered as a ghost
    duplicate — invisible to the project-keyed client (its fold collapsed the two),
    but a phantom card in any instance-keyed view (#779), and it broke per-instance
    derivations that must survive a stop→resume cycle (the pty worktree name).
    """
    runner, _ = _pty_runner(runner_config)
    pty = await runner.spawn("alpha", resume_mode="pty")
    assert pty.status is InstanceStatus.RUNNING
    await runner.stop(pty.instance_id)
    resumed = await runner.resume(pty.instance_id)
    try:
        assert resumed.instance_id == pty.instance_id  # same logical session
        rows = [i for i in runner.list_instances() if i.project == "alpha"]
        assert len(rows) == 1  # replaced, not duplicated
        assert rows[0] is resumed and resumed.status is InstanceStatus.RUNNING
    finally:
        await runner.stop(resumed.instance_id)


async def test_resume_standard_revives_same_instance_identity(runner_config) -> None:
    """Same identity contract for a standard bridge stop→resume."""
    runner, _ = _pty_runner(runner_config)
    std = await runner.spawn("alpha", resume_mode="standard")
    assert std.status is InstanceStatus.RUNNING
    await runner.stop(std.instance_id)
    resumed = await runner.resume(std.instance_id)
    try:
        assert resumed.instance_id == std.instance_id
        rows = [i for i in runner.list_instances() if i.project == "alpha"]
        assert len(rows) == 1
        assert rows[0] is resumed and resumed.status is InstanceStatus.RUNNING
    finally:
        await runner.stop(resumed.instance_id)


# ----- worktree passthrough for interactive sessions (#779) -------------------------


def test_pty_argv_carries_worktree_name_when_given() -> None:
    """spawn_mode="worktree" adds `--worktree <name>` to the flag-form argv (#779)."""
    runner = SessionRunner.__new__(SessionRunner)  # argv builder is pure — no init needed
    runner._binary = "claude"
    base = runner._build_pty_bridge_argv(Path("/l.log"), "alpha", "default", resume=False)
    assert "--worktree" not in base  # same-dir spawn: no worktree flag
    wt = runner._build_pty_bridge_argv(
        Path("/l.log"), "alpha", "default", resume=True, worktree_name="clauster-abcd1234"
    )
    i = wt.index("--worktree")
    assert wt[i + 1] == "clauster-abcd1234"
    assert wt[-1] == "--continue"  # resume keeps --continue alongside the worktree


def test_pty_worktree_name_stable_and_mode_gated() -> None:
    """The derived worktree name keys off the stable instance_id; same-dir gets None."""
    from clauster.models import RemoteControlInstance

    wt = RemoteControlInstance(project="alpha", label="alpha", spawn_mode="worktree")
    name = SessionRunner._pty_worktree_name(wt)
    assert name == f"clauster-{wt.instance_id[:8]}"
    assert SessionRunner._pty_worktree_name(wt) == name  # deterministic
    same_dir = RemoteControlInstance(project="alpha", label="alpha", spawn_mode="same-dir")
    assert SessionRunner._pty_worktree_name(same_dir) is None


@_POSIX_ONLY
async def test_spawn_pty_worktree_passes_flag_and_resume_reuses_name(
    runner_config, monkeypatch
) -> None:
    """A worktree pty spawn passes --worktree, and its resume reuses the SAME name.

    The name derives from the instance_id, which a resume revives — so
    `claude --continue --worktree <name>` restores the conversation in the same
    worktree (claude reuses an existing name; empirically verified).
    """
    runner, _ = _pty_runner(runner_config)
    seen: list[list[str]] = []
    real = SessionRunner._popen_keeper

    def _capture(self, cwd, sidecar, bridge_argv, screen_sidecar=None):
        seen.append(list(bridge_argv))
        return real(self, cwd, sidecar, bridge_argv, screen_sidecar)

    monkeypatch.setattr(SessionRunner, "_popen_keeper", _capture)
    pty = await runner.spawn("alpha", resume_mode="pty", spawn_mode="worktree")
    assert pty.status is InstanceStatus.RUNNING
    first = seen[-1]
    i = first.index("--worktree")
    expected = f"clauster-{pty.instance_id[:8]}"
    assert first[i + 1] == expected

    await runner.stop(pty.instance_id)
    resumed = await runner.resume(pty.instance_id)
    try:
        second = seen[-1]
        j = second.index("--worktree")
        assert second[j + 1] == expected  # same identity -> same worktree
        assert "--continue" in second
    finally:
        await runner.stop(resumed.instance_id)


# ----- audited coverage gaps (2026-07 audit) ---------------------------------


def test_recover_keeper_pid_none_without_bridge_pid(runner_config) -> None:
    # runner.py 1530-1531: no bridge pid means no sidecar can be matched safely —
    # the lookup returns None instead of guessing a keeper to signal.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    assert runner._recover_keeper_pid("alpha", bridge_pid=None, bridge_proc_start=None) is None


def test_recover_keeper_pid_skips_foreign_and_corrupt_sidecars(runner_config) -> None:
    # runner.py 1534-1535: a sidecar for a DIFFERENT bridge pid, and one that doesn't
    # parse at all, are both skipped — the recovery must never adopt another
    # bridge's keeper (that pid is what stop()/poll_once would later signal).
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    (runner._log_dir / "gamma-1700000000000-0.keeper.json").write_text(
        json.dumps({"keeper_pid": 7777, "bridge_pid": 1111, "bridge_proc_start": 100.0})
    )
    (runner._log_dir / "gamma-1700000000000-1.keeper.json").write_text("not json {{{")

    assert runner._recover_keeper_pid("gamma", bridge_pid=2222, bridge_proc_start=100.0) is None


@_POSIX_ONLY
async def test_spawn_pty_error_surfaces_keeper_error_detail(
    runner_config, tmp_path, monkeypatch
) -> None:
    # runner.py 1637->1640: a pty spawn that lands in ERROR must copy the keeper's
    # recorded reason into error_detail — the UI shows WHY, not a bare "Error".
    from clauster.models import Project, RemoteControlInstance

    runner, _ = _pty_runner(runner_config)

    async def _noop_persist() -> None:
        pass

    class _DeadProc:
        pid = 4242

        def poll(self):  # noqa: ANN202 — mimic subprocess.Popen.poll
            return 70

    monkeypatch.setattr(runner, "_persist", _noop_persist)
    monkeypatch.setattr(runner, "_popen_keeper", lambda *a, **k: _DeadProc())
    monkeypatch.setattr(
        runner,
        "_await_ready_pty",
        lambda sidecar, proc: {"state": "error", "error": "openpty failed: boom"},
    )
    proj = Project(name="alpha", path=runner_config[0].projects_root / "alpha")
    inst = RemoteControlInstance(project="alpha", label="alpha", resume_mode="pty")

    out = await runner._spawn_pty(
        inst, proj, "alpha", tmp_path / "alpha.log", "default", resume=False
    )

    assert out.status is InstanceStatus.ERROR
    assert out.error_detail == "openpty failed: boom"  # the keeper's reason, surfaced
