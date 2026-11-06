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


# Two parse failures that are not a JSONDecodeError: deeply-nested JSON raises
# RecursionError (not a ValueError at all) and a >4300-digit int literal raises a bare
# ValueError. Each escaped as a raw exception (a 500) instead of a PluginCliError.
_UNPARSEABLE = [
    pytest.param("[" * 100_000, id="deeply-nested"),
    pytest.param("1" * 5000, id="oversized-int"),
]


@pytest.mark.parametrize("payload", _UNPARSEABLE)
def test_cli_list_plugins_rejects_unparseable_json(tmp_path: Path, payload: str) -> None:
    run, _calls = _fake_run(rc=0, stdout=payload)
    with pytest.raises(plugins.PluginCliError, match="invalid JSON"):
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


@pytest.mark.parametrize("payload", _UNPARSEABLE)
def test_cli_list_marketplaces_rejects_unparseable_json(tmp_path: Path, payload: str) -> None:
    run, _calls = _fake_run(rc=0, stdout=payload)
    with pytest.raises(plugins.PluginCliError, match="invalid JSON"):
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
        # An entry with no `scope` field is never treated as installed here (TASK-523).
        assert body["plugins"] == [{**payload[0], "atScope": False}]
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


def _break_settings_file(scope: str, projects_root: Path, *, unreadable: bool = False) -> None:
    """Break the settings file ``scope``'s direct read consults.

    By default write malformed JSON there. With ``unreadable``, put a directory at the
    file path instead, so ``read_bytes`` raises an ``OSError`` on every OS (no chmod).
    """
    if scope == "user":
        # The isolated HOME's ~/.claude/settings.json, expanded the same way SessionRunner
        # expands ~/.claude.json (the route derives this path from that file's parent).
        path = Path("~/.claude/settings.json").expanduser()
    elif scope == "local":
        path = projects_root / "alpha" / ".claude" / "settings.local.json"
    else:
        path = projects_root / "alpha" / ".claude" / "settings.json"
    if unreadable:
        path.mkdir(parents=True, exist_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")


@pytest.mark.parametrize("scope", ["project", "local", "user"])
def test_route_plugins_enabled_corrupt_settings_is_422(
    write_config, tmp_path, projects_root, scope: str
) -> None:
    # Issue 1528 sweep: a hand-corrupted settings file makes the direct read raise
    # InvalidCandidateError; the route must map it to a clean 422, never a 500.
    _break_settings_file(scope, projects_root)
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get(f"/api/config-write/plugins/enabled?scope={scope}&project=alpha")
        assert resp.status_code == 422


@pytest.mark.parametrize("route", ["plugins/enabled", "marketplaces/declared"])
@pytest.mark.parametrize("scope", ["project", "local", "user"])
def test_route_unreadable_settings_is_400(
    write_config, tmp_path, projects_root, scope: str, route: str
) -> None:
    # Issue 1528 follow-up: a settings path that exists but cannot be read (here a
    # directory) raises a typed ConfigWriteError, mapped to a 400, never an OSError 500.
    # The detail names only the file, never its absolute path.
    _break_settings_file(scope, projects_root, unreadable=True)
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get(f"/api/config-write/{route}?scope={scope}&project=alpha")
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "unreadable" in detail
        assert str(projects_root) not in detail
        assert str(Path.home()) not in detail


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
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_LOG", str(argv_log))
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
    # TASK-517: a `list` read on each side of the update, which is the only mutation.
    calls = [json.loads(line)["argv"] for line in argv_log.read_text().splitlines()]
    assert calls == [
        ["list", "--json"],
        ["update", "hello@market", "--scope", "user"],
        ["list", "--json"],
    ]


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


@pytest.mark.parametrize("scope", ["project", "local", "user"])
def test_route_marketplaces_declared_corrupt_settings_is_422(
    write_config, tmp_path, projects_root, scope: str
) -> None:
    # Issue 1528 sweep: same guard as /plugins/enabled -- a corrupt settings file is a
    # clean 422 from the declared-marketplaces read, never an unhandled 500.
    _break_settings_file(scope, projects_root)
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get(f"/api/config-write/marketplaces/declared?scope={scope}&project=alpha")
        assert resp.status_code == 422


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
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_LOG", str(argv_log))
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
    calls = [json.loads(line)["argv"] for line in argv_log.read_text().splitlines()]
    assert calls == [
        ["marketplace", "list", "--json"],
        ["marketplace", "update"],
        ["marketplace", "list", "--json"],
    ]


def test_route_marketplaces_action_update_one(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_LOG", str(argv_log))
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
    calls = [json.loads(line)["argv"] for line in argv_log.read_text().splitlines()]
    assert calls == [
        ["marketplace", "list", "--json"],
        ["marketplace", "update", "market"],
        ["marketplace", "list", "--json"],
    ]


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


# --- current-CLI audit (claude 2.1.318, 2026-11-04) ---------------------------------
# Each message below is the CLI's own text, captured from a live HOME-isolated probe.


def test_cli_marketplace_remove_not_declared_at_scope_maps_to_404(tmp_path: Path) -> None:
    # `marketplace remove --scope user` on a name only project settings declare: absent
    # at the confirmed scope, so the same 404 as an unknown name (was a generic 400).
    run, _calls = _fake_run(
        rc=1,
        stderr=(
            "✘ Failed to remove marketplace: Marketplace 'probe-mkt' is not declared in "
            "user settings. Omit --scope to remove it from all scopes."
        ),
    )
    with pytest.raises(plugins.MarketplaceNotFoundError):
        plugins.cli_marketplace_remove(str(FAKE_CLAUDE), tmp_path, "probe-mkt", "user", run=run)


@pytest.mark.parametrize(
    ("verb", "message"),
    [
        ("enable", 'Plugin "fmt@probe-mkt" is already enabled at project scope'),
        ("disable", 'Plugin "fmt@probe-mkt" is already disabled at project scope'),
        (
            "enable",
            'Plugin "fmt@probe-mkt" is installed at project scope, not user. Use --scope '
            "project or omit --scope to auto-detect.",
        ),
    ],
)
def test_cli_enable_disable_goal_state_and_scope_refusals_surface_as_400(
    tmp_path: Path, verb: str, message: str
) -> None:
    # Current CLIs exit 1 for both; neither is "not found", and neither is swallowed as
    # success: the dashboard badge is the MERGED state, so the CLI's message is what tells
    # the operator which scope already holds the value.
    run, _calls = _fake_run(rc=1, stderr=f'✘ Failed to {verb} plugin "fmt@probe-mkt": {message}')
    fn = plugins.cli_enable_plugin if verb == "enable" else plugins.cli_disable_plugin
    with pytest.raises(plugins.PluginCliError) as exc_info:
        fn(str(FAKE_CLAUDE), tmp_path, "fmt@probe-mkt", "project", run=run)
    assert not isinstance(exc_info.value, plugins.PluginNotFoundError)
    assert message.split(",")[0] in str(exc_info.value)


@pytest.mark.parametrize("fn_name", ["cli_install_plugin", "cli_update_plugin"])
def test_install_and_update_never_auto_accept_a_marketplace_command(
    tmp_path: Path, fn_name: str
) -> None:
    # `-y`/`--yes` and `--accept-command` exist on current CLIs; passing either would run a
    # marketplace-declared command unseen. The argv carries neither, and the CLI's
    # non-TTY refusal surfaces as a 400 instead.
    run, calls = _fake_run(
        rc=1, stdout="The command was only displayed. Re-run from a terminal to accept it."
    )
    with pytest.raises(plugins.PluginCliError) as exc_info:
        getattr(plugins, fn_name)(str(FAKE_CLAUDE), tmp_path, "x@m", "project", run=run)
    assert not isinstance(exc_info.value, plugins.PluginNotFoundError)
    argv = calls[0]["argv"]
    assert not {"-y", "--yes", "--accept-command"} & set(argv)
    assert calls[0]["kwargs"]["stdin"] is subprocess.DEVNULL


def test_cli_list_plugins_masks_plugin_mcp_server_credentials(tmp_path: Path) -> None:
    payload = [
        {
            "id": "deployer@acme-tools",
            "version": "1.2.0",
            "scope": "project",
            "enabled": True,
            "installPath": "/home/u/.claude/plugins/cache/acme-tools/deployer/1.2.0",
            "mcpServers": {
                "deploy": {
                    "command": "node",
                    "args": ["server.js", "--key", "ARGSECRET123"],
                    "env": {"REGION": "eu-west-1", "DEPLOY_KEY": "AKIA-literal"},
                    "headers": {"X-Registry": "literal-value"},
                },
                # A secret-shaped NAME must not over-mask the entry's type and location.
                "github-token": {"type": "http", "url": "https://mcp.example.com/x"},
            },
        }
    ]
    run, _calls = _fake_run(rc=0, stdout=json.dumps(payload))
    (got,) = plugins.cli_list_plugins(str(FAKE_CLAUDE), tmp_path, run=run)
    server = got["mcpServers"]["deploy"]
    assert server["env"] == {"REGION": cw.REDACTION_SENTINEL, "DEPLOY_KEY": cw.REDACTION_SENTINEL}
    assert server["headers"] == {"X-Registry": cw.REDACTION_SENTINEL}
    assert server["args"] == cw.REDACTION_SENTINEL
    assert server["command"] == "node"
    assert got["mcpServers"]["github-token"] == {
        "type": "http",
        "url": "https://mcp.example.com/x",
    }
    # What the dashboard renders stays intact.
    assert got["id"] == "deployer@acme-tools" and got["enabled"] is True
    assert got["installPath"] == payload[0]["installPath"]


@pytest.mark.parametrize(
    ("fn_name", "stderr"),
    [
        ("cli_list_plugins", "Error: settings file not found"),
        ("cli_list_marketplaces", "Error: known_marketplaces.json not found"),
    ],
)
def test_failing_reads_are_never_404(tmp_path: Path, fn_name: str, stderr: str) -> None:
    # The dashboard shows a 404 on a read as "this config surface is unavailable", the
    # same state as config-write being off: a real CLI failure must stay a 400.
    run, _calls = _fake_run(rc=1, stderr=stderr)
    with pytest.raises(plugins.PluginCliError) as exc_info:
        getattr(plugins, fn_name)(str(FAKE_CLAUDE), tmp_path, run=run)
    assert not isinstance(
        exc_info.value, plugins.PluginNotFoundError | plugins.MarketplaceNotFoundError
    )


@pytest.mark.parametrize("fn_name", ["cli_enable_plugin", "cli_uninstall_plugin"])
def test_not_declared_is_a_404_only_for_marketplace_remove(tmp_path: Path, fn_name: str) -> None:
    run, _calls = _fake_run(rc=1, stderr="Plugin 'x@m' is not declared in user settings")
    with pytest.raises(plugins.PluginCliError) as exc_info:
        getattr(plugins, fn_name)(str(FAKE_CLAUDE), tmp_path, "x@m", "user", run=run)
    assert not isinstance(exc_info.value, plugins.PluginNotFoundError)


def test_cli_list_marketplaces_masks_url_credentials(tmp_path: Path) -> None:
    payload = [
        {"name": "probe-mkt", "source": "directory", "path": "/srv/mkt"},
        {
            "name": "private",
            "source": "git",
            "url": "https://bot:ghp_secret@git.example.com/p.git",
        },
        {"name": "acme", "source": "github", "repo": "acme/plugins", "ref": "v2"},
        {"name": "claudeai-org", "source": "claudeai", "marketplaceId": "m1", "status": "ok"},
    ]
    run, _calls = _fake_run(rc=0, stdout=json.dumps(payload))
    got = plugins.cli_list_marketplaces(str(FAKE_CLAUDE), tmp_path, run=run)
    assert "ghp_secret" not in json.dumps(got)
    assert got[1]["url"] == cw.REDACTION_SENTINEL
    assert [got[0], got[2], got[3]] == [payload[0], payload[2], payload[3]]


def _write_project_settings(project_dir: Path, data: dict) -> None:
    settings = cw.project_settings_path(project_dir)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data), encoding="utf-8")


#: One `extraKnownMarketplaces` entry per documented marketplace source type
#: (settings-reference "Marketplace source types", marketplace-reference "Fields by type").
_DOCUMENTED_SOURCES = {
    "gh": {"source": {"source": "github", "repo": "acme/plugins", "ref": "v2", "path": "mk"}},
    "gh-sparse": {
        "source": {"source": "github", "repo": "acme/mono", "sparsePaths": [".claude-plugin"]},
        "autoUpdate": True,
    },
    "gl": {"source": {"source": "git", "url": "https://gitlab.example.com/t/p.git", "ref": "x"}},
    "hosted": {"source": {"source": "url", "url": "https://plugins.example.com/m.json"}},
    "f": {"source": {"source": "file", "path": "/opt/acme/marketplace.json"}},
    "d": {"source": {"source": "directory", "path": "/opt/acme"}, "autoUpdate": False},
    "team-tools": {
        "source": {
            "source": "settings",
            "name": "team-tools",
            "plugins": [{"name": "fmt", "source": {"source": "github", "repo": "acme/fmt"}}],
        }
    },
    "future": {"source": {"source": "some-new-type", "where": "elsewhere"}},
}


def test_read_marketplaces_passes_every_documented_source_type_through(tmp_path: Path) -> None:
    # Nothing is fetched, resolved, or dropped: every type (and an unknown future one)
    # reads back exactly as stored.
    _write_project_settings(tmp_path, {"extraKnownMarketplaces": _DOCUMENTED_SOURCES})
    assert plugins.read_project_marketplaces(tmp_path) == _DOCUMENTED_SOURCES


def test_read_marketplaces_masks_headers_and_headers_helper(tmp_path: Path) -> None:
    declared = {
        "private-hosted": {
            "source": {
                "source": "url",
                "url": "https://plugins.example.com/marketplace.json",
                "headers": {"Authorization": "Bearer ${TOKEN}", "Cookie": "session=literal"},
                "headersHelper": "/usr/local/bin/mint-token --secret literal",
            }
        },
        "inline": {
            "source": {
                "source": "settings",
                "name": "inline",
                "plugins": [
                    {
                        "name": "pkg",
                        "source": {"source": "archive", "url": "https://r.example.com/p.zip"},
                        "headers": {"X-Registry": "literal-value"},
                        "headersHelper": "print-headers",
                    }
                ],
            }
        },
        "cred-url": {"source": {"source": "git", "url": "https://bot:tok@git.example.com/r.git"}},
    }
    _write_project_settings(tmp_path, {"extraKnownMarketplaces": declared})
    got = plugins.read_project_marketplaces(tmp_path)
    text = json.dumps(got)
    for secret in ("${TOKEN}", "session=literal", "mint-token", "literal-value", "bot:tok"):
        assert secret not in text
    hosted = got["private-hosted"]["source"]
    assert hosted["headers"] == {
        "Authorization": cw.REDACTION_SENTINEL,
        "Cookie": cw.REDACTION_SENTINEL,
    }
    assert hosted["headersHelper"] == cw.REDACTION_SENTINEL
    assert hosted["url"] == "https://plugins.example.com/marketplace.json"  # no creds: kept
    entry = got["inline"]["source"]["plugins"][0]
    assert entry["headers"] == {"X-Registry": cw.REDACTION_SENTINEL}
    assert entry["headersHelper"] == cw.REDACTION_SENTINEL
    assert entry["name"] == "pkg"


def test_marketplace_add_failure_never_echoes_source_credentials(tmp_path: Path) -> None:
    # The failure detail quotes the identifier; for `marketplace add` that is the source,
    # and a git source can embed a token. It must not reach the 400 detail unmasked.
    run, _calls = _fake_run(rc=1, stderr="Invalid marketplace source format.")
    with pytest.raises(plugins.PluginCliError) as exc_info:
        plugins.cli_marketplace_add(
            str(FAKE_CLAUDE),
            tmp_path,
            "https://bot:ghp_tok@git.example.com/r.git",
            "user",
            run=run,
        )
    assert "ghp_tok" not in str(exc_info.value)
    assert "git.example.com/r.git" in str(exc_info.value)  # still diagnosable


def test_redact_plugin_display_masks_malformed_credential_fields_too() -> None:
    # A `headers`/`env` that is not the documented object still has every leaf masked
    # (structure kept); an empty `headersHelper` has nothing to hide and stays as stored.
    got = plugins.redact_plugin_display(
        {"headers": ["Cookie: a=1", {"X": "y"}], "env": "FOO=bar", "headersHelper": ""}
    )
    assert got == {
        "headers": [cw.REDACTION_SENTINEL, {"X": cw.REDACTION_SENTINEL}],
        "env": cw.REDACTION_SENTINEL,
        "headersHelper": "",
    }


@pytest.mark.parametrize("value", [["/bin/h", "--t", "SEK"], {"cmd": "SEK"}, "/bin/h --t SEK"])
def test_headers_helper_and_args_mask_whole_whatever_their_shape(value) -> None:
    # One sentinel for the whole value: the Settings tab restores a list by position, so
    # per-element sentinels would let a deleted element shift a stored secret.
    got = plugins.redact_plugin_display({"headersHelper": value, "args": value})
    assert got == {"headersHelper": cw.REDACTION_SENTINEL, "args": cw.REDACTION_SENTINEL}


def test_read_marketplaces_secret_shaped_name_does_not_mask_the_entry(tmp_path: Path) -> None:
    declared = {"auth-tools": {"source": {"source": "github", "repo": "acme/auth-tools"}}}
    _write_project_settings(tmp_path, {"extraKnownMarketplaces": declared})
    assert plugins.read_project_marketplaces(tmp_path) == declared


def test_read_marketplaces_empty_canonical_key_still_wins(tmp_path: Path) -> None:
    # Presence decides, not truthiness: `{}` under the canonical key ignores the alias.
    _write_project_settings(
        tmp_path,
        {
            "extraKnownMarketplaces": {},
            "additionalMarketplaces": {"ignored": {"source": {"source": "github", "repo": "a/b"}}},
        },
    )
    assert plugins.read_project_marketplaces(tmp_path) == {}


def test_read_marketplaces_reads_the_additional_marketplaces_alias(tmp_path: Path) -> None:
    alias = {"acme": {"source": {"source": "github", "repo": "acme/plugins"}}}
    _write_project_settings(tmp_path, {"additionalMarketplaces": alias})
    assert plugins.read_project_marketplaces(tmp_path) == alias


def test_read_marketplaces_canonical_key_wins_over_the_alias(tmp_path: Path) -> None:
    # Claude Code's own rule: with both spellings in one file, the alias is ignored.
    canonical = {"real": {"source": {"source": "directory", "path": "/opt/real"}}}
    _write_project_settings(
        tmp_path,
        {
            "extraKnownMarketplaces": canonical,
            "additionalMarketplaces": {"ignored": {"source": {"source": "github", "repo": "a/b"}}},
        },
    )
    assert plugins.read_project_marketplaces(tmp_path) == canonical


def test_read_enabled_plugins_accepts_every_documented_id_form(tmp_path: Path) -> None:
    # settings-reference `enabledPlugins`: `name@marketplace` -> bool, including the
    # `@synced` form for a claude.ai-synced plugin and a reserved `@skills-dir` id.
    enabled = {"fmt@team-tools": True, "notes@synced": False, "helper@skills-dir": False}
    _write_project_settings(tmp_path, {"enabledPlugins": enabled})
    assert plugins.read_project_enabled_plugins(tmp_path) == enabled


@pytest.mark.parametrize("plugin_id", ["notes@synced", "helper@skills-dir", "fmt@builtin"])
def test_validate_plugin_id_accepts_reserved_marketplace_suffixes(plugin_id: str) -> None:
    # The CLI takes `<name>@synced` for enable/disable (cli-reference) and lists
    # `@skills-dir` / `@builtin` ids; the shape check must not refuse them.
    plugins.validate_plugin_id(plugin_id)


def test_route_marketplaces_declared_masks_header_credentials(
    write_config, tmp_path, projects_root
) -> None:
    _write_project_settings(
        projects_root / "alpha",
        {
            "extraKnownMarketplaces": {
                "hosted": {
                    "source": {
                        "source": "url",
                        "url": "https://plugins.example.com/m.json",
                        "headers": {"X-Custom": "literal-credential"},
                    }
                }
            }
        },
    )
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/marketplaces/declared?scope=project&project=alpha")
        assert resp.status_code == 200
        assert "literal-credential" not in resp.text
        source = resp.json()["marketplaces"]["hosted"]["source"]
        assert source["headers"] == {"X-Custom": cw.REDACTION_SENTINEL}


def test_route_marketplaces_list_masks_url_credentials(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    payload = [{"name": "p", "source": "git", "url": "https://u:secret-tok@h.example/r.git"}]
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", json.dumps(payload))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/marketplaces?scope=project&project=alpha")
        assert resp.status_code == 200
        assert "secret-tok" not in resp.text


def test_route_marketplaces_action_remove_not_declared_is_404(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_EXIT_CODE", "1")
    monkeypatch.setenv(
        "FAKE_CLAUDE_PLUGIN_STDERR",
        "Failed to remove marketplace: Marketplace 'x' is not declared in project settings.",
    )
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


# --- TASK-523: the install scope of each listed plugin -------------------------------
#
# `plugin list --json` (claude 2.1.319) lists every install, other projects' too: a
# `user` entry, and `project`/`local` entries with the `projectPath` they belong to.


def _entry(pid: str, scope: str | None, project_path: Path | None = None, **extra) -> dict:
    """Build one `plugin list --json` entry in the shape claude 2.1.319 prints."""
    e: dict = {"id": pid, "version": "1.0.0", "enabled": True, **extra}
    if scope is not None:
        e["scope"] = scope
    if project_path is not None:
        e["projectPath"] = str(project_path)
    return e


def test_annotate_marks_only_installs_at_this_scope_and_project(tmp_path: Path) -> None:
    root = tmp_path
    alpha, beta = root / "alpha", root / "beta"
    alpha.mkdir()
    beta.mkdir()
    entries = [
        _entry("u@m", "user"),
        _entry("p@m", "project", alpha),
        _entry("l@m", "local", alpha),
        _entry("o@m", "project", beta),
        _entry("x@m", "project", tmp_path.parent / "elsewhere"),
        _entry("g@m", "managed"),
        _entry("n@m", None),
    ]
    at_project = {
        e["id"]: e for e in plugins.annotate_plugin_scopes(entries, "project", alpha, root)
    }
    assert {k for k, e in at_project.items() if e["atScope"]} == {"p@m"}
    assert at_project["p@m"]["projectName"] == "alpha"
    assert at_project["l@m"]["projectName"] == "alpha"
    assert at_project["o@m"]["projectName"] == "beta"  # another project's install
    assert at_project["x@m"]["projectName"] is None  # outside projects_root
    assert "projectName" not in at_project["u@m"]
    assert at_project["u@m"]["scope"] == "user"  # the CLI's own field is kept
    at_local = plugins.annotate_plugin_scopes(entries, "local", alpha, root)
    assert {e["id"] for e in at_local if e["atScope"]} == {"l@m"}
    at_user = plugins.annotate_plugin_scopes(entries, "user", None, root)
    assert {e["id"] for e in at_user if e["atScope"]} == {"u@m"}


def test_annotate_never_matches_a_missing_or_odd_project_path(tmp_path: Path) -> None:
    entries = [
        {"id": "a@m", "scope": "project"},
        {"id": "b@m", "scope": "project", "projectPath": 7},
        {"id": "c@m", "scope": "project", "projectPath": "bad\x00path"},
        "not-a-dict",
    ]
    out = plugins.annotate_plugin_scopes(entries, "project", tmp_path, tmp_path)
    assert [e["atScope"] for e in out[:3]] == [False, False, False]
    assert all(e["projectName"] is None for e in out[:3])
    assert out[3] == "not-a-dict"


def test_route_plugins_list_marks_the_install_scope(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    payload = [
        _entry("u@m", "user"),
        _entry("p@m", "project", projects_root / "alpha"),
        _entry("o@m", "project", projects_root / "beta"),
    ]
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", json.dumps(payload))
    with _client(write_config, tmp_path, _ON) as c:
        rows = c.get("/api/config-write/plugins?scope=project&project=alpha").json()["plugins"]
        user_rows = c.get("/api/config-write/plugins?scope=user").json()["plugins"]
    by_id = {r["id"]: r for r in rows}
    assert [by_id[k]["atScope"] for k in ("u@m", "p@m", "o@m")] == [False, True, False]
    assert by_id["u@m"]["scope"] == "user"
    assert by_id["o@m"]["projectName"] == "beta"
    assert [r["atScope"] for r in user_rows] == [True, False, False]


# --- TASK-517: what an update changed ------------------------------------------------


def test_describe_plugin_update_reports_the_version_change(tmp_path: Path) -> None:
    before = [_entry("d@m", "user", lastUpdated="t1")]
    after = [_entry("d@m", "user", version="1.1.0", lastUpdated="t2")]
    msg = plugins.describe_plugin_update("d@m", "user", None, before, after)
    assert msg == "Updated d@m from version 1.0.0 to version 1.1.0."


def test_describe_plugin_update_says_already_up_to_date(tmp_path: Path) -> None:
    same = [_entry("d@m", "user", lastUpdated="t1", installPath="/c/1.0.0")]
    msg = plugins.describe_plugin_update("d@m", "user", None, same, json.loads(json.dumps(same)))
    assert msg == "d@m is already up to date (version 1.0.0)."


def test_describe_plugin_update_same_version_but_reinstalled(tmp_path: Path) -> None:
    before = [_entry("d@m", "user", lastUpdated="t1")]
    after = [_entry("d@m", "user", lastUpdated="t2")]
    msg = plugins.describe_plugin_update("d@m", "user", None, before, after)
    assert msg == "Updated d@m. It is still at version 1.0.0."


def test_describe_plugin_update_matches_the_entry_at_this_scope_and_project(
    tmp_path: Path,
) -> None:
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    before = [
        _entry("d@m", "user", version="9.0.0"),
        _entry("d@m", "project", tmp_path / "beta", version="5.0.0"),
        _entry("d@m", "project", alpha, version="1.0.0", lastUpdated="t1"),
    ]
    after = [
        _entry("d@m", "user", version="9.0.0"),
        _entry("d@m", "project", tmp_path / "beta", version="6.0.0"),
        _entry("d@m", "project", alpha, version="1.0.0", lastUpdated="t1"),
    ]
    msg = plugins.describe_plugin_update("d@m", "project", alpha, before, after)
    assert msg == "d@m is already up to date (version 1.0.0)."


def test_describe_plugin_update_never_guesses_when_a_read_is_missing(tmp_path: Path) -> None:
    before = [_entry("d@m", "user")]
    unread = plugins.describe_plugin_update("d@m", "user", None, before, None)
    assert unread == (
        "The update of d@m ran, but Clauster could not read the installed version afterwards."
    )
    gone = plugins.describe_plugin_update("d@m", "user", None, before, [])
    assert gone == "The update of d@m ran, but the plugin is no longer listed at this scope."
    fresh = plugins.describe_plugin_update("d@m", "user", None, [], [_entry("d@m", "user")])
    assert fresh == "Updated d@m. It is now at version 1.0.0."
    no_version = [{"id": "d@m", "scope": "user"}]
    assert plugins.describe_plugin_update("d@m", "user", None, no_version, no_version) == (
        "d@m is already up to date (version unknown)."
    )


def test_describe_plugin_update_masks_the_message() -> None:
    leak = "https://u:tok3n-value@h.example/x"
    before = [_entry("d@m", "user")]
    after = [_entry("d@m", "user", version=leak)]
    msg = plugins.describe_plugin_update("d@m", "user", None, before, after)
    assert "tok3n-value" not in msg
    assert msg.startswith("Updated d@m from version 1.0.0 to version ")


def _write_marketplace(location: Path, names: list[str]) -> None:
    """Write a marketplace checkout at ``location`` offering plugins ``names``."""
    (location / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    doc = {"name": "m", "plugins": [{"name": n, "source": f"./plugins/{n}"} for n in names]}
    (location / ".claude-plugin" / "marketplace.json").write_bytes(json.dumps(doc).encode())


def test_read_marketplace_plugin_names_dir_file_and_failures(tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    _write_marketplace(checkout, ["a", "b"])
    assert plugins._read_marketplace_plugin_names(str(checkout)) == ["a", "b"]
    file_only = checkout / ".claude-plugin" / "marketplace.json"
    assert plugins._read_marketplace_plugin_names(str(file_only)) == ["a", "b"]
    assert plugins._read_marketplace_plugin_names(str(tmp_path / "absent")) is None
    assert plugins._read_marketplace_plugin_names(None) is None
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"{not json")
    assert plugins._read_marketplace_plugin_names(str(bad)) is None
    no_list = tmp_path / "nolist.json"
    no_list.write_bytes(b'{"plugins": {"a": 1}}')
    assert plugins._read_marketplace_plugin_names(str(no_list)) is None
    odd = tmp_path / "odd.json"
    odd.write_bytes(b'{"plugins": [{"name": "a"}, {"name": 3}, "x"]}')
    assert plugins._read_marketplace_plugin_names(str(odd)) == ["a", "", ""]
    monkeypatch.setattr(plugins, "MARKETPLACE_FILE_MAX_BYTES", 8)
    assert plugins._read_marketplace_plugin_names(str(checkout)) is None  # oversized


def test_read_marketplace_plugin_names_unreadable_file_is_none(
    tmp_path: Path, monkeypatch
) -> None:
    checkout = tmp_path / "checkout"
    _write_marketplace(checkout, ["a"])

    def denied(*_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", denied)
    assert plugins._read_marketplace_plugin_names(str(checkout)) is None


@pytest.mark.skipif(sys.platform == "win32", reason="mkfifo is POSIX-only")
def test_read_marketplace_plugin_names_never_opens_a_fifo(tmp_path: Path) -> None:
    import os

    fifo = tmp_path / "pipe.json"
    os.mkfifo(fifo)
    assert plugins._read_marketplace_plugin_names(str(fifo)) is None  # would block if opened


def test_marketplace_plugin_names_reads_each_install_location(tmp_path: Path) -> None:
    _write_marketplace(tmp_path / "one", ["a"])
    listing = [
        {"name": "one", "source": "directory", "installLocation": str(tmp_path / "one")},
        {"name": "two", "source": "github", "installLocation": str(tmp_path / "missing")},
        {"source": "nameless"},
    ]
    run, calls = _fake_run(stdout=json.dumps(listing))
    assert plugins.marketplace_plugin_names("claude", tmp_path, None, run=run) == {
        "one": ["a"],
        "two": None,
    }
    assert plugins.marketplace_plugin_names("claude", tmp_path, "one", run=run) == {"one": ["a"]}
    assert calls[0]["argv"][1:] == ["plugin", "marketplace", "list", "--json"]


def test_describe_marketplace_update_unchanged_and_changed() -> None:
    same = plugins.describe_marketplace_update("m", {"m": ["a", "b"]}, {"m": ["b", "a"]})
    assert same == "Refreshed marketplace m. Its plugin list did not change (2 plugins)."
    one = plugins.describe_marketplace_update("m", {"m": ["a"]}, {"m": ["a"]})
    assert one == "Refreshed marketplace m. Its plugin list did not change (1 plugin)."
    grew = plugins.describe_marketplace_update("m", {"m": ["a", "b"]}, {"m": ["a", "b", "c"]})
    assert grew == ("Refreshed marketplace m. It now offers 3 plugins (before: 2). Added: c.")
    shrank = plugins.describe_marketplace_update("m", {"m": ["a", "b"]}, {"m": ["a"]})
    assert shrank == "Refreshed marketplace m. It now offers 1 plugin (before: 2). Removed: b."
    swapped = plugins.describe_marketplace_update("m", {"m": ["a", "b"]}, {"m": ["a", "z"]})
    assert swapped == (
        "Refreshed marketplace m. It now offers 2 plugins (before: 2). Added: z. Removed: b."
    )


def test_describe_marketplace_update_caps_the_names_shown() -> None:
    new = [f"p{i}" for i in range(8)] + ["x" * 100]
    msg = plugins.describe_marketplace_update("m", {"m": []}, {"m": new})
    assert "Added: p0, p1, p2, p3, p4 and 4 more." in msg
    long = plugins.describe_marketplace_update("m", {"m": []}, {"m": ["y" * 100]})
    assert "y" * 81 not in long and "y" * 80 + "…" in long


def test_describe_marketplace_update_masks_names_before_the_cut() -> None:
    # A short credential URL as a plugin name: the final mask of the sentence hides it.
    short = plugins.describe_marketplace_update("m", {"m": []}, {"m": ["https://u:pw-x@h.io/"]})
    assert "pw-x" not in short
    # A long one: a cut inside `user:pass@` would drop the `@` the mask keys on.
    long_secret = "https://u:" + "S" * 100 + "@h.example/x"
    long = plugins.describe_marketplace_update("m", {"m": []}, {"m": [long_secret]})
    assert "SSSS" not in long


def test_read_marketplace_plugin_names_refuses_a_symlink_out_of_the_checkout(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"plugins": [{"name": "leak"}]}')
    checkout = tmp_path / "checkout"
    (checkout / ".claude-plugin").mkdir(parents=True)
    try:
        (checkout / ".claude-plugin" / "marketplace.json").symlink_to(outside)
    except OSError:
        pytest.skip("this host cannot create symlinks")
    assert plugins._read_marketplace_plugin_names(str(checkout)) is None


def test_describe_marketplace_update_never_guesses_an_unread_list() -> None:
    unread = (
        "Refreshed marketplace m. Clauster could not read its plugin list, so it cannot tell "
        "whether the list changed."
    )
    assert plugins.describe_marketplace_update("m", {"m": None}, {"m": ["a"]}) == unread
    assert plugins.describe_marketplace_update("m", {"m": ["a"]}, None) == unread
    assert plugins.describe_marketplace_update("m", {}, {}) == unread  # not listed at all
    every = plugins.describe_marketplace_update(None, {"a": ["x"], "b": ["y"]}, {"a": ["x"]})
    assert every.startswith("Refreshed marketplace a. Its plugin list did not change (1 plugin).")
    assert "Refreshed marketplace b. Clauster could not read its plugin list" in every
    assert plugins.describe_marketplace_update(None, {}, {}) == (
        "Refreshed the marketplaces. No marketplace is listed."
    )


def _update_plugin(c: TestClient) -> dict:
    resp = c.post(
        "/api/config-write/plugins/action",
        json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "op": "update", "plugin": "d@m"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_route_plugin_update_says_already_up_to_date(write_config, tmp_path, monkeypatch) -> None:
    # The fake lists the same entry before and after: the CLI changed nothing.
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_LIST_STDOUT", json.dumps([_entry("d@m", "user")]))
    with _client(write_config, tmp_path, _ON) as c:
        body = _update_plugin(c)
    assert body["message"] == "d@m is already up to date (version 1.0.0)."


def test_route_plugin_update_reports_the_new_version(write_config, tmp_path, monkeypatch) -> None:
    reads = iter([[_entry("d@m", "user")], [_entry("d@m", "user", version="1.2.0")]])
    monkeypatch.setattr(plugins, "cli_list_plugins", lambda *a, **k: next(reads))
    with _client(write_config, tmp_path, _ON) as c:
        body = _update_plugin(c)
    assert body["message"] == "Updated d@m from version 1.0.0 to version 1.2.0."


def test_route_plugin_update_at_project_scope_matches_the_project_install(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    # The route must pass the project dir: a user-scope-only match would say "no longer
    # listed" for every project or local update.
    alpha = projects_root / "alpha"
    reads = iter(
        [
            [_entry("d@m", "project", alpha)],
            [_entry("d@m", "project", alpha, version="1.1.0")],
        ]
    )
    monkeypatch.setattr(plugins, "cli_list_plugins", lambda *a, **k: next(reads))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "update",
                "plugin": "d@m",
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "Updated d@m from version 1.0.0 to version 1.1.0."


@pytest.mark.parametrize(
    ("route", "body"),
    [
        (
            "/api/config-write/plugins/action",
            {
                "scope": "project",
                "project": "alpha",
                "confirm": "nope",
                "op": "update",
                "plugin": "d@m",
            },
        ),
        (
            "/api/config-write/plugins/action",
            {
                "scope": "project",
                "project": "../x",
                "confirm": "../x",
                "op": "update",
                "plugin": "d@m",
            },
        ),
        (
            "/api/config-write/marketplaces/action",
            {
                "scope": "project",
                "project": "alpha",
                "confirm": "nope",
                "op": "update",
                "name": "m",
            },
        ),
        (
            "/api/config-write/marketplaces/action",
            {
                "scope": "project",
                "project": "../x",
                "confirm": "../x",
                "op": "update",
                "name": "m",
            },
        ),
    ],
)
def test_route_update_refused_by_a_gate_spawns_nothing(
    write_config, tmp_path, projects_root, monkeypatch, route, body
) -> None:
    # The before-read must sit after the confirm and path gates, so a refusal runs no CLI.
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_LOG", str(argv_log))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(route, json=body)
    assert resp.status_code == 400
    assert not argv_log.exists()


def test_route_plugin_update_failed_read_after_is_reported_not_400(
    write_config, tmp_path, monkeypatch
) -> None:
    calls = {"n": 0}

    def flaky(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise plugins.PluginCliError("claude plugin list exited 1: boom")
        return [_entry("d@m", "user")]

    monkeypatch.setattr(plugins, "cli_list_plugins", flaky)
    with _client(write_config, tmp_path, _ON) as c:
        body = _update_plugin(c)
    assert "could not read the installed version afterwards" in body["message"]


def test_route_plugin_update_failed_read_before_runs_nothing(
    write_config, tmp_path, monkeypatch
) -> None:
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_LOG", str(argv_log))
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_EXIT_CODE_LIST", "1")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "op": "update",
                "plugin": "d@m",
            },
        )
    assert resp.status_code == 400
    calls = [json.loads(line)["argv"] for line in argv_log.read_text().splitlines()]
    assert calls == [["list", "--json"]]  # the update itself never ran


def test_route_plugin_non_update_ops_carry_no_message(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/plugins/action",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "disable",
                "plugin": "d@m",
            },
        )
    assert resp.status_code == 200
    assert "message" not in resp.json()


def _update_marketplace(c: TestClient) -> dict:
    resp = c.post(
        "/api/config-write/marketplaces/action",
        json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "op": "update", "name": "m"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (["a", "b"], "Refreshed marketplace m. Its plugin list did not change (2 plugins)."),
        (
            ["a", "b", "c"],
            "Refreshed marketplace m. It now offers 3 plugins (before: 2). Added: c.",
        ),
    ],
)
def test_route_marketplace_update_reports_the_plugin_list(
    write_config, tmp_path, monkeypatch, after, expected
) -> None:
    checkout = tmp_path / "mkt-checkout"
    _write_marketplace(checkout, ["a", "b"])
    listing = [{"name": "m", "source": "directory", "installLocation": str(checkout)}]
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_MARKETPLACE_LIST_STDOUT", json.dumps(listing))
    real_update = plugins.cli_marketplace_update

    def update_and_refresh(binary, cwd, name, **kwargs):
        real_update(binary, cwd, name, **kwargs)
        _write_marketplace(checkout, after)  # what the CLI's refresh would leave on disk

    monkeypatch.setattr(plugins, "cli_marketplace_update", update_and_refresh)
    with _client(write_config, tmp_path, _ON) as c:
        body = _update_marketplace(c)
    assert body["message"] == expected


def test_route_marketplace_update_failed_read_before_runs_nothing(
    write_config, tmp_path, monkeypatch
) -> None:
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_LOG", str(argv_log))
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_EXIT_CODE_MARKETPLACE_LIST", "1")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/marketplaces/action",
            json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "op": "update", "name": "m"},
        )
    assert resp.status_code == 400
    calls = [json.loads(line)["argv"] for line in argv_log.read_text().splitlines()]
    assert calls == [["marketplace", "list", "--json"]]


def test_route_marketplace_update_failed_read_after_is_reported_not_400(
    write_config, tmp_path, monkeypatch
) -> None:
    calls = {"n": 0}

    def flaky(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise plugins.PluginCliError("claude plugin marketplace list exited 1: boom")
        return {"m": ["a"]}

    monkeypatch.setattr(plugins, "marketplace_plugin_names", flaky)
    with _client(write_config, tmp_path, _ON) as c:
        body = _update_marketplace(c)
    assert "could not read its plugin list" in body["message"]
