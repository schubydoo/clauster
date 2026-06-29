"""MCP-server config-write surface (#688) over the #347 Foundation.

Covers the pure structural validator (validate-never-execute, incl. an RCE-negative
sentinel that proves a candidate ``command`` is never run), the project/user read+write
routers, and the gated routes (capability/scope 404, type-the-name 400, bad-shape 422
writing nothing, stale-hash 409, path-escape 400, redaction round-trip).

Every test that touches ``~/.claude.json`` passes an explicit ``tmp_path`` file and
runs under the autouse HOME-isolation fixture — the live account is never touched.
"""

from __future__ import annotations

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
