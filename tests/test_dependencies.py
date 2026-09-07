"""Tests for the typed request-scoped accessors (#1156).

The ``Annotated[..., Depends(...)]`` aliases must inject exactly the object
``create_app`` put on ``app.state``, ``get_runner`` must fail closed when no
runner is wired, and the accessors must read the attribute names the real
``create_app`` actually sets.
"""

from typing import cast

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import load_config
from clauster.dependencies import ConfigDep, RunnerDep, get_config, get_runner


class _Marker:
    def __init__(self, name: str) -> None:
        self.name = name


class _ReqShim:
    """Minimal stand-in exposing only ``request.app.state``, which is all the accessors read."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app


def test_deps_inject_the_app_state_objects():
    config, runner = _Marker("config"), _Marker("runner")
    app = FastAPI()
    app.state.config = config
    app.state.runner = runner

    @app.get("/probe")
    def probe(cfg: ConfigDep, run: RunnerDep) -> dict:
        return {"config_ok": cfg is app.state.config, "runner_ok": run is app.state.runner}

    assert TestClient(app).get("/probe").json() == {"config_ok": True, "runner_ok": True}


def test_runner_dep_fails_closed_when_no_runner_is_wired():
    app = FastAPI()
    app.state.runner = None

    @app.get("/needs-runner")
    def needs_runner(run: RunnerDep) -> dict:
        return {"unreachable": True}

    resp = TestClient(app).get("/needs-runner")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "runner unavailable"


def test_accessors_read_the_real_create_app_state(write_config):
    app = create_app(load_config(write_config()))
    request = cast(Request, _ReqShim(app))
    assert get_config(request) is app.state.config
    assert get_runner(request) is app.state.runner
