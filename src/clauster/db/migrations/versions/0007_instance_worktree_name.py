"""Persist a pty session's explicit git-worktree name so a reattach keeps it (#1241).

Adds one nullable column to ``instances``: ``worktree_name``.

A ``spawn_mode="worktree"`` interactive session runs in
``<repo>/.claude/worktrees/clauster-<instance_id[:8]>``, so the name is normally DERIVED
from the row's own id and needs no column. It stops being derivable on a keeper-only
reattach: a live keeper adopted from its sidecar with no row to take its identity from is
carded under a FRESH instance_id, and the derived name then points at a worktree that does
not exist. A resume would build a second one and orphan the original (which still holds any
uncommitted work and its branch), and the stop-time ``git worktree unlock`` would target the
empty name and leave the real worktree locked.

The keeper sidecar records the name the bridge was actually launched with, so the value is
recoverable — but only while that keeper is alive. Persisting it here carries the recovery
past the restart after the session ends, which is exactly when the Resume that needs it
happens.

Additive and nullable, mirroring ``0006``: a row written by an older build (and every
ordinary session, whose name IS derivable) loads with the column absent and falls back to
the derivation, so nothing changes for them.

Revision ID: 1d7f2b60c4ae
Revises: 7b1c4d2ea9f3
Create Date: 2026-08-29

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1d7f2b60c4ae"
down_revision: str | None = "7b1c4d2ea9f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``worktree_name`` column to ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.add_column(sa.Column("worktree_name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Drop the ``worktree_name`` column from ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.drop_column("worktree_name")
