"""Lightweight ``state.json`` persistence (spec §8, schema versioning D14).

Persists only the few instance fields the startup pointer-walk *can't* recover —
``label``, ``intentional_stop``, ``spawn_mode``, ``permission_mode``,
``resume_mode`` — keyed by project name. Everything
else (pid, env_id, urls, status) is re-derived live, so this file is small and
non-authoritative: corruption degrades to "forget the labels", never a crash.

Atomic-write pattern mirrors ``trust.trust_directory``: write ``.tmp`` then
``os.replace``. Two one-time copies guard the two ways a load can act on data it
does not trust: ``.bak`` before a schema migration, and ``.corrupt.bak`` when the
file exists but cannot be used as a store at all.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

from .atomicio import atomic_copy_file, atomic_write_text

CURRENT_SCHEMA = 1

_log = logging.getLogger("clauster.state")


def _describe(exc: BaseException) -> str:
    """Render an exception for a log line: its type and its ``str``, never its ``repr``.

    ``UnicodeDecodeError.args[1]`` is the raw undecoded bytes of the whole file, so
    ``%r`` would put the entire file on one log line (measured on a sibling seam: 420 KB
    for a 200 KB input). ``str`` gives the bounded "codec can't decode byte 0xff in
    position N" form. The other three — ``JSONDecodeError``, the int-limit
    ``ValueError``, ``RecursionError`` — carry no file content either way.
    """
    return f"{type(exc).__name__}: {exc}"


class CorruptStateFile(Exception):
    """A state file exists but cannot be used as one.

    Raised only by :meth:`KeyedJsonStore.load_strict`; :meth:`KeyedJsonStore.load`
    catches it and degrades. Carries a short human reason, never file content.
    """


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
    unknown-field drop, and the atomic write. "Corrupt" is every shape that leaves a
    file we cannot use as a store: undecodable bytes, unparseable JSON, and JSON that
    parses to the wrong shape. Subclasses customize *only* the class attributes below,
    so the on-disk shape stays exactly per-store.
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
        """Return ``{key: {persisted fields}}``, tolerating a missing or corrupt file.

        The read every caller wants except one: a store that cannot be used degrades to
        ``{}`` so the app keeps running. Only a missing file degrades silently. Every
        other failure is warned about, and a file that is still on disk and still
        unusable is copied aside once as ``.corrupt.bak``.

        ANY mismatched ``schema_version`` — older or newer — is coerced to the current
        one, taking a separate one-time ``.bak`` first. Unknown fields are dropped.
        """
        try:
            return self.load_strict()
        except CorruptStateFile as exc:
            self._discard(str(exc))
            return {}
        except OSError as exc:
            # Unreadable (permissions, IO), not unusable: there is no copy to take, and
            # it is the arm actually worth diagnosing, so it must not pass silently.
            self._LOG.warning("could not read %s: %s", self._path, _describe(exc))
            return {}

    def load_strict(self) -> dict[str, dict]:
        """Like :meth:`load`, but raise :class:`CorruptStateFile` instead of degrading.

        For a caller that must not act on a degraded read. ``ops.migrate_state`` is
        ``load()`` then ``save()``, so a silent ``{}`` there overwrites the file the
        read could not understand — a wipe reported as success. The DB-backed
        ``db.stores.StateStore.load_strict`` exists for the same reason (#949).

        A missing file still returns ``{}``: there is nothing to misinterpret. An
        unreadable one (permissions, IO) raises its ``OSError`` unchanged, which is what
        the DB-backed store raises too — an unreadable file is not an empty one, and a
        caller that writes back what it read must not write back ``{}``.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except UnicodeDecodeError as exc:
            # A non-UTF-8 file: the bytes are not text, so there is no parse to attempt.
            raise CorruptStateFile(_describe(exc)) from exc
        # Any other OSError propagates: see the docstring. FileNotFoundError is caught
        # above it, so an absent file still reads as an empty store.
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            # Two parse failures are NOT JSONDecodeError: a *bare* ValueError from the
            # base-10 integer-string-conversion limit (CVE-2020-10735), on by default
            # for a >4300-digit int literal on every supported interpreter (>=3.11), and
            # RecursionError, which is not a ValueError at all — CPython's recursive
            # scanner overflows on deeply-nested JSON before json can raise (3.14.7+
            # bounds the depth itself and raises JSONDecodeError instead). Both used to
            # escape and propagate; this store is read at startup, so that took the app
            # down instead of degrading. `pointers.load_pointer` and `usage` catch the
            # same pair. No `OSError` here: the read is its own try above.
            raise CorruptStateFile(_describe(exc)) from exc
        if not isinstance(data, dict):
            # Valid JSON of the wrong shape is still a file we cannot use as a store,
            # and it reaches the same destructive re-save, so it degrades the same way.
            raise CorruptStateFile(f"top-level JSON is a {type(data).__name__}, not an object")
        # Before the schema branch, not after: :meth:`_migrate` coerces a non-object
        # record map to ``{}`` on its way past, which would launder real damage into a
        # clean-looking empty store — and ``ops.migrate_state`` would then write that
        # back over the file. An ABSENT map is not damage: a file that has never held a
        # record looks exactly like that, and only a PRESENT non-object is corruption.
        if self._MAP_KEY in data and not isinstance(data[self._MAP_KEY], dict):
            raise CorruptStateFile(
                f"{self._MAP_KEY!r} is a {type(data[self._MAP_KEY]).__name__}, not an object"
            )
        if data.get("schema_version") != self._SCHEMA:
            data = self._migrate(data, raw)

        # Total by construction: the map is either absent (an empty store) or the dict
        # the check above already accepted, and ``_migrate`` substitutes ``{}`` for an
        # absent one rather than dropping the key.
        records = data.get(self._MAP_KEY, {})
        out: dict[str, dict] = {}
        for key, fields in records.items():
            # Deliberately NOT the corrupt verdict the map-shape check above gives, even
            # though the damage looks similar: this store is non-authoritative, so one
            # unreadable record costs a label and the rest still load. Rejecting the
            # whole file would throw away the records we CAN read to punish one we
            # can't. A map that is not a map has no records to save that way.
            if isinstance(fields, dict):
                out[key] = {k: fields[k] for k in self._PERSISTED_FIELDS if k in fields}
        return out

    def save(self, records: dict[str, dict]) -> None:
        """Atomically persist ``{key: {persisted fields}}`` (durable, owner-only)."""
        payload = {"schema_version": self._SCHEMA, self._MAP_KEY: records}
        atomic_write_text(self._path, json.dumps(payload, indent=2))

    def _discard(self, reason: str) -> None:
        """Warn that a file is being thrown away, and keep one copy of it.

        The two halves always travel together, so they live in one method: a warning
        without a copy loses the file, and a copy without a warning is a silent
        degrade. ``reason`` is a short description — never file content.
        """
        self._LOG.warning("ignoring corrupt %s: %s", self._path, reason)
        self._backup_corrupt()

    def _backup_corrupt(self) -> None:
        """Copy a file we could not use aside, once, as ``.corrupt.bak``.

        Its own slot, not the pre-migration ``.bak``: that one is a snapshot of data we
        *did* parse, and a corrupt file must not be able to claim it and leave the next
        real migration with no snapshot. Never re-taken — an existing copy is the older,
        more original one. Best-effort: a failure is warned about, never raised, so it
        can't block a load or abort a caller's transaction.

        The copy exists because a caller may write over the file straight after a
        degraded load: ``db.bootstrap.import_legacy_json`` degrades by design.
        :func:`clauster.atomicio.atomic_copy_file` re-opens and streams the file rather
        than writing back the ``raw`` that failed — that is what keeps a non-UTF-8 file
        copyable at all, and what keeps the copy byte-exact instead of newline-folded.
        """
        backup = self._path.with_suffix(self._path.suffix + ".corrupt.bak")
        if backup.exists():
            return
        try:
            atomic_copy_file(self._path, backup)
        except OSError as exc:
            self._LOG.warning(
                "could not copy corrupt %s aside: %s: %s", self._path, type(exc).__name__, exc
            )

    def _migrate(self, data: dict, raw: str) -> dict:
        """Back up once, then coerce to the current schema.

        Only v1 exists today: a well-formed record map is preserved as-is, and a
        missing map becomes an empty v1. The ``else {}`` covers only that missing case
        now — :meth:`load_strict` rejects a present non-object map before it reaches
        here, because coercing one would hide the damage. The pre-coerce ``.bak`` guards
        against losing data we couldn't interpret.
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
