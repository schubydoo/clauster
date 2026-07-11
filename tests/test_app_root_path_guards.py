"""root_path-stripping in the auth + UI guards (#812).

Both guards classify routes against app-local paths (``/login``, ``/api/…``). Under a
reverse proxy that does NOT strip the mount prefix, ``request.url.path`` still carries it
(``/prefix/login``), which would misclassify a route and, for the UI kill switch, fail
*open*. ``_app_local_path`` strips the configured ``root_path`` so classification is correct
regardless of the proxy; a prefix-stripping proxy (the supported default) is a no-op.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from starlette.requests import Request

from clauster.app import _app_local_path, _ui_guard_matches, create_app
from clauster.config import load_config

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _req(path: str, root: str = "") -> Request:
    """A minimal GET Request with the given ASGI path + root_path."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "root_path": root,
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
        }
    )


# ----- _app_local_path branches ------------------------------------------------


def test_app_local_path_no_prefix_is_identity():
    assert _app_local_path(_req("/login")) == "/login"


def test_app_local_path_strips_nonstripping_proxy_prefix():
    # The #812 case: a proxy forwards the full path incl. the mount prefix.
    assert _app_local_path(_req("/prefix/login", "/prefix")) == "/login"
    assert _app_local_path(_req("/prefix/api/instances", "/prefix")) == "/api/instances"


def test_app_local_path_stripping_proxy_is_noop():
    # Supported default: the proxy already stripped the prefix, so the path is local.
    assert _app_local_path(_req("/login", "/prefix")) == "/login"


def test_app_local_path_exact_prefix_becomes_root():
    assert _app_local_path(_req("/prefix", "/prefix")) == "/"


def test_app_local_path_normalizes_trailing_slash_root():
    # A root_path configured with a trailing slash ("/prefix/") must not defeat the
    # boundary strip and leave the prefix in place (Greptile P1 on #880).
    assert _app_local_path(_req("/prefix/login", "/prefix/")) == "/login"
    assert _app_local_path(_req("/prefix/api/x", "/prefix/")) == "/api/x"
    # A bare "/" root is no real prefix — normalizes to empty, so it's a pure no-op.
    assert _app_local_path(_req("/login", "/")) == "/login"


def test_app_local_path_does_not_strip_coincidental_prefix():
    # "/prefixfoo" is not under the "/prefix" mount — the boundary check must not strip it.
    assert _app_local_path(_req("/prefixfoo", "/prefix")) == "/prefixfoo"


# ----- the fix's core: the UI kill switch no longer fails open -----------------


def test_ui_guard_matches_prefixed_login_only_after_strip():
    # Without the strip, a prefixed UI path misses the route set → kill switch fails OPEN.
    req = _req("/prefix/login", "/prefix")
    assert _ui_guard_matches("GET", req.url.path) is False  # pre-fix: fails open
    assert _ui_guard_matches("GET", _app_local_path(req)) is True  # post-fix: 404'd


# ----- auth guard classifies /api under a non-stripping proxy ------------------


def _client(write_config, tmp_path, *, root_path: str) -> TestClient:
    cfg = load_config(
        write_config(
            f"claude:\n  binary: {FAKE_CLAUDE}\n"
            f"state_dir: {tmp_path}/.s\n"
            f'root_path: "{root_path}"\n'
            "auth:\n  enabled: true\n"
        )
    )
    return TestClient(create_app(cfg), follow_redirects=False)


def test_api_path_under_nonstripping_proxy_returns_401_not_login_redirect(write_config, tmp_path):
    # With root_path stripped, an unauthenticated /prefix/api/... is classified as API →
    # 401, not the 303 login redirect a browser HTML route gets. This is the observable
    # sign the guard matched the app-local path (the guard returns before routing, so the
    # unmatched prefixed route never 404s first).
    client = _client(write_config, tmp_path, root_path="/prefix")
    resp = client.get("/prefix/api/instances")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "authentication required"


def test_api_path_without_prefix_still_401(write_config, tmp_path):
    # No-regression: the same classification holds with no proxy prefix configured.
    client = _client(write_config, tmp_path, root_path="")
    resp = client.get("/api/instances")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "authentication required"
