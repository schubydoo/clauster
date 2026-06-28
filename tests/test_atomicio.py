"""Atomic, owner-only state-file writes (state dir holds the session secret)."""

from __future__ import annotations

import stat
import sys

import pytest

import clauster.atomicio as atomicio
from clauster.atomicio import atomic_write_text, ensure_private_dir

# Perm assertions are POSIX-only; Windows has no 0700/0600 mode bits.
_posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")


def test_atomic_write_text_creates_file_and_parents(tmp_path):
    target = tmp_path / "sub" / "f.json"
    atomic_write_text(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'


def test_atomic_write_text_replaces_existing_leaving_no_temp(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]  # no stray .tmp left


@_posix
def test_atomic_write_text_is_0600_in_a_0700_dir(tmp_path):
    d = tmp_path / "state"
    target = d / "f.json"
    atomic_write_text(target, "x")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


@_posix
def test_ensure_private_dir_tightens_a_preexisting_loose_dir(tmp_path):
    d = tmp_path / "loose"
    d.mkdir()
    d.chmod(0o755)  # a dir that already existed world-readable (mkdir mode wouldn't fix it)
    ensure_private_dir(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_ensure_private_dir_fails_closed_on_posix_chmod_failure(tmp_path, monkeypatch):
    # Fail closed: on POSIX a chmod failure must not silently leave the secret dir loose.
    import pathlib

    def _boom(self, *a, **k):
        raise OSError("chmod denied")

    monkeypatch.setattr(pathlib.Path, "chmod", _boom)
    d = tmp_path / "state"
    if sys.platform == "win32":
        ensure_private_dir(d)  # chmod is a no-op on Windows — must not raise
    else:
        with pytest.raises(OSError, match="chmod denied"):
            ensure_private_dir(d)


def test_atomic_write_text_cleans_up_temp_on_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"

    def _boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(atomicio.os, "replace", _boom)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(target, "data")
    assert list(tmp_path.iterdir()) == []  # the unique temp was removed; no target written


def test_atomic_write_text_cleans_up_temp_on_fsync_failure(tmp_path, monkeypatch):
    # The likely write-path fault (ENOSPC/EIO surfacing at fsync) must remove the temp
    # and propagate — never leave a half-written stray .tmp behind.
    def _boom(fd):
        raise OSError("EIO")

    monkeypatch.setattr(atomicio.os, "fsync", _boom)
    with pytest.raises(OSError, match="EIO"):
        atomic_write_text(tmp_path / "f.txt", "data")
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_text_cleans_up_temp_on_interrupt_mid_write(tmp_path, monkeypatch):
    # A KeyboardInterrupt mid-write is why the cleanup catches BaseException, not just
    # Exception — the temp must still be removed and the interrupt re-raised.
    def _interrupt(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(atomicio.os, "fsync", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        atomic_write_text(tmp_path / "f.txt", "data")
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_text_closes_fd_when_fdopen_fails(tmp_path, monkeypatch):
    # If os.fdopen raises before the `with` adopts the fd (e.g. EMFILE under fd-table
    # pressure), the raw mkstemp fd must be closed, not leaked — and the temp removed.
    captured: dict[str, int] = {}
    real_mkstemp = atomicio.tempfile.mkstemp
    real_close = atomicio.os.close
    closed: list[int] = []

    def _spy_mkstemp(*a, **k):
        fd, name = real_mkstemp(*a, **k)
        captured["fd"] = fd
        return fd, name

    def _boom_fdopen(*a, **k):
        raise OSError("too many open files")

    def _spy_close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(atomicio.tempfile, "mkstemp", _spy_mkstemp)
    monkeypatch.setattr(atomicio.os, "fdopen", _boom_fdopen)
    monkeypatch.setattr(atomicio.os, "close", _spy_close)

    with pytest.raises(OSError, match="too many open files"):
        atomic_write_text(tmp_path / "f.txt", "data")
    assert captured["fd"] in closed  # the raw fd was closed, not leaked
    assert list(tmp_path.iterdir()) == []  # temp removed, target not written


def test_atomic_write_text_fdopen_failure_survives_double_close(tmp_path, monkeypatch):
    # If FileIO adopted+closed the fd before a wrapper stage raised, the guarded os.close
    # hits EBADF — that must be swallowed so the ORIGINAL fdopen error propagates, not the
    # double-close error.
    real_close = atomicio.os.close

    def _boom_fdopen(*a, **k):
        raise OSError("primary fdopen failure")

    def _already_closed(fd):
        # Mimic the adopted-then-wrapper-failed case: the fd is genuinely released
        # (so the temp can be unlinked — Windows can't delete a still-open file), but
        # the second close still signals EBADF, which the production guard must swallow.
        real_close(fd)
        raise OSError("EBADF: fd already closed")

    monkeypatch.setattr(atomicio.os, "fdopen", _boom_fdopen)
    monkeypatch.setattr(atomicio.os, "close", _already_closed)
    with pytest.raises(OSError, match="primary fdopen failure"):
        atomic_write_text(tmp_path / "f.txt", "data")
    assert list(tmp_path.iterdir()) == []  # temp still removed


def test_fsync_dir_ignores_open_error(tmp_path, monkeypatch):
    # A directory whose fd can't be opened for fsync (Windows can't open a dir; or a
    # perm/race) is a no-op — durability of the rename is then the filesystem's to keep.
    def _boom(*a, **k):
        raise OSError("no dir fd")

    monkeypatch.setattr(atomicio.os, "open", _boom)
    atomicio.fsync_dir(tmp_path)  # must not raise


def test_fsync_dir_ignores_fsync_error(tmp_path, monkeypatch):
    # A directory fsync the filesystem rejects (e.g. EINVAL on some FUSE/tmpfs) is
    # swallowed — best-effort durability, never a hard failure.
    def _boom(fd):
        raise OSError("fsync rejected")

    monkeypatch.setattr(atomicio.os, "fsync", _boom)
    atomicio.fsync_dir(tmp_path)  # must not raise


def test_ensure_private_dir_ignores_chmod_failure_on_windows(tmp_path, monkeypatch):
    # On Windows (os.name == "nt") there are no POSIX mode bits, so a chmod failure is a
    # no-op that is ignored — only POSIX fails closed (covered by the test above).
    import pathlib

    def _boom(self, *a, **k):
        raise OSError("no chmod")

    monkeypatch.setattr(atomicio.os, "name", "nt")
    monkeypatch.setattr(pathlib.Path, "chmod", _boom)
    ensure_private_dir(tmp_path / "win")  # simulated Windows: must NOT raise
    assert (tmp_path / "win").exists()  # the primary behavior (mkdir) still happened
