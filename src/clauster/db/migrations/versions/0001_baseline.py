"""Baseline schema: projects, instances, hosted_sessions (#362).

The initial persistence-foundation migration. Creates the three foundation tables
that replace the ``state.json`` + ``hosted_state.json`` JSON stores. Session
lifecycle / event-history tables are added by a later migration (#363).

Revision ID: f45313346555
Revises:
Create Date: 2026-06-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f45313346555"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the projects, instances, and hosted_sessions tables."""
    op.create_table(
        "projects",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "instances",
        sa.Column("project_name", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("intentional_stop", sa.Boolean(), nullable=True),
        sa.Column("spawn_mode", sa.String(length=32), nullable=True),
        sa.Column("permission_mode", sa.String(length=32), nullable=True),
        sa.Column("resume_mode", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_name"], ["projects.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_name"),
    )
    op.create_table(
        "hosted_sessions",
        sa.Column("claustrum_process_id", sa.String(length=255), nullable=False),
        sa.Column("project", sa.String(length=255), nullable=True),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("permission_mode", sa.String(length=32), nullable=True),
        sa.Column("claude_session_uuid", sa.String(length=64), nullable=True),
        sa.Column("daemon_last_seq", sa.Integer(), nullable=True),
        sa.Column("hosted_log_path", sa.String(length=4096), nullable=True),
        sa.Column("agent_pid", sa.Integer(), nullable=True),
        sa.Column("agent_proc_start", sa.Float(), nullable=True),
        sa.Column("started_at", sa.String(length=64), nullable=True),
        sa.Column("intentional_stop", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("claustrum_process_id"),
    )


def downgrade() -> None:
    """Drop the foundation tables (instances first — it FKs projects)."""
    op.drop_table("instances")
    op.drop_table("hosted_sessions")
    op.drop_table("projects")
