"""SQLAlchemy 2.0 declarative models for the persistence layer (#362, #363).

The foundation tables mirror exactly what the two JSON stores held — nothing more.
Each table's data columns are the JSON store's ``_PERSISTED_FIELDS`` whitelist, so
the DB-backed stores round-trip the same ``dict[str, dict]`` the callers already use:

* :class:`Project` — a project the runner has tracked (name is the natural key the
  ``state.json`` map was keyed by). Present so ``instances`` can carry a real
  foreign key, the seam the session-history table (#363) builds on.
* :class:`Instance` — per-project bridge intent the startup pointer-walk can't
  re-derive (``state.json`` ``instances`` map). One row per project.
* :class:`HostedSession` — a hosted-channel session keyed by its
  ``claustrum_process_id`` (``hosted_state.json`` ``sessions`` map).
* :class:`SessionEvent` — the append-only session lifecycle / event history (#363):
  one row per ``spawned`` / ``ready`` / ``ended`` / ``crashed`` transition, with a
  terminal-row cost/token snapshot. Survives a restart, so a per-project "last used
  / total cost" is readable straight from the DB; unblocks #298 and #303.

Only portable column types are used (no SQLite-only types), so the same metadata
runs on Postgres for the multi-user work (#364). Timestamps are timezone-aware.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    """Return a timezone-aware UTC ``now`` (default for audit timestamps)."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for every foundation table.

    Carries the shared :attr:`metadata` Alembic autogenerate + the baseline
    migration target. Concrete tables are defined below.
    """


class TimestampMixin:
    """Adds tz-aware ``created_at`` / ``updated_at`` audit columns.

    ``updated_at`` is refreshed on every row update via ``onupdate`` so the
    session-history work (#363) has a truthful "last touched" without each store
    having to remember to set it.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class Project(Base, TimestampMixin):
    """A project the runner tracks, keyed by its directory name.

    The ``state.json`` map was keyed by project name; this promotes that key to a
    real row so :class:`Instance` can foreign-key it. Created on demand when an
    instance row is written for a not-yet-seen project.
    """

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)

    instances: Mapped[list[Instance]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    session_events: Mapped[list[SessionEvent]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Instance(Base, TimestampMixin):
    """Per-instance bridge intent — the ``state.json`` ``instances`` record (#777).

    Holds only the fields the startup pointer-walk can't re-derive (instance_id,
    label, the intentional-stop flag, and the spawn/permission/resume modes).

    Since issue 777 the primary key is ``instance_id`` (a stable RFC 4122 UUID
    minted at spawn time), not ``project_name``.  This allows multiple rows per
    project — a standard bridge keeps its 1-per-project constraint at the runner
    level, while interactive (pty) sessions may have N rows per project.

    ``project_name`` is kept as a non-null FK so the session-history table can
    join through it; a unique index on ``(project_name, resume_mode)`` is added
    by migration 0003 and enforced at the app level (not the DB level) to keep
    the standard-singleton rule soft and observable.
    """

    __tablename__ = "instances"

    instance_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_name: Mapped[str] = mapped_column(
        String(255), ForeignKey("projects.name", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    intentional_stop: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    spawn_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    permission_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resume_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)

    project: Mapped[Project] = relationship(back_populates="instances")


class HostedSession(Base, TimestampMixin):
    """A Direct Session (hosted-channel) record — the ``hosted_state.json`` ``sessions`` row.

    Keyed by the client-chosen ``claustrum_process_id``. Holds only what a clauster
    restart can't re-derive: the metadata to rebuild the dashboard row plus the
    best-effort ``daemon_last_seq`` reattach cursor. All columns are nullable —
    the JSON store dropped absent fields, and the store layer preserves that
    "absent stays absent" round-trip.
    """

    __tablename__ = "hosted_sessions"

    claustrum_process_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    permission_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    claude_session_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    daemon_last_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hosted_log_path: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    agent_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_proc_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    intentional_stop: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class SessionEvent(Base, TimestampMixin):
    """One append-only row per bridge/session lifecycle transition (#363).

    The session-history table the Projects-zone "last used" sort (#298) and the
    pty resume picker (#303) build on. Append-only: a session emits a ``spawned``
    row at launch, a ``ready`` row when it registers, and a terminal ``ended`` or
    ``crashed`` row when it stops. Launch/end timestamps, duration, mode, and a
    per-project cost rollup are all derivable from the row stream — no row is
    updated in place, so a query is a simple time-ordered scan.

    Cost / token columns are populated **only on the terminal row** (``ended`` /
    ``crashed``) and carry the project's cumulative end-of-session usage snapshot
    (sourced from :mod:`clauster.usage`); they stay ``NULL`` on ``spawned`` /
    ``ready`` rows. They are an approximate, informational dollar figure (the
    price table is hand-maintained) — never an authoritative ledger.

    Only portable column types are used (no SQLite-only types), so the same
    metadata runs on Postgres for the multi-user work (#364).
    """

    __tablename__ = "session_events"
    __table_args__ = (
        # The two hot read shapes: per-project history ordered by time, and the
        # global "most recent first" feed. Both filter/sort on ``at``; the
        # composite index serves the per-project query and the standalone ``at``
        # index serves the global one without a full-table scan.
        Index("ix_session_events_project_at", "project_name", "at"),
        Index("ix_session_events_at", "at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_name: Mapped[str] = mapped_column(
        String(255), ForeignKey("projects.name", ondelete="CASCADE"), nullable=False
    )
    # "standard" / "pty" / "hosted" — the channel/resume axis the session ran on.
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    # "spawned" / "ready" / "ended" / "crashed" — the lifecycle transition kind.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # When the transition happened (tz-aware UTC). Distinct from ``created_at``
    # (the row's insert time) so a backfilled or deferred write keeps event order.
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    # Non-reversible correlation token grouping one session's rows. Mirrors the
    # webhook ``session_ref`` (a hashed starter-session id), so it never persists a
    # bearer-equivalent session id. NULL when no session id was available yet.
    session_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Cumulative end-of-session usage snapshot — terminal rows only, else NULL.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_creation_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    project: Mapped[Project] = relationship(back_populates="session_events")
