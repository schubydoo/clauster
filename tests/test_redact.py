from __future__ import annotations

from clauster import redact


def test_strip_ansi():
    assert redact.strip_ansi("\x1b[31mred\x1b[0m text") == "red text"
    assert redact.strip_ansi("plain") == "plain"


def test_redact_ids_keeps_prefix():
    assert redact.redact_ids("open env_01ABCDEFGHIJKLMNOP now") == "open env_<redacted> now"
    assert "session_<redacted>" in redact.redact_ids("session_01XYZABCDEFGHIJ hi")
    assert "cse_<redacted>" in redact.redact_ids("worker cse_01XYZABCDEFGHIJ")


def test_redact_secrets():
    assert "<redacted>" in redact.redact_secrets("tok ghp_abcdefghijklmnopqrstuvwxyz0123")
    assert "<redacted>" in redact.redact_secrets("key AKIAIOSFODNN7EXAMPLE end")
    assert "<redacted>" in redact.redact_secrets("Authorization: Bearer abcdef0123456789xyz")


def test_sanitize_line_combines_ansi_and_ids():
    line = "\x1b[32m[bridge:api] environment_id=env_01ABCDEFGHIJKLMNOP\x1b[0m"
    out = redact.sanitize_line(line)
    assert "\x1b" not in out
    assert "env_01ABCDEFGHIJKLMNOP" not in out
    assert "env_<redacted>" in out


def test_no_env_or_session_id_ever_leaks():
    # The core D11 guarantee: no raw env_/session_/cse_ ULID survives the WS path.
    raw = "x env_01BCDEFGHIJKLMNOPQRSTUVWX session_01ZZZZZZZZZZZZZZZZZZZZZZ cse_01QQQQQQQQQQ y"
    out = redact.sanitize_line(raw)
    assert "env_01" not in out
    assert "session_01" not in out
    assert "cse_01" not in out


def test_sanitize_can_keep_ansi_when_disabled():
    assert "\x1b" in redact.sanitize_line("\x1b[31mhi\x1b[0m", strip_ansi_seq=False)
