from __future__ import annotations

import hashlib
import hmac
import os
import sys

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


# ----- API tokens (#360) ---------------------------------------------------


def test_mint_token_shape_and_hash():
    raw, token_hash = auth.mint_token()
    assert raw.startswith("clauster_pat_")
    # 32 bytes urlsafe-base64 -> 43 chars after the prefix; high-entropy, no padding.
    assert len(raw) > len("clauster_pat_") + 40
    assert token_hash == auth.hash_token(raw)
    assert token_hash == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_mint_token_unique():
    raw1, _ = auth.mint_token()
    raw2, _ = auth.mint_token()
    assert raw1 != raw2  # fresh randomness each call


def test_verify_token_roundtrip():
    raw, token_hash = auth.mint_token()
    assert auth.verify_token(raw, token_hash) is True
    assert auth.verify_token(raw + "x", token_hash) is False
    assert auth.verify_token("clauster_pat_totally-wrong", token_hash) is False


def test_verify_token_fail_closed_without_configured_hash():
    # No token configured -> nothing authenticates (no oracle from a missing hash).
    raw, _ = auth.mint_token()
    assert auth.verify_token(raw, None) is False
    assert auth.verify_token(raw, "") is False


def test_verify_token_none_presented_is_false():
    _, token_hash = auth.mint_token()
    assert auth.verify_token(None, token_hash) is False
    assert auth.verify_token("", token_hash) is False


def test_verify_token_uses_constant_time_compare(monkeypatch):
    # Pin the constant-time guarantee: a regression to `==` would skip compare_digest.
    raw, token_hash = auth.mint_token()
    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert auth.verify_token(raw, token_hash) is True
    assert calls, "verify_token must route through hmac.compare_digest"


@pytest.mark.parametrize(
    ("header", "expected_key"),
    [
        ("Bearer clauster_pat_abc123", "clauster_pat_abc123"),
        ("bearer clauster_pat_abc123", "clauster_pat_abc123"),  # case-insensitive scheme
        ("BEARER clauster_pat_abc123", "clauster_pat_abc123"),
        ("Bearer   spaced  ", "spaced"),  # surrounding whitespace stripped
    ],
)
def test_parse_bearer_extracts_credential(header, expected_key):
    assert auth.parse_bearer(header) == expected_key


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Basic abc",
        "Bearer",
        "Bearer ",
        "Bearer    ",
        "Token abc",
        "abc",
        "Bearer tok1 tok2",  # embedded space rejected per RFC 6750 §2.1
    ],
)
def test_parse_bearer_rejects_malformed(header):
    assert auth.parse_bearer(header) is None


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


def test_session_expired(monkeypatch):
    # Deterministic expiry: drive itsdangerous' clock instead of a wall-clock
    # sleep. The token is signed at a fixed instant, then read one second later,
    # so it is reliably older than max_age=0 with no real-time wait.
    from itsdangerous.timed import TimestampSigner

    clock = {"now": 1_000_000}
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: clock["now"])

    s = auth.make_serializer(b"secret-key-0001")
    tok = auth.issue_session(s, "admin")
    clock["now"] += 1  # advance past the signing instant
    assert auth.read_session(s, tok, max_age=0) is None  # older than max_age


# ----- session epoch (logout revocation) ----------------------------------


def test_epoch_missing_is_zero(tmp_path):
    assert auth.read_epoch(tmp_path) == 0


def test_epoch_corrupt_is_zero(tmp_path):
    (tmp_path / "session.epoch").write_text("not-a-number")
    assert auth.read_epoch(tmp_path) == 0


def test_bump_epoch_increments_and_persists(tmp_path):
    assert auth.bump_epoch(tmp_path) == 1
    assert auth.read_epoch(tmp_path) == 1
    assert auth.bump_epoch(tmp_path) == 2
    assert auth.read_epoch(tmp_path) == 2


def test_bump_epoch_floored_against_memory_on_read_error(tmp_path, monkeypatch):
    # Regression: a transient read error must NOT lower the epoch to 1 (which
    # would un-revoke cookies issued before prior bumps). The in-memory floor
    # carries the real value, so the bump still advances past it.
    import pathlib

    (tmp_path / "session.epoch").write_text("7", encoding="utf-8")  # prior logouts
    real_read_text = pathlib.Path.read_text

    def boom(self, *args, **kwargs):
        if self.name == "session.epoch":
            raise PermissionError("simulated transient read error")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", boom)
    # Caller passes its last-known epoch (7) as the floor -> 8, never 1.
    assert auth.bump_epoch(tmp_path, floor=7) == 8
    assert (tmp_path / "session.epoch").read_bytes().decode().strip() == "8"


def test_bump_epoch_floor_beats_corrupt_disk(tmp_path):
    # A corrupt on-disk value reads as 0, but the floor prevents regression.
    (tmp_path / "session.epoch").write_text("garbage", encoding="utf-8")
    assert auth.bump_epoch(tmp_path, floor=7) == 8


def test_bump_epoch_disk_beats_lower_floor(tmp_path):
    # When the on-disk epoch is ahead of the floor (e.g. another process bumped
    # it), the disk value wins so the epoch is monotonic.
    (tmp_path / "session.epoch").write_text("5", encoding="utf-8")
    assert auth.bump_epoch(tmp_path, floor=2) == 6


def test_bump_epoch_empty_file_is_fresh_start(tmp_path):
    # An empty/missing file reads as 0; with no floor that's a fresh start.
    (tmp_path / "session.epoch").write_text("", encoding="utf-8")
    assert auth.bump_epoch(tmp_path) == 1


def test_session_rejected_when_epoch_stale():
    s = auth.make_serializer(b"secret-key-0001")
    tok = auth.issue_session(s, "admin", epoch=0)
    assert auth.read_session(s, tok, 3600, current_epoch=0) == "admin"  # same epoch
    assert auth.read_session(s, tok, 3600, current_epoch=1) is None  # bumped -> revoked


def test_session_newer_epoch_accepted():
    # A cookie issued at the current (or, defensively, a higher) epoch is valid.
    s = auth.make_serializer(b"secret-key-0001")
    tok = auth.issue_session(s, "admin", epoch=5)
    assert auth.read_session(s, tok, 3600, current_epoch=5) == "admin"
    assert auth.read_session(s, tok, 3600, current_epoch=4) == "admin"


def test_pre_epoch_cookie_valid_until_first_bump():
    # A cookie from before the feature (no "e" field) reads as epoch 0: still
    # valid at epoch 0, revoked once the epoch is bumped.
    s = auth.make_serializer(b"secret-key-0001")
    legacy = s.dumps({"u": "admin"})  # no "e"
    assert auth.read_session(s, legacy, 3600, current_epoch=0) == "admin"
    assert auth.read_session(s, legacy, 3600, current_epoch=1) is None


def test_session_non_dict_payload_rejected():
    # A validly-signed but non-dict payload is rejected (defensive guard).
    s = auth.make_serializer(b"secret-key-0001")
    assert auth.read_session(s, s.dumps("just-a-string"), 3600) is None


def test_session_malformed_epoch_reads_as_zero():
    # A signed cookie carrying a non-numeric "e" degrades to epoch 0 rather than
    # raising — resilient against a future/garbled payload shape.
    s = auth.make_serializer(b"secret-key-0001")
    tok = s.dumps({"u": "admin", "e": "bogus"})
    assert auth.read_session(s, tok, 3600, current_epoch=0) == "admin"
    assert auth.read_session(s, tok, 3600, current_epoch=1) is None


# ----- step-up elevation (#978) --------------------------------------------


def test_elevation_roundtrip():
    e = auth.make_elevation_serializer(b"secret-key-0001")
    assert auth.read_elevation(e, auth.issue_elevation(e, "admin"), 600) == "admin"


def test_elevation_expired(monkeypatch):
    # Deterministic expiry: drive itsdangerous' clock (same idiom as test_session_expired).
    from itsdangerous.timed import TimestampSigner

    clock = {"now": 1_000_000}
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: clock["now"])
    e = auth.make_elevation_serializer(b"secret-key-0001")
    tok = auth.issue_elevation(e, "admin")
    clock["now"] += 1  # advance past the signing instant
    assert auth.read_elevation(e, tok, max_age=0) is None  # older than the unlock window


def test_elevation_epoch_revocation():
    # A logout epoch bump revokes outstanding elevation tokens too (same embed as sessions).
    e = auth.make_elevation_serializer(b"secret-key-0001")
    tok = auth.issue_elevation(e, "admin", epoch=3)
    assert auth.read_elevation(e, tok, 600, current_epoch=3) == "admin"
    assert auth.read_elevation(e, tok, 600, current_epoch=4) is None


def test_session_and_elevation_tokens_are_not_interchangeable():
    # The distinct salt is the whole point: a session cookie must not verify as an
    # elevation token (which would grant Tier-B without a fresh password proof), and an
    # elevation token must not double as a session cookie.
    secret = b"secret-key-0001"
    s = auth.make_serializer(secret)
    e = auth.make_elevation_serializer(secret)
    session_tok = auth.issue_session(s, "admin")
    elevation_tok = auth.issue_elevation(e, "admin")
    assert auth.read_elevation(e, session_tok, 600) is None  # session ≠ elevation
    assert auth.read_session(s, elevation_tok, 3600) is None  # elevation ≠ session


# ----- reverse-proxy HMAC --------------------------------------------------


def _proxy_header(secret: str, user: str, method: str, path: str, t: int) -> str:
    sig = hmac.new(
        secret.encode(), f"{user}:{t}:{method}:{path}".encode(), hashlib.sha256
    ).hexdigest()
    return f"t={t},v1={sig}"


def test_proxy_hmac_valid_and_bindings():
    secret, t = "proxy-secret", 1000
    hdr = _proxy_header(secret, "alice", "GET", "/api/instances", t)
    ok = lambda **kw: auth.verify_proxy_hmac(  # noqa: E731
        secret,
        hdr,
        kw.get("u", "alice"),
        kw.get("m", "GET"),
        kw.get("p", "/api/instances"),
        60,
        now=kw.get("now", t),
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


def test_normalize_origin_ipv6_rebracketed():
    # urlsplit strips the brackets from an IPv6 host; normalize_origin must put
    # them back so the origin round-trips and matches the allowlist.
    assert auth.normalize_origin("http://[::1]:7621") == "http://[::1]:7621"
    assert auth.normalize_origin("HTTP://[::1]:80/") == "http://[::1]"
    assert auth.normalize_origin("https://[2001:DB8::1]:443") == "https://[2001:db8::1]"


def test_normalize_origin_malformed_port_does_not_raise():
    # urlsplit().port raises ValueError on an out-of-range / non-numeric port; the
    # CSRF origin gate must return a non-matching value (-> 403), never let that
    # escape and 500 on an attacker-supplied Origin header (the #122 .port class).
    assert auth.normalize_origin("http://x:99999") == "http://x:99999"
    assert auth.normalize_origin("http://x:notaport") == "http://x:notaport"


def test_normalize_origin_malformed_passthrough():
    # An origin with no scheme/hostname (e.g. the literal "null" Origin, or a bare
    # token) can't be structured; it degrades to a lowercased, slash-trimmed
    # passthrough that simply won't match the allowlist (fails closed).
    assert auth.normalize_origin("null") == "null"
    assert auth.normalize_origin("GARBAGE/") == "garbage"


def test_build_allowed_origins(tmp_path):
    loopback = ClausterConfig(projects_root=tmp_path, host="127.0.0.1", port=7621)
    origins = auth.build_allowed_origins(loopback)
    assert "http://127.0.0.1:7621" in origins
    assert "http://localhost:7621" in origins
    assert "http://[::1]:7621" in origins  # IPv6 loopback origin is allowed too

    # A non-loopback bind auto-allows nothing; operator must list origins explicitly.
    public = ClausterConfig(
        projects_root=tmp_path,
        host="0.0.0.0",
        port=7621,
        auth={
            "allow_unauthenticated_network": True,
            "allowed_origins": ["https://clauster.example.com"],
        },
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
        # A malformed CIDR entry is skipped, not fatal: a later valid entry still matches.
        ("10.0.0.5", ["garbage/cidr", "10.0.0.0/8"], True),
        # Only a malformed entry -> nothing to match -> untrusted (no crash).
        ("10.0.0.5", ["garbage/cidr"], False),
    ],
)
def test_peer_trusted(ip, nets, expected):
    assert auth.peer_trusted(ip, nets) is expected


# ----- session secret ------------------------------------------------------


def test_secret_persists_and_is_0600(tmp_path):
    s1 = auth.load_or_create_secret(tmp_path)
    s2 = auth.load_or_create_secret(tmp_path)
    assert s1 == s2 and len(s1) == 32
    if sys.platform == "win32":
        # Windows has no POSIX permission bits; chmod(0o600) is a no-op there.
        pytest.skip("POSIX file modes not enforced on Windows")
    assert (tmp_path / "session.secret").stat().st_mode & 0o777 == 0o600


def test_secret_create_fsyncs_parent_dir(tmp_path, monkeypatch):
    # Creating session.secret must fsync the parent dir, not just the file: fsync of the
    # file alone persists its data but not the directory entry, so a crash could drop the
    # new secret and rotate every session's signing key on restart.
    synced: list = []
    monkeypatch.setattr(auth, "fsync_dir", lambda d: synced.append(d))
    auth.load_or_create_secret(tmp_path)
    assert synced == [tmp_path]


def test_secret_env_override(tmp_path, monkeypatch):
    value = "x" * 40  # >= 32 bytes
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET", value)
    assert auth.load_or_create_secret(tmp_path) == value.encode()


def test_secret_env_too_short_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET", "too-short")
    with pytest.raises(ValueError, match="at least 32 bytes"):
        auth.load_or_create_secret(tmp_path)


def test_secret_env_file_override(tmp_path, monkeypatch):
    # CLAUSTER_SESSION_SECRET_FILE reads the secret from a file (trailing newline
    # stripped) so it stays out of the process environment (#368).
    value = "y" * 40  # >= 32 bytes
    secret_file = tmp_path / "session_secret"
    secret_file.write_text(value + "\n", encoding="utf-8")
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET_FILE", str(secret_file))
    assert auth.load_or_create_secret(tmp_path) == value.encode()


def test_secret_env_file_wins_over_plain(tmp_path, monkeypatch):
    secret_file = tmp_path / "session_secret"
    secret_file.write_text("f" * 40, encoding="utf-8")
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET", "e" * 40)
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET_FILE", str(secret_file))
    assert auth.load_or_create_secret(tmp_path) == ("f" * 40).encode()


def test_secret_env_file_unreadable_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="unreadable file"):
        auth.load_or_create_secret(tmp_path)


def test_secret_env_file_empty_fails_closed(tmp_path, monkeypatch):
    # A blank secret file would otherwise return "" and silently rotate the signing
    # key by falling through to a fresh disk secret — must fail closed instead.
    secret_file = tmp_path / "session_secret"
    secret_file.write_text("\n", encoding="utf-8")
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET_FILE", str(secret_file))
    with pytest.raises(ValueError, match="empty file"):
        auth.load_or_create_secret(tmp_path)


def test_secret_env_file_non_utf8_fails_closed(tmp_path, monkeypatch):
    secret_file = tmp_path / "session_secret"
    secret_file.write_bytes(b"\xff\xfe\x80sekret")
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET_FILE", str(secret_file))
    with pytest.raises(ValueError, match="not valid UTF-8") as exc:
        auth.load_or_create_secret(tmp_path)
    assert "sekret" not in str(exc.value)


def test_secret_env_file_blank_falls_through_to_plain(tmp_path, monkeypatch):
    # A blank _FILE is treated as unset, so the plain CLAUSTER_SESSION_SECRET applies.
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET_FILE", "   ")
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET", "s" * 40)
    assert auth.load_or_create_secret(tmp_path) == ("s" * 40).encode()


def test_secret_env_file_too_short_names_file_var(tmp_path, monkeypatch):
    # The too-short error must name the variable the value actually came from.
    secret_file = tmp_path / "session_secret"
    secret_file.write_text("short", encoding="utf-8")
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET_FILE", str(secret_file))
    with pytest.raises(ValueError, match="CLAUSTER_SESSION_SECRET_FILE must be at least 32 bytes"):
        auth.load_or_create_secret(tmp_path)


def test_secret_truncated_on_disk_is_rejected(tmp_path, monkeypatch):
    # A partial/corrupt session.secret (<32 bytes, e.g. a crash mid-write) must not be
    # used as a short signing key — refuse to boot rather than load it. Patch sleep so
    # the loser-retry loop finishes instantly.
    (tmp_path / "session.secret").write_bytes(b"short")  # 5 bytes
    monkeypatch.setattr(auth.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError, match="truncated"):
        auth.load_or_create_secret(tmp_path)


def test_secret_unreadable_on_disk_is_rejected(tmp_path, monkeypatch):
    # session.secret exists (so the O_EXCL create loses to it on every platform) but every
    # read raises — the OSError is absorbed and surfaced in the message so it's not
    # misreported as merely truncated. Force the read error directly rather than via a
    # directory: os.open of a directory raises PermissionError (not FileExistsError) on
    # Windows, before the read path is even reached.
    import pathlib

    def _unreadable(self):
        raise OSError("read denied")

    (tmp_path / "session.secret").write_bytes(b"")  # exists -> FileExistsError on O_EXCL
    monkeypatch.setattr(auth.time, "sleep", lambda *_: None)
    monkeypatch.setattr(pathlib.Path, "read_bytes", _unreadable)
    with pytest.raises(RuntimeError, match="last read error"):
        auth.load_or_create_secret(tmp_path)


def test_secret_short_write_fails_closed(tmp_path, monkeypatch):
    # A short os.write of the 32-byte secret would leave a truncated key on disk —
    # refuse to boot rather than persist and later sign with it. Force os.write to
    # report one byte short and assert the guard raises rather than fsyncing a stub.
    real_write = os.write

    def _short_write(fd, data):
        real_write(fd, data)  # actually write so the fd/file are consistent
        return len(data) - 1  # ...but report a short write

    monkeypatch.setattr(os, "write", _short_write)
    with pytest.raises(OSError, match="short write creating"):
        auth.load_or_create_secret(tmp_path)
