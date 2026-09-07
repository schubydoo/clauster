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
and FastAPI injects it per request.

Add a new accessor in the same PR that first moves a route needing it. Do not
import :mod:`clauster.app` here: once a router uses these, ``app`` imports this
module, so the reverse would be a cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request

if TYPE_CHECKING:
    from .config import ClausterConfig
    from .runner import SessionRunner


def get_config(request: Request) -> ClausterConfig:
    """Return the ClausterConfig stored on ``app.state`` at build time."""
    return request.app.state.config


def get_runner(request: Request) -> SessionRunner:
    """Return the SessionRunner from ``app.state``, or fail closed with a 404.

    ``create_app`` always wires a runner, so ``None`` happens only in a harness or
    CLI context that skipped the ``SessionRunner`` coercion. Failing closed here
    keeps a moved handler from dereferencing ``None`` into an unhandled 500 -- the
    same 404-invisible shape the config-write user-scope routes already use.
    """
    runner = request.app.state.runner
    if runner is None:
        raise HTTPException(status_code=404, detail="runner unavailable")
    return runner


ConfigDep = Annotated["ClausterConfig", Depends(get_config)]
RunnerDep = Annotated["SessionRunner", Depends(get_runner)]
