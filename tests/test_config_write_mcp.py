"""MCP-server config-write surface (#688) over the #347 Foundation.

Covers the pure structural validator (validate-never-execute, incl. an RCE-negative
sentinel that proves a candidate ``command`` is never run), the project/user read+write
routers, and the gated routes (capability/scope 404, type-the-name 400, bad-shape 422
writing nothing, stale-hash 409, path-escape 400, redaction round-trip).

Every test that touches ``~/.claude.json`` passes an explicit ``tmp_path`` file and
runs under the autouse HOME-isolation fixture — the live account is never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_mcp as mcp
from clauster.app import create_app
from clauster.config import load_config

# --- structural validator: accept the valid shapes ---------------------------------


def test_validate_accepts_stdio_entry() -> None:
    mcp.validate_mcp_servers(
        {"srv": {"command": "/bin/foo", "args": ["--flag"], "env": {"K": "v"}}}
    )  # no raise


def test_validate_accepts_minimal_stdio() -> None:
    mcp.validate_mcp_servers({"srv": {"command": "node"}})  # args/env optional


def test_validate_accepts_remote_entry() -> None:
    mcp.validate_mcp_servers(
        {"api": {"type": "http", "url": "https://x/mcp", "headers": {"H": "v"}}}
    )
    mcp.validate_mcp_servers({"api": {"transport": "sse", "url": "https://x/sse"}})


def test_validate_accepts_explicit_stdio_type() -> None:
    mcp.validate_mcp_servers({"srv": {"type": "stdio", "command": "node"}})


def test_validate_accepts_empty_map() -> None:
    mcp.validate_mcp_servers({})  # removing all servers is valid


# --- structural validator: reject the bad shapes (→ InvalidCandidateError / 422) ----


@pytest.mark.parametrize(
    "candidate",
    [
        ["not", "a", "dict"],
        {"": {"command": "x"}},  # empty server name
        {"srv": "not-an-object"},
        {"srv": {}},  # stdio with no command
        {"srv": {"command": ""}},  # empty command
        {"srv": {"command": 123}},  # non-string command
        {"srv": {"command": "x", "args": "nope"}},  # args not a list
        {"srv": {"command": "x", "args": [1, 2]}},  # args not strings
        {"srv": {"command": "x", "env": {"K": 1}}},  # env value not a string
        {"srv": {"command": "x", "env": ["nope"]}},  # env not an object
        {"srv": {"command": "x", "bogus": 1}},  # unknown key
        {"srv": {"type": "ftp", "url": "x"}},  # unknown transport
        {"srv": {"type": "http"}},  # remote with no url
        {"srv": {"type": "http", "url": ""}},  # empty url
        {"srv": {"type": "http", "url": "x", "headers": {"H": 1}}},  # bad headers
        {"srv": {"type": "http", "transport": "sse", "url": "x"}},  # type/transport disagree
        {"srv": {"type": 5, "command": "x"}},  # non-string type
    ],
)
def test_validate_rejects_bad_shape(candidate: object) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        mcp.validate_mcp_servers(candidate)


# --- RCE-NEGATIVE: validation must NEVER execute the candidate command -------------


def test_validation_never_executes_command(tmp_path: Path) -> None:
    """A candidate command pointing at a marker-writing script must NOT run on validate.

    Proves the validator is structural-only: if validation resolved/spawned the
    command, the sentinel marker file would appear. It must not.
    """
    marker = tmp_path / "MARKER_SHOULD_NOT_EXIST"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    candidate = {
        "evil": {
            "command": str(script),
            "args": [str(marker)],
            "env": {"OUT": str(marker)},
        }
    }
    # Validating (and writing) must store the command as inert data, never run it.
    mcp.validate_mcp_servers(candidate)
    mcp.write_project_servers(tmp_path, candidate, expected_hash=cw.hash_bytes(b""))

    assert not marker.exists(), "validation/write executed the candidate command (RCE!)"
    # The command landed verbatim as stored data, proving it was treated as inert.
    stored = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert stored["mcpServers"]["evil"]["command"] == str(script)


# --- project read/write round-trip + stale-hash ------------------------------------


def test_project_write_then_read_round_trip(tmp_path: Path) -> None:
    _servers, h0 = mcp.read_project_servers(tmp_path)
    assert _servers == {}
    mcp.write_project_servers(tmp_path, {"srv": {"command": "/bin/foo"}}, expected_hash=h0)
    servers, _h1 = mcp.read_project_servers(tmp_path)
    assert servers == {"srv": {"command": "/bin/foo"}}


def test_project_write_preserves_sibling_keys(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}}, "other": 1}),
        encoding="utf-8",
    )
    _s, h = mcp.read_project_servers(tmp_path)
    mcp.write_project_servers(tmp_path, {"a": {"command": "x"}, "b": {"command": "y"}}, h)
    out = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert out["other"] == 1  # untouched sibling preserved
    assert set(out["mcpServers"]) == {"a", "b"}


def test_project_write_stale_hash_raises(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    stale = cw.hash_bytes(b'{"mcpServers": {"old": {"command": "x"}}}')
    with pytest.raises(cw.StaleConfigWriteError):
        mcp.write_project_servers(tmp_path, {"new": {"command": "y"}}, stale)


def test_project_write_no_hash_on_existing_file_is_stale(tmp_path: Path) -> None:
    # An absent hash is only the first-write path; refusing the unguarded overwrite of
    # an EXISTING file means a client can't drop `hash` to bypass the external-edit guard.
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    with pytest.raises(cw.StaleConfigWriteError):
        mcp.write_project_servers(tmp_path, {"s": {"command": "x"}}, expected_hash=None)


def test_project_write_no_hash_on_absent_file_ok(tmp_path: Path) -> None:
    # The legitimate first write: no file yet ⇒ no hash needed.
    mcp.write_project_servers(tmp_path, {"s": {"command": "x"}}, expected_hash=None)
    out = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert out["mcpServers"] == {"s": {"command": "x"}}


def test_project_read_rejects_non_object_file(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(cw.InvalidCandidateError):
        mcp.read_project_servers(tmp_path)


# --- redaction round-trip (read masks; write keep-stored on sentinel) --------------


def test_project_redaction_and_keep_stored(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": {"command": "x", "env": {"API_TOKEN": "sk-live-real"}}}}),
        encoding="utf-8",
    )
    servers, h = mcp.read_project_servers(tmp_path)
    # Read-back masks the secret — the live value never leaves the reader.
    assert servers["s"]["env"]["API_TOKEN"] == cw.REDACTION_SENTINEL
    assert "sk-live-real" not in json.dumps(servers)

    # Write the masked view back unchanged (e.g. user only tweaked a sibling) — the
    # "********" sentinel keeps the stored secret rather than clobbering it.
    mcp.write_project_servers(tmp_path, servers, h)
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert on_disk["mcpServers"]["s"]["env"]["API_TOKEN"] == "sk-live-real"


# --- user-scope subtree writer (HOME-isolated, explicit tmp file) ------------------


def test_user_write_preserves_other_keys(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    cj.write_text(
        json.dumps({"projects": {"/p": {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    mcp.write_user_servers(cj, {"srv": {"command": "/bin/foo"}})
    out = json.loads(cj.read_text(encoding="utf-8"))
    assert out["mcpServers"]["srv"]["command"] == "/bin/foo"  # subtree written
    assert out["projects"]["/p"]["hasTrustDialogAccepted"] is True  # sibling preserved


def test_user_read_redacts(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    cj.write_text(
        json.dumps({"mcpServers": {"s": {"command": "x", "env": {"TOKEN": "sk-x"}}}}),
        encoding="utf-8",
    )
    servers = mcp.read_user_servers(cj)
    assert servers["s"]["env"]["TOKEN"] == cw.REDACTION_SENTINEL


def test_user_write_rejects_bad_shape_without_writing(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"  # absent
    with pytest.raises(cw.InvalidCandidateError):
        mcp.write_user_servers(cj, {"srv": {"command": ""}})
    assert not cj.exists()  # nothing written on a validation failure


def test_user_read_missing_file_is_empty(tmp_path: Path) -> None:
    assert mcp.read_user_servers(tmp_path / "absent.json") == {}


def test_read_rejects_malformed_json(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(cw.InvalidCandidateError):
        mcp.read_project_servers(tmp_path)


# --- local-scope writer (~/.claude.json projects[<abs-path>].mcpServers) -----------


def test_local_write_then_read_round_trip(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    assert mcp.read_project_local_servers(cj, project_dir) == {}
    mcp.write_project_local_servers(cj, project_dir, {"s": {"command": "/bin/x"}})
    assert mcp.read_project_local_servers(cj, project_dir) == {"s": {"command": "/bin/x"}}


def test_local_write_keyed_by_resolved_absolute_project_path(tmp_path: Path) -> None:
    # Same shape trust.py uses for hasTrustDialogAccepted: projects[str(path.resolve())].
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    mcp.write_project_local_servers(cj, project_dir, {"s": {"command": "x"}})
    out = json.loads(cj.read_text(encoding="utf-8"))
    assert str(project_dir.resolve()) in out["projects"]
    assert out["projects"][str(project_dir.resolve())]["mcpServers"] == {"s": {"command": "x"}}


def test_local_write_preserves_other_projects_and_sibling_subtrees(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    proj_a = tmp_path / "a"
    proj_a.mkdir()
    proj_b = tmp_path / "b"
    proj_b.mkdir()
    cj.write_text(
        json.dumps(
            {
                "projects": {
                    str(proj_a.resolve()): {
                        "hasTrustDialogAccepted": True,
                        "mcpServers": {"old": {"command": "x"}},
                    },
                    str(proj_b.resolve()): {"hasTrustDialogAccepted": True},
                },
                "misc": 1,
            }
        ),
        encoding="utf-8",
    )
    mcp.write_project_local_servers(cj, proj_a, {"new": {"command": "y"}})
    out = json.loads(cj.read_text(encoding="utf-8"))
    a_entry = out["projects"][str(proj_a.resolve())]
    assert a_entry["mcpServers"] == {"new": {"command": "y"}}
    assert a_entry["hasTrustDialogAccepted"] is True  # sibling subtree preserved
    assert out["projects"][str(proj_b.resolve())]["hasTrustDialogAccepted"] is True
    assert out["misc"] == 1


def test_local_read_redacts_and_write_keeps_stored_secret(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    mcp.write_project_local_servers(
        cj, project_dir, {"s": {"command": "x", "env": {"API_TOKEN": "sk-live-real"}}}
    )
    servers = mcp.read_project_local_servers(cj, project_dir)
    assert servers["s"]["env"]["API_TOKEN"] == cw.REDACTION_SENTINEL
    assert "sk-live-real" not in json.dumps(servers)
    # Writing the masked view back unchanged keeps the stored secret (keep-stored).
    mcp.write_project_local_servers(cj, project_dir, servers)
    out = json.loads(cj.read_text(encoding="utf-8"))
    assert (
        out["projects"][str(project_dir.resolve())]["mcpServers"]["s"]["env"]["API_TOKEN"]
        == "sk-live-real"
    )


def test_local_write_rejects_bad_shape_without_writing(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"  # absent
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    with pytest.raises(cw.InvalidCandidateError):
        mcp.write_project_local_servers(cj, project_dir, {"s": {"command": ""}})
    assert not cj.exists()  # nothing written on a validation failure


def test_local_read_missing_file_is_empty(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    assert mcp.read_project_local_servers(tmp_path / "absent.json", project_dir) == {}


def test_local_scope_independent_of_project_and_user_scope(tmp_path: Path) -> None:
    # Local scope lives in ~/.claude.json's per-project block; it must never collide
    # with the project-scope .mcp.json file or the flat user-scope mcpServers subtree.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    mcp.write_project_servers(project_dir, {"p": {"command": "proj"}}, expected_hash=None)
    mcp.write_user_servers(cj, {"u": {"command": "user"}})
    mcp.write_project_local_servers(cj, project_dir, {"l": {"command": "local"}})

    project_servers, _ = mcp.read_project_servers(project_dir)
    assert project_servers == {"p": {"command": "proj"}}
    assert mcp.read_user_servers(cj) == {"u": {"command": "user"}}
    assert mcp.read_project_local_servers(cj, project_dir) == {"l": {"command": "local"}}


# --- #837: unapproved_mcp_servers (pre-spawn MCP-approval preflight) ---------------


def test_unapproved_no_mcp_json_is_empty(tmp_path: Path) -> None:
    # No .mcp.json at all -> nothing to warn about.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_empty_servers_map_is_empty(tmp_path: Path) -> None:
    # An .mcp.json present but with no mcpServers key/empty object -> nothing to warn.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_all_approved_is_empty(tmp_path: Path) -> None:
    # Every committed server is either enabled or disabled -> nothing left to warn.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}),
        encoding="utf-8",
    )
    mcp.write_project_approvals(cj, project_dir.resolve(), ["a"], ["b"])
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_some_unapproved_lists_the_set(tmp_path: Path) -> None:
    # "a" approved, "b" rejected, "c" never decided -> only "c" is unapproved.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "a": {"command": "x"},
                    "b": {"command": "y"},
                    "c": {"command": "z"},
                }
            }
        ),
        encoding="utf-8",
    )
    mcp.write_project_approvals(cj, project_dir.resolve(), ["a"], ["b"])
    assert mcp.unapproved_mcp_servers(cj, project_dir) == ["c"]


def test_unapproved_no_approvals_file_lists_every_server(tmp_path: Path) -> None:
    # A committed .mcp.json with no ~/.claude.json approvals at all -> every server
    # is unapproved (this is the exact scenario #837 hangs on: a fresh clone with a
    # committed .mcp.json and no prior approval decision).
    cj = tmp_path / "claude.json"  # never created
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}),
        encoding="utf-8",
    )
    assert mcp.unapproved_mcp_servers(cj, project_dir) == ["a", "b"]


def test_unapproved_malformed_mcp_json_is_safe_empty(tmp_path: Path, caplog) -> None:
    # Malformed .mcp.json ("cannot determine") must degrade to [] and NEVER raise —
    # a preflight read failure must not crash (or block) the launch path.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text("{not valid json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert mcp.unapproved_mcp_servers(cj, project_dir) == []
    assert any("could not read" in rec.message for rec in caplog.records)


def test_unapproved_non_object_mcp_json_is_safe_empty(tmp_path: Path) -> None:
    # Valid JSON but not an object (e.g. a bare list) -> also "cannot determine".
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text("[]", encoding="utf-8")
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_malformed_claude_json_is_safe_empty(tmp_path: Path, caplog) -> None:
    # .mcp.json is fine, but ~/.claude.json (the approvals store) is corrupt -> still
    # "cannot determine", never a crash. Distinct from the .mcp.json failure above:
    # this exercises the second try/except around the approvals read.
    cj = tmp_path / "claude.json"
    cj.write_text("{not valid json", encoding="utf-8")
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}}}), encoding="utf-8"
    )
    with caplog.at_level("WARNING"):
        assert mcp.unapproved_mcp_servers(cj, project_dir) == []
    assert any("could not read MCP approvals" in rec.message for rec in caplog.records)


def test_unapproved_resolves_project_dir_for_approvals_lookup(tmp_path: Path) -> None:
    # Project.path from discovery is not guaranteed resolved (e.g. a symlinked
    # projects_root); approvals are keyed by the RESOLVED path (mirrors
    # resolve_project_dir), so a caller passing an unresolved-but-equivalent path
    # must still see the approval. Simulate via a "./" indirection.
    cj = tmp_path / "claude.json"
    real_dir = tmp_path / "proj"
    real_dir.mkdir()
    (real_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}}}), encoding="utf-8"
    )
    mcp.write_project_approvals(cj, real_dir.resolve(), ["a"], [])
    unresolved = tmp_path / "." / "proj"
    assert mcp.unapproved_mcp_servers(cj, unresolved) == []


# --- #850: settings-file decisions also count as "decided" (no false warn) ----------


def _mcp_project(tmp_path: Path, *servers: str) -> tuple[Path, Path]:
    """Make a project with a committed .mcp.json listing `servers`; return (claude_json, dir)."""
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {s: {"command": "x"} for s in servers}}), encoding="utf-8"
    )
    return cj, project_dir


def _write_settings(path: Path, **keys) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys), encoding="utf-8")


def test_unapproved_settings_local_disable_counts_as_decided(tmp_path: Path) -> None:
    # #850 repro: a server DISABLED in .claude/settings.local.json is decided as far as
    # claude is concerned (no enable gate), so the preflight must NOT warn about it.
    cj, project_dir = _mcp_project(tmp_path, "demo-fs", "demo-http")
    _write_settings(
        project_dir / ".claude" / "settings.local.json",
        disabledMcpjsonServers=["demo-fs", "demo-http"],
    )
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_project_settings_enable_counts_as_decided(tmp_path: Path) -> None:
    # An ENABLE in the project-shared .claude/settings.json likewise clears the gate.
    cj, project_dir = _mcp_project(tmp_path, "a", "b")
    _write_settings(project_dir / ".claude" / "settings.json", enabledMcpjsonServers=["a", "b"])
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_user_settings_decision_counts_as_decided(tmp_path: Path) -> None:
    # The user-scope ~/.claude/settings.json (beside ~/.claude.json) is honored too.
    cj, project_dir = _mcp_project(tmp_path, "a")
    _write_settings(cj.parent / ".claude" / "settings.json", enabledMcpjsonServers=["a"])
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_merges_every_source(tmp_path: Path) -> None:
    # 'a' decided in ~/.claude.json, 'b' via settings.local.json, 'c' never decided
    # anywhere -> only 'c' still needs approval.
    cj, project_dir = _mcp_project(tmp_path, "a", "b", "c")
    mcp.write_project_approvals(cj, project_dir.resolve(), ["a"], [])
    _write_settings(project_dir / ".claude" / "settings.local.json", disabledMcpjsonServers=["b"])
    assert mcp.unapproved_mcp_servers(cj, project_dir) == ["c"]


def test_unapproved_malformed_settings_file_is_ignored(tmp_path: Path) -> None:
    # A corrupt settings file must not crash the preflight AND must not silently hide a
    # genuinely-undecided server — it simply contributes no decisions.
    cj, project_dir = _mcp_project(tmp_path, "a")
    (project_dir / ".claude").mkdir(parents=True)
    (project_dir / ".claude" / "settings.local.json").write_text("{not json", encoding="utf-8")
    assert mcp.unapproved_mcp_servers(cj, project_dir) == ["a"]


# --- #958 P2: read_project_approvals folds in settings-file top-level approvals -----


def _approvals_project(tmp_path: Path) -> tuple[Path, Path]:
    """Return (claude_json, project_dir) for a read_project_approvals fixture."""
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    return cj, project_dir


def test_read_approvals_settings_local_enable_shows_enabled(tmp_path: Path) -> None:
    # DF-9: `claude mcp add-json --scope local` relocates the approval into
    # settings.local.json and clears ~/.claude.json — the panel must still show it enabled.
    cj, project_dir = _approvals_project(tmp_path)
    _write_settings(project_dir / ".claude" / "settings.local.json", enabledMcpjsonServers=["fs"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["fs"],
        "disabled": [],
        "locked": ["fs"],
    }


def test_read_approvals_project_settings_disable_shows_disabled(tmp_path: Path) -> None:
    cj, project_dir = _approvals_project(tmp_path)
    _write_settings(project_dir / ".claude" / "settings.json", disabledMcpjsonServers=["http"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": [],
        "disabled": ["http"],
        "locked": ["http"],
    }


def test_read_approvals_user_settings_folded(tmp_path: Path) -> None:
    cj, project_dir = _approvals_project(tmp_path)
    _write_settings(cj.parent / ".claude" / "settings.json", enabledMcpjsonServers=["u"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["u"],
        "disabled": [],
        "locked": ["u"],
    }


def test_read_approvals_settings_local_overrides_claude_json(tmp_path: Path) -> None:
    # Conflict across sources: enabled in ~/.claude.json but disabled in settings.local.json
    # — the more-specific (local) source wins, matching what claude loads.
    cj, project_dir = _approvals_project(tmp_path)
    mcp.write_project_approvals(cj, project_dir, ["z"], [])
    _write_settings(project_dir / ".claude" / "settings.local.json", disabledMcpjsonServers=["z"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": [],
        "disabled": ["z"],
        "locked": ["z"],
    }


def test_read_approvals_project_settings_overrides_user_settings(tmp_path: Path) -> None:
    # Settings-vs-settings precedence (not just claude.json-vs-local): user settings.json
    # disables X, project settings.json enables X — project is more specific, so X reads
    # as enabled. Pins the ~/.claude.json < user < project < local ordering across the
    # settings files themselves.
    cj, project_dir = _approvals_project(tmp_path)
    _write_settings(cj.parent / ".claude" / "settings.json", disabledMcpjsonServers=["x"])
    _write_settings(project_dir / ".claude" / "settings.json", enabledMcpjsonServers=["x"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["x"],
        "disabled": [],
        "locked": ["x"],
    }


def test_read_approvals_local_settings_overrides_project_settings(tmp_path: Path) -> None:
    # Local is the most specific settings file: project settings.json enables X, local
    # settings.local.json disables X — local wins, X reads as disabled.
    cj, project_dir = _approvals_project(tmp_path)
    _write_settings(project_dir / ".claude" / "settings.json", enabledMcpjsonServers=["x"])
    _write_settings(project_dir / ".claude" / "settings.local.json", disabledMcpjsonServers=["x"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": [],
        "disabled": ["x"],
        "locked": ["x"],
    }


def test_read_approvals_claude_json_only_unchanged(tmp_path: Path) -> None:
    # No settings files -> behaves exactly as the legacy ~/.claude.json-only read.
    cj, project_dir = _approvals_project(tmp_path)
    mcp.write_project_approvals(cj, project_dir, ["a"], ["b"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["a"],
        "disabled": ["b"],
        "locked": [],
    }


def test_read_approvals_malformed_settings_ignored(tmp_path: Path) -> None:
    # A corrupt settings file contributes nothing — the ~/.claude.json state still reads.
    cj, project_dir = _approvals_project(tmp_path)
    mcp.write_project_approvals(cj, project_dir, ["a"], [])
    (project_dir / ".claude" / "settings.local.json").parent.mkdir(parents=True, exist_ok=True)
    (project_dir / ".claude" / "settings.local.json").write_text("{bad", encoding="utf-8")
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["a"],
        "disabled": [],
        "locked": [],
    }


def test_read_approvals_locked_even_when_claude_json_agrees(tmp_path: Path) -> None:
    # A server enabled in BOTH ~/.claude.json AND settings.local.json is still `locked`:
    # the settings entry owns the effective decision, so a panel Unset (which only rewrites
    # ~/.claude.json) would be reverted on reload. The panel renders such a row read-only
    # rather than offer an action that silently won't take (#958 P2 write-asymmetry guard).
    cj, project_dir = _approvals_project(tmp_path)
    mcp.write_project_approvals(cj, project_dir, ["fs"], [])
    _write_settings(project_dir / ".claude" / "settings.local.json", enabledMcpjsonServers=["fs"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["fs"],
        "disabled": [],
        "locked": ["fs"],
    }


def _base_lists(cj: Path, project_dir: Path) -> dict[str, list[str]]:
    """Return the raw ~/.claude.json projects[] approval lists (no settings overlay)."""
    proj = json.loads(cj.read_text(encoding="utf-8"))["projects"][str(project_dir)]
    return {
        "enabled": proj.get("enabledMcpjsonServers", []),
        "disabled": proj.get("disabledMcpjsonServers", []),
    }


def test_write_approvals_does_not_copy_settings_owned_into_claude_json(tmp_path: Path) -> None:
    # Greptile P1: the panel echoes the merged (settings-folded) lists back on any save.
    # Persisting `fs` (owned only by settings.local.json) into ~/.claude.json would create a
    # phantom base approval that lingers if the settings entry is later removed. The writer
    # must drop settings-owned names and persist ONLY the decisions this path owns.
    cj, project_dir = _approvals_project(tmp_path)
    _write_settings(project_dir / ".claude" / "settings.local.json", enabledMcpjsonServers=["fs"])
    mcp.write_project_approvals(cj, project_dir, ["fs", "other"], [])
    assert _base_lists(cj, project_dir) == {"enabled": ["other"], "disabled": []}
    # The display read still reflects fs (from settings) + the new `other`.
    got = mcp.read_project_approvals(cj, project_dir)
    assert set(got["enabled"]) == {"fs", "other"} and got["locked"] == ["fs"]


def test_write_approvals_preserves_original_base_value_for_settings_owned(tmp_path: Path) -> None:
    # A name that is a GENUINE ~/.claude.json approval AND is later also settings-owned keeps
    # its original base value on a subsequent save — the writer neither drops the real base
    # decision nor overwrites it with the settings-derived one.
    cj, project_dir = _approvals_project(tmp_path)
    mcp.write_project_approvals(cj, project_dir, ["fs"], [])  # genuine base approval first
    _write_settings(project_dir / ".claude" / "settings.local.json", enabledMcpjsonServers=["fs"])
    mcp.write_project_approvals(cj, project_dir, ["fs", "other"], [])  # merged echo + a new one
    assert _base_lists(cj, project_dir) == {"enabled": ["other", "fs"], "disabled": []}


def test_write_approvals_drops_contradictory_base_pair_for_settings_owned(tmp_path: Path) -> None:
    # Defensive: a hand-corrupted base listing a settings-owned name in BOTH enabled and
    # disabled must not be persisted as a self-contradicting pair. The name is dropped from
    # both (a settings file owns it, so the inert base entry is meaningless) — the writer's
    # own output stays a clean set even from a corrupt base.
    cj, project_dir = _approvals_project(tmp_path)
    cj.write_text(
        json.dumps(
            {
                "projects": {
                    str(project_dir): {
                        "enabledMcpjsonServers": ["fs"],
                        "disabledMcpjsonServers": ["fs"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    _write_settings(project_dir / ".claude" / "settings.local.json", enabledMcpjsonServers=["fs"])
    mcp.write_project_approvals(cj, project_dir, ["fs", "other"], [])
    assert _base_lists(cj, project_dir) == {"enabled": ["other"], "disabled": []}


def test_write_approvals_dedupes_preserved_base_duplicates(tmp_path: Path) -> None:
    # Defensive: a hand-edited ~/.claude.json with a duplicate owned name must not survive as
    # a duplicate when its base value is preserved across a save (the incoming lists are
    # already validated dup-free, so only a tampered base can reach the dedup path).
    cj, project_dir = _approvals_project(tmp_path)
    cj.write_text(
        json.dumps({"projects": {str(project_dir): {"enabledMcpjsonServers": ["fs", "fs"]}}}),
        encoding="utf-8",
    )
    _write_settings(project_dir / ".claude" / "settings.local.json", enabledMcpjsonServers=["fs"])
    mcp.write_project_approvals(cj, project_dir, ["fs", "other"], [])
    assert _base_lists(cj, project_dir) == {"enabled": ["other", "fs"], "disabled": []}


# --- gated routes (full FastAPI lifespan) ------------------------------------------

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _client(write_config, tmp_path, extra: str) -> TestClient:
    # The autouse HOME-isolation fixture already pins HOME to an isolated tmp dir, so
    # runner.claude_json resolves to <isolated-home>/.claude.json — the real account is
    # never touched by the user-scope routes.
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}")
    return TestClient(create_app(load_config(cfg)))


_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
_PROJECT_ONLY = "config_write:\n  enabled: true\n"


def test_route_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get("/api/config-write/mcp?project=alpha").status_code == 404
        assert (
            c.put(
                "/api/config-write/mcp",
                json={"scope": "project", "project": "alpha", "confirm": "alpha", "servers": {}},
            ).status_code
            == 404
        )


def test_route_user_scope_404_when_user_off(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        assert c.get("/api/config-write/mcp?scope=user").status_code == 404
        assert (
            c.put(
                "/api/config-write/mcp",
                json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "servers": {}},
            ).status_code
            == 404
        )


def test_route_confirm_mismatch_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "servers": {"s": {"command": "x"}},
            },
        )
        assert resp.status_code == 400


def test_route_bad_shape_is_422_and_writes_nothing(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "servers": {"s": {"command": "x", "bogus": 1}},  # unknown key
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".mcp.json").exists()  # nothing written


def test_route_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "../escape",
                "confirm": "../escape",
                "servers": {},
            },
        )
        assert resp.status_code == 400
        assert c.get("/api/config-write/mcp?project=../escape").status_code == 400


def test_route_stale_hash_is_409(write_config, tmp_path, projects_root) -> None:
    (projects_root / "alpha" / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "servers": {"s": {"command": "x"}},
                "hash": cw.hash_bytes(b"something else"),
            },
        )
        assert resp.status_code == 409


def test_route_no_hash_on_existing_file_is_409(write_config, tmp_path, projects_root) -> None:
    (projects_root / "alpha" / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "servers": {"s": {"command": "x"}},
                # no "hash" — must not silently overwrite an existing file
            },
        )
        assert resp.status_code == 409


def test_route_project_write_read_round_trip(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        read0 = c.get("/api/config-write/mcp?project=alpha")
        assert read0.status_code == 200
        h0 = read0.json()["hash"]
        wr = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "servers": {"srv": {"command": "/bin/foo", "args": ["--x"]}},
                "hash": h0,
            },
        )
        assert wr.status_code == 200
        read1 = c.get("/api/config-write/mcp?project=alpha")
        assert read1.json()["servers"] == {"srv": {"command": "/bin/foo", "args": ["--x"]}}


def test_route_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get("/api/config-write/mcp?scope=bogus").status_code == 422
        assert (
            c.put(
                "/api/config-write/mcp",
                json={"scope": "bogus", "servers": {}},
            ).status_code
            == 422
        )


def test_route_user_write_round_trip(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        wr = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "servers": {"u": {"command": "node"}},
            },
        )
        assert wr.status_code == 200
        read = c.get("/api/config-write/mcp?scope=user")
        assert read.json()["servers"] == {"u": {"command": "node"}}
    # The write landed in the ISOLATED home (autouse fixture), never the real account.
    isolated = Path(os.environ["HOME"]) / ".claude.json"
    out = json.loads(isolated.read_text(encoding="utf-8"))
    assert out["mcpServers"]["u"]["command"] == "node"


# --- regression: the route-level error guards (P1/P2 from review) -------------------


def test_route_read_corrupt_file_is_422(write_config, tmp_path, projects_root) -> None:
    # A hand-edited / partially-written .mcp.json that is not valid JSON must surface
    # as a clean 422 from the GET route, never an unhandled 500 (P1). read_project_servers
    # -> _load_json_obj raises InvalidCandidateError; the route maps it through the helper.
    (projects_root / "alpha" / ".mcp.json").write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/mcp?project=alpha")
        assert resp.status_code == 422


def test_route_read_non_utf8_file_is_422(write_config, tmp_path, projects_root) -> None:
    # A non-UTF-8 .mcp.json: _load_json_obj's raw.decode("utf-8") raises UnicodeDecodeError,
    # which is wrapped as InvalidCandidateError so the GET route reports 422, not a 500.
    (projects_root / "alpha" / ".mcp.json").write_bytes(b"\xff\xfe not utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get("/api/config-write/mcp?project=alpha").status_code == 422


def test_route_read_user_corrupt_file_is_422(write_config, tmp_path) -> None:
    # User-scope complement of the project read guard (P1): a corrupt ~/.claude.json must
    # surface as a clean 422 from the GET route, never an unhandled 500. read_user_servers
    # -> _load_json_obj raises InvalidCandidateError; the route maps it through the helper.
    (Path(os.environ["HOME"]) / ".claude.json").write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get("/api/config-write/mcp?scope=user").status_code == 422


def test_route_write_over_non_utf8_file_is_422(write_config, tmp_path, projects_root) -> None:
    # The write-side complement: a non-UTF-8 existing file is read under the lock in
    # _mutate; the UnicodeDecodeError must surface as a clean 422, never escape the
    # ConfigWriteError guard as a 500. (A valid hash can't be formed for unreadable bytes,
    # but the decode failure is reached before the hash check via the stale-hash path.)
    (projects_root / "alpha" / ".mcp.json").write_bytes(b"\xff\xfe not utf-8")
    file_hash = hashlib.sha256(b"\xff\xfe not utf-8").hexdigest()
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "servers": {"s": {"command": "x"}},
                "hash": file_hash,
            },
        )
        assert resp.status_code == 422


def test_route_read_non_object_file_is_422(write_config, tmp_path, projects_root) -> None:
    # Valid JSON whose root is not an object (e.g. an array) is likewise a 422 on read,
    # not a 500 — we refuse to interpret a file we could not parse as a server map.
    (projects_root / "alpha" / ".mcp.json").write_text("[1, 2, 3]", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get("/api/config-write/mcp?project=alpha").status_code == 422


def test_route_write_missing_project_dir_is_404(write_config, tmp_path, projects_root) -> None:
    # A contained-but-absent project dir on the first-write path used to reach
    # mkstemp(dir=path.parent) and raise FileNotFoundError outside the ConfigWriteError
    # guard -> unhandled 500 (P2). It must now be a clean 404, and nothing is written.
    absent = projects_root / "noexist"
    assert not absent.exists()
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "noexist",
                "confirm": "noexist",
                "servers": {"s": {"command": "x"}},
            },
        )
        assert resp.status_code == 404
    assert not (absent / ".mcp.json").exists()  # nothing written


def test_route_confirm_runs_before_validate(write_config, tmp_path, projects_root) -> None:
    # P2 ordering: the type-the-name confirm is the FIRST semantic gate after capability.
    # A request that BOTH omits a valid confirm AND carries a malformed `servers` must
    # fail at the confirm gate (400), never reach the structural validator (422).
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "servers": "not-a-dict",  # malformed shape (would 422 if reached)
            },
        )
        assert resp.status_code == 400
    # User scope: same ordering — bad confirm short-circuits before the shape check.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={"scope": "user", "confirm": "WRONG", "servers": 123},
        )
        assert resp.status_code == 400


def test_route_missing_servers_is_422_after_valid_confirm(
    write_config, tmp_path, projects_root
) -> None:
    # With a VALID confirm token, a request omitting `servers` reaches the shape check
    # and is a 422 — confirms the reorder did not drop the structural guard.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={"scope": "project", "project": "alpha", "confirm": "alpha"},
        )
        assert resp.status_code == 422


def test_route_read_empty_project_is_422(write_config, tmp_path, projects_root) -> None:
    # GET with no/empty project name fails the project-string guard (422) before any I/O.
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get("/api/config-write/mcp?project=").status_code == 422


def test_route_user_missing_servers_is_422_after_valid_confirm(write_config, tmp_path) -> None:
    # User scope, valid confirm, no `servers` -> the user-branch shape check 422s.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN},
        )
        assert resp.status_code == 422


def test_route_user_invalid_entry_is_422(write_config, tmp_path) -> None:
    # User scope, valid confirm + dict `servers` but a malformed entry: the structural
    # validator raises InvalidCandidateError, mapped through _map_config_write_error -> 422.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "servers": {"s": {"command": "x", "bogus": 1}},  # unknown key
            },
        )
        assert resp.status_code == 422


def test_route_project_non_string_hash_is_422(write_config, tmp_path, projects_root) -> None:
    # A non-string `hash` (when present) is a 422 before any write.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "servers": {"s": {"command": "x"}},
                "hash": 123,  # not a string
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".mcp.json").exists()


def test_route_project_missing_name_is_400(write_config, tmp_path, projects_root) -> None:
    # Project scope with no `project` name: confirm runs first and a project-scope write
    # with no resolvable token fails closed at 400 (never reaches resolve/validate).
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={"scope": "project", "confirm": "", "servers": {"s": {"command": "x"}}},
        )
        assert resp.status_code == 400


# --- local scope routes (own confirm token, project-only gate, no gitignore) -------


def test_route_local_scope_works_without_allow_user_scope(
    write_config, tmp_path, projects_root
) -> None:
    # Local scope needs no allow_user_scope opt-in — the base `enabled` flag alone
    # gates it, same as project scope (#766 scope decision).
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        read0 = c.get("/api/config-write/mcp?scope=local&project=alpha")
        assert read0.status_code == 200
        assert read0.json() == {
            "scope": "local",
            "project": "alpha",
            "servers": {},
            "hash": None,
        }
        wr = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "servers": {"srv": {"command": "/bin/foo"}},
            },
        )
        assert wr.status_code == 200
        read1 = c.get("/api/config-write/mcp?scope=local&project=alpha")
        assert read1.json()["servers"] == {"srv": {"command": "/bin/foo"}}
    # The write landed in the ISOLATED home (autouse fixture), never the real account,
    # keyed by the resolved absolute project path — never the project-scope .mcp.json.
    isolated = Path(os.environ["HOME"]) / ".claude.json"
    out = json.loads(isolated.read_text(encoding="utf-8"))
    resolved_project = str((projects_root / "alpha").resolve())
    assert out["projects"][resolved_project]["mcpServers"]["srv"]["command"] == "/bin/foo"
    assert not (projects_root / "alpha" / ".mcp.json").exists()


def test_route_local_confirm_rejects_plain_project_name(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha",  # the PROJECT-scope token, not the local one
                "servers": {"srv": {"command": "/bin/foo"}},
            },
        )
        assert resp.status_code == 400


def test_route_local_scope_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get("/api/config-write/mcp?scope=local&project=alpha").status_code == 404
        assert (
            c.put(
                "/api/config-write/mcp",
                json={
                    "scope": "local",
                    "project": "alpha",
                    "confirm": "alpha (local)",
                    "servers": {},
                },
            ).status_code
            == 404
        )


def test_route_local_write_missing_project_dir_is_404(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "local",
                "project": "noexist",
                "confirm": "noexist (local)",
                "servers": {"s": {"command": "x"}},
            },
        )
        assert resp.status_code == 404


def test_route_local_write_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "local",
                "project": "../escape",
                "confirm": "../escape (local)",
                "servers": {},
            },
        )
        assert resp.status_code == 400
        assert c.get("/api/config-write/mcp?scope=local&project=../escape").status_code == 400


def test_route_local_write_bad_shape_is_422_and_writes_nothing(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "servers": {"s": {"command": "x", "bogus": 1}},
            },
        )
        assert resp.status_code == 422
    isolated = Path(os.environ["HOME"]) / ".claude.json"
    assert not isolated.exists()  # nothing written on a validation failure


def test_route_local_write_no_hash_forwarded(write_config, tmp_path, projects_root) -> None:
    # MCP local scope owns its own locking (nested subtree merge) — a client-supplied
    # "hash" must be silently ignored, never forwarded/enforced (mirrors user scope).
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "servers": {"s": {"command": "x"}},
                "hash": "bogus-hash-value",
            },
        )
        assert resp.status_code == 200


def test_route_local_read_corrupt_claude_json_is_422(
    write_config, tmp_path, projects_root
) -> None:
    # A hand-edited/corrupt ~/.claude.json must surface as a clean 422 from the local-
    # scope GET route too, never an unhandled 500 (same guard as the user-scope read).
    (Path(os.environ["HOME"]) / ".claude.json").write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get("/api/config-write/mcp?scope=local&project=alpha").status_code == 422
