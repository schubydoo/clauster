"""Database persistence foundation (issue #362, persistence epic #308).

Replaces the lightweight ``state.json`` + ``hosted_state.json`` JSON stores with a
real SQLAlchemy 2.0 database (SQLite by default; Postgres-capable via URL) behind
the *exact same* ``load()`` / ``save()`` dict contract the JSON stores exposed —
so :mod:`clauster.runner` and :mod:`clauster.hosted` are unchanged.

Layout:

* :mod:`clauster.db.engine` — URL resolution + engine/session factory (SQLite
  PRAGMAs: WAL, foreign keys, busy-timeout).
* :mod:`clauster.db.models` — the SQLAlchemy 2.0 declarative tables for the
  foundation: projects, instances, hosted sessions.
* :mod:`clauster.db.stores` — DB-backed ``StateStore`` / ``HostedStateStore``
  preserving the JSON stores' ``dict[str, dict]`` API and fail-closed posture.
* :mod:`clauster.db.bootstrap` — fail-closed startup: run the Alembic baseline to
  ``head``, then a one-time import of any pre-existing JSON state.
"""

from __future__ import annotations
