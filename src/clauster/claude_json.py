"""Shared locked read-modify-write of ``~/.claude.json`` (the trust/config-write primitive).

The ``claude`` CLI, the workspace-trust writer (:mod:`clauster.trust`), and the
config-write trust tier (#347/#687) all read-modify-write the **same**
``~/.claude.json``. This module owns the one hardened machinery they share so a
second, subtly-different JSON writer never gets hand-rolled:

* **Atomic replace** (unique ``mkstemp`` temp + ``os.replace``) so no reader ever
  sees a half-written file, and every key clauster doesn't touch is preserved.
* **An advisory ``flock``** (:func:`_locked`) held across the whole
  read-modify-write, so the read and the replace are one critical section.
  Without it two writers can interleave read → read → write → write and the
  second silently clobbers the first's change (a lost update). The lock fully
  serializes clauster's own concurrent writers and shrinks the window against the
  CLI to the near-zero gap between our read-under-lock and replace. (The CLI does
  not take this lock, so it cannot be eliminated entirely — but the atomic
  replace keeps even a lost update from corrupting the file.)
* **A one-time ``.bak``** taken before the first modification.

This was factored verbatim out of :mod:`clauster.trust` (behavior-preserving) so
both trust and config-write build their per-operation mutation on the identical
primitive. :func:`update_claude_json` is the subtree-merge entry: a caller mutates
only its own subtree in place and every other key is preserved — never serialize a
whole-file blob over the top.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

try:
    import fcntl  # POSIX only; Windows has no flock equivalent we rely on here.
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]

_log = logging.getLogger("clauster.claude_json")


def _is_posix() -> bool:
    """Return whether POSIX file-mode semantics apply (False on Windows).

    A seam over ``os.name == "posix"`` so the non-POSIX atomic-write branch is testable on
    a POSIX host without monkeypatching ``os.name`` itself (which would break ``tempfile``).
    """
    return os.name == "posix"


@contextlib.contextmanager
def _locked(claude_json: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for a read-modify-write of ``claude_json``.

    Uses a sidecar ``<file>.lock`` (never the target itself — ``os.replace``
    swaps the inode, which would orphan a lock held on the old one). POSIX-only;
    where ``fcntl`` is unavailable (Windows) or the lock file can't be opened,
    this degrades to a best-effort no-op rather than blocking the write — the
    atomic replace still prevents a torn file.
    """
    if fcntl is None:
        yield
        return
    lock_path = claude_json.with_suffix(claude_json.suffix + ".lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        _log.warning("could not open %s; proceeding without a lock: %s", lock_path, exc)
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # implicitly releases the flock


def _read_claude_json(claude_json: Path) -> tuple[str | None, dict]:
    """Return ``(raw_text, parsed_dict)`` for ``claude_json``.

    ``raw_text`` is the verbatim on-disk content (for the one-time backup) or
    ``None`` when the file is missing/unreadable/corrupt — matching the prior
    behavior of skipping the backup in those cases. The parsed value is always a
    dict (a valid-JSON non-object root is coerced to ``{}``).
    """
    try:
        raw = claude_json.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        # Missing file or unparseable JSON → start from empty state (no backup).
        # A broader OSError (e.g. PermissionError on a file that *does* exist) is
        # deliberately NOT caught: swallowing it would treat a readable-but-failing
        # file as empty and then replace it with only the keys we touch, silently
        # dropping every other Claude setting. Let it propagate to the caller.
        return None, {}
    return raw, data if isinstance(data, dict) else {}


def _atomic_write_claude_json(claude_json: Path, raw: str | None, data: dict) -> None:
    """Atomically replace ``claude_json`` with ``data`` rendered as ``json.dumps(indent=2)``.

    A thin wrapper over :func:`_atomic_write_json` that fixes the ``~/.claude.json`` render;
    must be called inside :func:`_locked`.
    """
    _atomic_write_json(claude_json, raw, data, lambda d: json.dumps(d, indent=2))


def update_claude_json(claude_json: Path, mutate: Callable[[dict], object]) -> bool:
    """Locked read → ``mutate(data)`` → atomic replace; return whether a write landed.

    The one transaction wrapper trust and config-write share. ``mutate`` receives
    the parsed dict and edits **only its own subtree in place** (every other key is
    preserved by the atomic replace). It may signal "nothing to write" by returning
    ``False`` (e.g. an idempotent flag already set) — the write is then skipped and
    this returns ``False``. Any other return value (including ``None``) is treated
    as "changed" and the atomic write proceeds, returning ``True``. Read errors
    other than missing/corrupt (e.g. ``PermissionError``) propagate, never silently
    emptying the file.
    """
    with _locked(claude_json):
        raw, data = _read_claude_json(claude_json)
        if mutate(data) is False:
            return False
        _atomic_write_claude_json(claude_json, raw, data)
        return True


def locked_replace_json_file(
    path: Path, mutate: Callable[[bytes], dict], *, render: Callable[[dict], str]
) -> None:
    """Locked read-modify-write of an arbitrary JSON ``path`` (the project-file primitive).

    The project-scope twin of :func:`update_claude_json` for files that are *not*
    ``~/.claude.json`` (e.g. a project ``.mcp.json``). It reuses the identical
    hardened machinery — the advisory ``flock`` held across the whole transaction,
    the one-time ``.bak``, and the unique-``mkstemp`` + mode-preserving + atomic
    ``os.replace`` — so no second, subtly-weaker writer is hand-rolled.

    ``mutate`` receives the **verbatim current bytes** read under the lock (``b""``
    when absent) and returns the new full object to render; doing the read inside the
    lock lets the caller run an external-edit / stale-hash check against those exact
    bytes with the read and the replace as one critical section (no TOCTOU window a
    second clauster writer could slip through). ``render`` serializes the returned
    object to text. ``mutate`` may raise to abort the whole transaction before any
    write (the lock is released, nothing is replaced).
    """
    with _locked(path):
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = b""
        data = mutate(current)
        raw = current.decode("utf-8") if current else None
        _atomic_write_json(path, raw, data, render)


def _atomic_write_json(
    path: Path, raw: str | None, data: dict, render: Callable[[dict], str]
) -> None:
    """Back up ``raw`` once, then atomically replace ``path`` with ``render(data)``.

    The render-agnostic core shared by :func:`_atomic_write_claude_json` and
    :func:`locked_replace_json_file`. Must be called inside :func:`_locked`. The backup is
    best-effort: a failure is surfaced via a warning (never a silent drop) but doesn't block
    the write.

    The temp file is uniquely named (``mkstemp``), not a shared ``<file>.tmp``: when
    :func:`_locked` degrades to a no-op (Windows / lock-open failure) two writers can run
    concurrently, and a single fixed temp name lets them stomp each other's inode —
    corrupting the write or failing the second ``os.replace`` with ``FileNotFoundError``. A
    per-write temp keeps each replace atomic regardless of the lock; ``mkstemp`` in the
    target's directory keeps the replace on one filesystem. Existing file permissions are
    mirrored on POSIX so the atomic replace can't silently re-permission a token-bearing file
    (a new file keeps ``mkstemp``'s 0600); on Windows, POSIX mode bits are meaningless (ACLs).
    """
    if raw is not None:
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            try:
                backup.write_text(raw, encoding="utf-8")
            except OSError as exc:
                _log.warning("could not write %s backup: %s", backup, exc)

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(render(data))
            if _is_posix():
                try:
                    mode = stat.S_IMODE(path.stat().st_mode)
                except FileNotFoundError:
                    mode = 0o600
                os.fchmod(fh.fileno(), mode)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
