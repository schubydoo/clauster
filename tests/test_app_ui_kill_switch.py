"""Tests for the `ui.enabled` web-dashboard kill switch (#806).

Covers both states — the default (`ui.enabled=true`, zero behavior change) and
the opt-out (`ui.enabled=false`, dashboard surface 404s while the JSON API keeps
working) — plus the config-editor exclusion and the fail-closed startup warning
for a `ui.enabled=false` + `auth.enabled=true` deployment with no way to
authenticate.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from clauster import auth
from clauster.app import create_app
from clauster.config import load_config
from clauster.config_editor import EXCLUDED_FIELDS
from clauster.runner import SessionRunner

ORIGIN = "http://testserver"  # TestClient's default origin

# The exact route list #806 gates when ui.enabled is false — the dashboard page,
# login/logout, and the internal HTML-fragment / per-session interactive routes
# #302 already classified as "internal/unversioned only".
_UI_GET_ROUTES = [
    "/",
    "/login",
    "/api/projects/alpha/row",
    "/api/widget",
    "/api/instances/some-id/qr",
    "/static/vendor/tabler/css/tabler.min.css",
]
_UI_POST_ROUTES = [
    "/login",
    "/logout",
    "/api/instances/some-id/message",
    "/api/instances/some-id/permissions/req-1",
    "/api/instances/some-id/forget",
]

# The JSON API that must keep working regardless of ui.enabled — the rest of the
# bare /api/... surface, the /api/v1 alias, and /healthz.
_JSON_GET_ROUTES = [
    "/api/projects",
    "/api/sessions",
    "/api/instances",
    "/api/agents",
    "/api/doctor",
    "/api/v1/projects",
    "/api/v1/instances",
    "/healthz",
]


def _client(write_config, extra: str = "") -> TestClient:
    config = load_config(write_config(extra))
    return TestClient(create_app(config))


# ----- ui.enabled=false: the dashboard surface 404s -------------------------


@pytest.mark.parametrize("path", _UI_GET_ROUTES)
def test_ui_disabled_gets_404_on_dashboard_surface(write_config, path):
    client = _client(write_config, "ui:\n  enabled: false\n")
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", _UI_POST_ROUTES)
def test_ui_disabled_posts_404_on_dashboard_surface(write_config, path):
    client = _client(write_config, "ui:\n  enabled: false\n")
    resp = client.post(path, json={}, headers={"origin": ORIGIN})
    assert resp.status_code == 404


def test_ui_disabled_static_directory_listing_also_404s(write_config):
    client = _client(write_config, "ui:\n  enabled: false\n")
    assert client.get("/static/").status_code == 404


@pytest.mark.parametrize("path", ["/", "/login"])
def test_ui_disabled_head_request_also_404s(write_config, path):
    # Starlette auto-serves HEAD for every GET route (runs the handler, strips the
    # body), so a HEAD to a GET-only _UI_ONLY_ROUTES entry must gate exactly like
    # the GET — otherwise the kill switch leaks a confirmable 200 and still runs
    # the dashboard handler. (Greptile P2 on #810.)
    client = _client(write_config, "ui:\n  enabled: false\n")
    assert client.head(path).status_code == 404


# ----- ui.enabled=false: the JSON API keeps working -------------------------


@pytest.mark.parametrize("path", _JSON_GET_ROUTES)
def test_ui_disabled_json_api_still_works(write_config, path):
    client = _client(write_config, "ui:\n  enabled: false\n")
    assert client.get(path).status_code == 200


def test_ui_disabled_metrics_endpoint_still_reachable_when_its_own_gate_is_on(write_config):
    # /metrics has its own independent gate (observability.prometheus_enabled) —
    # ui.enabled must not interfere with it either way.
    client = _client(
        write_config, "ui:\n  enabled: false\nobservability:\n  prometheus_enabled: true\n"
    )
    assert client.get("/metrics").status_code == 200


def test_ui_disabled_still_401s_json_api_when_auth_enabled_and_unauthenticated(
    write_config,
):
    raw, token_hash = auth.mint_token()
    client = _client(
        write_config,
        f"ui:\n  enabled: false\nauth:\n  enabled: true\n  api_token_hash: {token_hash}\n",
    )
    assert client.get("/api/instances").status_code == 401
    resp = client.get("/api/instances", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200


# ----- ui.enabled=true (default): zero behavior change ----------------------


def test_ui_enabled_default_dashboard_renders(write_config):
    client = _client(write_config)
    assert client.get("/").status_code == 200
    assert client.get("/login").status_code == 200


def test_ui_enabled_default_head_request_returns_normal_status(write_config):
    # The HEAD normalization only gates when ui.enabled is false. With the UI on,
    # ui_guard is a no-op and HEAD / falls through to the router unchanged — a
    # GET-only FastAPI route answers HEAD with 405 (its normal behavior). The
    # point is that it is NOT the ui_guard's 404: the guard doesn't interfere.
    client = _client(write_config)
    assert client.head("/").status_code == 405


def test_ui_enabled_default_fragment_and_widget_routes_render(write_config):
    client = _client(write_config)
    assert client.get("/api/projects/alpha/row").status_code == 200
    assert client.get("/api/widget").status_code == 200


def test_ui_enabled_default_static_asset_serves(write_config):
    client = _client(write_config)
    # Any real asset under src/clauster/static suffices; 200 (not 404) proves the
    # mount is present when ui.enabled is left at its default.
    resp = client.get("/static/vendor/tabler/css/tabler.min.css")
    assert resp.status_code in (200, 304)


def test_ui_enabled_explicit_true_behaves_identically_to_default(write_config):
    client = _client(write_config, "ui:\n  enabled: true\n")
    assert client.get("/").status_code == 200


# ----- websocket streams are unaffected (BaseHTTPMiddleware never wraps them) -----


def test_ui_disabled_websocket_route_is_not_404d_by_the_http_middleware(write_config):
    config = load_config(write_config("ui:\n  enabled: false\n"))
    with TestClient(create_app(config)) as client:
        # No such instance exists, so the route accepts then closes (1008) rather
        # than ever reaching the dashboard-only HTTP path — proving the ui_guard
        # HTTP middleware (which only wraps `scope["type"] == "http"`) never
        # touches the websocket scope at all.
        with (
            pytest.raises(Exception),  # noqa: B017 - server closes (1008) right after accept
            client.websocket_connect("/ws/bridge-log/nope") as ws,
        ):
            ws.receive_json()


# ----- config editor: ui.enabled is excluded, never web-editable -----------


def test_ui_enabled_is_excluded_from_the_web_config_editor():
    assert "ui.enabled" in EXCLUDED_FIELDS


# ----- fail-closed lockout warning (#806) -----------------------------------


def _auth_enabled_ui_disabled_config(runner_config):
    config, claude_json = runner_config
    config.ui.enabled = False
    config.auth.enabled = True
    return config, claude_json


def test_no_credential_configured_warns_loudly_but_still_starts(runner_config, caplog):
    config, claude_json = _auth_enabled_ui_disabled_config(runner_config)
    runner = SessionRunner(config, claude_json=claude_json)
    try:
        with caplog.at_level(logging.WARNING, logger="clauster.app"):
            create_app(config, runner=runner)
        assert any(
            "ui.enabled is false and auth.enabled is true" in r.message for r in caplog.records
        )
    finally:
        runner.persistence.dispose()


def test_legacy_api_token_hash_silences_the_warning(runner_config, caplog):
    _raw, token_hash = auth.mint_token()
    config, claude_json = _auth_enabled_ui_disabled_config(runner_config)
    config.auth.api_token_hash = token_hash
    runner = SessionRunner(config, claude_json=claude_json)
    try:
        with caplog.at_level(logging.WARNING, logger="clauster.app"):
            create_app(config, runner=runner)
        assert not any("ui.enabled is false" in r.message for r in caplog.records)
    finally:
        runner.persistence.dispose()


def test_named_token_silences_the_warning(runner_config, caplog):
    config, claude_json = _auth_enabled_ui_disabled_config(runner_config)
    runner = SessionRunner(config, claude_json=claude_json)
    try:
        runner.persistence.api_token_store().issue("ci")
        with caplog.at_level(logging.WARNING, logger="clauster.app"):
            create_app(config, runner=runner)
        assert not any("ui.enabled is false" in r.message for r in caplog.records)
    finally:
        runner.persistence.dispose()


def test_reverse_proxy_auth_silences_the_warning(runner_config, caplog):
    config, claude_json = _auth_enabled_ui_disabled_config(runner_config)
    config.auth.reverse_proxy.enabled = True
    config.auth.reverse_proxy.trusted_ips = ["127.0.0.1"]
    runner = SessionRunner(config, claude_json=claude_json)
    try:
        with caplog.at_level(logging.WARNING, logger="clauster.app"):
            create_app(config, runner=runner)
        assert not any("ui.enabled is false" in r.message for r in caplog.records)
    finally:
        runner.persistence.dispose()


def test_ui_enabled_never_warns_regardless_of_auth(runner_config, caplog):
    config, claude_json = runner_config
    config.auth.enabled = True
    runner = SessionRunner(config, claude_json=claude_json)
    try:
        with caplog.at_level(logging.WARNING, logger="clauster.app"):
            create_app(config, runner=runner)
        assert not any("ui.enabled is false" in r.message for r in caplog.records)
    finally:
        runner.persistence.dispose()


def test_auth_disabled_never_warns_even_with_ui_off(runner_config, caplog):
    config, claude_json = runner_config
    config.ui.enabled = False
    runner = SessionRunner(config, claude_json=claude_json)
    try:
        with caplog.at_level(logging.WARNING, logger="clauster.app"):
            create_app(config, runner=runner)
        assert not any("ui.enabled is false" in r.message for r in caplog.records)
    finally:
        runner.persistence.dispose()


def test_token_store_read_failure_fails_open_to_still_warning(runner_config, caplog, monkeypatch):
    # A DB hiccup on the advisory token-count read must not crash startup, and —
    # being unable to prove a token exists — the warning still fires (better to
    # over-warn than silently skip the heads-up).
    from clauster.db.stores import ApiTokenStore

    config, claude_json = _auth_enabled_ui_disabled_config(runner_config)
    runner = SessionRunner(config, claude_json=claude_json)

    def _boom(self):
        raise OSError("db unavailable")

    monkeypatch.setattr(ApiTokenStore, "list_all", _boom)
    try:
        with caplog.at_level(logging.WARNING, logger="clauster.app"):
            create_app(config, runner=runner)
        assert any(
            "ui.enabled is false and auth.enabled is true" in r.message for r in caplog.records
        )
    finally:
        runner.persistence.dispose()
