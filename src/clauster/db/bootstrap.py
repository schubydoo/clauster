"""Fail-closed DB startup: migrate to head, then one-time JSON import (#362).

Called once during app startup, before any store is read. Two steps, both
fail-closed per the maintainer's directive:

1. **Migrate to head.** Run the Alembic baseline (and any future revisions)
   against the live engine connection. A migration failure raises
   :class:`MigrationError` — the app must refuse to start rather than run against a
   half-migrated database. The Alembic config is built in code (script location +
   the injected connection), so no ``alembic.ini`` URL is consulted at runtime.

2. **One-time JSON import.** On the *first* boot onto the DB — detected by an empty
   schema (no rows imported yet) and the presence of a legacy ``state.json`` /
   ``hosted_state.json`` — copy those records into the tables, then rename each JSON
   file to ``*.imported`` (kept, not deleted — the same conservatism as the JSON
   store's ``.bak``). An import error logs and leaves the JSON untouched; it never
   crashes boot and never half-imports (the import runs in one transaction).
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..hosted_state import HostedStateStore as JsonHostedStateStore
from ..state import StateStore as JsonStateStore
from .models import HostedSession, Instance
from .stores import HostedStateStore, StateStore

_log = logging.getLogger("clauster.db.bootstrap")

# The packaged migration environment (env.py + versions/) lives beside this module.
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_ALEMBIC_INI = Path(__file__).resolve().parent / "alembic.ini"


class MigrationError(RuntimeError):
    """Raised when the schema can't be brought to head — the app must not start."""


def _alembic_config(connection: object) -> Config:
    """Build an Alembic ``Config`` bound to the live ``connection``.

    Sets the packaged script location and injects the open connection via
    ``config.attributes`` (the shared-connection pattern), so ``env.py`` upgrades
    the database the app already opened rather than building its own engine.
    """
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    return cfg


def upgrade_to_head(engine: Engine) -> None:
    """Bring the schema to ``head``, fail-closed.

    Runs inside a single connection so ``env.py`` binds to it. Any failure is
    wrapped in :class:`MigrationError` — the caller must let it propagate and abort
    startup, never run on a partially-migrated database.
    """
    try:
        with engine.begin() as connection:
            command.upgrade(_alembic_config(connection), "head")
    except Exception as exc:  # noqa: BLE001 — re-raised as a typed fatal error
        # A migration failure is fatal by design: surface it loudly and refuse to
        # start. We catch broadly because Alembic/DBAPI raise a wide range here, but
        # we never swallow — every path re-raises MigrationError.
        _log.error("database migration to head failed; refusing to start: %s", exc)
        raise MigrationError(f"database migration failed: {exc}") from exc


def _schema_is_empty(session: Session) -> bool:
    """Whether the foundation tables hold no rows (the first-boot import trigger)."""
    instances = session.execute(select(func.count()).select_from(Instance)).scalar_one()
    hosted = session.execute(select(func.count()).select_from(HostedSession)).scalar_one()
    return instances == 0 and hosted == 0


def import_legacy_json(state_dir: Path, session_factory: sessionmaker[Session]) -> bool:
    """Import legacy ``state.json`` / ``hosted_state.json`` once, fail-closed.

    Returns ``True`` if anything was imported. Skips entirely when the schema
    already holds rows (a prior import or normal use). On any error the JSON files
    are left intact and ``False`` is returned — boot continues on the empty DB.
    """
    state_dir = state_dir.expanduser()
    state_path = state_dir / JsonStateStore.FILENAME
    hosted_path = state_dir / JsonHostedStateStore.FILENAME
    if not state_path.exists() and not hosted_path.exists():
        return False

    instance_records: dict[str, dict] = {}
    hosted_records: dict[str, dict] = {}
    try:
        with session_factory() as session, session.begin():
            if not _schema_is_empty(session):
                # Already migrated or in use — never re-import on top of live rows.
                # The transaction is an empty no-op; it commits nothing on block exit.
                return False
            instance_records = JsonStateStore(state_dir).load() if state_path.exists() else {}
            hosted_records = JsonHostedStateStore(state_dir).load() if hosted_path.exists() else {}
            StateStore._sync(session, instance_records)
            HostedStateStore._sync(session, hosted_records)
    except (SQLAlchemyError, OSError) as exc:
        # Fail-closed: leave the JSON intact (re-tried next boot) and run on the DB.
        _log.warning("legacy JSON import failed; leaving JSON in place: %s", exc)
        return False

    # Import committed — retire the JSON so a later restart doesn't re-trigger, but
    # keep the file (renamed) rather than deleting it, mirroring the .bak posture.
    _retire(state_path)
    _retire(hosted_path)
    imported = bool(instance_records) or bool(hosted_records)
    if imported:
        _log.info(
            "imported legacy JSON state into the database (%d instances, %d hosted)",
            len(instance_records),
            len(hosted_records),
        )
    return imported


def _retire(path: Path) -> None:
    """Rename ``path`` to ``*.imported`` (best-effort; keep, don't delete)."""
    if not path.exists():
        return
    try:
        path.rename(path.with_suffix(path.suffix + ".imported"))
    except OSError as exc:
        # A rename failure is non-fatal: the schema-empty guard stops a re-import.
        _log.warning("could not retire %s after import: %s", path, exc)
