from __future__ import annotations

import pytest

from clauster import redact


def test_strip_ansi():
    assert redact.strip_ansi("\x1b[31mred\x1b[0m text") == "red text"
    assert redact.strip_ansi("plain") == "plain"


def test_redact_ids_keeps_prefix():
    assert redact.redact_ids("open env_01ABCDEFGHIJKLMNOP now") == "open env_<redacted> now"
    assert "session_<redacted>" in redact.redact_ids("session_01XYZABCDEFGHIJ hi")
    assert "cse_<redacted>" in redact.redact_ids("worker cse_01XYZABCDEFGHIJ")


def test_redact_ids_masks_bare_uuid():
    # organization_uuid / bridgeId style UUIDs must not survive the WS stream.
    out = redact.redact_ids('"organization_uuid":"fc0a4ee9-762e-42df-a376-484f5ff00f39"')
    assert "fc0a4ee9-762e-42df-a376-484f5ff00f39" not in out
    assert "<redacted>" in out
    # full sanitizer path, mixed with a bridge id.
    line = "[bridge:init] bridgeId=2d783407-cd32-4951-bba5-47fd9b82b8dc machine=claude-code"
    out = redact.sanitize_line(line)
    assert "2d783407-cd32-4951-bba5-47fd9b82b8dc" not in out
    assert "machine=claude-code" in out  # non-UUID context is untouched


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_abcdefghijklmnopqrstuvwxyz0123",  # GitHub classic token
        "github_pat_11ABCDEFGHIJKLMNOPQRST_uvwxyz0123456789",  # GitHub fine-grained PAT
        "glpat-abcdef1234567890XY",  # GitLab PAT
        "AKIAIOSFODNN7EXAMPLE",  # AWS access key id
        "sk-abcdefghijklmnop0123456789",  # OpenAI/Anthropic-style
        "sk-ant-api03-aB3_xY7zQ9rS2tU4vW6_zaBcDeFgHi",  # Anthropic key w/ underscores
        "xoxb-0123456789-abcdefABCDEF",  # Slack bot token
    ],
)
def test_redact_secrets_masks_each_shape(secret):
    # Every shape in _SECRET_RES is exercised, embedded in surrounding context.
    out = redact.redact_secrets(f"prefix {secret} suffix")
    assert secret not in out
    assert "<redacted>" in out
    assert out.startswith("prefix ") and out.endswith(" suffix")  # context untouched


def test_redact_secrets_bearer_header():
    out = redact.redact_secrets("Authorization: Bearer abcdef0123456789xyz")
    assert "abcdef0123456789xyz" not in out
    assert "<redacted>" in out


@pytest.mark.parametrize(
    "benign",
    [
        "tok ghp_tooshort here",  # below the {16,} quantifier — not a real token shape
        "the bearer of bad news",  # the word "bearer" with no 12+ char token after it
        "a skinny sk-cat ran",  # sk- but too short / wrong charset
        "plain prose with no secrets at all",
    ],
)
def test_redact_secrets_does_not_over_redact(benign):
    # Boundary/negative: near-misses must pass through verbatim (no over-masking).
    assert redact.redact_secrets(benign) == benign


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


def test_redact_for_disk_masks_session_url_and_secrets_over_a_chunk():
    # The at-rest on-disk redactor (logs.redact_session_url: true) runs over a
    # multi-line chunk, not one streamed line. It must mask the session-URL
    # identifiers and obvious secrets, and strip ANSI so a split id can't smuggle one.
    chunk = (
        "[bridge:init] Created initial session session_01ABCDEFGHIJKLMNOP\n"
        "open https://claude.ai/code/session_01ABCDEFGHIJKLMNOP\n"
        "\x1b[33menvironment_id=env_01ZZZZZZZZZZZZ\x1b[0m token sk-abcdef0123456789\n"
    )
    out = redact.redact_for_disk(chunk)
    assert "session_01" not in out
    assert "env_01" not in out
    assert "sk-abcdef0123456789" not in out
    assert "\x1b" not in out
    # Non-secret structure is preserved (so the log stays useful at rest).
    assert "Created initial session" in out
    assert "claude.ai/code/session_<redacted>" in out


def test_redact_screen_text_masks_ids_and_secrets_per_row():
    # The pty-screen view (#534) feeds already-rendered plaintext rows; redaction runs
    # per row and the id/secret masks fire just like the streamed-line path.
    rows = [
        "user@host:~$ echo env_01ABCDEFGHIJKLMNOP",
        "token sk-abcdef0123456789 ok",
        "plain row, nothing to hide",
    ]
    out = redact.redact_screen_text(rows)
    assert len(out) == len(rows)  # row count preserved (fixed terminal geometry)
    assert "env_01ABCDEFGHIJKLMNOP" not in out[0] and "env_<redacted>" in out[0]
    assert "sk-abcdef0123456789" not in out[1] and "<redacted>" in out[1]
    assert out[2] == rows[2]  # a benign row passes through verbatim


def test_redact_screen_text_empty_screen_is_empty():
    assert redact.redact_screen_text([]) == []


def test_sanitize_redacts_secret_split_by_ansi_even_when_strip_disabled():
    # ANSI bytes interleaved inside an identifier must NOT let it bypass redaction
    # when strip_ansi_in_stream is disabled. Redaction runs against a stripped view;
    # since the colored line would leak, we fall back to the stripped+redacted form
    # (color sacrificed for safety on that one line).
    line = "env_01ABCDEFG\x1b[0mHIJKLMNOP detail"
    out = redact.sanitize_line(line, strip_ansi_seq=False)
    assert "env_01ABCDEFGHIJKLMNOP" not in out
    assert "env_<redacted>" in out
    assert "\x1b" not in out  # fell back to the safe stripped form for this line
