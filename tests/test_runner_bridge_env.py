"""Runner wiring for config-driven bridge PATH/env (`claude.path_append` / `claude.env`)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from clauster.config import ClausterConfig
from clauster.runner import SessionRunner


def _runner_with_env(runner_config, *, path_append=None, env=None) -> SessionRunner:
    config, claude_json = runner_config
    extended = ClausterConfig(
        projects_root=config.projects_root,
        state_dir=config.state_dir,
        claude={
            "binary": config.claude.binary,
            "path_append": path_append or [],
            "env": env or {},
        },
    )
    return SessionRunner(extended, claude_json=claude_json)


def _capture_env(target, runner: SessionRunner, monkeypatch, tmp_path: Path) -> dict | None:
    """Run ``target`` (`_popen` or `_popen_keeper`) with Popen stubbed; return its env kwarg."""
    captured: dict = {}

    class _FakeProc:
        pid = 4321

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    cwd = runner._config.projects_root / "alpha"
    if target == "popen":
        runner._popen(cwd, tmp_path / "bridge.log", "alpha", "same-dir", "default")
    else:
        runner._popen_keeper(cwd, tmp_path / "bridge.keeper.json", ["claude", "--remote-control"])
    return captured["env"]


def test_popen_appends_path_and_overlays_env(runner_config, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/u")
    runner = _runner_with_env(
        runner_config, path_append=["~/.local/bin", "/opt/tools"], env={"FOO": "bar"}
    )
    env = _capture_env("popen", runner, monkeypatch, tmp_path)
    assert env is not None
    assert env["PATH"] == os.pathsep.join(["/usr/bin", "/home/u/.local/bin", "/opt/tools"])
    assert env["FOO"] == "bar"


def test_popen_keeper_appends_path_and_overlays_env(runner_config, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/u")
    runner = _runner_with_env(runner_config, path_append=["~/.local/bin"], env={"FOO": "bar"})
    env = _capture_env("keeper", runner, monkeypatch, tmp_path)
    # The keeper inherits the extended env; inside it the bridge re-derives child_env
    # from this os.environ, so the pty bridge gets the same PATH/env as the standard path.
    assert env is not None
    assert env["PATH"] == os.pathsep.join(["/usr/bin", "/home/u/.local/bin"])
    assert env["FOO"] == "bar"


def test_bridge_env_drops_secret_named_config_env(runner_config, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin")
    # A config env map naming a Clauster secret must not re-introduce it past the scrub.
    runner = _runner_with_env(runner_config, env={"CLAUSTER_SESSION_SECRET": "must-not-leak"})
    env = _capture_env("popen", runner, monkeypatch, tmp_path)
    assert env is not None
    assert "CLAUSTER_SESSION_SECRET" not in env


def test_popen_leaves_path_untouched_without_path_append(runner_config, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin")
    runner = _runner_with_env(runner_config)  # no path_append / env
    env = _capture_env("popen", runner, monkeypatch, tmp_path)
    assert env is not None
    assert env["PATH"] == "/usr/bin"  # inherited PATH passes through unchanged
