"""Tests for the first-run setup wizard (#978).

Drives the loopback-only setup app end-to-end (render, validate, write, re-exec wiring) and
the ``clauster run`` glue that serves it when no config exists.
"""

from __future__ import annotations

import os
import stat
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import auth, setup_wizard
from clauster.config import first_config_path, load_config

PASSWORD = "hunter2secret"


def _app_and_paths(tmp_path: Path):
    projects = tmp_path / "code"
    projects.mkdir()
    write_path = tmp_path / "clauster.yml"
    app = setup_wizard.create_setup_app(write_path, port=7621)
    return app, TestClient(app), projects, write_path


def _valid_payload(projects: Path, **over) -> dict:
    body = {
        "projects_root": str(projects),
        "host": "127.0.0.1",
        "port": 7621,
        "password": PASSWORD,
        "confirm": PASSWORD,
    }
    body.update(over)
    return body


# ----- render --------------------------------------------------------------


def test_get_renders_form_with_csp(tmp_path):
    _, client, _, _ = _app_and_paths(tmp_path)
    res = client.get("/")
    assert res.status_code == 200
    assert 'id="setup-form"' in res.text
    csp = res.headers["Content-Security-Policy"]
    assert "script-src 'self' 'nonce-" in csp
    assert res.headers["X-Frame-Options"] == "DENY"


def test_healthz(tmp_path):
    _, client, _, _ = _app_and_paths(tmp_path)
    assert client.get("/healthz").json() == {"status": "setup"}


# ----- happy path ----------------------------------------------------------


def test_valid_submit_writes_auth_enabled_config(tmp_path):
    app, client, projects, write_path = _app_and_paths(tmp_path)
    app.state.uvicorn_server = types.SimpleNamespace(should_exit=False)
    res = client.post("/setup", json=_valid_payload(projects))
    assert res.status_code == 200
    assert res.json() == {"ok": True, "url": "http://127.0.0.1:7621/"}
    # The completion flag + the wired server's shutdown request drive the caller's re-exec.
    assert app.state.setup_complete is True
    assert app.state.uvicorn_server.should_exit is True
    # The written config loads, has auth enabled, and the password verifies against the hash.
    cfg = load_config(write_path)
    assert cfg.auth.enabled and cfg.auth.password_required
    assert cfg.projects_root == projects
    assert auth.verify_password(auth.make_hasher(), cfg.auth.password_hash, PASSWORD)
    # The plaintext password is never written to disk.
    assert PASSWORD not in write_path.read_text(encoding="utf-8")


def test_atomic_write_config_cleans_up_temp_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "clauster.yml"

    def _boom(*a, **k):
        raise OSError("replace failed")

    monkeypatch.setattr(setup_wizard.os, "replace", _boom)
    with pytest.raises(OSError):
        setup_wizard._atomic_write_config(target, "content")
    assert not target.exists()
    assert not list(tmp_path.glob("clauster.yml.*.tmp"))  # the temp file was removed


def test_second_submit_after_completion_conflicts(tmp_path):
    app, client, projects, _ = _app_and_paths(tmp_path)
    assert client.post("/setup", json=_valid_payload(projects)).status_code == 200
    # A second submit after completion is rejected — guards last-writer-wins credential lockout.
    res = client.post(
        "/setup",
        json=_valid_payload(projects, password="another-secret", confirm="another-secret"),
    )
    assert res.status_code == 409


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_write_keeps_parent_dir_permissions(tmp_path):
    # The wizard must NOT tighten its target's parent to 0700 (that would lock other users
    # out of a shared dir like the cwd); only the config file itself is private (#978 review).
    projects = tmp_path / "code"
    projects.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)  # noqa: S103 - deliberately shared, to prove the wizard won't tighten it
    write_path = shared / "clauster.yml"
    client = TestClient(setup_wizard.create_setup_app(write_path, port=7621))
    assert client.post("/setup", json=_valid_payload(projects)).status_code == 200
    assert stat.S_IMODE(os.stat(shared).st_mode) == 0o755  # parent untouched
    assert stat.S_IMODE(os.stat(write_path).st_mode) == 0o600  # file private (holds the hash)


def test_wildcard_host_url_has_no_single_host(tmp_path):
    _, client, projects, _ = _app_and_paths(tmp_path)
    res = client.post("/setup", json=_valid_payload(projects, host="0.0.0.0"))
    assert res.status_code == 200
    assert res.json()["url"] == "http://<this-host>:7621/"


# ----- validation ----------------------------------------------------------


def test_missing_projects_root(tmp_path):
    _, client, projects, _ = _app_and_paths(tmp_path)
    res = client.post("/setup", json=_valid_payload(projects, projects_root=""))
    assert res.status_code == 400
    assert "projects_root" in res.json()["errors"]


def test_nonexistent_projects_root(tmp_path):
    _, client, projects, _ = _app_and_paths(tmp_path)
    res = client.post(
        "/setup", json=_valid_payload(projects, projects_root=str(tmp_path / "nope"))
    )
    assert res.status_code == 400
    assert "projects_root" in res.json()["errors"]


def test_short_password(tmp_path):
    _, client, projects, _ = _app_and_paths(tmp_path)
    res = client.post("/setup", json=_valid_payload(projects, password="short", confirm="short"))
    assert res.status_code == 400
    assert "password" in res.json()["errors"]


def test_password_mismatch(tmp_path):
    _, client, projects, _ = _app_and_paths(tmp_path)
    res = client.post("/setup", json=_valid_payload(projects, confirm="different-value"))
    assert res.status_code == 400
    assert "confirm" in res.json()["errors"]


@pytest.mark.parametrize("bad_port", ["abc", 0, 70000])
def test_bad_port(tmp_path, bad_port):
    _, client, projects, _ = _app_and_paths(tmp_path)
    res = client.post("/setup", json=_valid_payload(projects, port=bad_port))
    assert res.status_code == 400
    assert "port" in res.json()["errors"]


def test_non_dict_body_is_all_errors(tmp_path):
    _, client, _, _ = _app_and_paths(tmp_path)
    res = client.post("/setup", json=["not", "a", "dict"])
    assert res.status_code == 400
    assert "projects_root" in res.json()["errors"]


def test_malformed_json_body(tmp_path):
    _, client, _, _ = _app_and_paths(tmp_path)
    res = client.post("/setup", content=b"not json", headers={"content-type": "application/json"})
    assert res.status_code == 400


# ----- CSRF / write failure ------------------------------------------------


def test_cross_origin_submit_rejected(tmp_path):
    _, client, projects, write_path = _app_and_paths(tmp_path)
    res = client.post(
        "/setup", headers={"origin": "https://evil.example"}, json=_valid_payload(projects)
    )
    assert res.status_code == 403
    assert not write_path.exists()  # nothing written on a rejected origin


def test_loopback_origin_accepted(tmp_path):
    _, client, projects, _ = _app_and_paths(tmp_path)
    res = client.post(
        "/setup", headers={"origin": "http://localhost:7621"}, json=_valid_payload(projects)
    )
    assert res.status_code == 200


def test_write_failure_is_500(tmp_path, monkeypatch):
    _, client, projects, _ = _app_and_paths(tmp_path)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(setup_wizard, "_atomic_write_config", _boom)
    res = client.post("/setup", json=_valid_payload(projects))
    assert res.status_code == 500
    assert "could not write" in res.json()["detail"]
    # The raw OSError (which can carry the target path) is never surfaced.
    assert "disk full" not in res.json()["detail"]


def test_model_validate_gate_rejects(tmp_path, monkeypatch):
    # Fail-closed belt: if the generated config wouldn't construct, nothing is written.
    _, client, projects, write_path = _app_and_paths(tmp_path)
    orig = setup_wizard.ClausterConfig.model_validate

    def _reject(data):
        return orig({})  # missing projects_root -> a real ValidationError

    monkeypatch.setattr(setup_wizard.ClausterConfig, "model_validate", staticmethod(_reject))
    res = client.post("/setup", json=_valid_payload(projects))
    assert res.status_code == 400
    assert "could not be validated" in res.json()["detail"]
    # The raw exception (which can carry internal paths) is never surfaced.
    assert "ValidationError" not in res.json()["detail"]
    assert not write_path.exists()


# ----- config helper -------------------------------------------------------


def test_first_config_path_prefers_explicit(tmp_path):
    explicit = tmp_path / "custom.yml"
    assert first_config_path(explicit) == explicit


def test_first_config_path_defaults_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUSTER_CONFIG", raising=False)
    assert first_config_path(None) == tmp_path / "clauster.yml"


# ----- clauster run glue ---------------------------------------------------


def test_run_routes_to_wizard_when_no_config(tmp_path, monkeypatch):
    from clauster import __main__ as m

    seen = {}
    monkeypatch.setattr(m, "_run_setup_wizard", lambda cp: seen.setdefault("cp", cp) or 0)
    monkeypatch.chdir(tmp_path)  # empty dir -> no clauster.yml found
    monkeypatch.delenv("CLAUSTER_CONFIG", raising=False)
    monkeypatch.delenv("CLAUSTER_HOME", raising=False)
    assert m._run(None) == 0
    assert "cp" in seen  # routed to the wizard rather than erroring


def test_wizard_reexecs_after_completion(tmp_path, monkeypatch):
    from clauster import __main__ as m

    reexeced = {}
    monkeypatch.setattr(m, "_reexec", lambda: reexeced.setdefault("yes", True))

    class _FakeServer:
        def __init__(self, config):
            self.config = config

        def run(self):  # simulate the operator completing setup
            self.config.app.state.setup_complete = True

    monkeypatch.setattr(m.uvicorn, "Config", lambda app, **kw: types.SimpleNamespace(app=app))
    monkeypatch.setattr(m.uvicorn, "Server", _FakeServer)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUSTER_CONFIG", raising=False)
    monkeypatch.delenv("CLAUSTER_HOME", raising=False)
    assert m._run_setup_wizard(None) == 0
    assert reexeced.get("yes")


def test_setup_port_override(tmp_path, monkeypatch):
    from clauster import __main__ as m

    captured = {}
    monkeypatch.setattr(m, "_reexec", lambda: None)

    class _FakeServer:
        def __init__(self, config):
            self.config = config

        def run(self):
            pass

    def _fake_config(app, **kw):
        captured["port"] = kw.get("port")
        return types.SimpleNamespace(app=app)

    monkeypatch.setattr(m.uvicorn, "Config", _fake_config)
    monkeypatch.setattr(m.uvicorn, "Server", _FakeServer)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUSTER_CONFIG", raising=False)
    monkeypatch.delenv("CLAUSTER_HOME", raising=False)

    monkeypatch.setenv("CLAUSTER_SETUP_PORT", "9099")
    m._run_setup_wizard(None)
    assert captured["port"] == 9099  # honored the override

    monkeypatch.setenv("CLAUSTER_SETUP_PORT", "not-a-number")
    m._run_setup_wizard(None)
    assert captured["port"] == setup_wizard.DEFAULT_PORT  # bad value -> default, no crash

    monkeypatch.setenv("CLAUSTER_SETUP_PORT", "70000")
    m._run_setup_wizard(None)
    assert (
        captured["port"] == setup_wizard.DEFAULT_PORT
    )  # out of range -> default (won't reach bind)
