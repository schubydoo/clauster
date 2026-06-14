"""Runner wiring for the resume-recap feature: spawn-time hook install + env flag."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from clauster.config import ClausterConfig
from clauster.runner import SessionRunner


def _runner_with_recap(runner_config, *, enabled: bool) -> tuple[SessionRunner, Path]:
    config, claude_json = runner_config
    recap_config = ClausterConfig(
        projects_root=config.projects_root,
        state_dir=config.state_dir,
        claude={"binary": config.claude.binary, "resume_recap": enabled},
    )
    return SessionRunner(recap_config, claude_json=claude_json), claude_json


async def test_spawn_installs_recap_hook_when_enabled(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner, _ = _runner_with_recap(runner_config, enabled=True)

    await runner.spawn("alpha")
    try:
        settings = runner._settings_json
        assert settings.is_file()
        data = json.loads(settings.read_text())
        commands = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
        assert any("resume_recap.py" in c for c in commands)
    finally:
        await runner.stop("alpha")


async def test_spawn_skips_recap_hook_when_disabled(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner, _ = _runner_with_recap(runner_config, enabled=False)

    await runner.spawn("alpha")
    try:
        assert not runner._settings_json.exists()
    finally:
        await runner.stop("alpha")


async def test_spawn_survives_recap_hook_install_failure(runner_config, monkeypatch):
    """A failed hook install is best-effort: the bridge still spawns."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner, _ = _runner_with_recap(runner_config, enabled=True)

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("clauster.runner.ensure_recap_hook_installed", _boom)
    inst = await runner.spawn("alpha")
    try:
        assert inst.status.name == "RUNNING"
    finally:
        await runner.stop("alpha")


def _capture_popen_env(runner: SessionRunner, monkeypatch, tmp_path: Path) -> dict | None:
    captured: dict = {}

    class _FakeProc:
        pid = 4321

    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    runner._popen(
        runner._config.projects_root / "alpha",
        tmp_path / "bridge.log",
        "alpha",
        "same-dir",
        "default",
    )
    return captured["env"]


def test_popen_injects_recap_env_when_enabled(runner_config, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET", "must-not-leak-to-the-bridge")
    runner, _ = _runner_with_recap(runner_config, enabled=True)
    env = _capture_popen_env(runner, monkeypatch, tmp_path)
    assert env is not None
    assert env["CLAUSTER_RESUME_RECAP"] == "1"
    assert env["CLAUSTER_RESUME_RECAP_MAX_CHARS"] == "8000"
    assert "CLAUSTER_SESSION_SECRET" not in env  # secret scrubbed even with recap on


def test_popen_scrubs_secret_and_omits_recap_when_disabled(runner_config, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET", "must-not-leak-to-the-bridge")
    monkeypatch.setenv("ORDINARY_BRIDGE_ENV", "kept")
    runner, _ = _runner_with_recap(runner_config, enabled=False)
    env = _capture_popen_env(runner, monkeypatch, tmp_path)
    # The bridge env is now the SCRUBBED parent environment (never None): the
    # session secret is gone, no recap flags are set, ordinary vars survive.
    assert env is not None
    assert "CLAUSTER_SESSION_SECRET" not in env
    assert "CLAUSTER_RESUME_RECAP" not in env
    assert env["ORDINARY_BRIDGE_ENV"] == "kept"  # a non-secret var propagates (cross-platform)
