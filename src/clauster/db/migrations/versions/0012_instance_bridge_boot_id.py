"""Persist each bridge's boot id so a clock step over an hour cannot fake a dead bridge.

Adds one nullable column to ``instances``: ``bridge_boot_id`` — the value of
``/proc/sys/kernel/random/boot_id`` at spawn, the boot the paired ``bridge_start_ticks``
(8e2d05b7a913) was measured in.

``bridge_start_ticks`` alone left one residue (#1401). Ticks restart at zero each boot, so
they cannot tell a live process from a row written in an EARLIER boot whose pid was recycled
onto a process holding the same count. ``procutil.is_live_process`` closed that with a coarse
wall-clock epoch conjunct, but that conjunct had two faults of its own: it capped the fix at
about an hour of correction, so a clock STEP larger than that (a VM snapshot restore, an
RTC-less board syncing long after boot) failed it even on an exact tick match and reopened
#1399; and across a reboot the epoch gap is (previous uptime + downtime), so a host that
rebooted inside an hour could admit a process holding both the same pid and the same count.

The boot id settles both exactly. It is stable for one boot and regenerated on the next, so
a recorded value that differs from the live one proves a row is from an earlier boot — its
pid names a different process regardless of the wall clock. With it recorded,
``is_live_process`` drops the epoch conjunct entirely: an exact tick match plus a matching
boot id is a complete identity, and neither half moves when NTP corrects the clock.

Additive and nullable, mirroring 8e2d05b7a913: a row written by an older build loads with the
column absent, and the comparison degrades to the exact tick match ALONE — correct within a
boot, and re-stamped with a boot id on that bridge's next spawn. Linux-only, exactly as
``bridge_start_ticks`` is: macOS and Windows record an absolute create-time and are exposed to
neither the drift fault nor the cross-boot one, so both columns stay NULL there.

Revision ID: d0a7c3f21b84
Revises: c4f1b6a2e590
Create Date: 2026-09-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0a7c3f21b84"
down_revision: str | None = "c4f1b6a2e590"  # 0011_hosted_session_agent_start_ticks (#1404)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable per-boot bridge boot-id column to ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.add_column(sa.Column("bridge_boot_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Drop the per-boot bridge boot-id column from ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.drop_column("bridge_boot_id")
