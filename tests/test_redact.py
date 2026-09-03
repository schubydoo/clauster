from __future__ import annotations

import bisect
import random
import re
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
    # The stray BEL itself is now cut from the redaction view (#1370) — it renders as
    # nothing, so keeping it only hid an identifier split across it. CR/LF are never cut,
    # so the line structure this test guards is unchanged.
    assert out == "line1 stray\nline2 important\nline3 bell tail\nline4\n"


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
    # per row. This surface masks to the NEUTRAL <redacted> token (no readable prefix).
    rows = [
        "user@host:~$ echo env_01ABCDEFGHIJKLMNOP",
        "token sk-abcdef0123456789 ok",
        "plain row, nothing to hide",
    ]
    out = redact.redact_screen_text(rows)
    assert len(out) == len(rows)  # row count preserved (fixed terminal geometry)
    assert "env_01ABCDEFGHIJKLMNOP" not in out[0] and "<redacted>" in out[0]
    assert "sk-abcdef0123456789" not in out[1] and "<redacted>" in out[1]
    assert out[2] == rows[2]  # a benign row passes through verbatim


def test_redact_screen_text_empty_screen_is_empty():
    assert redact.redact_screen_text([]) == []


def test_redact_screen_text_masks_a_glued_prefix_weld():
    # pyte consumes the escape that welded a word onto an identifier, so `agent<ESC>env_<ULID>`
    # arrives here as the row `agentenv_<ULID>` with no cut to anchor on. The old anchored
    # screen pass left it bare — the leading `\b` fails after `t` — so the identifier reached
    # the live screen. This surface now fails closed and masks the glued identifier (#1433).
    row = f"agentenv_{_ULID}"
    out = redact.redact_screen_text([row])[0]
    assert _ULID not in out
    assert out == "agent<redacted>"  # neutral token; the `agent` prefix is not id-shaped
    # The old anchored pipeline (still the log path's behaviour) leaves the id bare: this is
    # the gap #1433 closes, and the assertion above fails on the pre-fix screen pass.
    assert _ULID in redact.redact_secrets(redact.redact_ids(row))


@pytest.mark.parametrize(
    "row",
    [
        # Finding 1: the glued SECRET cores (`sk-`/`glpat-`/`xoxb-`) would match inside an
        # ordinary hyphenated word if unanchored, so they stay ANCHORED on this surface. These
        # ordinary branch names and paths must pass through verbatim.
        "feat/task-queue-retry-backoff",
        "risk-assessment-checklist",
        "disk-usage-summary-2026",
        # An ordinary compound name that embeds an id prefix without the real id shape.
        "resolve_session_transcript",
        "venv_project1",
        "venv_01abc",  # too short after `01`
        "nothing to hide on this row",
    ],
)
def test_redact_screen_text_does_not_over_mask_ordinary_text(row):
    # The screen masks a welded REAL id, not any word. An ordinary branch name, path, or
    # snake_case identifier stays readable (#1433, the maintainer's tight core).
    assert redact.redact_screen_text([row]) == [row]


@pytest.mark.parametrize(
    ("row", "must_not_leak"),
    [
        # A glued id lands INSIDE a longer secret. Masking it first with a sequential `sub`
        # inserts a `<` that shortens the secret below its `{n,}` minimum, so the secret mask
        # then fails and its prefix leaks — masking LESS than the old pass (#1433 review). The
        # union pass covers every byte instead.
        ("Bearer abcenv_01ABCDEFGH", "Bearer abc"),
        ("sk-AAAAenv_01ABCDEFGH", "sk-AAAA"),
    ],
)
def test_redact_screen_text_never_masks_less_than_the_anchored_pass(row, must_not_leak):
    # Parity floor for the screen surface: a prefix the old sequential redact_ids/redact_secrets
    # pass masked must stay masked. The `<` a mask inserts must never expose a neighbour.
    old = redact.redact_secrets(redact.redact_ids(row))
    assert must_not_leak not in old, "control: the anchored pass already masked this prefix"
    assert must_not_leak not in redact.redact_screen_text([row])[0], row


def test_redact_screen_text_masks_both_ids_in_an_id_to_id_weld():
    # Two real ids welded with no boundary between them. The NEUTRAL <redacted> token is what
    # closes this: masking the second id inserts a `<` that gives the first id the trailing
    # boundary it lacked, and the fixed point then masks it too. Keeping a `session_` prefix
    # (word chars) would leave the first id bare (#1433 review).
    out = redact.redact_screen_text(["env_01ABCDEFGHsession_01ABCDEFGH"])[0]
    assert out == "<redacted><redacted>"
    assert "01ABCDEFGH" not in out


def test_redact_screen_text_leaves_a_welded_secret_as_residue():
    # Documented residue on this cut-less surface (#1433): a secret welded onto a word (no
    # boundary) is NOT masked, because the secret cores stay anchored to avoid destroying
    # ordinary text (see test_redact_screen_text_does_not_over_mask_ordinary_text). The old
    # log-path pass leaves it too, so the screen masks no less than before.
    row = "agentghp_abcdefghijklmnopqrstuv"
    assert redact.redact_screen_text([row]) == [row]
    assert redact.redact_secrets(redact.redact_ids(row)) == row


@pytest.mark.parametrize(
    ("row", "masked"),
    [
        # A glued id is masked ONLY when it has the real id shape (prefix + `01` + eight or
        # more), so a live weld cannot leave a bearer-equivalent id readable...
        ("agentenv_01BX5ZZKBKACTAV9WEVGEMMVRZ", True),
        ("prefixsession_01ABCDEFGH", True),  # a different glued prefix, real id shape
        # ...but an ordinary compound name that merely embeds the prefix stays readable.
        ("resolve_session_transcript", False),
        ("venv_project1", False),
        ("venv_01abc", False),  # too short after `01`
    ],
)
def test_redact_screen_text_masks_only_a_glued_real_id(row, masked):
    # The maintainer's tight core for #1433: the glued (no-leading-boundary) id match requires
    # `(env|session|cse)_01` + eight or more, so the screen masks a welded REAL id without
    # over-masking ordinary snake_case names.
    out = redact.redact_screen_text([row])[0]
    if masked:
        assert "<redacted>" in out and row not in out, out
    else:
        assert out == row, out


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


# ---------------------------------------------------------------------------
# #1379 / #1344 / #1370 — the escape-weld + control-char-split family.
#
# `strip_ansi` removes a sequence and leaves no trace of WHERE it was, so after
# stripping a welded id (`user<ESC>[32menv_<ULID>`) is byte-identical to legitimate
# compound text (`userenv_production`). The fix records the cut offsets and re-tries the
# masks anchored at each one. These pin both directions: the weld/split shapes must mask,
# and text with no escapes must be byte-identical to what main produced.
# ---------------------------------------------------------------------------

_ULID = "01ABCDEFGHJKMNPQRSTVWXYZ01"


@pytest.mark.parametrize(
    ("shape", "line"),
    [
        # The one that leaks on main through the single most common escape there is.
        ("csi weld", f"agent\x1b[32menv_{_ULID}\x1b[0m done"),
        ("dcs weld", f"user\x1bPq junk\x07env_{_ULID} x"),
        ("sos weld", f"user\x1bXsos\x07env_{_ULID} x"),
        ("pm weld", f"user\x1b^pm\x1b\\env_{_ULID} x"),
        ("apc weld", f"user\x1b_apc\x07env_{_ULID} x"),
        ("osc 8 hyperlink weld", f"a\x1b]8;;https://e/x\x07env_{_ULID}\x1b]8;;\x07 b"),
        ("bare C0 weld, no ESC", f"user\x07env_{_ULID} x"),
        ("DEL weld", f"user\x7fenv_{_ULID} x"),
        ("lone ESC weld", f"user\x1benv_{_ULID} x"),
        ("8-bit C1 weld", f"user\x9benv_{_ULID} x"),
        ("nested sequences", f"a\x1b[1m\x1b]0;title\x07env_{_ULID} x"),
        ("adjacent sequences", f"a\x1b[1m\x1b[32menv_{_ULID} x"),
        # Split shapes: the id's own start is still a real word boundary, but the
        # control char inside it used to break the {6,} run into two short fragments.
        ("csi split", "env_01ABCDEFG\x1b[0mHIJKLMNOP tail"),
        ("bare C0 split, no ESC (#1370)", "env_01AB\x07CDEFGH"),
        ("8-bit C1 split", "env_01AB\x9bCDEFGH"),
        # Default-ignorable Unicode splits: each half is too short for {6,} on its own, so
        # before #1434 the char stayed in the rendered view and the id reached the reader
        # whole. Each mirrors the BEL split above with a code point a `<pre>` shows nothing
        # for. Positive control: without widening `_INVISIBLE_PATTERN` the char is not cut,
        # the halves stay split, no mask fires, `env_<redacted>` is absent, so each case fails.
        # The first group is `Cf` (format); the second is default-ignorable but NOT `Cf` (a
        # variation selector, the combining grapheme joiner, a Hangul filler, a supplement
        # selector) — the leak a `Cf`-only strip would have left one code point over.
        ("zero-width space split (U+200B)", "env_01AB\u200bCDEFGH"),
        ("zero-width non-joiner split (U+200C)", "env_01AB\u200cCDEFGH"),
        ("zero-width joiner split (U+200D)", "env_01AB\u200dCDEFGH"),
        ("word joiner split (U+2060)", "env_01AB\u2060CDEFGH"),
        ("BOM / ZWNBSP split (U+FEFF)", "env_01AB\ufeffCDEFGH"),
        ("soft hyphen split (U+00AD)", "env_01AB\u00adCDEFGH"),
        ("bidi override split (U+202E)", "env_01AB\u202eCDEFGH"),
        ("tag-block split (U+E0041)", "env_01AB\U000e0041CDEFGH"),
        ("variation selector split (U+FE0F)", "env_01AB\ufe0fCDEFGH"),
        ("combining grapheme joiner split (U+034F)", "env_01AB\u034fCDEFGH"),
        ("hangul filler split (U+3164)", "env_01AB\u3164CDEFGH"),
        ("variation selector supplement split (U+E0100)", "env_01AB\U000e0100CDEFGH"),
        # Default-ignorable WELDs, not splits: the char sits BEFORE the id and deletes the word
        # boundary its `\b` needs, so the id start is supplied by the cut instead. Drives
        # `_cut_spans` (source 2), where the splits above drive the `\b`-anchored pass. The
        # positive control here is a Hangul filler (category `Lo`, a word character), so `\b`
        # does NOT hold after it and the id welds \u2014 main leaks it whole. The U+200B weld is a
        # weaker case (a `Cf` char is not a word character, so `\b` already holds after it and
        # main masks it), kept only to exercise `_cut_spans` on the fixed path.
        ("hangul-filler weld (U+3164)", f"user\u3164env_{_ULID} x"),
        ("zero-width weld (U+200B)", f"user\u200benv_{_ULID} x"),
        # Two sequences in opposite directions on one token: one welds the start away,
        # the other splits the body.
        ("weld + split", "a\x1b[32menv_01ABCDEFG\x1b[0mHIJKLMNOP tail"),
    ],
)
def test_sanitize_line_masks_a_welded_or_split_identifier(shape, line):
    out = redact.sanitize_line(line)
    assert "env_<redacted>" in out, shape
    assert _ULID not in out and "01ABCDEFG" not in out, shape


def test_invisible_pattern_is_default_ignorable_not_cf():
    # #1434: the widened set must be the Unicode Default_Ignorable_Code_Point property (what a
    # `<pre>` renders as nothing), NOT the `Cf` category. This DERIVES that property from
    # `unicodedata` and asserts `_INVISIBLE_RE` covers all of it, so a Unicode bump that adds a
    # default-ignorable code point outside the frozen ranges (or a dropped range endpoint) reds
    # here rather than becoming a silent weld leak. The component sets are the fixed Unicode
    # properties; only `Cf` is version-varying, and it is read live.
    import sys
    import unicodedata

    variation_selector = (
        set(range(0x180B, 0x180E))
        | {0x180F}
        | set(range(0xFE00, 0xFE10))
        | set(range(0xE0100, 0xE01F0))
    )
    other_default_ignorable = (
        {0x034F}
        | set(range(0x115F, 0x1161))
        | set(range(0x17B4, 0x17B6))
        | {0x2065, 0x3164, 0xFFA0}
        | set(range(0xFFF0, 0xFFF9))
        | {0xE0000}
        | set(range(0xE0002, 0xE0020))
        | set(range(0xE0080, 0xE0100))
        | set(range(0xE01F0, 0xE1000))
    )
    # Cf code points Unicode EXCLUDES from Default_Ignorable because they DO render:
    prepended_concatenation_mark = {
        0x0600,
        0x0601,
        0x0602,
        0x0603,
        0x0604,
        0x0605,
        0x06DD,
        0x070F,
        0x0890,
        0x0891,
        0x08E2,
        0x110BD,
        0x110CD,
    }
    other_excluded = set(range(0xFFF9, 0xFFFC)) | set(
        range(0x13430, 0x13440)
    )  # interlinear, Egyptian
    cf = {c for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Cf"}
    default_ignorable = (
        (cf | variation_selector | other_default_ignorable)
        - prepended_concatenation_mark
        - other_excluded
    )

    controls = (
        set(range(0x00, 0x09)) | {0x0B, 0x0C} | set(range(0x0E, 0x20)) | set(range(0x7F, 0xA0))
    )
    stripped = {cp for cp in range(sys.maxunicode + 1) if redact._INVISIBLE_RE.fullmatch(chr(cp))}

    # Leak direction: EVERY default-ignorable code point must be stripped, or it welds. One here
    # but not in `stripped` is a member `_INVISIBLE_PATTERN` must gain — UNLESS a Unicode bump
    # made it a prepended-concatenation mark that now renders, in which case it belongs in
    # `prepended_concatenation_mark` above, not in the pattern.
    missed = sorted(default_ignorable - stripped)
    assert not missed, (
        f"default-ignorable code points not stripped: {[hex(c) for c in missed[:20]]} — widen "
        "_INVISIBLE_PATTERN, or if the new point RENDERS add it to prepended_concatenation_mark"
    )
    # Over-strip direction, EXACT rather than a sample: nothing beyond the controls and the derived
    # set may be stripped, or a range endpoint has overshot into visible text (e.g. `⁠-ⁿ`
    # swallowing the superscripts, or a raw-`Cf` widening eating ARABIC END OF AYAH). The visible
    # prepended-concatenation marks, U+2028/U+2029 and TAB/CR/LF all fall outside `stripped` here.
    over = sorted(stripped - controls - default_ignorable)
    assert not over, (
        f"code points stripped that are NOT default-ignorable: {[hex(c) for c in over[:20]]} — "
        "narrow _INVISIBLE_PATTERN; it must never delete a character a browser draws"
    )


@pytest.mark.parametrize(
    ("shape", "line", "gone"),
    [
        ("uuid weld", "user\x1b[32m2d783407-cd32-4951-bba5-47fd9b82b8dc x", "2d783407"),
        ("ghp weld", "user\x1b[32mghp_abcdefghijklmnopqrstuvwxyz0123 x", "ghp_abcdef"),
        ("AKIA split", "AKIAIOSF\x07ODNN7EXAMPLE x", "AKIAIOSF"),
        ("bearer weld", "hdr:\x1b[1mBearer abcdef0123456789xyz x", "abcdef0123456789xyz"),
    ],
)
def test_sanitize_line_masks_a_welded_or_split_uuid_or_secret(shape, line, gone):
    # The cut-anchored pass covers every mask in the module, not only `_ID_RE` — a UUID or
    # a listed token shape welded by an escape is the same bug with a different pattern.
    out = redact.sanitize_line(line)
    assert "<redacted>" in out, shape
    assert gone not in out, shape


@pytest.mark.parametrize(
    "text",
    [
        "userenv_production is fine",  # the documented residue: no escape, no cut
        "myenv_foobarbaz ran",
        "a normal INFO line with no escapes at all",
        "path/to/xcse_foobarbaz.log",
    ],
)
def test_no_escape_means_byte_identical_output(text):
    # The regression bound: with no escapes there are no cuts, so the cut-anchored pass is
    # the identity and the output is exactly what the `\b`-anchored masks alone produce.
    # This is also what rules out the over-masking of the rejected unanchored re-scan.
    assert redact.sanitize_line(text) == redact.redact_secrets(redact.redact_ids(text))
    assert redact.sanitize_line(text) == text


def test_a_cut_inside_an_already_masked_span_is_not_re_scanned():
    # Two cuts land inside one identifier: the first anchors the mask, the second falls
    # inside the span it consumed. Re-scanning from there would emit a second, overlapping
    # replacement and corrupt the line, so later cuts inside a masked span are skipped.
    line = "user\x1b[32menv_01ABCDEF\x1b[0mGHIJKLMN\x1b[0mOPQRST tail"
    out = redact.sanitize_line(line)
    assert out == "userenv_<redacted> tail"


def test_a_second_welded_identifier_after_a_cut_bounded_span_is_masked():
    # Only a span that ended at a real `\b` may skip the cuts inside it. Here `session_AAAcse`
    # fullmatches to the cut before `_` (no real trailing `\b`), and `cse_AAAAAA` is welded to
    # start at an interior cut. Skipping past a cut-bounded span dropped the `cse` mask and
    # streamed its core `AAAAAA` — a reachable identifier left partly readable (#1379).
    out = redact.sanitize_line("x\x1b[msession_AAA\x1b[mcse\x1b[m_AAAAAA z")
    assert "AAAAAA" not in out
    assert out == "xsession_<redacted><redacted> z"


def test_strip_ansi_removes_the_c1_string_sequences_whole():
    # #1344: DCS/SOS/PM/APC lost only their two-character introducer, so the payload
    # reached the stream as readable junk exactly as OSC did before #1329.
    for sequence in (
        "\x1bPq payload\x1b\\",
        "\x1bXpayload\x07",
        "\x1b^payload\x1b\\",
        "\x1b_payload\x07",
    ):
        assert redact.strip_ansi(f"before {sequence}after") == "before after"


def test_strip_ansi_keeps_an_unterminated_c1_string_payload():
    # Same discipline as the unterminated OSC: with no terminator there is no sequence to
    # remove, so only the two-character introducer goes.
    assert redact.strip_ansi("\x1bPq never terminated") == "q never terminated"


def test_strip_ansi_is_linear_on_the_c1_string_introducers():
    # The #1329 ReDoS guard, re-run over the introducers #1344 added to the same branch.
    hostile = "\x1b_" * 20_000
    start = time.monotonic()
    assert redact.strip_ansi(hostile) == ""
    assert time.monotonic() - start < 1.0


def test_strip_ansi_leaves_bare_control_characters_alone():
    # `strip_ansi` is the DISPLAY strip other modules scan against (pty_screen,
    # login_shepherd). Only the redaction view removes invisible controls, so widening
    # this one would silently change what those scanners see.
    assert redact.strip_ansi("a\x07b\x9bc\x7fd") == "a\x07b\x9bc\x7fd"


def test_views_report_ascending_deduplicated_cuts():
    # The cut offsets are positions in the VISIBLE view, ascending, with adjacent sequences
    # collapsing to one cut — anchoring twice at the same offset is wasted work. Cuts from
    # the escape strip and from the invisible-control strip land in one sorted tuple.
    stripped, visible, cuts, _ = redact._views("ab\x1b[1m\x1b[32mcd\x07ef")
    assert stripped == "abcd\x07ef"
    assert visible == "abcdef"
    assert cuts == (2, 4)
    assert redact._views("no escapes here")[2] == ()


def test_redact_for_disk_keeps_line_structure_while_cutting_invisible_controls():
    # CR/LF and TAB are visible separators and are never cut, so the multi-line chunk this
    # feeds (the public log mirror, `error_detail`) keeps its shape while a welded id in it
    # is still masked.
    chunk = f"col1\tcol2\r\nrow \x1b[32menv_{_ULID}\x1b[0m\r\ntail\n"
    out = redact.redact_for_disk(chunk)
    assert out == "col1\tcol2\r\nrow env_<redacted>\r\ntail\n"


def test_sanitize_line_falls_back_when_a_weld_only_shows_in_the_stripped_view():
    # With ANSI stripping disabled the colored line is kept only if it is provably as
    # redacted as the rendered view. A weld is invisible to the colored pass (`\b` still
    # holds around the escape), so the guard must fall back rather than stream the id.
    out = redact.sanitize_line(f"agent\x1b[32menv_{_ULID}\x1b[0m done", strip_ansi_seq=False)
    assert _ULID not in out
    assert "env_<redacted>" in out
    assert "\x1b" not in out


def test_cut_masks_cannot_drift_from_the_anchored_ones():
    # Each mask is written once as a bare core and compiled three ways: `\b`core`\b` for the
    # base passes, core`\b` for a cut-supplied start, core alone for cut-supplied ends. If a
    # future edit adds a shape to only one tuple, this fails rather than leaving a welded
    # instance of that shape unmasked.
    expected = [redact._ID_RE, redact._UUID_RE, *redact._SECRET_RES]
    assert len(expected) == len(redact._MASKS)
    for wanted, (anchored, opened, closed, _kp, _sr) in zip(expected, redact._MASKS, strict=True):
        assert anchored is wanted
        assert anchored.pattern == rf"\b{opened.pattern}\b"
        assert closed.pattern == rf"{opened.pattern}\b"
        assert anchored.flags == opened.flags == closed.flags


def test_single_run_flag_excludes_the_fixed_and_whitespace_cores():
    # `single_run` marks a core `_cut_spans` may mask a whole run of and skip. It MUST be
    # False for the fixed-length cores (a second match can start inside one run and end past
    # it) and for bearer (internal whitespace lets a match resume past the run) — #1379.
    single = {opened.pattern: sr for _a, opened, _c, _kp, sr in redact._MASKS}
    assert single[redact._UUID_CORE] is False
    assert single[r"AKIA[0-9A-Z]{16}"] is False
    assert single[r"bearer\s+[A-Za-z0-9._-]{12,}"] is False
    assert single[redact._ID_CORE] is True
    assert single[r"sk-[A-Za-z0-9_-]{16,}"] is True


def test_is_single_class_run_rejects_whitespace_in_either_form():
    # The check reads the pattern text, so it must reject whitespace written as the `\s`
    # escape AND as a literal space. A future core with a literal space would otherwise pass
    # and make the whole-run skip unsound (its run ends at the space a second match needs).
    assert redact._is_single_class_run(r"bearer\s+[A-Za-z0-9._-]{12,}") is False
    assert redact._is_single_class_run(r"bearer +[A-Za-z0-9._-]{12,}") is False
    assert redact._is_single_class_run("bearer\t+[A-Za-z0-9._-]{12,}") is False
    # A whitespace-free open-ended class run is still accepted.
    assert redact._is_single_class_run(r"sk-[A-Za-z0-9_-]{16,}") is True


@pytest.mark.parametrize(
    ("shape", "line"),
    [
        # A cut deletes the boundary that USED to END the identifier just as effectively as
        # the one before it: every mask ends in `\b`, so `session_<ULID><BEL>_x` was masked
        # before this change and the joined `session_<ULID>_x` is not. Covering only the
        # leading side would have made the fix itself introduce a leak — found by
        # `fuzz/redact_fuzzer.py`'s render oracle, not by review.
        ("bare C0 before an underscore", "session_01ABCDEF\x07_x"),
        ("OSC before an underscore", "env_01ABCDEFGH\x1b]0;t\x07_x"),
        ("weld at the start AND at the end", "u\x1b[1menv_01ABCDEFGH\x07_x"),
        ("split, then a weld at the end", "env_01AB\x07CDEFGH\x07_x"),
    ],
)
def test_a_cut_also_supplies_the_trailing_boundary(shape, line):
    out = redact.sanitize_line(line)
    assert "01ABCDEF" not in out, shape
    assert "env_<redacted>" in out or "session_<redacted>" in out, shape


def test_masking_never_drops_below_the_pre_change_pipeline():
    # The parity property as a test rather than a comment: the fourth span source in
    # `_sanitize` is the old strip-then-mask view, so whatever it masked is still masked. This
    # is the exact line the fuzzer caught the fix regressing.
    line = "session_01ABCDEF\x07_x"
    assert "<redacted>" in redact.redact_secrets(redact.redact_ids(redact.strip_ansi(line)))
    assert "<redacted>" in redact.sanitize_line(line)


def test_an_identifier_the_attacker_wrote_mid_token_is_documented_residue():
    # The residue `_sanitize` states: the id's start is neither a word boundary nor a cut,
    # so nothing distinguishes it from ordinary compound text. Escapes elsewhere on the line
    # do not manufacture a boundary for it.
    out = redact.sanitize_line("\x1b[32mprefix\x1b[0m userenv_01ABCDEFGHIJ tail")
    assert out == "prefix userenv_01ABCDEFGHIJ tail"


def test_sanitize_line_is_linear_in_the_number_of_cuts():
    # The cut machinery adds a second scan and a bisect per mask. Both are linear, but the
    # obvious alternative — walking back from each cut to find a match that ends there —
    # is quadratic when one long token holds thousands of cuts, which is trivial for a
    # bridge to emit. Measured ~0.15s for this N and growing 2x per doubling; the bound is
    # loose enough not to flake on a slow CI runner but tight enough to fail a quadratic
    # regression, and small enough that it fails the assert rather than tripping the
    # suite-wide `--timeout` (which kills the whole xdist worker).
    hostile = ("env_01ABCDEFGH\x07" + "x" * 10) * 20_000
    start = time.monotonic()
    redact.sanitize_line(hostile)
    assert time.monotonic() - start < 5.0


@pytest.mark.parametrize(
    ("shape", "line", "expected"),
    [
        # Welded at the start, but the run then continues into a longer token with no cut
        # to end it: the trailing `\b` fails and no cut can stand in for it, so this stays
        # residue rather than masking a prefix of somebody's compound word.
        ("no usable cut end", "a\x1b[1menv_01ABCDEFGH_x", "aenv_01ABCDEFGH_x"),
        # A cut IS in range this time, but the run it would end is only two characters
        # long — below the mask's `{6,}` — so shrinking to it does not produce an
        # identifier either.
        ("cut end too short", "a\x1b[1menv_01\x07CDEFGH_x", "aenv_01CDEFGH_x"),
    ],
)
def test_a_cut_start_without_a_usable_end_masks_nothing(shape, line, expected):
    assert redact.sanitize_line(line) == expected, shape


def _legacy(text: str) -> str:
    """The pre-#1379 pipeline, as the visible text it produced."""
    return redact._INVISIBLE_RE.sub(
        "", redact.redact_secrets(redact.redact_ids(redact.strip_ansi(text)))
    )


@pytest.mark.parametrize(
    ("shape", "line", "canary"),
    [
        # A short id span landing INSIDE a long token span. Resolving the overlap by
        # dropping the long span left everything past the id unmasked — masking LESS than
        # the pipeline this replaces, which is the one thing the design must never do.
        # `_apply_spans` clips instead, so both are covered.
        (
            "id nested in a longer token",
            "clauster_pat_" + "H" * 16 + "\x1b[menv_AAAAAA-Xenv_BBBBBB",
            "env_BBBBBB",
        ),
        (
            "token grown across a cut",
            "ghp_" + "A" * 16 + "\x0cglpat-" + "B" * 16,
            "B" * 16,
        ),
    ],
)
def test_an_overlapping_span_is_clipped_not_dropped(shape, line, canary):
    assert canary not in _legacy(line), f"{shape}: the control is not a parity case"
    assert canary not in redact.sanitize_line(line), shape


#: Tokens split on every character no mask can span, so a leak inside a longer run of
#: punctuation-joined text is still seen. A coarser split hides exactly the defect these
#: pin: the leaked `env_BBBBBB` above sits inside one `-`-joined 50-character token.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{6,}")


@pytest.mark.parametrize(
    "line",
    [
        "clauster_pat_" + "H" * 16 + "\x1b[menv_AAAAAA-Xenv_BBBBBB",
        "\x1b[32menv_01ABCDEFGH\x1b[0m and session_01ZZZZZZZZ\x07_tail",
        "Bearer " + "J" * 20 + "\x1b]0;t\x07AKIAIOSFODNN7EXAMPLE",
        "user\x9b2d783407-cd32-4951-bba5-47fd9b82b8dc\x7fsk-abcdefghijklmnop0123456789",
        "\x1bPq p\x1b\\ghp_abcdefghijklmnopqrstuvwxyz0123\x00xoxb-0123456789-abcdefABCDEF",
    ],
)
def test_sanitize_line_never_masks_less_than_the_pipeline_it_replaces(line):
    # The parity floor, stated as a property: the fourth span source in `_sanitize` is the
    # old strip-then-mask view, so every token that pipeline removed is still removed. The
    # same property held over 400,000 randomly assembled lines while this was written.
    old = _legacy(line)
    new = redact.sanitize_line(line)
    for token in _TOKEN_RE.findall(redact._INVISIBLE_RE.sub("", redact.strip_ansi(line))):
        if token not in old:
            assert token not in new, f"{token!r} was masked before this change and is not now"


def test_a_cut_at_every_greedy_match_start_stays_linear():
    # Each cut anchors a match, and `sk-[A-Za-z0-9_-]{16,}` is greedy, so without the
    # already-covered skip in `_cut_spans` every cut scans to the end of the input: 640 KB
    # of this shape took 8.3 seconds. `redact_for_disk` is handed a whole bridge log
    # (`bridge_log_max_size_mb`, 10 MB by default), so the input size is the bridge's to
    # choose. Measured ~0.2s for this N and growing 2x per doubling.
    hostile = ("\x01sk-" + "A" * 16) * 40_000
    start = time.monotonic()
    redact.sanitize_line(hostile)
    assert time.monotonic() - start < 5.0


def _cut_spans_anchoring_every_cut(visible, cuts, opened, closed, keeps_prefix, _single_run):
    """Reference `_cut_spans` with no already-covered skip: anchor at every cut in turn."""
    spans = []
    for cut in cuts:
        hit = closed.match(visible, cut)
        if hit is not None:
            spans.append(redact._span(hit, hit.end(), keeps_prefix))
            continue
        loose = opened.match(visible, cut)
        if loose is None:
            continue
        candidate = bisect.bisect_right(cuts, loose.end()) - 1
        if candidate < 0 or cuts[candidate] <= cut:
            continue
        hit = opened.fullmatch(visible, cut, cuts[candidate])
        if hit is not None:
            spans.append(redact._span(hit, cuts[candidate], keeps_prefix))
    return spans


def _mask_coverage(cut_spans_fn, text):
    """Return the bytearray of visible offsets any mask span covers, using `cut_spans_fn`."""
    _stripped, visible, cuts, invisible = redact._views(text)
    covered = bytearray(len(visible))
    for anchored, opened, closed, keeps_prefix, single_run in redact._MASKS:
        spans = [redact._span(m, m.end(), keeps_prefix) for m in anchored.finditer(visible)]
        spans += cut_spans_fn(visible, cuts, opened, closed, keeps_prefix, single_run)
        spans += redact._trailing_cut_spans(visible, cuts, opened, keeps_prefix)
        spans += [
            (redact._map_offset(m.start(), invisible), redact._map_offset(m.end(), invisible), "")
            for m in anchored.finditer(_stripped)
        ]
        for start, end, _replacement in spans:
            covered[start:end] = b"\x01" * (end - start)
    return covered


def _never_masks_less(line):
    """Assert production covers every character anchoring at every cut would (no under-mask)."""
    reference = _mask_coverage(_cut_spans_anchoring_every_cut, line)
    production = _mask_coverage(redact._cut_spans, line)
    assert len(reference) == len(production)
    missed = [i for i, (r, p) in enumerate(zip(reference, production, strict=True)) if r and not p]
    assert not missed, f"production masks less at {missed} for {line!r}"


# Deterministic reproducers, each a cut-bounded run whose interior cut starts a second welded
# identifier the buggy `reach` skip dropped. The first three are the `session_AAAcse` fullmatch
# family; the fourth is the `closed`-backtrack family a `-`-bearing class exposes; the fifth is
# the fixed-length family (two UUIDs sharing eight hex digits, welded), where `opened`'s end is
# not the class-run end so a whole-run mask must NOT be used.
_REACH_UNDERMASK_LINES = [
    "x\x1b[msession_AAA\x1b[mcse\x1b[m_AAAAAA z",
    "\x1b[msession_AAA\x1b[mcse\x1b[m_BBBBBB\x1b[menv_CCCCCC ",
    "u\x01session_AAA\x01cse\x01_DDDDDD\x01env_EEEEEE tail",
    "Z\x1b[mxoxb-" + "A" * 10 + "-\x1b[mxoxb-" + "B" * 10 + "\x1b[m_x",
    "\x1b[2maaaaaaaa-bbbb-cccc-dddd-abcd\x1b[0m2d783407-cd32-4951-bba5-47fd9b82b8dc",
]


@pytest.mark.parametrize("line", _REACH_UNDERMASK_LINES)
def test_reach_skip_deterministic_undermask_shapes(line):
    # The `reach` skip must never mask less than anchoring at every cut would.
    _never_masks_less(line)


def test_reach_skip_backtracked_boundary_does_not_drop_a_second_token():
    # `closed` for a `-`-bearing class backtracks its trailing `\b` onto an interior `-`, which
    # is NOT a hard stop. Masking only to there and skipping past dropped the second welded
    # `xoxb-` token and streamed its body (#1379 review). The fix masks the whole greedy run.
    out = redact.sanitize_line("Z\x1b[mxoxb-" + "A" * 10 + "-\x1b[mxoxb-" + "B" * 10 + "\x1b[m_x")
    assert "BBBBBBBBBB" not in out


def test_reach_skip_does_not_whole_run_mask_a_fixed_length_uuid():
    # A UUID is fixed-length with interior `-`, so `opened`'s end is the pattern end, not the
    # class-run end: a second UUID that shares eight hex digits starts inside the first and ends
    # past it. Whole-run masking would skip its cut and stream 28 of its 36 characters. The
    # `single_run` flag keeps the UUID mask precise (#1379 review).
    real = "2d783407-cd32-4951-bba5-47fd9b82b8dc"
    out = redact.sanitize_line(f"\x1b[2maaaaaaaa-bbbb-cccc-dddd-abcd\x1b[0m{real}")
    assert "cd32-4951-bba5-47fd9b82b8dc" not in out


@pytest.mark.parametrize("seed", range(400))
def test_reach_skip_never_masks_less_than_anchoring_at_every_cut(seed):
    # The `reach` skip in `_cut_spans` must never mask less than anchoring at every cut. It may
    # mask MORE (masking a whole greedy run over-masks a trailing `-`), so this asserts the
    # coverage is a superset, not equality. This diff'd zero over millions of assembled lines
    # while the fix was written; 400 with `-`/whitespace/uuid shapes guard the class in CI.
    rng = random.Random(seed)  # noqa: S311 — assembling test fixtures, not crypto
    esc = ["\x1b[m", "\x1b[32m", "\x1bPq\x1b\\", "\x1b_x\x07", "\x07", "\x01", "\x1f", "\r\n", ""]
    tok = [
        "session_AAA",
        "cse",
        "env",
        "session",
        "_AAAAAA",
        "_BBBBBB",
        "-",
        "_",
        "_x",
        " ",
        "env_AAAAAA",
        "session_CCCCCC",
        "cse_DDDDDD",
        "sk-ABCDEFGHIJKLMNOP",
        "xoxb-AAAAAAAAAA",
        "glpat-CCCCCCCCCCCCCCCC",
        "bearer DDDDDDDDDDDDDDDDDDDD",
        "clauster_pat_EEEEEEEEEEEEEEEE",
        "2d783407-cd32-4951-bba5-47fd9b82b8dc",
        "AKIAIOSFODNN7EXAMPLE",
        # UUID fragments, so assembly can weld two UUIDs that share hex digits.
        "aaaaaaaa-bbbb-cccc-dddd-",
        "2d783407",
        "abcd",
        "x",
        "",
    ]
    line = "".join(rng.choice(esc) + rng.choice(tok) for _ in range(rng.randint(3, 9)))
    _never_masks_less(line)
