from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clauster import auth
from clauster.app import _CSP, create_app
from clauster.runner import SessionRunner

PASSWORD = "hunter2"
_PW_HASH = auth.hash_password(auth.make_hasher(), PASSWORD)
ORIGIN = "http://testserver"  # TestClient's default origin


def _open_client(runner_config) -> TestClient:
    """An app with auth disabled — security headers must still be stamped."""
    config, claude_json = runner_config
    config.auth.enabled = False
    return TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))


def _password_client(runner_config) -> TestClient:
    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.password_required = True
    config.auth.password_hash = _PW_HASH
    config.auth.allowed_origins = [ORIGIN]
    return TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))


def _login(client: TestClient) -> None:
    resp = client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"origin": ORIGIN},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


# ----- the headers are present on a normal response ------------------------


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
    ],
)
def test_static_headers_present(runner_config, header, value):
    client = _open_client(runner_config)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers[header] == value


def test_csp_present_and_locked_down(runner_config):
    client = _open_client(runner_config)
    resp = client.get("/healthz")
    csp = resp.headers["Content-Security-Policy"]
    assert csp == _CSP
    # The defence-in-depth essentials are spelled out.
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    assert "default-src 'self'" in csp
    # Same-origin live WebSocket streams (bridge log + hosted) must stay allowed.
    assert "connect-src 'self' ws: wss:" in csp
    # The dashboard's inline scripts + Alpine's eval keep these relaxations; if
    # they ever tighten, the docstring tradeoff must be revisited deliberately.
    assert "script-src 'self' 'unsafe-inline' 'unsafe-eval'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp


def test_headers_on_dashboard_html(runner_config):
    """The rendered dashboard (HTML 200) carries the headers — UI still loads."""
    client = _open_client(runner_config)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Content-Security-Policy"] == _CSP


def test_headers_on_rejected_response(runner_config):
    """A 401 from the auth guard still carries the security headers."""
    client = _password_client(runner_config)
    resp = client.get("/api/instances")
    assert resp.status_code == 401
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Content-Security-Policy"] == _CSP
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_headers_on_csrf_403(runner_config):
    """A 403 from the Origin/CSRF gate still carries the security headers."""
    client = _password_client(runner_config)
    _login(client)
    resp = client.post("/api/instances", json={}, headers={"origin": "http://evil.test"})
    assert resp.status_code == 403
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Content-Security-Policy"] == _CSP


# ----- HSTS is emitted only over HTTPS -------------------------------------


def test_hsts_absent_over_plain_http(runner_config):
    """Plain-HTTP (cookie_secure=auto) must NOT pin HSTS — would brick the LAN UI."""
    client = _open_client(runner_config)
    resp = client.get("/healthz")
    assert "Strict-Transport-Security" not in resp.headers


def test_hsts_present_when_secure(runner_config):
    """With cookie_secure=always (the HTTPS-detection seam), HSTS is emitted."""
    config, claude_json = runner_config
    config.auth.enabled = False
    config.auth.cookie_secure = "always"
    client = TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    hsts = resp.headers["Strict-Transport-Security"]
    assert "max-age=31536000" in hsts
    assert "includeSubDomains" in hsts
