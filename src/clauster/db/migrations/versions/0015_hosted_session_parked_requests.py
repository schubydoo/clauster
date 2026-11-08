"""Persist the permission prompts a Direct Session waits on, so a restart keeps them.

Adds one nullable column to ``hosted_sessions``: ``parked_requests`` — a JSON list of the
``can_use_tool`` (and other parked) control requests the agent was waiting on at the last
save, each as ``{request_id, subtype, request}`` (TASK-537).

Before this, parked prompts lived only in memory. A plain Clauster restart reattached the
agent past the prompt (the replay cursor is after its frame), so the agent went on waiting
for an answer the dashboard no longer showed. With the column, the reattach parks them
again. When the reattach cannot reach a running agent, the row names them instead.

Additive and nullable, mirroring f3a9d1c7b204: a row an older build wrote loads with the
column absent, which reads as "no prompt waiting", exactly the behaviour before.

Revision ID: 2c6e8a4f1d93
Revises: 9f7e9be8c129
Create Date: 2026-11-08

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2c6e8a4f1d93"
down_revision: str | None = "9f7e9be8c129"  # 0014_instance_sandbox_mode (#1101)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``parked_requests`` JSON column to ``hosted_sessions``."""
    with op.batch_alter_table("hosted_sessions") as batch_op:
        batch_op.add_column(
            sa.Column("parked_requests", sa.JSON(none_as_null=True), nullable=True)
        )


def downgrade() -> None:
    """Drop the ``parked_requests`` column from ``hosted_sessions``."""
    with op.batch_alter_table("hosted_sessions") as batch_op:
        batch_op.drop_column("parked_requests")
