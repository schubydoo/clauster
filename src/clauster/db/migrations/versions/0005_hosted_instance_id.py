"""Persist the hosted-session ``instance_id`` across a restart (#841).

Adds a nullable ``instance_id`` column to ``hosted_sessions``: the per-runtime
``RemoteControlInstance.instance_id`` (a dashed RFC 4122 UUID) that #834/#840 made
the lifecycle routes also accept, resolved via ``HostedManager._key_for``. Before
this migration a restart never restored it, so ``reattach_all`` re-minted a fresh
one via the model's ``default_factory`` and a client that had cached the old id
got a 404. Nullable and additive-only: a pre-migration row (or one saved by an
older clauster build) loads with the column absent and simply mints a fresh id,
same as today's behavior.

Revision ID: 59427b230fd4
Revises: c28a9ef64664
Create Date: 2026-07-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "59427b230fd4"
down_revision: str | None = "c28a9ef64664"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``instance_id`` column to ``hosted_sessions``."""
    with op.batch_alter_table("hosted_sessions") as batch_op:
        batch_op.add_column(sa.Column("instance_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    """Drop the ``instance_id`` column from ``hosted_sessions``."""
    with op.batch_alter_table("hosted_sessions") as batch_op:
        batch_op.drop_column("instance_id")
