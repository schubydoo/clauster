"""DB-backed stores: ``StateStore`` / ``HostedStateStore`` (#362) + history (#363).

:class:`StateStore` and :class:`HostedStateStore` are drop-in replacements for the
JSON stores in :mod:`clauster.state` and :mod:`clauster.hosted_state`: identical
``load() -> dict[str, dict]`` and ``save(records)`` signatures, so
:mod:`clauster.runner` and :mod:`clauster.hosted` are unchanged. The callers
already wrap both in ``asyncio.to_thread``, so a synchronous DB API never blocks
the event loop.

Contract preserved from the JSON stores:

* ``load`` returns ``{key: {persisted fields present for that key}}`` — a field
  absent in the row stays absent in the dict (the JSON store dropped ``None`` /
  unknown keys; callers ``.get(...)`` with defaults).
* ``save`` is a *full replace* of the map: the callers compute the complete subset
  each round, so a key gone from ``records`` must be deleted from the table.
* Fail-closed: a corrupt/unreadable store degrades ``load`` to ``{}`` (never
  crash). ``save`` re-raises as ``OSError`` so the callers' existing best-effort
  ``except OSError`` path (a stale cursor, not a failed spawn) still applies.

:class:`SessionHistoryStore` (#363) is the append-only session-event history: it
appends a lifecycle row, reads history per-project or globally, and derives a
per-project "last used / total cost" rollup. It follows the same fail-closed read
posture — every read degrades to empty on a DB error and never crashes a poll —
while ``append`` is best-effort and swallows write errors (history is
non-authoritative; a lost event row must never fail a spawn or stop).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .models import HostedSession, Instance, Project, SessionEvent

_log = logging.getLogger("clauster.db.stores")

# Terminal lifecycle kinds — the rows that carry the end-of-session cost snapshot.
_TERMINAL_KINDS = ("ended", "crashed")

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


@dataclass(frozen=True)
class HistoryEvent:
    """One session-history row as the read API hands it out (a plain value object).

    A read-only snapshot of a :class:`~clauster.db.models.SessionEvent` row.
    Token / cost fields are populated only on a terminal (``ended`` / ``crashed``)
    row; they are ``None`` on ``spawned`` / ``ready`` rows.
    """

    id: int
    project_name: str
    mode: str
    kind: str
    at: datetime
    session_ref: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None


@dataclass(frozen=True)
class ProjectRollup:
    """Per-project "last used / total cost" derived straight from the history table.

    ``last_used`` is the most recent event timestamp for the project (``None`` when
    it has no history). ``total_cost_usd`` / token fields are the cumulative
    end-of-session snapshot from the project's most recent terminal row — the same
    ballpark figure :mod:`clauster.usage` produces — or ``None`` when no terminal
    row has been recorded yet. ``event_count`` is the project's total row count.
    """

    project_name: str
    last_used: datetime | None = None
    event_count: int = 0
    total_cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None


def _to_event(row: SessionEvent) -> HistoryEvent:
    """Map a persisted row to the read API's value object."""
    return HistoryEvent(
        id=row.id,
        project_name=row.project_name,
        mode=row.mode,
        kind=row.kind,
        at=row.at,
        session_ref=row.session_ref,
        cost_usd=row.cost_usd,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_creation_tokens=row.cache_creation_tokens,
        cache_read_tokens=row.cache_read_tokens,
    )


class SessionHistoryStore:
    """Append-only session lifecycle / event history, backed by ``session_events`` (#363).

    Records one row per ``spawned`` / ``ready`` / ``ended`` / ``crashed`` transition
    and serves the per-project + global history reads the Projects-zone "last used"
    sort (#298) and the pty resume picker (#303) build on. Reads are fail-closed
    (degrade to empty on a DB error, never crash a poll); :meth:`append` is
    best-effort and swallows write errors — history is non-authoritative, so a lost
    event row must never fail a spawn or stop.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """Bind the store to a session factory (the shared engine's ``sessionmaker``)."""
        self._sessions = session_factory

    def append(
        self,
        *,
        project_name: str,
        mode: str,
        kind: str,
        at: datetime | None = None,
        session_ref: str | None = None,
        cost_usd: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        cache_read_tokens: int | None = None,
    ) -> bool:
        """Append one lifecycle event; return whether it was written.

        Ensures a parent :class:`~clauster.db.models.Project` row exists (the FK
        target, mirroring :meth:`StateStore._sync`) before inserting. Best-effort:
        on any DB error the failure is logged and ``False`` returned — a lost
        history row must never break the bridge lifecycle. ``cost``/token fields are
        only meaningful on a terminal (``ended`` / ``crashed``) row.
        """
        try:
            with self._sessions() as session, session.begin():
                if not session.get(Project, project_name):
                    session.add(Project(name=project_name))
                session.add(
                    SessionEvent(
                        project_name=project_name,
                        mode=mode,
                        kind=kind,
                        at=at if at is not None else datetime.now(tz=UTC),
                        session_ref=session_ref,
                        cost_usd=cost_usd,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_creation_tokens=cache_creation_tokens,
                        cache_read_tokens=cache_read_tokens,
                    )
                )
            return True
        except SQLAlchemyError as exc:
            # Best-effort, fail-closed: history is non-authoritative, so a write
            # failure degrades to a missing row, never a failed spawn/stop.
            _log.warning("could not record session event (%s/%s): %s", project_name, kind, exc)
            return False

    def history_for(self, project_name: str, *, limit: int | None = None) -> list[HistoryEvent]:
        """Return a project's events, newest first; ``[]`` on any DB error.

        ``limit`` caps the number of rows returned (the most recent ``limit``).
        Served by the ``(project_name, at)`` composite index.
        """
        try:
            with self._sessions() as session:
                stmt = (
                    select(SessionEvent)
                    .where(SessionEvent.project_name == project_name)
                    .order_by(SessionEvent.at.desc(), SessionEvent.id.desc())
                )
                if limit is not None:
                    stmt = stmt.limit(limit)
                return [_to_event(row) for row in session.execute(stmt).scalars().all()]
        except SQLAlchemyError as exc:
            _log.warning(
                "session history read failed for %s, degrading to empty: %s", project_name, exc
            )
            return []

    def history(self, *, limit: int | None = None) -> list[HistoryEvent]:
        """Return events across all projects, newest first; ``[]`` on any DB error.

        ``limit`` caps the number of rows returned (the most recent ``limit``).
        Served by the standalone ``at`` index.
        """
        try:
            with self._sessions() as session:
                stmt = select(SessionEvent).order_by(
                    SessionEvent.at.desc(), SessionEvent.id.desc()
                )
                if limit is not None:
                    stmt = stmt.limit(limit)
                return [_to_event(row) for row in session.execute(stmt).scalars().all()]
        except SQLAlchemyError as exc:
            _log.warning("global session history read failed, degrading to empty: %s", exc)
            return []

    def rollup_for(self, project_name: str) -> ProjectRollup:
        """Return a project's "last used / total cost" rollup; empty on a DB error.

        ``last_used`` is the project's most recent event timestamp. The cost / token
        totals come from the project's most recent terminal (``ended`` / ``crashed``)
        row, which carries the cumulative end-of-session snapshot. A project with no
        history (or on a DB error) yields a rollup with ``last_used=None`` and
        ``total_cost_usd=None`` — never a crash.
        """
        try:
            with self._sessions() as session:
                last_used = session.execute(
                    select(func.max(SessionEvent.at)).where(
                        SessionEvent.project_name == project_name
                    )
                ).scalar_one()
                count = session.execute(
                    select(func.count())
                    .select_from(SessionEvent)
                    .where(SessionEvent.project_name == project_name)
                ).scalar_one()
                terminal = session.execute(
                    select(SessionEvent)
                    .where(
                        SessionEvent.project_name == project_name,
                        SessionEvent.kind.in_(_TERMINAL_KINDS),
                    )
                    .order_by(SessionEvent.at.desc(), SessionEvent.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                return ProjectRollup(
                    project_name=project_name,
                    last_used=last_used,
                    event_count=count,
                    total_cost_usd=terminal.cost_usd if terminal else None,
                    input_tokens=terminal.input_tokens if terminal else None,
                    output_tokens=terminal.output_tokens if terminal else None,
                    cache_creation_tokens=terminal.cache_creation_tokens if terminal else None,
                    cache_read_tokens=terminal.cache_read_tokens if terminal else None,
                )
        except SQLAlchemyError as exc:
            _log.warning(
                "session rollup read failed for %s, degrading to empty: %s", project_name, exc
            )
            return ProjectRollup(project_name=project_name)

    def sortmeta_for_all(
        self, names: list[str]
    ) -> dict[str, tuple[datetime | None, float | None]]:
        """Return ``{name: (last_used, cost_usd)}`` for many projects in ONE session.

        The batched form of :meth:`rollup_for` for the Projects sort control, which
        needs only ``last_used`` and the most-recent-terminal ``cost_usd``. Two grouped
        queries replace ``rollup_for``'s per-project 3-SELECT loop (the dashboard-sort
        N+1: P projects × a session checkout × 3 SELECTs). Names with no history are
        omitted (the caller defaults them to ``(None, None)``). Degrades to ``{}`` on
        any DB error — a sort never crashes the dashboard.
        """
        if not names:
            return {}
        try:
            out: dict[str, tuple[datetime | None, float | None]] = {}
            with self._sessions() as session:
                for project_name, last_used in session.execute(
                    select(SessionEvent.project_name, func.max(SessionEvent.at))
                    .where(SessionEvent.project_name.in_(names))
                    .group_by(SessionEvent.project_name)
                ):
                    out[project_name] = (last_used, None)
                # Most-recent terminal row per project (the cumulative cost snapshot),
                # picked with one windowed pass instead of a per-project LIMIT 1 query.
                ranked = (
                    select(
                        SessionEvent.project_name.label("project_name"),
                        SessionEvent.cost_usd.label("cost_usd"),
                        func.row_number()
                        .over(
                            partition_by=SessionEvent.project_name,
                            order_by=(SessionEvent.at.desc(), SessionEvent.id.desc()),
                        )
                        .label("rn"),
                    )
                    .where(
                        SessionEvent.project_name.in_(names),
                        SessionEvent.kind.in_(_TERMINAL_KINDS),
                    )
                    .subquery()
                )
                for project_name, cost_usd in session.execute(
                    select(ranked.c.project_name, ranked.c.cost_usd).where(ranked.c.rn == 1)
                ):
                    prior = out.get(project_name, (None, None))
                    out[project_name] = (prior[0], cost_usd)
            return out
        except SQLAlchemyError as exc:
            _log.warning("batch sortmeta read failed, degrading to empty: %s", exc)
            return {}
