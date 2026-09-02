"""Lightweight ``state.json`` persistence (spec §8, schema versioning D14).

Persists only the few instance fields the startup pointer-walk *can't* recover —
``label``, ``intentional_stop``, ``spawn_mode``, ``permission_mode``,
``resume_mode`` — keyed by project name. Everything
else (pid, env_id, urls, status) is re-derived live, so this file is small and
non-authoritative: corruption degrades to "forget the labels", never a crash.

Atomic-write pattern mirrors ``trust.trust_directory``: write ``.tmp`` then
``os.replace``. Two one-time copies guard the two ways a load can act on data it
does not trust: ``.bak`` before a schema migration, and ``.corrupt.bak`` when the
file exists but cannot be read as JSON at all.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable

from .atomicio import atomic_write_text, replace_with_retry

CURRENT_SCHEMA = 1

_log = logging.getLogger("clauster.state")


@runtime_checkable
class KeyedStore(Protocol):
    """The ``{key: {fields}}`` load/save contract shared by every state store.

    Both the legacy JSON stores here and the DB-backed stores in
    :mod:`clauster.db.stores` satisfy this, so callers (the runner, the hosted
    manager) type against the contract rather than a concrete class — the seam the
    persistence foundation (#362) swapped the backend behind.
    """

    def load(self) -> dict[str, dict]:
        """Return the persisted ``{key: {fields}}`` map (``{}`` if unreadable)."""
        ...

    def save(self, records: dict[str, dict]) -> None:
        """Persist the full ``{key: {fields}}`` map (a replace, not a merge)."""
        ...


class KeyedJsonStore:
    """Schema-versioned JSON store of ``{key: {persisted fields}}`` records.

    A small, non-authoritative on-disk map: each concrete store sets the filename,
    the JSON inner-map key, the persisted-field whitelist, the current schema, and
    a logger. The shared behaviour is the tolerant fail-closed load (a missing file
    degrades to ``{}`` silently; a corrupt one degrades to ``{}`` with a warning and a
    one-time ``.corrupt.bak`` copy), the in-place coercion of any mismatched
    ``schema_version``, older or newer (taking a separate one-time ``.bak`` first), the
    unknown-field drop, and the atomic write. Subclasses customize *only* the
    class attributes below, so the on-disk shape stays exactly per-store.
    """

    FILENAME: str  # public: clauster.db.bootstrap reads it to find the legacy file
    _MAP_KEY: str
    _PERSISTED_FIELDS: tuple[str, ...]
    _SCHEMA: int
    _LOG: logging.Logger

    def __init__(self, state_dir: Path) -> None:
        """Point the store at its JSON file under ``state_dir``."""
        self._path = state_dir.expanduser() / self.FILENAME

    def load(self) -> dict[str, dict]:
        """Return ``{key: {persisted fields}}``.

        Tolerates a missing or corrupt file (returns ``{}``) and coerces ANY
        mismatched ``schema_version`` — older or newer — to the current one, taking a
        one-time ``.bak`` first. Unknown fields are dropped. Only a missing file
        degrades silently: every other failure is warned about, and one that leaves a
        file we could not interpret on disk is copied aside once (``.corrupt.bak``).
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except UnicodeDecodeError as exc:
            # UnicodeDecodeError (a ValueError) for a non-UTF-8 file is the "corrupt
            # file" case the docstring promises to tolerate — degrade to {}.
            self._log_corrupt(exc)
            self._backup_corrupt()
            return {}
        except OSError as exc:
            # Unreadable (permissions, IO), not malformed — no copy to take, and it is
            # the arm actually worth diagnosing, so it must not pass silently.
            self._LOG.warning("could not read %s: %s: %s", self._path, type(exc).__name__, exc)
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            # Two parse failures are NOT JSONDecodeError: a *bare* ValueError from the
            # base-10 integer-string-conversion limit (CVE-2020-10735), on by default
            # for a >4300-digit int literal on every supported interpreter (>=3.11), and
            # RecursionError, which is not a ValueError at all — CPython's recursive
            # scanner overflows on deeply-nested JSON before json can raise. Both used
            # to escape and propagate; this store is read at startup, so that took the
            # app down instead of degrading. `pointers.load_pointer` and `usage` catch
            # the same pair. No `OSError` here: the read is its own try above.
            self._log_corrupt(exc)
            self._backup_corrupt()
            return {}
        if not isinstance(data, dict):
            return {}
        if data.get("schema_version") != self._SCHEMA:
            data = self._migrate(data, raw)

        records = data.get(self._MAP_KEY)
        if not isinstance(records, dict):
            return {}
        out: dict[str, dict] = {}
        for key, fields in records.items():
            if isinstance(fields, dict):
                out[key] = {k: fields[k] for k in self._PERSISTED_FIELDS if k in fields}
        return out

    def save(self, records: dict[str, dict]) -> None:
        """Atomically persist ``{key: {persisted fields}}`` (durable, owner-only)."""
        payload = {"schema_version": self._SCHEMA, self._MAP_KEY: records}
        atomic_write_text(self._path, json.dumps(payload, indent=2))

    def _log_corrupt(self, exc: BaseException) -> None:
        """Warn that a file was discarded, naming which shape of corruption it was.

        Formatted with ``str``, never ``repr``: ``UnicodeDecodeError.args[1]`` is the
        whole decoded buffer, so ``%r`` would put the entire file on one log line.
        """
        self._LOG.warning("ignoring corrupt %s: %s: %s", self._path, type(exc).__name__, exc)

    def _backup_corrupt(self) -> None:
        """Copy a file we could not interpret aside, once, as ``.corrupt.bak``.

        Its own slot, not the pre-migration ``.bak``: that one is a snapshot of data we
        *did* parse, and a corrupt file must not be able to claim it and leave the next
        real migration with no snapshot. A byte-level copy rather than a re-write,
        because a file we could not decode has no text form and because the exact bytes
        are the whole point of keeping it. Never re-taken — an existing copy is the
        older, more original one. Best-effort: a failure is warned about, never raised,
        so it can't block a load or abort a caller's transaction.

        The copy exists because a caller may write over the file straight after a
        degraded load: ``ops.migrate_state`` is ``load()`` then ``save()``.
        """
        backup = self._path.with_suffix(self._path.suffix + ".corrupt.bak")
        if backup.exists():
            return
        tmp = backup.with_suffix(backup.suffix + ".tmp")
        try:
            # copy2 (not copyfile) carries the source's owner-only mode onto the copy;
            # the tmp + replace keeps a torn copy from ever occupying the one slot.
            shutil.copy2(self._path, tmp)
            replace_with_retry(tmp, backup)
        except OSError as exc:
            self._LOG.warning("could not copy corrupt %s aside: %s", self._path, exc)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def _migrate(self, data: dict, raw: str) -> dict:
        """Back up once, then coerce to the current schema.

        Only v1 exists today: a well-formed record map is preserved as-is, and a
        missing or non-dict map degrades to an empty v1 — we never trust an
        ambiguous shape. The pre-coerce ``.bak`` guards against losing data we
        couldn't interpret.
        """
        backup = self._path.with_suffix(self._path.suffix + ".bak")
        if not backup.exists():
            try:
                atomic_write_text(backup, raw)
            except OSError as exc:
                # Best-effort; never block the load, but surface it so a missing
                # pre-migration backup isn't a silent loss before we coerce away
                # the legacy data.
                self._LOG.warning("could not write %s backup: %s", backup, exc)
        records = data.get(self._MAP_KEY)
        return {
            "schema_version": self._SCHEMA,
            self._MAP_KEY: records if isinstance(records, dict) else {},
        }


class StateStore(KeyedJsonStore):
    """Persists per-project bridge intent (label, stop flag, spawn/permission mode).

    Backed by a single ``state.json`` under the state dir; reads tolerate a
    missing/corrupt file and coerce any mismatched schema in place.
    """

    FILENAME = "state.json"
    _MAP_KEY = "instances"
    _PERSISTED_FIELDS = (
        "project_name",
        "label",
        "intentional_stop",
        "spawn_mode",
        "permission_mode",
        "resume_mode",
    )
    _SCHEMA = CURRENT_SCHEMA
    _LOG = _log
