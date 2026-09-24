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
    "row",
    [
        # A glued id is masked ONLY when it has the real id shape (prefix + `01` + eight or
        # more), so a live weld cannot leave a bearer-equivalent id readable. The ordinary
        # names that must STAY readable are covered by
        # test_redact_screen_text_does_not_over_mask_ordinary_text.
        "agentenv_01BX5ZZKBKACTAV9WEVGEMMVRZ",
        "prefixsession_01ABCDEFGH",  # a different glued prefix, real id shape
    ],
)
def test_redact_screen_text_masks_a_glued_real_id(row):
    # The maintainer's tight core for #1433: the glued (no-leading-boundary) id match requires
    # `(env|session|cse)_01` + eight or more, so the screen masks a welded REAL id.
    out = redact.redact_screen_text([row])[0]
    assert "<redacted>" in out and row not in out, out


# --- #1496: a greedy secret core welds onto a UUID and hides its body on the screen surface ---
_UUID_1496 = "12345678-1234-1234-1234-123456789abc"


def test_redact_screen_text_masks_a_uuid_welded_onto_a_greedy_secret():
    # The issue's exact reproduce: `ghp_[A-Za-z0-9]{16,}` is greedy over a class that includes
    # the UUID's leading hex group but not its `-`, so it eats `12345678` and stops, and the
    # anchored `_UUID_RE` (eight leading hex behind a `\b`) never matches. The middle
    # `-1234-1234-1234-123456789abc` used to reach the browser (#1496).
    row = "ghp_" + "B" * 16 + _UUID_1496
    out = redact.redact_screen_text([row])[0]
    assert "-1234-" not in out and "123456789abc" not in out, out
    assert "<redacted>" in out
    # Positive control: the pre-fix anchored screen pass (still the log path's behaviour) leaves
    # the UUID body bare. Reverting the fix makes the assertion above fail on exactly this.
    assert "-1234-1234-1234-123456789abc" in redact.redact_secrets(redact.redact_ids(row))


@pytest.mark.parametrize(
    "prefixed",
    [
        "ghp_" + "B" * 16,  # class [A-Za-z0-9], no `-`: eats the first hex group, leaks the middle
        "ghs_" + "B" * 16,  # the same gh[pousr]_ family
        "github_pat_" + "B" * 20,  # class [A-Za-z0-9_], no `-`: same leak shape
        "sk-" + "B" * 16,  # hyphen-bearing class: swallows the WHOLE UUID (no residue)
        "glpat-" + "B" * 16,
        "xoxb-" + "B" * 16,
        "clauster_pat_" + "B" * 16,
        "bearer " + "B" * 12,  # `\s+` + a `-`/`.`-bearing class: swallows the WHOLE UUID
    ],
)
def test_redact_screen_text_masks_a_uuid_welded_onto_any_greedy_core(prefixed):
    # Every OPEN-ended (greedy) core in `_SECRET_CORES` welded onto a UUID with no separator: the
    # `gh[pousr]_`/`github_pat_` family plus the hyphen-bearing `sk-`/`glpat-`/`xox`/
    # `clauster_pat_` cores and `bearer …`. Whether the core's class excludes `-` (eats only the
    # first hex group, leaking the middle) or includes it (swallows the whole UUID), no fragment
    # of the UUID may survive (#1496). The fixed-count `AKIA[0-9A-Z]{16}` core has its own test,
    # `test_redact_screen_row_masks_a_uuid_welded_right_after_an_akia_key` (#1508).
    out = redact.redact_screen_text([prefixed + _UUID_1496])[0]
    assert "-1234-" not in out and "123456789abc" not in out, out


@pytest.mark.parametrize(
    "prefixed",
    [
        "session_" + "B" * 6,  # `(env|session|cse)_[A-Za-z0-9]{6,}`: class [A-Za-z0-9], no `-`
        "cse_" + "B" * 6,
        "env_" + "B" * 6,
    ],
)
def test_redact_screen_text_masks_a_uuid_welded_onto_a_greedy_id(prefixed):
    # The greedy-core twin folded into #1496: clauster's own id cores are open-ended over a class
    # that includes the UUID's leading hex group but not its `-`, so `session_<...>12345678-...`
    # eats the first hex group and stops, and the anchored `_UUID_RE` never matches. The middle
    # `-1234-1234-1234-123456789abc` used to reach the browser exactly as the secret cores did.
    row = prefixed + _UUID_1496
    out = redact.redact_screen_text([row])[0]
    assert "-1234-" not in out and "123456789abc" not in out, out
    assert "<redacted>" in out
    # Positive control: the pre-fix anchored screen pass (still the log path's behaviour) keeps the
    # `session_`/`cse_`/`env_` prefix and leaves the UUID body bare. Reverting the fold (dropping
    # `_ID_RE` from `_screen_spans`' greedy_spans) makes the assertion above fail on exactly this.
    assert "-1234-1234-1234-123456789abc" in redact.redact_secrets(redact.redact_ids(row))


@pytest.mark.parametrize(
    "glued",
    [
        "env_01" + "B" * 8,  # `_SCREEN_GLUED_ID_RE`: `(env|session|cse)_01[A-Za-z0-9]{8,}`
        "session_01" + "B" * 8,
        "cse_01" + "B" * 8,
    ],
)
def test_redact_screen_text_masks_a_uuid_welded_onto_a_glued_id(glued):
    # The glued-id core folded into #1496: `_SCREEN_GLUED_ID_RE` is a greedy id core (open-ended
    # `[A-Za-z0-9]{8,}`, no leading `\b`) that a consumed escape welds onto the word before it. It
    # includes the UUID's leading hex group but not its `-`, so `agentenv_01<...>12345678-...`
    # eats the first hex group and stops at the `-`. `_ID_RE` never matches (a word char precedes
    # the id, no `\b`) and neither does the anchored `_UUID_RE` (a word char precedes the hex),
    # so the middle `-1234-1234-1234-123456789abc` used to reach the browser.
    row = "agent" + glued + _UUID_1496  # `agent` welds the escape-glued id onto a word
    out = redact.redact_screen_text([row])[0]
    assert "-1234-" not in out and "123456789abc" not in out, out
    assert "<redacted>" in out
    # Positive control: the pre-fix anchored pass (the log path's behaviour) cannot see the glued
    # id at all -- `_ID_RE` needs a leading `\b` -- so the whole id + UUID body stays bare.
    # Reverting the fold (dropping the `_SCREEN_GLUED_ID_RE` matches from `_screen_spans`'
    # greedy_spans) makes the assertion above fail on exactly this UUID body.
    assert "-1234-1234-1234-123456789abc" in redact.redact_secrets(redact.redact_ids(row))


def test_redact_screen_text_masks_a_uuid_welded_onto_a_second_uuid_after_a_secret():
    # A secret welded onto two UUIDs back to back: the first UUID's head the secret ate, and the
    # second UUID welded onto the first. The fixed point must mask both, so neither body leaks.
    row = "ghp_" + "B" * 16 + _UUID_1496 + _UUID_1496
    out = redact.redact_screen_text([row])[0]
    assert "-1234-" not in out and "123456789abc" not in out, out


def test_redact_screen_text_masks_a_uuid_welded_onto_a_plain_word():
    # Was residue for #1496; #1615 closes it. A UUID welded onto an ORDINARY word (no secret, no
    # cut) is masked: the UUID shape is now masked wherever it appears, on both paths. The words
    # that must stay readable are in test_redaction_keeps_ordinary_text_readable_on_both_paths.
    row = "commit" + _UUID_1496  # `commit` is not a secret shape
    assert redact.redact_screen_text([row]) == ["commit<redacted>"]
    assert redact.sanitize_line(row) == "commit<redacted>"
    # Positive control: the anchored pipeline (main's log path for this line) leaves it bare.
    assert redact.redact_secrets(redact.redact_ids(row)) == row


def test_redact_wrapped_rows_masks_a_uuid_a_greedy_secret_ate_across_the_wrap():
    # #1496 on the wrap path. A greedy `ghp_` on the JOINED line eats the UUID's leading hex
    # group; with the head consumed and NO trailing id to anchor on, the middle `-1234-...` used
    # to leak. The join scan now masks the welded UUID through `_screen_spans`, so the wrap path
    # masks no fewer cells than the per-row path. The UUID is split across the wrap so neither
    # row matches it on its own. `tests/test_pty_screen.py` drives the same shape through
    # `frame()` for the pyte + width-refit surface.
    row0 = "ghp_" + "B" * 16 + "12345678-12"
    row1 = "34-1234-1234-123456789abc"
    out = redact.redact_wrapped_screen_rows([row0, row1], hard_seams=[True], soft_seams=[False])
    assert "-1234-" not in "".join(out) and "123456789abc" not in "".join(out)
    assert "<redacted>" in out[0]


def test_redact_wrapped_rows_joins_a_soft_seam_without_its_layout_whitespace():
    # #1508, safety invariant 4. A TUI soft wrap leaves padding after the upper row and a hanging
    # indent before the lower one. The verbatim join keeps that whitespace inside the token, so
    # only the head masked and the tail leaked. A soft seam rebuilds the logical line.
    rows = ["⏺ wrap=sk-" + "a" * 20 + "   ", "  " + "a" * 13 + ":end" + "  "]
    leaked = redact.redact_wrapped_screen_rows(rows, hard_seams=[True], soft_seams=[False])
    assert "aaaa" in leaked[1]  # positive control: the verbatim join leaks the tail
    out = redact.redact_wrapped_screen_rows(rows, hard_seams=[False], soft_seams=[True])
    assert "aaaa" not in "".join(out)
    assert out[1].startswith("  <redacted>:end")  # the indent and the text after it stay


def test_redact_wrapped_rows_soft_seam_masks_a_bearer_wrapped_at_its_space():
    # #1508: the first repro. The TUI moved the whole value to the next row.
    out = redact.redact_wrapped_screen_rows(
        ["weld=bearer    ", "  live0123456789abcdef"], hard_seams=[False], soft_seams=[True]
    )
    assert "0123" not in "".join(out) and "live" not in "".join(out)


def test_redact_wrapped_rows_soft_seam_masks_a_bearer_whose_value_wraps_again():
    # #1508: a `bearer` header broken at its space, whose value is then ALSO broken mid-token.
    # The verbatim join ends the value at the second seam's indent; the plain seam view welds
    # `Bearer` to its value. Only the spaced view (one space after `bearer`, nothing elsewhere)
    # reads the header whole.
    rows = ["Authorization: Bearer   ", "  eyJ" + "A" * 14, "  " + "B" * 10 + " done"]
    out = redact.redact_wrapped_screen_rows(
        rows, hard_seams=[False, False], soft_seams=[True, True]
    )
    assert "BBBB" not in "".join(out) and "AAAA" not in "".join(out)
    assert out[2].endswith(" done")


def test_redact_wrapped_rows_soft_seam_masks_a_token_starting_at_a_word_break():
    # #1508: the upper row ends in a word, the TUI broke at the space, and the token that starts
    # the next row is then broken mid-way. Joined with nothing, `foo` welds onto `sk-` and hides
    # its `\b`, so the mask is retried starting at the seam (as at a removed escape, #1379).
    rows = ["key: foo   ", "  sk-" + "A" * 14, "  " + "A" * 8 + " ok"]
    out = redact.redact_wrapped_screen_rows(
        rows, hard_seams=[False, False], soft_seams=[True, True]
    )
    assert "AAAA" not in "".join(out)
    assert out[0].startswith("key: foo") and out[2].endswith(" ok")


def test_redact_wrapped_rows_soft_seam_masks_a_fixed_token_ending_at_a_seam():
    # #1508: a UUID split by one soft wrap and ending at the next, where the following row starts
    # with a word. Joined with nothing, the word takes the UUID's trailing `\b`, so the mask is
    # retried ending at the seam. The word itself stays readable.
    uuid = "12345678-1234-1234-1234-123456789abc"
    rows = ["id " + uuid[:20] + "  ", "  " + uuid[20:], "  next"]
    out = redact.redact_wrapped_screen_rows(
        rows, hard_seams=[False, False], soft_seams=[True, True]
    )
    assert "123456789abc" not in "".join(out) and "-1234-" not in "".join(out)
    assert out[2] == "  next"


def test_redact_wrapped_rows_rejects_a_seam_count_that_does_not_match():
    # A missing seam flag must not silently read as False; the caller has a bug.
    with pytest.raises(ValueError, match="3 rows need 2 seams, got 2 hard and 1 soft"):
        redact.redact_wrapped_screen_rows(
            ["a", "b", "c"], hard_seams=[True, True], soft_seams=[True]
        )
    with pytest.raises(ValueError, match="3 rows need 2 seams, got 1 hard and 2 soft"):
        redact.redact_wrapped_screen_rows(
            ["a", "b", "c"], hard_seams=[True], soft_seams=[True, True]
        )
    assert redact.redact_wrapped_screen_rows([], hard_seams=[], soft_seams=[]) == []


def test_redact_wrapped_rows_joins_only_the_hard_runs_verbatim():
    # #1508 review, safety invariant 4. The verbatim join must cover only the rows pyte's own
    # wrap connects. Joined across the soft seams too, the `bearer` on row 1 takes row 2's
    # `...ab.bearer` as its value, and the real header on rows 2-3 is never matched.
    rows = [
        "  some text here, x bearer abcdefgh     ",
        "  bearer                                ",
        "  ijklmnopqrstuvwxyzab.bearer live012345",
        "KLMNOPQRSTUV done                       ",
    ]
    out = redact.redact_wrapped_screen_rows(
        rows, hard_seams=[False, False, True], soft_seams=[True, True, False]
    )
    shown = "".join(out)
    assert "KLMN" not in shown and "live0123" not in shown
    assert out[3].rstrip().endswith(" done")


def test_redact_wrapped_rows_join_is_the_hard_run_even_when_seam_views_miss():
    # #1508 review, found by a differential search against a whole-group join. The header
    # `-bearer live0123456789` spans a hard seam, and only the verbatim join of that hard run
    # reads it: joined from row 0, the first `bearer` takes row 1 whole as its value, and both
    # seam views weld or space the rows so that no `bearer` is followed by the value.
    rows = [
        "ab.bearer                     ",
        "Bearerab.bearer-bearer        ",
        "  live0123456789 Bearerbearer ",
        "Bearer                        ",
    ]
    out = redact.redact_wrapped_screen_rows(
        rows, hard_seams=[False, True, True], soft_seams=[True, True, False]
    )
    assert "live0123" not in "".join(out)


def test_redact_wrapped_rows_spaces_a_bearer_the_weld_took_its_boundary_from():
    # #1508 review. `bearer` stands alone on its row, after an indent the screen shows. In the
    # welded view it follows the upper row's `abc`, so a `\b`-anchored test for the space would
    # see `abcbearer` and add none, and the value on the next row would leak. The test takes
    # no `\b`; the seam retry then reads `bearer live...` from the cut.
    rows = ["  some words here abc    ", "  bearer                 ", "  live0123456789abcdef   "]
    out = redact.redact_wrapped_screen_rows(
        rows, hard_seams=[False, False], soft_seams=[True, True]
    )
    assert "live0123" not in "".join(out) and "abcdef" not in "".join(out)
    assert out[0].startswith("  some words here abc")


def test_redact_wrapped_rows_masks_a_uuid_welded_to_a_token_found_at_a_seam():
    # #1508 review. `ghp_` starts a row after a soft seam, so on the welded logical line only the
    # seam retry finds it. It is greedy and eats the UUID's leading hex group, so the UUID has to
    # be looked for after it too (#1496), or its tail leaks.
    token = "ghp" + "_" + "c5263eadf7e0c0e1"
    rows = [
        "  some words here and there :ziqcq",
        "  " + token + "12345678-f7c9",
        "  -1523-d2a2-686b9d96c4fb done",
    ]
    out = redact.redact_wrapped_screen_rows(
        rows, hard_seams=[False, False], soft_seams=[True, True]
    )
    shown = "".join(out)
    assert "686b9d" not in shown and "f7c9" not in shown and "c5263e" not in shown
    assert out[2].rstrip().endswith(" done")


_UUID_1508 = "12345678-f7c9-1523-d2a2-686b9d96c4fb"


@pytest.mark.parametrize(
    ("token", "after"),
    [
        (_UUID_1508, "zz"),  # the review's input: a UUID welded to word characters
        ("AKIAIOSFODNN7EXAMPLE", "zz"),  # fixed-count key, a word char after the 16th
        ("ghp_" + "A" * 16, "_backup"),  # `_` is a word char outside the token's class
        ("session_01ABCDEFGHJK", "_backup"),  # a real `01`-shape id
        ("sk-" + "A" * 16, "é"),  # a non-ASCII letter is a word char too
    ],
)
def test_redact_screen_row_masks_a_token_welded_to_the_word_after_it(token, after):
    # #1508 review, safety invariant 4. With a word character right after it, the token has no
    # trailing `\b`, so the anchored mask never matched it. It masked only when the pty screen's
    # width-refit trim happened to cut the following word away, which depends on how long the
    # rest of the row rendered. The screen surface now masks it without the trailing boundary.
    row = f"see {token}{after} done"
    out = redact.redact_screen_text([row])[0]
    assert token[6:14] not in out, out
    assert out.startswith("see ") and out.endswith(" done")


def test_redact_screen_row_masks_a_uuid_welded_right_after_an_akia_key():
    # #1508: the fixed-count key used to fail its anchored match on a direct weld, so neither
    # it nor the UUID after it reached the welded-UUID check. Both mask now.
    out = redact.redact_screen_text(["key AKIAIOSFODNN7EXAMPLE" + _UUID_1508 + " done"])[0]
    assert "IOSFODNN" not in out and "686b9d" not in out and "-1523-" not in out, out


@pytest.mark.parametrize("word", ["session_timeout_ms", "env_production_db", "cse_worker_pool"])
def test_redact_screen_row_keeps_an_id_look_alike_with_a_trailing_word(word):
    # #1508 guard: the open-tail masks leave the plain id core out on purpose. A name that only
    # looks like an id, followed by `_more`, must stay readable, as it did before.
    assert redact.redact_screen_text([f"set {word} = 3"]) == [f"set {word} = 3"]


def test_redact_screen_row_open_tail_does_not_break_a_welded_id_chain():
    # #1508 guard. A chain of real ids welded end to end unwinds from its last id, one per
    # fixed-point scan. A greedy open-tail match reads `env_01<a>env` up to the next `_`; if it
    # were fed into the fixed point it would erase every other `env` prefix, and those ids
    # would show. It is unioned at render only.
    row = "env_01AAAAAAAA" * 12 + " end"
    out = redact.redact_screen_text([row])[0]
    assert "AAAA" not in out and out.endswith(" end"), out


def test_redact_wrapped_rows_open_tail_covers_every_path():
    # #1508 review: the welded UUID masks in a hard run, and in a row the soft-wrap view adds
    # cells to (the union render, where the width-refit trim may never happen).
    hard = redact.redact_wrapped_screen_rows(
        ["x " + _UUID_1508[:20], _UUID_1508[20:] + "zz ok"], hard_seams=[True], soft_seams=[False]
    )
    assert "686b9d" not in "".join(hard) and "-1523-" not in "".join(hard), hard
    soft = redact.redact_wrapped_screen_rows(
        [_UUID_1508 + "zz x sk-ABCDEFGH   ", "  IJKLMNOPQRST done"],
        hard_seams=[False],
        soft_seams=[True],
    )
    assert "686b9d" not in soft[0] and "ABCDEFGH" not in soft[0], soft
    assert "IJKL" not in soft[1] and soft[1].endswith(" done"), soft


def test_redact_wrapped_rows_seam_view_fails_closed_at_the_scan_cap(monkeypatch):
    # #1508 review. The soft-wrap fixed point is capped, because it runs in the keeper's drain
    # loop. A view that has not settled at the cap must mask MORE, never less: every non-space
    # character of the view. Falling back to the hard-wrap maps would let a crafted chain switch
    # off the soft-wrap catch for the secret beside it.
    rows = ["  key sk-ABCDEFGH   ", "  IJKLMNOPQRST done"]
    uncapped = redact.redact_wrapped_screen_rows(rows, hard_seams=[False], soft_seams=[True])
    monkeypatch.setattr(redact, "_SEAM_MAX_SCANS", 1)  # the first scan adds, so it cannot settle
    capped = redact.redact_wrapped_screen_rows(rows, hard_seams=[False], soft_seams=[True])
    assert "IJKL" not in "".join(capped) and "ABCD" not in "".join(capped)
    words = lambda out: set(" ".join(out).split())  # noqa: E731 -- a one-line helper
    assert words(capped) <= words(uncapped)  # the cap never shows a word the full scan hid
    assert "done" in words(uncapped) and "done" not in words(capped)  # it really failed closed


def test_redact_wrapped_rows_bounds_the_soft_wrap_scans(monkeypatch):
    # #1508 review. The crafted worst case: a 40-row chain of welded ids behind soft seams, one
    # of them after `bearer` so both seam views run. Uncapped, each view needs one scan per id
    # (hundreds, about 500 ms). Capped, each view stops at `_SEAM_MAX_SCANS`, and the whole
    # chain is still masked because the views fail closed.
    body = ("env_01AAAAAAAA" * 400)[: 118 * 39 - (118 * 39) % 14]
    rows = ["  " + "z " * 55 + "bearer"] + [
        "  " + body[i : i + 118] for i in range(0, len(body), 118)
    ]
    rows = [r.ljust(120) for r in rows]
    seams = len(rows) - 1
    calls = []
    real = redact._screen_seam_spans
    monkeypatch.setattr(
        redact, "_screen_seam_spans", lambda text, cuts: calls.append(1) or real(text, cuts)
    )
    out = redact.redact_wrapped_screen_rows(
        rows, hard_seams=[False] * seams, soft_seams=[True] * seams
    )
    assert len(calls) <= 2 * redact._SEAM_MAX_SCANS  # two views, each capped
    assert "AAAA" not in "".join(out)


def _chain_1612(n: int, prefixes: tuple[str, ...] = ("env",)) -> list[str]:
    # Distinct real `01`-shape ids, so a leak is counted per id, not per repeated string.
    return [f"{prefixes[i % len(prefixes)]}_01{i:08d}" for i in range(n)]


def _leaked_1612(ids: list[str], out: list[str]) -> list[str]:
    shown = "".join(out)
    return [i for i in ids if i[-8:] in shown]  # the id's own digits, never in a mask token


def test_redact_wrapped_rows_masks_every_id_in_a_hard_wrapped_welded_chain():
    # #1612, safety invariant 4: the async reviewer's input. A chain of welded ids with no word
    # boundary after its last id never starts the anchored fixed point, and the open-tail scan
    # read `env_01<a>env` up to the next `_`, so it skipped every id whose `env` it had eaten.
    # On main, 237 of these 1000 ids reached the browser. Every one must mask.
    ids = _chain_1612(1000)
    body = "".join(ids) + "_x"
    rows = [body[i : i + 120] for i in range(0, len(body), 120)]
    seams = len(rows) - 1
    out = redact.redact_wrapped_screen_rows(
        rows, hard_seams=[True] * seams, soft_seams=[False] * seams
    )
    assert _leaked_1612(ids, out) == []
    assert len(out) == len(rows)


@pytest.mark.parametrize("prefixes", [("env",), ("session", "cse", "env")])
@pytest.mark.parametrize("after", ["_x", "é", "_", " done"])
def test_redact_screen_row_masks_every_id_in_a_welded_chain(after, prefixes):
    # #1612: the same chain inside one row, with no wrap at all. The per-row pass must not
    # depend on a trailing boundary either (`after` is a word char, `_`, or a real boundary).
    ids = _chain_1612(6, prefixes)
    out = redact.redact_screen_text(["see " + "".join(ids) + after])
    assert _leaked_1612(ids, out) == [], out
    assert out[0].startswith("see ")


def test_redact_wrapped_rows_masks_every_id_in_a_soft_wrapped_welded_chain():
    # #1612: the chain behind soft seams (hanging indent), both short of the scan cap and past it.
    for n in (8, 400):
        ids = _chain_1612(n)
        body = "".join(ids) + "_x"
        rows = ["  " + body[i : i + 110] for i in range(0, len(body), 110)]
        seams = len(rows) - 1
        out = redact.redact_wrapped_screen_rows(
            rows, hard_seams=[False] * seams, soft_seams=[True] * seams
        )
        assert _leaked_1612(ids, out) == [], n


def test_pty_frame_masks_every_id_in_a_welded_chain():
    # #1612 through the real surface: pyte wraps the chain across a full 40 x 120 screen, and the
    # frame is what the WebSocket sends.
    from clauster.pty_screen import PtyScreen

    ids = _chain_1612(330)
    scr = PtyScreen(cols=120, rows=40)
    scr.feed(("".join(ids) + "_x").encode())
    assert _leaked_1612(ids, scr.frame()["rows"]) == []


def test_redact_wrapped_rows_bounds_the_row_and_hard_run_scans(monkeypatch):
    # #1612. The crafted worst case for the hard-wrap maps: a full 40 x 120 hard run of welded
    # ids that ends at a word boundary, so the joined fixed point unwinds it one id per scan
    # (about 340 scans and 250 ms uncapped). Capped, each row and each run stops at
    # `_SCREEN_MAX_SCANS`, and the chain is still fully masked because a capped run fails closed.
    ids = _chain_1612(340)
    body = "".join(ids) + " end"
    rows = [body[i : i + 120] for i in range(0, len(body), 120)]
    assert len(rows) == 40
    calls: list[int] = []
    real = redact._screen_spans
    monkeypatch.setattr(
        redact, "_screen_spans", lambda text: calls.append(len(text)) or real(text)
    )

    def run() -> list[str]:
        calls.clear()
        return redact.redact_wrapped_screen_rows(
            rows, hard_seams=[True] * 39, soft_seams=[False] * 39
        )

    out = run()
    assert len([n for n in calls if n > 120]) <= redact._SCREEN_MAX_SCANS  # the one hard run
    assert len(calls) <= 41 * redact._SCREEN_MAX_SCANS  # 40 rows + 1 run, each capped
    assert _leaked_1612(ids, out) == []
    # Positive control: uncapped, the same input really is the worst case the cap is for.
    monkeypatch.setattr(redact, "_SCREEN_MAX_SCANS", 10**6)
    uncapped = run()
    assert len([n for n in calls if n > 120]) > 300
    assert _leaked_1612(ids, uncapped) == []


def test_redact_wrapped_rows_bounds_the_scans_of_a_wide_row(monkeypatch):
    # #1612. A row on its own is capped too (`row_cov` and the per-row pass). At 120 columns a
    # row holds too few welded ids to reach the cap, so this uses two 700-column rows of 50 ids
    # each, joined by no seam at all: uncapped, each needs one scan per id.
    ids = _chain_1612(100)
    rows = ["".join(ids[:50]) + " end", "".join(ids[50:]) + " end"]
    calls: list[int] = []
    real = redact._screen_spans
    monkeypatch.setattr(redact, "_screen_spans", lambda text: calls.append(1) or real(text))

    def run() -> list[str]:
        calls.clear()
        return redact.redact_wrapped_screen_rows(rows, hard_seams=[False], soft_seams=[False])

    out = run()
    # Per row: `row_cov`, then the per-row pass's rewrite loop and its coverage map.
    assert len(calls) <= 2 * 3 * redact._SCREEN_MAX_SCANS
    assert out == ["<redacted> <redacted>"] * 2  # each row failed closed, `end` included
    monkeypatch.setattr(redact, "_SCREEN_MAX_SCANS", 10**6)  # positive control
    uncapped = run()
    assert len(calls) > 2 * 50
    assert _leaked_1612(ids, uncapped) == [] and all(r.endswith(" end") for r in uncapped)


def test_redact_screen_row_fails_closed_at_the_scan_cap(monkeypatch):
    # #1612. The per-row pass is capped the same way, and must mask MORE at the cap, never less.
    row = "see env_01AAAAAAAAAA and sk-ABCDEFGHIJKLMNOPQR done"
    uncapped = redact.redact_screen_text([row])[0]
    monkeypatch.setattr(redact, "_SCREEN_MAX_SCANS", 1)  # the first scan adds, so it cannot settle
    capped = redact.redact_screen_text([row])[0]
    assert capped == "<redacted> <redacted> <redacted> <redacted> <redacted>"
    assert "done" in uncapped and "AAAA" not in uncapped  # the uncapped pass really settled


def _screen_corpus_1612(seed: int, count: int) -> list[tuple[list[str], list[bool], list[bool]]]:
    # Random wrapped screens built from token shapes, welds and separators. Each is a list of rows
    # with its hard and soft seam flags, in every combination.
    frags = [
        "env_", "session_", "cse_", "01", "AAAAAAAA", "AAAAAA", "sk-", "A" * 15, "AKIA", "B" * 16,
        _UUID_1508, "zz", " ", " ", "  ", "_", "-", "x", "ghp_", "bearer ", "bearer", "é",
        "github_pat_", "glpat-", "xoxb-", "done", "env_01AAAAAAAA", "env_01BBBBBBBB",
    ]  # fmt: skip
    rnd = random.Random(seed)  # noqa: S311 -- assembling test fixtures, not crypto
    corpus = []
    for _ in range(count):
        width = rnd.randint(12, 40)
        text = "".join(rnd.choice(frags) for _ in range(rnd.randint(2, 14)))
        rows = [text[i : i + width] for i in range(0, len(text), width)] or [""]
        rows = [r if not rnd.random() < 0.3 else "  " + r for r in rows]
        seams = len(rows) - 1
        hard = [rnd.random() < 0.6 for _ in range(seams)]
        soft = [rnd.random() < 0.5 for _ in range(seams)]
        corpus.append((rows, hard, soft))
    return corpus


def _pieces_1612(row: str) -> list[str]:
    # What the operator can read in one rendered row: the text between mask tokens and spaces.
    return [p for chunk in row.split() for p in chunk.split(redact._REDACTED) if p]


def _reveals_1612(capped: list[str], uncapped: list[str]) -> int:
    # Rows where the capped render shows a piece of text the uncapped render did not.
    bad = 0
    for got, want in zip(capped, uncapped, strict=True):
        shown = _pieces_1612(want)
        if any(not any(p in q for q in shown) for p in _pieces_1612(got)):
            bad += 1
    return bad


def test_screen_scan_caps_never_reveal_what_the_uncapped_scans_hid(monkeypatch):
    # #1612. Over a random corpus of wrapped screens, forcing every cap low (so it fires on most
    # screens) must never show text the uncapped scans hid: a capped row, run or view fails
    # closed. The positive control swaps the fail-closed map for an empty one (the cap "stops
    # masking" instead) and the same oracle must catch it.
    corpus = _screen_corpus_1612(1612, 3000)

    def render(cap: int) -> list[list[str]]:
        monkeypatch.setattr(redact, "_SCREEN_MAX_SCANS", cap)
        monkeypatch.setattr(redact, "_SEAM_MAX_SCANS", cap)
        return [
            redact.redact_wrapped_screen_rows(rows, hard_seams=hard, soft_seams=soft)
            for rows, hard, soft in corpus
        ]

    uncapped = render(10**6)
    for cap, fired in ((1, 1000), (2, 50)):  # most screens settle by the second scan
        capped = render(cap)
        assert sum(_reveals_1612(c, u) for c, u in zip(capped, uncapped, strict=True)) == 0
        assert sum(c != u for c, u in zip(capped, uncapped, strict=True)) > fired  # it fired
    monkeypatch.setattr(redact, "_fail_closed", lambda text: bytearray(len(text)))
    broken = render(1)
    assert sum(_reveals_1612(c, u) for c, u in zip(broken, uncapped, strict=True)) > 100


def test_redact_screen_row_masks_every_uuid_in_a_welded_chain():
    # #1612, the same shape for UUIDs: the second UUID of a welded chain masked (it starts where
    # the first one's open-tail match ends), but a third one after it showed.
    uuids = [f"12345678-f7c9-1523-d2a2-686b9d96c4{i:02d}" for i in range(4)]
    for after in (" ok", "zz"):
        out = redact.redact_screen_text(["see " + "".join(uuids) + after])[0]
        assert "686b9d" not in out and "f7c9" not in out, out
        assert out.startswith("see ") and out.endswith(after)
    # The same chain, hard-wrapped mid-UUID onto a second row.
    text = "see " + "".join(uuids) + " ok"
    wrapped = redact.redact_wrapped_screen_rows(
        [text[:70], text[70:]], hard_seams=[True], soft_seams=[False]
    )
    assert "686b9d" not in "".join(wrapped) and "f7c9" not in "".join(wrapped), wrapped


def test_bearer_is_the_only_whitespace_core():
    # `_seam_view`'s spaced view keeps a space only after `bearer`, because that is the one core
    # a wrap at a space can split (#1508). A second core that can match whitespace needs the
    # same treatment, so adding one fails here.
    cores = [redact._ID_CORE, redact._UUID_CORE, *(core for core, _ in redact._SECRET_CORES)]
    spacey = [core for core in cores if r"\s" in core or re.search(r"\s", core)]
    assert spacey == [r"bearer\s+[A-Za-z0-9._-]{12,}"]


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


# --- #1615: every secret of a welded chain, and a UUID welded onto the word before it ---------

_KINDS_1615 = [
    "ghp",
    "gho",
    "github_pat",
    "glpat",
    "akia",
    "sk",
    "xoxb",
    "clauster_pat",
    "bearer",
    "Bearer",
]


def _secret_1615(kind: str, i: int) -> str:
    # One real-shaped secret per core. Its `K<index>Z` marker sits in exactly one element of a
    # chain and in no mask token, so a leak is counted per secret, not per repeated string.
    body = f"K{i:05d}" + "Z" * 14  # 20 characters: no open tail needs more
    return {
        "ghp": "ghp_" + body,
        "gho": "gho_" + body,
        "github_pat": "github_pat_" + body,
        "glpat": "glpat-" + body,
        "akia": "AKIA" + body[:16],  # the fixed count
        "sk": "sk-" + body,
        "xoxb": "xoxb-" + body,
        "clauster_pat": "clauster_pat_" + body,
        "bearer": "bearer " + body,
        "Bearer": "Bearer " + body,
    }[kind]


def _leaks_1615(n: int, shown: str) -> list[int]:
    return [i for i in range(n) if f"K{i:05d}Z" in shown]


def _paths_1615(line: str) -> dict[str, str]:
    return {
        "sanitize_line": redact.sanitize_line(line),
        "redact_for_disk": redact.redact_for_disk(line),
        "screen": redact.redact_screen_text([line])[0],
    }


@pytest.mark.parametrize("after", [" done", "", "_x", "é"])
@pytest.mark.parametrize("n", [2, 3, 40])
@pytest.mark.parametrize("kind", _KINDS_1615)
def test_every_secret_of_a_welded_same_kind_chain_masks_on_both_paths(kind, n, after):
    # #1615, safety invariant 4. A secret welded onto another of its kind: `ghp_<a>ghp_<b>` reads
    # as `ghp_<a>ghp` up to the next `_`, `AKIA<a>AKIA<b>` as two fixed-length keys end to end,
    # `bearer <a>bearer <b>` as `bearer <a>bearer` up to the space. A plain scan resumed after
    # the first match and never saw the second. Every secret must mask, on the log path and on
    # the screen, whatever follows the chain (a boundary, nothing, a word char, a non-ASCII one).
    line = "tok " + "".join(_secret_1615(kind, i) for i in range(n)) + after
    for path, out in _paths_1615(line).items():
        assert _leaks_1615(n, out) == [], (path, out)
        assert out.startswith("tok "), (path, out)
        assert after != " done" or out.endswith(" done"), (path, out)


@pytest.mark.parametrize("kind", ["ghp", "gho", "akia", "bearer", "Bearer"])
def test_a_welded_chain_leaks_through_the_anchored_pipeline(kind):
    # Positive control for the test above. Main's log path for a line with no escape is exactly
    # this sequential anchored pipeline, and it shows the second secret (#1615). The other kinds
    # carry their own prefix inside their class, so a chain of them is one match already.
    line = "tok " + _secret_1615(kind, 0) + _secret_1615(kind, 1) + " done"
    assert "K00001Z" in redact.redact_secrets(redact.redact_ids(line))


@pytest.mark.parametrize("n", [2, 3, 40])
@pytest.mark.parametrize("first", range(len(_KINDS_1615)))
def test_every_secret_of_a_welded_mixed_chain_masks_on_both_paths(first, n):
    # #1615: the same weld across kinds, in every rotation. A key after a token, a token after a
    # key, a `bearer` header after a GitHub token and so on.
    kinds = [_KINDS_1615[(first + i) % len(_KINDS_1615)] for i in range(n)]
    line = "tok " + "".join(_secret_1615(kind, i) for i, kind in enumerate(kinds)) + " done"
    for path, out in _paths_1615(line).items():
        assert _leaks_1615(n, out) == [], (path, kinds, out)
        assert out.startswith("tok ") and out.endswith(" done"), (path, out)


@pytest.mark.parametrize("kind", _KINDS_1615)
def test_a_secret_welded_onto_a_masked_uuid_masks_on_both_paths(kind):
    # A masked token of another shape is a place a mask may start too: `<UUID>ghp_<a>`.
    line = f"id {_UUID_1508}{_secret_1615(kind, 0)}{_secret_1615(kind, 1)} done"
    for path, out in _paths_1615(line).items():
        assert _leaks_1615(2, out) == [] and "686b9d" not in out, (path, out)


@pytest.mark.parametrize("kind", _KINDS_1615)
def test_a_welded_chain_masks_across_escapes_on_the_log_path(kind):
    # The log path's own weld signal: an escape between the secrets, and around the chain. With
    # colour kept, the colored line shows the chain, so the line falls back to the masked form.
    chain = "\x1b[1m".join(_secret_1615(kind, i) for i in range(3))
    line = f"tok\x1b[31m{chain}\x1b[0m_x done"
    for out in (redact.sanitize_line(line), redact.sanitize_line(line, strip_ansi_seq=False)):
        assert _leaks_1615(3, out) == [] and "\x1b" not in out, out


@pytest.mark.parametrize("kind", _KINDS_1615)
def test_a_welded_chain_masks_at_a_row_end_and_across_hard_and_soft_wraps(kind):
    # #1615 on the screen surface: the chain at the very end of a row, hard-wrapped by pyte at
    # several widths (so the seams fall inside and between secrets), and soft-wrapped by the TUI
    # with a hanging indent and trailing padding.
    n = 12
    chain = "".join(_secret_1615(kind, i) for i in range(n))
    assert _leaks_1615(n, redact.redact_screen_text(["see " + chain])[0]) == []
    text = "see " + chain + "_x"
    for width in (19, 20, 37, 120):
        rows = [text[i : i + width] for i in range(0, len(text), width)]
        seams = len(rows) - 1
        hard = redact.redact_wrapped_screen_rows(
            rows, hard_seams=[True] * seams, soft_seams=[False] * seams
        )
        assert _leaks_1615(n, "".join(hard)) == [], (width, hard)
        soft_rows = ["  " + row + "   " for row in rows]
        soft = redact.redact_wrapped_screen_rows(
            soft_rows, hard_seams=[False] * seams, soft_seams=[True] * seams
        )
        assert _leaks_1615(n, "".join(soft)) == [], (width, soft)
        assert len(hard) == len(soft) == len(rows)


def _tui_frame_1615(lead: str, token: str, cols: int) -> list[str]:
    # A word-wrapping TUI moves a long token onto rows of its own, each after a hanging indent.
    # The soft-wrap view joins those rows with nothing between them, so the token is welded onto
    # the last word of the row above.
    from clauster.pty_screen import PtyScreen

    rows = [lead] + ["  " + token[i : i + cols - 2] for i in range(0, len(token), cols - 2)]
    scr = PtyScreen(cols=cols, rows=len(rows) + 2)
    scr.feed("\r\n".join(rows).encode())
    return scr.frame()["rows"]


@pytest.mark.parametrize("cols", [40, 120])
@pytest.mark.parametrize("kind", _KINDS_1615)
def test_pty_frame_masks_a_welded_chain_the_tui_moved_onto_its_own_rows(kind, cols):
    # #1615 review, safety invariant 4. The chain starts at a soft-wrap seam, not at a word
    # boundary, so the open-tail pass must take the seam as a place a mask may start. Before,
    # everything past the second seam showed.
    n = 8
    chain = "".join(_secret_1615(kind, i) for i in range(n))
    out = _tui_frame_1615(
        "  Here is the list of tokens that were printed by the tool"[:cols], chain, cols
    )
    assert _leaks_1615(n, "".join(out)) == [], out
    assert out[0].startswith("  Here is the list")


@pytest.mark.parametrize(
    "line",
    [
        "see env_01ABCDEFGHsk-QWERTYUIOPASDFGH done",  # an open-tail id seeds the screen
        "see env_ABCDEFsk-QWERTYUIOPASDFGH done",  # an anchored id seeds both paths
        "see session_abcdefglpat-QWERTYUIOPASDFGH done",
        "see env_abcdebearer tokenQWERTYUIOPASDF done",
    ],
)
def test_a_secret_that_starts_inside_a_masked_id_masks_on_both_paths(line):
    # #1615 review: an id is a masked token too, so a secret that starts inside it and runs past
    # its end is masked on both paths. Dropping the id seeds leaves `QWERTYUIOP` showing.
    for path, out in _paths_1615(line).items():
        assert "QWERTYUIOP" not in out and out.endswith(" done"), (path, out)


def test_a_secret_inside_an_id_found_at_an_escape_masks_on_the_log_path():
    # The same seed on the log path's escape branch: the id starts at a cut, not a boundary, so
    # only its cut-anchored span can seed the secret that starts inside it.
    line = "see\x1b[menv_ABCDEFsk-QWERTYUIOPASDFGH done"
    for out in (redact.sanitize_line(line), redact.redact_for_disk(line)):
        assert "QWERTYUIOP" not in out and out.endswith(" done"), out


def test_pty_frame_masks_every_secret_of_a_welded_mixed_chain():
    # #1615 through the real surface: pyte wraps a long mixed chain across a 40 x 120 screen, and
    # the frame is what the WebSocket sends.
    from clauster.pty_screen import PtyScreen

    n = 150
    chain = "".join(_secret_1615(_KINDS_1615[i % len(_KINDS_1615)], i) for i in range(n))
    scr = PtyScreen(cols=120, rows=40)
    scr.feed(("see " + chain + "_x").encode())
    assert _leaks_1615(n, "".join(scr.frame()["rows"])) == []


@pytest.mark.parametrize("after", [" done", "zz", ""])
@pytest.mark.parametrize("word", ["run_", "agent", "id=x", "commit", "0x"])
@pytest.mark.parametrize("n", [1, 2, 3])
def test_a_uuid_welded_onto_the_word_before_it_masks_on_both_paths(word, n, after):
    # #1615: `run_<UUID>` (and a chain of UUIDs after the word) showed on both paths, because the
    # UUID mask needed a word boundary before its first hex digit. A UUID is now masked wherever
    # it appears.
    uuids = [f"12345678-f7c9-1523-d2a2-686b9d96c4{i:02d}" for i in range(n)]
    line = "id " + word + "".join(uuids) + after
    for path, out in _paths_1615(line).items():
        assert "686b9d" not in out and "f7c9" not in out, (path, out)
        assert out.startswith("id " + word) and out.endswith(after), (path, out)
    # Positive control: the anchored pipeline (main's log path for this line) shows the UUID.
    assert "686b9d" in redact.redact_secrets(redact.redact_ids(line))


def test_a_bearer_header_inside_the_value_of_the_one_before_masks_on_both_paths():
    # `.` is in the bearer value class, so the first header's value runs on through the second
    # `bearer` and stops at its space. The second header starts at a word boundary, but inside
    # the first match, so a plain scan never tried it and its value showed (#1615).
    line = "h bearer " + "A" * 12 + ".bearer " + "B" * 12 + " done"
    for path, out in _paths_1615(line).items():
        assert "BBBB" not in out and out.endswith(" done"), (path, out)
    assert "B" * 12 in redact.redact_secrets(redact.redact_ids(line))  # the leak on main


def test_a_key_that_starts_inside_the_key_before_it_masks_on_both_paths():
    # `AKIAKIA...`: the second key's `AKIA` starts inside the first key's own prefix, so only a
    # walk that resumes one character after each START finds it. Its last three characters sit
    # past the first key's fixed end.
    line = "key AKIAKIA" + "B" * 13 + "XYZ done"
    for path, out in _paths_1615(line).items():
        assert "XYZ" not in out and "BBB" not in out, (path, out)
        assert out.startswith("key ") and out.endswith(" done"), (path, out)


@pytest.mark.parametrize("kind", _KINDS_1615)
def test_a_secret_at_an_escape_with_no_end_boundary_masks_on_the_log_path(kind):
    # The log path's cut is a place a mask may start (#1379), and the secret needs no trailing
    # boundary (#1615): here a word character follows it and no escape ends it, so no other
    # span source can end the mask.
    line = f"x\x1b[m{_secret_1615(kind, 0)}é done"
    for out in (redact.sanitize_line(line), redact.redact_for_disk(line)):
        assert _leaks_1615(1, out) == [] and out.endswith("é done"), out


def test_two_uuids_that_share_hex_digits_both_mask_on_both_paths():
    # A second UUID that starts inside the first one's last group. The welded-UUID helper names
    # this gap for its own non-overlapping scan; the every-start UUID scan closes it on both paths.
    line = "x aaaaaaaa-bbbb-cccc-dddd-abcd2d783407-cd32-4951-bba5-47fd9b82b8dc y"
    for path, out in _paths_1615(line).items():
        assert "cd32" not in out and "47fd9b" not in out, (path, out)
        assert out.startswith("x ") and out.endswith(" y"), (path, out)


@pytest.mark.parametrize(
    "text",
    [
        "set session_timeout_ms = 3",
        "resolve_session_transcript ran",
        "env_production_db and cse_worker_pool",
        "risk-assessment-checklist",
        "feat/task-queue-retry-backoff",
        "the bearer of bad news",
        "bearer_token_name = 'x'",
        "run_12345678 finished",
        "build-2024-01-01-1234 ok",
        "sha256:" + "ab" * 32,
        "v1.2.3-4-gdeadbee",
        "trace 12345678-1234-1234-1234 no last group",
        "tok ghp_tooshort here",
        "AKIA123 is too short",
    ],
)
def test_redaction_keeps_ordinary_text_readable_on_both_paths(text):
    # #1615 readability controls: masking a UUID wherever it appears, and a secret that starts
    # inside a masked token, must not touch ordinary identifiers, hyphenated words, hashes or
    # near-miss shapes.
    for path, out in _paths_1615(text).items():
        assert out == text, (path, out)


def test_a_secret_welded_onto_an_ordinary_word_stays_documented_residue():
    # #1615 keeps this residue on both paths: nothing marks where `agent` ends and the token
    # starts, and unanchoring the secret cores would mask inside ordinary hyphenated words.
    line = "agentghp_" + "A" * 20 + " done"
    for path, out in _paths_1615(line).items():
        assert out == line, path


def test_secret_candidates_are_every_start_of_every_core():
    # The every-start walk, against brute force: each core matched at each position. The
    # positive control is a plain `finditer`, which resumes after each match and must miss
    # overlapping starts on this corpus.
    rnd = random.Random(1615)  # noqa: S311 -- assembling test fixtures, not crypto
    frags = [
        "ghp_", "gho_", "github_pat_", "glpat-", "AKIA", "AKI", "sk-", "xoxb-", "xox",
        "clauster_pat_", "bearer ", "Bearer\t", "bearer", "A" * 8, "Z" * 5, "0123", "_", "-",
        ".", " ", "é",
    ]  # fmt: skip
    cores = [re.compile(core, flags) for core, flags in redact._SECRET_CORES]
    missed = 0
    for _ in range(3000):
        text = "".join(rnd.choice(frags) for _ in range(rnd.randint(1, 14)))
        want = sorted(
            hit.span()
            for rx in cores
            for q in range(len(text))
            if (hit := rx.match(text, q)) is not None
        )
        assert list(redact._secret_candidates(text)) == want, text
        missed += sorted(m.span() for rx in cores for m in rx.finditer(text)) != want
    assert missed > 100


def test_a_run_of_self_chaining_secrets_is_read_once(monkeypatch):
    # `("sk-" + "A" * 16) * n` has n starts in one class run. The first reads the run; the rest
    # take its end instead of reading it again, which keeps the walk linear. Positive control:
    # with the reuse switched off, the same run is read n times.
    calls: list[int] = []

    class Counting:
        def __init__(self, rx: re.Pattern[str]) -> None:
            self.rx = rx

        def match(self, text: str, pos: int) -> re.Match[str] | None:
            calls.append(pos)
            return self.rx.match(text, pos)

    real = redact._SECRET_STARTS
    text = ("sk-" + "A" * 16) * 2000
    monkeypatch.setattr(
        redact, "_SECRET_STARTS", tuple(s._replace(opened=Counting(s.opened)) for s in real)
    )
    found = list(redact._secret_candidates(text))
    assert len(found) == 2000 and len(calls) == 1
    calls.clear()
    monkeypatch.setattr(
        redact,
        "_SECRET_STARTS",
        tuple(s._replace(opened=Counting(s.opened), single_run=False) for s in real),
    )
    assert list(redact._secret_candidates(text)) == found and len(calls) == 2000


def test_open_spans_come_back_merged():
    # Every start of a self-chaining run is its own span to the end of the run. Handed on
    # unmerged, each coverage test would read the whole run again.
    text = ("sk-" + "A" * 16) * 2000
    assert redact._open_spans(text, (), []) == [(0, len(text))]
    assert redact._merged([(5, 9), (0, 3), (3, 4), (6, 7)]) == [(0, 4), (5, 9)]


def test_open_spans_hold_no_list_of_every_start():
    # `"AKIA" * n` has a candidate every four characters. They stream in order and the kept ones
    # merge as they come, so memory does not grow with the number of starts. A list per start
    # held about 55 MB for each MB of such input (#1615 review).
    import tracemalloc

    text = "AKIA" * 50_000
    tracemalloc.start()
    try:
        spans = redact._open_spans(text, (), [])
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert spans == [(0, len(text))]
    assert peak < 1_000_000, peak


@pytest.mark.parametrize(
    "unit",
    [
        *(_secret_1615(kind, 7) for kind in _KINDS_1615),
        _UUID_1508,
        "aaaaaaaa-bbbb-cccc-dddd-",
        "\x01sk-" + "A" * 16,
        "\x01ghp_" + "A" * 16,
        "".join(_secret_1615(kind, 3) for kind in _KINDS_1615),
    ],
)
def test_a_long_welded_chain_stays_linear(unit):
    # #1615. `redact_for_disk` is handed a whole bridge log (10 MB by default), so a chain of
    # welded secrets or UUIDs must cost linear time. About 0.15 s for this N; the bound catches
    # a quadratic walk without tripping the suite-wide timeout.
    hostile = unit * (400_000 // len(unit))
    start = time.monotonic()
    redact.sanitize_line(hostile)
    redact.redact_screen_text([hostile[:4800]])
    assert time.monotonic() - start < 5.0


def test_secret_start_rejects_a_core_without_a_counted_class_tail():
    # A future core the every-start walk cannot split fails at import, not silently.
    with pytest.raises(ValueError, match="counted character class"):
        redact._secret_start(r"tok_\w+", 0, re.compile(r"tok_\w+"))


@pytest.mark.parametrize(
    "line",
    [
        "[bridge:init] bridgeId=2d783407-cd32-4951-bba5-47fd9b82b8dc machine=claude-code",
        f"a {_UUID_1508} b {_UUID_1508}",
        f"run_{_UUID_1508}",
        f"{_UUID_1508}zz",
        f"x aaaaaaaa-bbbb-cccc-dddd-abcd{_UUID_1508}",
        f"tok {_secret_1615('ghp', 0)} {_UUID_1508}",
        f"tok {_secret_1615('ghp', 0)}{_UUID_1508}",
        f"{_secret_1615('bearer', 0)}.bearer {'B' * 12} done",
        "plain words only",
    ],
)
def test_the_fast_path_check_agrees_with_the_anchored_union(line):
    # `_fast_path_misses` skips the second run of the anchored masks for a line with no secret
    # shape: a UUID with a `\b` on both sides is exactly an anchored UUID match. It must answer
    # as the full comparison against every anchored mask does, and the spans it hands on must be
    # the ones the union path would find.
    spans, full = _fast_path_reference(line)
    assert redact._fast_path_misses(line) == (spans if full else None)
    if not full:  # the old path, byte for byte
        assert redact.sanitize_line(line) == redact.redact_secrets(redact.redact_ids(line))


def _fast_path_reference(line: str) -> tuple[list[tuple[int, int]], bool]:
    # The full comparison `_fast_path_misses` shortcuts: the open spans seeded with every anchored
    # match, and whether the line must leave the sequential path. It must when an open span masks
    # a character outside the anchored matches, or when two anchored matches overlap (#1617).
    anchored = sorted(m.span() for mask in redact._MASKS for m in mask[0].finditer(line))
    covered = bytearray(len(line))
    for s, e in anchored:
        covered[s:e] = b"\x01" * (e - s)
    spans = redact._open_spans(line, (), anchored)
    overlap = any(anchored[k + 1][0] < anchored[k][1] for k in range(len(anchored) - 1))
    return spans, overlap or any(covered.find(0, s, e) >= 0 for s, e in spans)


def test_the_fast_path_check_agrees_with_the_anchored_union_over_a_corpus():
    # The same agreement over random escape-free lines built from ids, UUIDs, UUID fragments,
    # secrets and separators. Both answers must occur, or the corpus proves nothing.
    rnd = random.Random(16150)  # noqa: S311 -- assembling test fixtures, not crypto
    frags = [
        _UUID_1508, _UUID_1508[:20], _UUID_1508[20:], "aaaaaaaa-bbbb-cccc-dddd-", "abcd", "run_",
        "env_01AAAAAAAA", "session_", "ghp_", "AKIA", "B" * 16, "bearer ", "sk-", "-", "_",
        " ", ".", "zz", "é", "done",
    ]  # fmt: skip
    seen = set()
    for _ in range(3000):
        line = "".join(rnd.choice(frags) for _ in range(rnd.randint(1, 10)))
        spans, full = _fast_path_reference(line)
        assert redact._fast_path_misses(line) == (spans if full else None), line
        seen.add(full)
    assert seen == {True, False}


# --- #1617: an id welded after a masked token, and a bearer value the fast path cut short -------

_ID_SHAPES_1617 = ["session_01", "env_01", "cse_01", "env_", "session_"]


def _id_1617(shape: str, i: int) -> str:
    # One id per shape, with the `K<index>Z` marker of `_leaks_1615` inside its value. The
    # `01` shapes are real ids; `env_`/`session_` alone are the plain id core.
    return f"{shape}K{i:05d}ZABCD"


_BEFORE_1617 = [*(_secret_1615(kind, 0) for kind in _KINDS_1615), _UUID_1508]


@pytest.mark.parametrize("after", [" done", "", "_x", "é"])
@pytest.mark.parametrize("shape", _ID_SHAPES_1617)
@pytest.mark.parametrize("before", _BEFORE_1617)
def test_an_id_welded_after_a_masked_token_masks_on_both_paths(before, shape, after):
    # #1617, safety invariant 4. `key AKIA<16>session_01<...>`: the key masks, and the id that
    # starts right at its end (or inside it, for a greedy core that reads over `session`) showed
    # on the log path. Every secret kind and a UUID before the id; every id shape; a boundary,
    # nothing, a word char or a non-ASCII char after it.
    line = f"key {before}{_id_1617(shape, 1)}{after}"
    for path, out in _paths_1615(line).items():
        assert _leaks_1615(2, out) == [] and "686b9d" not in out, (path, out)
        assert out.startswith("key "), (path, out)
        assert after != " done" or out.endswith(" done"), (path, out)


@pytest.mark.parametrize("before", [_secret_1615("akia", 0), _secret_1615("ghp", 0), _UUID_1508])
def test_a_chain_of_ids_welded_after_a_masked_token_masks_on_both_paths(before):
    # A kept id is a masked token too, so each id of a chain after the first one masks.
    ids = "".join(_id_1617(_ID_SHAPES_1617[i % 5], i + 1) for i in range(8))
    line = f"key {before}{ids} done"
    for path, out in _paths_1615(line).items():
        assert _leaks_1615(9, out) == [] and "686b9d" not in out, (path, out)
        assert out.startswith("key ") and out.endswith(" done"), (path, out)


@pytest.mark.parametrize("before", [_secret_1615("akia", 0), _secret_1615("ghp", 0), _UUID_1508])
def test_a_welded_id_leaks_on_the_log_path_without_the_id_scan(monkeypatch, before):
    # Positive control for the two tests above: with the welded-id scan switched off (main has
    # none), the log path shows the id after the masked token.
    line = f"key {before}{_id_1617('session_01', 1)} done"
    never = redact._ID_START._replace(prefix=re.compile(r"(?!)"))
    monkeypatch.setattr(redact, "_ID_START", never)
    assert _leaks_1615(2, redact.sanitize_line(line)) == [1]


def test_pty_frame_masks_an_id_welded_after_a_masked_token():
    # The same weld through the real screen surface. The `01` shape was already masked there;
    # a plain id core welded onto a masked key showed.
    from clauster.pty_screen import PtyScreen

    scr = PtyScreen(cols=120, rows=4)
    scr.feed(f"key {_secret_1615('akia', 0)}{_id_1617('env_', 1)} done".encode())
    rows = scr.frame()["rows"]
    assert _leaks_1615(2, "".join(rows)) == [] and rows[0].startswith("key ")


_INNER_1617 = [
    _UUID_1508,
    _secret_1615("ghp", 0),
    _secret_1615("akia", 0),
    _secret_1615("sk", 0),
    _secret_1615("glpat", 0),
    _secret_1615("xoxb", 0),
    "env_ABCDEFGH",
    _id_1617("session_01", 0),
]
_TAILS_1617 = [
    ".s3cr3tV4lu3XyZq9",
    "-s3cr3tV4lu3XyZq9",
    ".s3cr3t.V4lu3.XyZq9",
    "..s3cr3t-V4lu3.XyZq9",
]


@pytest.mark.parametrize("lead", ["", "pre0."])
@pytest.mark.parametrize("tail", _TAILS_1617)
@pytest.mark.parametrize("inner", _INNER_1617)
@pytest.mark.parametrize("hdr", ["Bearer", "bearer", "BEARER"])
def test_the_whole_bearer_value_masks_when_it_holds_another_token(hdr, inner, tail, lead):
    # #1617, safety invariant 4. The bearer value class holds `.` and `-`, so the whole of
    # `Bearer <UUID>.<tail>` is one anchored bearer match. The sequential fast path masked the
    # UUID first, the bearer mask then found no value, and `.<tail>` showed. Every token shape
    # inside the value, tails with one or several dots and hyphens, and value text before it.
    line = f"Authorization: {hdr} {lead}{inner}{tail} done"
    for path, out in _paths_1615(line).items():
        assert "s3cr3t" not in out and "V4lu3" not in out and "pre0" not in out, (path, out)
        assert _leaks_1615(1, out) == [] and "686b9d" not in out, (path, out)
        assert out.startswith("Authorization: ") and out.endswith(" done"), (path, out)


@pytest.mark.parametrize(
    ("inner", "tail"),
    [
        (_UUID_1508, ".s3cr3tV4lu3XyZq9"),
        (_UUID_1508, "-s3cr3tV4lu3XyZq9"),
        (_secret_1615("ghp", 0), ".s3cr3tV4lu3XyZq9"),
        (_secret_1615("akia", 0), ".s3cr3t.V4lu3.XyZq9"),
        ("env_ABCDEFGH", "..s3cr3t-V4lu3.XyZq9"),
    ],
)
def test_the_bearer_tail_leaks_through_the_sequential_pipeline(inner, tail):
    # Positive control for the test above: main's log path for these escape-free lines is this
    # sequential pipeline (no open span leaves the anchored matches), and it shows the tail.
    line = f"Authorization: Bearer {inner}{tail} done"
    assert "V4lu3" in redact.redact_secrets(redact.redact_ids(line))
    assert redact._fast_path_misses(line) is not None


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("cfg.session_timeout_ms = 3", None),
        ("os.path.join(a, b).strip().session_timeout_ms", None),
        ("v1.2.3.4 and a.b.c.d and x.y-z.w", None),
        ("the bearer of bad news. session_timeout_ms stays", None),
        (f"id {_UUID_1508} session_timeout_ms", "id <redacted> session_timeout_ms"),
        (f"id {_UUID_1508}.session_timeout_ms", "id <redacted>.session_timeout_ms"),
        (f"tok {_secret_1615('ghp', 0)} session_timeout_ms", "tok <redacted> session_timeout_ms"),
        (f"key {_secret_1615('akia', 0)}.cse_worker_pool", "key <redacted>.cse_worker_pool"),
    ],
)
def test_ordinary_names_near_a_masked_token_stay_readable_on_both_paths(text, want):
    # #1617 readability controls. Only an id that starts inside a masked token, or right at its
    # end, is masked. One after a separator, and ordinary dotted names, stay readable.
    for path, out in _paths_1615(text).items():
        assert out == (text if want is None else want), (path, out)


def _union_render(line: str) -> str:
    # `sanitize_line` forced onto the union path, whatever the fast-path gate says.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(redact, "_fast_path_misses", lambda text: _fast_path_reference(text)[0])
        return redact.sanitize_line(line)


def test_the_fast_path_never_shows_what_the_union_path_hides():
    # #1617. Over random escape-free lines, a line the gate keeps on the sequential path must not
    # show a piece of text the union path hides. Positive control: on the lines the gate sends to
    # the union path for an overlap, the sequential path does show such a piece, so the oracle
    # can see the leak the gate exists to stop.
    rnd = random.Random(1617)  # noqa: S311 -- assembling test fixtures, not crypto
    frags = [
        _UUID_1508, "env_01AAAAAAAA", "env_ABCDEF", "session_", "ghp_", "AKIA", "B" * 16,
        "Bearer ", "bearer ", "sk-", "glpat-", "xoxb-", ".", "-", "_", " ", "zz", "s3cr3t",
    ]  # fmt: skip
    kept = caught = 0
    for _ in range(4000):
        line = "".join(rnd.choice(frags) for _ in range(rnd.randint(1, 10)))
        sequential = redact.redact_secrets(redact.redact_ids(line))
        union = _union_render(line)
        if redact._fast_path_misses(line) is None:
            kept += 1
            assert redact.sanitize_line(line) == sequential, line
            assert _reveals_1612([sequential], [union]) == 0, (line, sequential, union)
        else:
            caught += _reveals_1612([sequential], [union])
    assert kept > 1000 and caught > 50, (kept, caught)
