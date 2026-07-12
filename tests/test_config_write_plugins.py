"""CLI-driven plugin + marketplace management (#771), over the #766/#687 Foundation.

Mirrors :mod:`test_config_write_mcp_cli`'s layering:

* :mod:`clauster.config_write_plugins` unit tests — every ``claude plugin``
  invocation is fully STUBBED via an injected ``run`` callable (never the real
  binary/account): exact argv and cwd are asserted.
* The gated ``/api/config-write/plugins*`` / ``/api/config-write/marketplaces*``
  routes — exercised through a *fake* `claude` binary
  (``tests/fixtures/fake_claude/claude``'s ``plugin`` subcommand, scripted via env
  vars), never the real CLI, under the autouse HOME-isolation fixture.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from clauster import claude_cli
from clauster import config_write as cw
from clauster import config_write_plugins as plugins
from clauster.app import create_app
from clauster.config import load_config

# The fake `claude` stub is an extensionless POSIX shebang script; Windows CreateProcess
# can't launch it directly ([WinError 193]), so on Windows the tests point at the same-named
# `.cmd` wrapper (`_WIN_STUB_SUFFIX`), which shells it through `python` — the established
# idiom in test_ops.py / test_provisioning.py. The route tests that spawn the real
# `claude plugin` subprocess then run on every platform (the pure-unit tests that inject a
# fake `run=` callable were already cross-platform).
_WIN_STUB_SUFFIX = ".cmd" if sys.platform == "win32" else ""
FAKE_CLAUDE = (
    Path(__file__).resolve().parent / "fixtures" / "fake_claude" / f"claude{_WIN_STUB_SUFFIX}"
)


def _fake_run(rc: int = 0, stdout: str = "", stderr: str = ""):
    """Build a fake subprocess-runner + its call log, for injecting as ``run=``."""
    calls: list[dict] = []

    def run(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return subprocess.CompletedProcess(list(argv), rc, stdout=stdout, stderr=stderr)

    return run, calls


# --- validators: plugin id / marketplace name / marketplace source -----------------


@pytest.mark.parametrize("value", ["hello", "hello@market", "my_plugin", "p.v2", "A1@M-1"])
def test_validate_plugin_id_accepts_sane_ids(value: str) -> None:
    plugins.validate_plugin_id(value)  # no raise


@pytest.mark.parametrize(
    "value",
    ["--scope", "-e", "-", "--", "hello@-evil", "-evil@market", "", "has space", "a/b"],
)
def test_validate_plugin_id_rejects_option_like_or_bad_ids(value: str) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        plugins.validate_plugin_id(value)


def test_validate_plugin_id_rejects_non_string() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        plugins.validate_plugin_id(None)


@pytest.mark.parametrize("value", ["market", "acme-tools", "team_tools", "M1"])
def test_validate_marketplace_name_accepts_sane_names(value: str) -> None:
    plugins.validate_marketplace_name(value)  # no raise


@pytest.mark.parametrize("value", ["--scope", "-evil", "", "has space"])
def test_validate_marketplace_name_rejects_bad_names(value: str) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        plugins.validate_marketplace_name(value)


@pytest.mark.parametrize(
    "value",
    [
        "owner/repo",
        "https://github.com/owner/repo",
        "./relative/path",
        "/abs/path",
        "git@github.com:owner/repo.git",
    ],
)
def test_validate_marketplace_source_accepts_sane_sources(value: str) -> None:
    plugins.validate_marketplace_source(value)  # no raise


@pytest.mark.parametrize("value", ["-evil-source", "--scope", "-"])
def test_validate_marketplace_source_rejects_leading_dash(value: str) -> None:
    # Arg-injection: an unescaped leading '-' would be parsed by `claude`'s own
    # argument parser as an option rather than the positional source (verified
    # live against `claude plugin marketplace add -evil-source`).
    with pytest.raises(cw.InvalidCandidateError):
        plugins.validate_marketplace_source(value)


def test_validate_marketplace_source_rejects_control_chars() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        plugins.validate_marketplace_source("owner/repo\x00evil")


def test_validate_marketplace_source_rejects_empty_or_non_string() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        plugins.validate_marketplace_source("")
    with pytest.raises(cw.InvalidCandidateError):
        plugins.validate_marketplace_source(123)


# --- cli_enable_plugin / cli_disable_plugin -----------------------------------------


def test_cli_enable_plugin_builds_expected_argv(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0, stdout="Successfully enabled plugin: hello")
    plugins.cli_enable_plugin(str(FAKE_CLAUDE), tmp_path, "hello@market", "project", run=run)
    argv, kwargs = calls[0]["argv"], calls[0]["kwargs"]
    assert Path(argv[0]).name.startswith("claude")
    assert argv[1:] == ["plugin", "enable", "hello@market", "--scope", "project"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs.get("shell", False) is False


def test_cli_disable_plugin_builds_expected_argv(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0, stdout="Successfully disabled plugin: hello")
    plugins.cli_disable_plugin(str(FAKE_CLAUDE), tmp_path, "hello@market", "user", run=run)
    assert calls[0]["argv"][1:] == ["plugin", "disable", "hello@market", "--scope", "user"]


def test_cli_enable_plugin_rejects_bad_id_before_spawn(tmp_path: Path) -> None:
    run, calls = _fake_run()
    with pytest.raises(cw.InvalidCandidateError):
        plugins.cli_enable_plugin(str(FAKE_CLAUDE), tmp_path, "--evil", "project", run=run)
    assert calls == []  # never spawned


def test_cli_enable_plugin_other_failure_maps_to_plugin_cli_error(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=1, stderr="boom: disk full")
    with pytest.raises(plugins.PluginCliError):
        plugins.cli_enable_plugin(str(FAKE_CLAUDE), tmp_path, "hello", "project", run=run)


# --- cli_install_plugin -------------------------------------------------------------


def test_cli_install_plugin_builds_expected_argv(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0, stdout="Successfully installed plugin: hello@market")
    plugins.cli_install_plugin(str(FAKE_CLAUDE), tmp_path, "hello@market", "local", run=run)
    assert calls[0]["argv"][1:] == ["plugin", "install", "hello@market", "--scope", "local"]


def test_cli_install_plugin_not_found_maps_to_plugin_not_found_error(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=1, stderr='Plugin "hello" not found in marketplace "market"')
    with pytest.raises(plugins.PluginNotFoundError):
        plugins.cli_install_plugin(str(FAKE_CLAUDE), tmp_path, "hello@market", "user", run=run)


# --- cli_uninstall_plugin: --keep-data / --prune -y ---------------------------------


def test_cli_uninstall_plugin_builds_minimal_argv(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0, stdout="Successfully uninstalled plugin: hello")
    plugins.cli_uninstall_plugin(str(FAKE_CLAUDE), tmp_path, "hello@market", "project", run=run)
    assert calls[0]["argv"][1:] == ["plugin", "uninstall", "hello@market", "--scope", "project"]


def test_cli_uninstall_plugin_keep_data_flag(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0)
    plugins.cli_uninstall_plugin(
        str(FAKE_CLAUDE), tmp_path, "hello", "project", keep_data=True, run=run
    )
    assert "--keep-data" in calls[0]["argv"]


def test_cli_uninstall_plugin_prune_always_pairs_with_yes(tmp_path: Path) -> None:
    # --prune's confirmation is required whenever stdin/stdout is not a TTY
    # (verified via --help); this module's spawn always closes stdin, so -y must
    # ALWAYS accompany --prune or a real prune would hang until the timeout.
    run, calls = _fake_run(rc=0)
    plugins.cli_uninstall_plugin(
        str(FAKE_CLAUDE), tmp_path, "hello", "project", prune=True, run=run
    )
    argv = calls[0]["argv"]
    assert "--prune" in argv
    assert "-y" in argv
    assert argv.index("-y") == argv.index("--prune") + 1


def test_cli_uninstall_plugin_not_found_maps_to_plugin_not_found_error(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=1, stderr='Plugin "x@m" not found in installed plugins')
    with pytest.raises(plugins.PluginNotFoundError):
        plugins.cli_uninstall_plugin(str(FAKE_CLAUDE), tmp_path, "x@m", "user", run=run)


# --- cli_update_plugin ---------------------------------------------------------------


def test_cli_update_plugin_builds_expected_argv(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0)
    plugins.cli_update_plugin(str(FAKE_CLAUDE), tmp_path, "hello@market", "user", run=run)
    assert calls[0]["argv"][1:] == ["plugin", "update", "hello@market", "--scope", "user"]


def test_cli_update_plugin_not_found_maps_to_plugin_not_found_error(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=1, stderr='Plugin "hello" not found')
    with pytest.raises(plugins.PluginNotFoundError):
        plugins.cli_update_plugin(str(FAKE_CLAUDE), tmp_path, "hello@market", "project", run=run)


# --- cli_list_plugins / cli_plugin_details ------------------------------------------


def test_cli_list_plugins_parses_json(tmp_path: Path) -> None:
    payload = [{"id": "hello@market", "enabled": True}]
    run, _calls = _fake_run(rc=0, stdout=json.dumps(payload))
    assert plugins.cli_list_plugins(str(FAKE_CLAUDE), tmp_path, run=run) == payload


def test_cli_list_plugins_rejects_non_list_json(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=0, stdout=json.dumps({"not": "a list"}))
    with pytest.raises(plugins.PluginCliError):
        plugins.cli_list_plugins(str(FAKE_CLAUDE), tmp_path, run=run)


def test_cli_list_plugins_rejects_invalid_json(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=0, stdout="not json{{")
    with pytest.raises(plugins.PluginCliError):
        plugins.cli_list_plugins(str(FAKE_CLAUDE), tmp_path, run=run)


def test_cli_plugin_details_returns_stdout(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0, stdout="hello 0.0.1\n  test plugin\n")
    out = plugins.cli_plugin_details(str(FAKE_CLAUDE), tmp_path, "hello", run=run)
    assert "test plugin" in out
    assert calls[0]["argv"][1:] == ["plugin", "details", "hello"]


def test_cli_plugin_details_rejects_bad_id_before_spawn(tmp_path: Path) -> None:
    run, calls = _fake_run()
    with pytest.raises(cw.InvalidCandidateError):
        plugins.cli_plugin_details(str(FAKE_CLAUDE), tmp_path, "--evil", run=run)
    assert calls == []


# --- cli_marketplace_add / _remove / _update / list ---------------------------------


def test_cli_marketplace_add_builds_expected_argv(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0, stdout="Successfully added marketplace: market")
    plugins.cli_marketplace_add(str(FAKE_CLAUDE), tmp_path, "owner/repo", "project", run=run)
    assert calls[0]["argv"][1:] == [
        "plugin",
        "marketplace",
        "add",
        "owner/repo",
        "--scope",
        "project",
    ]


def test_cli_marketplace_add_rejects_bad_source_before_spawn(tmp_path: Path) -> None:
    run, calls = _fake_run()
    with pytest.raises(cw.InvalidCandidateError):
        plugins.cli_marketplace_add(str(FAKE_CLAUDE), tmp_path, "-evil", "project", run=run)
    assert calls == []


def test_cli_marketplace_add_git_not_found_is_plugin_cli_error_not_404(tmp_path: Path) -> None:
    # Greptile P2: a failed `marketplace add` whose git/network stderr contains
    # "not found" (e.g. `fatal: repository '…' not found`) is a FAILURE, not an
    # absent clauster-side entity -- it must NOT be reclassified as a 404
    # MarketplaceNotFoundError. It stays a generic PluginCliError (-> 400), with
    # the redacted stderr kept in the detail.
    run, _calls = _fake_run(rc=1, stderr="fatal: repository 'https://x/repo' not found")
    with pytest.raises(plugins.PluginCliError) as exc_info:
        plugins.cli_marketplace_add(str(FAKE_CLAUDE), tmp_path, "owner/repo", "project", run=run)
    assert not isinstance(exc_info.value, plugins.MarketplaceNotFoundError)
    assert "not found" in str(exc_info.value)  # stderr kept for diagnosis


def test_cli_marketplace_remove_always_passes_scope(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0, stdout="Successfully removed marketplace: market")
    plugins.cli_marketplace_remove(str(FAKE_CLAUDE), tmp_path, "market", "local", run=run)
    assert calls[0]["argv"][1:] == [
        "plugin",
        "marketplace",
        "remove",
        "market",
        "--scope",
        "local",
    ]


def test_cli_marketplace_remove_not_found_maps_to_marketplace_not_found_error(
    tmp_path: Path,
) -> None:
    run, _calls = _fake_run(rc=1, stderr="Marketplace 'x' not found")
    with pytest.raises(plugins.MarketplaceNotFoundError):
        plugins.cli_marketplace_remove(str(FAKE_CLAUDE), tmp_path, "x", "user", run=run)


def test_cli_marketplace_update_all_omits_name(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0)
    plugins.cli_marketplace_update(str(FAKE_CLAUDE), tmp_path, None, run=run)
    assert calls[0]["argv"][1:] == ["plugin", "marketplace", "update"]


def test_cli_marketplace_update_one_appends_name_no_scope_flag(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0)
    plugins.cli_marketplace_update(str(FAKE_CLAUDE), tmp_path, "market", run=run)
    argv = calls[0]["argv"][1:]
    assert argv == ["plugin", "marketplace", "update", "market"]
    assert "--scope" not in argv


def test_cli_marketplace_update_not_found_maps_to_marketplace_not_found_error(
    tmp_path: Path,
) -> None:
    run, _calls = _fake_run(rc=1, stderr="Marketplace 'x' not found")
    with pytest.raises(plugins.MarketplaceNotFoundError):
        plugins.cli_marketplace_update(str(FAKE_CLAUDE), tmp_path, "x", run=run)


def test_cli_list_marketplaces_parses_json(tmp_path: Path) -> None:
    payload = [{"name": "market", "source": "directory"}]
    run, _calls = _fake_run(rc=0, stdout=json.dumps(payload))
    assert plugins.cli_list_marketplaces(str(FAKE_CLAUDE), tmp_path, run=run) == payload


def test_cli_list_marketplaces_rejects_non_list_json(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=0, stdout=json.dumps({"bad": True}))
    with pytest.raises(plugins.PluginCliError):
        plugins.cli_list_marketplaces(str(FAKE_CLAUDE), tmp_path, run=run)


def test_cli_list_marketplaces_rejects_invalid_json(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=0, stdout="not json{{")
    with pytest.raises(plugins.PluginCliError):
        plugins.cli_list_marketplaces(str(FAKE_CLAUDE), tmp_path, run=run)


# --- spawn-level failure modes (validate-before-spawn) ------------------------------


def test_resolve_binary_not_found_propagates(tmp_path: Path) -> None:
    with pytest.raises(claude_cli.ClaudeNotFound):
        plugins.cli_enable_plugin("definitely-not-a-real-binary-xyz", tmp_path, "hello", "project")


def test_run_timeout_raises_plugin_cli_error(tmp_path: Path) -> None:
    def run(argv, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    with pytest.raises(plugins.PluginCliError):
        plugins.cli_enable_plugin(str(FAKE_CLAUDE), tmp_path, "hello", "project", run=run)


def test_run_timeout_message_never_leaks_argv(tmp_path: Path) -> None:
    def run(argv, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=60)

    with pytest.raises(plugins.PluginCliError) as exc_info:
        plugins.cli_marketplace_add(
            str(FAKE_CLAUDE), tmp_path, "https://x/should-not-leak", "project", run=run
        )
    msg = str(exc_info.value)
    assert "should-not-leak" not in msg
    assert "marketplace" in msg  # the verb (args[0]), never the full argv
    assert "timed out" in msg


def test_run_oserror_raises_redacted_plugin_cli_error(tmp_path: Path) -> None:
    def run(argv, **_kwargs):
        raise OSError("TOKEN: sk-should-not-leak")

    with pytest.raises(plugins.PluginCliError) as exc_info:
        plugins.cli_enable_plugin(str(FAKE_CLAUDE), tmp_path, "hello", "project", run=run)
    assert "sk-should-not-leak" not in str(exc_info.value)
    assert "failed to run" in str(exc_info.value)


def test_failure_detail_is_redacted(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=1, stderr="TOKEN: sk-should-not-leak\nother line")
    with pytest.raises(plugins.PluginCliError) as exc_info:
        plugins.cli_enable_plugin(str(FAKE_CLAUDE), tmp_path, "hello", "project", run=run)
    assert "sk-should-not-leak" not in str(exc_info.value)


# --- require_install_confirm: the STRONG per-install confirm ------------------------


def test_require_install_confirm_accepts_exact_match() -> None:
    plugins.require_install_confirm("hello@market", "hello@market")  # no raise


def test_require_install_confirm_rejects_mismatch() -> None:
    with pytest.raises(HTTPException) as exc_info:
        plugins.require_install_confirm("hello@market", "wrong")
    assert exc_info.value.status_code == 400


def test_require_install_confirm_rejects_non_string() -> None:
    with pytest.raises(HTTPException) as exc_info:
        plugins.require_install_confirm("hello@market", None)
    assert exc_info.value.status_code == 400


def test_require_install_confirm_cannot_be_replayed_for_a_different_plugin() -> None:
    # A confirm typed for installing "a@market" must never confirm "b@market".
    with pytest.raises(HTTPException):
        plugins.require_install_confirm("b@market", "a@market")


# --- direct (non-CLI) reads: enabledPlugins / extraKnownMarketplaces ----------------


def test_read_project_enabled_plugins_round_trip(tmp_path: Path) -> None:
    assert plugins.read_project_enabled_plugins(tmp_path) == {}
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"enabledPlugins": {"hello@market": True, "x@m": False}, "other": 1}),
        encoding="utf-8",
    )
    assert plugins.read_project_enabled_plugins(tmp_path) == {
        "hello@market": True,
        "x@m": False,
    }


def test_read_enabled_plugins_ignores_non_bool_entries(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"enabledPlugins": {"good@m": True, "bad@m": "not-a-bool"}}),
        encoding="utf-8",
    )
    assert plugins.read_project_enabled_plugins(tmp_path) == {"good@m": True}


def test_read_project_local_enabled_plugins(tmp_path: Path) -> None:
    local = cw.project_local_settings_path(tmp_path)
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps({"enabledPlugins": {"a@m": True}}), encoding="utf-8")
    assert plugins.read_project_local_enabled_plugins(tmp_path) == {"a@m": True}


def test_read_user_enabled_plugins(tmp_path: Path) -> None:
    settings_json = tmp_path / "settings.json"
    settings_json.write_text(json.dumps({"enabledPlugins": {"a@m": False}}), encoding="utf-8")
    assert plugins.read_user_enabled_plugins(settings_json) == {"a@m": False}


def test_read_project_marketplaces_round_trip(tmp_path: Path) -> None:
    assert plugins.read_project_marketplaces(tmp_path) == {}
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"extraKnownMarketplaces": {"market": {"source": {"source": "directory"}}}}),
        encoding="utf-8",
    )
    assert plugins.read_project_marketplaces(tmp_path) == {
        "market": {"source": {"source": "directory"}}
    }


def test_read_project_local_marketplaces(tmp_path: Path) -> None:
    local = cw.project_local_settings_path(tmp_path)
    local.parent.mkdir(parents=True)
    local.write_text(
        json.dumps({"extraKnownMarketplaces": {"m2": {"source": {"source": "github"}}}}),
        encoding="utf-8",
    )
    assert plugins.read_project_local_marketplaces(tmp_path) == {
        "m2": {"source": {"source": "github"}}
    }


def test_read_user_marketplaces(tmp_path: Path) -> None:
    settings_json = tmp_path / "settings.json"
    settings_json.write_text(
        json.dumps({"extraKnownMarketplaces": {"m3": {"source": {"source": "github"}}}}),
        encoding="utf-8",
    )
    assert plugins.read_user_marketplaces(settings_json) == {
        "m3": {"source": {"source": "github"}}
    }


# --- gated routes (full FastAPI lifespan, fake `claude` binary) --------------------


def _client(write_config, tmp_path: Path, extra: str) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}")
    return TestClient(create_app(load_config(cfg)))


_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
_PROJECT_ONLY = "config_write:\n  enabled: true\n"


# --- GET /api/config-write/plugins (installed list, CLI) ---------------------------


def test_route_plugins_list_success(write_config, tmp_path, projects_root, monkeypatch) -> None:
    payload = [{"id": "hello@market", "enabled": True}]
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", json.dumps(payload))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins?scope=project&project=alpha")
        assert resp.status_code == 200
        body = resp.json()
        assert body["plugins"] == payload
        assert body["project"] == "alpha"  # project scope carries the project key


def test_route_plugins_list_user_scope_omits_project(write_config, tmp_path, monkeypatch) -> None:
    # Greptile P2: user-scope list must OMIT `project` (a meaningless "") to match
    # the sibling routes (/plugins/enabled, action POSTs) -- not carry `"project": ""`.
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", json.dumps([]))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins?scope=user")
        assert resp.status_code == 200
        assert "project" not in resp.json()


def test_route_plugins_list_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get("/api/config-write/plugins?scope=project&project=alpha").status_code == 404


def test_route_plugins_list_capability_before_scope(write_config, tmp_path) -> None:
    # #819: a disabled surface 404s for ANY request, a bogus scope included --
    # never a 422 leaking that the surface would otherwise validate the scope.
    with _client(write_config, tmp_path, "") as c:
        resp = c.get("/api/config-write/plugins?scope=bogus&project=alpha")
        assert resp.status_code == 404


def test_route_plugins_list_bad_scope_is_422_when_enabled(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins?scope=bogus&project=alpha")
        assert resp.status_code == 422


def test_route_plugins_list_missing_project_dir_is_404(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins?scope=project&project=noexist")
        assert resp.status_code == 404


def test_route_plugins_list_user_scope_404_when_off(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        resp = c.get("/api/config-write/plugins?scope=user")
        assert resp.status_code == 404


# --- GET /api/config-write/plugins/enabled (direct read) ---------------------------


def test_route_plugins_enabled_read_project_scope(write_config, tmp_path, projects_root) -> None:
    alpha = projects_root / "alpha"
    settings = alpha / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"enabledPlugins": {"a@m": True}}), encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins/enabled?scope=project&project=alpha")
        assert resp.status_code == 200
        assert resp.json() == {"scope": "project", "project": "alpha", "enabled": {"a@m": True}}


def test_route_plugins_enabled_read_local_scope(write_config, tmp_path, projects_root) -> None:
    alpha = projects_root / "alpha"
    local = alpha / ".claude" / "settings.local.json"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps({"enabledPlugins": {"a@m": False}}), encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins/enabled?scope=local&project=alpha")
        assert resp.json()["enabled"] == {"a@m": False}


def test_route_plugins_enabled_read_user_scope_empty_by_default(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins/enabled?scope=user")
        assert resp.status_code == 200
        assert resp.json() == {"scope": "user", "enabled": {}}


def test_route_plugins_enabled_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get("/api/config-write/plugins/enabled").status_code == 404


# --- GET /api/config-write/plugins/{plugin_id} (details, CLI) ----------------------


def test_route_plugin_details_success(write_config, tmp_path, projects_root, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", "hello 0.0.1\n  test plugin\n")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins/hello@market?scope=project&project=alpha")
        assert resp.status_code == 200
        assert "test plugin" in resp.json()["details"]


def test_route_plugin_details_option_like_id_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins/--evil?scope=project&project=alpha")
        assert resp.status_code == 422


def test_route_plugin_details_not_found_is_404(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_EXIT_CODE", "1")
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDERR", 'Plugin "x" not found')
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins/x@m?scope=project&project=alpha")
        assert resp.status_code == 404


# --- POST /api/config-write/plugins/action ------------------------------------------


def test_route_plugins_action_enable_success(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", "Successfully enabled plugin: hello")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "enable",
                "plugin": "hello@market",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "scope": "project",
            "plugin": "hello@market",
            "op": "enable",
            "ok": True,
            "project": "alpha",
        }
    record = json.loads(argv_file.read_text())
    assert record["argv"] == ["enable", "hello@market", "--scope", "project"]
    assert record["cwd"] == str((projects_root / "alpha").resolve())


def test_route_plugins_action_disable_success(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "disable",
                "plugin": "hello@market",
            },
        )
        assert resp.status_code == 200


def test_route_plugins_action_install_requires_strong_confirm(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "install",
                "plugin": "hello@market",
                # confirm_plugin omitted -> must reject before any spawn
            },
        )
        assert resp.status_code == 400


def test_route_plugins_action_install_confirm_mismatch_is_400(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "install",
                "plugin": "hello@market",
                "confirm_plugin": "someone-else@market",
            },
        )
        assert resp.status_code == 400


def test_route_plugins_action_install_success_with_matching_confirm(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", "Successfully installed plugin: hello@market")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "install",
                "plugin": "hello@market",
                "confirm_plugin": "hello@market",
            },
        )
        assert resp.status_code == 200
    record = json.loads(argv_file.read_text())
    assert record["argv"] == ["install", "hello@market", "--scope", "project"]


def test_route_plugins_action_uninstall_keep_data_and_prune(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_FILE", str(argv_file))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "uninstall",
                "plugin": "hello@market",
                "keep_data": True,
                "prune": True,
            },
        )
        assert resp.status_code == 200
    argv = json.loads(argv_file.read_text())["argv"]
    assert "--keep-data" in argv
    assert "--prune" in argv
    assert "-y" in argv


def test_route_plugins_action_uninstall_not_found_is_404(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_EXIT_CODE", "1")
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDERR", 'Plugin "x@m" not found in installed plugins')
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "uninstall",
                "plugin": "x@m",
            },
        )
        assert resp.status_code == 404


def test_route_plugins_action_update_success(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_FILE", str(argv_file))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "op": "update",
                "plugin": "hello@market",
            },
        )
        assert resp.status_code == 200
        assert "project" not in resp.json()
    argv = json.loads(argv_file.read_text())["argv"]
    assert argv == ["update", "hello@market", "--scope", "user"]


def test_route_plugins_action_bad_op_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "bogus",
                "plugin": "hello",
            },
        )
        assert resp.status_code == 422


def test_route_plugins_action_bad_scope_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "bogus",
                "project": "alpha",
                "confirm": "alpha",
                "op": "enable",
                "plugin": "hello",
            },
        )
        assert resp.status_code == 422


def test_route_plugins_action_option_like_plugin_name_is_422(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "enable",
                "plugin": "--scope",
            },
        )
        assert resp.status_code == 422


def test_route_plugins_action_confirm_mismatch_is_400(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "op": "enable",
                "plugin": "hello",
            },
        )
        assert resp.status_code == 400


def test_route_plugins_action_confirm_runs_before_op_validation(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "op": "bogus-op",
                "plugin": "",
            },
        )
        assert resp.status_code == 400


def test_route_plugins_action_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "enable",
                "plugin": "hello",
            },
        )
        assert resp.status_code == 404


def test_route_plugins_action_capability_before_scope(write_config, tmp_path) -> None:
    # #819: capability gate fires even against a bogus scope, before the 422.
    with _client(write_config, tmp_path, "") as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={"scope": "bogus", "op": "enable", "plugin": "hello", "confirm": "x"},
        )
        assert resp.status_code == 404


def test_route_plugins_action_user_scope_404_when_off(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "op": "enable",
                "plugin": "hello",
            },
        )
        assert resp.status_code == 404


def test_route_plugins_action_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "../escape",
                "confirm": "../escape",
                "op": "enable",
                "plugin": "hello",
            },
        )
        assert resp.status_code == 400


def test_route_plugins_action_missing_project_dir_is_404(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "noexist",
                "confirm": "noexist",
                "op": "enable",
                "plugin": "hello",
            },
        )
        assert resp.status_code == 404


def test_route_plugins_action_missing_plugin_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "enable",
            },
        )
        assert resp.status_code == 422


def test_route_plugins_action_bad_keep_data_type_is_422(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "uninstall",
                "plugin": "hello",
                "keep_data": "yes",
            },
        )
        assert resp.status_code == 422


def test_route_plugins_action_bad_prune_type_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "uninstall",
                "plugin": "hello",
                "prune": "yes",
            },
        )
        assert resp.status_code == 422


def test_route_plugins_action_local_scope_ensures_gitignore(
    write_config, tmp_path, projects_root
) -> None:
    alpha = projects_root / "alpha"
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "op": "enable",
                "plugin": "hello@market",
            },
        )
        assert resp.status_code == 200
    gitignore = (alpha / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/settings.local.json" in gitignore


# --- GET /api/config-write/marketplaces (merged list, CLI) --------------------------


def test_route_marketplaces_list_success(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    payload = [{"name": "market", "source": "directory"}]
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", json.dumps(payload))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/marketplaces?scope=project&project=alpha")
        assert resp.status_code == 200
        body = resp.json()
        assert body["marketplaces"] == payload
        assert body["project"] == "alpha"  # project scope carries the project key


def test_route_marketplaces_list_user_scope_omits_project(
    write_config, tmp_path, monkeypatch
) -> None:
    # Greptile P2: user-scope marketplace list must OMIT `project`, matching siblings.
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", json.dumps([]))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/marketplaces?scope=user")
        assert resp.status_code == 200
        assert "project" not in resp.json()


def test_route_marketplaces_list_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get("/api/config-write/marketplaces").status_code == 404


# --- GET /api/config-write/marketplaces/declared (direct read) ---------------------


def test_route_marketplaces_declared_project_scope(write_config, tmp_path, projects_root) -> None:
    alpha = projects_root / "alpha"
    settings = alpha / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"extraKnownMarketplaces": {"m": {"source": {"source": "github"}}}}),
        encoding="utf-8",
    )
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/marketplaces/declared?scope=project&project=alpha")
        assert resp.json()["marketplaces"] == {"m": {"source": {"source": "github"}}}


def test_route_marketplaces_declared_user_scope_empty_by_default(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/marketplaces/declared?scope=user")
        assert resp.json() == {"scope": "user", "marketplaces": {}}


def test_route_marketplaces_declared_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get("/api/config-write/marketplaces/declared").status_code == 404


# --- POST /api/config-write/marketplaces/action -------------------------------------


def test_route_marketplaces_action_add_success(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", "Successfully added marketplace: market")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "source": "owner/repo",
            },
        )
        assert resp.status_code == 200
    argv = json.loads(argv_file.read_text())["argv"]
    assert argv == ["marketplace", "add", "owner/repo", "--scope", "project"]


def test_route_marketplaces_action_add_rejects_leading_dash_source(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "source": "-evil-source",
            },
        )
        assert resp.status_code == 422


def test_route_marketplaces_action_add_git_not_found_is_400_not_404(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    # Greptile P2 end-to-end: a failed `marketplace add` whose git stderr says
    # "repository not found" must surface as 400 (the add FAILED), never a 404.
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_EXIT_CODE", "1")
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDERR", "fatal: repository 'https://x/repo' not found")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "source": "owner/repo",
            },
        )
        assert resp.status_code == 400


def test_route_marketplaces_action_add_missing_source_is_422(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
            },
        )
        assert resp.status_code == 422


def test_route_marketplaces_action_remove_success(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", "Successfully removed marketplace: market")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "remove",
                "name": "market",
            },
        )
        assert resp.status_code == 200
    argv = json.loads(argv_file.read_text())["argv"]
    assert argv == ["marketplace", "remove", "market", "--scope", "project"]


def test_route_marketplaces_action_remove_rejects_leading_dash_name(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "remove",
                "name": "--evil",
            },
        )
        assert resp.status_code == 422


def test_route_marketplaces_action_remove_not_found_is_404(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_EXIT_CODE", "1")
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDERR", "Marketplace 'x' not found")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "remove",
                "name": "x",
            },
        )
        assert resp.status_code == 404


def test_route_marketplaces_action_update_all(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_FILE", str(argv_file))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "update",
            },
        )
        assert resp.status_code == 200
    argv = json.loads(argv_file.read_text())["argv"]
    assert argv == ["marketplace", "update"]


def test_route_marketplaces_action_update_one(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_FILE", str(argv_file))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "update",
                "name": "market",
            },
        )
        assert resp.status_code == 200
    argv = json.loads(argv_file.read_text())["argv"]
    assert argv == ["marketplace", "update", "market"]


def test_route_marketplaces_action_bad_op_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "bogus",
            },
        )
        assert resp.status_code == 422


def test_route_marketplaces_action_bad_scope_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "bogus",
                "project": "alpha",
                "confirm": "alpha",
                "op": "update",
            },
        )
        assert resp.status_code == 422


def test_route_marketplaces_action_confirm_mismatch_is_400(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "op": "update",
            },
        )
        assert resp.status_code == 400


def test_route_marketplaces_action_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={"scope": "project", "project": "alpha", "confirm": "alpha", "op": "update"},
        )
        assert resp.status_code == 404


def test_route_marketplaces_action_capability_before_scope(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={"scope": "bogus", "op": "update", "confirm": "x"},
        )
        assert resp.status_code == 404


def test_route_marketplaces_action_user_scope_404_when_off(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "op": "update"},
        )
        assert resp.status_code == 404


def test_route_marketplaces_action_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "../escape",
                "confirm": "../escape",
                "op": "update",
            },
        )
        assert resp.status_code == 400


def test_route_marketplaces_action_missing_project_dir_is_404(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "noexist",
                "confirm": "noexist",
                "op": "update",
            },
        )
        assert resp.status_code == 404


def test_route_marketplaces_action_local_scope_add_ensures_gitignore(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", "Successfully added marketplace: market")
    alpha = projects_root / "alpha"
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "op": "add",
                "source": "owner/repo",
            },
        )
        assert resp.status_code == 200
    gitignore = (alpha / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/settings.local.json" in gitignore


def test_route_marketplaces_action_local_scope_update_does_not_touch_gitignore(
    write_config, tmp_path, projects_root
) -> None:
    # `update` writes no settings key -- gitignore-on-create only applies to
    # add/remove, which actually touch settings.local.json.
    alpha = projects_root / "alpha"
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "op": "update",
            },
        )
        assert resp.status_code == 200
    assert not (alpha / ".gitignore").exists()


# --- coverage closers: bad-scope 422s, CLI-error mapping, runner-missing 404 -------


def test_route_user_scope_404_when_runner_missing(write_config, tmp_path) -> None:
    # _plugin_cli_cwd's "no runner wired" fail-closed branch, shared by every
    # user-scope plugin/marketplace route -- mirrors
    # test_config_write_settings.test_route_user_scope_404_when_runner_missing.
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ON}")
    app = create_app(load_config(cfg))
    with TestClient(app) as c:
        app.state.runner = None  # simulate an app started without a runner
        assert c.get("/api/config-write/plugins?scope=user").status_code == 404


def test_route_plugins_list_cli_error_maps_to_400(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", "not json{{")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins?scope=project&project=alpha")
        assert resp.status_code == 400


def test_route_plugins_enabled_bad_scope_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins/enabled?scope=bogus")
        assert resp.status_code == 422


def test_route_plugin_details_bad_scope_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/plugins/hello?scope=bogus&project=alpha")
        assert resp.status_code == 422


def test_route_marketplaces_list_bad_scope_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/marketplaces?scope=bogus&project=alpha")
        assert resp.status_code == 422


def test_route_marketplaces_list_cli_error_maps_to_400(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", "not json{{")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/marketplaces?scope=project&project=alpha")
        assert resp.status_code == 400


def test_route_marketplaces_declared_bad_scope_is_422(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/marketplaces/declared?scope=bogus")
        assert resp.status_code == 422


def test_route_marketplaces_action_remove_missing_name_is_422(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "remove",
            },
        )
        assert resp.status_code == 422


def test_route_marketplaces_action_update_bad_name_type_is_422(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "update",
                "name": "",
            },
        )
        assert resp.status_code == 422


def test_route_marketplaces_action_update_rejects_leading_dash_name(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "update",
                "name": "--evil",
            },
        )
        assert resp.status_code == 422


def test_route_marketplaces_action_update_user_scope_skips_project_key(
    write_config, tmp_path, monkeypatch
) -> None:
    # user-scope success: the "scope != user" branch that adds `project` to the
    # result must be SKIPPED (not merely false-but-untested).
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "op": "update"},
        )
        assert resp.status_code == 200
        assert "project" not in resp.json()
