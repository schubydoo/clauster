"""Persist each hosted agent's boot-relative start ticks so clock drift can't fake a loss.

Adds one nullable column to ``hosted_sessions``: ``agent_start_ticks`` — field 22 of
``/proc/<pid>/stat``, the hosted twin of ``instances.bridge_start_ticks`` (8e2d05b7a913).

``agent_proc_start`` alone could not do this job. It holds psutil's ``create_time``, which
on Linux is ``starttime/CLK_TCK + boot_time()``, and ``boot_time()`` re-reads ``/proc/stat``
btime on every call. btime tracks the live realtime-vs-uptime offset, so NTP slew moves it
under a process that never restarted — five distinct btime values spanning four seconds
inside 3.5 minutes on a drifting host, against the 0.05s bound ``is_killable_hosted``
compares with. The survived agent then reads as "not that process", so
``HostedManager._is_orphan`` classifies it as *lost* rather than as a recoverable orphan and
``kill_if_match`` refuses the kill. That direction is safe — no kill is ever aimed at a
stranger — but it is not harmless: the agent keeps running with no dashboard control, and a
Resume spawns a second agent beside it on the same conversation.

Ticks are measured against the boot instant instead, so they do not move. They cannot
replace the epoch outright — ticks restart at zero each boot, so after a reboot an unrelated
process could hold both the same pid and the same count — which is why
``procutil.is_live_process`` keeps both and swaps their roles: exact on the ticks (the
PID-reuse defense, now genuinely exact rather than nearly-tight), coarse on the epoch (a
"same boot?" discriminator).

Additive and nullable, mirroring 8e2d05b7a913: a row written by an older build loads with
the column absent and the comparison degrades to exactly today's epoch-only behaviour, which
still drifts. The bridge half's self-heal is deliberately NOT mirrored, and the cost is
worth stating rather than implying. It could be: ``runner._ticks_on_exact_match`` needs no
poll tick, only a moment at which the old 0.05s epoch bound holds, and ``reattach_all``
produces exactly one — a True answer from ``is_killable_hosted`` on a tick-less row MEANS
that bound held — and it already ends in a ``_persist``, so a stamp would cost no extra
write. It is left out to keep #1404 to the one change it needs. The gap is wider here than
for bridges, not narrower: a bridge samples several times a minute and heals on the first
undrifted one, while a hosted row gets one sample per clauster restart, and the row that
most needs the fix is a long-lived Direct Session that is never respawned.

Revision ID: c4f1b6a2e590
Revises: c1f4a70b9e63
Create Date: 2026-09-01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f1b6a2e590"
# Alembic chains on revision IDs, not filenames. This parent is 0010 from issue 1402, which
# merged first; `test_the_migration_chain_has_exactly_one_head` reds CI if a later branch
# ever forks the chain off an older parent.
down_revision: str | None = "c1f4a70b9e63"  # 0010_instance_keeper_start_ticks (#1402)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable boot-relative agent start-ticks column to ``hosted_sessions``."""
    with op.batch_alter_table("hosted_sessions") as batch_op:
        batch_op.add_column(sa.Column("agent_start_ticks", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the boot-relative agent start-ticks column from ``hosted_sessions``."""
    with op.batch_alter_table("hosted_sessions") as batch_op:
        batch_op.drop_column("agent_start_ticks")
