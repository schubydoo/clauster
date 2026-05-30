"""Lightweight ``state.json`` persistence (spec §8, schema versioning D14).

Persists only the few instance fields the startup pointer-walk *can't* recover —
``label``, ``intentional_stop``, ``spawn_mode``, ``permission_mode`` — keyed by
project name. Everything
else (pid, env_id, urls, status) is re-derived live, so this file is small and
non-authoritative: corruption degrades to "forget the labels", never a crash.

Atomic-write pattern mirrors ``trust.trust_directory``: write ``.tmp`` then
``os.replace``; a one-time ``.bak`` is taken before a schema migration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CURRENT_SCHEMA = 1
_PERSISTED_FIELDS = ("label", "intentional_stop", "spawn_mode", "permission_mode")


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir.expanduser() / "state.json"

    def load(self) -> dict[str, dict]:
        """Return ``{project: {persisted fields}}``.

        Tolerates a missing or corrupt file (returns ``{}``) and migrates an
        older schema_version (taking a ``.bak`` first). Unknown fields are dropped.
        """
        try:
            raw = self._path.read_text()
        except (FileNotFoundError, OSError):
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        if data.get("schema_version") != CURRENT_SCHEMA:
            data = self._migrate(data, raw)

        instances = data.get("instances")
        if not isinstance(instances, dict):
            return {}
        out: dict[str, dict] = {}
        for project, fields in instances.items():
            if isinstance(fields, dict):
                out[project] = {k: fields[k] for k in _PERSISTED_FIELDS if k in fields}
        return out

    def save(self, instances: dict[str, dict]) -> None:
        """Atomically persist ``{project: {persisted fields}}``."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": CURRENT_SCHEMA, "instances": instances}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self._path)

    def _migrate(self, data: dict, raw: str) -> dict:
        """Back up once, then coerce to the current schema.

        Only v1 exists today, so any unrecognized/older shape is conservatively
        reset to an empty v1 — we never trust ambiguous legacy data.
        """
        backup = self._path.with_suffix(self._path.suffix + ".bak")
        if not backup.exists():
            try:
                backup.write_text(raw)
            except OSError:
                pass  # backup is best-effort; never block the load
        instances = data.get("instances")
        return {
            "schema_version": CURRENT_SCHEMA,
            "instances": instances if isinstance(instances, dict) else {},
        }
