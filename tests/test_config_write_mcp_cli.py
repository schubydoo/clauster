"""CLI-driven MCP add/remove/edit + enable/disable (#769), over the #766 Foundation.

Three layers, mirroring the module split:

* :mod:`clauster.config_write_mcp_cli` unit tests — every ``claude mcp`` invocation
  is fully STUBBED via an injected ``run`` callable (never the real binary/account):
  exact argv, cwd, and env are asserted, proving a secret rides the child env (or
  never leaves the process at all) and never argv.
* :mod:`clauster.config_write_mcp`'s #769 additions — the approval-list validator/
  read/write, and the secret-safe single-entry merge writers (the CLI-bypass path).
* The gated ``/api/config-write/mcp/*`` routes — exercised through a *fake* `claude`
  binary (``tests/fixtures/fake_claude/claude``'s ``mcp`` subcommand, scripted via
  env vars), never the real CLI, under the autouse HOME-isolation fixture.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import claude_cli
from clauster import config_write as cw
from clauster import config_write_mcp as mcp
from clauster import config_write_mcp_cli as mcp_cli
from clauster.app import create_app
from clauster.config import load_config

# The fake `claude` stub is an extensionless POSIX shebang script; Windows CreateProcess
# can't launch it directly ([WinError 193]), so on Windows the tests point at the same-named
# `.cmd` wrapper (`_WIN_STUB_SUFFIX`), which shells it through `python` — the established
# idiom in test_ops.py / test_provisioning.py. The route tests that spawn the real
# `claude mcp` subprocess then run on every platform (the pure-unit tests that inject a fake
# `run=` callable were already cross-platform).
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


# --- record_cli_argv (#958 P6 argv capture) ----------------------------------------


def test_record_cli_argv_noop_without_sink() -> None:
    # No active sink -> no-op, never raises (the CLI runs normally, uncaptured).
    cw.record_cli_argv("mcp", ["add-json", "srv"])


def test_record_cli_argv_captures_and_redacts() -> None:
    sink: list = []
    token = cw.cli_argv_sink.set(sink)
    try:
        cw.record_cli_argv("plugin", ["marketplace", "add", "${MARKET_TOKEN}"])
    finally:
        cw.cli_argv_sink.reset(token)
    assert sink[0][:3] == ["plugin", "marketplace", "add"]
    # A ${…} interpolation is masked in place (defense-in-depth) — never the raw value.
    assert "${MARKET_TOKEN}" not in sink[0][3] and cw.REDACTION_SENTINEL in sink[0][3]


# --- entry_needs_direct_write (routing predicate) -----------------------------------


def test_entry_needs_direct_write_detects_secret_keyed_env() -> None:
    assert mcp_cli.entry_needs_direct_write({"command": "x", "env": {"API_TOKEN": "sk-real"}})


def test_entry_needs_direct_write_detects_secretish_header() -> None:
    assert mcp_cli.entry_needs_direct_write(
        {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer sk-x"}}
    )


def test_entry_needs_direct_write_false_for_clean_entry() -> None:
    assert not mcp_cli.entry_needs_direct_write({"command": "x", "args": ["--flag"]})


def test_entry_needs_direct_write_true_for_interpolation_placeholder() -> None:
    # A `${VAR}` placeholder is secret-SHAPED (conservative masking direction) even
    # though it holds no literal value -- it still gets routed away from the CLI.
    assert mcp_cli.entry_needs_direct_write({"command": "x", "env": {"TOKEN": "${MY_TOKEN}"}})


@pytest.mark.parametrize(
    "entry",
    [
        # HIGH must-fix: a real secret under a BENIGN key — redact_secrets (key-name
        # detection) misses these, so they must still be forced off the CLI argv.
        {"command": "srv", "env": {"DEPLOY_KEY": "AKIAEXAMPLE1234567890"}},
        {"command": "srv", "env": {"GH_PAT": "ghp_exampletokenvalue000"}},
        {"type": "http", "url": "https://x/mcp", "headers": {"X-Custom": "Bearer sk-benignkey"}},
    ],
)
def test_entry_needs_direct_write_true_for_benign_keyed_env_or_headers(entry: dict) -> None:
    # redact_secrets alone would NOT flag these (proving the key-name gap is real),
    # yet the routing predicate must — err toward has-secret on any env/headers value.
    assert cw.redact_secrets(entry) == entry  # key-name detection misses it
    assert mcp_cli.entry_needs_direct_write(entry)  # but the predicate still routes it away


def test_entry_needs_direct_write_false_for_empty_env_and_headers() -> None:
    # An empty (or blank-valued) env/headers block carries no secret -> CLI path is fine.
    assert not mcp_cli.entry_needs_direct_write({"command": "x", "env": {}})
    assert not mcp_cli.entry_needs_direct_write({"command": "x", "env": {"K": ""}})


# --- cli_add_server: argv shape, secret refusal, error classification --------------


def test_cli_add_server_builds_expected_argv(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0, stdout="Added stdio MCP server srv to project config")
    mcp_cli.cli_add_server(
        str(FAKE_CLAUDE), tmp_path, "srv", {"command": "node"}, "project", run=run
    )
    assert len(calls) == 1
    argv, kwargs = calls[0]["argv"], calls[0]["kwargs"]
    assert Path(argv[0]).name.startswith("claude")
    assert argv[1:] == [
        "mcp",
        "add-json",
        "srv",
        json.dumps({"command": "node"}),
        "--scope",
        "project",
    ]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs.get("shell", False) is False
    assert "MCP_CLIENT_SECRET" not in kwargs["env"]


def test_cli_add_server_client_secret_via_env_never_argv(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0, stdout="Added http MCP server remote to local config")
    mcp_cli.cli_add_server(
        str(FAKE_CLAUDE),
        tmp_path,
        "remote",
        {"type": "http", "url": "https://x/mcp"},
        "local",
        client_secret="sk-oauth-secret",
        run=run,
    )
    argv, kwargs = calls[0]["argv"], calls[0]["kwargs"]
    assert "sk-oauth-secret" not in argv
    assert "--client-secret" in argv
    assert kwargs["env"]["MCP_CLIENT_SECRET"] == "sk-oauth-secret"


def test_cli_add_server_refuses_entry_with_inline_env(tmp_path: Path) -> None:
    run, calls = _fake_run()
    with pytest.raises(ValueError, match="inline env/headers"):
        mcp_cli.cli_add_server(
            str(FAKE_CLAUDE),
            tmp_path,
            "srv",
            {"command": "x", "env": {"API_TOKEN": "sk-real-secret"}},
            "project",
            run=run,
        )
    assert calls == []  # never spawned


def test_cli_add_server_rejects_bad_shape_before_spawn(tmp_path: Path) -> None:
    run, calls = _fake_run()
    with pytest.raises(cw.InvalidCandidateError):
        mcp_cli.cli_add_server(str(FAKE_CLAUDE), tmp_path, "srv", {"bogus": 1}, "project", run=run)
    assert calls == []


def test_cli_add_server_already_exists_maps_to_server_exists_error(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=1, stderr="MCP server srv already exists in .mcp.json")
    with pytest.raises(mcp.ServerExistsError):
        mcp_cli.cli_add_server(
            str(FAKE_CLAUDE), tmp_path, "srv", {"command": "x"}, "project", run=run
        )


def test_cli_add_server_other_failure_maps_to_mcp_cli_error(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=1, stderr="boom: disk full")
    with pytest.raises(mcp_cli.McpCliError):
        mcp_cli.cli_add_server(
            str(FAKE_CLAUDE), tmp_path, "srv", {"command": "x"}, "project", run=run
        )


def test_cli_add_server_error_detail_is_redacted(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=1, stderr="TOKEN: sk-should-not-leak\nother line")
    with pytest.raises(mcp_cli.McpCliError) as exc_info:
        mcp_cli.cli_add_server(
            str(FAKE_CLAUDE), tmp_path, "srv", {"command": "x"}, "project", run=run
        )
    assert "sk-should-not-leak" not in str(exc_info.value)


# --- cli_remove_server ---------------------------------------------------------------


def test_cli_remove_server_builds_expected_argv(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0, stdout="Removed MCP server srv from project config")
    mcp_cli.cli_remove_server(str(FAKE_CLAUDE), tmp_path, "srv", "project", run=run)
    assert calls[0]["argv"][1:] == ["mcp", "remove", "srv", "--scope", "project"]


def test_cli_remove_server_not_found_raises_by_default(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=1, stderr='No MCP server named "srv" in local scope')
    with pytest.raises(mcp.ServerNotFoundError):
        mcp_cli.cli_remove_server(str(FAKE_CLAUDE), tmp_path, "srv", "local", run=run)


def test_cli_remove_server_not_found_ignored_when_requested(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=1, stderr='No MCP server named "srv" in local scope')
    mcp_cli.cli_remove_server(
        str(FAKE_CLAUDE), tmp_path, "srv", "local", ignore_missing=True, run=run
    )  # no raise


# --- cli_edit_server: remove (ignore-missing) + re-add -----------------------------


def test_cli_edit_server_calls_remove_then_add(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[2] == "remove":
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr='No MCP server named "srv" in project scope'
            )
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    mcp_cli.cli_edit_server(
        str(FAKE_CLAUDE), tmp_path, "srv", {"command": "y"}, "project", run=run
    )
    assert len(calls) == 2
    assert calls[0][2] == "remove"
    assert calls[1][2] == "add-json"


def test_cli_edit_server_readd_failure_restores_prior(tmp_path: Path) -> None:
    # MUST-FIX #4: remove succeeds, re-add fails -> the prior definition is restored via
    # the injected `restore` closure (which returns True), and the error says so.
    restored: list[bool] = []

    def run(argv, **_kwargs):
        if argv[2] == "remove":
            return subprocess.CompletedProcess(argv, 0, stdout="removed", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom: re-add failed")

    def restore() -> bool:
        restored.append(True)
        return True  # a real prior existed and was restored

    with pytest.raises(mcp_cli.McpCliError, match="previous definition was restored"):
        mcp_cli.cli_edit_server(
            str(FAKE_CLAUDE),
            tmp_path,
            "srv",
            {"command": "y"},
            "project",
            restore=restore,
            run=run,
        )
    assert restored == [True]  # restore WAS attempted


def test_cli_edit_server_readd_failure_no_prior_says_nothing_to_restore(tmp_path: Path) -> None:
    # GREPTILE P2a: remove succeeds (nothing there), re-add fails, restore reports NO prior
    # (returns False) -> the message must NOT falsely claim a restore; it says no server is
    # present / nothing to restore.
    def run(argv, **_kwargs):
        if argv[2] == "remove":
            return subprocess.CompletedProcess(argv, 0, stdout="removed", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    def restore() -> bool:
        return False  # no prior definition existed

    with pytest.raises(mcp_cli.McpCliError) as exc_info:
        mcp_cli.cli_edit_server(
            str(FAKE_CLAUDE),
            tmp_path,
            "srv",
            {"command": "y"},
            "project",
            restore=restore,
            run=run,
        )
    msg = str(exc_info.value)
    assert "nothing to restore" in msg
    assert "restored" not in msg  # never falsely claim a restoration


def test_cli_edit_server_readd_and_restore_both_fail_surfaces_loss(tmp_path: Path) -> None:
    # remove succeeds, re-add fails, AND restore fails -> the error must explicitly state
    # the server is now missing (loss surfaced loudly, never silent-by-omission).
    def run(argv, **_kwargs):
        if argv[2] == "remove":
            return subprocess.CompletedProcess(argv, 0, stdout="removed", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    def restore() -> bool:
        raise RuntimeError("disk full")

    with pytest.raises(mcp_cli.McpCliError, match="now missing"):
        mcp_cli.cli_edit_server(
            str(FAKE_CLAUDE),
            tmp_path,
            "srv",
            {"command": "y"},
            "project",
            restore=restore,
            run=run,
        )


def test_cli_edit_server_readd_failure_no_restore_surfaces_loss(tmp_path: Path) -> None:
    # remove succeeds, re-add fails, no restore closure given -> loss surfaced loudly.
    def run(argv, **_kwargs):
        if argv[2] == "remove":
            return subprocess.CompletedProcess(argv, 0, stdout="removed", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    with pytest.raises(mcp_cli.McpCliError, match="now missing"):
        mcp_cli.cli_edit_server(
            str(FAKE_CLAUDE), tmp_path, "srv", {"command": "y"}, "project", run=run
        )


def test_cli_edit_server_addserver_valueerror_after_remove_triggers_restore(
    tmp_path: Path,
) -> None:
    # GREPTILE P2b: cli_add_server raises ValueError (entry needs the direct writer) — if
    # that fires AFTER the remove succeeded, cli_edit_server must catch it so `restore`
    # still runs (rather than the ValueError escaping and leaving the server deleted).
    restored: list[bool] = []

    # remove exits 0; the re-add never reaches the CLI because entry_needs_direct_write is
    # True -> cli_add_server raises ValueError before spawning.
    def run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    def restore() -> bool:
        restored.append(True)
        return True

    with pytest.raises(mcp_cli.McpCliError, match="previous definition was restored"):
        mcp_cli.cli_edit_server(
            str(FAKE_CLAUDE),
            tmp_path,
            "srv",
            {"command": "y", "env": {"API_TOKEN": "sk-real"}},  # -> needs direct write
            "project",
            restore=restore,
            run=run,
        )
    assert restored == [True]  # the ValueError was caught and restore ran


# --- cli_reset_project_choices -------------------------------------------------------


def test_cli_reset_project_choices_builds_expected_argv(tmp_path: Path) -> None:
    run, calls = _fake_run(rc=0, stdout="reset")
    mcp_cli.cli_reset_project_choices(str(FAKE_CLAUDE), tmp_path, run=run)
    assert calls[0]["argv"][1:] == ["mcp", "reset-project-choices"]
    assert calls[0]["kwargs"]["cwd"] == str(tmp_path)


def test_cli_reset_project_choices_failure_raises_mcp_cli_error(tmp_path: Path) -> None:
    run, _calls = _fake_run(rc=1, stderr="boom")
    with pytest.raises(mcp_cli.McpCliError):
        mcp_cli.cli_reset_project_choices(str(FAKE_CLAUDE), tmp_path, run=run)


# --- spawn-level failure modes (validate-before-spawn) ------------------------------


def test_resolve_binary_not_found_propagates(tmp_path: Path) -> None:
    with pytest.raises(claude_cli.ClaudeNotFound):
        mcp_cli.cli_add_server(
            "definitely-not-a-real-binary-xyz", tmp_path, "srv", {"command": "x"}, "project"
        )


def test_run_timeout_raises_mcp_cli_error(tmp_path: Path) -> None:
    def run(argv, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    with pytest.raises(mcp_cli.McpCliError):
        mcp_cli.cli_add_server(
            str(FAKE_CLAUDE), tmp_path, "srv", {"command": "x"}, "project", run=run
        )


def test_run_oserror_raises_redacted_mcp_cli_error(tmp_path: Path) -> None:
    # A spawn OSError (e.g. exec of a non-executable) -> McpCliError, str(exc) redacted.
    def run(argv, **_kwargs):
        raise OSError("TOKEN: sk-should-not-leak")

    with pytest.raises(mcp_cli.McpCliError) as exc_info:
        mcp_cli.cli_add_server(
            str(FAKE_CLAUDE), tmp_path, "srv", {"command": "x"}, "project", run=run
        )
    assert "sk-should-not-leak" not in str(exc_info.value)
    assert "failed to run" in str(exc_info.value)


def test_run_timeout_message_never_leaks_argv(tmp_path: Path) -> None:
    # MUST-FIX #2: TimeoutExpired.__str__ embeds the whole command (incl. the entry JSON,
    # which for an OAuth add could carry a secret). The raised message must be built from
    # the verb only, never str(exc). Prove it: a secret-shaped value in the entry (routed
    # here only because we call cli_add_server directly with a client_secret) must not
    # appear in the error text.
    entry = {"type": "http", "url": "https://x/mcp"}

    def run(argv, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

    with pytest.raises(mcp_cli.McpCliError) as exc_info:
        mcp_cli.cli_add_server(
            str(FAKE_CLAUDE),
            tmp_path,
            "remote",
            entry,
            "local",
            client_secret="sk-oauth-should-not-leak",  # noqa: S106 - test literal
            run=run,
        )
    msg = str(exc_info.value)
    assert "sk-oauth-should-not-leak" not in msg
    assert "https://x/mcp" not in msg  # the argv (entry JSON) is not in the message
    assert "add-json" in msg  # but the verb is
    assert "timed out" in msg


# --- MUST-FIX #3: server-name arg-injection validation (at the VALIDATOR level) -----


@pytest.mark.parametrize(
    "name",
    ["--scope", "--client-secret", "-e", "-s", "--transport", "-", "--"],
)
def test_validator_rejects_option_like_server_name(name: str) -> None:
    # A name that looks like a CLI option would be parsed by `claude mcp` as a flag
    # (arg-injection / positional shift, verified live). The structural validator must
    # reject it -> 422, nothing spawned. Tested at the validator (the fake stub ignores
    # argv semantics, so an end-to-end test couldn't prove the parser behavior).
    with pytest.raises(cw.InvalidCandidateError):
        mcp.validate_mcp_servers({name: {"command": "x"}})


@pytest.mark.parametrize(
    "name",
    ["srv", "github", "context-mode", "my_server", "srv.v2", "A1", "_x"],
)
def test_validator_accepts_sane_server_names(name: str) -> None:
    mcp.validate_mcp_servers({name: {"command": "x"}})  # no raise


@pytest.mark.parametrize(
    "name",
    ["has space", "semi;colon", "pipe|x", "quote'x", "back`tick", "dollar$x", "slash/x"],
)
def test_validator_rejects_shell_metachar_server_names(name: str) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        mcp.validate_mcp_servers({name: {"command": "x"}})


# --- config_write_mcp #769 additions: approval-list validator ----------------------


def test_validate_approvals_accepts_valid_shapes() -> None:
    mcp.validate_approvals({})
    mcp.validate_approvals({"enabled": []})
    mcp.validate_approvals({"enabled": ["a"], "disabled": ["b"]})


@pytest.mark.parametrize(
    "candidate",
    [
        ["not", "a", "dict"],
        {"enabled": "not-a-list"},
        {"disabled": [1, 2]},
        {"enabled": ["a", "a"]},  # duplicate within one list
        {"disabled": ["b", "b"]},
        {"enabled": ["a"], "disabled": ["a"]},  # self-contradicting overlap
        {"enabled": [""]},  # empty name
        {"bogus": []},  # unknown key
    ],
)
def test_validate_approvals_rejects_bad_shape(candidate: object) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        mcp.validate_approvals(candidate)


# --- config_write_mcp #769 additions: approval read/write --------------------------


def test_project_approvals_round_trip(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": [],
        "disabled": [],
        "locked": [],
    }
    mcp.write_project_approvals(cj, project_dir, ["a"], ["b"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["a"],
        "disabled": ["b"],
        "locked": [],
    }


def test_project_approvals_write_preserves_siblings(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    cj.write_text(
        json.dumps(
            {
                "projects": {
                    str(project_dir.resolve()): {
                        "hasTrustDialogAccepted": True,
                        "mcpServers": {"x": {"command": "y"}},
                    }
                },
                "misc": 1,
            }
        ),
        encoding="utf-8",
    )
    mcp.write_project_approvals(cj, project_dir, ["a"], [])
    out = json.loads(cj.read_text(encoding="utf-8"))
    entry = out["projects"][str(project_dir.resolve())]
    assert entry["hasTrustDialogAccepted"] is True
    assert entry["mcpServers"] == {"x": {"command": "y"}}
    assert entry["enabledMcpjsonServers"] == ["a"]
    assert entry["disabledMcpjsonServers"] == []
    assert out["misc"] == 1


def test_project_approvals_rejects_bad_shape_without_writing(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    with pytest.raises(cw.InvalidCandidateError):
        mcp.write_project_approvals(cj, project_dir, ["a"], ["a"])
    assert not cj.exists()


# --- config_write_mcp #769 additions: secret-safe single-entry merge writers -------


def test_write_project_server_entry_add_merges_with_existing(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}}}), encoding="utf-8"
    )
    mcp.write_project_server_entry(
        tmp_path, "b", {"command": "y", "env": {"TOKEN": "sk-real"}}, op="add"
    )
    out = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert out["mcpServers"]["a"] == {"command": "x"}
    assert out["mcpServers"]["b"]["env"]["TOKEN"] == "sk-real"


def test_write_project_server_entry_add_conflict_on_existing_name(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}}}), encoding="utf-8"
    )
    with pytest.raises(mcp.ServerExistsError):
        mcp.write_project_server_entry(tmp_path, "a", {"command": "z"}, op="add")
    out = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert out["mcpServers"]["a"] == {"command": "x"}  # untouched


def test_write_project_server_entry_edit_overwrites_existing_name(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}}}), encoding="utf-8"
    )
    mcp.write_project_server_entry(tmp_path, "a", {"command": "z"}, op="edit")
    out = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert out["mcpServers"]["a"] == {"command": "z"}


def test_write_user_server_entry_add_conflict(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}}}), encoding="utf-8")
    with pytest.raises(mcp.ServerExistsError):
        mcp.write_user_server_entry(cj, "a", {"command": "z"}, op="add")


def test_write_user_server_entry_add_success(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}}}), encoding="utf-8")
    mcp.write_user_server_entry(cj, "b", {"command": "y", "env": {"TOKEN": "sk-real"}}, op="add")
    out = json.loads(cj.read_text(encoding="utf-8"))
    assert out["mcpServers"]["a"] == {"command": "x"}
    assert out["mcpServers"]["b"]["env"]["TOKEN"] == "sk-real"


def test_write_project_local_server_entry_add_and_edit(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    mcp.write_project_local_server_entry(cj, project_dir, "a", {"command": "x"}, op="add")
    with pytest.raises(mcp.ServerExistsError):
        mcp.write_project_local_server_entry(cj, project_dir, "a", {"command": "y"}, op="add")
    mcp.write_project_local_server_entry(cj, project_dir, "a", {"command": "y"}, op="edit")
    assert mcp.read_project_local_servers(cj, project_dir)["a"]["command"] == "y"


def test_direct_writer_matches_cli_file_state_for_env_entry(tmp_path: Path) -> None:
    # MUST-FIX #1 confirmation: the direct writer yields the same stored mcpServers entry
    # the real `claude mcp add-json` produces for an env-bearing (non-OAuth) server, so
    # routing it away from the CLI loses nothing. (The live CLI adds a cosmetic "args": []
    # for stdio; the stored env/command are identical, which is what matters.)
    entry = {"command": "/bin/echo", "env": {"DEPLOY_KEY": "AKIAEXAMPLE"}}
    mcp.write_project_server_entry(tmp_path, "srv", entry, op="add")
    stored = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["srv"]
    assert stored["command"] == "/bin/echo"
    assert stored["env"] == {"DEPLOY_KEY": "AKIAEXAMPLE"}


def test_snapshot_server_entry_returns_unredacted_by_scope(tmp_path: Path) -> None:
    # snapshot_server_entry is the edit-rollback reader: it must return the UNREDACTED
    # stored entry (the rollback writes it back verbatim) for each scope, and None absent.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    # project scope
    mcp.write_project_server_entry(
        project_dir, "p", {"command": "x", "env": {"API_TOKEN": "sk-real"}}, op="add"
    )
    snap = mcp.snapshot_server_entry("project", "p", claude_json=cj, project_dir=project_dir)
    assert snap["env"]["API_TOKEN"] == "sk-real"  # unredacted
    # user + local scope
    mcp.write_user_server_entry(cj, "u", {"command": "x", "env": {"TOKEN": "sk-u"}}, op="add")
    mcp.write_project_local_server_entry(
        cj, project_dir, "l", {"command": "x", "env": {"TOKEN": "sk-l"}}, op="add"
    )
    assert (
        mcp.snapshot_server_entry("user", "u", claude_json=cj, project_dir=project_dir)["env"][
            "TOKEN"
        ]
        == "sk-u"
    )
    assert (
        mcp.snapshot_server_entry("local", "l", claude_json=cj, project_dir=project_dir)["env"][
            "TOKEN"
        ]
        == "sk-l"
    )
    # absent name -> None, in every scope
    assert (
        mcp.snapshot_server_entry("project", "absent", claude_json=cj, project_dir=project_dir)
        is None
    )
    assert (
        mcp.snapshot_server_entry("user", "absent", claude_json=cj, project_dir=project_dir)
        is None
    )
    assert (
        mcp.snapshot_server_entry("local", "absent", claude_json=cj, project_dir=project_dir)
        is None
    )


def test_snapshot_server_entry_missing_files_are_none(tmp_path: Path) -> None:
    cj = tmp_path / "absent.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    assert (
        mcp.snapshot_server_entry("project", "x", claude_json=cj, project_dir=project_dir) is None
    )
    assert mcp.snapshot_server_entry("user", "x", claude_json=cj, project_dir=project_dir) is None
    assert mcp.snapshot_server_entry("local", "x", claude_json=cj, project_dir=project_dir) is None


# --- gated routes (full FastAPI lifespan, fake `claude` binary) --------------------


def _client(write_config, tmp_path: Path, extra: str) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}")
    return TestClient(create_app(load_config(cfg)))


_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
_PROJECT_ONLY = "config_write:\n  enabled: true\n"


def test_route_server_add_project_scope_via_cli(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CLAUDE_MCP_STDOUT", "Added stdio MCP server srv to project config")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "name": "srv",
                "entry": {"command": "node"},
            },
        )
        assert resp.status_code == 200
    record = json.loads(argv_file.read_text())
    assert record["argv"] == [
        "add-json",
        "srv",
        json.dumps({"command": "node"}),
        "--scope",
        "project",
    ]
    assert record["cwd"] == str((projects_root / "alpha").resolve())
    assert record["has_client_secret_env"] is False


def test_route_server_add_audits_redacted_argv(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    # #958 P6: a CLI-driven add records the redacted `claude mcp add-json …` argv it ran in
    # the shared config_audit.log. Proves the argv sink set by the route propagates into the
    # worker-thread `_run` across asyncio.to_thread (the reason it's a contextvar).
    monkeypatch.setenv("FAKE_CLAUDE_MCP_STDOUT", "Added stdio MCP server srv to project config")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "name": "srv",
                "entry": {"command": "node"},
            },
        )
        assert resp.status_code == 200
    lines = (tmp_path / ".s" / "config_audit.log").read_text(encoding="utf-8").splitlines()
    entry = next(e for e in map(json.loads, lines) if e["surface"] == "mcp")
    assert entry["argv"] == [
        ["mcp", "add-json", "srv", json.dumps({"command": "node"}), "--scope", "project"]
    ]
    assert "files" in entry  # the changed-file fingerprints ride alongside (see test_config_audit)


def test_route_server_add_conflict_is_409(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MCP_EXIT_CODE", "1")
    monkeypatch.setenv("FAKE_CLAUDE_MCP_STDERR", "MCP server srv already exists in .mcp.json")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "name": "srv",
                "entry": {"command": "node"},
            },
        )
        assert resp.status_code == 409


def test_route_server_remove_not_found_is_404(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MCP_EXIT_CODE", "1")
    monkeypatch.setenv("FAKE_CLAUDE_MCP_STDERR", 'No MCP server named "srv" in project scope')
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "remove",
                "name": "srv",
            },
        )
        assert resp.status_code == 404


def test_route_server_add_with_secret_bypasses_cli(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    # If the CLI were ever invoked it would write argv_file -- assert it never is.
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ARGV_FILE", str(argv_file))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "name": "srv",
                "entry": {"command": "node", "env": {"API_TOKEN": "sk-live-real"}},
            },
        )
        assert resp.status_code == 200
    assert not argv_file.exists()  # CLI never spawned for a secret-bearing entry
    on_disk = json.loads((projects_root / "alpha" / ".mcp.json").read_text(encoding="utf-8"))
    assert on_disk["mcpServers"]["srv"]["env"]["API_TOKEN"] == "sk-live-real"


def test_route_server_edit_project_scope_reaches_cli_add(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CLAUDE_MCP_EXIT_CODE", "0")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "edit",
                "name": "srv",
                "entry": {"command": "node2"},
            },
        )
        assert resp.status_code == 200
    # remove (ignored) then add-json both ran; the recorded file reflects the last
    # (add-json) call reaching the CLI.
    record = json.loads(argv_file.read_text())
    assert record["argv"][0] == "add-json"
    assert record["argv"][1] == "srv"


def test_route_server_remote_client_secret_via_env(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ARGV_FILE", str(argv_file))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "op": "add",
                "name": "remote",
                "entry": {"type": "http", "url": "https://x/mcp"},
                "client_secret": "sk-oauth-secret",  # noqa: S106 - test literal, not real
            },
        )
        assert resp.status_code == 200
    record = json.loads(argv_file.read_text())
    assert "sk-oauth-secret" not in record["argv"]
    assert "--client-secret" in record["argv"]
    assert record["has_client_secret_env"] is True


def test_route_server_remove_success(write_config, tmp_path, projects_root, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MCP_STDOUT", "Removed MCP server srv from project config")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "remove",
                "name": "srv",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "scope": "project",
            "name": "srv",
            "op": "remove",
            "project": "alpha",
            "ok": True,
        }


def test_route_server_bad_scope_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "bogus",
                "project": "alpha",
                "confirm": "alpha",
                "op": "remove",
                "name": "srv",
            },
        )
        assert resp.status_code == 422


def test_route_server_missing_name_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "remove",
                "name": "",
            },
        )
        assert resp.status_code == 422


def test_route_server_user_scope_add_with_secret_bypasses_cli(
    write_config, tmp_path, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ARGV_FILE", str(argv_file))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "op": "add",
                "name": "u",
                "entry": {"command": "node", "env": {"API_TOKEN": "sk-live-real"}},
            },
        )
        assert resp.status_code == 200
    assert not argv_file.exists()  # CLI never spawned for a secret-bearing entry
    isolated = Path(os.environ["HOME"]) / ".claude.json"
    out = json.loads(isolated.read_text(encoding="utf-8"))
    assert out["mcpServers"]["u"]["env"]["API_TOKEN"] == "sk-live-real"


def test_route_server_local_scope_add_with_secret_bypasses_cli(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ARGV_FILE", str(argv_file))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "op": "add",
                "name": "l",
                "entry": {"command": "node", "env": {"API_TOKEN": "sk-live-real"}},
            },
        )
        assert resp.status_code == 200
    assert not argv_file.exists()  # CLI never spawned for a secret-bearing entry
    isolated = Path(os.environ["HOME"]) / ".claude.json"
    out = json.loads(isolated.read_text(encoding="utf-8"))
    resolved_project = str((projects_root / "alpha").resolve())
    assert (
        out["projects"][resolved_project]["mcpServers"]["l"]["env"]["API_TOKEN"] == "sk-live-real"
    )


@pytest.mark.parametrize(
    "entry",
    [
        # MUST-FIX #1: real secrets under BENIGN keys (redact_secrets misses these) must
        # still route to the direct writer and NEVER appear in the fake_claude argv capture.
        {"command": "node", "env": {"DEPLOY_KEY": "AKIAEXAMPLE1234567890"}},
        {"command": "node", "env": {"GH_PAT": "ghp_exampletokenvalue000"}},
        {"type": "http", "url": "https://x/mcp", "headers": {"X-Custom": "Bearer sk-benign"}},
    ],
)
def test_route_server_benign_keyed_secret_never_reaches_cli_argv(
    write_config, tmp_path, projects_root, monkeypatch, entry
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ARGV_FILE", str(argv_file))
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "name": "srv",
                "entry": entry,
            },
        )
        assert resp.status_code == 200
    assert not argv_file.exists()  # CLI never spawned -> no secret in any argv
    on_disk = json.loads((projects_root / "alpha" / ".mcp.json").read_text(encoding="utf-8"))
    assert on_disk["mcpServers"]["srv"] == entry  # written verbatim by the direct writer


def test_route_server_option_like_name_is_422(write_config, tmp_path, projects_root) -> None:
    # MUST-FIX #3 at the route: an option-like server name is rejected (422), never spawned.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "name": "--scope",
                "entry": {"command": "x"},
            },
        )
        assert resp.status_code == 422


def test_route_server_edit_readd_failure_restores_and_500s(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    # MUST-FIX #4 end-to-end: seed a prior server, then drive an edit whose CLI re-add
    # fails after the remove. The prior definition must be restored on disk, and the
    # request surfaces the failure (not a silent 200).
    (projects_root / "alpha" / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"srv": {"command": "OLD", "env": {"API_TOKEN": "sk-prior"}}}}),
        encoding="utf-8",
    )
    # remove exits 0, add-json exits 1 -> re-add fails after a successful remove.
    monkeypatch.setenv("FAKE_CLAUDE_MCP_REMOVE_EXIT_CODE", "0")
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ADDJSON_EXIT_CODE", "1")
    monkeypatch.setenv("FAKE_CLAUDE_MCP_STDERR", "boom: re-add failed")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "edit",
                "name": "srv",
                "entry": {"command": "NEW"},
            },
        )
        assert resp.status_code == 400  # McpCliError -> 400, not a silent success
        assert "restored" in resp.json()["detail"]
    # The prior definition (incl. its real secret) was restored verbatim on disk.
    on_disk = json.loads((projects_root / "alpha" / ".mcp.json").read_text(encoding="utf-8"))
    assert on_disk["mcpServers"]["srv"] == {"command": "OLD", "env": {"API_TOKEN": "sk-prior"}}


def test_route_server_edit_readd_failure_no_prior_still_400s(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    # Edit of a name with NO prior definition whose CLI re-add fails: the restore closure
    # reports no prior (returns False), so the request surfaces the failure (400) with a
    # message that does NOT falsely claim a restoration (GREPTILE P2a).
    monkeypatch.setenv("FAKE_CLAUDE_MCP_REMOVE_EXIT_CODE", "0")
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ADDJSON_EXIT_CODE", "1")
    monkeypatch.setenv("FAKE_CLAUDE_MCP_STDERR", "boom: re-add failed")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "edit",
                "name": "srv",
                "entry": {"command": "NEW"},
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "nothing to restore" in detail
        assert "restored" not in detail


def test_route_server_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "remove",
                "name": "srv",
            },
        )
        assert resp.status_code == 404


def test_route_server_confirm_mismatch_is_400(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "op": "remove",
                "name": "srv",
            },
        )
        assert resp.status_code == 400


def test_route_server_confirm_runs_before_op_validation(
    write_config, tmp_path, projects_root
) -> None:
    # Confirm is the first semantic gate -- a bad confirm short-circuits BEFORE the
    # op/name/entry shape checks, mirroring the whole-map PUT route's ordering.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "op": "bogus-op",
                "name": "",
            },
        )
        assert resp.status_code == 400


def test_route_server_bad_op_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "bogus",
                "name": "srv",
            },
        )
        assert resp.status_code == 422


def test_route_server_missing_entry_for_add_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "name": "srv",
            },
        )
        assert resp.status_code == 422


def test_route_server_bad_entry_shape_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "name": "srv",
                "entry": {"bogus": 1},
            },
        )
        assert resp.status_code == 422


def test_route_server_bad_client_secret_type_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "name": "srv",
                "entry": {"command": "x"},
                "client_secret": 123,
            },
        )
        assert resp.status_code == 422


def test_route_server_user_scope_404_when_user_off(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "op": "remove", "name": "srv"},
        )
        assert resp.status_code == 404


def test_route_server_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "../escape",
                "confirm": "../escape",
                "op": "remove",
                "name": "srv",
            },
        )
        assert resp.status_code == 400


def test_route_server_missing_project_dir_is_404(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "noexist",
                "confirm": "noexist",
                "op": "remove",
                "name": "srv",
            },
        )
        assert resp.status_code == 404


def test_route_server_user_scope_add_via_cli(write_config, tmp_path, monkeypatch) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CLAUDE_MCP_STDOUT", "Added stdio MCP server u to user config")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "op": "add",
                "name": "u",
                "entry": {"command": "node"},
            },
        )
        assert resp.status_code == 200
        assert "project" not in resp.json()
    record = json.loads(argv_file.read_text())
    assert record["argv"][:2] == ["add-json", "u"]
    assert record["argv"][-2:] == ["--scope", "user"]


# --- approvals routes (project scope only) -----------------------------------------


def test_route_approvals_read_write_round_trip(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        read0 = c.get("/api/config-write/mcp/approvals?project=alpha")
        assert read0.status_code == 200
        assert read0.json() == {"project": "alpha", "enabled": [], "disabled": [], "locked": []}
        wr = c.put(
            "/api/config-write/mcp/approvals",
            json={"project": "alpha", "confirm": "alpha", "enabled": ["a"], "disabled": ["b"]},
        )
        assert wr.status_code == 200
        read1 = c.get("/api/config-write/mcp/approvals?project=alpha")
        assert read1.json() == {
            "project": "alpha",
            "enabled": ["a"],
            "disabled": ["b"],
            "locked": [],
        }


def test_route_approvals_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get("/api/config-write/mcp/approvals?project=alpha").status_code == 404
        resp = c.put(
            "/api/config-write/mcp/approvals",
            json={"project": "alpha", "confirm": "alpha", "enabled": [], "disabled": []},
        )
        assert resp.status_code == 404


def test_route_approvals_confirm_mismatch_is_400(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        resp = c.put(
            "/api/config-write/mcp/approvals",
            json={"project": "alpha", "confirm": "WRONG", "enabled": [], "disabled": []},
        )
        assert resp.status_code == 400


def test_route_approvals_bad_shape_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        resp = c.put(
            "/api/config-write/mcp/approvals",
            json={"project": "alpha", "confirm": "alpha", "enabled": ["a"], "disabled": ["a"]},
        )
        assert resp.status_code == 422


def test_route_approvals_missing_lists_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        resp = c.put(
            "/api/config-write/mcp/approvals", json={"project": "alpha", "confirm": "alpha"}
        )
        assert resp.status_code == 422


def test_route_approvals_read_missing_project_is_422(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        assert c.get("/api/config-write/mcp/approvals?project=").status_code == 422


# --- reset-project-choices route ----------------------------------------------------


def test_route_reset_project_choices_success(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CLAUDE_MCP_STDOUT", "reset done")
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        resp = c.post(
            "/api/config-write/mcp/reset-project-choices",
            json={"project": "alpha", "confirm": "alpha"},
        )
        assert resp.status_code == 200
    record = json.loads(argv_file.read_text())
    assert record["argv"] == ["reset-project-choices"]
    assert record["cwd"] == str((projects_root / "alpha").resolve())


def test_route_reset_project_choices_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        resp = c.post(
            "/api/config-write/mcp/reset-project-choices",
            json={"project": "alpha", "confirm": "alpha"},
        )
        assert resp.status_code == 404


def test_route_reset_project_choices_confirm_mismatch_is_400(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        resp = c.post(
            "/api/config-write/mcp/reset-project-choices",
            json={"project": "alpha", "confirm": "WRONG"},
        )
        assert resp.status_code == 400


def test_route_reset_project_choices_cli_failure_is_400(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MCP_EXIT_CODE", "1")
    monkeypatch.setenv("FAKE_CLAUDE_MCP_STDERR", "boom")
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        resp = c.post(
            "/api/config-write/mcp/reset-project-choices",
            json={"project": "alpha", "confirm": "alpha"},
        )
        assert resp.status_code == 400


def test_route_reset_project_choices_missing_project_dir_is_404(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        resp = c.post(
            "/api/config-write/mcp/reset-project-choices",
            json={"project": "noexist", "confirm": "noexist"},
        )
        assert resp.status_code == 404
