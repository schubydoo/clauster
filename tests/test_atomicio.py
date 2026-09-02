"""Atomic, owner-only state-file writes (state dir holds the session secret)."""

from __future__ import annotations

import logging
import shutil
import stat
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import clauster.atomicio as atomicio
from clauster.atomicio import atomic_copy_file, atomic_write_text, ensure_private_dir

# Perm assertions are POSIX-only; Windows has no 0700/0600 mode bits.
_posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")

# cross_process_lock's flock (lock-file creation, the unconfigured warning, the self-heal)
# only runs where fcntl exists; on Windows (fcntl is None) it deliberately no-ops, so tests
# asserting that flock behavior must skip there — keyed on the ACTUAL gate, not sys.platform.
_needs_flock = pytest.mark.skipif(
    atomicio.fcntl is None, reason="cross-process flock is POSIX-only; Windows no-ops it"
)


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


def test_atomic_copy_file_is_byte_exact(tmp_path):
    # Bytes, not text: read_text applies universal newlines on every OS, so a
    # text round-trip would fold CRLF (and a lone CR) to LF. The callers copy a file
    # they have declared unusable, where a stray control byte can be anywhere.
    blob = b'{"schema_version": 1,\r\n "instances": \r{ oops\x00'
    src = tmp_path / "src.json"
    src.write_bytes(blob)
    atomic_copy_file(src, tmp_path / "copy.json")
    assert (tmp_path / "copy.json").read_bytes() == blob
    assert sorted(f.name for f in tmp_path.iterdir()) == ["copy.json", "src.json"]


def test_atomic_copy_file_copies_a_file_that_is_not_valid_utf8(tmp_path):
    # The reason it is a byte copy at all: there is no text form of these bytes.
    blob = b"\xff\xfe\x00not utf-8"
    src = tmp_path / "src.json"
    src.write_bytes(blob)
    atomic_copy_file(src, tmp_path / "copy.json")
    assert (tmp_path / "copy.json").read_bytes() == blob


@_posix
def test_atomic_copy_file_is_0600_even_from_a_permissive_source(tmp_path):
    # shutil.copy2 would propagate the SOURCE mode. A legacy state file predating the
    # atomic writer, or one restored through ops._safe_extract_tar (a bare open), sits
    # at the umask default, and a copy of the hosted store carries a claude session
    # uuid. The mode must come from the 0600 temp instead.
    src = tmp_path / "src.json"
    src.write_text("{}", encoding="utf-8")
    src.chmod(0o644)
    atomic_copy_file(src, tmp_path / "copy.json")
    mode = (tmp_path / "copy.json").stat().st_mode
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_atomic_copy_file_cleans_up_temp_on_replace_failure(tmp_path, monkeypatch):
    # A real temp exists on disk when the replace raises, so this exercises the
    # cleanup rather than skipping over it.
    src = tmp_path / "src.json"
    src.write_text("data", encoding="utf-8")

    def _boom(*_a, **_k):
        raise OSError("replace failed")

    monkeypatch.setattr(atomicio, "replace_with_retry", _boom)
    with pytest.raises(OSError, match="replace failed"):
        atomic_copy_file(src, tmp_path / "copy.json")
    assert sorted(f.name for f in tmp_path.iterdir()) == ["src.json"]


def test_atomic_copy_file_leaves_no_temp_when_the_source_vanishes(tmp_path, monkeypatch):
    # The temp is opened before the source is, so a source that disappears between the
    # caller's check and the copy must not strand it.
    src = tmp_path / "src.json"
    src.write_text("data", encoding="utf-8")
    real_open = Path.open

    def _vanish(self, *a, **k):
        if self == src:
            raise FileNotFoundError(str(src))
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", _vanish)
    with pytest.raises(FileNotFoundError):
        atomic_copy_file(src, tmp_path / "copy.json")
    assert sorted(f.name for f in tmp_path.iterdir()) == ["src.json"]


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


# --- Windows owner-only ACL (#914): driven on POSIX via the `_is_windows` seam + fake icacls ---


def _fake_icacls(monkeypatch, calls, *, returncode=0, run_error=None, which="icacls"):
    """Wire up a simulated Windows: _is_windows True, a fake icacls, a captured subprocess.run."""
    monkeypatch.setattr(atomicio, "_is_windows", lambda: True)
    monkeypatch.setattr(
        atomicio.shutil,
        "which",
        (lambda name: None) if which is None else (lambda name: f"/x/{name}"),
    )
    monkeypatch.setenv("USERNAME", "someuser")

    def _run(argv, **kwargs):
        calls.append(argv)
        if run_error is not None:
            raise run_error
        return types.SimpleNamespace(returncode=returncode, stderr="access denied", stdout="")

    monkeypatch.setattr(atomicio.subprocess, "run", _run)


def test_ensure_private_dir_windows_sets_owner_only_acl(tmp_path, monkeypatch):
    # On Windows, chmod is a no-op, so ensure_private_dir sets an explicit owner-only ACL via
    # icacls: remove inheritance + grant Full to only the current user and SYSTEM (SID).
    atomicio._SECURED_DIRS.clear()
    calls: list = []
    _fake_icacls(monkeypatch, calls)
    d = tmp_path / "state"
    ensure_private_dir(d)
    assert d.exists()
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0].endswith("icacls")
    assert str(d) in argv
    assert "/inheritance:r" in argv
    assert "someuser:(OI)(CI)F" in argv
    assert "*S-1-5-18:(OI)(CI)F" in argv  # SYSTEM's language-neutral SID


def test_ensure_private_dir_windows_acl_is_cached_per_process(tmp_path, monkeypatch):
    atomicio._SECURED_DIRS.clear()
    calls: list = []
    _fake_icacls(monkeypatch, calls)
    d = tmp_path / "state"
    ensure_private_dir(d)
    ensure_private_dir(d)  # second touch must NOT re-shell to icacls
    assert len(calls) == 1


def test_ensure_private_dir_windows_warns_but_proceeds_on_icacls_nonzero(
    tmp_path, monkeypatch, caplog
):
    # Best-effort (#914): a non-zero icacls exit warns loudly and proceeds on the inherited
    # ACL rather than blocking every write — a fail-closed raise here bricked valid installs.
    atomicio._SECURED_DIRS.clear()
    calls: list = []
    _fake_icacls(monkeypatch, calls, returncode=5)
    d = tmp_path / "state"
    with caplog.at_level(logging.WARNING):
        ensure_private_dir(d)  # must NOT raise
    assert d.exists()
    assert any("owner-only ACL" in r.message for r in caplog.records)
    assert "exited 5" in caplog.text


def test_ensure_private_dir_windows_warns_when_icacls_missing(tmp_path, monkeypatch, caplog):
    atomicio._SECURED_DIRS.clear()
    _fake_icacls(monkeypatch, [], which=None)
    d = tmp_path / "state"
    with caplog.at_level(logging.WARNING):
        ensure_private_dir(d)  # must NOT raise
    assert d.exists()
    assert "icacls not found on PATH" in caplog.text


def test_ensure_private_dir_windows_warns_without_username(tmp_path, monkeypatch, caplog):
    atomicio._SECURED_DIRS.clear()
    _fake_icacls(monkeypatch, [])
    monkeypatch.delenv("USERNAME", raising=False)
    d = tmp_path / "state"
    with caplog.at_level(logging.WARNING):
        ensure_private_dir(d)  # must NOT raise
    assert d.exists()
    assert "USERNAME is unset" in caplog.text


def test_ensure_private_dir_windows_warns_on_icacls_oserror(tmp_path, monkeypatch, caplog):
    atomicio._SECURED_DIRS.clear()
    _fake_icacls(monkeypatch, [], run_error=OSError("spawn failed"))
    d = tmp_path / "state"
    with caplog.at_level(logging.WARNING):
        ensure_private_dir(d)  # must NOT raise
    assert d.exists()
    assert "icacls failed to run" in caplog.text


def test_ensure_private_dir_windows_warns_on_icacls_timeout(tmp_path, monkeypatch, caplog):
    # A hung/wedged icacls hits subprocess.run's 30s timeout → TimeoutExpired, which is a
    # subprocess.SubprocessError, NOT an OSError. Best-effort must still catch it: warn +
    # proceed on the inherited ACL, never let the timeout fail the state write.
    import subprocess

    atomicio._SECURED_DIRS.clear()
    _fake_icacls(monkeypatch, [], run_error=subprocess.TimeoutExpired(cmd="icacls", timeout=30))
    d = tmp_path / "state"
    with caplog.at_level(logging.WARNING):
        ensure_private_dir(d)  # must NOT raise despite the timeout
    assert d.exists()
    assert "icacls failed to run" in caplog.text


def test_ensure_private_dir_windows_failed_acl_attempted_once(tmp_path, monkeypatch, caplog):
    # A host without a working icacls must not re-shell (or re-warn) on every write: the dir
    # is marked attempted after the first touch regardless of outcome.
    atomicio._SECURED_DIRS.clear()
    calls: list = []
    _fake_icacls(monkeypatch, calls, returncode=5)
    d = tmp_path / "state"
    with caplog.at_level(logging.WARNING):
        ensure_private_dir(d)
        ensure_private_dir(d)  # second touch: no re-shell, no second warning
    assert len(calls) == 1
    assert sum("owner-only ACL" in r.message for r in caplog.records) == 1


# --- in-process write lock (#914) ---


def test_inproc_path_lock_same_path_shares_one_lock(tmp_path):
    a = atomicio.inproc_path_lock(tmp_path / "f")
    b = atomicio.inproc_path_lock(tmp_path / "f")
    c = atomicio.inproc_path_lock(tmp_path / "g")
    assert a is b  # same file → same lock (serializes our own writers)
    assert a is not c


def test_inproc_path_lock_serializes_concurrent_writers(tmp_path):
    # With the lock held across a read-modify-write, five racing threads never lose an update.
    target = tmp_path / "counter"
    seen: list[int] = []

    def rmw() -> None:
        with atomicio.inproc_path_lock(target):
            v = seen[-1] if seen else 0
            time.sleep(0.005)  # widen the interleave window
            seen.append(v + 1)

    threads = [threading.Thread(target=rmw) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == [1, 2, 3, 4, 5]  # strictly serialized, no lost update


# --- os.replace retry over a Windows sharing violation (#914) ---


def test_replace_with_retry_succeeds_first_try(tmp_path):
    src = tmp_path / "s"
    src.write_text("x")
    atomicio.replace_with_retry(src, tmp_path / "d")
    assert (tmp_path / "d").read_text() == "x"
    assert not src.exists()


def test_replace_with_retry_retries_permission_error_then_succeeds(tmp_path, monkeypatch):
    src = tmp_path / "s"
    src.write_text("x")
    dst = tmp_path / "d"
    real = atomicio.os.replace
    n = {"c": 0}

    def flaky(s, d):
        n["c"] += 1
        if n["c"] < 3:
            raise PermissionError("sharing violation")
        real(s, d)

    monkeypatch.setattr(atomicio.os, "replace", flaky)
    monkeypatch.setattr(atomicio.time, "sleep", lambda _s: None)
    atomicio.replace_with_retry(src, dst)
    assert n["c"] == 3
    assert dst.read_text() == "x"


def test_replace_with_retry_rejects_nonpositive_attempts(tmp_path):
    # attempts < 1 would run zero iterations = a silent no-write; reject it (never a silent drop).
    with pytest.raises(ValueError, match="attempts must be"):
        atomicio.replace_with_retry(tmp_path / "s", tmp_path / "d", attempts=0)


def test_replace_with_retry_gives_up_and_reraises(tmp_path, monkeypatch):
    monkeypatch.setattr(
        atomicio.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError("locked"))
    )
    monkeypatch.setattr(atomicio.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        atomicio.replace_with_retry(tmp_path / "s", tmp_path / "d", attempts=3)


# --- newline: LF, never CRLF, on any OS (#914) ---


def test_atomic_write_text_writes_lf_not_crlf(tmp_path):
    target = tmp_path / "f.txt"
    atomic_write_text(target, "a\nb\nc\n")
    assert target.read_bytes() == b"a\nb\nc\n"  # byte-identical cross-OS, never \r\n


# --- cross-process lock: state-dir lock file, no target-dir litter (follow-up to #915) ---


def test_configure_lock_dir_creates_owner_only_dir(tmp_path):
    lock_dir = tmp_path / "state" / "locks"
    atomicio.configure_lock_dir(lock_dir)
    assert lock_dir.is_dir()
    if sys.platform != "win32":
        assert stat.S_IMODE(lock_dir.stat().st_mode) == 0o700
    assert atomicio._LOCK_DIR == lock_dir


def test_cross_process_lock_file_none_when_unconfigured(tmp_path):
    # No lock dir set → no lock file (the manager warns + degrades to inproc-only).
    assert atomicio._cross_process_lock_file(tmp_path / "CLAUDE.md") is None


def test_cross_process_lock_file_lives_in_lock_dir_not_target_dir(tmp_path):
    lock_dir = tmp_path / "locks"
    atomicio.configure_lock_dir(lock_dir)
    target = tmp_path / "proj" / "CLAUDE.md"
    lock_file = atomicio._cross_process_lock_file(target)
    assert lock_file is not None
    assert lock_file.parent == lock_dir  # in the state dir, NOT beside the target
    assert lock_file.suffix == ".lock"
    assert lock_file.parent != target.parent


def test_cross_process_lock_file_explicit_lock_dir_skips_the_global(tmp_path):
    # A deployment-bound caller passes its own locks/ dir; the module global must not be
    # consulted at all — a later configure_lock_dir for another state dir can't redirect it.
    explicit = tmp_path / "deployment-locks"
    explicit.mkdir()
    lock_file = atomicio._cross_process_lock_file(tmp_path / "CLAUDE.md", lock_dir=explicit)
    assert lock_file is not None
    assert lock_file.parent == explicit


def test_cross_process_lock_file_same_realpath_shares_one_file(tmp_path):
    # The editor target and the config-write target for the SAME project-root CLAUDE.md
    # both go through `_lock_key` (realpath), so they must hash to ONE lock file — this is
    # what makes the two write paths mutually exclude.
    atomicio.configure_lock_dir(tmp_path / "locks")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")
    editor_target = (proj / "CLAUDE.md").resolve()
    # A different path form (via a symlink to the project dir) that realpaths to the same file.
    link = tmp_path / "link"
    try:
        link.symlink_to(proj, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without symlink priv
        pytest.skip("symlinks unavailable")
    aliased_target = link / "CLAUDE.md"
    assert atomicio._cross_process_lock_file(editor_target) == atomicio._cross_process_lock_file(
        aliased_target
    )


@_needs_flock
def test_cross_process_lock_creates_lock_file_in_state_dir(tmp_path):
    lock_dir = tmp_path / "locks"
    atomicio.configure_lock_dir(lock_dir)
    target = tmp_path / "proj" / "CLAUDE.md"
    with atomicio.cross_process_lock(target):
        pass
    # The lock file lands in the state dir; nothing appears in the target's (absent) dir.
    assert list(lock_dir.glob("*.lock"))
    assert not (target.parent).exists() or not list(target.parent.glob("*.lock"))


@_needs_flock
def test_cross_process_lock_self_heals_vanished_lock_dir(tmp_path):
    # If the configured lock dir (a subdir of state_dir) is removed under a running service
    # (or on evicted tmpfs) while state_dir survives, the next lock recreates it OWNER-ONLY
    # and succeeds — self-heal like main's old sidecar did, not a hard-fail on every write.
    lock_dir = tmp_path / "state" / "locks"
    atomicio.configure_lock_dir(lock_dir)
    shutil.rmtree(lock_dir)  # only `locks/` vanishes; the parent `state/` survives
    assert not lock_dir.exists()
    target = tmp_path / "proj" / "CLAUDE.md"
    with atomicio.cross_process_lock(target):  # must NOT raise — recreates the dir
        pass
    assert lock_dir.is_dir() and list(lock_dir.glob("*.lock"))
    # @_needs_flock only runs on POSIX, so the mode is always meaningful here.
    assert stat.S_IMODE(lock_dir.stat().st_mode) == 0o700  # recreated owner-only


@_needs_flock
def test_cross_process_lock_fails_loud_when_state_dir_gone(tmp_path):
    # A vanished *state_dir* (the lock dir's PARENT — it holds session.secret + tokens) must
    # NOT be silently recreated world-readable via parents=True: the mkdir has no parents=True,
    # so a missing state_dir raises FileNotFoundError (a genuine fault worth surfacing) rather
    # than degrading past — matching the fail-loud-not-open contract.
    lock_dir = tmp_path / "state" / "locks"
    atomicio.configure_lock_dir(lock_dir)
    shutil.rmtree(tmp_path / "state")  # the whole state dir vanishes
    with pytest.raises(FileNotFoundError):
        with atomicio.cross_process_lock(tmp_path / "proj" / "CLAUDE.md"):
            pass


@_needs_flock
def test_cross_process_lock_warns_once_when_unconfigured(tmp_path, caplog):
    # Unconfigured → NEVER silent: warn once, then yield (inproc lock still holds).
    target = tmp_path / "CLAUDE.md"
    with caplog.at_level(logging.WARNING, logger="clauster.atomicio"):
        with atomicio.cross_process_lock(target):
            pass
        with atomicio.cross_process_lock(target):  # second entry must NOT warn again
            pass
    msg = "cross-process file lock dir not configured"
    warnings = [r for r in caplog.records if msg in r.message]
    assert len(warnings) == 1


def test_cross_process_lock_noop_without_fcntl(tmp_path, monkeypatch, caplog):
    # Windows (no fcntl): yield without a flock and WITHOUT the unconfigured warning — the
    # inproc lock is the only guard there today, so this is not a regression.
    monkeypatch.setattr(atomicio, "fcntl", None)
    with caplog.at_level(logging.WARNING, logger="clauster.atomicio"):
        with atomicio.cross_process_lock(tmp_path / "CLAUDE.md"):
            pass
    assert not any("lock dir not configured" in r.message for r in caplog.records)


def test_cross_process_lock_serializes_across_lock_instances(tmp_path):
    # Two threads whose critical sections would overlap are serialized by the flock even
    # though they never touch the inproc lock — the property that makes two PROCESSES mutually
    # exclude. Skipped where fcntl is unavailable.
    if atomicio.fcntl is None:  # pragma: no cover - POSIX-only
        pytest.skip("fcntl unavailable")
    atomicio.configure_lock_dir(tmp_path / "locks")
    target = tmp_path / "proj" / "CLAUDE.md"
    counter_lock = threading.Lock()
    active = 0
    max_active = 0

    def worker() -> None:
        nonlocal active, max_active
        with atomicio.cross_process_lock(target):
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with counter_lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max_active == 1


def test_import_without_fcntl_degrades_module_to_none(monkeypatch):
    # The POSIX-only `import fcntl` guard: on a platform lacking fcntl (Windows) the
    # module still imports and degrades `fcntl` to None. Force the ImportError on this
    # POSIX host by making the real import of `fcntl` raise, reload, and assert the
    # fallback actually fired — then reload again to restore the real module.
    import builtins
    import importlib

    real_import = builtins.__import__

    def no_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("simulated: platform without fcntl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    try:
        importlib.reload(atomicio)
        assert atomicio.fcntl is None
    finally:
        monkeypatch.undo()
        importlib.reload(atomicio)
