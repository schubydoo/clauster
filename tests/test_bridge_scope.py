"""Per-bridge task limit: host probe, argv wrapping, both launch paths, and doctor."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys

import pytest
from pydantic import ValidationError

from clauster import bridge_launch, bridge_scope, ops
from clauster.config import ClausterConfig, InstanceDefaults
from clauster.runner import SessionRunner

SUPPORT = bridge_scope.ScopeSupport(
    True, "ok", "/usr/bin/env", "/usr/bin/systemd-run", "/run/user/1000"
)


@pytest.fixture
def host(monkeypatch, tmp_path):
    """Fake a Linux host that meets every requirement. A test then removes one."""
    runtime = tmp_path / "run"
    linger = tmp_path / "linger"
    (runtime / "1000").mkdir(parents=True)
    linger.mkdir()
    (linger / "svc").touch()
    monkeypatch.setattr(bridge_scope.sys, "platform", "linux")
    monkeypatch.setattr(bridge_scope.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(bridge_scope, "_user_name", lambda uid: "svc")
    monkeypatch.setattr(bridge_scope, "_uid", lambda: 1000)
    monkeypatch.setattr(bridge_scope, "_RUNTIME_ROOT", runtime)
    monkeypatch.setattr(bridge_scope, "_LINGER_DIR", linger)
    monkeypatch.setattr(bridge_scope, "_is_socket", lambda path: path.name == "bus")
    monkeypatch.setattr(bridge_scope, "_preflight", lambda *args: None)
    return linger


def test_probe_available(host, tmp_path):
    support = bridge_scope.probe_scope_support()
    assert support.available
    assert support.systemd_run == "/usr/bin/systemd-run"
    assert support.env_binary == "/usr/bin/env"
    assert support.runtime_dir == str(tmp_path / "run" / "1000")


def test_probe_refuses_non_linux(host, monkeypatch):
    monkeypatch.setattr(bridge_scope.sys, "platform", "darwin")
    assert not bridge_scope.probe_scope_support().available


@pytest.mark.parametrize("missing", ["systemd-run", "env", "true"])
def test_probe_refuses_missing_binary(host, monkeypatch, missing):
    monkeypatch.setattr(
        bridge_scope.shutil,
        "which",
        lambda name: None if name == missing else f"/usr/bin/{name}",
    )
    support = bridge_scope.probe_scope_support()
    assert not support.available
    assert support.detail == f"{missing} is not on PATH"


def test_probe_refuses_unknown_user(host, monkeypatch):
    def _no_user(uid):
        raise KeyError("no such uid")

    monkeypatch.setattr(bridge_scope, "_user_name", _no_user)
    support = bridge_scope.probe_scope_support()
    assert not support.available
    assert "identify" in support.detail


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no pwd module")
def test_user_name_ignores_the_user_variable(monkeypatch):
    import os
    import pwd

    monkeypatch.setenv("USER", "someone-else")
    monkeypatch.setenv("LOGNAME", "someone-else")
    assert bridge_scope._user_name(os.getuid()) == pwd.getpwuid(os.getuid()).pw_name


def test_probe_refuses_missing_bus(host, monkeypatch):
    monkeypatch.setattr(bridge_scope, "_is_socket", lambda path: False)
    support = bridge_scope.probe_scope_support()
    assert not support.available
    assert "user bus" in support.detail


def test_probe_reports_a_failed_test_scope(host, monkeypatch):
    monkeypatch.setattr(bridge_scope, "_preflight", lambda *args: "test scope failed: denied")
    support = bridge_scope.probe_scope_support()
    assert not support.available
    assert support.detail == "test scope failed: denied"


def _preflight(monkeypatch, result):
    seen = {}

    def _run(argv, **kwargs):
        seen["argv"] = argv
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(bridge_scope.subprocess, "run", _run)
    outcome = bridge_scope._preflight("/e", "/s", "/t", "/run/user/1")
    return outcome, seen["argv"]


def test_preflight_success_uses_a_limited_empty_scope(monkeypatch):
    done = subprocess.CompletedProcess([], 0, "", "Running as unit: x.scope")
    outcome, argv = _preflight(monkeypatch, done)
    assert outcome is None
    assert "--quiet" not in argv  # --quiet hides why a scope job failed
    assert "TasksMax=16" in argv and argv[-1] == "/t"
    assert any(a.startswith("--unit=clauster-bridge-probe-") for a in argv)


def test_preflight_reports_the_last_error_line(monkeypatch):
    done = subprocess.CompletedProcess([], 1, "", "first\nJob failed: denied\n")
    outcome, _argv = _preflight(monkeypatch, done)
    assert outcome == "test scope failed: Job failed: denied"


def test_preflight_redacts_the_reason(monkeypatch):
    secret = "session_01ABCDEFGHJKMNPQRSTVWXYZ01"
    done = subprocess.CompletedProcess([], 1, "", f"failed\x1b[1m{secret}\n")
    outcome, _argv = _preflight(monkeypatch, done)
    assert secret not in outcome


def test_preflight_reports_an_exit_with_no_output(monkeypatch):
    outcome, _argv = _preflight(monkeypatch, subprocess.CompletedProcess([], 7, "", ""))
    assert outcome == "test scope failed: exit 7"


@pytest.mark.parametrize(
    "error", [OSError("no exec"), subprocess.TimeoutExpired("systemd-run", 10)]
)
def test_preflight_reports_a_launch_failure(monkeypatch, error):
    outcome, _argv = _preflight(monkeypatch, error)
    assert outcome is not None and outcome.startswith("test scope failed:")


def test_scoped_argv_refuses_a_binary_path_with_equals(monkeypatch, caplog):
    def _fail():
        raise AssertionError("must not probe")

    monkeypatch.setattr(bridge_scope, "probe_scope_support", _fail)
    with caplog.at_level(logging.WARNING, logger="clauster.bridge_scope"):
        argv = bridge_scope.scoped_argv(
            ["/opt/a=b/claude"], limit=500, project="p", bridge_runtime_dir=None, support=None
        )
    assert argv == ["/opt/a=b/claude"]
    assert "contains '='" in caplog.text


def test_probe_reports_an_unreadable_linger_state(host, monkeypatch):
    class _Unreadable:
        def __truediv__(self, _name):
            return self

        def exists(self):
            raise PermissionError("denied")

    monkeypatch.setattr(bridge_scope, "_LINGER_DIR", _Unreadable())
    support = bridge_scope.probe_scope_support()
    assert not support.available
    assert "cannot read the linger state" in support.detail


def test_probe_refuses_without_linger(host):
    (host / "svc").unlink()
    support = bridge_scope.probe_scope_support()
    assert not support.available
    assert "enable-linger svc" in support.detail


def test_is_socket_treats_oserror_as_absent(tmp_path):
    class _Unreadable:
        def is_socket(self):
            raise OSError("denied")

    assert bridge_scope._is_socket(_Unreadable()) is False
    assert bridge_scope._is_socket(tmp_path / "missing") is False


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no os.getuid")
def test_uid_is_the_process_uid():
    import os

    assert bridge_scope._uid() == os.getuid()


def test_wrap_argv_runs_the_bridge_in_a_limited_scope():
    bridge = ["/opt/claude", "remote-control", "--name", "x"]
    argv = bridge_scope.wrap_argv(
        bridge, limit=30000, project="alpha", support=SUPPORT, bridge_runtime_dir=None
    )
    assert argv[:6] == [
        "/usr/bin/env",
        "XDG_RUNTIME_DIR=/run/user/1000",
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",  # a banner on the pty would hide the login screen from the keeper
    ]
    assert "--slice=clauster_bridges.slice" in argv
    assert argv[argv.index("-p") + 1] == "TasksMax=30000"
    assert argv[-7:] == ["/usr/bin/env", "-u", "XDG_RUNTIME_DIR", *bridge]


def test_wrap_argv_restores_the_bridge_runtime_dir():
    argv = bridge_scope.wrap_argv(
        ["/c"], limit=128, project="p", support=SUPPORT, bridge_runtime_dir="/custom"
    )
    assert argv[-3:] == ["/usr/bin/env", "XDG_RUNTIME_DIR=/custom", "/c"]


@pytest.mark.parametrize("project", ["my project", "a/b;rm -rf", "é.x-y", "", "n" * 200])
def test_wrap_argv_unit_name_is_always_safe(project):
    argv = bridge_scope.wrap_argv(
        ["/c"], limit=128, project=project, support=SUPPORT, bridge_runtime_dir=None
    )
    unit = next(a for a in argv if a.startswith("--unit=")).removeprefix("--unit=")
    assert unit.startswith("clauster-bridge-") and unit.endswith(".scope")
    assert len(unit) < 100
    assert all(c.isascii() and (c.isalnum() or c in "_-.") for c in unit)


def test_wrap_argv_unit_names_are_unique():
    def unit():
        argv = bridge_scope.wrap_argv(
            ["/c"], limit=128, project="p", support=SUPPORT, bridge_runtime_dir=None
        )
        return next(a for a in argv if a.startswith("--unit="))

    assert unit() != unit()


def test_scoped_argv_without_limit_does_not_probe(monkeypatch):
    def _fail():
        raise AssertionError("must not probe")

    monkeypatch.setattr(bridge_scope, "probe_scope_support", _fail)
    argv = ["/opt/claude"]
    assert (
        bridge_scope.scoped_argv(
            argv, limit=None, project="p", bridge_runtime_dir=None, support=None
        )
        is argv
    )


def test_scoped_argv_falls_back_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(
        bridge_scope,
        "probe_scope_support",
        lambda: bridge_scope.ScopeSupport(False, "linger is off"),
    )
    with caplog.at_level(logging.WARNING, logger="clauster.bridge_scope"):
        assert bridge_scope.scoped_argv(
            ["/c"], limit=500, project="p", bridge_runtime_dir=None, support=None
        ) == ["/c"]
    assert "without a task limit: linger is off" in caplog.text


def test_scoped_argv_wraps_when_supported(monkeypatch):
    monkeypatch.setattr(bridge_scope, "probe_scope_support", lambda: SUPPORT)
    argv = bridge_scope.scoped_argv(
        ["/c"], limit=500, project="p", bridge_runtime_dir=None, support=None
    )
    assert argv[0] == "/usr/bin/env" and argv[-1] == "/c"


@pytest.mark.parametrize("value", [127, 4194305, 0, -1])
def test_config_rejects_out_of_range_limit(value):
    with pytest.raises(ValidationError):
        InstanceDefaults(bridge_task_limit=value)


def test_config_limit_is_off_by_default():
    assert InstanceDefaults().bridge_task_limit is None


def _runner(runner_config, limit) -> SessionRunner:
    config, claude_json = runner_config
    extended = ClausterConfig(
        projects_root=config.projects_root,
        state_dir=config.state_dir,
        claude={"binary": config.claude.binary, "node_from_nvm": False},
        instance_defaults={"bridge_task_limit": limit},
    )
    return SessionRunner(extended, claude_json=claude_json)


def _capture(monkeypatch):
    seen: dict = {}

    class _Proc:
        pid = 4321

    def _popen(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", _popen)
    return seen


def test_standard_bridge_is_scoped(runner_config, monkeypatch, tmp_path):
    monkeypatch.setattr(bridge_scope, "probe_scope_support", lambda: SUPPORT)
    runner = _runner(runner_config, 30000)
    seen = _capture(monkeypatch)
    cwd = runner._config.projects_root / "alpha"
    runner._popen(cwd, tmp_path / "bridge.log", "alpha", "same-dir", "default")
    assert seen["cmd"][0] == "/usr/bin/env"
    assert "TasksMax=30000" in seen["cmd"]
    assert "remote-control" in seen["cmd"]
    assert any(a.startswith("--unit=clauster-bridge-alpha-") for a in seen["cmd"])


def test_standard_bridge_unscoped_without_limit(runner_config, monkeypatch, tmp_path):
    runner = _runner(runner_config, None)
    seen = _capture(monkeypatch)
    cwd = runner._config.projects_root / "alpha"
    runner._popen(cwd, tmp_path / "bridge.log", "alpha", "same-dir", "default")
    assert not any("systemd-run" in a for a in seen["cmd"])


def test_pty_scope_wraps_the_bridge_and_not_the_keeper(runner_config, monkeypatch, tmp_path):
    monkeypatch.setattr(bridge_scope, "probe_scope_support", lambda: SUPPORT)
    runner = _runner(runner_config, 30000)
    seen = _capture(monkeypatch)
    runner._popen_keeper(
        runner._config.projects_root / "alpha",
        tmp_path / "bridge.keeper.json",
        ["/opt/claude", "--remote-control"],
        state_dir=tmp_path / "state",
    )
    cmd = seen["cmd"]
    split = cmd.index("--")
    assert not any("systemd-run" in a for a in cmd[:split])
    assert cmd[split + 1] == "/usr/bin/env"
    assert cmd[-2:] == ["/opt/claude", "--remote-control"]


def test_pty_bridge_unscoped_without_limit(runner_config, monkeypatch, tmp_path):
    runner = _runner(runner_config, None)
    seen = _capture(monkeypatch)
    runner._popen_keeper(
        runner._config.projects_root / "alpha",
        tmp_path / "bridge.keeper.json",
        ["/opt/claude", "--remote-control"],
        state_dir=tmp_path / "state",
    )
    assert not any("systemd-run" in a for a in seen["cmd"])


def _config(tmp_path, limit) -> ClausterConfig:
    return ClausterConfig(
        projects_root=tmp_path,
        state_dir=tmp_path / "state",
        instance_defaults={"bridge_task_limit": limit},
    )


def test_doctor_silent_without_limit(tmp_path):
    assert ops._check_bridge_task_limit(_config(tmp_path, None)) is None


def test_doctor_ok_when_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_scope, "probe_scope_support", lambda: SUPPORT)
    monkeypatch.setattr(ops.task_limits, "bridge_slice_refused", lambda: None)
    check = ops._check_bridge_task_limit(_config(tmp_path, 30000))
    assert check is not None and check.status == ops.OK
    assert "30000" in check.detail


def test_doctor_warns_when_limit_cannot_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bridge_scope,
        "probe_scope_support",
        lambda: bridge_scope.ScopeSupport(False, "linger is off"),
    )
    check = ops._check_bridge_task_limit(_config(tmp_path, 30000))
    assert check is not None and check.status == ops.WARN
    assert "NO limit" in check.detail and "linger is off" in check.detail


def test_doctor_output_includes_the_task_limit_check(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bridge_scope,
        "probe_scope_support",
        lambda: bridge_scope.ScopeSupport(False, "linger is off"),
    )
    (tmp_path / "projects").mkdir()
    path = tmp_path / "clauster.yml"
    path.write_text(
        f"schema_version: 1\nprojects_root: {(tmp_path / 'projects').as_posix()}\n"
        f"state_dir: {(tmp_path / 'state').as_posix()}\n"
        "instance_defaults:\n  bridge_task_limit: 30000\n",
        encoding="utf-8",
    )
    checks, _ok = ops.run_doctor(path)
    check = next(c for c in checks if c.name == "bridge task limit")
    assert check.status == ops.WARN


def test_preflight_survives_undecodable_stderr(tmp_path, monkeypatch):
    # Real decoding: the fake program writes a byte that is invalid UTF-8.
    script = tmp_path / "fail.py"
    script.write_text(
        "import sys\nsys.stderr.buffer.write(b'bad \\xff byte\\n')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    real_run = subprocess.run

    def _run(_argv, **kwargs):
        kwargs["encoding"] = "utf-8"
        return real_run([sys.executable, str(script)], **kwargs)

    monkeypatch.setattr(bridge_scope.subprocess, "run", _run)
    outcome = bridge_scope._preflight("/e", "/s", "/t", "/run/user/1")
    assert outcome is not None and outcome.startswith("test scope failed: bad")


def test_scoped_argv_uses_a_given_probe_result(monkeypatch):
    def _fail():
        raise AssertionError("must not probe")

    monkeypatch.setattr(bridge_scope, "probe_scope_support", _fail)
    argv = bridge_scope.scoped_argv(
        ["/c"], limit=500, project="p", bridge_runtime_dir=None, support=SUPPORT
    )
    assert argv[0] == "/usr/bin/env"


async def test_launch_worker_uses_the_probe_of_its_spawn(runner_config, monkeypatch):
    runner = _runner(runner_config, 500)
    calls = []

    def _probe():
        calls.append(1)
        return SUPPORT

    monkeypatch.setattr(bridge_scope, "probe_scope_support", _probe)
    await runner._spawner._prepare_bridge_scope()
    cwd = runner._config.projects_root / "alpha"
    argv = await asyncio.to_thread(runner._launch._scoped_argv, ["/c"], cwd)
    assert len(calls) == 1  # the worker did not probe again
    assert argv[0] == "/usr/bin/env"


async def test_scope_probe_runs_before_the_bridge_cap(runner_config, monkeypatch):
    runner = _runner(runner_config, 500)
    order = []

    class _Stop(Exception):
        pass

    def _probe():
        order.append("probe")
        return SUPPORT

    def _cap(_max):
        order.append("cap")
        raise _Stop

    monkeypatch.setattr(runner._launch, "prepare_scope", _probe)
    monkeypatch.setattr(runner._spawner, "_enforce_bridge_cap", _cap)
    with pytest.raises(_Stop):
        await runner.spawn("alpha")
    assert order == ["probe", "cap"]


@pytest.mark.parametrize(
    "resume_mode",
    [
        "standard",
        pytest.param(
            "pty",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="pty is gated on Windows"),
        ),
    ],
)
async def test_cancelled_scope_probe_launches_nothing(runner_config, monkeypatch, resume_mode):
    runner = _runner(runner_config, 500)
    launched = []

    def _cancel():
        raise asyncio.CancelledError

    monkeypatch.setattr(runner._launch, "prepare_scope", _cancel)
    monkeypatch.setattr(runner._launch, "_popen", lambda *a, **k: launched.append(a))
    monkeypatch.setattr(runner._launch, "_popen_keeper", lambda *a, **k: launched.append(a))
    with pytest.raises(asyncio.CancelledError):
        await runner.spawn("alpha", resume_mode=resume_mode)
    assert launched == []
    assert not runner._instances  # no row is left in STARTING
    log_dir = runner._launch._log_dir
    assert not log_dir.exists() or not list(log_dir.iterdir())  # and no log file


async def test_no_limit_skips_the_scope_probe(runner_config, monkeypatch):
    runner = _runner(runner_config, None)

    def _fail():
        raise AssertionError("must not probe")

    monkeypatch.setattr(runner._launch, "prepare_scope", _fail)
    await runner._spawner._prepare_bridge_scope()


def test_doctor_warns_for_a_binary_path_with_equals(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.claude_cli, "resolve_binary", lambda _b: "/opt/a=b/claude")
    monkeypatch.setattr(bridge_scope, "probe_scope_support", lambda: SUPPORT)
    check = ops._check_bridge_task_limit(_config(tmp_path, 30000))
    assert check is not None and check.status == ops.WARN
    assert "NO limit" in check.detail and "contains '='" in check.detail


def test_doctor_leaves_a_missing_binary_to_the_claude_check(tmp_path, monkeypatch):
    def _missing(_binary):
        raise ops.claude_cli.ClaudeNotFound("missing")

    monkeypatch.setattr(ops.claude_cli, "resolve_binary", _missing)
    monkeypatch.setattr(bridge_scope, "probe_scope_support", lambda: SUPPORT)
    monkeypatch.setattr(ops.task_limits, "bridge_slice_refused", lambda: None)
    check = ops._check_bridge_task_limit(_config(tmp_path, 30000))
    assert check is not None and check.status == ops.OK


@pytest.mark.parametrize(
    "resume_mode",
    [
        "standard",
        pytest.param(
            "pty",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="pty is gated on Windows"),
        ),
    ],
)
async def test_real_launch_worker_reuses_the_spawn_probe(runner_config, monkeypatch, resume_mode):
    runner = _runner(runner_config, 500)
    probes = []
    launched = []
    real_popen = subprocess.Popen

    def _probe():
        probes.append(1)
        return SUPPORT

    def _popen(cmd, *args, **kwargs):
        if any(a in ("remote-control", "--remote-control") for a in cmd):
            launched.append(cmd)
            raise OSError("stop after the launch argv is known")
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(bridge_scope, "probe_scope_support", _probe)
    monkeypatch.setattr(subprocess, "Popen", _popen)
    await runner.spawn("alpha", resume_mode=resume_mode)
    assert len(probes) == 1
    assert len(launched) == 1 and any("TasksMax=500" in a for a in launched[0])


async def test_spawn_warns_when_the_bridge_gets_no_limit(runner_config, monkeypatch):
    runner = _runner(runner_config, 500)
    monkeypatch.setattr(
        bridge_scope,
        "probe_scope_support",
        lambda: bridge_scope.ScopeSupport(False, "linger is off"),
    )
    assert (
        await runner._spawner._prepare_bridge_scope()
        == "This bridge has no task limit: linger is off"
    )


async def test_spawn_warns_for_a_binary_path_with_equals(runner_config, monkeypatch):
    runner = _runner(runner_config, 500)
    monkeypatch.setattr(bridge_scope, "probe_scope_support", lambda: SUPPORT)
    monkeypatch.setattr(bridge_launch, "resolve_binary", lambda _b: "/opt/a=b/claude")
    warning = await runner._spawner._prepare_bridge_scope()
    assert warning is not None and "contains '='" in warning


async def test_no_warning_when_the_bridge_gets_its_limit(runner_config, monkeypatch):
    runner = _runner(runner_config, 500)
    monkeypatch.setattr(bridge_scope, "probe_scope_support", lambda: SUPPORT)
    monkeypatch.setattr(bridge_launch, "resolve_binary", lambda _b: "/opt/claude")
    assert await runner._spawner._prepare_bridge_scope() is None


def test_scope_problem_tolerates_a_missing_binary(runner_config, monkeypatch):
    runner = _runner(runner_config, 500)

    def _missing(_binary):
        raise bridge_launch.ClaudeNotFound("missing")

    monkeypatch.setattr(bridge_launch, "resolve_binary", _missing)
    assert runner._launch.scope_problem(SUPPORT) is None


async def test_spawn_outcome_carries_the_no_limit_warning(runner_config, monkeypatch):
    runner = _runner(runner_config, 500)
    real_popen = subprocess.Popen

    def _popen(cmd, *args, **kwargs):
        if "remote-control" in cmd:
            raise OSError("stop at the launch")
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(
        bridge_scope,
        "probe_scope_support",
        lambda: bridge_scope.ScopeSupport(False, "linger is off"),
    )
    monkeypatch.setattr(subprocess, "Popen", _popen)
    outcome = await runner.spawn_detailed("alpha")
    assert "This bridge has no task limit: linger is off" in outcome.warnings
