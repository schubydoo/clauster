from __future__ import annotations

import time

import pytest

from clauster import redact


def test_strip_ansi():
    assert redact.strip_ansi("\x1b[31mred\x1b[0m text") == "red text"
    assert redact.strip_ansi("plain") == "plain"


@pytest.mark.parametrize(
    "sequence",
    [
        "\x1b]0;my title\x07",  # OSC 0 — icon name + window title, BEL
        "\x1b]0;my title\x1b\\",  # OSC 0 — ST
        "\x1b]2;my title\x07",  # OSC 2 — window title, BEL
        "\x1b]2;my title\x1b\\",  # OSC 2 — ST
        "\x1b]8;;https://evil.example/a\x07",  # OSC 8 — hyperlink target, BEL
        "\x1b]8;;https://evil.example/a\x1b\\",  # OSC 8 — ST
        "\x1b]52;c;c2VjcmV0\x07",  # OSC 52 — clipboard payload, BEL
        "\x1b]52;c;c2VjcmV0\x1b\\",  # OSC 52 — ST
    ],
)
def test_strip_ansi_removes_osc_whole(sequence):
    # #1329: `]` (0x5D) sits inside the two-character alternative's 0x5C-0x5F range, so
    # while that alternative came first the OSC one was unreachable and only `ESC ]` was
    # consumed — the payload (title / hyperlink / clipboard) survived as readable text,
    # plus a raw BEL byte, in everything sanitize_line streams to the browser.
    assert redact.strip_ansi(f"before {sequence}after") == "before after"


def test_strip_ansi_keeps_unterminated_osc_payload():
    # No terminator, so there is no OSC sequence to remove: the two-character `ESC ]`
    # introducer goes (as for any bare escape) and the rest is ordinary text.
    assert redact.strip_ansi("\x1b]0;never terminated") == "0;never terminated"


def test_strip_ansi_is_linear_on_osc():
    # ReDoS guard for the reordering: with the OSC alternative first, an OSC body of
    # `[^\x07]*` would rescan to the end of the input at every one of the N introducers.
    # That is quadratic — measured at ~2.6s for this N, growing 4x per doubling — while
    # excluding ESC from the body bounds each scan at the next escape and keeps the
    # sub() linear (single-digit ms). N is kept small enough that a regression fails the
    # assert rather than tripping the suite-wide `--timeout`, which kills the whole
    # xdist worker instead of reporting one red test.
    hostile = "\x1b]" * 20_000
    start = time.monotonic()
    assert redact.strip_ansi(hostile) == ""
    assert time.monotonic() - start < 1.0


def test_strip_ansi_keeps_an_osc8_label_that_is_itself_the_url():
    # The shape that gains from the fix: a hyperlink whose visible label is the URL. The
    # escape's own target is removed with the sequence; the label is ordinary text and
    # stays, so a scanner over the stripped view sees the URL exactly once.
    url = "https://claude.ai/oauth/authorize?real=1"
    assert redact.strip_ansi(f"\x1b]8;;{url}\x07{url}\x1b]8;;\x07") == url


def test_sanitize_line_does_not_stream_an_osc_payload():
    # End-to-end over the WS path: neither the title text, the BEL, nor the id inside it
    # may reach the browser.
    out = redact.sanitize_line("\x1b]0;env_01ABCDEFGHIJKLMNOP\x07ready")
    assert out == "ready"


def test_sanitize_line_still_redacts_inside_a_kept_osc_payload():
    # With ANSI stripping disabled the colored line is kept when it redacts to the same
    # text as the stripped view (redact.py's equality guard). Since strip_ansi now removes
    # the OSC payload from BOTH sides of that comparison, the guard is blind to what the
    # payload contains and always passes — so the only thing keeping an id out of the kept
    # line is the raw pass masking it in place. That is what this pins.
    out = redact.sanitize_line("\x1b]0;env_01ABCDEFGHIJKLMNOP\x07ready", strip_ansi_seq=False)
    assert "\x1b" in out  # the colored line really was kept, not silently stripped
    assert "env_01ABCDEFGHIJKLMNOP" not in out
    assert "env_<redacted>" in out


def test_redact_for_disk_never_swallows_lines_past_a_stray_osc_introducer():
    # redact_for_disk runs over a multi-line CHUNK, not one line, and feeds the public log
    # mirror plus `instance.error_detail` — the two surfaces an operator reads when a bridge
    # has already failed. A real OSC never spans a line, so the body excludes CR/LF: without
    # that, one stray `ESC ]` would scan to a BEL several lines later and delete everything
    # between, losing log content with no error and no marker (invariant 1).
    chunk = "line1 \x1b]stray\nline2 important\nline3 bell\x07 tail\nline4\n"
    out = redact.redact_for_disk(chunk)
    assert "line2 important" in out
    assert "line3 bell" in out
    assert out == "line1 stray\nline2 important\nline3 bell\x07 tail\nline4\n"


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
