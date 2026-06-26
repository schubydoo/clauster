from __future__ import annotations

import time

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
        "/login",
        data={"password": PASSWORD},
        headers={"origin": ORIGIN},
        follow_redirects=False,
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
    assert client.get("/static/favicon.svg").status_code == 200  # static mount is public
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
        "/login",
        data={"password": "nope"},
        headers={"origin": ORIGIN},
        follow_redirects=False,
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
        client.post(
            "/login",
            data={"password": "x"},
            headers={"origin": ORIGIN},
            follow_redirects=False,
        )
    resp = client.post(
        "/login",
        data={"password": "x"},
        headers={"origin": ORIGIN},
        follow_redirects=False,
    )
    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) >= 1  # tells the client when to retry


# ----- cookie flags --------------------------------------------------------


def test_cookie_flags_auto_http_not_secure(runner_config):
    client = _password_client(runner_config)
    resp = client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"origin": ORIGIN},
        follow_redirects=False,
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
        "/login",
        data={"password": PASSWORD},
        headers={"origin": ORIGIN},
        follow_redirects=False,
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
    resp = client.get(
        "/api/instances", headers={"Remote-User": "alice", "X-Proxy-Auth": "t=1,v1=bad"}
    )
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


def test_throttle_ignores_forged_user_header_without_hmac(runner_config, monkeypatch):
    # Item-2 (#408): on a trusted IP, a client can SET Remote-User but cannot forge a
    # valid X-Proxy-Auth HMAC. Without the HMAC the throttle key must NOT trust the
    # header — otherwise a fresh fabricated username per attempt mints a fresh per-key
    # login budget and evades the limiter. So a flood of forged usernames from one
    # trusted IP collapses to the shared-IP path and trips the global backoff (429).
    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.password_required = True
    config.auth.password_hash = _PW_HASH
    config.auth.allowed_origins = [ORIGIN]
    config.auth.reverse_proxy.enabled = True
    config.auth.reverse_proxy.trusted_ips = ["10.0.0.1"]
    config.auth.reverse_proxy.shared_secret = "proxy-secret"
    client = TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))
    monkeypatch.setattr("clauster.auth.peer_ip", lambda scope: "10.0.0.1")

    saw_429 = False
    for i in range(60):  # each attempt forges a DIFFERENT Remote-User (no valid HMAC)
        resp = client.post(
            "/login",
            data={"password": "wrong"},
            headers={"origin": ORIGIN, "Remote-User": f"forged-{i}"},
            follow_redirects=False,
        )
        if resp.status_code == 429:
            saw_429 = True
            break
        assert resp.status_code == 401  # bad password until the global backoff bites
    assert saw_429, "forged-username flood evaded the throttle (per-key budget minted)"


def test_throttle_trusts_user_only_with_valid_hmac(runner_config, monkeypatch):
    # The flip side: a genuinely proxy-authenticated user (valid HMAC) DOES get a
    # per-key budget — so one such user's failures don't lock out everyone behind the
    # proxy. Two valid users on the same trusted IP throttle independently.
    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.password_required = True
    config.auth.password_hash = _PW_HASH
    config.auth.allowed_origins = [ORIGIN]
    config.auth.reverse_proxy.enabled = True
    config.auth.reverse_proxy.trusted_ips = ["10.0.0.1"]
    config.auth.reverse_proxy.shared_secret = "proxy-secret"
    client = TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))
    monkeypatch.setattr("clauster.auth.peer_ip", lambda scope: "10.0.0.1")

    # alice fails enough to lock her own per-key budget (default max_failures small).
    for _ in range(40):
        t = int(time.time())
        hdr = _proxy_header("proxy-secret", "alice", "POST", "/login", t)
        client.post(
            "/login",
            data={"password": "wrong"},
            headers={"origin": ORIGIN, "Remote-User": "alice", "X-Proxy-Auth": hdr},
            follow_redirects=False,
        )
    # bob (a DIFFERENT valid proxy user, same IP) still has his own fresh budget.
    t = int(time.time())
    hdr = _proxy_header("proxy-secret", "bob", "POST", "/login", t)
    bob = client.post(
        "/login",
        data={"password": PASSWORD},
        headers={"origin": ORIGIN, "Remote-User": "bob", "X-Proxy-Auth": hdr},
        follow_redirects=False,
    )
    assert bob.status_code == 303, "a distinct HMAC-verified user was wrongly throttled"


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
    # `with` enters the TestClient so the app lifespan (startup) runs — required for WS tests
    # so the gate is exercised against startup-seeded state, not a half-initialized app.
    with _password_client(runner_config) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/bridge-log/alpha", headers={"origin": ORIGIN}):
                pass


@pytest.mark.parametrize(
    "path",
    [
        "/ws/bridge-log/alpha",
        "/ws/pty-terminal/alpha",
        "/ws/hosted/alpha",
        "/ws/clone-progress/alpha",
    ],
)
def test_all_ws_endpoints_reject_unauthenticated(runner_config, path):
    # #549 parity pin: EVERY WebSocket endpoint must gate on the same `auth.enabled` +
    # `_ws_authorized` check before accept — a new handler (or a dropped gate on one of them)
    # can't silently expose a live stream. Pinned so the hand-rolled per-handler gate can't drift.
    # `with` enters the TestClient so the app lifespan runs (tests-testclient-lifespan rule).
    with _password_client(runner_config) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path, headers={"origin": ORIGIN}):
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
        project="demo",
        label="demo",
        status=InstanceStatus.RUNNING,
        bridge_debug_log_path=logf,
    )
    with TestClient(create_app(config, runner=runner)) as client:
        with client.websocket_connect("/ws/bridge-log/demo") as ws:
            line = ws.receive_text()
    assert "RED" in line and "\x1b[" not in line  # ANSI stripped
    assert "env_01TESTENVAAAAAAAAAAAAAAAA" not in line  # id redacted (D11)


# ----- live read-only pty terminal (#534) ----------------------------------


def test_pty_terminal_streams_redacted_frames(runner_config, tmp_path):
    # Happy path: a running pty bridge with a capture file -> the read-only WS reads
    # raw frames, strips ANSI, redacts ids/secrets, and sends whole lines.
    config, claude_json = runner_config  # auth off
    runner = SessionRunner(config, claude_json=claude_json)
    capf = tmp_path / "demo.pty.log"
    capf.write_text(
        "frame \x1b[32mGREEN\x1b[0m session_01TESTSESSIONAAAAAAAAA ghp_AAAAAAAAAAAAAAAAAAAA\n"
    )
    runner._instances["demo"] = RemoteControlInstance(
        project="demo",
        label="demo",
        status=InstanceStatus.RUNNING,
        resume_mode="pty",
        bridge_pty_log_path=capf,
    )
    with TestClient(create_app(config, runner=runner)) as client:
        with client.websocket_connect("/ws/pty-terminal/demo") as ws:
            line = ws.receive_text()
    assert "GREEN" in line and "\x1b[" not in line  # ANSI stripped
    assert "session_01TESTSESSIONAAAAAAAAA" not in line  # bearer-equivalent id redacted
    assert "ghp_AAAAAAAAAAAAAAAAAAAA" not in line  # secret shape redacted


def test_pty_terminal_1008_for_standard_bridge(runner_config, tmp_path):
    # A standard (non-pty) bridge has no captured PTY -> close 1008, never stream.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    capf = tmp_path / "demo.pty.log"
    capf.write_text("anything\n")
    runner._instances["demo"] = RemoteControlInstance(
        project="demo",
        label="demo",
        status=InstanceStatus.RUNNING,
        resume_mode="standard",
        bridge_pty_log_path=capf,  # even if a path is somehow set, standard mode is refused
    )
    with TestClient(create_app(config, runner=runner)) as client:
        with client.websocket_connect("/ws/pty-terminal/demo") as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()


def test_pty_terminal_1008_when_no_capture_path(runner_config):
    # A pty bridge with no capture file (pruned / never captured) -> close 1008.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    runner._instances["demo"] = RemoteControlInstance(
        project="demo",
        label="demo",
        status=InstanceStatus.RUNNING,
        resume_mode="pty",
        bridge_pty_log_path=None,
    )
    with TestClient(create_app(config, runner=runner)) as client:
        with client.websocket_connect("/ws/pty-terminal/demo") as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()


def test_pty_terminal_1008_for_unknown_instance(runner_config):
    # No such instance -> close 1008 (no path leak).
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    with TestClient(create_app(config, runner=runner)) as client:
        with client.websocket_connect("/ws/pty-terminal/ghost") as ws:
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()


# ----- API token (Bearer) auth (#360) --------------------------------------

_TOKEN, _TOKEN_HASH = auth.mint_token()


def _token_client(runner_config) -> TestClient:
    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.api_token_hash = _TOKEN_HASH
    config.auth.allowed_origins = [ORIGIN]
    return TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))


def _bearer(token: str = _TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_token_authenticates_api_get(runner_config):
    client = _token_client(runner_config)
    assert client.get("/api/instances", headers=_bearer()).status_code == 200


def test_no_token_is_401(runner_config):
    client = _token_client(runner_config)
    assert client.get("/api/instances").status_code == 401


def test_wrong_token_is_401(runner_config):
    client = _token_client(runner_config)
    assert client.get("/api/instances", headers=_bearer("clauster_pat_wrong")).status_code == 401


def test_malformed_authorization_header_is_401(runner_config):
    client = _token_client(runner_config)
    resp = client.get("/api/instances", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


def test_token_disabled_when_auth_off(runner_config):
    # Master switch: with auth.enabled False, a configured token never gates (and
    # never opens a NEW path) — the request passes through like any other.
    config, claude_json = runner_config
    config.auth.api_token_hash = _TOKEN_HASH  # configured but auth.enabled stays False
    client = TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))
    assert client.get("/api/instances").status_code == 200


def test_token_exempts_csrf_origin_check(runner_config):
    # A Bearer request carries no ambient cookie, so the CSRF Origin gate is
    # exempt (mirrors the reverse-proxy exemption). No Origin header at all here:
    # an empty body reaches the route -> 422 (proves auth+CSRF both passed).
    client = _token_client(runner_config)
    resp = client.post("/api/instances", json={}, headers=_bearer())
    assert resp.status_code == 422


def test_unsafe_method_without_token_or_origin_blocked(runner_config):
    # Belt-and-suspenders: a NON-token unsafe request with no Origin is still
    # CSRF-blocked (the token exemption must not have widened the gate).
    client = _token_client(runner_config)
    resp = client.request("POST", "/api/instances", json={}, headers={"referer": ""})
    assert resp.status_code == 403


def test_token_authorizes_websocket_without_origin(runner_config):
    # A headless token client sends no Origin; the WS Origin requirement is
    # exempt for the token path. Reaching accept (then the 1008 close for the
    # nonexistent instance) proves the gate opened.
    client = _token_client(runner_config)
    with client.websocket_connect("/ws/bridge-log/ghost", headers=_bearer()) as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_bad_token_websocket_rejected(runner_config):
    client = _token_client(runner_config)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/bridge-log/ghost", headers=_bearer("clauster_pat_wrong")
        ):
            pass


# ----- misc app behaviours --------------------------------------------------


def test_throttle_allows_when_ip_unknown():
    assert LoginThrottle().allowed(None) == (True, 0.0)


def test_throttle_per_key_lock_after_max_failures():
    t = LoginThrottle(max_failures=3, window_seconds=300)
    for _ in range(3):
        assert t.allowed("1.2.3.4")[0] is True
        t.record_failure("1.2.3.4")
    allowed, retry_after = t.allowed("1.2.3.4")
    assert allowed is False and retry_after == 300.0
    assert t.allowed("9.9.9.9")[0] is True  # a different key is unaffected
    t.reset("1.2.3.4")
    assert t.allowed("1.2.3.4")[0] is True  # success clears the lock


def test_throttle_evicts_empty_key_after_window_expiry():
    # A pruned-to-empty key must be dropped, not left as a permanent ``key: []`` —
    # otherwise a failed-login flood from many distinct IPs leaks one entry per IP (#402).
    t = LoginThrottle(max_failures=3, window_seconds=300)
    now = time.monotonic()
    t._failures["1.2.3.4"] = [now - (t._window + 100)]  # one expired timestamp
    assert t.allowed("1.2.3.4") == (True, 0.0)  # gate check prunes the expired entry
    assert "1.2.3.4" not in t._failures  # evicted, not leaked as key: []

    # Eviction only drops empties — a key with a still-live timestamp is retained.
    t._failures["5.6.7.8"] = [now - (t._window + 100), now]  # one expired, one live
    assert t.allowed("5.6.7.8")[0] is True
    assert t._failures.get("5.6.7.8") == [now]  # pruned to the live entry, key kept


def test_throttle_shared_proxy_ip_skips_per_key_lock_uses_global_backoff():
    # A shared proxy IP: per-key lock would lock everyone out, so it's skipped — only the
    # global backoff applies once failures cross the ceiling.
    t = LoginThrottle(
        max_failures=3, window_seconds=300, global_ceiling=5, backoff_cap_seconds=60.0
    )
    for _ in range(5):  # at the ceiling, still allowed (shared → no per-key lock)
        assert t.allowed("proxy-ip", shared=True)[0] is True
        t.record_failure("proxy-ip", shared=True)
    # The 6th failure pushes the global count over the ceiling → backoff (429 + wait).
    t.record_failure("proxy-ip", shared=True)
    allowed, retry_after = t.allowed("proxy-ip", shared=True)
    assert allowed is False and 0.0 < retry_after <= 60.0
    assert "proxy-ip" not in t._failures  # per-key lock truly skipped for a shared key


def test_throttle_paths_are_independent():
    # A shared-proxy flood must NOT 429 a distinguishable direct client (no cross-spill),
    # and a direct client's per-key lock must NOT touch the global counter.
    t = LoginThrottle(max_failures=3, window_seconds=300, global_ceiling=2)
    for _ in range(10):
        t.record_failure("proxy-ip", shared=True)
    assert t.allowed("1.2.3.4")[0] is True  # direct client unaffected by the shared flood
    t2 = LoginThrottle(max_failures=2, window_seconds=300, global_ceiling=2)
    for _ in range(2):
        t2.record_failure("1.2.3.4")
    assert t2._global == []  # a per-key failure never feeds the global ceiling


def test_throttle_backoff_is_capped():
    t = LoginThrottle(global_ceiling=1, backoff_cap_seconds=5.0)
    for _ in range(40):  # drive the exponent way past the cap
        t.record_failure("x", shared=True)
    _, retry_after = t.allowed("x", shared=True)
    assert retry_after <= 5.0


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
