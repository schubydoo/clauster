"""Persist each bridge's boot-relative start ticks so clock drift can't fake a dead bridge.

Adds one nullable column to ``instances``: ``bridge_start_ticks`` — field 22 of
``/proc/<pid>/stat``, the same quantity a bridge pointer's ``procStart`` carries.

``bridge_proc_start`` alone could not do this job. It holds psutil's ``create_time``, which
on Linux is ``starttime/CLK_TCK + boot_time()``, and ``boot_time()`` re-reads ``/proc/stat``
btime on every call. btime tracks the live realtime-vs-uptime offset, so NTP slew moves it
under a process that never restarted — five distinct btime values spanning four seconds
inside 3.5 minutes on a drifting host, against a 0.05s comparison bound. The bridge then
reads "not our process", the instance is demoted to STOPPED, and the phantom-prune deletes
its still-running card.

Ticks are measured against the boot instant instead, so they do not move. They cannot
replace the epoch outright — ticks restart at zero each boot, so after a reboot an unrelated
process could hold both the same pid and the same count — which is why
``procutil.is_live_process`` keeps both and swaps their roles: exact on the ticks (the
PID-reuse defense, now genuinely exact rather than nearly-tight), coarse on the epoch (a
"same boot?" discriminator).

Additive and nullable, mirroring ``keeper_proc_start`` in 3a5e9c81d47b: a row written by an
older build loads with the column absent and the comparison degrades to exactly today's
epoch-only behaviour, which still drifts. What keeps that degradation from being destructive
is a separate guard rather than this column: the phantom-prune treats a verdict reached
WITHOUT the drift-immune half as inconclusive and refuses to delete a card on the strength of
its own project's pid. So a pre-#1399 row can still show a stale Stopped card until its next
spawn re-stamps it, but it can no longer be deleted for it.

Revision ID: 8e2d05b7a913
Revises: 3a5e9c81d47b
Create Date: 2026-09-01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8e2d05b7a913"
down_revision: str | None = "3a5e9c81d47b"  # 0008_keeper_proc_start (#1178)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable boot-relative bridge start-ticks column to ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.add_column(sa.Column("bridge_start_ticks", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the boot-relative bridge start-ticks column from ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.drop_column("bridge_start_ticks")
