"""Named public-API bearer tokens table (#302).

Adds the ``api_tokens`` table backing ``clauster api-token issue/list/rotate/
revoke``: one row per named token, keyed by an autoincrement id, with a unique
operator-facing ``label`` and a unique SHA-256 ``token_hash`` (the same at-rest
form as the legacy single-token ``auth.api_token_hash``). ``last_used_at`` is
updated best-effort on a successful Bearer auth; ``created_at``/``updated_at``
come from the shared :class:`~clauster.db.models.TimestampMixin`.

The legacy ``config.auth.api_token_hash`` config field is untouched by this
migration — it keeps authenticating as a config-carried "unnamed" token folded
into the same verification path at the application layer (``app.py``), not
promoted into a row here.

Revision ID: c28a9ef64664
Revises: b3a1c4f9e021
Create Date: 2026-07-02

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c28a9ef64664"
down_revision: str | None = "b3a1c4f9e021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``api_tokens`` table with its label/hash uniqueness indexes."""
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # unique=True below creates the backing index for both columns; named
    # explicitly so `downgrade` can drop them by name on every backend (SQLite
    # included, where `op.drop_table` alone won't necessarily reap them first).
    op.create_index("ix_api_tokens_label", "api_tokens", ["label"], unique=True)
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    """Drop the ``api_tokens`` table and its indexes."""
    op.drop_index("ix_api_tokens_token_hash", table_name="api_tokens")
    op.drop_index("ix_api_tokens_label", table_name="api_tokens")
    op.drop_table("api_tokens")
