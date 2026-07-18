"""Route tests for the Tier-B "Advanced" config surface + step-up re-auth (#978).

Covers POST /api/reauth (step-up) and GET/PUT /api/config/advanced (the clone/webhooks
Tier-B scalars behind the config_write capability + a fresh password proof).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import load_config
from test_app_auth import _PW_HASH, ORIGIN, PASSWORD, _login

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _advanced_client(write_config, tmp_path, *, config_write=True) -> tuple[TestClient, Path]:
    """Build an authed client whose config has an on-disk source + Tier-B enabled."""
    path = write_config(
        f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n"
        "clone:\n  timeout_seconds: 300\n"
    )
    config = load_config(path)
    config.auth.enabled = True
    config.auth.password_required = True
    config.auth.password_hash = _PW_HASH
    config.auth.allowed_origins = [ORIGIN]
    config.config_write.enabled = config_write
    return TestClient(create_app(config)), path


def _elevate(client: TestClient) -> None:
    """Log in, then step up — the client now holds both the session + elevation cookies."""
    _login(client)
    res = client.post("/api/reauth", json={"password": PASSWORD}, headers={"origin": ORIGIN})
    assert res.status_code == 200, res.text
    assert res.json()["elevated"] is True
    assert client.cookies.get("clauster_elevation")


# ----- POST /api/reauth ----------------------------------------------------


def test_reauth_requires_login(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    # No session -> the guard blocks /api/reauth with a 401 before any password check.
    res = client.post("/api/reauth", json={"password": PASSWORD}, headers={"origin": ORIGIN})
    assert res.status_code == 401


def test_reauth_wrong_password_rejected(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _login(client)
    res = client.post("/api/reauth", json={"password": "nope"}, headers={"origin": ORIGIN})
    assert res.status_code == 401
    assert client.cookies.get("clauster_elevation") is None


def test_reauth_correct_sets_elevation_cookie(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _login(client)
    res = client.post("/api/reauth", json={"password": PASSWORD}, headers={"origin": ORIGIN})
    assert res.status_code == 200
    body = res.json()
    assert body["elevated"] is True
    assert body["expires_in"] == 600
    assert client.cookies.get("clauster_elevation")


def test_reauth_throttled_after_repeated_failures(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _login(client)
    saw_429 = False
    for _ in range(8):  # per-key hard lock trips after max_failures=5
        res = client.post("/api/reauth", json={"password": "x"}, headers={"origin": ORIGIN})
        if res.status_code == 429:
            saw_429 = True
            assert res.headers.get("Retry-After")
            break
    assert saw_429, "reauth never tripped the login throttle"


# ----- GET /api/config/advanced --------------------------------------------


def test_advanced_get_404_when_config_write_disabled(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path, config_write=False)
    _login(client)  # logged in, but the capability is off -> 404 regardless
    res = client.get("/api/config/advanced")
    assert res.status_code == 404  # invisible surface, not a 403


def test_advanced_get_403_without_elevation(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _login(client)  # authed but NOT stepped up
    res = client.get("/api/config/advanced")
    assert res.status_code == 403
    assert res.json()["detail"] == "reauth_required"


def test_advanced_get_returns_tier_b_only(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _elevate(client)
    res = client.get("/api/config/advanced")
    assert res.status_code == 200
    body = res.json()
    assert body["fields"]["clone.timeout_seconds"] == 300
    assert "webhooks.enabled" in body["fields"]
    assert isinstance(body["hash"], str) and body["hash"]
    # No Tier-A, Tier-C secret/bind/auth key ever surfaces on the Advanced GET.
    keys = list(body["fields"])
    assert all(k.startswith(("clone.", "webhooks.")) for k in keys)
    assert not any("password" in k or "secret" in k or "urls" in k for k in keys)


# ----- PUT /api/config/advanced --------------------------------------------


def test_advanced_put_applies_edit_and_audits(write_config, tmp_path):
    client, path = _advanced_client(write_config, tmp_path)
    _elevate(client)
    h = client.get("/api/config/advanced").json()["hash"]
    res = client.put(
        "/api/config/advanced",
        json={"edits": {"clone.timeout_seconds": 137}, "hash": h},
        headers={"origin": ORIGIN},
    )
    assert res.status_code == 200
    assert res.json()["restart_required"] is True
    # The file changed; a fresh GET reflects it (read from disk, no live-reload).
    assert client.get("/api/config/advanced").json()["fields"]["clone.timeout_seconds"] == 137
    # Audit line records the key NAME only — never the value (137 must not appear).
    audit = (tmp_path / ".s" / "config_audit.log").read_text()
    assert '"surface":"config-advanced"' in audit
    assert "clone.timeout_seconds" in audit
    assert "137" not in audit


def test_advanced_put_rejects_non_tier_b_keys(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _elevate(client)
    h = client.get("/api/config/advanced").json()["hash"]
    # A Tier-C key (auth master switch) and a Tier-A key both 400 — the Advanced allowlist
    # is exactly TIER_B, so neither a lockout switch nor an ordinary knob can be smuggled in.
    for smuggled in ({"auth.enabled": True}, {"usage.fx_rate": 2.0}):
        res = client.put(
            "/api/config/advanced",
            json={"edits": smuggled, "hash": h},
            headers={"origin": ORIGIN},
        )
        assert res.status_code == 400, smuggled


def test_advanced_put_requires_elevation(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _login(client)  # authed, not stepped up
    res = client.put(
        "/api/config/advanced",
        json={"edits": {"clone.timeout_seconds": 99}, "hash": "whatever"},
        headers={"origin": ORIGIN},
    )
    assert res.status_code == 403
    assert res.json()["detail"] == "reauth_required"


def test_advanced_put_stale_hash_conflicts(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _elevate(client)
    res = client.put(
        "/api/config/advanced",
        json={"edits": {"clone.timeout_seconds": 42}, "hash": "stale-hash"},
        headers={"origin": ORIGIN},
    )
    assert res.status_code == 409


def test_reauth_malformed_body_rejected(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _login(client)
    # A non-JSON body degrades to an empty password (never a 500).
    res = client.post(
        "/api/reauth",
        content=b"not json",
        headers={"origin": ORIGIN, "content-type": "application/json"},
    )
    assert res.status_code == 401
    # A non-dict JSON body (a bare list) likewise yields an empty password.
    res2 = client.post("/api/reauth", json=["x"], headers={"origin": ORIGIN})
    assert res2.status_code == 401


def test_advanced_get_falls_back_without_source_path(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _elevate(client)
    client.app.state.config._source_path = None  # in-memory config, no on-disk source
    body = client.get("/api/config/advanced").json()
    assert body["hash"] is None
    assert "clone.timeout_seconds" in body["fields"]  # served from the in-memory config


def test_advanced_put_no_source_path_conflicts(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _elevate(client)
    client.app.state.config._source_path = None
    res = client.put(
        "/api/config/advanced",
        json={"edits": {"clone.timeout_seconds": 5}, "hash": "x"},
        headers={"origin": ORIGIN},
    )
    assert res.status_code == 409


def test_advanced_put_empty_edits_or_missing_hash(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _elevate(client)
    empty = client.put(
        "/api/config/advanced", json={"edits": {}, "hash": "x"}, headers={"origin": ORIGIN}
    )
    assert empty.status_code == 422
    no_hash = client.put(
        "/api/config/advanced", json={"edits": {"clone.max_mb": 10}}, headers={"origin": ORIGIN}
    )
    assert no_hash.status_code == 422


def test_advanced_put_invalid_value_rejected(write_config, tmp_path):
    client, _ = _advanced_client(write_config, tmp_path)
    _elevate(client)
    h = client.get("/api/config/advanced").json()["hash"]
    # A Tier-B key whose value fails model validation -> 422, and nothing is written.
    res = client.put(
        "/api/config/advanced",
        json={"edits": {"clone.timeout_seconds": "not-an-int"}, "hash": h},
        headers={"origin": ORIGIN},
    )
    assert res.status_code == 422
