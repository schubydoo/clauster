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

Cross-OS helpers here are shared by the JSON writers too (#914):
:func:`inproc_path_lock` serializes THIS process's concurrent same-file writers on
every OS (POSIX ``fcntl.flock`` doesn't cover a second thread reliably on Windows —
it isn't available there at all), and :func:`replace_with_retry` retries the atomic
rename over a transient Windows sharing violation.

:func:`cross_process_lock` (follow-up to #915) adds the missing *cross-process* guard
for config/CLAUDE.md writes: both the CLAUDE.md editor and the config-write path hold
its ``flock`` on a lock file KEYED BY THE TARGET but living in the deployment state dir
(:func:`configure_lock_dir`), not beside the target — so two clauster processes editing
the same ``<project>/CLAUDE.md`` mutually exclude without littering the project dir with
a visible ``CLAUDE.md.lock``. Since #949 the bridge lifecycle uses the same primitive:
``SessionRunner`` holds a per-project ``cross_process_lock`` (keyed by the project
directory) across its spawn/stop/forget/adopt sections, so a headless CLI/MCP writer
and the running web app mutually exclude their read-modify-writes of the shared
instance store and can't double-launch a standard bridge.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

try:
    import fcntl  # POSIX only; Windows has no flock equivalent we rely on here.
except ImportError:
    fcntl = None  # type: ignore[assignment]

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

# Cross-PROCESS serialization of config/CLAUDE.md read-modify-writes (#915; see the module
# docstring for why the lock file lives here rather than beside the target).
# `configure_lock_dir` is called once in `create_app`; until then the flock is skipped
# (the in-process lock still holds) and a WARNING fires once so the degrade is never silent.
_LOCK_DIR: Path | None = None
_LOCK_DIR_LOCK = threading.Lock()
_CROSS_PROCESS_UNCONFIGURED_WARNED = False

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


def configure_lock_dir(path: Path) -> None:
    """Set the state-dir directory that holds cross-process lock files, owner-only.

    Called once early in ``create_app`` (before any request can write) with
    ``<state_dir>/locks`` so the flock in :func:`cross_process_lock` lands in the
    deployment state dir, never beside the target. The dir is created owner-only via
    :func:`ensure_private_dir` (the lock files hold no secrets, but the state dir must
    stay ``0700`` regardless). Idempotent — safe to call again with the same path.

    The cross-process guarantee is scoped to ONE deployment's state dir: two clauster
    deployments with *different* state dirs sharing a ``projects_root`` would derive the
    same digest under different dirs and NOT mutually exclude. That's acceptable — clauster
    is single-operator / single-deployment by design — but it is a narrowing vs a sidecar
    beside the target, which any two processes touching that file would have shared.
    """
    global _LOCK_DIR
    resolved = path.expanduser()
    ensure_private_dir(resolved)
    with _LOCK_DIR_LOCK:
        _LOCK_DIR = resolved


def _cross_process_lock_file(target: Path, lock_dir: Path | None = None) -> Path | None:
    """Return the state-dir lock-file path for ``target``, or ``None`` if unconfigured.

    Keyed by the SAME normalized realpath (:func:`_lock_key`) both write paths use, so
    the CLAUDE.md editor's target and the config-write path's target for the project-root
    ``CLAUDE.md`` hash to ONE lock file and therefore mutually exclude. A truncated
    SHA-256 of the key names the file (a stable, filesystem-safe, collision-resistant
    handle) so nothing about the target's path leaks into the lock dir's listing.

    ``lock_dir`` overrides the module-global directory: a caller bound to a specific
    deployment (the bridge lifecycle, #949) passes its own state dir's ``locks/`` so a
    LATER ``configure_lock_dir`` for a different state dir in the same process can't
    silently redirect its lock files away from the ones external processes use.
    """
    if lock_dir is None:
        with _LOCK_DIR_LOCK:
            lock_dir = _LOCK_DIR
    if lock_dir is None:
        return None
    digest = hashlib.sha256(_lock_key(target).encode()).hexdigest()[:32]
    return lock_dir / f"{digest}.lock"


@contextlib.contextmanager
def cross_process_lock(target: Path, *, lock_dir: Path | None = None):
    """Hold an exclusive ``flock`` across processes for a write to ``target``.

    The lock file lives in the configured state dir (:func:`configure_lock_dir`), keyed
    by ``target``'s realpath, so two clauster PROCESSES writing the same file serialize
    without a lock artifact appearing beside the target. ``lock_dir`` pins that
    directory per-caller instead (see :func:`_cross_process_lock_file`) — deployment-
    bound callers pass their own so a later global reconfigure can't redirect them.
    Layered UNDER the caller's :func:`inproc_path_lock` (always acquired inproc-first,
    cross-process-second, in that order everywhere) so the two never deadlock; this
    manager acquires no inproc lock.

    Degrades — never silently:

    * ``fcntl`` unavailable (Windows): yield without a flock. The inproc lock is the only
      guard there today, so this is no regression.
    * lock dir not configured (test-only misuse — ``create_app`` always configures it):
      log a WARNING **once** and yield. The inproc lock still serializes this process's
      own writers; only the cross-process guard is missing, and the operator is told.

    The lock file is **never unlinked** — it is created ``O_CREAT`` and left in place. This
    is deliberate: deleting it would reintroduce the classic ``flock``+``unlink`` inode race
    (two processes could end up locking different inodes of a recreated file). The files are
    bounded by the number of distinct config/CLAUDE.md targets ever written (small), so the
    accumulation is a non-issue; a future "cleanup" must NOT prune them.
    """
    if fcntl is None:
        yield
        return
    lock_file = _cross_process_lock_file(target, lock_dir)  # pragma: skip-on-win
    if lock_file is None:  # pragma: skip-on-win
        _warn_cross_process_unconfigured()
        yield
        return
    # Self-heal a vanished lock dir (removed by hand under a running service, or on evicted
    # tmpfs): `exist_ok=True` is a cheap stat in the common case, and `mode=0o700` keeps a
    # recreated `locks/` owner-only. NOT `parents=True` — that would recreate a vanished
    # `state_dir` (the parent, which holds `session.secret` + claustrum tokens) at 0o755,
    # silently un-doing `ensure_private_dir`'s 0o700; instead a missing state_dir raises
    # FileNotFoundError, the right fail-loud outcome (a gone state_dir is a genuine fault).
    # `os.open` likewise stays unwrapped — a real permission/IO fault is worth surfacing.
    lock_file.parent.mkdir(exist_ok=True, mode=0o700)  # pragma: skip-on-win
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)  # pragma: skip-on-win
    try:  # pragma: skip-on-win
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:  # pragma: skip-on-win
        os.close(fd)  # implicitly releases the flock


def _warn_cross_process_unconfigured() -> None:  # pragma: skip-on-win
    """Log the "lock dir not configured" warning at most once per process."""
    global _CROSS_PROCESS_UNCONFIGURED_WARNED
    with _LOCK_DIR_LOCK:
        if _CROSS_PROCESS_UNCONFIGURED_WARNED:
            return
        _CROSS_PROCESS_UNCONFIGURED_WARNED = True
    logger.warning(
        "cross-process file lock dir not configured; config/CLAUDE.md writes and "
        "bridge-lifecycle sections are serialized in-process only"
    )


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
    try:  # pragma: skip-on-win
        path.chmod(0o700)
    except OSError:  # pragma: skip-on-win
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
    try:  # pragma: skip-on-win
        os.fsync(dir_fd)
    except OSError:  # pragma: skip-on-win
        pass
    finally:  # pragma: skip-on-win
        os.close(dir_fd)
