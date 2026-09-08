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

from clauster import auth
from clauster.app import create_app
from clauster.config import load_config
from clauster.dependencies import (
    ConfigDep,
    HostedDep,
    RunnerDep,
    get_allowed_origins,
    get_authenticate,
    get_claustrum_daemon,
    get_clone_jobs,
    get_clone_tasks,
    get_config,
    get_cookie_secure,
    get_elevation_serializer,
    get_engine,
    get_hosted,
    get_login_serializer,
    get_login_shepherd,
    get_login_status_cache,
    get_login_throttle,
    get_password_hasher,
    get_render,
    get_require_elevated,
    get_runner,
    get_runner_or_none,
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


def test_runner_or_none_returns_none_when_unwired():
    # Unlike get_runner, the nullable accessor hands back None rather than raising, so a
    # moved permissions/hooks route keeps the capability check first and only the
    # user-scope branch (via user_settings_json) fail-closes on the absent runner (#1156).
    absent = FastAPI()  # never sets app.state.runner at all
    assert get_runner_or_none(cast(HTTPConnection, _ConnShim(absent))) is None
    nulled = FastAPI()
    nulled.state.runner = None
    assert get_runner_or_none(cast(HTTPConnection, _ConnShim(nulled))) is None


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
    # #1156 config-write A: the nullable runner accessor the moved permissions/hooks
    # routes inject reads the same live app.state.runner get_runner does.
    assert get_runner_or_none(conn) is app.state.runner
    assert get_hosted(conn) is app.state.hosted
    assert get_engine(conn) is app.state.engine
    assert get_clone_jobs(conn) is app.state.clone_jobs
    assert get_clone_tasks(conn) is app.state.clone_tasks
    # claustrum_daemon is published as None at build time (the lifespan swaps in the live
    # daemon only when claustrum.enabled); the accessor still reads the same attribute.
    assert get_claustrum_daemon(conn) is app.state.claustrum_daemon
    # get_render returns the closure create_app published, not an app.state-typed object.
    assert get_render(conn) is app.state.render
    # #1156 ops domain: the login-status cache and the two published closures
    # (_authenticate, require_elevated) the moved /healthz + Tier-B routes read.
    assert get_login_status_cache(conn) is app.state.login_status_cache
    assert get_authenticate(conn) is app.state.authenticate
    assert get_require_elevated(conn) is app.state.require_elevated
    # #1156 websockets domain: the moved WS gate reads the same Origin allowlist
    # (built once from the auth config) the in-app HTTP CSRF gate uses.
    assert get_allowed_origins(conn) is app.state.allowed_origins
    assert app.state.allowed_origins == auth.build_allowed_origins(app.state.config)
    # #1156 login domain: the auth serializers/hasher/throttle and the _cookie_secure
    # closure stay built in create_app (the middleware + _authenticate/require_elevated
    # closures read them) and are injected into the moved login/logout/reauth routes.
    assert get_login_serializer(conn) is app.state.login_serializer
    assert get_elevation_serializer(conn) is app.state.elevation_serializer
    assert get_login_throttle(conn) is app.state.login_throttle
    assert get_password_hasher(conn) is app.state.password_hasher
    assert get_cookie_secure(conn) is app.state.cookie_secure
    assert get_login_shepherd(conn) is app.state.login_shepherd
