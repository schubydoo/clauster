"""Workspace-trust writer (spec §11 RESOLVED + guardrail).

A bridge refuses to spawn in an untrusted directory. Trust lives in
``~/.claude.json`` under ``projects[<resolved-abs-path>].hasTrustDialogAccepted``
and inherits down a tree. Clauster offers an explicit "Trust this directory"
action that sets the flag for exactly one project key.

The ``claude`` CLI writes this same file concurrently. Two layers guard it:

* **Atomic replace** (temp + ``os.replace``) so no reader ever sees a
  half-written file, and every key clauster doesn't touch is preserved.
* **An advisory ``flock``** (:func:`_locked`) held across the whole
  read-modify-write, so the read and the replace are one critical section.
  Without it, two writers can interleave read → read → write → write and the
  second writer silently clobbers the first's change (a lost update). The lock
  fully serializes clauster's own concurrent writers (e.g. a "Trust" click that
  overlaps a spawn's pre-enable) and shrinks the window against the CLI to the
  near-zero gap between our read-under-lock and replace. (The CLI does not take
  this lock, so it cannot be eliminated entirely — but the atomic replace keeps
  even a lost update from corrupting the file.)

A one-time ``.bak`` is taken before the first modification.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

try:
    import fcntl  # POSIX only; Windows has no flock equivalent we rely on here.
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]

from .discovery import CLAUDE_JSON, trust_state_for
from .models import TrustState

_log = logging.getLogger("clauster.trust")


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
        os.replace(tmp, claude_json)
    finally:
        # On the happy path os.replace consumed the temp; this only fires if the
        # write/replace raised before the rename, so the temp never lingers.
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def is_trusted(path: Path, claude_json: Path = CLAUDE_JSON) -> bool:
    """Whether ``path`` (or an ancestor) has accepted the Claude trust dialog."""
    from .discovery import _load_trusted_paths

    return trust_state_for(path, _load_trusted_paths(claude_json)) is TrustState.TRUSTED


def trust_directory(path: Path, claude_json: Path = CLAUDE_JSON) -> None:
    """Atomically set ``hasTrustDialogAccepted=true`` for ``path`` only.

    Reads, mutates one key, and replaces atomically under :func:`_locked` so a
    concurrent CLI writer never sees a half-written file and clauster's own
    overlapping writers can't lose each other's updates. Idempotent.
    """
    resolved = str(path.resolve())

    with _locked(claude_json):
        raw, data = _read_claude_json(claude_json)

        projects = data.get("projects")
        if not isinstance(projects, dict):
            projects = {}
            data["projects"] = projects
        entry = projects.get(resolved)
        if not isinstance(entry, dict):
            entry = {}
            projects[resolved] = entry
        entry["hasTrustDialogAccepted"] = True

        _atomic_write_claude_json(claude_json, raw, data)


# Top-level ~/.claude.json flags that record the operator already acknowledged
# remote control, so `claude remote-control` skips its one-time interactive
# "Enable Remote Control? (y/n)" prompt (which a detached-stdin bridge can never
# answer — empirically verified 2026-05-31).
_REMOTE_CONTROL_FLAGS = ("hasUsedRemoteControl", "remoteDialogSeen")


def ensure_remote_control_enabled(claude_json: Path = CLAUDE_JSON) -> bool:
    """Atomically set the remote-control acknowledgment flags in ``claude_json``.

    Returns True if the file was changed, False if the flags were already set (or
    the file couldn't be read). Same locked, atomic temp+replace + one-time
    ``.bak`` as :func:`trust_directory`, since the ``claude`` CLI writes this file
    too. Idempotent.
    """
    with _locked(claude_json):
        raw, data = _read_claude_json(claude_json)

        if all(data.get(flag) is True for flag in _REMOTE_CONTROL_FLAGS):
            return False  # already acknowledged — nothing to do

        for flag in _REMOTE_CONTROL_FLAGS:
            data[flag] = True

        _atomic_write_claude_json(claude_json, raw, data)
    return True
