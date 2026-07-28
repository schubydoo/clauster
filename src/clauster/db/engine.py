"""Engine + session-factory construction for the persistence layer (#362).

Builds the SQLite URL under ``state_dir``, builds a SQLAlchemy 2.0
:class:`~sqlalchemy.engine.Engine`, and hands out a ``sessionmaker``. SQLite gets
the durability/concurrency PRAGMAs the JSON store's atomic-write posture implied:
write-ahead logging (concurrent reads during a write), enforced foreign keys (off
by default in SQLite), and a busy-timeout so a brief writer overlap waits instead
of raising ``database is locked``.

Clauster is SQLite-only (#796) — there is no remote-database substrate switch.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..atomicio import ensure_private_dir

_log = logging.getLogger("clauster.db.engine")

# Strong registry of every live engine, so a test can dispose the ones a bare
# ``TestClient(create_app(...))`` — or a throwaway ``SessionRunner(cfg)`` — left open
# (those paths skip the app lifespan that would call ``dispose()``, so the SQLite
# connections warn "unclosed database" on GC). This MUST hold strong refs, not weak:
# a throwaway ``Persistence`` is unreferenced the instant its expression ends, so a
# ``WeakSet`` drops it before test teardown and CPython's cyclic GC (3.13+) finalizes
# its connection mid-run — exactly the leak this guards. ``dispose_engine`` de-registers
# on every deliberate dispose, so prod (which builds ~one engine and disposes it on
# shutdown) never accumulates. See ``dispose_live_engines`` (test teardown disposes + clears).
_LIVE_ENGINES: set[Engine] = set()

# The on-disk SQLite file name under ``state_dir`` when no URL is configured. Sits
# beside ``state.json`` / ``hosted_state.json`` / ``session.secret`` in the 0700 dir.
DB_FILENAME = "clauster.db"

# Wait up to this long for a competing writer before raising "database is locked".
# Writes are tiny and rare (a poll-driven persist), so a brief overlap should block,
# not fail — mirroring the JSON store's "a write failure is best-effort" tolerance.
_SQLITE_BUSY_TIMEOUT_MS = 5000


def resolve_url(state_dir: Path) -> str:
    """Return the SQLAlchemy URL: SQLite under ``state_dir``.

    Builds a SQLite URL pointing at ``<state_dir>/clauster.db`` with the path
    resolved to absolute so it's stable regardless of the process working directory.
    """
    db_path = (state_dir.expanduser() / DB_FILENAME).resolve()
    # as_posix() so the URL uses forward slashes on every platform: on Windows a raw
    # str(path) is ``C:\...\clauster.db``, which yields a backslash URL that is both
    # OS-inconsistent and awkward for SQLAlchemy's sqlite dialect. ``sqlite:///C:/...``
    # is the portable form.
    return f"sqlite:///{db_path.as_posix()}"


def create_db_engine(state_dir: Path) -> Engine:
    """Build the SQLite engine for ``state_dir``.

    Registers a ``connect`` listener that sets WAL journaling, enforces foreign
    keys, and arms the busy-timeout on every pooled connection.
    """
    url = resolve_url(state_dir)
    # SQLite can't create the parent directory for its file; ensure it exists and
    # is 0700 first (it also holds session.secret) — the same posture the JSON
    # store got from atomicio.ensure_private_dir before its first write.
    ensure_private_dir(state_dir.expanduser())
    # The stores run synchronously inside ``asyncio.to_thread`` (runner/hosted
    # persist off-loop), so a pooled connection is checked out from a worker
    # thread. Set check_same_thread=False explicitly to make that contract
    # visible here rather than relying on the dialect's URL-shape default.
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False})
    _arm_sqlite_pragmas(engine)
    _LIVE_ENGINES.add(engine)
    return engine


def dispose_live_engines() -> None:
    """Dispose every still-live engine, then clear the registry — a test-teardown helper.

    Deterministically closes the SQLite connections that a bare
    ``TestClient(create_app(...))`` test — or a throwaway ``SessionRunner(cfg)`` — leaves
    open (those skip the app lifespan that would dispose the engine). ``dispose()`` is
    idempotent, so this is a no-op for an engine a ``with``-client already closed via
    lifespan shutdown. Clears the strong registry (:data:`_LIVE_ENGINES`) afterward so
    engines don't accumulate across the test session. See :data:`_LIVE_ENGINES`.
    """
    for engine in list(_LIVE_ENGINES):
        engine.dispose()
    _LIVE_ENGINES.clear()


def _arm_sqlite_pragmas(engine: Engine) -> None:
    """Apply WAL / foreign-keys / busy-timeout to every new SQLite connection.

    Registered as a ``connect`` event so a fresh pooled connection always carries
    the PRAGMAs — they are per-connection in SQLite, not persisted on the file.
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: object, _record: object) -> None:
        """Apply the durability/concurrency pragmas to each new SQLite connection."""
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            # SQLite silently refuses WAL on some filesystems (network/overlay mounts)
            # and stays in rollback-journal mode while the statement still "succeeds".
            # Surface that downgrade — the synchronous=NORMAL durability posture below
            # assumes WAL — rather than letting it pass unnoticed (fail-closed, loudly).
            mode = cursor.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode and str(mode[0]).lower() != "wal":
                _log.warning(
                    "SQLite WAL mode unavailable on this filesystem (got %r); "
                    "crash durability may be reduced",
                    mode[0],
                )
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            # NORMAL keeps WAL durable across app crashes (only a power loss can lose
            # the last transaction) while avoiding an fsync per commit — the same
            # crash-safe-but-fast posture the JSON store's fsync-then-replace gave.
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a ``sessionmaker`` bound to ``engine`` (the stores' session source)."""
    return sessionmaker(bind=engine, future=True, expire_on_commit=False)


def dispose_engine(engine: Engine) -> None:
    """Close all pooled connections and de-register the engine — call on app shutdown.

    De-registering from :data:`_LIVE_ENGINES` keeps the strong registry bounded: every
    deliberate dispose (app shutdown, ``Persistence.dispose``) drops its own entry, so a
    long-lived process that builds and disposes engines never accumulates them.
    """
    engine.dispose()
    _LIVE_ENGINES.discard(engine)
