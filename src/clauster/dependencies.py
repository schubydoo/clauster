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
type that module never imported. None of :mod:`clauster.config`,
:mod:`clauster.runner`, :mod:`clauster.hosted`, :mod:`clauster.engine`,
:mod:`clauster.clone_jobs`, or :mod:`clauster.claustrum_daemon` imports
:mod:`clauster.app`, so these imports are cycle-free, and ``app`` already imports
all of them. Do not import :mod:`clauster.app` here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection
from starlette.responses import Response

from .claustrum_daemon import ClaustrumDaemon
from .clone_jobs import CloneJobManager
from .config import ClausterConfig
from .engine import ClausterEngine
from .hosted import HostedManager
from .runner import SessionRunner


def get_config(conn: HTTPConnection) -> ClausterConfig:
    """Return the ClausterConfig stored on ``app.state`` at build time."""
    return conn.app.state.config


def get_hosted(conn: HTTPConnection) -> HostedManager:
    """Return the HostedManager from ``app.state``, or fail closed with a 404.

    ``create_app`` sets it unconditionally, so an absent or ``None`` value happens
    only in a harness that skipped the wiring. Fails closed like :func:`get_runner`
    rather than dereferencing ``None`` into an unhandled 500.
    """
    hosted = getattr(conn.app.state, "hosted", None)
    if hosted is None:
        raise HTTPException(status_code=404, detail="hosted channel unavailable")
    return hosted


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


def get_engine(conn: HTTPConnection) -> ClausterEngine:
    """Return the ClausterEngine stored on ``app.state`` at build time.

    ``create_app`` publishes it right after construction so a moved handler can run
    the shared discovery facade (``engine.list_projects``) the ``create_app`` closure
    used to call directly.
    """
    return conn.app.state.engine


def get_render(conn: HTTPConnection) -> Callable[..., Response]:
    """Return the ``_render`` template helper published on ``app.state``.

    Unlike the object accessors this returns a callable, not an ``app.state``-typed
    value. ``create_app`` closes over a renderer and publishes it so a moved HTML
    route renders identically. The load-bearing effect for the current fragment
    route (``_project_row.html``) is the ``Cache-Control: no-store`` header the
    renderer sets; the per-request CSP nonce it also injects is inherited but unused
    by that template, which carries no inline ``<script>``.
    """
    return conn.app.state.render


def get_clone_jobs(conn: HTTPConnection) -> CloneJobManager:
    """Return the CloneJobManager stored on ``app.state`` at build time."""
    return conn.app.state.clone_jobs


def get_clone_tasks(conn: HTTPConnection) -> set[asyncio.Task]:
    """Return the in-flight clone-task ref set published on ``app.state``.

    ``create_app`` holds strong refs to running clone tasks in this set so they are
    not garbage-collected mid-run; a moved clone route adds to it through here.
    """
    return conn.app.state.clone_tasks


def get_claustrum_daemon(conn: HTTPConnection) -> ClaustrumDaemon | None:
    """Return the ClaustrumDaemon from ``app.state``, or ``None`` when unwired.

    ``create_app`` publishes ``None`` at build time and the lifespan swaps in the
    live daemon only when ``claustrum.enabled``. Unlike :func:`get_runner` this does
    not fail closed: the hosted spawn/resume path treats a missing daemon (or a
    daemon whose ``client`` is not yet connected) as a 503 with its own message, so
    the accessor hands back the raw ``daemon | None`` and leaves that decision to the
    handler. ``getattr`` guards a harness that never set the attribute at all.
    """
    return getattr(conn.app.state, "claustrum_daemon", None)


ConfigDep = Annotated[ClausterConfig, Depends(get_config)]
HostedDep = Annotated[HostedManager, Depends(get_hosted)]
RunnerDep = Annotated[SessionRunner, Depends(get_runner)]
EngineDep = Annotated[ClausterEngine, Depends(get_engine)]
RenderDep = Annotated[Callable[..., Response], Depends(get_render)]
CloneJobsDep = Annotated[CloneJobManager, Depends(get_clone_jobs)]
CloneTasksDep = Annotated[set[asyncio.Task], Depends(get_clone_tasks)]
ClaustrumDaemonDep = Annotated[ClaustrumDaemon | None, Depends(get_claustrum_daemon)]
