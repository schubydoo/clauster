"""Session lifecycle / event-history table (#363).

Adds the append-only ``session_events`` table on top of the persistence
foundation (#362, baseline ``f45313346555``). One row per bridge/session
lifecycle transition (``spawned`` / ``ready`` / ``ended`` / ``crashed``), carrying
the mode, a tz-aware event timestamp, a hashed session correlation token, and a
cumulative cost/token snapshot on the terminal row only. Launch/end timestamps,
duration, mode, and a per-project cost rollup are all derivable from the row
stream — no row is mutated in place.

This design is append-only-events (not one-row-per-session-with-status): a query
is a simple time-ordered scan, which is the shape the Projects-zone "last used"
sort (#298) and the pty resume picker (#303) both need.

Revision ID: f4424422f656
Revises: f45313346555
Create Date: 2026-06-21

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4424422f656"
down_revision: str | None = "f45313346555"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the append-only session_events table and its read-path indexes."""
    op.create_table(
        "session_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_name", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_ref", sa.String(length=128), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_creation_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_name"], ["projects.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_session_events_project_at", "session_events", ["project_name", "at"])
    op.create_index("ix_session_events_at", "session_events", ["at"])


def downgrade() -> None:
    """Drop the session_events table (indexes go with it)."""
    op.drop_index("ix_session_events_at", table_name="session_events")
    op.drop_index("ix_session_events_project_at", table_name="session_events")
    op.drop_table("session_events")
