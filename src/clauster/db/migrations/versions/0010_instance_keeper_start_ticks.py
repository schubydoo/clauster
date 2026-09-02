"""Persist each keeper's boot-relative start ticks so clock drift can't fake a dead keeper.

Adds one nullable column to ``instances``: ``keeper_start_ticks`` — field 22 of
``/proc/<pid>/stat`` for the PTY keeper pid, the keeper half of what 8e2d05b7a913 added for
the bridge.

``keeper_proc_start`` alone could not do this job, for the same reason ``bridge_proc_start``
could not. It holds psutil's ``create_time``, which on Linux is ``starttime/CLK_TCK +
boot_time()``, and ``boot_time()`` re-reads ``/proc/stat`` btime on every call. btime tracks
the live realtime-vs-uptime offset, so NTP slew moves it under a process that never
restarted — a 4-second spread inside 3.5 minutes was measured on a drifting host, against a
0.05s comparison bound.

The consequence differs from the bridge half's, and is not milder. ``forget`` refuses to drop
a record whose bridge or keeper is still running, and ``procutil.is_live_keeper`` is that
keeper gate. Under drift it answers "dead" for a live keeper, so ``forget`` deletes the row
while the keeper and its pty bridge keep running: no card, no row, and no automated path back
— ``pty_keeper.find_orphan_keepers`` is the only sweep that could reap it and the ``clauster
keepers`` CLI behind it needs the record that just went away. Recovery is by hand, editing
the state database.

Ticks are measured against the boot instant instead, so they do not move. They cannot replace
the epoch outright — ticks restart at zero each boot, so after a reboot an unrelated process
could hold both the same pid and the same count — which is why ``procutil.is_live_process``
keeps both and swaps their roles: exact on the ticks, coarse on the epoch.

Additive and nullable, mirroring 8e2d05b7a913 and 3a5e9c81d47b: a row written by an older
build loads with the column absent and the keeper comparison degrades to exactly today's
epoch-only behaviour.

Unlike the bridge half there is no self-heal pass, and one residue is accepted rather than
covered. A CARDED instance re-measures its keeper trio off the live pid every time the keeper
is spawned, reattached or adopted — the trio is never carried over from a row — so the stamp
lands on the first such event and the row is re-persisted with it. An UNCARDED row does not:
``rediscover`` deliberately leaves a pid-less row uncarded while its project already has an
unresolved live pty bridge, and none of those three events ever fires for it, so its
``keeper_start_ticks`` stays absent indefinitely. ``forget``'s persisted-only arm then still
compares that row on the drifting epoch alone, which is the pre-#1402 exposure, for that row
shape only. Closing it means stamping ticks from inside a delete gate or carding a row
``rediscover`` refuses to card on purpose; both are worse than the residue, and #1402 states
the epoch-only degrade for an older build as intended behaviour.

Revision ID: c1f4a70b9e63
Revises: 8e2d05b7a913
Create Date: 2026-09-01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1f4a70b9e63"
down_revision: str | None = "8e2d05b7a913"  # 0009_instance_bridge_start_ticks (#1399)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable boot-relative keeper start-ticks column to ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.add_column(sa.Column("keeper_start_ticks", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the boot-relative keeper start-ticks column from ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.drop_column("keeper_start_ticks")
