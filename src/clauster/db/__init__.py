"""Database persistence foundation (issue #362, persistence epic #308).

Replaces the lightweight ``state.json`` + ``hosted_state.json`` JSON stores with a
real SQLAlchemy 2.0 database (SQLite; #796 committed to SQLite-only) behind the
*exact same* ``load()`` / ``save()`` dict contract the JSON stores exposed — so
:mod:`clauster.runner` and :mod:`clauster.hosted` are unchanged.

Layout:

* :mod:`clauster.db.engine` — URL resolution + engine/session factory (SQLite
  PRAGMAs: WAL, foreign keys, busy-timeout).
* :mod:`clauster.db.models` — the SQLAlchemy 2.0 declarative tables: projects,
  instances, hosted sessions, and the append-only session-event history (#363).
* :mod:`clauster.db.stores` — DB-backed ``StateStore`` / ``HostedStateStore``
  preserving the JSON stores' ``dict[str, dict]`` API and fail-closed posture,
  plus ``SessionHistoryStore`` for the session lifecycle / event history (#363).
* :mod:`clauster.db.bootstrap` — fail-closed startup: run the Alembic baseline to
  ``head``, then a one-time import of any pre-existing JSON state.
"""

from __future__ import annotations
