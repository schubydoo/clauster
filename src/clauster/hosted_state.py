"""Persistence for hosted-channel sessions (CL-6 — reattach across clauster restarts).

A small, separate sibling of :mod:`clauster.state`: the bridge ``state.json`` is
project-keyed (one record per project), but hosted sessions live in their own
:class:`~clauster.hosted.HostedManager` registry, keyed by the client-chosen
``claustrum_process_id`` with possibly several per project. Rather than bend the
bridge schema, hosted state gets its own ``hosted_state.json`` — the same
tolerant-load + atomic-write + schema-versioned posture, keyed by process id.

Like the bridge store this is small and non-authoritative: it holds only what a
restart can't re-derive (the process id to reattach to, the display metadata, and
a best-effort ``daemon_last_seq`` replay cursor). Corruption degrades to "forget
the hosted sessions", never a crash — the daemon's own replay buffer and frame
de-duplication make a stale or missing cursor cost only replay overlap.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .atomicio import atomic_write_text

CURRENT_SCHEMA = 1

# The hosted-session fields a restart can't recover live. ``daemon_last_seq`` is
# the reattach replay cursor; the rest rebuild the dashboard row. All JSON-safe
# (the manager serializes Path/datetime before handing them here).
_PERSISTED_FIELDS = (
    "project",
    "label",
    "permission_mode",
    "claude_session_uuid",
    "daemon_last_seq",
    "hosted_log_path",
    "agent_pid",
    "agent_proc_start",
    "started_at",
    "intentional_stop",
)

_log = logging.getLogger("clauster.hosted_state")


class HostedStateStore:
    """Persists hosted-session records keyed by ``claustrum_process_id``.

    Backed by a single ``hosted_state.json`` under the state dir; reads tolerate a
    missing/corrupt file (degrade to ``{}``) and migrate older schemas in place.
    """

    def __init__(self, state_dir: Path) -> None:
        """Bind the store to ``hosted_state.json`` under ``state_dir``."""
        self._path = state_dir.expanduser() / "hosted_state.json"

    def load(self) -> dict[str, dict]:
        """Return ``{process_id: {persisted fields}}``.

        Tolerates a missing or corrupt file (returns ``{}``) and migrates an older
        ``schema_version`` (taking a ``.bak`` first). Unknown fields are dropped.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            # A non-UTF-8 (UnicodeDecodeError) or unreadable file is the "corrupt"
            # case the docstring promises to tolerate — degrade to {}.
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        if data.get("schema_version") != CURRENT_SCHEMA:
            data = self._migrate(data, raw)
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            return {}
        out: dict[str, dict] = {}
        for process_id, fields in sessions.items():
            if isinstance(fields, dict):
                out[process_id] = {k: fields[k] for k in _PERSISTED_FIELDS if k in fields}
        return out

    def save(self, sessions: dict[str, dict]) -> None:
        """Atomically persist ``{process_id: {persisted fields}}`` (durable, owner-only)."""
        payload = {"schema_version": CURRENT_SCHEMA, "sessions": sessions}
        atomic_write_text(self._path, json.dumps(payload, indent=2))

    def _migrate(self, data: dict, raw: str) -> dict:
        """Back up once, then re-stamp to the current schema.

        Only v1 exists today: a well-formed ``sessions`` map is preserved (load then
        filters each record to known fields), and a non-dict ``sessions`` value
        degrades to empty. A one-time ``.bak`` is taken before re-stamping.
        """
        backup = self._path.with_suffix(self._path.suffix + ".bak")
        if not backup.exists():
            try:
                atomic_write_text(backup, raw)
            except OSError as exc:
                # Best-effort; never block the load, but surface it so a missing
                # pre-migration backup isn't a silent loss before we coerce away.
                _log.warning("could not write %s backup: %s", backup, exc)
        sessions = data.get("sessions")
        return {
            "schema_version": CURRENT_SCHEMA,
            "sessions": sessions if isinstance(sessions, dict) else {},
        }
