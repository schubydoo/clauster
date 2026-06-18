"""DB-backed ``StateStore`` / ``HostedStateStore`` (#362).

Drop-in replacements for the JSON stores in :mod:`clauster.state` and
:mod:`clauster.hosted_state`: identical ``load() -> dict[str, dict]`` and
``save(records)`` signatures, so :mod:`clauster.runner` and
:mod:`clauster.hosted` are unchanged. The callers already wrap both in
``asyncio.to_thread``, so a synchronous DB API never blocks the event loop.

Contract preserved from the JSON stores:

* ``load`` returns ``{key: {persisted fields present for that key}}`` — a field
  absent in the row stays absent in the dict (the JSON store dropped ``None`` /
  unknown keys; callers ``.get(...)`` with defaults).
* ``save`` is a *full replace* of the map: the callers compute the complete subset
  each round, so a key gone from ``records`` must be deleted from the table.
* Fail-closed: a corrupt/unreadable store degrades ``load`` to ``{}`` (never
  crash). ``save`` re-raises as ``OSError`` so the callers' existing best-effort
  ``except OSError`` path (a stale cursor, not a failed spawn) still applies.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .models import HostedSession, Instance, Project

_log = logging.getLogger("clauster.db.stores")

# The fields each store round-trips, in the order the JSON ``_PERSISTED_FIELDS``
# whitelist listed them. Kept here so the DB store drops the same unknown keys.
_INSTANCE_FIELDS = ("label", "intentional_stop", "spawn_mode", "permission_mode", "resume_mode")
_HOSTED_FIELDS = (
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


def _present(row: object, fields: tuple[str, ...]) -> dict:
    """Return ``{field: value}`` for non-``None`` columns of ``row``.

    Mirrors the JSON store's drop-absent behaviour: a column left ``NULL`` is
    omitted entirely, so a caller's ``.get(field)`` sees ``None`` exactly as it did
    when the JSON file simply didn't carry the key.
    """
    out: dict = {}
    for field in fields:
        value = getattr(row, field)
        if value is not None:
            out[field] = value
    return out


class StateStore:
    """Per-project bridge intent, backed by the ``instances`` table.

    Same contract as :class:`clauster.state.StateStore`: keyed by project name,
    round-trips the label / intentional-stop / spawn-permission-resume modes,
    fail-closed on read.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """Bind the store to a session factory (the shared engine's ``sessionmaker``)."""
        self._sessions = session_factory

    def load(self) -> dict[str, dict]:
        """Return ``{project_name: {persisted fields}}``; ``{}`` on any DB error."""
        try:
            with self._sessions() as session:
                rows = session.execute(select(Instance)).scalars().all()
                return {row.project_name: _present(row, _INSTANCE_FIELDS) for row in rows}
        except SQLAlchemyError as exc:
            # Fail-closed like the JSON store's corrupt-file path: a read failure
            # degrades to "forget the labels", never a crash on startup.
            _log.warning("state load failed, degrading to empty: %s", exc)
            return {}

    def save(self, records: dict[str, dict]) -> None:
        """Replace the ``instances`` map with ``records`` (full upsert + prune).

        Ensures a :class:`Project` row exists for each key (foreign-key parent),
        upserts the instance row, and deletes instance rows for projects no longer
        in ``records``. Raises :class:`OSError` on failure so the callers'
        best-effort ``except OSError`` still applies.
        """
        try:
            with self._sessions() as session:
                with session.begin():
                    self._sync(session, records)
        except SQLAlchemyError as exc:
            raise OSError(f"state save failed: {exc}") from exc

    @staticmethod
    def _sync(session: Session, records: dict[str, dict]) -> None:
        """Upsert every record and delete instance rows absent from ``records``."""
        keep = set(records)
        existing = {
            row.project_name: row for row in session.execute(select(Instance)).scalars().all()
        }
        known_projects = set(session.execute(select(Project.name)).scalars().all())
        for name, fields in records.items():
            if name not in known_projects:
                session.add(Project(name=name))
                known_projects.add(name)
            row = existing.get(name)
            if row is None:
                row = Instance(project_name=name)
                session.add(row)
            for field in _INSTANCE_FIELDS:
                setattr(row, field, fields.get(field))
        for name, row in existing.items():
            if name not in keep:
                session.delete(row)


class HostedStateStore:
    """Hosted-channel sessions, backed by the ``hosted_sessions`` table.

    Same contract as :class:`clauster.hosted_state.HostedStateStore`: keyed by
    ``claustrum_process_id``, round-trips the reattach metadata + cursor,
    fail-closed on read.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """Bind the store to a session factory (the shared engine's ``sessionmaker``)."""
        self._sessions = session_factory

    def load(self) -> dict[str, dict]:
        """Return ``{process_id: {persisted fields}}``; ``{}`` on any DB error."""
        try:
            with self._sessions() as session:
                rows = session.execute(select(HostedSession)).scalars().all()
                return {row.claustrum_process_id: _present(row, _HOSTED_FIELDS) for row in rows}
        except SQLAlchemyError as exc:
            _log.warning("hosted-state load failed, degrading to empty: %s", exc)
            return {}

    def save(self, records: dict[str, dict]) -> None:
        """Replace the ``hosted_sessions`` map with ``records`` (full upsert + prune)."""
        try:
            with self._sessions() as session:
                with session.begin():
                    self._sync(session, records)
        except SQLAlchemyError as exc:
            raise OSError(f"hosted-state save failed: {exc}") from exc

    @staticmethod
    def _sync(session: Session, records: dict[str, dict]) -> None:
        """Upsert every record and delete hosted rows absent from ``records``."""
        keep = set(records)
        existing = {
            row.claustrum_process_id: row
            for row in session.execute(select(HostedSession)).scalars().all()
        }
        for process_id, fields in records.items():
            row = existing.get(process_id)
            if row is None:
                row = HostedSession(claustrum_process_id=process_id)
                session.add(row)
            for field in _HOSTED_FIELDS:
                setattr(row, field, fields.get(field))
        gone = [pid for pid in existing if pid not in keep]
        if gone:
            session.execute(
                delete(HostedSession).where(HostedSession.claustrum_process_id.in_(gone))
            )
