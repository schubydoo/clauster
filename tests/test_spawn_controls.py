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
    CapacityExceeded,
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


def test_build_cmd_passes_capacity_for_multisession_modes(runner_config):
    config, claude_json = runner_config
    config.instance_defaults.capacity = 7
    runner = SessionRunner(config, claude_json=claude_json)
    for mode in ("same-dir", "worktree"):
        cmd = runner._build_cmd(Path("/tmp/x.log"), "alpha", mode, "default")
        assert cmd[cmd.index("--capacity") + 1] == "7"


def test_build_cmd_omits_capacity_for_session_mode(runner_config):
    runner = _runner(runner_config)
    cmd = runner._build_cmd(Path("/tmp/x.log"), "alpha", "session", "default")
    assert "--capacity" not in cmd


def test_build_cmd_session_name_prefix_when_set(runner_config):
    config, claude_json = runner_config
    config.instance_defaults.session_name_prefix = "acme"
    runner = SessionRunner(config, claude_json=claude_json)
    cmd = runner._build_cmd(Path("/tmp/x.log"), "alpha", "same-dir", "default")
    assert cmd[cmd.index("--remote-control-session-name-prefix") + 1] == "acme"


def test_build_cmd_no_session_name_prefix_by_default(runner_config):
    runner = _runner(runner_config)
    cmd = runner._build_cmd(Path("/tmp/x.log"), "alpha", "same-dir", "default")
    assert "--remote-control-session-name-prefix" not in cmd


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


# ----- max_bridges (clauster-enforced concurrent-bridge cap) -----------


async def test_max_bridges_refuses_over_cap(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    config.instance_defaults.max_bridges = 1
    runner = SessionRunner(config, claude_json=claude_json)
    await runner.spawn("alpha", spawn_mode="same-dir")  # 1st: 0 live others -> ok
    with pytest.raises(CapacityExceeded):
        await runner.spawn("beta", spawn_mode="same-dir")  # 2nd: 1 live >= cap -> refused
    await runner.stop("alpha")


async def test_max_bridges_unset_allows_concurrent(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    assert config.instance_defaults.max_bridges is None  # default: no limit
    runner = SessionRunner(config, claude_json=claude_json)
    await runner.spawn("alpha", spawn_mode="same-dir")
    await runner.spawn("beta", spawn_mode="same-dir")  # no cap -> both live
    assert runner.running_count() == 2
    await runner.stop("alpha")
    await runner.stop("beta")


# ----- session-mode reconcile ------------------------------------------


def test_session_mode_clean_exit_is_stopped():
    inst = RemoteControlInstance(
        project="x",
        label="x",
        status=InstanceStatus.RUNNING,
        intentional_stop=False,
        spawn_mode="session",
    )
    SessionRunner._reconcile_status(inst, alive=False)
    assert inst.status is InstanceStatus.STOPPED  # single-shot exit, not a crash


def test_same_dir_unexpected_exit_still_crashes():
    inst = RemoteControlInstance(
        project="x",
        label="x",
        status=InstanceStatus.RUNNING,
        intentional_stop=False,
        spawn_mode="same-dir",
    )
    SessionRunner._reconcile_status(inst, alive=False)
    assert inst.status is InstanceStatus.CRASHED


# ----- persistence round-trip ------------------------------------------


def test_permission_mode_persists(runner_config):
    config, _ = runner_config
    runner = _runner(runner_config)
    runner._instances["alpha"] = RemoteControlInstance(
        project="alpha",
        label="alpha",
        spawn_mode="worktree",
        permission_mode="acceptEdits",
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
        "/api/instances",
        json={"project": "alpha", "permission_mode": "bypassPermissions"},
    )
    assert resp.status_code == 403


# The hosted (claustrum stream-json) and background-agent (`claude --bg`) channels
# spawn outside the runner, so they must mirror its bypass-ceiling 403 — else a crafted
# request runs a session in a mode the project's ceiling forbids. The reject paths
# short-circuit before any daemon/spawn; the allow paths prove the ceiling gate is the
# only thing rejecting (hosted then 503s on the absent unit-test daemon; bg dispatch is
# stubbed so nothing real is trusted or spawned).
_BYPASS_CEILING = "projects:\n  alpha:\n    allow_bypass_permissions: true\n"


def test_api_spawn_hosted_bypass_without_ceiling_is_403(write_config):
    client = _client(write_config)
    resp = client.post(
        "/api/instances",
        json={"channel": "hosted", "project": "alpha", "permission_mode": "bypassPermissions"},
    )
    assert resp.status_code == 403


def test_api_spawn_hosted_bypass_with_ceiling_clears_gate(write_config):
    client = _client(write_config, _BYPASS_CEILING)
    resp = client.post(
        "/api/instances",
        json={"channel": "hosted", "project": "alpha", "permission_mode": "bypassPermissions"},
    )
    # Past the ceiling gate: the only remaining block is the absent claustrum daemon
    # (503), not a 403 — proving the ceiling no longer rejects bypass for this project.
    # Assert the specific detail so this keeps proving it reached the daemon-missing branch
    # of _hosted_prereqs (past the ceiling), not some other 503.
    assert resp.status_code == 503
    assert resp.json() == {"detail": "hosted channel unavailable: claustrum daemon not connected"}


def test_api_dispatch_agent_bypass_without_ceiling_is_403(write_config):
    # Background-task route: wrap in `with` so the app lifespan/startup runs (testing.md).
    with _client(write_config) as client:
        resp = client.post(
            "/api/agents",
            json={"project": "alpha", "permission_mode": "bypassPermissions"},
        )
        assert resp.status_code == 403


def test_api_dispatch_agent_bypass_with_ceiling_dispatches(write_config, monkeypatch):
    seen: dict = {}

    def _fake_dispatch(cwd, **kwargs):
        seen.update(kwargs)
        return "abcd1234"

    # Stub the dispatcher so nothing real is trusted or spawned; assert only that the
    # ceiling gate let the bypass request through to dispatch with the mode intact.
    monkeypatch.setattr("clauster.supervisor.dispatch_background_job", _fake_dispatch)
    with _client(write_config, _BYPASS_CEILING) as client:
        resp = client.post(
            "/api/agents",
            json={"project": "alpha", "permission_mode": "bypassPermissions"},
        )
        assert resp.status_code == 201
        assert resp.json() == {"id": "abcd1234"}
        assert seen["permission_mode"] == "bypassPermissions"


# Omitting permission_mode must NOT slip past the ceiling when the configured default is
# bypass: both channels gate the *effective* mode (request override, else instance_defaults),
# not just the raw request, so a project whose ceiling forbids bypass is still 403'd.
_DEFAULT_BYPASS = "instance_defaults:\n  permission_mode: bypassPermissions\n"


def test_api_spawn_hosted_default_bypass_without_ceiling_is_403(write_config):
    client = _client(write_config, _DEFAULT_BYPASS)
    resp = client.post("/api/instances", json={"channel": "hosted", "project": "alpha"})
    assert resp.status_code == 403


def test_api_dispatch_agent_default_bypass_without_ceiling_is_403(write_config):
    with _client(write_config, _DEFAULT_BYPASS) as client:
        resp = client.post("/api/agents", json={"project": "alpha"})
        assert resp.status_code == 403


# Symmetric allow path: when the ceiling DOES permit bypass and the request omits
# permission_mode (so it resolves from the bypass default), the *resolved* mode — not the
# raw None — must be what clears the gate and reaches spawn/dispatch. This locks the
# resolve-then-forward contract so a future "check resolved, forward raw" regression fails.
def test_api_spawn_hosted_default_bypass_with_ceiling_clears_gate(write_config):
    client = _client(write_config, _DEFAULT_BYPASS + _BYPASS_CEILING)
    resp = client.post("/api/instances", json={"channel": "hosted", "project": "alpha"})
    # Past the ceiling (resolved default is bypass, which the ceiling now allows): the only
    # remaining block is the absent daemon, so the specific 503 proves the resolved mode
    # cleared the gate rather than a raw-None slip.
    assert resp.status_code == 503
    assert resp.json() == {"detail": "hosted channel unavailable: claustrum daemon not connected"}


def test_api_dispatch_agent_default_bypass_with_ceiling_dispatches(write_config, monkeypatch):
    seen: dict = {}

    def _fake_dispatch(cwd, **kwargs):
        seen.update(kwargs)
        return "abcd1234"

    monkeypatch.setattr("clauster.supervisor.dispatch_background_job", _fake_dispatch)
    with _client(write_config, _DEFAULT_BYPASS + _BYPASS_CEILING) as client:
        resp = client.post("/api/agents", json={"project": "alpha"})
        assert resp.status_code == 201
        assert resp.json() == {"id": "abcd1234"}
        # The resolved default ("bypassPermissions"), not the omitted raw field, is forwarded.
        assert seen["permission_mode"] == "bypassPermissions"


# Pin the ordering fix: a missing project requested in bypass mode must 404 (existence
# check) before the ceiling runs — never leak a 403 for a project that doesn't exist. These
# fail immediately if either call site is reordered back to ceiling-before-existence.
def test_api_spawn_hosted_missing_project_bypass_still_404(write_config):
    client = _client(write_config)
    resp = client.post(
        "/api/instances",
        json={"channel": "hosted", "project": "missing", "permission_mode": "bypassPermissions"},
    )
    assert resp.status_code == 404


def test_api_dispatch_agent_missing_project_bypass_still_404(write_config):
    with _client(write_config) as client:
        resp = client.post(
            "/api/agents",
            json={"project": "missing", "permission_mode": "bypassPermissions"},
        )
        assert resp.status_code == 404


def test_api_spawn_non_string_mode_is_422(write_config):
    client = _client(write_config)
    resp = client.post("/api/instances", json={"project": "alpha", "spawn_mode": 5})
    assert resp.status_code == 422


# ----- dashboard renders the pickers (footgun gating) ------------------


def test_dashboard_renders_pickers(write_config):
    # Redesign: the spawn / permission / mode pickers moved into the per-project
    # launch popover (_project_row.html). Assert on the binding/option markup, not
    # on user-facing label text (a copy change could break that without a
    # behaviour regression). The permission picker is now Jinja-rendered <option>s
    # bound to the popover-local `lperm`.
    client = _client(write_config)
    html = client.get("/").text
    assert "x-model=\"spawnMode['alpha']\"" in html  # Spawn picker
    assert 'x-model="lperm"' in html  # Permissions picker (popover-local)
    assert '<option value="default">default</option>' in html  # a Jinja-rendered perm option
    assert '<option value="worktree">worktree</option>' in html  # alpha is a git repo


# The picker <option> gained a `:disabled` binding restricting bypass to the Desktop
# launch (where the typed-confirm guard lives), so match the tag prefix, not the full tag.
# The confirm panel always *mentions* bypassPermissions; only the picker <option> is gated.
_BYPASS_OPTION = '<option value="bypassPermissions"'


def test_bypass_option_hidden_without_ceiling(write_config):
    client = _client(write_config)
    assert _BYPASS_OPTION not in client.get("/").text


def test_bypass_option_shown_with_ceiling(write_config):
    extra = "projects:\n  alpha:\n    allow_bypass_permissions: true\n"
    client = _client(write_config, extra)
    html = client.get("/").text
    assert _BYPASS_OPTION in html
    # bypassPermissions is offered only for the Desktop/bridge mode, which carries the
    # typed-confirm footgun guard (browser/detached launches skip that confirm). Assert the
    # critical fragments separately so harmless attribute reordering/spacing can't break it.
    assert '<option value="bypassPermissions"' in html
    assert ":disabled=\"lmode !== 'desktop'\"" in html
