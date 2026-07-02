"""The persistence container — one engine, migrated + imported, shared by stores.

:class:`Persistence` is built once per process. Its constructor is the fail-closed
startup the maintainer specified: build the engine, run the Alembic baseline to
``head`` (refusing to start on failure), then a one-time import of any legacy JSON
state. After that it hands out the DB-backed :class:`StateStore` /
:class:`HostedStateStore`, which expose the same ``load()`` / ``save()`` dict
contract the JSON stores did — so :mod:`clauster.runner` and
:mod:`clauster.hosted` are unchanged.

Built inside :class:`clauster.runner.SessionRunner` (the first component
constructed) and reused by the app for the hosted store, so the whole process
shares a single engine and a single migration run.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from .bootstrap import import_legacy_json, upgrade_to_head
from .engine import create_db_engine, dispose_engine, make_session_factory
from .stores import ApiTokenStore, HostedStateStore, SessionHistoryStore, StateStore


class Persistence:
    """Owns the engine + session factory and the migrated, imported database."""

    def __init__(self, state_dir: Path, *, backup_before_migrate: bool = True) -> None:
        """Build the engine, migrate to head (fail-closed), and import legacy JSON.

        Raises :class:`clauster.db.bootstrap.MigrationError` if the schema can't be
        brought to head — the caller must let it propagate so the app refuses to
        start rather than run on a half-migrated database.

        ``backup_before_migrate`` (wired to ``config.db.backup_before_migrate``,
        default on) gates the pre-migration SQLite snapshot (#795): when a
        migration is actually pending, the live database is copied to
        ``state_dir/backups/`` before Alembic runs. It is a no-op on a plain
        restart already at head.

        Runs the (possibly multi-second) Alembic migration synchronously, so it must
        be constructed *off* the event loop — as ``create_app`` → ``SessionRunner``
        does today, before ``uvicorn.run``. Don't move construction into an
        already-running loop without offloading it, or the migration blocks the loop.
        """
        self._engine: Engine = create_db_engine(state_dir)
        try:
            upgrade_to_head(self._engine, state_dir, backup_before_migrate=backup_before_migrate)
            self._session_factory: sessionmaker[Session] = make_session_factory(self._engine)
            # One-time, fail-closed: import a pre-existing JSON state on the first boot.
            # The returned "did import" bool is informational and logged inside the call.
            import_legacy_json(state_dir, self._session_factory)
        except BaseException:
            # A failed startup step (e.g. MigrationError) still propagates fail-closed, but
            # dispose the just-built engine first so its connection pool isn't leaked when
            # construction raises before the caller ever receives the object to dispose().
            dispose_engine(self._engine)
            raise

    @property
    def session_factory(self) -> sessionmaker[Session]:
        """The shared session factory both stores bind to."""
        return self._session_factory

    def state_store(self) -> StateStore:
        """Return a DB-backed :class:`StateStore` (per-project bridge intent)."""
        return StateStore(self._session_factory)

    def hosted_state_store(self) -> HostedStateStore:
        """Return a DB-backed :class:`HostedStateStore` (hosted-channel sessions)."""
        return HostedStateStore(self._session_factory)

    def session_history_store(self) -> SessionHistoryStore:
        """Return a DB-backed :class:`SessionHistoryStore` (session-event history, #363)."""
        return SessionHistoryStore(self._session_factory)

    def api_token_store(self) -> ApiTokenStore:
        """Return a DB-backed :class:`ApiTokenStore` (named public-API tokens, #302)."""
        return ApiTokenStore(self._session_factory)

    def dispose(self) -> None:
        """Close the engine's connection pool (call on app shutdown)."""
        dispose_engine(self._engine)
