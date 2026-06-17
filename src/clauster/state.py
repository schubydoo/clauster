"""Lightweight ``state.json`` persistence (spec §8, schema versioning D14).

Persists only the few instance fields the startup pointer-walk *can't* recover —
``label``, ``intentional_stop``, ``spawn_mode``, ``permission_mode``,
``resume_mode`` — keyed by project name. Everything
else (pid, env_id, urls, status) is re-derived live, so this file is small and
non-authoritative: corruption degrades to "forget the labels", never a crash.

Atomic-write pattern mirrors ``trust.trust_directory``: write ``.tmp`` then
``os.replace``; a one-time ``.bak`` is taken before a schema migration.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

from .atomicio import atomic_write_text

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
    a logger. The shared behaviour is the tolerant fail-closed load (missing or
    corrupt file degrades to ``{}``), the in-place migration of an older
    ``schema_version`` (taking a one-time ``.bak`` before coercing), the
    unknown-field drop, and the atomic write. Subclasses customize *only* the
    class attributes below, so the on-disk shape stays exactly per-store.
    """

    _FILENAME: str
    _MAP_KEY: str
    _PERSISTED_FIELDS: tuple[str, ...]
    _SCHEMA: int
    _LOG: logging.Logger

    def __init__(self, state_dir: Path) -> None:
        """Point the store at its JSON file under ``state_dir``."""
        self._path = state_dir.expanduser() / self._FILENAME

    def load(self) -> dict[str, dict]:
        """Return ``{key: {persisted fields}}``.

        Tolerates a missing or corrupt file (returns ``{}``) and migrates an
        older ``schema_version`` (taking a ``.bak`` first). Unknown fields are
        dropped.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            # UnicodeDecodeError (a ValueError) for a non-UTF-8 file is the "corrupt
            # file" case the docstring promises to tolerate — degrade to {}.
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
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
    missing/corrupt file and migrate older schemas in place.
    """

    _FILENAME = "state.json"
    _MAP_KEY = "instances"
    _PERSISTED_FIELDS = (
        "label",
        "intentional_stop",
        "spawn_mode",
        "permission_mode",
        "resume_mode",
    )
    _SCHEMA = CURRENT_SCHEMA
    _LOG = _log
