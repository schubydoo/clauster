"""CLAUDE.md viewer/editor (spec §5) — module + route coverage."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import atomicio as _atomicio
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


def test_write_non_oserror_mid_write_removes_unique_temp(tmp_path, monkeypatch):
    # A non-OSError BaseException (e.g. KeyboardInterrupt) mid-write must remove the UNIQUE
    # temp and re-raise as-is — a unique CLAUDE.md.<pid>.<hex>.tmp can't self-heal like the
    # old fixed name, so it would otherwise accumulate next to CLAUDE.md.
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(
        "clauster.claude_md.atomicio.replace_with_retry",
        lambda s, d: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):  # re-raised as-is, NOT wrapped in ClaudeMdError
        write_claude_md(proj, "hello\n")
    assert list(proj.glob("CLAUDE.md*")) == []  # unique temp removed, no debris


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


def test_write_claude_md_acquires_cross_process_lock(tmp_path, monkeypatch):
    # The editor must take atomicio.cross_process_lock for the target — the SAME shared
    # cross-process lock the config-write path takes — so the two write paths mutually
    # exclude across processes (follow-up to #915). Spy that it is entered for the target.
    from clauster import atomicio

    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "CLAUDE.md"
    seen: list[Path] = []
    real = atomicio.cross_process_lock

    def _spy(t):
        seen.append(t)
        return real(t)

    monkeypatch.setattr("clauster.claude_md.atomicio.cross_process_lock", _spy)
    write_claude_md(proj, "hello\n")
    assert seen == [target]


def test_write_claude_md_shares_lock_file_with_config_write_path(tmp_path):
    # The editor's target and the config-write path's target for the project-root CLAUDE.md
    # must resolve to ONE cross-process lock file — that shared file is what serializes the
    # two surfaces. Confirm both map to the same lock file under a configured lock dir.
    from clauster import atomicio, claude_md
    from clauster import config_file_writer as fw

    atomicio.configure_lock_dir(tmp_path / "locks")
    proj = tmp_path / "proj"
    proj.mkdir()
    editor_target = claude_md._target(proj)
    config_write_target = fw.resolve_contained_path(proj, "CLAUDE.md")
    assert atomicio._cross_process_lock_file(editor_target) == atomicio._cross_process_lock_file(
        config_write_target
    )


def test_write_claude_md_leaves_no_lock_in_project_dir(tmp_path):
    # De-litter regression: after a write the project dir holds CLAUDE.md but NO `.lock`
    # (the cross-process lock lives in the configured state dir), even with it configured.
    from clauster import atomicio

    atomicio.configure_lock_dir(tmp_path / "locks")
    proj = tmp_path / "proj"
    proj.mkdir()
    write_claude_md(proj, "hello\n")
    assert (proj / "CLAUDE.md").read_text(encoding="utf-8") == "hello\n"
    assert not any(c.name.endswith(".lock") for c in proj.iterdir())


@pytest.mark.skipif(
    _atomicio.fcntl is None, reason="cross-process flock warning is POSIX-only; Windows no-ops it"
)
def test_write_claude_md_warns_once_when_lock_dir_unconfigured(tmp_path, caplog):
    # Unconfigured cross-process lock (test-only; create_app always configures it) → the
    # write still succeeds under the inproc lock, but the degrade is logged, never silent.
    import logging

    proj = tmp_path / "proj"
    proj.mkdir()
    with caplog.at_level(logging.WARNING, logger="clauster.atomicio"):
        write_claude_md(proj, "hello\n")
    assert (proj / "CLAUDE.md").read_text(encoding="utf-8") == "hello\n"
    assert any("cross-process file lock dir not configured" in r.message for r in caplog.records)


def test_write_claude_md_uses_unique_temp_name(tmp_path, monkeypatch):
    # A UNIQUE temp name (not a fixed CLAUDE.md.tmp) so a second clauster PROCESS saving the
    # same project can't clobber this one's temp mid-replace — the inproc lock is intra-process
    # only (#914).
    proj = tmp_path / "proj"
    proj.mkdir()
    seen: list = []

    def _spy_replace(src, dst):
        seen.append(Path(src).name)
        Path(src).replace(dst)

    monkeypatch.setattr("clauster.claude_md.atomicio.replace_with_retry", _spy_replace)
    write_claude_md(proj, "one\n")
    write_claude_md(proj, "two\n")
    assert seen[0] != "CLAUDE.md.tmp" and seen[1] != "CLAUDE.md.tmp"  # not the fixed shared name
    assert seen[0] != seen[1]  # unique per write
    assert all(n.startswith("CLAUDE.md.") and n.endswith(".tmp") for n in seen)


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


def test_create_app_configures_cross_process_lock_dir(write_config, tmp_path):
    # create_app must point the cross-process lock at <state_dir>/locks before any request
    # can write, so prod is ALWAYS configured (the warn-once path is then test-only misuse).
    from clauster import atomicio

    cfg = load_config(write_config(f"state_dir: {tmp_path}/.state\n"))
    create_app(cfg)
    assert atomicio._LOCK_DIR == (Path(cfg.state_dir).expanduser() / "locks")
    assert atomicio._LOCK_DIR.is_dir()


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
