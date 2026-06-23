from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from clauster import auth
from clauster.app import _csp_with_nonce, create_app
from clauster.runner import SessionRunner

PASSWORD = "hunter2"
_PW_HASH = auth.hash_password(auth.make_hasher(), PASSWORD)
ORIGIN = "http://testserver"  # TestClient's default origin

# script-src must list 'self', a per-request nonce, and 'unsafe-eval' (Alpine), and
# must NOT carry 'unsafe-inline' (dropped in #442 — dead config once a nonce is present).
_SCRIPT_SRC_RE = re.compile(r"script-src 'self' 'nonce-[A-Za-z0-9_-]+' 'unsafe-eval'")
# Pull the nonce token out of a CSP header.
_CSP_NONCE_RE = re.compile(r"script-src 'self' 'nonce-([A-Za-z0-9_-]+)'")
# Pull the nonce attribute off the first inline <script nonce="..."> in a body.
_BODY_NONCE_RE = re.compile(r'<script\s+nonce="([A-Za-z0-9_-]+)"')


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


def _assert_nonce_script_src(csp: str) -> None:
    """script-src is nonce-gated: 'self' + a nonce + 'unsafe-eval', never 'unsafe-inline'."""
    script_src = next(
        (d for d in csp.split(";") if d.strip().startswith("script-src")), ""
    ).strip()
    assert _SCRIPT_SRC_RE.search(csp), csp
    assert "'unsafe-inline'" not in script_src, (
        f"script-src must NOT carry 'unsafe-inline' once a nonce is present: {script_src!r}"
    )


# ----- the headers are present on a normal response ------------------------


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "same-origin"),
    ],
)
def test_static_headers_present(runner_config, header, value):
    client = _open_client(runner_config)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers[header] == value


def test_referrer_policy_is_same_origin_not_no_referrer(runner_config):
    """Referrer-Policy must be same-origin, never no-referrer.

    Under `no-referrer`, a spec-compliant browser serializes the Origin header of a
    same-origin <form> POST navigation to the literal "null"; the CSRF Origin gate then
    rejects it (not in the allowlist) and 403s the native login/logout forms — the only
    non-fetch POSTs in the app. `same-origin` keeps the real Origin on same-origin
    navigations while still suppressing the cross-origin referrer. Regression guard so a
    future "tighten the privacy header" change can't silently re-break login/logout
    (#454); the e2e login flow is the end-to-end guard.
    """
    client = _open_client(runner_config)
    resp = client.get("/healthz")
    # The durable invariant: never the value that nulls a same-origin form-POST Origin.
    # (`== "same-origin"` lives in the parametrized header test; this guards the *bad*
    # value directly, so it still fires if the chosen-good value is ever swapped.)
    assert resp.headers["Referrer-Policy"] != "no-referrer", (
        "Referrer-Policy must never be no-referrer — it serializes a same-origin "
        "form-POST Origin to the literal 'null', breaking the CSRF gate on /login and "
        "/logout (see #454)."
    )


def test_csp_present_and_locked_down(runner_config):
    client = _open_client(runner_config)
    resp = client.get("/healthz")
    csp = resp.headers["Content-Security-Policy"]
    # script-src is nonce-gated (#442): 'self' + a per-request nonce + 'unsafe-eval',
    # and 'unsafe-inline' is gone (a nonce makes it dead config and blocks an injected
    # inline script that lacks the per-request value).
    _assert_nonce_script_src(csp)
    # The defence-in-depth essentials are spelled out.
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp
    assert "default-src 'self'" in csp
    # connect-src is exactly 'self' — same-origin WebSocket streams match under it,
    # and bare ws:/wss: scheme-sources (any-host exfiltration channel) are excluded.
    assert "connect-src 'self';" in csp
    assert "ws:" not in csp and "wss:" not in csp
    # style-src keeps 'unsafe-inline' (the inline <style> + style="" attributes can't be
    # nonced); 'unsafe-eval' stays for Alpine. Dropping either is the #442 follow-up.
    assert "style-src 'self' 'unsafe-inline'" in csp


def test_csp_nonce_differs_per_request(runner_config):
    """Two requests get DIFFERENT nonces — guards the frozen-process-wide-nonce footgun.

    A nonce baked into a Jinja global (or any module constant) would be identical across
    every response, defeating the protection. The nonce must be a per-request secret.
    """
    client = _open_client(runner_config)
    first = client.get("/healthz").headers["Content-Security-Policy"]
    second = client.get("/healthz").headers["Content-Security-Policy"]
    n1 = _CSP_NONCE_RE.search(first)
    n2 = _CSP_NONCE_RE.search(second)
    assert n1 and n2, (first, second)
    assert n1.group(1) != n2.group(1), "CSP nonce must be regenerated per request"


@pytest.mark.parametrize("path", ["/", "/login"])
def test_csp_nonce_round_trip(runner_config, path):
    """The inline <script nonce="X"> in the body matches the nonce-X in the CSP header.

    This is the load-bearing correctness check: it proves the per-request nonce flows
    end to end (middleware → request.state → _render context → template) so the inline
    scripts actually execute under the nonce-gated script-src. No JS execution needed —
    a mismatch here is exactly what would silently blank the UI in a real browser.
    """
    client = _open_client(runner_config)
    resp = client.get(path, headers={"accept": "text/html"})
    assert resp.status_code == 200, resp.text
    csp = resp.headers["Content-Security-Policy"]
    header_nonce = _CSP_NONCE_RE.search(csp)
    body_nonce = _BODY_NONCE_RE.search(resp.text)
    assert header_nonce, csp
    assert body_nonce, "no inline <script nonce=...> in the rendered body"
    assert header_nonce.group(1) == body_nonce.group(1), (
        f"CSP header nonce {header_nonce.group(1)!r} != body <script> nonce "
        f"{body_nonce.group(1)!r} — the inline script would be blocked"
    )


def test_csp_nonce_round_trip_on_404(runner_config):
    """The friendly HTML 404 page also round-trips its nonce (the 404 handler renders too)."""
    client = _open_client(runner_config)
    resp = client.get("/no-such-path", headers={"accept": "text/html"})
    assert resp.status_code == 404
    csp = resp.headers["Content-Security-Policy"]
    header_nonce = _CSP_NONCE_RE.search(csp)
    body_nonce = _BODY_NONCE_RE.search(resp.text)
    assert header_nonce, csp
    assert body_nonce, "no inline <script nonce=...> in the rendered 404 body"
    assert header_nonce.group(1) == body_nonce.group(1)


def test_csp_with_nonce_fail_closed_when_none():
    """Defensive degraded path: a None nonce still drops 'unsafe-inline' (stricter, not looser)."""
    csp = _csp_with_nonce(None)
    script_src = next(
        (d for d in csp.split(";") if d.strip().startswith("script-src")), ""
    ).strip()
    assert "'unsafe-inline'" not in script_src, script_src
    assert "'nonce-" not in script_src
    assert script_src == "script-src 'self' 'unsafe-eval'"


def test_headers_on_dashboard_html(runner_config):
    """The rendered dashboard (HTML 200) carries the headers — UI still loads."""
    client = _open_client(runner_config)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<html" in resp.text.lower()
    assert resp.headers["X-Frame-Options"] == "DENY"
    _assert_nonce_script_src(resp.headers["Content-Security-Policy"])


def test_headers_on_rejected_response(runner_config):
    """A 401 from the auth guard still carries the security headers (nonce included)."""
    client = _password_client(runner_config)
    resp = client.get("/api/instances")
    assert resp.status_code == 401
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    _assert_nonce_script_src(resp.headers["Content-Security-Policy"])
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_headers_on_csrf_403(runner_config):
    """A 403 from the Origin/CSRF gate still carries the security headers (nonce included)."""
    client = _password_client(runner_config)
    _login(client)
    resp = client.post("/api/instances", json={}, headers={"origin": "http://evil.test"})
    assert resp.status_code == 403
    assert resp.headers["X-Frame-Options"] == "DENY"
    _assert_nonce_script_src(resp.headers["Content-Security-Policy"])


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
    assert hsts == "max-age=31536000"
    # No includeSubDomains: it must not reach sibling subdomains on a shared parent.
    assert "includeSubDomains" not in hsts
