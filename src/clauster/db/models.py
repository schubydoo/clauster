"""SQLAlchemy 2.0 declarative models for the persistence foundation (#362).

The foundation mirrors exactly what the two JSON stores held — nothing more. Each
table's data columns are the JSON store's ``_PERSISTED_FIELDS`` whitelist, so the
DB-backed stores round-trip the same ``dict[str, dict]`` the callers already use:

* :class:`Project` — a project the runner has tracked (name is the natural key the
  ``state.json`` map was keyed by). Present so ``instances`` can carry a real
  foreign key, the seam the session-history tables (#363) build on.
* :class:`Instance` — per-project bridge intent the startup pointer-walk can't
  re-derive (``state.json`` ``instances`` map). One row per project.
* :class:`HostedSession` — a hosted-channel session keyed by its
  ``claustrum_process_id`` (``hosted_state.json`` ``sessions`` map).

Only portable column types are used (no SQLite-only types), so the same metadata
runs on Postgres for the multi-user work (#364). Timestamps are timezone-aware.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
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


class Instance(Base, TimestampMixin):
    """Per-project bridge intent — the ``state.json`` ``instances`` record.

    Holds only the fields the startup pointer-walk can't re-derive (label, the
    intentional-stop flag, and the spawn/permission/resume modes). One row per
    project; the project name is both the foreign key and the natural key the
    store round-trips.
    """

    __tablename__ = "instances"

    project_name: Mapped[str] = mapped_column(
        String(255), ForeignKey("projects.name", ondelete="CASCADE"), primary_key=True
    )
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    intentional_stop: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    spawn_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    permission_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resume_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)

    project: Mapped[Project] = relationship(back_populates="instances")


class HostedSession(Base, TimestampMixin):
    """A hosted-channel session — the ``hosted_state.json`` ``sessions`` record.

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
