"""Atomic, owner-only file writes for the state directory.

Shared by the bridge/hosted state stores and the auth epoch writer. Every state
file lives under a directory that also holds ``session.secret``/``session.epoch``,
so the write path here:

* tightens the containing directory to ``0700`` even if it already existed (a bare
  ``mkdir(mode=0o700)`` only sets the mode on *creation*, so a pre-existing,
  looser dir would otherwise keep its perms while holding the secret) — and on
  Windows, where ``chmod`` is a no-op, sets an explicit owner-only ACL best-effort
  (see :func:`ensure_private_dir` / :func:`_restrict_windows_acl`);
* writes through a UNIQUE temp file (``mkstemp``, mode ``0600``) so a reader never
  sees a partial write and two concurrent writers can't clobber one fixed ``.tmp``;
* ``flush`` + ``fsync`` the temp before ``os.replace`` and best-effort ``fsync`` the
  directory after, so a crash can't leave an empty/half-written target.

Two cross-OS helpers here are shared by the JSON writers too (#914):
:func:`inproc_path_lock` serializes THIS process's concurrent same-file writers on
every OS (POSIX ``fcntl.flock`` doesn't cover a second thread reliably on Windows —
it isn't available there at all), and :func:`replace_with_retry` retries the atomic
rename over a transient Windows sharing violation.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# In-process serialization of same-file read-modify-writes (#914). `fcntl.flock` is
# POSIX-only, so on Windows two of THIS process's concurrent config writers would race
# (lost update). A per-path lock closes that on every OS; it complements the POSIX flock
# (which also guards *other processes*) and the atomic-replace + external-edit hash guard
# (which handle *other applications* like the `claude` CLI). Keyed by normalized abs path;
# the registry stays tiny (a handful of state/config files) so it never needs eviction.
_INPROC_LOCKS: dict[str, threading.Lock] = {}
_INPROC_REGISTRY_LOCK = threading.Lock()

# Once-per-process cache of state dirs already ACL-secured on Windows (#914):
# `ensure_private_dir` runs on every write and `icacls` is a subprocess, so only the
# first touch of a given directory shells out.
_SECURED_DIRS: set[str] = set()
_SECURED_DIRS_LOCK = threading.Lock()

#: SYSTEM's well-known, language-neutral SID (icacls grants use it so a non-English
#: Windows doesn't break on a localized "SYSTEM" account name).
_WIN_SYSTEM_SID = "*S-1-5-18"


def _is_windows() -> bool:
    """Return whether Windows file semantics apply (seam: the ACL branch is testable on POSIX)."""
    return os.name == "nt"


def _lock_key(path: Path) -> str:
    """Normalize ``path`` to a stable per-file key (realpath + case-folded on Windows).

    Resolving symlinks + ``..`` means the same file reached via different or symlinked path
    forms always maps to ONE lock / cache entry — else two forms would get two locks and fail
    to serialize.
    """
    return os.path.normcase(os.path.realpath(str(path)))


def inproc_path_lock(path: Path) -> threading.Lock:
    """Return this process's lock for ``path``, serializing our own same-file writers.

    The primary serialization on Windows (where `fcntl.flock` is unavailable) and a
    belt-and-suspenders complement to the POSIX flock elsewhere. Held across a whole
    read-modify-write so the read and the replace are one critical section within the
    process. Non-reentrant by design — the writers never nest a lock on the same path.
    """
    key = _lock_key(path)
    with _INPROC_REGISTRY_LOCK:
        lock = _INPROC_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _INPROC_LOCKS[key] = lock
        return lock


def replace_with_retry(src: Path, dst: Path, *, attempts: int = 5, delay: float = 0.05) -> None:
    """``os.replace(src, dst)`` with a bounded retry over a transient Windows sharing violation.

    On Windows ``os.replace`` raises ``PermissionError`` (``ERROR_SHARING_VIOLATION``) when
    another process holds ``dst`` open — e.g. the ``claude`` CLI reading ``~/.claude.json``,
    or an AV scanner touching a freshly written file — where POSIX ``rename`` has no such
    constraint. Retry a few times with a short backoff, then re-raise so a genuine failure
    still surfaces (never a silent drop). Effectively a no-op on POSIX (the first try wins).
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")  # else the loop is a silent no-write
    for attempt in range(attempts):  # pragma: no branch - always returns or (last try) raises
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _restrict_windows_acl(path: Path) -> None:
    """Best-effort owner-only ACL on ``path`` (Windows), the analogue of POSIX ``0700``.

    ``chmod`` is a no-op on Windows, so this uses built-in ``icacls`` to remove inheritance
    and grant Full control to only the current user and SYSTEM — the analogue of ``0700`` for
    the state dir that holds ``session.secret`` (the auth-signing key).

    **Best-effort, not fail-closed:** the default ``state_dir`` under ``%USERPROFILE%`` already
    inherits a user + SYSTEM + Administrators ACL (never world-readable), so this only strips
    inheritance as defense-in-depth. If ``icacls`` can't run — absent from ``PATH``, no
    ``USERNAME`` to name the grantee (a domain / service account resolves to a short or empty
    name), or a non-zero exit — we log a loud WARNING and proceed on the inherited ACL rather
    than block every state write, which would brick an otherwise-valid Windows install. Attempted
    once per directory per process regardless of outcome (``icacls`` is a subprocess and this
    runs on every write), so a host without a working ``icacls`` doesn't re-shell or re-warn.
    """
    key = _lock_key(path)
    with _SECURED_DIRS_LOCK:
        if key in _SECURED_DIRS:
            return
    reason = _apply_owner_only_acl(path)
    if reason is not None:
        logger.warning(
            "could not set an owner-only ACL on %s (%s) — relying on its inherited ACL; if "
            "state_dir is outside your user profile, tighten its permissions manually",
            path,
            reason,
        )
    with _SECURED_DIRS_LOCK:
        _SECURED_DIRS.add(key)


def _apply_owner_only_acl(path: Path) -> str | None:
    """Grant owner-only Full control on ``path`` via ``icacls``; return an error reason or None.

    Returns ``None`` on success, or a short human-readable reason string on any failure
    (caller logs it). Split out from :func:`_restrict_windows_acl` so the caching + warning
    policy is testable apart from the subprocess mechanics.
    """
    icacls = shutil.which("icacls")
    if icacls is None:
        return "icacls not found on PATH"
    user = os.environ.get("USERNAME")
    if not user:
        return "USERNAME is unset, cannot name the ACL grantee"
    argv = [
        icacls,
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"{user}:(OI)(CI)F",
        "/grant:r",
        f"{_WIN_SYSTEM_SID}:(OI)(CI)F",
    ]
    try:  # noqa: S603 — absolute icacls, list-argv, no shell
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        # subprocess.SubprocessError covers TimeoutExpired (a hung/wedged icacls hitting the
        # 30s cap) — which is NOT an OSError, so catching only OSError would let the timeout
        # escape and fail the whole state write, breaking the best-effort contract. Any
        # subprocess-layer failure degrades to a reason string → warn + proceed on the
        # inherited ACL, never a raised write.
        return f"icacls failed to run: {exc}"
    if proc.returncode != 0:
        return f"icacls exited {proc.returncode}: {proc.stderr.strip()}"
    return None


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` (and parents) if absent and tighten it to owner-only.

    ``mkdir``'s ``mode`` only applies when the directory is created, so this also
    re-tightens an already-existing directory — the state dir holds the session secret
    and must stay owner-only. On POSIX a ``chmod`` failure is raised (fail closed: we
    must not store the secret under a dir we can't secure). On Windows, where ``chmod``
    is a no-op, an explicit owner-only ACL is set instead (:func:`_restrict_windows_acl`),
    best-effort — the default state dir already inherits a private ACL, so a failed
    tightening warns rather than blocking every write (see that function).
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_windows():
        _restrict_windows_acl(path)
        return
    try:
        path.chmod(0o700)
    except OSError:
        # A chmod failure would leave a pre-existing dir that holds the session secret
        # potentially too permissive — fail closed rather than silently degrade.
        raise


def atomic_write_text(target: Path, text: str) -> None:
    r"""Atomically and durably write ``text`` to ``target`` at mode ``0600``.

    Writes a unique temp file in ``target``'s directory, ``fsync``s it, then
    ``os.replace``s it onto ``target`` — a reader never observes a partial file and
    a crash can't leave an empty target. The temp is removed if anything fails
    before the rename. The directory is ensured present + owner-only first.

    ``newline="\n"`` keeps the on-disk bytes identical across OSes (the default would
    translate ``\n`` to ``\r\n`` on Windows, #914). The replace retries over a transient
    Windows sharing violation (:func:`replace_with_retry`).
    """
    directory = target.parent
    ensure_private_dir(directory)
    # mkstemp creates the file mode 0600 and returns a unique name in `directory`,
    # so os.replace is a same-filesystem rename and concurrent writers don't collide.
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=target.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        # If fdopen raises at the open(2)/FileIO level (EMFILE/ENFILE under fd-table
        # pressure) the fd was NOT adopted, so the raw mkstemp fd would leak — close it.
        # The close is guarded: in the rarer case where FileIO adopted the fd before a
        # later wrapper stage raised, the fd is already closed, and an unguarded
        # os.close would raise EBADF and mask the original error.
        try:
            fh = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        replace_with_retry(tmp, target)
    except BaseException:
        # BaseException (not just Exception) so a KeyboardInterrupt/SystemExit mid-write
        # still removes the unique temp. We re-raise immediately — never a swallow.
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(directory)


def fsync_dir(directory: Path) -> None:
    """Best-effort ``fsync`` of a directory so a create/rename in it is durable.

    On POSIX, ``fsync``-ing a file persists its data but not the directory entry
    that links its name in — a crash can still drop a freshly created file. Call
    this on the parent after creating or replacing a file there. A no-op where
    directory fsync isn't supported (e.g. Windows, where opening a directory
    fails) — durability is then the filesystem's to keep (NTFS journals the
    metadata, so a rename is recovered on reboot without an explicit dir fsync).
    """
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
