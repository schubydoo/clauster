"""Persist each pty instance's keeper create-time as a PID-reuse defense (#1178).

Adds one nullable column to ``instances``: ``keeper_proc_start``. The bridge half of
every liveness gate already pairs ``bridge_pid`` with ``bridge_proc_start``, so a pid the
OS recycled cannot masquerade as a live bridge. The keeper half had no such pair — only a
cmdline gate (``procutil.is_keeper_process``), which rules out a recycled pid running
something *else* but not a **different live keeper** that happens to hold that pid. On a
host running many interactive sessions, keeper pids are exactly the pids most likely to be
reused by another keeper.

That asymmetry matters most in ``forget``, which never kills a process: a false "still
live" answer strands the record with no operator path out short of hand-editing the state
database. With the create-time persisted, ``procutil.is_live_keeper`` compares the pair the
same way ``is_live_bridge`` does.

Additive and nullable, mirroring ``bridge_proc_start`` in 7b1c4d2ea9f3: a row written by an
older build loads with the column absent, and the keeper gate degrades to exactly today's
cmdline-only behaviour rather than declaring a live keeper dead — or an old row
unforgettable, which is the failure this is meant to prevent.

Revision ID: 3a5e9c81d47b
Revises: 1d7f2b60c4ae
Create Date: 2026-08-29

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3a5e9c81d47b"
down_revision: str | None = "1d7f2b60c4ae"  # 0007_instance_worktree_name (#1241) — renumbered
# from 0007 when both branches, each revising 0006, met at merge time.
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable keeper create-time column to ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.add_column(sa.Column("keeper_proc_start", sa.Float(), nullable=True))


def downgrade() -> None:
    """Drop the keeper create-time column from ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.drop_column("keeper_proc_start")
