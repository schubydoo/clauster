"""CLAUDE.md viewer/editor (spec §5) — module + route coverage."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.claude_md import (
    MAX_BYTES,
    ClaudeMdConflict,
    ClaudeMdError,
    ClaudeMdNotTrusted,
    ClaudeMdPathError,
    ClaudeMdTooLarge,
    read_claude_md,
    write_claude_md,
)
from clauster.config import load_config
from clauster.runner import SessionRunner


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


def test_write_stale_base_sha_conflict_check_runs_inside_lock(tmp_path, monkeypatch):
    # The base_sha256 read-check-write is one critical section (#914) so two concurrent
    # same-project saves can't both pass the guard and lost-update: the conflict read runs
    # under the target's lock — assert it's held at raise time.
    from clauster import atomicio

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("current\n")
    target = proj / "CLAUDE.md"
    real_read = read_claude_md
    seen: dict = {}

    def _spy_read(path):
        # read_claude_md is called inside the write's critical section — capture lock state.
        seen["locked"] = atomicio.inproc_path_lock(target).locked()
        return real_read(path)

    monkeypatch.setattr("clauster.claude_md.read_claude_md", _spy_read)
    with pytest.raises(ClaudeMdConflict):
        write_claude_md(proj, "mine\n", base_sha256=_sha("what-i-loaded\n"))
    assert seen["locked"] is True  # the conflict guard read ran under the lock


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
        "clauster.claude_md.atomicio.replace_with_retry",
        lambda s, d: (_ for _ in ()).throw(OSError("cross-device")),
    )
    with pytest.raises(ClaudeMdError):
        write_claude_md(proj, "hello\n")
    assert list(proj.glob("CLAUDE.md*")) == []  # no orphan .tmp left behind


def test_write_replace_failure_tolerates_unlink_failure(tmp_path, monkeypatch):
    # If the atomic write fails AND the temp-file cleanup ALSO fails, the original
    # write error must still surface as ClaudeMdError (the unlink OSError is swallowed,
    # not re-raised over the real cause).
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(
        "clauster.claude_md.atomicio.replace_with_retry",
        lambda s, d: (_ for _ in ()).throw(OSError("cross-device")),
    )
    real_unlink = Path.unlink

    def boom_unlink(self, *args, **kwargs):
        if self.suffix == ".tmp":
            raise OSError("simulated: temp cleanup failed")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", boom_unlink)
    with pytest.raises(ClaudeMdError, match="could not write"):
        write_claude_md(proj, "hello\n")


def test_write_claude_md_holds_per_path_lock_during_replace(tmp_path, monkeypatch):
    # The fixed-name temp write is serialized under the per-path in-process lock (#914) so two
    # concurrent saves to the same project can't move/clobber the same CLAUDE.md.tmp mid-replace.
    from clauster import atomicio

    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "CLAUDE.md"
    seen: dict = {}

    def _spy_replace(src, dst):
        # The write must run INSIDE the target's lock — assert it's held right now.
        seen["locked"] = atomicio.inproc_path_lock(target).locked()
        Path(src).replace(dst)

    monkeypatch.setattr("clauster.claude_md.atomicio.replace_with_retry", _spy_replace)
    write_claude_md(proj, "hello\n")
    assert seen["locked"] is True


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


def test_write_untrusted_project_refused(tmp_path):
    # When claude_json is supplied, an untrusted project dir (e.g. a symlink that
    # resolves outside projects_root) must be refused — the confinement gate.
    proj = tmp_path / "proj"
    proj.mkdir()
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(json.dumps({"projects": {}}))  # trusts nothing
    with pytest.raises(ClaudeMdNotTrusted):
        write_claude_md(proj, "x\n", claude_json=claude_json)
    assert not (proj / "CLAUDE.md").exists()


# ----- routes -----------------------------------------------------------


def _client(write_config, tmp_path) -> TestClient:
    # state_dir is dot-prefixed so discovery never scans it as a project.
    cfg = load_config(write_config(f"state_dir: {tmp_path}/.state\n"))
    # CLAUDE.md writes are trust-gated; trust projects_root so edits are allowed.
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps(
            {"projects": {str(cfg.projects_root.resolve()): {"hasTrustDialogAccepted": True}}}
        )
    )
    runner = SessionRunner(cfg, claude_json=claude_json)
    return TestClient(create_app(cfg, runner=runner))


def test_get_claude_md_absent(write_config, tmp_path):
    client = _client(write_config, tmp_path)
    body = client.get("/api/projects/alpha/claude-md").json()  # alpha has no CLAUDE.md
    assert body["exists"] is False and body["content"] == "" and body["bridge_running"] is False


def test_get_claude_md_reports_bridge_running_for_any_running_instance(write_config, tmp_path):
    """bridge_running flips true off the any-RUNNING scan, even a display-hidden pty (#778).

    A RUNNING pty registered before a stopped standard bridge is invisible to the
    project-keyed card, but the editor's caution flag must still report it.
    """
    from clauster.models import InstanceStatus, RemoteControlInstance

    client = _client(write_config, tmp_path)
    runner = client.app.state.runner
    pty = RemoteControlInstance(
        project="alpha", label="alpha", resume_mode="pty", status=InstanceStatus.RUNNING
    )
    stopped = RemoteControlInstance(
        project="alpha", label="alpha", resume_mode="standard", status=InstanceStatus.STOPPED
    )
    runner._instances[pty.instance_id] = pty
    runner._instances[stopped.instance_id] = stopped  # last-registered → the displayed card
    body = client.get("/api/projects/alpha/claude-md").json()
    assert body["bridge_running"] is True


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
