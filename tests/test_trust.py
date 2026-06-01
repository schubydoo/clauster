from __future__ import annotations

import json
from pathlib import Path

from clauster import trust


def test_trust_directory_sets_flag_and_preserves_other_keys(tmp_path: Path):
    cj = tmp_path / "claude.json"
    cj.write_text(
        json.dumps({"projects": {"/other": {"hasTrustDialogAccepted": True}}, "misc": 1})
    )
    target = tmp_path / "proj"
    target.mkdir()

    trust.trust_directory(target, cj)
    data = json.loads(cj.read_text())

    assert data["projects"][str(target.resolve())]["hasTrustDialogAccepted"] is True
    assert data["projects"]["/other"]["hasTrustDialogAccepted"] is True  # untouched
    assert data["misc"] == 1  # unrelated keys preserved


def test_trust_directory_creates_backup_once(tmp_path: Path):
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"projects": {}}))
    target = tmp_path / "proj"
    target.mkdir()

    trust.trust_directory(target, cj)
    backup = cj.with_suffix(cj.suffix + ".bak")
    assert backup.exists()
    assert json.loads(backup.read_text()) == {"projects": {}}  # pre-modification snapshot


def test_trust_directory_idempotent_and_is_trusted(tmp_path: Path):
    cj = tmp_path / "claude.json"
    cj.write_text("{}")
    target = tmp_path / "proj"
    target.mkdir()

    assert trust.is_trusted(target, cj) is False
    trust.trust_directory(target, cj)
    trust.trust_directory(target, cj)  # second call must not corrupt
    assert trust.is_trusted(target, cj) is True


def test_trust_missing_file_is_created(tmp_path: Path):
    cj = tmp_path / "claude.json"  # does not exist
    target = tmp_path / "proj"
    target.mkdir()
    trust.trust_directory(target, cj)
    assert trust.is_trusted(target, cj) is True


def test_trust_directory_non_dict_root_coerced(tmp_path: Path):
    # A valid-JSON but non-object ~/.claude.json must not crash; it's coerced to
    # {} and the trust write proceeds.
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    target = tmp_path / "proj"
    target.mkdir()
    trust.trust_directory(target, cj)
    assert trust.is_trusted(target, cj) is True


def test_ensure_remote_control_enabled_sets_flags_and_preserves(tmp_path: Path):
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"projects": {"/p": {"hasTrustDialogAccepted": True}}, "misc": 1}))

    changed = trust.ensure_remote_control_enabled(cj)
    data = json.loads(cj.read_text())

    assert changed is True
    assert data["hasUsedRemoteControl"] is True
    assert data["remoteDialogSeen"] is True
    assert data["projects"]["/p"]["hasTrustDialogAccepted"] is True  # untouched
    assert data["misc"] == 1


def test_ensure_remote_control_enabled_idempotent(tmp_path: Path):
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"hasUsedRemoteControl": True, "remoteDialogSeen": True, "x": 9}))

    changed = trust.ensure_remote_control_enabled(cj)

    assert changed is False  # already acknowledged -> no rewrite
    assert json.loads(cj.read_text())["x"] == 9


def test_ensure_remote_control_enabled_missing_file_created(tmp_path: Path):
    cj = tmp_path / "claude.json"  # does not exist
    assert trust.ensure_remote_control_enabled(cj) is True
    data = json.loads(cj.read_text())
    assert data["hasUsedRemoteControl"] is True and data["remoteDialogSeen"] is True


def test_ensure_remote_control_enabled_non_dict_root_coerced(tmp_path: Path):
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert trust.ensure_remote_control_enabled(cj) is True
    assert json.loads(cj.read_text())["hasUsedRemoteControl"] is True


def test_trust_backup_failure_is_logged_not_silent(tmp_path: Path, caplog, monkeypatch):
    # A failed pre-modification .bak write must be surfaced (audit: no silent
    # drops), while the trust write still completes. Monkeypatch the write rather
    # than chmod — Windows ignores a dir's write bit, and that cell is merge-blocking.
    import pathlib

    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"projects": {}}), encoding="utf-8")
    target = tmp_path / "proj"
    target.mkdir()
    real_write_text = pathlib.Path.write_text

    def boom(self, *args, **kwargs):
        if self.suffix == ".bak":
            raise OSError("simulated: backup write failed")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    with caplog.at_level("WARNING", logger="clauster.trust"):
        trust.trust_directory(target, cj)
    assert trust.is_trusted(target, cj) is True  # trust write still succeeded
    assert not cj.with_suffix(cj.suffix + ".bak").exists()  # backup genuinely failed
    assert any("backup" in r.message for r in caplog.records)  # surfaced, not silent
