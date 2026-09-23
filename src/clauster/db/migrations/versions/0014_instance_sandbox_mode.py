"""Persist each bridge's sandbox choice so a restart cannot quietly reset it (#1101).

Adds one nullable column to ``instances``: ``sandbox_mode`` — the ``default`` / ``on`` /
``off`` choice a standard bridge was launched with (#780).

The runner already wrote the value into every persisted record, and the rebuild path
(``Rediscovery._saved_sandbox``) already read it back, but the store had no column to hold
it, so the value was dropped on every save. A STOPPED card rebuilt after a restart then read
it as absent and offered ``default``. That is invisible while the sandbox toggle is disabled
(#1037), because every value coerces to ``default`` anyway. Once the toggle returns, a bridge
launched with a non-default choice would resume as ``default`` after any restart, a silent
change to a security-relevant setting.

Additive and nullable, mirroring 1d7f2b60c4ae: a row written by an older build loads with the
column absent, and ``_saved_sandbox`` falls back to ``default`` for it, which is exactly how
every row behaved before the column existed. The next save of that row stores its choice.

Revision ID: 9f7e9be8c129
Revises: f3a9d1c7b204
Create Date: 2026-09-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f7e9be8c129"
down_revision: str | None = "f3a9d1c7b204"  # 0013_hosted_session_agent_boot_id (#1401)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``sandbox_mode`` column to ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.add_column(sa.Column("sandbox_mode", sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Drop the ``sandbox_mode`` column from ``instances``."""
    with op.batch_alter_table("instances") as batch_op:
        batch_op.drop_column("sandbox_mode")
