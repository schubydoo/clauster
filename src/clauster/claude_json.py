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
    # The lock-path derivation MUST stay identical to ``clauster.trust._locked`` (its thin
    # copy that resolves fcntl/os from trust's own namespace for monkeypatch tests): the two
    # writers serialize against each other only by both computing this same sidecar path.
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
    """Back up ``raw`` once, then atomically replace ``claude_json`` with ``data``.

    Must be called inside :func:`_locked`. The backup is best-effort: a failure
    is surfaced via a warning (never a silent drop) but does not block the write.

    The temp file is uniquely named (``mkstemp``), not a shared ``<file>.tmp``:
    when :func:`_locked` degrades to a no-op (Windows / lock-open failure) two
    writers can run concurrently, and a single fixed temp name lets them stomp
    each other's inode — corrupting the write or failing the second ``os.replace``
    with ``FileNotFoundError``. A per-write temp keeps each replace atomic
    regardless of the lock. ``mkstemp`` in the target's directory keeps the
    replace on one filesystem (so it stays atomic).
    """
    if raw is not None:
        backup = claude_json.with_suffix(claude_json.suffix + ".bak")
        if not backup.exists():
            try:
                backup.write_text(raw, encoding="utf-8")
            except OSError as exc:
                _log.warning("could not write %s backup: %s", backup, exc)

    fd, tmp_name = tempfile.mkstemp(
        dir=claude_json.parent, prefix=f"{claude_json.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
            # mkstemp makes the temp 0600; mirror the existing file's permissions so
            # the atomic replace doesn't silently re-permission ~/.claude.json. A new
            # file keeps 0600 (it can hold tokens). POSIX-only — on Windows file
            # permissions are ACL-based (stat reports 0o666 regardless), so POSIX mode
            # bits are meaningless and we leave mkstemp's default as-is.
            if os.name == "posix":
                try:
                    mode = stat.S_IMODE(claude_json.stat().st_mode)
                except FileNotFoundError:
                    mode = 0o600
                os.fchmod(fh.fileno(), mode)
        os.replace(tmp, claude_json)
    finally:
        # On the happy path os.replace consumed the temp; this only fires if the
        # write/replace raised before the rename, so the temp never lingers.
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


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
