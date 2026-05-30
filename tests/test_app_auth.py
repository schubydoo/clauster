from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from clauster import auth
from clauster.app import LoginThrottle, create_app
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import SessionRunner

from test_auth import _proxy_header  # reuse the HMAC header builder

PASSWORD = "hunter2"
_PW_HASH = auth.hash_password(auth.make_hasher(), PASSWORD)
ORIGIN = "http://testserver"  # TestClient's default origin


def _password_client(runner_config) -> TestClient:
    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.password_required = True
    config.auth.password_hash = _PW_HASH
    config.auth.allowed_origins = [ORIGIN]
    return TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))


def _login(client: TestClient) -> None:
    resp = client.post(
        "/login", data={"password": PASSWORD}, headers={"origin": ORIGIN}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text


# ----- guard ---------------------------------------------------------------


def test_unauth_api_returns_401(runner_config):
    client = _password_client(runner_config)
    assert client.get("/api/instances").status_code == 401


def test_unauth_html_redirects_to_login(runner_config):
    client = _password_client(runner_config)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/login")


def test_public_paths_reachable_unauthenticated(runner_config):
    client = _password_client(runner_config)
    assert client.get("/login").status_code == 200
    assert client.get("/static/clauster.css").status_code == 200
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}  # trimmed when unauthenticated


# ----- login / logout ------------------------------------------------------


def test_login_correct_then_authed(runner_config):
    client = _password_client(runner_config)
    _login(client)
    assert client.cookies.get("clauster_session")
    assert client.get("/api/instances").status_code == 200
    # healthz now returns full detail to the authed session
    assert "claude_version" in client.get("/healthz").json()


def test_login_wrong_password_rejected(runner_config):
    client = _password_client(runner_config)
    resp = client.post(
        "/login", data={"password": "nope"}, headers={"origin": ORIGIN}, follow_redirects=False
    )
    assert resp.status_code == 401
    assert not client.cookies.get("clauster_session")


def test_logout_clears_session(runner_config):
    client = _password_client(runner_config)
    _login(client)
    client.post("/logout", headers={"origin": ORIGIN}, follow_redirects=False)
    assert client.get("/api/instances").status_code == 401


def test_logout_revokes_captured_cookie(runner_config):
    # The real revocation property: a cookie copied off the wire BEFORE logout is
    # dead AFTER logout — not merely cleared from this client's jar. Replay the
    # captured value explicitly and expect 401.
    client = _password_client(runner_config)
    _login(client)
    captured = client.cookies.get("clauster_session")
    assert client.get("/api/instances").status_code == 200  # valid before logout
    client.post("/logout", headers={"origin": ORIGIN}, follow_redirects=False)
    replay = client.get("/api/instances", headers={"cookie": f"clauster_session={captured}"})
    assert replay.status_code == 401  # epoch bumped -> captured cookie revoked


def test_logout_revokes_all_sessions(runner_config):
    # Single-user logout is "log out everywhere": a second session's cookie dies
    # when the first logs out (shared server-side epoch).
    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.password_required = True
    config.auth.password_hash = _PW_HASH
    config.auth.allowed_origins = [ORIGIN]
    app = create_app(config, runner=SessionRunner(config, claude_json=claude_json))
    phone, laptop = TestClient(app), TestClient(app)
    _login(phone)
    _login(laptop)
    assert laptop.get("/api/instances").status_code == 200
    phone.post("/logout", headers={"origin": ORIGIN}, follow_redirects=False)
    assert laptop.get("/api/instances").status_code == 401  # revoked everywhere


def test_epoch_persists_across_restart(runner_config):
    # A bump survives an app restart (re-create_app reads session.epoch): a
    # cookie revoked before restart stays revoked after.
    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.password_required = True
    config.auth.password_hash = _PW_HASH
    config.auth.allowed_origins = [ORIGIN]
    app1 = create_app(config, runner=SessionRunner(config, claude_json=claude_json))
    c1 = TestClient(app1)
    _login(c1)
    captured = c1.cookies.get("clauster_session")
    c1.post("/logout", headers={"origin": ORIGIN}, follow_redirects=False)
    # "restart": a fresh app over the same state_dir
    app2 = create_app(config, runner=SessionRunner(config, claude_json=claude_json))
    c2 = TestClient(app2)
    replay = c2.get("/api/instances", headers={"cookie": f"clauster_session={captured}"})
    assert replay.status_code == 401


def test_login_throttled_after_repeated_failures(runner_config):
    client = _password_client(runner_config)
    for _ in range(5):
        client.post("/login", data={"password": "x"}, headers={"origin": ORIGIN}, follow_redirects=False)
    resp = client.post("/login", data={"password": "x"}, headers={"origin": ORIGIN}, follow_redirects=False)
    assert resp.status_code == 429


# ----- cookie flags --------------------------------------------------------


def test_cookie_flags_auto_http_not_secure(runner_config):
    client = _password_client(runner_config)
    resp = client.post(
        "/login", data={"password": PASSWORD}, headers={"origin": ORIGIN}, follow_redirects=False
    )
    setc = resp.headers["set-cookie"].lower()
    assert "httponly" in setc and "samesite=lax" in setc and "secure" not in setc


def test_cookie_secure_always(runner_config):
    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.password_required = True
    config.auth.password_hash = _PW_HASH
    config.auth.allowed_origins = [ORIGIN]
    config.auth.cookie_secure = "always"
    client = TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))
    resp = client.post(
        "/login", data={"password": PASSWORD}, headers={"origin": ORIGIN}, follow_redirects=False
    )
    assert "secure" in resp.headers["set-cookie"].lower()


# ----- CSRF (Origin check on unsafe methods) -------------------------------


def test_csrf_evil_origin_blocked(runner_config):
    client = _password_client(runner_config)
    _login(client)
    resp = client.post("/api/instances", json={}, headers={"origin": "http://evil.test"})
    assert resp.status_code == 403


def test_csrf_good_origin_passes_to_route(runner_config):
    client = _password_client(runner_config)
    _login(client)
    # Empty body -> 422 from the route; proves CSRF + auth both passed (not 403/401).
    resp = client.post("/api/instances", json={}, headers={"origin": ORIGIN})
    assert resp.status_code == 422


def test_csrf_missing_origin_and_referer_blocked(runner_config):
    client = _password_client(runner_config)
    _login(client)
    resp = client.request("POST", "/api/instances", json={}, headers={"referer": ""})
    assert resp.status_code == 403


# ----- reverse-proxy trust (peer_ip seam monkeypatched) --------------------


def _proxy_client(runner_config):
    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.reverse_proxy.enabled = True
    config.auth.reverse_proxy.trusted_ips = ["10.0.0.1"]
    config.auth.reverse_proxy.shared_secret = "proxy-secret"
    config.auth.allowed_origins = [ORIGIN]
    return TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))


def test_proxy_trusted_peer_valid_hmac_allows(runner_config, monkeypatch):
    client = _proxy_client(runner_config)
    monkeypatch.setattr("clauster.auth.peer_ip", lambda scope: "10.0.0.1")
    t = int(time.time())
    hdr = _proxy_header("proxy-secret", "alice", "GET", "/api/instances", t)
    resp = client.get("/api/instances", headers={"Remote-User": "alice", "X-Proxy-Auth": hdr})
    assert resp.status_code == 200


def test_proxy_bad_hmac_rejected(runner_config, monkeypatch):
    client = _proxy_client(runner_config)
    monkeypatch.setattr("clauster.auth.peer_ip", lambda scope: "10.0.0.1")
    resp = client.get("/api/instances", headers={"Remote-User": "alice", "X-Proxy-Auth": "t=1,v1=bad"})
    assert resp.status_code == 401


def test_proxy_missing_remote_user_rejected(runner_config, monkeypatch):
    client = _proxy_client(runner_config)
    monkeypatch.setattr("clauster.auth.peer_ip", lambda scope: "10.0.0.1")
    t = int(time.time())
    hdr = _proxy_header("proxy-secret", "alice", "GET", "/api/instances", t)
    assert client.get("/api/instances", headers={"X-Proxy-Auth": hdr}).status_code == 401


def test_proxy_trusted_peer_no_header_rejected(runner_config, monkeypatch):
    client = _proxy_client(runner_config)
    monkeypatch.setattr("clauster.auth.peer_ip", lambda scope: "10.0.0.1")
    # Trusted peer IP but NO X-Proxy-Auth/Remote-User: must NOT trust on peer-IP
    # alone — falls through to (absent) session auth -> 401.
    assert client.get("/api/instances").status_code == 401


def test_proxy_untrusted_peer_rejected(runner_config, monkeypatch):
    client = _proxy_client(runner_config)
    monkeypatch.setattr("clauster.auth.peer_ip", lambda scope: "9.9.9.9")
    t = int(time.time())
    hdr = _proxy_header("proxy-secret", "alice", "GET", "/api/instances", t)
    resp = client.get("/api/instances", headers={"Remote-User": "alice", "X-Proxy-Auth": hdr})
    assert resp.status_code == 401


def test_proxy_hmac_replay_on_other_endpoint_rejected(runner_config, monkeypatch):
    client = _proxy_client(runner_config)
    monkeypatch.setattr("clauster.auth.peer_ip", lambda scope: "10.0.0.1")
    t = int(time.time())
    # token signed for GET /api/instances, replayed against DELETE /api/instances/x
    hdr = _proxy_header("proxy-secret", "alice", "GET", "/api/instances", t)
    resp = client.delete(
        "/api/instances/x",
        headers={"Remote-User": "alice", "X-Proxy-Auth": hdr, "origin": ORIGIN},
    )
    assert resp.status_code == 401  # HMAC method+path binding defeats the replay


# ----- WebSocket auth (D12) ------------------------------------------------


def test_ws_rejected_without_auth(runner_config):
    client = _password_client(runner_config)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/bridge-log/alpha", headers={"origin": ORIGIN}):
            pass


def test_ws_rejected_bad_origin(runner_config):
    client = _password_client(runner_config)
    _login(client)
    tok = client.cookies.get("clauster_session")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/bridge-log/alpha",
            headers={"origin": "http://evil.test", "cookie": f"clauster_session={tok}"},
        ):
            pass


def test_ws_authorized_passes_auth_gate(runner_config):
    client = _password_client(runner_config)
    _login(client)
    tok = client.cookies.get("clauster_session")
    # Good origin + valid cookie => auth passes (accept), then closes 1008 for the
    # nonexistent instance. Reaching accept proves the gate opened.
    with client.websocket_connect(
        "/ws/bridge-log/ghost",
        headers={"origin": ORIGIN, "cookie": f"clauster_session={tok}"},
    ) as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_ws_streams_sanitized_lines(runner_config, tmp_path):
    # Happy path: a managed instance with a log file -> the loop reads, strips ANSI,
    # redacts ids, and sends whole lines (covers the streaming body + disconnect).
    config, claude_json = runner_config  # auth off
    runner = SessionRunner(config, claude_json=claude_json)
    logf = tmp_path / "bridge.log"
    logf.write_text("ready \x1b[31mRED\x1b[0m env_01TESTENVAAAAAAAAAAAAAAAA go\n")
    runner._instances["demo"] = RemoteControlInstance(
        project="demo", label="demo", status=InstanceStatus.RUNNING, bridge_debug_log_path=logf,
    )
    with TestClient(create_app(config, runner=runner)) as client:
        with client.websocket_connect("/ws/bridge-log/demo") as ws:
            line = ws.receive_text()
    assert "RED" in line and "\x1b[" not in line                 # ANSI stripped
    assert "env_01TESTENVAAAAAAAAAAAAAAAA" not in line           # id redacted (D11)


# ----- misc app behaviours --------------------------------------------------


def test_throttle_allows_when_ip_unknown():
    assert LoginThrottle().allowed(None) is True


def test_login_form_redirects_when_already_authed(runner_config):
    client = _password_client(runner_config)
    _login(client)
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith("/")


def test_healthz_claude_probe_failure(runner_config):
    # auth off + bogus binary -> the except branch (claude_ok False, version None).
    config, claude_json = runner_config
    config.claude.binary = "definitely-not-claude-xyz"
    client = TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))
    body = client.get("/healthz").json()
    assert body["claude_ok"] is False and body["claude_version"] is None
