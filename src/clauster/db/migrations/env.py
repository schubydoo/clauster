"""Alembic environment for the clauster persistence layer (#362).

Driven programmatically from :mod:`clauster.db.bootstrap`, never the ``alembic``
CLI in production. The bootstrap passes the live engine connection via
``config.attributes['connection']`` (the shared-connection cookbook pattern), so
this script does not read a URL from ``alembic.ini`` at runtime — it binds to the
connection the app already opened and upgrades it to ``head``.

If invoked without a shared connection (e.g. a developer running ``alembic`` by
hand to autogenerate a revision), it falls back to ``sqlalchemy.url`` from the
ini, or the ``CLAUSTER_DB_URL`` env var.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from clauster.db.models import Base

# Alembic Config object — access to the values within the .ini in use.
config = context.config

# The metadata the autogenerate + baseline target.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DB connection)."""
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("CLAUSTER_DB_URL")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection.

    Prefers the connection the bootstrap injected via ``config.attributes``; only
    builds its own engine when invoked standalone (the CLI autogenerate path).
    ``render_as_batch`` keeps ALTER operations working on SQLite, which has no
    native ``ALTER`` — harmless on Postgres.
    """
    connectable = config.attributes.get("connection")
    if connectable is not None:
        context.configure(
            connection=connectable,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    section = config.get_section(config.config_ini_section, {})
    url = os.environ.get("CLAUSTER_DB_URL")
    if url:
        section["sqlalchemy.url"] = url
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
