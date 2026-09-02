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
still drifts. Unlike the bridge half there is no self-heal to add here — the hosted row's
one liveness reader runs once, in ``reattach_all``, so there is no poll tick to land a
stamp on. Such a row is re-stamped the next time that conversation is spawned or resumed.

Revision ID: c4f1b6a2e590
Revises: 8e2d05b7a913
Create Date: 2026-09-01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f1b6a2e590"
# TODO(1404): down_revision = <0010 id> on rebase. Issue 1402's migration takes 0010 and is
# not open yet, so this points at 0009 for now to keep the chain linear and the test suite's
# Alembic template buildable. Retarget it at 0010's revision id once that PR exists, then
# rebase onto the main that carries it.
down_revision: str | None = "8e2d05b7a913"  # 0009_instance_bridge_start_ticks (#1399)
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
