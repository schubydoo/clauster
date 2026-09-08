"""Tests for the typed request-scoped accessors (#1156).

The ``Annotated[..., Depends(...)]`` aliases must inject exactly the object
``create_app`` put on ``app.state`` on BOTH an HTTP route and a WebSocket route,
``get_runner`` must fail closed when no runner is wired, and the accessors must
read the attribute names the real ``create_app`` actually sets.

This module keeps ``from __future__ import annotations`` on purpose: a real
``routes/*.py`` consumer uses it, so FastAPI resolves ``cfg: ConfigDep`` as a
string against this module's globals. That exercises the forward-reference path a
plain (non-future) test module would skip.
"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from clauster.app import create_app
from clauster.config import load_config
from clauster.dependencies import (
    ConfigDep,
    HostedDep,
    RunnerDep,
    get_clone_jobs,
    get_clone_tasks,
    get_config,
    get_engine,
    get_hosted,
    get_render,
    get_runner,
)


class _Marker:
    def __init__(self, name: str) -> None:
        self.name = name


class _ConnShim:
    """Minimal stand-in exposing only ``conn.app.state``, which is all the accessors read."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app


def test_deps_inject_on_an_http_route():
    config, runner = _Marker("config"), _Marker("runner")
    app = FastAPI()
    app.state.config = config
    app.state.runner = runner

    @app.get("/probe")
    def probe(cfg: ConfigDep, run: RunnerDep) -> dict:
        return {"config_ok": cfg is app.state.config, "runner_ok": run is app.state.runner}

    assert TestClient(app).get("/probe").json() == {"config_ok": True, "runner_ok": True}


def test_deps_inject_on_a_websocket_route():
    config, runner = _Marker("config"), _Marker("runner")
    app = FastAPI()
    app.state.config = config
    app.state.runner = runner

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, cfg: ConfigDep, run: RunnerDep) -> None:
        await websocket.accept()
        await websocket.send_json({"ok": cfg is app.state.config and run is app.state.runner})
        await websocket.close()

    with TestClient(app).websocket_connect("/ws") as conn:
        assert conn.receive_json() == {"ok": True}


def test_runner_dep_fails_closed_when_no_runner_is_wired():
    app = FastAPI()
    app.state.runner = None

    @app.get("/needs-runner")
    def needs_runner(run: RunnerDep) -> dict:
        return {"unreachable": True}

    resp = TestClient(app).get("/needs-runner")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "runner unavailable"


def test_runner_dep_fails_closed_when_runner_attribute_is_absent():
    app = FastAPI()  # never sets app.state.runner at all

    @app.get("/needs-runner")
    def needs_runner(run: RunnerDep) -> dict:
        return {"unreachable": True}

    assert TestClient(app).get("/needs-runner").status_code == 404


def test_hosted_dep_fails_closed_when_hosted_is_absent():
    app = FastAPI()  # never sets app.state.hosted at all

    @app.get("/needs-hosted")
    def needs_hosted(hosted: HostedDep) -> dict:
        return {"unreachable": True}

    resp = TestClient(app).get("/needs-hosted")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "hosted channel unavailable"


def test_accessors_read_the_real_create_app_state(write_config):
    app = create_app(load_config(write_config()))
    conn = cast(HTTPConnection, _ConnShim(app))
    assert get_config(conn) is app.state.config
    assert get_runner(conn) is app.state.runner
    assert get_hosted(conn) is app.state.hosted
    assert get_engine(conn) is app.state.engine
    assert get_clone_jobs(conn) is app.state.clone_jobs
    assert get_clone_tasks(conn) is app.state.clone_tasks
    # get_render returns the closure create_app published, not an app.state-typed object.
    assert get_render(conn) is app.state.render
