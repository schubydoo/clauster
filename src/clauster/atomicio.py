"""Atomic, owner-only file writes for the state directory.

Shared by the bridge/hosted state stores and the auth epoch writer. Every state
file lives under a directory that also holds ``session.secret``/``session.epoch``,
so the write path here:

* tightens the containing directory to ``0700`` even if it already existed (a bare
  ``mkdir(mode=0o700)`` only sets the mode on *creation*, so a pre-existing,
  looser dir would otherwise keep its perms while holding the secret);
* writes through a UNIQUE temp file (``mkstemp``, mode ``0600``) so a reader never
  sees a partial write and two concurrent writers can't clobber one fixed ``.tmp``;
* ``flush`` + ``fsync`` the temp before ``os.replace`` and best-effort ``fsync`` the
  directory after, so a crash can't leave an empty/half-written target.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` (and parents) if absent and tighten it to ``0700``.

    ``mkdir``'s ``mode`` only applies when the directory is created, so this also
    ``chmod``s an already-existing directory — the state dir holds the session
    secret and must stay owner-only. On POSIX a ``chmod`` failure is raised (fail
    closed: we must not store the secret under a dir we can't secure); on Windows,
    which has no POSIX mode bits, the ``chmod`` is a no-op that is ignored.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        # On POSIX a chmod failure would leave a pre-existing dir that holds the session
        # secret potentially too permissive — fail closed rather than silently degrade.
        # Windows has no POSIX mode bits, so its chmod is a no-op we can safely ignore.
        if os.name != "nt":
            raise


def atomic_write_text(target: Path, text: str) -> None:
    """Atomically and durably write ``text`` to ``target`` at mode ``0600``.

    Writes a unique temp file in ``target``'s directory, ``fsync``s it, then
    ``os.replace``s it onto ``target`` — a reader never observes a partial file and
    a crash can't leave an empty target. The temp is removed if anything fails
    before the rename. The directory is ensured present + ``0700`` first.
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
            fh = os.fdopen(fd, "w", encoding="utf-8")
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
        os.replace(tmp, target)
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
    fails) — durability is then the filesystem's to keep.
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
