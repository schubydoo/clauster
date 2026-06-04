from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from clauster import trust

# The advisory flock is POSIX-only; on Windows `trust.fcntl` is None and the
# lock degrades to a no-op, so the serialization/lockfile-path tests don't apply.
needs_fcntl = pytest.mark.skipif(
    trust.fcntl is None, reason="advisory flock is POSIX-only (no fcntl)"
)
# fchmod-based mode preservation is POSIX-only (Windows permissions are ACL-based).
needs_fchmod = pytest.mark.skipif(not hasattr(os, "fchmod"), reason="fchmod is POSIX-only")


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


@needs_fcntl
def test_locked_serializes_concurrent_writers(tmp_path: Path, monkeypatch):
    # The lost-update fix: the flock makes read-modify-write a single critical
    # section, so two clauster threads can never both be inside it at once (which
    # is how the second writer would clobber the first's change). We slow the
    # read and assert the in-flight count never exceeds 1.
    cj = tmp_path / "claude.json"
    cj.write_text("{}", encoding="utf-8")
    target = tmp_path / "proj"
    target.mkdir()

    counter_lock = threading.Lock()
    active = 0
    max_active = 0
    real_read = trust._read_claude_json

    def slow_read(path):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)  # widen the window an unlocked RMW would overlap in
            return real_read(path)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(trust, "_read_claude_json", slow_read)

    threads = [threading.Thread(target=trust.trust_directory, args=(target, cj)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_active == 1  # flock fully serialized the writers
    assert trust.is_trusted(target, cj) is True
    json.loads(cj.read_text(encoding="utf-8"))  # never left half-written


def test_locked_noop_without_fcntl(tmp_path: Path, monkeypatch):
    # On a platform without fcntl (Windows) the lock degrades to a no-op: the
    # write still completes and no .lock sidecar is created.
    monkeypatch.setattr(trust, "fcntl", None)
    cj = tmp_path / "claude.json"
    cj.write_text("{}", encoding="utf-8")
    target = tmp_path / "proj"
    target.mkdir()

    trust.trust_directory(target, cj)

    assert trust.is_trusted(target, cj) is True
    assert not cj.with_suffix(cj.suffix + ".lock").exists()


@needs_fcntl
def test_locked_lockfile_open_failure_is_best_effort(tmp_path: Path, monkeypatch, caplog):
    # If the .lock sidecar can't be opened, never block the write — proceed
    # unlocked (atomic replace still protects the file) and surface a warning.
    cj = tmp_path / "claude.json"
    cj.write_text("{}", encoding="utf-8")
    target = tmp_path / "proj"
    target.mkdir()
    real_open = os.open

    def boom(path, *args, **kwargs):
        if str(path).endswith(".lock"):
            raise OSError("simulated: cannot open lock file")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(trust.os, "open", boom)
    with caplog.at_level("WARNING", logger="clauster.trust"):
        trust.trust_directory(target, cj)

    assert trust.is_trusted(target, cj) is True  # write still completed
    assert any("without a lock" in r.message for r in caplog.records)


def test_unreadable_file_propagates_not_clobbered(tmp_path: Path, monkeypatch):
    # A present-but-unreadable ~/.claude.json (e.g. PermissionError) must NOT be
    # treated as empty and overwritten with only the touched keys — that would
    # silently drop every other Claude setting. The read error propagates instead.
    cj = tmp_path / "claude.json"
    original = json.dumps({"projects": {"/keep": {"hasTrustDialogAccepted": True}}, "misc": 7})
    cj.write_text(original, encoding="utf-8")
    target = tmp_path / "proj"
    target.mkdir()

    import pathlib

    real_read_text = pathlib.Path.read_text

    def boom(self, *args, **kwargs):
        if self.name == "claude.json":
            raise PermissionError("simulated: cannot read claude.json")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", boom)

    with pytest.raises(PermissionError):
        trust.trust_directory(target, cj)

    monkeypatch.undo()
    assert cj.read_text(encoding="utf-8") == original  # untouched — nothing clobbered


def test_atomic_write_uses_unique_temp_not_fixed_name(tmp_path: Path):
    # The write must not leave (or depend on) a single shared `<file>.tmp`; a
    # per-write temp is what keeps concurrent unlocked writers from stomping each
    # other. After a successful write, no fixed-name temp lingers.
    cj = tmp_path / "claude.json"
    cj.write_text("{}", encoding="utf-8")
    target = tmp_path / "proj"
    target.mkdir()

    trust.trust_directory(target, cj)

    assert not cj.with_suffix(cj.suffix + ".tmp").exists()  # no fixed-name temp
    assert trust.is_trusted(target, cj) is True
    leftover = list(tmp_path.glob("claude.json.*.tmp"))
    assert leftover == []  # unique temp was consumed by os.replace, none left behind


def test_concurrent_writers_without_lock_keep_valid_json(tmp_path: Path, monkeypatch):
    # With the lock degraded to a no-op (fcntl absent), the unique-temp write still
    # never leaves the file half-written: every concurrent writer replaces atomically.
    monkeypatch.setattr(trust, "fcntl", None)
    cj = tmp_path / "claude.json"
    cj.write_text("{}", encoding="utf-8")
    targets = [tmp_path / f"proj{i}" for i in range(6)]
    for t in targets:
        t.mkdir()

    threads = [threading.Thread(target=trust.trust_directory, args=(t, cj)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    json.loads(cj.read_text(encoding="utf-8"))  # always valid JSON, never torn
    assert list(tmp_path.glob("claude.json.*.tmp")) == []  # no temp debris


@needs_fchmod
def test_atomic_write_preserves_existing_file_mode(tmp_path: Path):
    # mkstemp creates the temp 0600; the replace must NOT silently tighten an
    # existing ~/.claude.json — its mode is mirrored onto the temp first.
    cj = tmp_path / "claude.json"
    cj.write_text("{}", encoding="utf-8")
    cj.chmod(0o644)
    target = tmp_path / "proj"
    target.mkdir()

    trust.trust_directory(target, cj)

    assert stat.S_IMODE(cj.stat().st_mode) == 0o644  # preserved, not reset to 0600


@needs_fchmod
def test_atomic_write_new_file_is_owner_only(tmp_path: Path):
    # A brand-new ~/.claude.json (the file didn't exist) is created owner-only —
    # it can hold tokens, so 0600 is the safe default rather than the umask.
    cj = tmp_path / "claude.json"  # does not exist
    target = tmp_path / "proj"
    target.mkdir()

    trust.trust_directory(target, cj)

    assert stat.S_IMODE(cj.stat().st_mode) == 0o600
