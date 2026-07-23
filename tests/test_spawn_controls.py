"""v0.2 spawn controls — spawn-mode + permission-mode pickers (footgun-gated).

Runner-level happy paths use the fake `claude` binary (via ``runner_config``).
App-level tests exercise only the *rejection* paths, which short-circuit in
validation before any process is spawned — so they never invoke a real binary.
"""

from __future__ import annotations

import json
import sys
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
    _normalize_custom_name,
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


def test_build_cmd_omits_session_name_prefix_for_session_mode(runner_config):
    # `session` is single-session, so the prefix is out of scope even when configured.
    config, claude_json = runner_config
    config.instance_defaults.session_name_prefix = "acme"
    runner = SessionRunner(config, claude_json=claude_json)
    cmd = runner._build_cmd(Path("/tmp/x.log"), "alpha", "session", "default")
    assert "--remote-control-session-name-prefix" not in cmd


def test_build_cmd_verbose_omitted_by_default(runner_config):
    runner = _runner(runner_config)
    cmd = runner._build_cmd(Path("/tmp/x.log"), "alpha", "same-dir", "default")
    assert "--verbose" not in cmd


def test_build_cmd_appends_verbose_when_configured(runner_config):
    config, claude_json = runner_config
    config.instance_defaults.verbose = True
    runner = SessionRunner(config, claude_json=claude_json)
    for mode in ("same-dir", "worktree", "session"):
        cmd = runner._build_cmd(Path("/tmp/x.log"), "alpha", mode, "default")
        assert "--verbose" in cmd


# ----- _build_cmd sandbox toggle (#780) --------------------------------


def test_build_cmd_no_sandbox_flag_by_default(runner_config):
    # Tri-state "default" (the _build_cmd param default) appends NEITHER flag.
    runner = _runner(runner_config)
    cmd = runner._build_cmd(Path("/tmp/x.log"), "alpha", "same-dir", "default")
    assert "--sandbox" not in cmd
    assert "--no-sandbox" not in cmd


def test_build_cmd_appends_sandbox_when_on(runner_config):
    runner = _runner(runner_config)
    cmd = runner._build_cmd(Path("/tmp/x.log"), "alpha", "same-dir", "default", "on")
    assert "--sandbox" in cmd
    assert "--no-sandbox" not in cmd


def test_build_cmd_appends_no_sandbox_when_off(runner_config):
    runner = _runner(runner_config)
    cmd = runner._build_cmd(Path("/tmp/x.log"), "alpha", "same-dir", "default", "off")
    assert "--no-sandbox" in cmd
    assert "--sandbox" not in cmd


def test_build_cmd_explicit_default_appends_neither(runner_config):
    runner = _runner(runner_config)
    cmd = runner._build_cmd(Path("/tmp/x.log"), "alpha", "same-dir", "default", "default")
    assert "--sandbox" not in cmd
    assert "--no-sandbox" not in cmd


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
    await runner.stop(inst.instance_id)


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
    await runner.stop(inst.instance_id)


# ----- flags actually reach the spawned process ------------------------


async def test_spawned_argv_records_modes(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    inst = await runner.spawn("alpha", spawn_mode="session", permission_mode="acceptEdits")
    argv = json.loads(Path(str(inst.bridge_debug_log_path) + ".argv.json").read_text())
    assert argv[argv.index("--spawn") + 1] == "session"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    await runner.stop(inst.instance_id)


async def test_spawn_uses_config_defaults(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    config.instance_defaults.permission_mode = "plan"
    runner = SessionRunner(config, claude_json=claude_json)
    inst = await runner.spawn("alpha")
    assert inst.permission_mode == "plan"
    assert inst.spawn_mode == "same-dir"
    await runner.stop(inst.instance_id)


# ----- custom bridge name (#780) -----------------------------------------
#
# --name is user-customizable for a *standard* (server-mode) bridge only: the
# pty (Interactive Session) flag form has no equivalent flag (verified: `claude
# remote-control --help` documents --name, `claude --help`'s --remote-control
# entry does not), so a custom name is a no-op there (see test_runner_pty.py's
# test_spawn_pty_ignores_custom_name).


def test_normalize_custom_name_none_falls_back_to_project_name():
    assert _normalize_custom_name(None, fallback="alpha") == "alpha"


def test_normalize_custom_name_blank_after_strip_falls_back():
    assert _normalize_custom_name("   ", fallback="alpha") == "alpha"


def test_normalize_custom_name_empty_string_falls_back():
    assert _normalize_custom_name("", fallback="alpha") == "alpha"


def test_normalize_custom_name_strips_surrounding_whitespace():
    assert _normalize_custom_name("  my session  ", fallback="alpha") == "my session"


def test_normalize_custom_name_passes_through_valid_value():
    assert _normalize_custom_name("my session", fallback="alpha") == "my session"


def test_normalize_custom_name_accepts_exactly_the_length_cap():
    assert _normalize_custom_name("x" * 128, fallback="alpha") == "x" * 128


def test_normalize_custom_name_rejects_too_long():
    with pytest.raises(InvalidSpawnOption):
        _normalize_custom_name("x" * 129, fallback="alpha")


@pytest.mark.parametrize(
    "bad",
    [
        "bad\nname",  # newline (Cc)
        "bad\rname",  # carriage return (Cc)
        "bad\tname",  # tab (Cc)
        "bad\x00name",  # NUL (Cc)
        "bad\x1fname",  # C0 control (Cc)
        "bad\x7fname",  # DEL (Cc)
        "bad\x9bname",  # C1 control (Cc)
        "bad​name",  # zero-width space (Cf — a format char)
        "bad‮name",  # right-to-left override — bidi spoof (Cf)
        "bad⁦name",  # left-to-right isolate — bidi spoof (Cf)
        "bad\u2028name",  # line separator (Zl, not category C)
        "bad\u2029name",  # paragraph separator (Zp, not category C)
    ],
)
def test_normalize_custom_name_rejects_display_unsafe_characters(bad):
    with pytest.raises(InvalidSpawnOption):
        _normalize_custom_name(bad, fallback="alpha")


@pytest.mark.parametrize("good", ["Café-Bridge", "Über Session", "橋 bridge", "Bridge 🚀"])
def test_normalize_custom_name_accepts_ordinary_unicode(good):
    # Legitimate non-ASCII (accents, CJK, emoji) is category L/N/S — never over-rejected.
    assert _normalize_custom_name(good, fallback="alpha") == good


async def test_spawn_custom_name_reaches_argv_and_label(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    inst = await runner.spawn("alpha", custom_name="My Custom Bridge")
    try:
        assert inst.label == "My Custom Bridge"
        argv = json.loads(Path(str(inst.bridge_debug_log_path) + ".argv.json").read_text())
        assert argv[argv.index("--name") + 1] == "My Custom Bridge"
    finally:
        await runner.stop(inst.instance_id)


async def test_spawn_blank_custom_name_falls_back_to_project_name(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    inst = await runner.spawn("alpha", custom_name="   ")
    try:
        assert inst.label == "alpha"
        argv = json.loads(Path(str(inst.bridge_debug_log_path) + ".argv.json").read_text())
        assert argv[argv.index("--name") + 1] == "alpha"
    finally:
        await runner.stop(inst.instance_id)


async def test_spawn_omitted_custom_name_keeps_default_behavior(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    inst = await runner.spawn("alpha")
    try:
        assert inst.label == "alpha"
    finally:
        await runner.stop(inst.instance_id)


async def test_spawn_invalid_custom_name_rejected_before_any_spawn(runner_config):
    runner = _runner(runner_config)
    with pytest.raises(InvalidSpawnOption):
        await runner.spawn("alpha", custom_name="bad\nname")
    assert runner.running_count() == 0  # rejected before _popen ever ran


async def test_resume_preserves_custom_name(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    inst = await runner.spawn("alpha", custom_name="My Custom Bridge")
    await runner.stop(inst.instance_id)
    resumed = await runner.resume(inst.instance_id)
    try:
        assert resumed.label == "My Custom Bridge"
        argv = json.loads(Path(str(resumed.bridge_debug_log_path) + ".argv.json").read_text())
        assert argv[argv.index("--name") + 1] == "My Custom Bridge"
    finally:
        await runner.stop(resumed.instance_id)


def _runner_client(runner_config) -> TestClient:
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    return TestClient(create_app(config, runner=runner))


def test_api_spawn_custom_name_reaches_label_and_argv(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _runner_client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": "alpha", "name": "My Custom Bridge"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["label"] == "My Custom Bridge"
        argv = json.loads(Path(str(body["bridge_debug_log_path"]) + ".argv.json").read_text())
        assert argv[argv.index("--name") + 1] == "My Custom Bridge"


def test_api_spawn_blank_custom_name_falls_back(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _runner_client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": "alpha", "name": "   "})
        assert resp.status_code == 201, resp.text
        assert resp.json()["label"] == "alpha"


def test_api_spawn_custom_name_too_long_is_422(write_config):
    client = _client(write_config)
    resp = client.post("/api/instances", json={"project": "alpha", "name": "x" * 129})
    assert resp.status_code == 422


def test_api_spawn_custom_name_control_char_is_422(write_config):
    client = _client(write_config)
    resp = client.post("/api/instances", json={"project": "alpha", "name": "bad\nname"})
    assert resp.status_code == 422


def test_api_spawn_non_string_custom_name_is_422(write_config):
    client = _client(write_config)
    resp = client.post("/api/instances", json={"project": "alpha", "name": 5})
    assert resp.status_code == 422


# ----- resume default-name round-trip (Greptile #811) -------------------
#
# A default-name standard bridge has label == the raw project name. resume() must
# pass custom_name=None for that case (the trusted fast-path), NOT feed the project
# name back through the validator — otherwise a project name containing a char the
# validator now rejects would make resume raise even though the first launch
# succeeded. Project names are constrained by PROJECT_NAME_RE so this is near-
# unreachable in practice, but the round-trip must stay symmetric.


async def test_resume_default_name_bridge_passes_none_not_project_name(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    inst = await runner.spawn("alpha")  # no custom name -> label == "alpha"
    await runner.stop(inst.instance_id)

    seen: dict = {}
    real_spawn = runner.spawn

    async def _spy(name, **kwargs):
        seen.update(kwargs)
        return await real_spawn(name, **kwargs)

    monkeypatch.setattr(runner, "spawn", _spy)
    resumed = await runner.resume(inst.instance_id)
    try:
        # The bare project-name label is forwarded as None (fallback path), not "alpha".
        assert seen["custom_name"] is None
        assert resumed.label == "alpha"
    finally:
        await runner.stop(resumed.instance_id)


# ----- sandbox toggle (#780) -------------------------------------------


async def test_spawn_sandbox_disabled_coerces_to_default(runner_config, monkeypatch):
    # #1037: the toggle is disabled for 1.0, so a requested "on"/"off" is coerced to "default"
    # — nothing is recorded on the instance and no flag reaches the bridge argv.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    for requested in ("on", "off"):
        inst = await runner.spawn("alpha", sandbox=requested)
        try:
            assert inst.sandbox_mode == "default"
            argv = json.loads(Path(str(inst.bridge_debug_log_path) + ".argv.json").read_text())
            assert "--sandbox" not in argv
            assert "--no-sandbox" not in argv
        finally:
            await runner.stop(inst.instance_id)


async def test_spawn_sandbox_reaches_argv_when_enabled(runner_config, monkeypatch):
    # When re-enabled (#1046, gate flipped) the requested choice is recorded and the matching
    # flag reaches the bridge argv.
    monkeypatch.setattr("clauster.runner.SANDBOX_TOGGLE_ENABLED", True)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    on = await runner.spawn("alpha", sandbox="on")
    try:
        assert on.sandbox_mode == "on"
        argv = json.loads(Path(str(on.bridge_debug_log_path) + ".argv.json").read_text())
        assert "--sandbox" in argv
        assert "--no-sandbox" not in argv
    finally:
        await runner.stop(on.instance_id)
    off = await runner.spawn("alpha", sandbox="off")
    try:
        assert off.sandbox_mode == "off"
        argv = json.loads(Path(str(off.bridge_debug_log_path) + ".argv.json").read_text())
        assert "--no-sandbox" in argv
        assert "--sandbox" not in argv
    finally:
        await runner.stop(off.instance_id)


async def test_spawn_sandbox_default_appends_neither(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    inst = await runner.spawn("alpha")  # sandbox omitted -> "default"
    try:
        assert inst.sandbox_mode == "default"
        argv = json.loads(Path(str(inst.bridge_debug_log_path) + ".argv.json").read_text())
        assert "--sandbox" not in argv
        assert "--no-sandbox" not in argv
    finally:
        await runner.stop(inst.instance_id)


async def test_spawn_invalid_sandbox_rejected_before_any_spawn(runner_config):
    runner = _runner(runner_config)
    with pytest.raises(InvalidSpawnOption):
        await runner.spawn("alpha", sandbox="bogus")
    assert runner.running_count() == 0  # rejected before _popen ever ran


async def test_resume_sandbox_disabled_stays_default(runner_config, monkeypatch):
    # #1037: with the toggle disabled, a resume re-applies "default" (no flag) even though the
    # original request was "on".
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    inst = await runner.spawn("alpha", sandbox="on")
    await runner.stop(inst.instance_id)
    resumed = await runner.resume(inst.instance_id)
    try:
        assert resumed.sandbox_mode == "default"
        argv = json.loads(Path(str(resumed.bridge_debug_log_path) + ".argv.json").read_text())
        assert "--sandbox" not in argv
    finally:
        await runner.stop(resumed.instance_id)


async def test_resume_preserves_sandbox_choice_when_enabled(runner_config, monkeypatch):
    monkeypatch.setattr("clauster.runner.SANDBOX_TOGGLE_ENABLED", True)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _runner(runner_config)
    inst = await runner.spawn("alpha", sandbox="on")
    await runner.stop(inst.instance_id)
    resumed = await runner.resume(inst.instance_id)
    try:
        assert resumed.sandbox_mode == "on"
        argv = json.loads(Path(str(resumed.bridge_debug_log_path) + ".argv.json").read_text())
        assert "--sandbox" in argv
    finally:
        await runner.stop(resumed.instance_id)


def test_sandbox_persisted_in_subset(runner_config):
    runner = _runner(runner_config)
    fake = RemoteControlInstance(project="alpha", label="alpha", sandbox_mode="off")
    runner._instances[fake.instance_id] = fake
    subset = runner._persist_subset()
    record = next(v for v in subset.values() if v.get("project_name") == "alpha")
    assert record["sandbox_mode"] == "off"


def test_stopped_from_persisted_coerces_sandbox_when_disabled(runner_config):
    # #1037: a gone standard bridge rebuilt from state.json coerces a persisted "on"/"off" to
    # "default" while the toggle is disabled, so a resume re-applies no flag.
    runner = _runner(runner_config)
    runner._persisted = {
        "iid-1": {"project_name": "alpha", "label": "alpha", "sandbox_mode": "off"},
    }
    inst = runner._stopped_from_persisted("alpha")
    assert inst is not None
    assert inst.sandbox_mode == "default"


def test_stopped_from_persisted_restores_sandbox_when_enabled(runner_config, monkeypatch):
    # When re-enabled (#1046) a rebuilt STOPPED card keeps its recorded sandbox choice.
    monkeypatch.setattr("clauster.runner.SANDBOX_TOGGLE_ENABLED", True)
    runner = _runner(runner_config)
    runner._persisted = {
        "iid-1": {"project_name": "alpha", "label": "alpha", "sandbox_mode": "off"},
    }
    inst = runner._stopped_from_persisted("alpha")
    assert inst is not None
    assert inst.sandbox_mode == "off"


def test_stopped_from_persisted_defaults_sandbox_when_absent(runner_config):
    runner = _runner(runner_config)
    runner._persisted = {"iid-1": {"project_name": "alpha", "label": "alpha"}}  # pre-#780
    inst = runner._stopped_from_persisted("alpha")
    assert inst is not None
    assert inst.sandbox_mode == "default"


def test_api_spawn_sandbox_disabled_coerces_to_default(runner_config, monkeypatch):
    # #1037: the API still accepts `sandbox` (a bad value still 422s below) but it's inert while
    # the toggle is disabled — coerced to "default", no flag in argv.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _runner_client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": "alpha", "sandbox": "on"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["sandbox_mode"] == "default"
        argv = json.loads(Path(str(body["bridge_debug_log_path"]) + ".argv.json").read_text())
        assert "--sandbox" not in argv


def test_api_spawn_invalid_sandbox_is_422(write_config):
    client = _client(write_config)
    resp = client.post("/api/instances", json={"project": "alpha", "sandbox": "bogus"})
    assert resp.status_code == 422


def test_api_spawn_non_string_sandbox_is_422(write_config):
    client = _client(write_config)
    resp = client.post("/api/instances", json={"project": "alpha", "sandbox": 5})
    assert resp.status_code == 422


# ----- max_bridges (clauster-enforced concurrent-bridge cap) -----------


async def test_max_bridges_refuses_over_cap(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    config.instance_defaults.max_bridges = 1
    runner = SessionRunner(config, claude_json=claude_json)
    first = await runner.spawn("alpha", spawn_mode="same-dir")  # 1st: 0 live others -> ok
    with pytest.raises(CapacityExceeded):
        await runner.spawn("beta", spawn_mode="same-dir")  # 2nd: 1 live >= cap -> refused
    await runner.stop(first.instance_id)


async def test_max_bridges_unset_allows_concurrent(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    assert config.instance_defaults.max_bridges is None  # default: no limit
    runner = SessionRunner(config, claude_json=claude_json)
    inst_a = await runner.spawn("alpha", spawn_mode="same-dir")
    inst_b = await runner.spawn("beta", spawn_mode="same-dir")  # no cap -> both live
    assert runner.running_count() == 2
    await runner.stop(inst_a.instance_id)
    await runner.stop(inst_b.instance_id)


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
    fake = RemoteControlInstance(
        project="alpha",
        label="alpha",
        spawn_mode="worktree",
        permission_mode="acceptEdits",
    )
    runner._instances[fake.instance_id] = fake
    subset = runner._persist_subset()
    record = next(v for v in subset.values() if v.get("project_name") == "alpha")
    assert record["permission_mode"] == "acceptEdits"


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
        # prompt required since #1033 (unregistered bg dispatch); ceiling intent unchanged
        resp = client.post(
            "/api/agents",
            json={"project": "alpha", "permission_mode": "bypassPermissions", "prompt": "hi"},
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
        # prompt required since #1033 (unregistered bg dispatch); ceiling intent unchanged
        resp = client.post("/api/agents", json={"project": "alpha", "prompt": "hi"})
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
    # a Jinja-rendered perm option (value-stable; the label is plain-language copy)
    assert '<option value="default">Ask each time (default)</option>' in html
    assert '<option value="worktree">worktree</option>' in html  # alpha is a git repo


def test_dashboard_renders_name_control_and_omits_sandbox(write_config, monkeypatch):
    # #780 Session-name launch-popover control (standard-only) still renders on its
    # `=== 'standard'` x-show gate. The Sandbox select is REMOVED for 1.0 (#1037) — it never
    # reached the session worker — so none of its markup is present. Assert on the binding
    # markup (the contract), not the copy. Force non-win32 so the standard-only gate renders.
    monkeypatch.setattr(sys, "platform", "linux")
    html = _client(write_config).get("/").text
    assert "x-model=\"customName['alpha']\"" in html  # Session name input still present
    assert "sandboxMode" not in html  # Sandbox select + its Alpine state removed
    assert 'id="sandbox-alpha"' not in html
    assert '<option value="on">Enabled</option>' not in html


# The picker <option> gained a `:disabled` binding restricting bypass to the Desktop
# launch (where the typed-confirm guard lives), so match the tag prefix, not the full tag.
# The confirm panel always *mentions* bypassPermissions; only the picker <option> is gated.
_BYPASS_OPTION = '<option value="bypassPermissions"'


# ----- #578: surface a launch precondition INSIDE the popover (Run stays enabled) -----
#
# Decision (maintainer): do NOT disable "Run Claude here" on a readiness blocker — that
# would hide the spawn picker / trust-on-start confirm that resolve it (a dead-end). The
# one launch-refused case (a non-git dir whose default spawn mode is worktree) is instead
# coerced to a valid mode on popover open and surfaced with a note. Reactive Alpine over
# the server-rendered `is_git_repo` flag, so assert on the binding markup (the contract).


def test_run_button_enabled_and_coerces_spawn_on_open(write_config):
    # Run is NOT disabled by any readiness blocker; opening it coerces the spawn mode so a
    # non-git+worktree default falls back to a valid mode. #533: the inline two-statement
    # handler became projectRow.toggleLaunchPop(isGit), which calls coerceSpawnMode(name,
    # isGit) on open — the per-row is_git_repo flag still flows in as the argument.
    html = _client(write_config).get("/").text
    assert 'data-test="run-launch"' in html
    rows = html.split('data-project="')
    alpha_row = next(r for r in rows if r.startswith('alpha"'))
    beta_row = next(r for r in rows if r.startswith('beta"'))
    assert "toggleLaunchPop(true)" in alpha_row  # alpha is a git repo
    assert "toggleLaunchPop(false)" in beta_row  # beta is not
    assert "this.coerceSpawnMode(name, isGitRepo)" in html  # the open path still coerces
    # The old disable-the-button approach is gone — the readiness-gated helper no longer exists.
    assert "runBlockReason" not in html


def test_spawn_mode_coerce_and_note_helpers(write_config):
    # coerceSpawnMode falls a non-git+worktree default back to same-dir; spawnModeNote surfaces
    # it. Neither references trustState — untrusted is handled by the trust-on-start confirm.
    html = _client(write_config).get("/").text
    assert "coerceSpawnMode(name, isGit)" in html
    assert '!isGit && (this.spawnMode[name] || DEFAULT_SPAWN_MODE) === "worktree"' in html
    assert 'this.spawnMode[name] = "same-dir"' in html
    assert "spawnModeNote(isGit)" in html
    assert '!isGit && DEFAULT_SPAWN_MODE === "worktree"' in html
    start = html.index("coerceSpawnMode(name, isGit)")
    end = html.index("displayTokens", start)
    assert "trustState" not in html[start:end]


def test_spawn_mode_note_binding_renders_per_row(write_config):
    # The note rides x-text/x-show on spawnModeNote(<is_git_repo>) per row — visible only when
    # the helper returns a string (non-git + worktree default), never x-html.
    html = _client(write_config).get("/").text
    assert "spawnModeNote(false)" in html  # beta (non-git) carries the note binding
    assert "spawnModeNote(true)" in html  # alpha (git) -> note suppressed at runtime
    assert "x-html" not in html


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


# ----- shared bypass-ceiling predicate (one decision for every channel) -------
#
# #351: the "bypassPermissions requires the per-project ceiling" decision used to be
# hand-rolled in three places (runner._validate_spawn_options, app._enforce_bypass_ceiling
# called from the hosted and background-agent routes). They now all call the single
# ClausterConfig.bypass_denied predicate, each keeping its own exception type. These tests
# lock that decision down directly AND across every spawn entry point, so a new channel
# (or a default-mode change) cannot diverge from the ceiling through a stale copy.


def _ceiling_config(projects_root, *, allow: bool) -> ClausterConfig:
    return ClausterConfig(
        projects_root=projects_root,
        projects={"alpha": {"allow_bypass_permissions": allow}},
    )


@pytest.mark.parametrize(
    ("permission_mode", "allow", "expected"),
    [
        # The one case that denies: bypass requested AND the ceiling forbids it.
        ("bypassPermissions", False, True),
        # Ceiling permits bypass for this project -> allowed.
        ("bypassPermissions", True, False),
        # Non-bypass modes are never gated by the ceiling, regardless of the flag.
        ("default", False, False),
        ("default", True, False),
        ("acceptEdits", False, False),
        ("plan", True, False),
        # A resolved-but-None mode (channel passed nothing) is not bypass -> allowed.
        (None, False, False),
    ],
)
def test_bypass_denied_truth_table(projects_root, permission_mode, allow, expected):
    config = _ceiling_config(projects_root, allow=allow)
    assert config.bypass_denied("alpha", permission_mode) is expected


def test_bypass_denied_unknown_project_denies_bypass(projects_root):
    # An absent project has no ceiling entry -> allows_bypass is False -> bypass is denied.
    config = _ceiling_config(projects_root, allow=True)
    assert config.bypass_denied("ghost", "bypassPermissions") is True
    assert config.bypass_denied("ghost", "default") is False


# Every spawn entry point must reach the SAME decision. Parametrize the request across the
# three HTTP-reachable channels so divergence is structurally visible: if a channel ever
# stops routing through the shared predicate, exactly one row here flips.
#
#   bridge     -> POST /api/instances (default channel)        runner._validate_spawn_options
#   hosted     -> POST /api/instances {channel: hosted}        app._enforce_bypass_ceiling
#   background -> POST /api/agents                              app._enforce_bypass_ceiling
#
# These exercise only the REJECT paths, which short-circuit in validation before any process
# is spawned (the module-docstring contract for app-level tests), so they never touch a real
# binary or daemon. The bridge ALLOW path is covered at the runner level by
# test_bypass_with_ceiling_allowed (which wires the fake `claude` via runner_config); the
# hosted/background ALLOW paths clear the gate without a real spawn and are asserted below.
def _bypass_post(client: TestClient, channel: str):
    if channel == "background":
        # prompt required since #1033 (unregistered bg dispatch); ceiling intent unchanged
        return client.post(
            "/api/agents",
            json={"project": "alpha", "permission_mode": "bypassPermissions", "prompt": "hi"},
        )
    body = {"project": "alpha", "permission_mode": "bypassPermissions"}
    if channel == "hosted":
        body["channel"] = "hosted"
    return client.post("/api/instances", json=body)


@pytest.mark.parametrize("channel", ["bridge", "hosted", "background"])
def test_every_channel_rejects_bypass_without_ceiling(write_config, channel):
    # `with` so the lifespan runs for the background-task route (testing.md); harmless for the
    # others. No ceiling configured -> every channel must 403 through the shared predicate.
    with _client(write_config) as client:
        assert _bypass_post(client, channel).status_code == 403


@pytest.mark.parametrize(
    ("channel", "expected_status"),
    [
        # Hosted clears the gate then 503s on the absent claustrum daemon (not a 403).
        ("hosted", 503),
        # Background clears the gate then dispatches via the stubbed supervisor -> 201.
        ("background", 201),
    ],
)
def test_spawnless_channels_clear_gate_with_ceiling(
    write_config, monkeypatch, channel, expected_status
):
    # The two channels that can clear the gate via HTTP without a real spawn: hosted dead-ends
    # at the absent daemon (503), background dispatches through a stub (201). Both prove the
    # shared predicate let the bypass THROUGH (the salient assertion is "not 403").
    monkeypatch.setattr(
        "clauster.supervisor.dispatch_background_job", lambda cwd, **kwargs: "abcd1234"
    )
    with _client(write_config, _BYPASS_CEILING) as client:
        resp = _bypass_post(client, channel)
        assert resp.status_code != 403
        assert resp.status_code == expected_status
