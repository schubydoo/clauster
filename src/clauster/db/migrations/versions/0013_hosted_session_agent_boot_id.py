"""Persist each hosted agent's boot id so a clock step cannot fake a dead hosted agent.

Adds one nullable column to ``hosted_sessions``: ``agent_boot_id`` — the value of
``/proc/sys/kernel/random/boot_id`` at spawn, the boot the paired ``agent_start_ticks``
(c4f1b6a2e590) was measured in, and the hosted twin of ``instances.bridge_boot_id``
(d0a7c3f21b84).

``agent_start_ticks`` alone left one residue (#1404 / #1401). Ticks restart at zero each
boot, so they cannot tell a live agent from a row written in an EARLIER boot whose pid was
recycled onto a process holding the same count. ``procutil.is_killable_hosted`` closed that
with a coarse wall-clock epoch conjunct, but that conjunct had two faults of its own: it
capped the fix at about an hour of correction, so a clock STEP larger than that (a VM
snapshot restore, an RTC-less board syncing long after boot) failed it even on an exact tick
match; and across a reboot the epoch gap is (previous uptime + downtime), so a host that
rebooted inside an hour could admit a process holding both the same pid and the same count —
the residue ``test_a_cross_boot_tick_collision_inside_the_epoch_window_is_admitted`` pinned.

The boot id settles both exactly. It is stable for one boot and regenerated on the next, so
a recorded value that differs from the live one proves a row is from an earlier boot — its
pid names a different process regardless of the wall clock. With it recorded,
``is_killable_hosted`` uses it INSTEAD of the epoch conjunct: an exact tick match plus a
matching boot id is a complete identity, and neither half moves when NTP corrects the clock.

Additive and nullable, mirroring c4f1b6a2e590: a row written by an older build loads with the
column absent, and the comparison falls back to the exact tick match plus the coarse epoch
conjunct — the pre-#1401 behaviour, re-stamped on that agent's next spawn or on the next
drift-immune tick match a reattach observes. Linux-only, exactly as ``agent_start_ticks`` is:
macOS and Windows record an absolute create-time and are exposed to neither the drift fault
nor the cross-boot one, so both columns stay NULL there.

Revision ID: f3a9d1c7b204
Revises: d0a7c3f21b84
Create Date: 2026-09-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a9d1c7b204"
down_revision: str | None = "d0a7c3f21b84"  # 0012_instance_bridge_boot_id (#1401)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable per-boot agent boot-id column to ``hosted_sessions``."""
    with op.batch_alter_table("hosted_sessions") as batch_op:
        batch_op.add_column(sa.Column("agent_boot_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Drop the per-boot agent boot-id column from ``hosted_sessions``."""
    with op.batch_alter_table("hosted_sessions") as batch_op:
        batch_op.drop_column("agent_boot_id")
