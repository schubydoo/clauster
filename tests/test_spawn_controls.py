"""v0.2 spawn controls — spawn-mode + permission-mode pickers (footgun-gated).

Runner-level happy paths use the fake `claude` binary (via ``runner_config``).
App-level tests exercise only the *rejection* paths, which short-circuit in
validation before any process is spawned — so they never invoke a real binary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import ClausterConfig, ProjectConfig, load_config
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import (
    InvalidSpawnOption,
    PermissionModeNotAllowed,
    SessionRunner,
)


def _runner(runner_config) -> SessionRunner:
    config, claude_json = runner_config
    return SessionRunner(config, claude_json=claude_json)


# ----- _build_cmd (pure) ------------------------------------------------

def test_build_cmd_includes_spawn_and_permission_flags(runner_config):
    runner = _runner(runner_config)
    cmd = runner._build_cmd(Path("/tmp/x.log"), "alpha", "worktree", "plan")
    assert cmd[cmd.index("--name") + 1] == "alpha"
    assert cmd[cmd.index("--spawn") + 1] == "worktree"
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"


# ----- validation -------------------------------------------------------

async def test_invalid_spawn_mode_rejected(runner_config):
    runner = _runner(runner_config)
    with pytest.raises(InvalidSpawnOption):
        await runner.spawn("alpha", spawn_mode="bogus")


async def test_invalid_permission_mode_rejected(runner_config):
    runner = _runner(runner_config)
    with pytest.raises(InvalidSpawnOption):
        await runner.spawn("alpha", permission_mode="nope")


async def test_worktree_on_non_git_rejected(runner_config):
    runner = _runner(runner_config)
    # beta has a CLAUDE.md but no .git -> worktree must be refused.
    with pytest.raises(InvalidSpawnOption):
        await runner.spawn("beta", spawn_mode="worktree")


async def test_worktree_on_git_allowed(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    inst = await runner.spawn("alpha", spawn_mode="worktree")  # alpha has .git
    assert inst.status is InstanceStatus.RUNNING
    assert inst.spawn_mode == "worktree"
    await runner.stop("alpha")


async def test_bypass_without_ceiling_rejected(runner_config):
    runner = _runner(runner_config)
    with pytest.raises(PermissionModeNotAllowed):
        await runner.spawn("alpha", permission_mode="bypassPermissions")


async def test_bypass_with_ceiling_allowed(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    config.projects = {"alpha": ProjectConfig(allow_bypass_permissions=True)}
    runner = SessionRunner(config, claude_json=claude_json)
    inst = await runner.spawn("alpha", permission_mode="bypassPermissions")
    assert inst.status is InstanceStatus.RUNNING
    assert inst.permission_mode == "bypassPermissions"
    await runner.stop("alpha")


# ----- flags actually reach the spawned process ------------------------

async def test_spawned_argv_records_modes(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    inst = await runner.spawn("alpha", spawn_mode="session", permission_mode="acceptEdits")
    argv = json.loads(Path(str(inst.bridge_debug_log_path) + ".argv.json").read_text())
    assert argv[argv.index("--spawn") + 1] == "session"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    await runner.stop("alpha")


async def test_spawn_uses_config_defaults(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    config.instance_defaults.permission_mode = "plan"
    runner = SessionRunner(config, claude_json=claude_json)
    inst = await runner.spawn("alpha")
    assert inst.permission_mode == "plan"
    assert inst.spawn_mode == "same-dir"
    await runner.stop("alpha")


# ----- session-mode reconcile ------------------------------------------

def test_session_mode_clean_exit_is_stopped():
    inst = RemoteControlInstance(
        project="x", label="x", status=InstanceStatus.RUNNING,
        intentional_stop=False, spawn_mode="session",
    )
    SessionRunner._reconcile_status(inst, alive=False)
    assert inst.status is InstanceStatus.STOPPED  # single-shot exit, not a crash


def test_same_dir_unexpected_exit_still_crashes():
    inst = RemoteControlInstance(
        project="x", label="x", status=InstanceStatus.RUNNING,
        intentional_stop=False, spawn_mode="same-dir",
    )
    SessionRunner._reconcile_status(inst, alive=False)
    assert inst.status is InstanceStatus.CRASHED


# ----- persistence round-trip ------------------------------------------

def test_permission_mode_persists(runner_config):
    config, _ = runner_config
    runner = _runner(runner_config)
    runner._instances["alpha"] = RemoteControlInstance(
        project="alpha", label="alpha", spawn_mode="worktree", permission_mode="acceptEdits",
    )
    assert runner._persist_subset()["alpha"]["permission_mode"] == "acceptEdits"


# ----- config -----------------------------------------------------------

def test_project_config_allows_bypass(projects_root):
    config = ClausterConfig(
        projects_root=projects_root,
        projects={"alpha": {"allow_bypass_permissions": True}},
    )
    assert config.allows_bypass("alpha") is True
    assert config.allows_bypass("beta") is False  # absent -> false
    assert config.instance_defaults.permission_mode == "default"


def test_project_config_ignores_unknown_keys(projects_root):
    # Additive-only schema: extra per-project keys must not break parsing.
    config = ClausterConfig(
        projects_root=projects_root,
        projects={"alpha": {"allow_bypass_permissions": True, "future_key": 7}},
    )
    assert config.allows_bypass("alpha") is True


def test_permission_mode_env_override(write_config, monkeypatch):
    monkeypatch.setenv("CLAUSTER_INSTANCE_DEFAULTS_PERMISSION_MODE", "plan")
    config = load_config(write_config())
    assert config.instance_defaults.permission_mode == "plan"


# ----- app route rejection paths (no spawn) ----------------------------

def _client(write_config, extra: str = "") -> TestClient:
    return TestClient(create_app(load_config(write_config(extra))))


def test_api_spawn_invalid_mode_is_422(write_config):
    client = _client(write_config)
    resp = client.post("/api/instances", json={"project": "alpha", "spawn_mode": "bogus"})
    assert resp.status_code == 422


def test_api_spawn_bypass_without_ceiling_is_403(write_config):
    client = _client(write_config)
    resp = client.post(
        "/api/instances", json={"project": "alpha", "permission_mode": "bypassPermissions"}
    )
    assert resp.status_code == 403


def test_api_spawn_non_string_mode_is_422(write_config):
    client = _client(write_config)
    resp = client.post("/api/instances", json={"project": "alpha", "spawn_mode": 5})
    assert resp.status_code == 422


# ----- dashboard renders the pickers (footgun gating) ------------------

def test_dashboard_renders_pickers(write_config):
    # Assert on the binding/option markup, not on user-facing label text (which a
    # copy change could break without a behaviour regression).
    client = _client(write_config)
    html = client.get("/").text
    assert "x-model=\"spawnMode['alpha']\"" in html
    assert "x-model=\"permMode['alpha']\"" in html
    assert '<option value="worktree">worktree</option>' in html  # alpha is a git repo


_BYPASS_OPTION = '<option value="bypassPermissions">'


def test_bypass_option_hidden_without_ceiling(write_config):
    # The confirm panel always *mentions* bypassPermissions; only the picker
    # <option> is gated. Assert on the option tag, not the bare word.
    client = _client(write_config)
    assert _BYPASS_OPTION not in client.get("/").text


def test_bypass_option_shown_with_ceiling(write_config):
    extra = "projects:\n  alpha:\n    allow_bypass_permissions: true\n"
    client = _client(write_config, extra)
    assert _BYPASS_OPTION in client.get("/").text
