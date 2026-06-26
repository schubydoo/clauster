"""In-app help offcanvas (#571).

A `?` control opens a Tabler offcanvas with hand-authored help content. It must ship
on the dashboard, the login page, and the friendly 404 — keyboard-accessible
(Esc / focus-trap), with an accessible name on the trigger, and the untrusted-free
static copy must never be rendered as HTML.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from clauster import auth
from clauster.app import create_app
from clauster.config import load_config
from clauster.runner import SessionRunner

PASSWORD = "hunter2"
_PW_HASH = auth.hash_password(auth.make_hasher(), PASSWORD)
ORIGIN = "http://testserver"


def _client(write_config) -> TestClient:
    return TestClient(create_app(load_config(write_config())))


def _password_client(runner_config) -> TestClient:
    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.password_required = True
    config.auth.password_hash = _PW_HASH
    config.auth.allowed_origins = [ORIGIN]
    return TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))


def test_dashboard_has_help_trigger_and_offcanvas(write_config):
    page = _client(write_config).get("/").text
    # The `?` trigger ships with an accessible name and dialog wiring.
    assert 'data-test="help-trigger"' in page
    assert 'aria-label="Help"' in page
    assert 'aria-controls="help-offcanvas"' in page
    assert 'aria-haspopup="dialog"' in page
    assert "#ic-help" in page  # the help glyph symbol is referenced
    # The offcanvas panel ships as a labelled modal dialog.
    assert 'data-test="help-offcanvas"' in page
    assert 'role="dialog"' in page
    assert 'aria-modal="true"' in page
    assert 'aria-labelledby="help-offcanvas-title"' in page
    assert 'data-test="help-close"' in page
    assert 'data-test="help-backdrop"' in page


def test_help_covers_the_key_concepts(write_config):
    page = _client(write_config).get("/").text
    # Hand-authored sections required by the spec (launch modes, permission modes,
    # zones, the External/unmanaged definition, the header tools).
    assert "Launch modes" in page
    assert "Permission modes" in page
    # The real launch modes are outcome-language (not "Standard/PTY/Browser"); standard/pty
    # are a resume sub-choice under Desktop, and "Fire-and-forget" was renamed to "Background".
    assert "In claude.ai / Desktop" in page
    assert "Here in the browser" in page
    assert "Background" in page
    assert "Managed vs. External sessions" in page
    # The External/unmanaged definition (UX-swarm fold-in on #571) is always-visible copy.
    assert "Clauster did not launch" in page
    assert "Configuration editor" in page
    # Links out to the README for depth (not a rendered slice — avoids drift).
    assert "github.com/schubydoo/clauster#readme" in page


def test_help_controller_is_keyboard_accessible_and_nonced(write_config):
    page = _client(write_config).get("/").text
    # Esc to close, click-away on the backdrop, and a manual focus trap on Tab.
    assert 'e.key === "Escape"' in page
    assert "trapTab" in page
    assert "closeHelp" in page and "openHelp" in page
    # Focus is restored to the opener on close.
    assert "trigger.focus()" in page
    # The inline controller carries the per-request CSP nonce so it survives the
    # nonce-gated script-src (no hard-coded nonce; round-trip is covered elsewhere).
    assert re.search(r"<script nonce=\"[^\"]+\">\s*\(function", page)


def test_help_panel_is_static_never_x_html(write_config):
    # The help copy is hand-authored static Jinja — it must never be bound via x-html
    # (the XSS sink). Guard the directive form, not the literal word in a comment.
    page = _client(write_config).get("/").text
    assert "x-html=" not in page and "x-html =" not in page


def test_login_page_has_help_trigger_and_offcanvas(runner_config):
    page = _password_client(runner_config).get("/login").text
    assert 'data-test="help-trigger"' in page
    assert 'aria-label="Help"' in page
    assert 'data-test="help-offcanvas"' in page
    assert "Launch modes" in page  # the panel content ships on the public login page
    assert re.search(r"<script nonce=\"[^\"]+\">\s*\(function", page)


def test_404_page_has_help_trigger_and_offcanvas(write_config):
    # A browser hitting a stale/mistyped non-API path gets the friendly HTML 404,
    # which now carries the same `?` help control (and the sprite it needs).
    resp = _client(write_config).get("/totally/bogus", headers={"accept": "text/html"})
    assert resp.status_code == 404
    page = resp.text
    assert 'data-test="help-trigger"' in page
    assert 'data-test="help-offcanvas"' in page
    assert "#ic-help" in page
    assert "Launch modes" in page
    assert re.search(r"<script nonce=\"[^\"]+\">\s*\(function", page)


def test_help_trigger_count_is_one_per_page(write_config):
    # Exactly one trigger + one panel render per page — the navbar emits only the
    # trigger; the panel is emitted once at end-of-body (no duplicate dialog).
    page = _client(write_config).get("/").text
    assert page.count('data-test="help-trigger"') == 1
    assert page.count('data-test="help-offcanvas"') == 1
