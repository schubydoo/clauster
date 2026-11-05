"""Runner wiring for config-driven bridge PATH/env (`claude.path_append` / `claude.env`)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from clauster import procutil
from clauster.config import ClausterConfig
from clauster.runner import SessionRunner


def _runner_with_env(
    runner_config, *, path_append=None, env=None, node_from_nvm=False
) -> SessionRunner:
    config, claude_json = runner_config
    extended = ClausterConfig(
        projects_root=config.projects_root,
        state_dir=config.state_dir,
        claude={
            "binary": config.claude.binary,
            "path_append": path_append or [],
            "env": env or {},
            "node_from_nvm": node_from_nvm,
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
        runner._popen_keeper(
            cwd,
            tmp_path / "bridge.keeper.json",
            ["claude", "--remote-control"],
            state_dir=tmp_path / "state",
        )
    return captured["env"]


def test_popen_appends_path_and_overlays_env(runner_config, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/u")
    runner = _runner_with_env(
        runner_config, path_append=["~/.local/bin", "/opt/tools"], env={"FOO": "bar"}
    )
    env = _capture_env("popen", runner, monkeypatch, tmp_path)
    assert env is not None
    local_bin = os.path.expanduser("~/.local/bin")  # USERPROFILE-resolved on Windows
    assert env["PATH"] == os.pathsep.join(["/usr/bin", local_bin, "/opt/tools"])
    assert env["FOO"] == "bar"


def test_popen_keeper_appends_path_and_overlays_env(runner_config, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/u")
    runner = _runner_with_env(runner_config, path_append=["~/.local/bin"], env={"FOO": "bar"})
    env = _capture_env("keeper", runner, monkeypatch, tmp_path)
    # The keeper inherits the extended env; inside it the bridge re-derives child_env
    # from this os.environ, so the pty bridge gets the same PATH/env as the standard path.
    assert env is not None
    local_bin = os.path.expanduser("~/.local/bin")  # USERPROFILE-resolved on Windows
    assert env["PATH"] == os.pathsep.join(["/usr/bin", local_bin])
    assert env["FOO"] == "bar"


_MEI = "/tmp/_MEI448run"


def _frozen_linux_bundle(monkeypatch, *, user_ld: str | None) -> None:
    """Act as the standalone binary's one-file child, with the env its bootloader set."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    # Pin the Linux library-path rule without flipping sys.platform for the spawn code.
    monkeypatch.setattr(procutil, "_bundle_library_path_var", lambda: "LD_LIBRARY_PATH")
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", "/usr/local/bin/clauster")
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", _MEI)
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    if user_ld is None:
        monkeypatch.setenv("LD_LIBRARY_PATH", _MEI)
        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    else:
        monkeypatch.setenv("LD_LIBRARY_PATH", f"{_MEI}:{user_ld}")
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", user_ld)


def test_frozen_standard_bridge_gets_the_users_env_not_the_bundles(
    runner_config, monkeypatch, tmp_path
):
    # Issue 1708: the standard bridge, and every session and tool under it, must see the
    # user's own LD_LIBRARY_PATH and no PyInstaller state.
    _frozen_linux_bundle(monkeypatch, user_ld="/opt/lib")
    env = _capture_env("popen", _runner_with_env(runner_config), monkeypatch, tmp_path)
    assert env is not None
    assert env["LD_LIBRARY_PATH"] == "/opt/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in env
    assert not [name for name in env if name.startswith("_PYI_")]


def test_frozen_standard_bridge_drops_a_bundle_only_library_path(
    runner_config, monkeypatch, tmp_path
):
    _frozen_linux_bundle(monkeypatch, user_ld=None)
    env = _capture_env("popen", _runner_with_env(runner_config), monkeypatch, tmp_path)
    assert env is not None
    assert "LD_LIBRARY_PATH" not in env


def test_frozen_operator_library_path_reaches_both_bridge_kinds(
    runner_config, monkeypatch, tmp_path
):
    # claude.env wins on both paths: directly for the standard bridge, and via the
    # keeper's *_ORIG (what its own child_env restores) for the pty bridge.
    _frozen_linux_bundle(monkeypatch, user_ld="/opt/lib")
    runner = _runner_with_env(runner_config, env={"LD_LIBRARY_PATH": "/operator/lib"})
    assert _capture_env("popen", runner, monkeypatch, tmp_path)["LD_LIBRARY_PATH"] == (
        "/operator/lib"
    )
    keeper = _capture_env("keeper", runner, monkeypatch, tmp_path)
    assert keeper["LD_LIBRARY_PATH_ORIG"] == "/operator/lib"


def test_frozen_keeper_launch_stays_inside_this_bundle(runner_config, monkeypatch, tmp_path):
    # The keeper is `<exe> __pty-keeper__`: it keeps this bundle's runtime so it runs as one
    # process whose pid is the one the runner records; its OWN child_env cleans the bridge.
    _frozen_linux_bundle(monkeypatch, user_ld="/opt/lib")
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET", "must-not-leak")
    env = _capture_env("keeper", _runner_with_env(runner_config), monkeypatch, tmp_path)
    assert env is not None
    assert env["_PYI_APPLICATION_HOME_DIR"] == _MEI
    assert env["LD_LIBRARY_PATH"] == f"{_MEI}:/opt/lib"
    assert env["LD_LIBRARY_PATH_ORIG"] == "/opt/lib"
    assert "CLAUSTER_SESSION_SECRET" not in env


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


def test_popen_prepends_resolved_nvm_bin_dir_before_base_path(
    runner_config, monkeypatch, tmp_path
):
    # #1018: claude.node_from_nvm=True PREPENDS the resolved nvm default node bin dir,
    # BEFORE the inherited PATH, so nvm's node wins over a distro node already on PATH
    # (e.g. /usr/bin/node). Operator path_append entries still append after the base.
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(
        procutil, "resolve_nvm_default_node_bin_dir", lambda: "/home/u/.nvm/versions/node/v24/bin"
    )
    runner = _runner_with_env(runner_config, path_append=["/opt/tools"], node_from_nvm=True)
    env = _capture_env("popen", runner, monkeypatch, tmp_path)
    assert env is not None
    assert env["PATH"] == os.pathsep.join(
        ["/home/u/.nvm/versions/node/v24/bin", "/usr/bin", "/opt/tools"]
    )


def test_popen_node_from_nvm_off_never_resolves(runner_config, monkeypatch, tmp_path):
    # Off (the default): the resolver must not even be called — no bash/nvm subprocess
    # spawned on a hot path when the operator hasn't opted in.
    monkeypatch.setenv("PATH", "/usr/bin")

    def _boom():
        raise AssertionError("resolve_nvm_default_node_bin_dir must not be called when off")

    monkeypatch.setattr(procutil, "resolve_nvm_default_node_bin_dir", _boom)
    runner = _runner_with_env(runner_config, node_from_nvm=False)
    env = _capture_env("popen", runner, monkeypatch, tmp_path)
    assert env is not None
    assert env["PATH"] == "/usr/bin"


def test_popen_node_from_nvm_no_op_when_unresolvable(runner_config, monkeypatch, tmp_path):
    # nvm/default unavailable: resolver returns None, PATH is left exactly as path_append
    # alone would produce it — the knob never blocks or corrupts a spawn.
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(procutil, "resolve_nvm_default_node_bin_dir", lambda: None)
    runner = _runner_with_env(runner_config, path_append=["/opt/tools"], node_from_nvm=True)
    env = _capture_env("popen", runner, monkeypatch, tmp_path)
    assert env is not None
    assert env["PATH"] == os.pathsep.join(["/usr/bin", "/opt/tools"])


def test_popen_keeper_prepends_resolved_nvm_bin_dir(runner_config, monkeypatch, tmp_path):
    # The pty spawn path (_popen_keeper) goes through the same _bridge_env_overlay, so
    # the pty bridge gets the same nvm-resolved PATH as the standard bridge — nvm's node
    # dir prepended before the inherited PATH so it wins over a distro node (#1018).
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(
        procutil, "resolve_nvm_default_node_bin_dir", lambda: "/home/u/.nvm/versions/node/v24/bin"
    )
    runner = _runner_with_env(runner_config, node_from_nvm=True)
    env = _capture_env("keeper", runner, monkeypatch, tmp_path)
    assert env is not None
    assert env["PATH"] == os.pathsep.join(["/home/u/.nvm/versions/node/v24/bin", "/usr/bin"])


def test_nvm_bin_dir_resolved_once_and_cached(runner_config, monkeypatch):
    # Greptile #803: the nvm lookup shells bash (seconds on a slow mount), so it must be
    # resolved ONCE per process and cached — not re-run on every spawn. Count the calls
    # across several _bridge_env_overlay builds; the resolver fires exactly once.
    calls = {"n": 0}

    def _counting():
        calls["n"] += 1
        return "/home/u/.nvm/versions/node/v20/bin"

    monkeypatch.setattr(procutil, "resolve_nvm_default_node_bin_dir", _counting)
    runner = _runner_with_env(runner_config, node_from_nvm=True)
    first = runner._bridge_env_overlay()
    runner._bridge_env_overlay()
    runner._bridge_env_overlay()
    assert calls["n"] == 1  # resolved once, then served from cache
    assert "/home/u/.nvm/versions/node/v20/bin" in first["PATH"]
