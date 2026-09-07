"""Typed request-scoped accessors for objects that ``create_app`` builds (#1156).

``create_app`` in :mod:`clauster.app` stores its long-lived collaborators on
``app.state``. When a route handler lived inside the ``create_app`` closure it
read those collaborators as free variables, and pyright checked the access at
definition. A handler moved into a :class:`fastapi.APIRouter` module can no
longer close over them, and ``app.state`` is typed ``Any`` -- so a bare
``request.app.state.runner`` loses that static check.

Each accessor here reads one ``app.state`` object and returns it with a concrete
type, so a moved handler keeps the typing the closure gave it. Declare the
matching ``Annotated`` alias as a parameter, for example ``runner: RunnerDep``,
and FastAPI injects it per request. The accessors take
:class:`~starlette.requests.HTTPConnection`, the shared base of ``Request`` and
``WebSocket``, so the same alias injects on an HTTP route and a WebSocket route
alike. Dependencies resolve before ``accept()``, so a dependency
``HTTPException`` on a WebSocket denies the handshake with that HTTP status
(a 404 body), not a 1008 close.

``ClausterConfig`` and ``SessionRunner`` are imported at runtime (not under
``TYPE_CHECKING``) on purpose: the ``Annotated`` aliases must hold the real class
objects, not string forward references. A ``routes/*.py`` module uses
``from __future__ import annotations``, so FastAPI resolves ``cfg: ConfigDep``
against that module's globals; a forward reference inside the alias would name a
type that module never imported. Neither :mod:`clauster.config` nor
:mod:`clauster.runner` imports :mod:`clauster.app`, so these imports are
cycle-free, and ``app`` already imports both. Do not import :mod:`clauster.app`
here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection

from .config import ClausterConfig
from .runner import SessionRunner


def get_config(conn: HTTPConnection) -> ClausterConfig:
    """Return the ClausterConfig stored on ``app.state`` at build time."""
    return conn.app.state.config


def get_runner(conn: HTTPConnection) -> SessionRunner:
    """Return the SessionRunner from ``app.state``, or fail closed with a 404.

    ``create_app`` always wires a runner, so an absent or ``None`` value happens
    only in a harness or CLI context that skipped the ``SessionRunner`` coercion.
    Failing closed here keeps a moved handler from dereferencing ``None`` into an
    unhandled 500 -- the same 404-invisible shape the config-write user-scope
    routes already use. On a WebSocket route this denies the handshake with a 404
    (dependencies resolve before ``accept()``), never a 500.
    """
    runner = getattr(conn.app.state, "runner", None)
    if runner is None:
        raise HTTPException(status_code=404, detail="runner unavailable")
    return runner


ConfigDep = Annotated[ClausterConfig, Depends(get_config)]
RunnerDep = Annotated[SessionRunner, Depends(get_runner)]
