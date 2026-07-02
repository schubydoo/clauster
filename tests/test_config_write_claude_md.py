"""CLAUDE.md config-write surface (#768) over the #347/#687 Foundation.

Covers the pure structural validator (a string under the size cap — no shape to
speak of, since CLAUDE.md is free-form prose, never parsed or executed), the
project/user/local read+write functions built on the #766 file/dir-writer
primitive, and the gated routes (capability/scope 404, type-the-name 400,
bad-shape/oversize 422 writing nothing, stale-hash 409, path-escape 400,
non-UTF-8 422, project-scope root-vs-``.claude/`` preference, and the
content-tier decision that this surface — alone among the config-write
children — never redacts on read).

Every test that touches ``~/.claude/CLAUDE.md`` runs under the autouse
HOME-isolation fixture and writes only into the isolated tmp home — the live
account is never touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import claude_md as cm
from clauster import config_write as cw
from clauster.app import create_app
from clauster.config import load_config

# --- structural validator -----------------------------------------------------------


def test_validate_accepts_string() -> None:
    cm.validate_content("# hello\n")  # no raise


def test_validate_accepts_empty_string() -> None:
    cm.validate_content("")  # "blank" op is just an empty string — no raise


def test_validate_rejects_non_string() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        cm.validate_content(123)


def test_validate_rejects_oversize() -> None:
    too_big = "x" * (cm.MAX_BYTES + 1)
    with pytest.raises(cw.InvalidCandidateError):
        cm.validate_content(too_big)


def test_validate_accepts_exactly_at_cap() -> None:
    cm.validate_content("x" * cm.MAX_BYTES)  # no raise — the cap itself is allowed


# --- containment rewrap (fw.PathEscapeError -> cw.PathEscapeError) -----------------


def test_resolve_rewraps_path_escape_error(tmp_path: Path) -> None:
    # The public read/write functions only ever pass fixed, safe relative names
    # (FILENAME/LOCAL_FILENAME) — this exercises the containment rewrap directly, the
    # same way test_config_file_writer.py exercises resolve_contained_path directly.
    with pytest.raises(cw.PathEscapeError):
        cm._resolve(tmp_path, "../escape.md")


# --- project scope: read/write round trip + root-vs-.claude/ preference -------------


def test_project_read_absent_is_empty(tmp_path: Path) -> None:
    content, file_hash, exists = cm.read_project_claude_md(tmp_path)
    assert content == ""
    assert exists is False
    assert file_hash == cw.hash_bytes(b"")


def test_project_write_then_read_round_trip(tmp_path: Path) -> None:
    _c, h0, _e = cm.read_project_claude_md(tmp_path)
    cm.write_project_claude_md(tmp_path, "# hello\n", h0)
    content, _h1, exists = cm.read_project_claude_md(tmp_path)
    assert content == "# hello\n"
    assert exists is True
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "# hello\n"


def test_project_write_defaults_to_root_when_neither_exists(tmp_path: Path) -> None:
    cm.write_project_claude_md(tmp_path, "root please", None)
    assert (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()


def test_project_prefers_existing_dot_claude_location(tmp_path: Path) -> None:
    # No root CLAUDE.md, but one already lives under .claude/ — reads and writes must
    # target that existing file, never silently create a second one at the root.
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("dotclaude content\n", encoding="utf-8")
    content, h, exists = cm.read_project_claude_md(tmp_path)
    assert content == "dotclaude content\n"
    assert exists is True
    cm.write_project_claude_md(tmp_path, "updated\n", h)
    assert (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8") == "updated\n"
    assert not (tmp_path / "CLAUDE.md").exists()


def test_project_prefers_root_when_both_exist(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("root\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("dotclaude\n", encoding="utf-8")
    content, _h, _exists = cm.read_project_claude_md(tmp_path)
    assert content == "root\n"


def test_project_write_stale_hash_raises(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("original\n", encoding="utf-8")
    stale = cw.hash_bytes(b"something else")
    with pytest.raises(cw.StaleConfigWriteError):
        cm.write_project_claude_md(tmp_path, "new\n", stale)


def test_project_write_no_hash_on_existing_file_is_stale(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("original\n", encoding="utf-8")
    with pytest.raises(cw.StaleConfigWriteError):
        cm.write_project_claude_md(tmp_path, "new\n", None)


def test_project_write_no_hash_on_absent_file_ok(tmp_path: Path) -> None:
    cm.write_project_claude_md(tmp_path, "fresh\n", None)
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "fresh\n"


def test_project_write_bad_shape_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        cm.write_project_claude_md(tmp_path, "x" * (cm.MAX_BYTES + 1), None)
    assert not (tmp_path / "CLAUDE.md").exists()


def test_project_read_rejects_non_utf8(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(cw.InvalidCandidateError):
        cm.read_project_claude_md(tmp_path)


# --- user scope (HOME-isolated, explicit tmp file) ----------------------------------


def test_user_read_absent_is_empty(tmp_path: Path) -> None:
    claude_json = tmp_path / ".claude.json"
    content, h, exists = cm.read_user_claude_md(claude_json)
    assert content == ""
    assert exists is False
    assert h == cw.hash_bytes(b"")


def test_user_write_then_read_round_trip(tmp_path: Path) -> None:
    claude_json = tmp_path / ".claude.json"
    _c, h0, _e = cm.read_user_claude_md(claude_json)
    cm.write_user_claude_md(claude_json, "# my memory\n", h0)
    content, _h1, exists = cm.read_user_claude_md(claude_json)
    assert content == "# my memory\n"
    assert exists is True
    assert (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8") == "# my memory\n"


def test_user_write_rejects_bad_shape_without_writing(tmp_path: Path) -> None:
    claude_json = tmp_path / ".claude.json"
    with pytest.raises(cw.InvalidCandidateError):
        cm.write_user_claude_md(claude_json, 123, None)  # type: ignore[arg-type]
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()


# --- local scope (CLAUDE.local.md at project root, gitignore-on-create) -------------


def test_local_write_then_read_round_trip(tmp_path: Path) -> None:
    _c, h0, _e = cm.read_project_local_claude_md(tmp_path)
    cm.write_project_local_claude_md(tmp_path, "only me\n", h0)
    content, _h1, exists = cm.read_project_local_claude_md(tmp_path)
    assert content == "only me\n"
    assert exists is True


def test_local_write_targets_project_root_not_dot_claude(tmp_path: Path) -> None:
    cm.write_project_local_claude_md(tmp_path, "local\n", None)
    assert (tmp_path / "CLAUDE.local.md").exists()
    assert not (tmp_path / ".claude" / "CLAUDE.local.md").exists()


def test_local_write_creates_gitignore_entry(tmp_path: Path) -> None:
    cm.write_project_local_claude_md(tmp_path, "local\n", None)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "CLAUDE.local.md" in gitignore


def test_local_write_gitignore_idempotent_across_writes(tmp_path: Path) -> None:
    _c, h0, _e = cm.read_project_local_claude_md(tmp_path)
    cm.write_project_local_claude_md(tmp_path, "v1\n", h0)
    _c1, h1, _e1 = cm.read_project_local_claude_md(tmp_path)
    cm.write_project_local_claude_md(tmp_path, "v2\n", h1)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.count("CLAUDE.local.md") == 1


def test_local_write_bad_shape_writes_nothing_and_no_gitignore(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        cm.write_project_local_claude_md(tmp_path, "x" * (cm.MAX_BYTES + 1), None)
    assert not (tmp_path / "CLAUDE.local.md").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_local_write_stale_hash_raises(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.local.md").write_text("original\n", encoding="utf-8")
    stale = cw.hash_bytes(b"something else")
    with pytest.raises(cw.StaleConfigWriteError):
        cm.write_project_local_claude_md(tmp_path, "new\n", stale)


def test_local_scope_is_independent_of_project_scope_file(tmp_path: Path) -> None:
    cm.write_project_claude_md(tmp_path, "project content\n", None)
    cm.write_project_local_claude_md(tmp_path, "local content\n", None)
    project_content, _h, _e = cm.read_project_claude_md(tmp_path)
    local_content, _h2, _e2 = cm.read_project_local_claude_md(tmp_path)
    assert project_content == "project content\n"
    assert local_content == "local content\n"


# --- gated routes (full FastAPI lifespan) --------------------------------------------

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"

_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
_PROJECT_ONLY = "config_write:\n  enabled: true\n"

_URL = "/api/config-write/claude-md"


def _client(write_config, tmp_path, extra: str) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}")
    return TestClient(create_app(load_config(cfg)))


def test_route_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get(f"{_URL}?project=alpha").status_code == 404
        assert (
            c.put(
                _URL,
                json={"scope": "project", "project": "alpha", "confirm": "alpha", "content": ""},
            ).status_code
            == 404
        )


def test_route_user_scope_404_when_user_off(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        assert c.get(f"{_URL}?scope=user").status_code == 404
        assert (
            c.put(
                _URL,
                json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "content": ""},
            ).status_code
            == 404
        )


def test_route_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?scope=bogus").status_code == 422
        assert c.put(_URL, json={"scope": "bogus", "content": ""}).status_code == 422


def test_route_confirm_mismatch_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "content": "hi",
            },
        )
        assert resp.status_code == 400


def test_route_confirm_runs_before_content_shape_check(write_config, tmp_path) -> None:
    # Ordering: confirm is the first semantic gate after capability — a request that
    # both omits a valid confirm AND carries a malformed `content` must fail at the
    # confirm gate (400), never reach the shape check (422).
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={"scope": "project", "project": "alpha", "confirm": "WRONG", "content": 123},
        )
        assert resp.status_code == 400


def test_route_bad_content_shape_is_422_and_writes_nothing(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={"scope": "project", "project": "alpha", "confirm": "alpha", "content": 123},
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / "CLAUDE.md").exists()


def test_route_oversize_content_is_422_and_writes_nothing(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": "x" * (cm.MAX_BYTES + 1),
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / "CLAUDE.md").exists()


def test_route_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "../escape",
                "confirm": "../escape",
                "content": "x",
            },
        )
        assert resp.status_code == 400
        assert c.get(f"{_URL}?project=../escape").status_code == 400


def test_route_stale_hash_is_409(write_config, tmp_path, projects_root) -> None:
    (projects_root / "alpha" / "CLAUDE.md").write_text("original\n", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": "new\n",
                "hash": cw.hash_bytes(b"something else"),
            },
        )
        assert resp.status_code == 409


def test_route_no_hash_on_existing_file_is_409(write_config, tmp_path, projects_root) -> None:
    (projects_root / "alpha" / "CLAUDE.md").write_text("original\n", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": "new\n",
            },
        )
        assert resp.status_code == 409


def test_route_project_write_read_round_trip(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        read0 = c.get(f"{_URL}?project=alpha")
        assert read0.status_code == 200
        assert read0.json()["content"] == ""
        assert read0.json()["exists"] is False
        h0 = read0.json()["hash"]
        wr = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": "# alpha memory\n",
                "hash": h0,
            },
        )
        assert wr.status_code == 200
        read1 = c.get(f"{_URL}?project=alpha")
        assert read1.json()["content"] == "# alpha memory\n"
        assert read1.json()["exists"] is True


def test_route_project_read_picks_up_existing_root_fixture(
    write_config, tmp_path, projects_root
) -> None:
    # The `beta` fixture project already has a root CLAUDE.md (conftest.projects_root).
    with _client(write_config, tmp_path, _ON) as c:
        read = c.get(f"{_URL}?project=beta")
        assert read.status_code == 200
        assert read.json()["content"] == "# beta\n"
        assert read.json()["exists"] is True


def test_route_missing_project_dir_is_404(write_config, tmp_path, projects_root) -> None:
    absent = projects_root / "noexist"
    assert not absent.exists()
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "noexist",
                "confirm": "noexist",
                "content": "x",
            },
        )
        assert resp.status_code == 404
    assert not absent.exists()


def test_route_user_write_round_trip(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        read0 = c.get(f"{_URL}?scope=user")
        assert read0.status_code == 200
        h0 = read0.json()["hash"]
        wr = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "content": "# user memory\n",
                "hash": h0,
            },
        )
        assert wr.status_code == 200
        read1 = c.get(f"{_URL}?scope=user")
        assert read1.json()["content"] == "# user memory\n"
    # Lands in the ISOLATED home (autouse fixture), never the real account.
    isolated = Path(os.environ["HOME"]) / ".claude" / "CLAUDE.md"
    assert isolated.read_text(encoding="utf-8") == "# user memory\n"


def test_route_read_does_not_redact_secret_shaped_content(
    write_config, tmp_path, projects_root
) -> None:
    # The resolved content-tier threat model: CLAUDE.md is prose, not credentials —
    # unlike every other config-write read route, this one must NEVER mask a
    # secret-shaped line.
    secret_line = "api_key: sk-super-secret-value\n"
    (projects_root / "alpha" / "CLAUDE.md").write_text(secret_line, encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        read = c.get(f"{_URL}?project=alpha")
        assert read.json()["content"] == secret_line


# --- local scope routes (own confirm token, project-only gate, gitignore-on-create) -


def test_route_local_scope_works_without_allow_user_scope(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        read0 = c.get(f"{_URL}?scope=local&project=alpha")
        assert read0.status_code == 200
        body = read0.json()
        assert body["scope"] == "local"
        assert body["project"] == "alpha"
        assert body["content"] == ""
        assert body["exists"] is False
        wr = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "content": "just for me\n",
            },
        )
        assert wr.status_code == 200
        read1 = c.get(f"{_URL}?scope=local&project=alpha")
        assert read1.json()["content"] == "just for me\n"


def test_route_local_confirm_rejects_plain_project_name(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha",  # the PROJECT-scope token, not the local one
                "content": "x",
            },
        )
        assert resp.status_code == 400


def test_route_local_write_creates_gitignore_entry(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        wr = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "content": "local\n",
            },
        )
        assert wr.status_code == 200
    gitignore = (projects_root / "alpha" / ".gitignore").read_text(encoding="utf-8")
    assert "CLAUDE.local.md" in gitignore


def test_route_local_scope_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get(f"{_URL}?scope=local&project=alpha").status_code == 404
        assert (
            c.put(
                _URL,
                json={
                    "scope": "local",
                    "project": "alpha",
                    "confirm": "alpha (local)",
                    "content": "",
                },
            ).status_code
            == 404
        )


def test_route_local_write_missing_project_dir_is_404(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "noexist",
                "confirm": "noexist (local)",
                "content": "x",
            },
        )
        assert resp.status_code == 404


def test_route_local_write_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "../escape",
                "confirm": "../escape (local)",
                "content": "x",
            },
        )
        assert resp.status_code == 400
        assert c.get(f"{_URL}?scope=local&project=../escape").status_code == 400


def test_route_local_write_bad_shape_is_422_and_writes_nothing(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "content": 123,
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / "CLAUDE.local.md").exists()


def test_route_local_write_missing_content_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={"scope": "local", "project": "alpha", "confirm": "alpha (local)"},
        )
        assert resp.status_code == 422


def test_route_local_write_non_string_hash_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "content": "x",
                "hash": 123,
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / "CLAUDE.local.md").exists()


# --- route-level read/error guards --------------------------------------------------


def test_route_read_non_utf8_project_file_is_422(write_config, tmp_path, projects_root) -> None:
    (projects_root / "alpha" / "CLAUDE.md").write_bytes(b"\xff\xfe not utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?project=alpha").status_code == 422


def test_route_read_corrupt_user_file_is_422(write_config, tmp_path) -> None:
    # The user-scope read guard: a hand-edited ~/.claude/CLAUDE.md that is not valid
    # UTF-8 must surface as a clean 422, never an unhandled 500.
    user_claude_md = Path(os.environ["HOME"]) / ".claude" / "CLAUDE.md"
    user_claude_md.parent.mkdir(parents=True, exist_ok=True)
    user_claude_md.write_bytes(b"\xff\xfe not utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?scope=user").status_code == 422


def test_route_local_read_corrupt_file_is_422(write_config, tmp_path, projects_root) -> None:
    # The local-scope read guard: a hand-edited CLAUDE.local.md that is not valid UTF-8
    # must surface as a clean 422 from the GET route, never an unhandled 500 (same
    # guard as the project/user reads above).
    (projects_root / "alpha" / "CLAUDE.local.md").write_bytes(b"\xff\xfe not utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?scope=local&project=alpha").status_code == 422


def test_route_user_write_stale_hash_is_409(write_config, tmp_path) -> None:
    # The user-scope write guard: a stale/mismatched hash must surface as a clean 409
    # from the PUT route, never an unhandled 500 — exercises the user-scope branch of
    # the write route's error mapping (distinct from the project/local branch above).
    user_claude_md = Path(os.environ["HOME"]) / ".claude" / "CLAUDE.md"
    user_claude_md.parent.mkdir(parents=True, exist_ok=True)
    user_claude_md.write_text("original\n", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "content": "new\n",
                "hash": cw.hash_bytes(b"something else"),
            },
        )
        assert resp.status_code == 409


def test_route_write_over_non_utf8_project_file_succeeds(
    write_config, tmp_path, projects_root
) -> None:
    # Unlike the JSON-subtree surfaces (permissions/hooks/MCP), a CLAUDE.md write is a
    # full-content overwrite — it never needs to parse/merge the EXISTING bytes, only
    # hash them for the stale-hash guard — so a matching hash over a non-UTF-8 existing
    # file still succeeds and replaces it wholesale.
    (projects_root / "alpha" / "CLAUDE.md").write_bytes(b"\xff\xfe not utf-8")
    file_hash = cw.hash_bytes(b"\xff\xfe not utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": "new\n",
                "hash": file_hash,
            },
        )
        assert resp.status_code == 200
    assert (projects_root / "alpha" / "CLAUDE.md").read_text(encoding="utf-8") == "new\n"


def test_route_read_empty_project_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?project=").status_code == 422


def test_route_project_non_string_hash_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": "x",
                "hash": 123,
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / "CLAUDE.md").exists()


def test_route_project_missing_name_is_400(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={"scope": "project", "confirm": "", "content": "x"},
        )
        assert resp.status_code == 400
