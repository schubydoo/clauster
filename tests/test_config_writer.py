"""Unit tests for the comment-preserving, backup-and-atomic config writer (FE-3, #299)."""

from __future__ import annotations

import sys

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


def test_write_prunes_old_backups_keeping_newest_five(write_config) -> None:
    path = write_config("usage:\n  fx_rate: 1.0\n")
    created: list[str] = []
    for i in range(7):
        before = set(path.parent.glob(path.name + ".bak-*"))
        write_edits(path, {"usage.fx_rate": float(i + 2)})  # no hash check — just churn backups
        created += [p.name for p in path.parent.glob(path.name + ".bak-*") if p not in before]
    surviving = sorted(p.name for p in path.parent.glob(path.name + ".bak-*"))
    # The newest 5 by timestamp are kept (not just any 5).
    assert surviving == sorted(created)[-5:]


def test_write_cleans_temp_file_on_replace_failure(write_config, monkeypatch) -> None:
    import clauster.config_writer as cw

    path = write_config("usage:\n  fx_rate: 1.0\n")

    def _boom(*_a, **_k):
        raise OSError("simulated atomic-replace failure")

    monkeypatch.setattr(cw.os, "replace", _boom)
    with pytest.raises(OSError, match="simulated"):
        write_edits(path, {"usage.fx_rate": 2.0}, expected_hash=file_hash(path))
    # The unique temp file is removed on failure — no orphan left behind.
    assert not list(path.parent.glob(path.name + ".*.tmp"))


def test_concurrent_writes_serialize_exactly_one_wins(write_config) -> None:
    import threading

    path = write_config("usage:\n  fx_rate: 1.0\n")
    h = file_hash(path)
    outcomes: list[str] = []

    def worker(val: float) -> None:
        try:
            write_edits(path, {"usage.fx_rate": val}, expected_hash=h)
            outcomes.append("ok")
        except StaleConfigError:
            outcomes.append("stale")

    threads = [threading.Thread(target=worker, args=(v,)) for v in (2.0, 3.0)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # The lock serializes; the second write sees changed bytes and is rejected stale.
    assert sorted(outcomes) == ["ok", "stale"]


def test_write_restores_on_post_write_parse_failure(write_config, monkeypatch) -> None:
    path = write_config("usage:\n  fx_rate: 1.0\n")
    original = path.read_text(encoding="utf-8")

    def _boom(_p):
        raise ValueError("simulated post-write load failure")

    # The render passes validation but the post-write re-parse fails -> restore + re-raise.
    monkeypatch.setattr("clauster.config_writer.load_config", _boom)
    with pytest.raises(ValueError, match="simulated"):
        write_edits(path, {"usage.fx_rate": 5.0}, expected_hash=file_hash(path))
    assert path.read_text(encoding="utf-8") == original  # rolled back to the pre-write content


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes; Windows has no 0o600")
def test_backup_inherits_source_mode_not_umask(write_config) -> None:
    # A hardened (0600) config holds secret hashes; its `.bak` must inherit that mode,
    # not widen to the process umask default (0644) and leak them to other local
    # users (CFG-1). Force the wide umask so the prior `write_bytes` bug would manifest.
    import os
    import stat

    path = write_config("usage:\n  fx_rate: 1.0\n")
    os.chmod(path, 0o600)
    old_umask = os.umask(0o022)
    try:
        write_edits(path, {"usage.fx_rate": 2.0}, expected_hash=file_hash(path))
    finally:
        os.umask(old_umask)

    backups = list(path.parent.glob(path.name + ".bak-*"))
    assert backups, "expected a backup file"
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600  # not the umask-wide 0o644
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # the rewritten config stays hardened


def test_write_closes_backup_fd_when_fdopen_fails(write_config, monkeypatch) -> None:
    # config_writer.py 141-143: if os.fdopen never takes ownership of the backup fd,
    # the writer must close the raw fd itself (no leak) and re-raise — the config
    # file stays untouched.
    import os

    import clauster.config_writer as cw

    path = write_config("usage:\n  fx_rate: 1.0\n")
    original = path.read_text(encoding="utf-8")

    closed: list[int] = []
    real_close = os.close

    def _spy_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def _boom_fdopen(fd, *a, **k):
        raise RuntimeError("simulated fdopen failure")

    monkeypatch.setattr(cw.os, "fdopen", _boom_fdopen)
    monkeypatch.setattr(cw.os, "close", _spy_close)
    with pytest.raises(RuntimeError, match="simulated fdopen"):
        write_edits(path, {"usage.fx_rate": 2.0}, expected_hash=file_hash(path))
    assert closed, "the backup fd must be closed when fdopen never took ownership"
    assert path.read_text(encoding="utf-8") == original  # the config was never touched
