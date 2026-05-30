from __future__ import annotations

import hashlib
import hmac

import pytest

from clauster import auth
from clauster.config import ClausterConfig


# ----- passwords -----------------------------------------------------------


def test_verify_password_runs_verify_even_without_hash():
    # Timing-defense pin: with no configured hash, verify_password must STILL run a
    # real argon2 verify (against the dummy) and never authenticate. Guards against a
    # regression that short-circuits `if stored_hash is None: return False`.
    class SpyHasher:
        called = False

        def verify(self, target, attempt):
            type(self).called = True
            return True

    spy = SpyHasher()
    assert auth.verify_password(spy, None, "anything") is False  # dummy never authenticates
    assert spy.called is True  # but the verify still ran


def test_password_roundtrip():
    h = auth.make_hasher()
    stored = auth.hash_password(h, "hunter2")
    assert auth.verify_password(h, stored, "hunter2") is True
    assert auth.verify_password(h, stored, "wrong") is False


def test_password_no_hash_is_false():
    h = auth.make_hasher()
    assert auth.verify_password(h, None, "anything") is False
    assert auth.verify_password(h, "", "anything") is False
    assert auth.verify_password(h, "not-a-valid-argon2-hash", "anything") is False


# ----- sessions ------------------------------------------------------------


def test_session_roundtrip():
    s = auth.make_serializer(b"secret-key-0001")
    assert auth.read_session(s, auth.issue_session(s, "admin"), 3600) == "admin"


def test_session_tampered_wrong_key_or_missing():
    s = auth.make_serializer(b"key-A")
    tok = auth.issue_session(s, "admin")
    assert auth.read_session(s, tok + "x", 3600) is None  # tampered
    assert auth.read_session(auth.make_serializer(b"key-B"), tok, 3600) is None  # wrong key
    assert auth.read_session(s, None, 3600) is None  # absent


def test_session_expired():
    import time

    s = auth.make_serializer(b"secret-key-0001")
    tok = auth.issue_session(s, "admin")
    time.sleep(1.1)
    assert auth.read_session(s, tok, max_age=0) is None  # older than max_age


# ----- reverse-proxy HMAC --------------------------------------------------


def _proxy_header(secret: str, user: str, method: str, path: str, t: int) -> str:
    sig = hmac.new(secret.encode(), f"{user}:{t}:{method}:{path}".encode(), hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def test_proxy_hmac_valid_and_bindings():
    secret, t = "proxy-secret", 1000
    hdr = _proxy_header(secret, "alice", "GET", "/api/instances", t)
    ok = lambda **kw: auth.verify_proxy_hmac(  # noqa: E731
        secret, hdr, kw.get("u", "alice"), kw.get("m", "GET"),
        kw.get("p", "/api/instances"), 60, now=kw.get("now", t)
    )
    assert ok() is True
    assert ok(now=t + 61) is False  # expired
    assert ok(now=t - 61) is False  # too far in the future
    assert ok(u="bob") is False  # wrong user
    assert ok(m="DELETE", p="/api/instances/x") is False  # replay on another endpoint
    assert auth.verify_proxy_hmac(secret, "garbage", "alice", "GET", "/", 60, now=t) is False
    assert auth.verify_proxy_hmac(secret, "t=abc,v1=zz", "alice", "GET", "/", 60, now=t) is False
    assert auth.verify_proxy_hmac(None, hdr, "alice", "GET", "/api/instances", 60, now=t) is False
    assert auth.verify_proxy_hmac(secret, hdr, None, "GET", "/api/instances", 60, now=t) is False


# ----- origins -------------------------------------------------------------


def test_normalize_origin():
    assert auth.normalize_origin("HTTP://Example.COM:80/") == "http://example.com"
    assert auth.normalize_origin("https://example.com:443") == "https://example.com"
    assert auth.normalize_origin("http://host:7621") == "http://host:7621"
    assert auth.normalize_origin("https://h.test/login?x=1") == "https://h.test"


def test_build_allowed_origins(tmp_path):
    loopback = ClausterConfig(projects_root=tmp_path, host="127.0.0.1", port=7621)
    origins = auth.build_allowed_origins(loopback)
    assert "http://127.0.0.1:7621" in origins
    assert "http://localhost:7621" in origins

    # A non-loopback bind auto-allows nothing; operator must list origins explicitly.
    public = ClausterConfig(
        projects_root=tmp_path, host="0.0.0.0", port=7621,
        auth={"allow_unauthenticated_network": True, "allowed_origins": ["https://clauster.example.com"]},
    )
    assert auth.build_allowed_origins(public) == {"https://clauster.example.com"}


# ----- peer trust ----------------------------------------------------------


@pytest.mark.parametrize(
    "ip,nets,expected",
    [
        ("127.0.0.1", ["127.0.0.1"], True),
        ("10.0.0.5", ["10.0.0.0/8"], True),
        ("10.0.0.5", ["192.168.0.0/16"], False),
        ("::1", ["::1"], True),
        ("::ffff:127.0.0.1", ["127.0.0.1"], True),  # v4-mapped normalization
        ("not-an-ip", ["127.0.0.1"], False),
        (None, ["127.0.0.1"], False),
        ("127.0.0.1", [], False),
    ],
)
def test_peer_trusted(ip, nets, expected):
    assert auth.peer_trusted(ip, nets) is expected


# ----- session secret ------------------------------------------------------


def test_secret_persists_and_is_0600(tmp_path):
    s1 = auth.load_or_create_secret(tmp_path)
    s2 = auth.load_or_create_secret(tmp_path)
    assert s1 == s2 and len(s1) == 32
    assert (tmp_path / "session.secret").stat().st_mode & 0o777 == 0o600


def test_secret_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET", "fixed-secret")
    assert auth.load_or_create_secret(tmp_path) == b"fixed-secret"
