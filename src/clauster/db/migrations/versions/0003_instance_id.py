"""Re-key the instances table by instance_id (#777).

Replaces the ``project_name`` primary key with a stable RFC 4122 ``instance_id``
column.  Standard (server-mode) bridges retain one row per project; interactive
(pty) sessions may have N rows per project.  ``project_name`` stays as a non-null
FK column so session_events can still join through it.

Migration strategy (SQLite-compatible, no ALTER COLUMN):
  1. Create the new ``instances_new`` table with ``instance_id`` PK.
  2. Copy existing rows, using ``project_name`` as the seed for a deterministic
     v5 UUID (namespace = DNS, name = project_name) so existing persisted entries
     get a stable, reproducible id — restarts after the migration see the same key
     they saved.  In offline (``--sql``) mode there is no connection to read rows
     through Python, so a set-based copy is emitted instead; those rows get a
     random (valid, unique) hex id — data is preserved, only the online path
     guarantees the deterministic id used by restart-reattach.
  3. Drop the old table and rename.

``downgrade`` reconstructs the original project-name-keyed table, collapsing any
multi-row-per-project state by taking the most-recently-updated row for each
project (standard: at most one per project today; this boundary is only ever
crossed by future pty rows added after the upgrade).

Revision ID: b3a1c4f9e021
Revises: f4424422f656
Create Date: 2026-07-01

"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "b3a1c4f9e021"
down_revision: str | None = "f4424422f656"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# UUID namespace for deterministic project-name → instance_id derivation.
# Using uuid.NAMESPACE_DNS (RFC 4122 §4.3) — arbitrary but stable across
# processes, so every deployment derives the same id for the same project name.
_NS = uuid.NAMESPACE_DNS

# SQLite expression producing a random RFC-4122-shaped hex id (offline mode only,
# where Python can't read rows to compute the deterministic v5 UUID).
_SQLITE_RANDOM_UUID = (
    "lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || "
    "substr(lower(hex(randomblob(2))), 2) || '-' || "
    "substr('89ab', (abs(random()) % 4) + 1, 1) || "
    "substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))"
)


def _project_instance_id(project_name: str) -> str:
    """Return a deterministic UUID for a pre-existing project-keyed row."""
    return str(uuid.uuid5(_NS, f"clauster.instance.{project_name}"))


def upgrade() -> None:
    """Re-key instances by instance_id; keep project_name as a non-null FK."""
    # 1. Create the replacement table.
    op.create_table(
        "instances_new",
        sa.Column("instance_id", sa.String(length=36), nullable=False),
        sa.Column("project_name", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("intentional_stop", sa.Boolean(), nullable=True),
        sa.Column("spawn_mode", sa.String(length=32), nullable=True),
        sa.Column("permission_mode", sa.String(length=32), nullable=True),
        sa.Column("resume_mode", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_name"], ["projects.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("instance_id"),
    )

    # 2. Copy existing rows.
    if context.is_offline_mode():
        # --sql mode: no connection to read rows; emit a set-based copy whose
        # ids are random (see module docstring).
        # Constant SQL: _SQLITE_RANDOM_UUID is a module literal, not user input.
        offline_copy = (
            "INSERT INTO instances_new "  # noqa: S608
            "(instance_id, project_name, label, intentional_stop, "
            " spawn_mode, permission_mode, resume_mode, created_at, updated_at) "
            f"SELECT {_SQLITE_RANDOM_UUID}, project_name, label, intentional_stop, "
            "       spawn_mode, permission_mode, resume_mode, created_at, updated_at "
            "FROM instances"
        )
        op.execute(offline_copy)
    else:
        conn = op.get_bind()
        rows = conn.execute(sa.text("SELECT * FROM instances")).fetchall()
        for row in rows:
            row_dict = dict(row._mapping)
            project_name = row_dict["project_name"]
            conn.execute(
                sa.text(
                    "INSERT INTO instances_new "
                    "(instance_id, project_name, label, intentional_stop, "
                    " spawn_mode, permission_mode, resume_mode, created_at, updated_at) "
                    "VALUES (:iid, :pn, :label, :is_, :sm, :pm, :rm, :ca, :ua)"
                ),
                {
                    "iid": _project_instance_id(project_name),
                    "pn": project_name,
                    "label": row_dict.get("label"),
                    "is_": row_dict.get("intentional_stop"),
                    "sm": row_dict.get("spawn_mode"),
                    "pm": row_dict.get("permission_mode"),
                    "rm": row_dict.get("resume_mode"),
                    "ca": row_dict.get("created_at"),
                    "ua": row_dict.get("updated_at"),
                },
            )

    # 3. Swap tables.
    op.drop_table("instances")
    op.rename_table("instances_new", "instances")


def downgrade() -> None:
    """Reconstruct the project-name-keyed instances table."""
    op.create_table(
        "instances_old",
        sa.Column("project_name", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("intentional_stop", sa.Boolean(), nullable=True),
        sa.Column("spawn_mode", sa.String(length=32), nullable=True),
        sa.Column("permission_mode", sa.String(length=32), nullable=True),
        sa.Column("resume_mode", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_name"], ["projects.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_name"),
    )

    # Collapse any multi-row-per-project state: take the most-recently-updated
    # row per project (deterministic; today's pty rows are post-777 only).
    # Set-based, so it works identically online and offline.
    # SQLite-only: the bare non-aggregated columns paired with MAX(updated_at)
    # rely on SQLite's documented "bare columns come from the row that supplied
    # the MAX" behavior. clauster has no other DB backend (db/ is SQLite via
    # SQLAlchemy), so the standard-SQL ambiguity that would fail on PostgreSQL
    # does not apply here.
    op.execute(
        "INSERT INTO instances_old "
        "(project_name, label, intentional_stop, spawn_mode, "
        " permission_mode, resume_mode, created_at, updated_at) "
        "SELECT project_name, label, intentional_stop, spawn_mode, "
        "       permission_mode, resume_mode, created_at, MAX(updated_at) "
        "FROM instances "
        "GROUP BY project_name"
    )

    op.drop_table("instances")
    op.rename_table("instances_old", "instances")
