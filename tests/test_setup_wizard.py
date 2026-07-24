"""Tests for the first-run setup wizard (#978, token-gate #1017).

Drives the setup app end-to-end (render, validate, write, re-exec wiring) in both modes — the
default loopback (Origin-checked) and the non-loopback token-gated mode — plus the
``clauster run`` glue that serves it when no config exists.
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


def test_setup_form_opts_non_credential_inputs_out_of_autofill(tmp_path):
    # #1036: EVERY non-password setup input opts out of autofill (per field); the two password
    # fields do NOT (a manager should still offer to save the new password).
    from conftest import audit_autofill

    _, client, _, _ = _app_and_paths(tmp_path)
    html = client.get("/").text
    missing, pw_optout = audit_autofill(html)
    assert not missing, f"non-credential setup inputs missing the opt-out: {missing}"
    assert not pw_optout, f"setup password fields wrongly opted out: {pw_optout}"
    assert 'data-lpignore="true"' in html  # the shared global rendered in this separate env


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


def test_write_creates_missing_parent_dirs(tmp_path):
    # A fresh `-c /opt/clauster/prod/clauster.yml` whose parents don't exist yet must still
    # complete setup (the wizard creates them) rather than 500 (#978 review).
    projects = tmp_path / "code"
    projects.mkdir()
    write_path = tmp_path / "new" / "nested" / "clauster.yml"
    client = TestClient(setup_wizard.create_setup_app(write_path, port=7621))
    assert client.post("/setup", json=_valid_payload(projects)).status_code == 200
    assert write_path.exists()


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


def test_loopback_form_has_no_setup_token_attribute(tmp_path):
    # Loopback mode (setup_token=None): the form carries no data-setup-token, so setup.js sends
    # no header — the flow is exactly as before the token gate existed.
    _, client, _, _ = _app_and_paths(tmp_path)
    assert "data-setup-token" not in client.get("/").text


# ----- token-gated (non-loopback) mode (#1017) -----------------------------

_TOKEN = "s3cret-setup-token"


def _token_app(tmp_path: Path):
    projects = tmp_path / "code"
    projects.mkdir(exist_ok=True)  # a test may build both a loopback and a token app in one tmp
    write_path = tmp_path / "clauster.yml"
    app = setup_wizard.create_setup_app(write_path, port=7621, setup_token=_TOKEN)
    return TestClient(app), projects, write_path


def test_token_mode_get_without_token_is_403_and_hides_form(tmp_path):
    client, _, _ = _token_app(tmp_path)
    res = client.get("/")
    assert res.status_code == 403
    assert 'id="setup-form"' not in res.text  # the form is never rendered without the token
    assert _TOKEN not in res.text  # the page never reflects/leaks the expected token


def test_token_mode_get_with_wrong_token_is_403(tmp_path):
    client, _, _ = _token_app(tmp_path)
    assert client.get("/", params={"token": "nope"}).status_code == 403


def test_token_mode_get_with_token_renders_form_carrying_token(tmp_path):
    client, _, _ = _token_app(tmp_path)
    res = client.get("/", params={"token": _TOKEN})
    assert res.status_code == 200
    assert f'data-setup-token="{_TOKEN}"' in res.text


def test_token_mode_submit_requires_header(tmp_path):
    client, projects, write_path = _token_app(tmp_path)
    res = client.post("/setup", json=_valid_payload(projects))
    assert res.status_code == 403
    assert "setup token" in res.json()["detail"]
    assert not write_path.exists()  # nothing written without the token


def test_token_mode_submit_wrong_header_rejected(tmp_path):
    client, projects, write_path = _token_app(tmp_path)
    res = client.post("/setup", headers={"x-setup-token": "wrong"}, json=_valid_payload(projects))
    assert res.status_code == 403
    assert not write_path.exists()


def test_token_mode_submit_with_header_writes_config_ignoring_origin(tmp_path):
    # The header is the CSRF defense in token mode, so a foreign Origin is irrelevant as long as
    # the token header is correct (the browser Origin is the operator's un-allowlistable LAN IP).
    client, projects, write_path = _token_app(tmp_path)
    res = client.post(
        "/setup",
        headers={"x-setup-token": _TOKEN, "origin": "http://192.168.1.50:7621"},
        json=_valid_payload(projects),
    )
    assert res.status_code == 200
    assert write_path.exists()


# ----- bind-field honesty vs CLAUSTER_HOST, and footer (#1017 review) -------


def _env_host_app(tmp_path: Path, env_host: str = "0.0.0.0"):
    projects = tmp_path / "code"
    projects.mkdir(exist_ok=True)
    write_path = tmp_path / "clauster.yml"
    app = setup_wizard.create_setup_app(write_path, port=7621, env_host=env_host)
    return TestClient(app), projects, write_path


def test_env_host_makes_bind_field_readonly_and_prefilled(tmp_path):
    # With CLAUSTER_HOST set the bind is env-controlled, so the field can't be a free choice the
    # re-exec would silently override — it renders read-only at the env value with an explanation.
    client, _, _ = _env_host_app(tmp_path, env_host="0.0.0.0")
    html = client.get("/").text
    assert 'value="0.0.0.0"' in html
    assert "readonly" in html
    assert "CLAUSTER_HOST" in html


def test_env_host_written_config_records_env_not_posted_host(tmp_path):
    # A stale/crafted posted host must not win: the app binds CLAUSTER_HOST regardless, so the
    # written config records that, never a value the runtime ignores (no control that lies).
    client, projects, write_path = _env_host_app(tmp_path, env_host="0.0.0.0")
    res = client.post("/setup", json=_valid_payload(projects, host="127.0.0.1"))
    assert res.status_code == 200
    assert load_config(write_path).host == "0.0.0.0"


def test_no_env_host_keeps_free_bind_choice(tmp_path):
    # Host install (no CLAUSTER_HOST): the field stays editable and the posted host is honored.
    _, client, projects, write_path = _app_and_paths(tmp_path)
    assert "readonly" not in client.get("/").text
    res = client.post("/setup", json=_valid_payload(projects, host="0.0.0.0"))
    assert res.status_code == 200
    assert load_config(write_path).host == "0.0.0.0"


def test_env_port_makes_field_readonly_and_config_records_it(tmp_path):
    # CLAUSTER_PORT overrides the file on re-exec exactly like CLAUSTER_HOST, so the port field
    # is read-only at the env value and a posted port is ignored in the written config.
    projects = tmp_path / "code"
    projects.mkdir()
    write_path = tmp_path / "clauster.yml"
    client = TestClient(setup_wizard.create_setup_app(write_path, port=7621, env_port=9000))
    html = client.get("/").text
    assert 'value="9000"' in html
    assert "readonly" in html
    assert "CLAUSTER_PORT" in html
    res = client.post("/setup", json=_valid_payload(projects, port=12345))
    assert res.status_code == 200
    assert load_config(write_path).port == 9000


def test_resolve_env_port_unset_is_none(monkeypatch):
    monkeypatch.delenv("CLAUSTER_PORT", raising=False)
    assert setup_wizard.resolve_env_port() is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("9000", 9000), ("  8080 ", 8080), ("", None), ("nope", None), ("0", None), ("70000", None)],
)
def test_resolve_env_port_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("CLAUSTER_PORT", raw)
    assert setup_wizard.resolve_env_port() == expected


def test_footer_reflects_exposure_mode(tmp_path):
    # Loopback footer says loopback; token mode says reachable/token-gated (not "Loopback-only").
    _, loopback_client, _, _ = _app_and_paths(tmp_path)
    assert "Loopback-only" in loopback_client.get("/").text
    token_client, _, _ = _token_app(tmp_path)
    html = token_client.get("/", params={"token": _TOKEN}).text
    assert "Loopback-only" not in html
    assert "Token-gated" in html


# ----- host / token policy helpers (#1017) ---------------------------------


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("[::1]", True),
        ("127.0.0.5", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.10", False),
        ("clauster.lan", False),  # unresolvable hostname → fail-safe non-loopback (token)
    ],
)
def test_is_loopback_host(host, loopback):
    assert setup_wizard._is_loopback_host(host) is loopback


def test_resolve_setup_host_env_and_default(monkeypatch):
    monkeypatch.delenv("CLAUSTER_SETUP_HOST", raising=False)
    assert setup_wizard.resolve_setup_host() == setup_wizard.SETUP_HOST
    monkeypatch.setenv("CLAUSTER_SETUP_HOST", "0.0.0.0")
    assert setup_wizard.resolve_setup_host() == "0.0.0.0"
    monkeypatch.setenv("CLAUSTER_SETUP_HOST", "  ")  # blank → falls back to loopback default
    assert setup_wizard.resolve_setup_host() == setup_wizard.SETUP_HOST


def test_mint_setup_token_only_for_non_loopback():
    assert setup_wizard.mint_setup_token("127.0.0.1") is None
    tok = setup_wizard.mint_setup_token("0.0.0.0")
    assert isinstance(tok, str) and len(tok) >= 32


def test_setup_url_appends_token_and_handles_wildcard():
    assert setup_wizard.setup_url("127.0.0.1", 7621) == "http://127.0.0.1:7621/"
    assert (
        setup_wizard.setup_url("192.168.1.9", 80, token="abc")
        == "http://192.168.1.9:80/?token=abc"
    )
    # A wildcard bind has no single host — the placeholder is rendered, the token still literal.
    wild = setup_wizard.setup_url("0.0.0.0", 7621, token="abc")
    assert "<this-host>" in wild and wild.endswith("?token=abc")


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
        # A NON-projects_root ValidationError (bad port) -> the generic branch, not the
        # projects_root field-error mapping (which test_nonexistent_projects_root covers).
        return orig({"projects_root": str(projects), "port": "not-an-int"})

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
