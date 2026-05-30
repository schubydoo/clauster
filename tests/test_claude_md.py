"""CLAUDE.md viewer/editor (spec §5) — module + route coverage."""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.claude_md import (
    MAX_BYTES,
    ClaudeMdConflict,
    ClaudeMdError,
    ClaudeMdPathError,
    ClaudeMdTooLarge,
    read_claude_md,
    write_claude_md,
)
from clauster.config import load_config


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----- read -------------------------------------------------------------


def test_read_absent_file(tmp_path):
    doc = read_claude_md(tmp_path)
    assert doc.exists is False and doc.content == "" and doc.sha256 is None and doc.size == 0


def test_read_existing_file(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# hi\n", encoding="utf-8")
    doc = read_claude_md(tmp_path)
    assert doc.exists is True
    assert doc.content == "# hi\n"
    assert doc.sha256 == _sha("# hi\n")
    assert doc.size == len(b"# hi\n")


def test_read_non_utf8_rejected(tmp_path):
    (tmp_path / "CLAUDE.md").write_bytes(b"\xff\xfe not utf8")
    with pytest.raises(ClaudeMdError):
        read_claude_md(tmp_path)


def test_read_symlink_escape_rejected(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("password\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    os.symlink(secret, proj / "CLAUDE.md")
    with pytest.raises(ClaudeMdPathError):
        read_claude_md(proj)


# ----- write ------------------------------------------------------------


def test_write_creates_file_and_audit(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    state = tmp_path / "state"
    doc = write_claude_md(proj, "# new\n", state_dir=state, user="admin")
    assert doc.exists and doc.content == "# new\n" and doc.sha256 == _sha("# new\n")
    assert (proj / "CLAUDE.md").read_text() == "# new\n"

    line = (state / "claude_md_audit.log").read_text().strip()
    entry = json.loads(line)
    assert entry["project"] == "proj" and entry["action"] == "create"
    assert entry["user"] == "admin" and entry["sha256"] == doc.sha256


def test_write_update_audits_as_update(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("old\n")
    state = tmp_path / "state"
    write_claude_md(proj, "new\n", base_sha256=_sha("old\n"), state_dir=state)
    actions = [
        json.loads(line)["action"]
        for line in (state / "claude_md_audit.log").read_text().splitlines()
    ]
    assert actions == ["update"]


def test_write_over_cap_rejected(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    with pytest.raises(ClaudeMdTooLarge):
        write_claude_md(proj, "x" * (MAX_BYTES + 1))
    assert not (proj / "CLAUDE.md").exists()  # nothing written


def test_write_at_cap_allowed(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    doc = write_claude_md(proj, "x" * MAX_BYTES)
    assert doc.size == MAX_BYTES


def test_write_stale_base_sha_conflict(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("current\n")
    with pytest.raises(ClaudeMdConflict):
        write_claude_md(proj, "mine\n", base_sha256=_sha("what-i-loaded\n"))
    assert (proj / "CLAUDE.md").read_text() == "current\n"  # unchanged


def test_write_matching_base_sha_succeeds(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("current\n")
    doc = write_claude_md(proj, "mine\n", base_sha256=_sha("current\n"))
    assert doc.content == "mine\n"


def test_write_replace_failure_cleans_tmp_and_raises(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(
        "clauster.claude_md.os.replace",
        lambda s, d: (_ for _ in ()).throw(OSError("cross-device")),
    )
    with pytest.raises(ClaudeMdError):
        write_claude_md(proj, "hello\n")
    assert list(proj.glob("CLAUDE.md*")) == []  # no orphan .tmp left behind


def test_write_audit_failure_does_not_fail_write(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    # Audit append raises, but the content write already committed -> save succeeds.
    monkeypatch.setattr(
        "clauster.claude_md._append_audit",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    doc = write_claude_md(proj, "kept\n", state_dir=tmp_path / "state")
    assert doc.exists and (proj / "CLAUDE.md").read_text() == "kept\n"


def test_write_symlink_escape_rejected(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("password\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    os.symlink(secret, proj / "CLAUDE.md")
    with pytest.raises(ClaudeMdPathError):
        write_claude_md(proj, "pwned\n")
    assert secret.read_text() == "password\n"  # untouched


# ----- routes -----------------------------------------------------------


def _client(write_config, tmp_path) -> TestClient:
    # state_dir is dot-prefixed so discovery never scans it as a project.
    cfg = load_config(write_config(f"state_dir: {tmp_path}/.state\n"))
    return TestClient(create_app(cfg))


def test_get_claude_md_absent(write_config, tmp_path):
    client = _client(write_config, tmp_path)
    body = client.get("/api/projects/alpha/claude-md").json()  # alpha has no CLAUDE.md
    assert body["exists"] is False and body["content"] == "" and body["bridge_running"] is False


def test_get_claude_md_present(write_config, tmp_path):
    client = _client(write_config, tmp_path)
    resp = client.get("/api/projects/beta/claude-md")  # beta fixture has "# beta\n"
    assert resp.status_code == 200
    assert resp.json()["content"] == "# beta\n"


def test_get_claude_md_unknown_project_404(write_config, tmp_path):
    client = _client(write_config, tmp_path)
    assert client.get("/api/projects/nope/claude-md").status_code == 404
    assert client.get("/api/projects/..%2Fetc/claude-md").status_code == 404


def test_put_claude_md_creates(write_config, tmp_path):
    client = _client(write_config, tmp_path)
    resp = client.put("/api/projects/alpha/claude-md", json={"content": "# alpha rules\n"})
    assert resp.status_code == 200
    assert resp.json()["exists"] is True
    assert client.get("/api/projects/alpha/claude-md").json()["content"] == "# alpha rules\n"


def test_put_claude_md_over_cap_413(write_config, tmp_path):
    client = _client(write_config, tmp_path)
    resp = client.put("/api/projects/alpha/claude-md", json={"content": "x" * (MAX_BYTES + 1)})
    assert resp.status_code == 413


def test_put_claude_md_conflict_409(write_config, tmp_path):
    client = _client(write_config, tmp_path)
    resp = client.put(
        "/api/projects/beta/claude-md",
        json={"content": "clobber\n", "base_sha256": _sha("stale\n")},
    )
    assert resp.status_code == 409


def test_put_claude_md_requires_content_422(write_config, tmp_path):
    client = _client(write_config, tmp_path)
    assert client.put("/api/projects/alpha/claude-md", json={}).status_code == 422
