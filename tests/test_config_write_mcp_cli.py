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
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import claude_cli
from clauster import config_write as cw
from clauster import config_write_mcp as mcp
from clauster import config_write_mcp_cli as mcp_cli
from clauster.app import create_app
from clauster.config import load_config

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _fake_run(rc: int = 0, stdout: str = "", stderr: str = ""):
    """Build a fake subprocess-runner + its call log, for injecting as ``run=``."""
    calls: list[dict] = []

    def run(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return subprocess.CompletedProcess(list(argv), rc, stdout=stdout, stderr=stderr)

    return run, calls


# --- entry_has_secret ---------------------------------------------------------------


def test_entry_has_secret_detects_env_token() -> None:
    assert mcp_cli.entry_has_secret({"command": "x", "env": {"API_TOKEN": "sk-real"}})


def test_entry_has_secret_detects_secretish_header() -> None:
    assert mcp_cli.entry_has_secret(
        {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer sk-x"}}
    )


def test_entry_has_secret_false_for_clean_entry() -> None:
    assert not mcp_cli.entry_has_secret({"command": "x", "args": ["--flag"]})


def test_entry_has_secret_true_for_interpolation_placeholder() -> None:
    # A `${VAR}` placeholder is secret-SHAPED (conservative masking direction) even
    # though it holds no literal value -- it still gets routed away from the CLI.
    assert mcp_cli.entry_has_secret({"command": "x", "env": {"TOKEN": "${MY_TOKEN}"}})


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


def test_cli_add_server_refuses_entry_with_literal_secret(tmp_path: Path) -> None:
    run, calls = _fake_run()
    with pytest.raises(ValueError, match="literal secret"):
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
    assert mcp.read_project_approvals(cj, project_dir) == {"enabled": [], "disabled": []}
    mcp.write_project_approvals(cj, project_dir, ["a"], ["b"])
    assert mcp.read_project_approvals(cj, project_dir) == {"enabled": ["a"], "disabled": ["b"]}


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
        assert read0.json() == {"project": "alpha", "enabled": [], "disabled": []}
        wr = c.put(
            "/api/config-write/mcp/approvals",
            json={"project": "alpha", "confirm": "alpha", "enabled": ["a"], "disabled": ["b"]},
        )
        assert wr.status_code == 200
        read1 = c.get("/api/config-write/mcp/approvals?project=alpha")
        assert read1.json() == {"project": "alpha", "enabled": ["a"], "disabled": ["b"]}


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
