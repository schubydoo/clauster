from __future__ import annotations

import json
from pathlib import Path

from clauster import trust


def test_trust_directory_sets_flag_and_preserves_other_keys(tmp_path: Path):
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"projects": {"/other": {"hasTrustDialogAccepted": True}}, "misc": 1}))
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
