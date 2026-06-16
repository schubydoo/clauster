"""Unit tests for the comment-preserving, backup-and-atomic config writer (FE-3, #299)."""

from __future__ import annotations

import pytest

from clauster.config import load_config
from clauster.config_editor import (
    ConfigValidationError,
    DisallowedFieldError,
    StaleConfigError,
    file_hash,
)
from clauster.config_writer import write_edits


def test_write_persists_tier_a_change_and_preserves_comments(write_config) -> None:
    path = write_config("usage:\n  fx_rate: 1.0  # keep me\nmetrics:\n  show_disk: true\n")
    before = file_hash(path)

    new_hash = write_edits(path, {"usage.fx_rate": 3.0}, expected_hash=before)

    assert new_hash != before
    reloaded = load_config(path)
    assert reloaded.usage.fx_rate == 3.0
    # ruamel round-trip keeps the operator's inline comment.
    assert "# keep me" in path.read_text(encoding="utf-8")
    # a timestamped backup of the prior content exists.
    backups = list(path.parent.glob(path.name + ".bak-*"))
    assert backups, "expected a backup file"


def test_write_rejects_stale_hash_without_touching_file(write_config) -> None:
    path = write_config("usage:\n  fx_rate: 1.0\n")
    original = path.read_text(encoding="utf-8")
    with pytest.raises(StaleConfigError):
        write_edits(path, {"usage.fx_rate": 9.0}, expected_hash="deadbeef")
    assert path.read_text(encoding="utf-8") == original
    assert not list(path.parent.glob(path.name + ".bak-*"))


def test_write_rejects_disallowed_field(write_config) -> None:
    path = write_config("")
    original = path.read_text(encoding="utf-8")
    with pytest.raises(DisallowedFieldError):
        write_edits(path, {"auth.enabled": False}, expected_hash=file_hash(path))
    assert path.read_text(encoding="utf-8") == original


def test_write_rejects_invalid_value_without_writing(write_config) -> None:
    path = write_config("")
    original = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        write_edits(path, {"instance_defaults.capacity": 0}, expected_hash=file_hash(path))
    assert path.read_text(encoding="utf-8") == original
    assert not list(path.parent.glob(path.name + ".bak-*"))


def test_write_creates_missing_section(write_config) -> None:
    # No `logs:` block in the file — the writer creates it.
    path = write_config("")
    write_edits(path, {"logs.keep_rotated": 9}, expected_hash=file_hash(path))
    assert load_config(path).logs.keep_rotated == 9
