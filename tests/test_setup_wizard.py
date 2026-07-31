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


def test_token_mode_non_ascii_token_is_403_not_500(tmp_path):
    # secrets.compare_digest raises TypeError on a non-ASCII str, so `?token=%C3%A9` used to
    # escape as a 500 — an unhandled raise on attacker-controlled input, on the ONE surface
    # with no auth in front of it (the first-run wizard binds non-loopback in the Docker
    # image and gates a config writer). Never a bypass; it 500'd rather than granting. The
    # gate must be total on str: every input returns a bool.
    client, _, _ = _token_app(tmp_path)
    assert client.get("/", params={"token": "é"}).status_code == 403


def test_token_mode_non_ascii_submit_header_is_403_not_500(tmp_path):
    # The same gate fronts the POST CSRF check, so both entry points are affected.
    # The header value is sent as latin-1 BYTES on purpose: HTTP headers are latin-1 on the
    # wire and httpx refuses to encode a non-ASCII str, but Starlette decodes the bytes back
    # to a non-ASCII str — so this is reachable by any client that isn't httpx, which is
    # exactly the shape that matters for an unauthenticated surface.
    client, projects, write_path = _token_app(tmp_path)
    res = client.post(
        "/setup",
        headers={b"x-setup-token": "é".encode("latin-1")},
        json=_valid_payload(projects),
    )
    assert res.status_code == 403
    assert not write_path.exists()  # nothing written


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


# ----- the post-setup login must actually work (#1071) ----------------------


def test_wildcard_bind_records_browser_origin_so_login_is_reachable(tmp_path):
    # THE #1071 regression, asserted the way the operator experiences it: complete setup on a
    # 0.0.0.0 bind (what the Docker image forces) and the very next request — the login POST —
    # must clear the Origin gate. Before the fix the wizard wrote no allowed_origins,
    # build_allowed_origins returned an EMPTY set for a non-loopback bind, and login 403'd
    # "origin check failed" with the dashboard permanently unreachable.
    browser_origin = "http://localhost:7621"
    client, projects, write_path = _env_host_app(tmp_path, env_host="0.0.0.0")
    res = client.post("/setup", headers={"origin": browser_origin}, json=_valid_payload(projects))
    assert res.status_code == 200

    config = load_config(write_path)
    assert config.host == "0.0.0.0"
    assert auth.normalize_origin(browser_origin) in auth.build_allowed_origins(config)


def test_loopback_bind_still_records_no_allowed_origins(tmp_path):
    # A loopback install must be untouched: build_allowed_origins already auto-allows
    # 127.0.0.1/localhost/[::1] at the bound port, so writing the key would be redundant noise
    # in the generated file.
    _, client, projects, write_path = _app_and_paths(tmp_path)
    res = client.post(
        "/setup", headers={"origin": "http://localhost:7621"}, json=_valid_payload(projects)
    )
    assert res.status_code == 200
    assert load_config(write_path).auth.allowed_origins == []
    assert "allowed_origins" not in write_path.read_text(encoding="utf-8")


def test_wildcard_bind_without_origin_records_nothing(tmp_path):
    # A scripted submit (curl/compose provisioning) carries no Origin. There is nothing to
    # record and nothing may be guessed — the operator sets allowed_origins themselves. Fails
    # closed and visibly rather than widening the allowlist to something invented.
    client, projects, write_path = _env_host_app(tmp_path, env_host="0.0.0.0")
    assert client.post("/setup", json=_valid_payload(projects)).status_code == 200
    assert load_config(write_path).auth.allowed_origins == []


def _token_env_host_app(tmp_path: Path, env_host: str = "0.0.0.0"):
    """The real container shape: wizard itself bound non-loopback (token-gated) AND the app
    re-execing onto a non-loopback CLAUSTER_HOST. Only here is a LAN/any Origin actually
    reachable — in loopback mode the wizard's own Origin gate 403s it first."""
    projects = tmp_path / "code"
    projects.mkdir(exist_ok=True)
    write_path = tmp_path / "clauster.yml"
    app = setup_wizard.create_setup_app(
        write_path, port=7621, setup_token=_TOKEN, env_host=env_host
    )
    return TestClient(app), projects, write_path


def test_token_mode_records_lan_origin_so_login_is_reachable(tmp_path):
    # The container case end to end: the operator reaches the wizard at a LAN address that
    # could never be pre-allowlisted, the token header authorizes the submit, and the origin
    # they actually used is what gets recorded — so the login POST that follows is accepted.
    browser_origin = "http://192.168.1.50:7621"
    client, projects, write_path = _token_env_host_app(tmp_path)
    res = client.post(
        "/setup",
        headers={"x-setup-token": _TOKEN, "origin": browser_origin},
        json=_valid_payload(projects),
    )
    assert res.status_code == 200
    assert auth.normalize_origin(browser_origin) in auth.build_allowed_origins(
        load_config(write_path)
    )


def test_recorded_origin_uses_the_bind_port_not_the_wizard_port(tmp_path):
    # The wizard serves on its own port, and with CLAUSTER_PORT unset the operator can edit
    # the bind port on the form. Recording the port the Origin carries would then write an
    # allowlist entry that is known-wrong — the app binds :8000, the browser sends :8000,
    # and the login POST 403s exactly as #1071 describes. The host is the operator's; the
    # port must be the one the app will actually serve.
    projects = tmp_path / "code"
    projects.mkdir()
    write_path = tmp_path / "clauster.yml"
    app = setup_wizard.create_setup_app(
        write_path, port=7621, setup_token=_TOKEN, env_host="0.0.0.0"
    )
    client = TestClient(app)
    res = client.post(
        "/setup",
        headers={"x-setup-token": _TOKEN, "origin": "http://nas.local:7621"},
        json=_valid_payload(projects, port=8000),
    )
    assert res.status_code == 200

    config = load_config(write_path)
    assert config.port == 8000
    assert config.auth.allowed_origins == ["http://nas.local:8000"]
    # The property that actually matters: the URL the operator will browse is accepted.
    assert auth.normalize_origin("http://nas.local:8000") in auth.build_allowed_origins(config)


def test_recorded_origin_keeps_ipv6_literal_bracketed(tmp_path):
    # urlsplit strips the brackets off an IPv6 host, so rebuilding the origin has to put
    # them back — otherwise it normalizes to the malformed `http://::1` and matches nothing.
    projects = tmp_path / "code"
    projects.mkdir()
    write_path = tmp_path / "clauster.yml"
    app = setup_wizard.create_setup_app(
        write_path, port=7621, setup_token=_TOKEN, env_host="fd00::1"
    )
    res = TestClient(app).post(
        "/setup",
        headers={"x-setup-token": _TOKEN, "origin": "http://[fd00::2]:7621"},
        json=_valid_payload(projects),
    )
    assert res.status_code == 200
    assert load_config(write_path).auth.allowed_origins == ["http://[fd00::2]:7621"]


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-origin",
        "file:///etc/passwd",
        "://x",
        "javascript:x",
        "http://:80",  # hostless
        "http://a.com,http",  # comma-mangled host
        # Unterminated / empty IPv6 brackets. normalize_origin cannot parse these either,
        # and hands the string back unchanged — so they reach the second urlsplit, which
        # raises ValueError("Invalid IPv6 URL"). Unguarded that 500s the submit BEFORE the
        # config is written, i.e. a junk header could abort setup entirely.
        "http://[::1",
        "http://[]",
    ],
)
def test_wildcard_bind_rejects_malformed_origin(tmp_path, bad):
    # normalize_origin returns the cleaned input unchanged when it can't parse scheme+host, so
    # a junk header must not be written through into the allowlist verbatim. Token mode is the
    # only place this is reachable — loopback mode rejects a non-loopback Origin outright.
    client, projects, write_path = _token_env_host_app(tmp_path)
    res = client.post(
        "/setup", headers={"x-setup-token": _TOKEN, "origin": bad}, json=_valid_payload(projects)
    )
    assert res.status_code == 200
    assert load_config(write_path).auth.allowed_origins == []


def test_non_wildcard_loopback_literal_bind_still_records_origin(tmp_path):
    # 127.0.0.2 is loopback to _is_loopback_host (all of 127.0.0.0/8) but NOT in the
    # _LOOPBACK_HOSTS set that build_allowed_origins consults, so it auto-allows nothing and
    # DOES need the origin recorded. Keying the decision off the wrong predicate would silently
    # reintroduce #1071 for this bind.
    client, projects, write_path = _token_env_host_app(tmp_path, env_host="127.0.0.2")
    res = client.post(
        "/setup",
        headers={"x-setup-token": _TOKEN, "origin": "http://127.0.0.2:7621"},
        json=_valid_payload(projects),
    )
    assert res.status_code == 200
    assert auth.build_allowed_origins(load_config(write_path)) == {"http://127.0.0.2:7621"}


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


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "::1"])
def test_host_is_bindable_true(host):
    assert setup_wizard.host_is_bindable(host) is True


@pytest.mark.parametrize("host", ["clauster.invalid", "not a host!!", "192.0.2.7"])
def test_host_is_bindable_false(host):
    # Not bindable: an unresolvable / malformed host (RFC 6761 `.invalid`) fails at getaddrinfo;
    # a syntactically-valid address not on any local interface (RFC 5737 TEST-NET `192.0.2.7`)
    # resolves but fails the ephemeral bind — exercising both rejection paths.
    assert setup_wizard.host_is_bindable(host) is False


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
