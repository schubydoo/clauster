"""Fail-closed DB startup: snapshot + migrate to head, then one-time JSON import.

Called once during app startup, before any store is read. Three steps:

1. **Snapshot if a migration is pending (#795).** Before running Alembic, compare
   the connection's current revision against the packaged migration head. Only
   when they differ — i.e. a migration is actually about to run, never on a plain
   restart already at head — copy the live SQLite file to
   ``state_dir/backups/pre-<current>-<head>-<timestamp>.db`` via ``VACUUM INTO``,
   then prune old snapshots beyond the retention count. The snapshot is
   best-effort: a write failure is logged as a WARNING and startup proceeds
   (the migration itself is transactional, so a failed backup is not the
   fail-closed case — bricking the service over an unrelated backup write is a
   worse footgun). Recovery: copy a snapshot from ``state_dir/backups/`` back to
   ``state_dir/clauster.db`` with the service stopped.

2. **Migrate to head.** Run the Alembic baseline (and any future revisions)
   against the live engine connection. A migration failure raises
   :class:`MigrationError` — the app must refuse to start rather than run against a
   half-migrated database. The Alembic config is built in code (script location +
   the injected connection), so no ``alembic.ini`` URL is consulted at runtime.

3. **One-time JSON import.** On the *first* boot onto the DB — detected by an empty
   schema (no rows imported yet) and the presence of a legacy ``state.json`` /
   ``hosted_state.json`` — copy those records into the tables, then rename each JSON
   file to ``*.imported`` (kept, not deleted — the same conservatism as the JSON
   store's ``.bak``). An import error logs and leaves the JSON untouched; it never
   crashes boot and never half-imports (the import runs in one transaction).

Since issue 777, ``StateStore`` is keyed by ``instance_id``.  Legacy JSON files
are still keyed by project name, so ``import_legacy_json`` converts them via the
same deterministic UUID5 derivation the migration uses.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..hosted_state import HostedStateStore as JsonHostedStateStore
from ..state import StateStore as JsonStateStore
from .models import HostedSession, Instance
from .stores import HostedStateStore, StateStore

# Same namespace as migration 0003 — must match so restart sees the same key.
_NS = uuid.NAMESPACE_DNS


def _project_instance_id(project_name: str) -> str:
    """Derive the same deterministic instance_id migration 0003 would assign."""
    return str(uuid.uuid5(_NS, f"clauster.instance.{project_name}"))


_log = logging.getLogger("clauster.db.bootstrap")

# The packaged migration environment (env.py + versions/) lives beside this module.
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_ALEMBIC_INI = Path(__file__).resolve().parent / "alembic.ini"

# Sub-directory of state_dir holding pre-migration snapshots (#795).
_BACKUPS_DIRNAME = "backups"
# How many pre-migration snapshots to retain; older ones are pruned on each run.
_DEFAULT_SNAPSHOT_RETENTION = 5


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


def _pending_revision(connection: Connection) -> tuple[str | None, str | None]:
    """Return ``(current_revision, head_revision)`` without running any migration.

    ``current`` is read via :class:`~alembic.migration.MigrationContext` off the
    live connection (``None`` on a brand-new, unversioned database); ``head`` is
    the packaged :class:`~alembic.script.ScriptDirectory`'s head revision. Equal
    values mean the schema is already current — the plain-restart no-op case.
    """
    head = ScriptDirectory.from_config(_alembic_config(connection)).get_current_head()
    current = MigrationContext.configure(connection).get_current_revision()
    return current, head


def _prune_snapshots(backups_dir: Path, keep: int = _DEFAULT_SNAPSHOT_RETENTION) -> None:
    """Keep only the newest ``keep`` pre-migration snapshots, oldest-first pruned."""
    snapshots = sorted(backups_dir.glob("pre-*.db"))
    for old in snapshots[:-keep] if keep > 0 else snapshots:
        old.unlink(missing_ok=True)


def _snapshot_before_migrate(
    engine: Engine, state_dir: Path, current: str | None, head: str | None
) -> None:
    """Best-effort ``VACUUM INTO`` snapshot of the SQLite file before migrating.

    Never raises: a snapshot failure (disk full, unwritable ``state_dir``, ...) is
    logged as a WARNING and swallowed, since blocking startup over a backup write
    is the worse footgun — the migration itself is transactional and safe on its
    own (see module docstring). A non-SQLite or file-less engine (e.g. the
    in-memory engines some tests use) is silently skipped: there's no file to copy.
    """
    try:
        db_path_str = engine.url.database
        if not db_path_str or db_path_str == ":memory:":
            return
        db_path = Path(db_path_str)
        if not db_path.is_file():
            return
        backups_dir = state_dir.expanduser() / _BACKUPS_DIRNAME
        backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
        dest = backups_dir / f"pre-{current or 'none'}-{head or 'none'}-{stamp}.db"
        conn = sqlite3.connect(str(db_path))
        try:
            # Bound parameter for the target filename — sqlite's VACUUM INTO accepts
            # any expression there, so this avoids hand-quoting a path into SQL text.
            conn.execute("VACUUM INTO ?", (str(dest),))
        finally:
            conn.close()
        _prune_snapshots(backups_dir)
        _log.info("pre-migration snapshot written: %s", dest)
    except Exception as exc:  # noqa: BLE001 — best-effort; never blocks startup
        _log.warning(
            "pre-migration snapshot failed (%s); proceeding with migration without one", exc
        )


def upgrade_to_head(
    engine: Engine,
    state_dir: Path,
    *,
    backup_before_migrate: bool = True,
) -> None:
    """Bring the schema to ``head``, fail-closed, snapshotting first if pending.

    When the connection's current revision differs from the packaged head (#795),
    and ``backup_before_migrate`` is true (the default; wired to
    ``config.db.backup_before_migrate``), copies the live SQLite file to
    ``state_dir/backups/`` via ``VACUUM INTO`` before running Alembic — never on a
    plain restart already at head. The snapshot is best-effort and its failure
    never aborts startup (see :func:`_snapshot_before_migrate`).

    The migration itself still runs inside a single connection so ``env.py`` binds
    to it. Any migration failure is wrapped in :class:`MigrationError` — the
    caller must let it propagate and abort startup, never run on a
    partially-migrated database.
    """
    try:
        with engine.connect() as probe:
            current, head = _pending_revision(probe)
        if current != head and backup_before_migrate:
            _snapshot_before_migrate(engine, state_dir, current, head)
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
            raw_instances = JsonStateStore(state_dir).load() if state_path.exists() else {}
            # Legacy JSON is keyed by project name; convert to instance_id-keyed shape.
            instance_records = {
                _project_instance_id(project_name): {**fields, "project_name": project_name}
                for project_name, fields in raw_instances.items()
            }
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
