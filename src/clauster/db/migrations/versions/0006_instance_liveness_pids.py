"""Persist each instance's bridge/keeper pids so a fresh process can see liveness (#1088).

Adds three nullable columns to ``instances``: ``bridge_pid``, ``bridge_proc_start`` and
``keeper_pid``. Before this, "which instances are live, and what are their pids" existed
only in the memory of whichever process spawned them, so a short-lived process (the CLI,
``clauster mcp``) could not reconstruct liveness for a persisted row — and the pointer-walk
that stood in for it resolves at most ONE instance per project, silently dropping the rest
(#1088) while the running server never adopted rows another process created (#1091).

``bridge_proc_start`` is load-bearing rather than metadata: a bare pid is reusable, so
liveness is always checked as the pair (``procutil.is_live_bridge``). Persisting a pid
without its start time would let a recycled pid resurrect an unrelated process as a live
bridge — the same PID-reuse defence the keeper-sidecar reattach already applies.

Additive and nullable, mirroring ``hosted_sessions.agent_pid``/``agent_proc_start``: a row
written by an older build loads with the columns absent, and the reattach path falls back to
the pointer/sidecar lookup rather than declaring a surviving bridge dead. That fallback is
what keeps the first boot after upgrade from reporting every live bridge as stopped.

Revision ID: 7b1c4d2ea9f3
Revises: 59427b230fd4
Create Date: 2026-07-25

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b1c4d2ea9f3"
down_revision: str | None = "59427b230fd4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable liveness columns to ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.add_column(sa.Column("bridge_pid", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("bridge_proc_start", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("keeper_pid", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the liveness columns from ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.drop_column("keeper_pid")
        batch_op.drop_column("bridge_proc_start")
        batch_op.drop_column("bridge_pid")
