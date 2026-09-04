"""Pure server-side terminal emulation for the read-only live pty-screen view (#534)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
from pathlib import Path

import pytest

from clauster import pty_screen
from clauster.pty_screen import PtyScreen, PyteUnavailableError, extract_osc8_hyperlinks

# Mirrors `redact._ID_RE`, restated rather than imported: a test that asks the redactor
# whether it redacted cannot fail. Same trade the fuzz harnesses make.
_BARE_ID_RE = re.compile(r"\b(env|session|cse)_[A-Za-z0-9]{6,}\b")


def test_renders_plaintext_cells_and_cursor():
    scr = PtyScreen(cols=20, rows=3)
    scr.feed(b"hello \x1b[31mworld\x1b[0m")  # SGR color escapes are consumed by pyte
    frame = scr.frame()
    assert frame["cols"] == 20 and frame["rows_count"] == 3
    assert len(frame["rows"]) == 3
    assert frame["rows"][0].startswith("hello world")
    assert "\x1b" not in frame["rows"][0]  # cells only, never raw ANSI
    assert frame["cursor"] == {"x": 11, "y": 0}


def test_frame_redacts_secrets_and_ids():
    scr = PtyScreen(cols=60, rows=2)
    scr.feed(b"export TOK=sk-abcdef0123456789 session_01ABCDEFGHIJKLMNOP")
    joined = "".join(scr.frame()["rows"])
    assert "sk-abcdef0123456789" not in joined
    assert "session_01ABCDEFGHIJKLMNOP" not in joined
    assert "<redacted>" in joined


def test_find_session_id_recovers_cursor_fragmented_url():
    # #665: at the TUI winsize claude prints its connect URL with cursor-positioning escapes,
    # so the raw byte stream is NOT contiguous — and the literal "code" is overwritten in
    # place, so a plain ANSI-strip can't recover it either. Only a real emulator that honors
    # the cursor move reconstructs the logical line. This mirrors the captured failure: write
    # "...cod" + a wrong char, reposition the cursor back over it, then overwrite with "e/...".
    scr = PtyScreen(cols=120, rows=4)
    scr.feed(b"https://claude.ai/codX\x1b[22Ge/session_01TESTpty665ABC")
    # The raw bytes the keeper's _RE_CONNECT_URL would scan: the escape splits "code/session_"
    # AND ANSI-stripping leaves "codXe/session_" — both miss. pyte renders the line whole.
    assert scr.find_session_id() == "session_01TESTpty665ABC"


def test_find_session_id_reads_unredacted_screen():
    # find_session_id is server-side only — it must return the REAL id so the keeper can build
    # the connect URL, even though frame() (what reaches the browser) redacts that same id.
    scr = PtyScreen(cols=80, rows=2)
    scr.feed(b"  https://claude.ai/code/session_01ABCDEFGHIJKLMNOP")
    assert scr.find_session_id() == "session_01ABCDEFGHIJKLMNOP"
    assert "session_01ABCDEFGHIJKLMNOP" not in "".join(scr.frame()["rows"])  # frame stays redacted


def test_find_session_id_none_when_absent():
    scr = PtyScreen(cols=40, rows=2)
    scr.feed(b"just some terminal output, no connect url here")
    assert scr.find_session_id() is None


# --- find_authorize_url() / find_oauth_token() (#846: setup-token PTY reader) --------


def test_find_authorize_url_scrapes_the_rendered_screen():
    # `claude setup-token` is a full TUI (#846): the reassembled screen (not necessarily
    # the raw byte stream) is what carries the whole URL line.
    scr = PtyScreen(cols=120, rows=4)
    scr.feed(b"Open this URL to authorize: https://claude.com/cai/oauth/authorize?code=1")
    assert scr.find_authorize_url() == "https://claude.com/cai/oauth/authorize?code=1"


def test_find_authorize_url_none_when_absent():
    scr = PtyScreen(cols=60, rows=2)
    scr.feed(b"Checking credentials...")
    assert scr.find_authorize_url() is None


def test_find_authorize_url_prefers_known_host_over_a_decoy():
    # Shares the exact selection rule login_shepherd's plain-pipe reader uses (last
    # known-auth-host match wins over an earlier docs/decoy link).
    scr = PtyScreen(cols=120, rows=4)
    scr.feed(
        b"See the docs at https://docs.example.com/help\r\n"
        b"Open this URL to authorize: https://claude.ai/oauth/authorize?fake=1"
    )
    assert scr.find_authorize_url() == "https://claude.ai/oauth/authorize?fake=1"


def test_find_authorize_url_is_incremental_across_feeds():
    scr = PtyScreen(cols=120, rows=4)
    scr.feed(b"Open this URL to authorize: ")
    assert scr.find_authorize_url() is None
    scr.feed(b"https://claude.com/cai/oauth/authorize?code=2")
    assert scr.find_authorize_url() == "https://claude.com/cai/oauth/authorize?code=2"


def test_concurrent_feed_and_scan_does_not_corrupt_the_screen():
    # login_shepherd's setup-token flow feeds this screen from its reader thread while the
    # request thread concurrently scans it with find_authorize_url (#846). pyte is not
    # reentrant, so without PtyScreen's internal lock a scan can catch feed() mid-mutation
    # and raise "dictionary changed size during iteration" (or read a torn row). Feeding
    # in tiny chunks while a reader hammers find_authorize_url maximizes that overlap; with
    # the lock every scan sees a whole, self-consistent screen: no reader ever raises, and
    # once the full URL has been fed the scan returns it intact.
    # (A scan that lands BETWEEN two feeds can still see a legitimately partial URL — that
    # is a separate consumer-side stability concern, tracked as a follow-up, not a screen
    # corruption, so it is not asserted here.)
    scr = None
    url = "https://claude.com/cai/oauth/authorize?code=" + ("a" * 400)
    errors: list[BaseException] = []
    stop = threading.Event()

    def _scan() -> None:
        try:
            while not stop.is_set():
                scr.find_authorize_url()
        except BaseException as exc:  # noqa: BLE001 — record any reader-thread crash
            errors.append(exc)

    for _ in range(40):
        scr = PtyScreen(cols=600, rows=4)
        reader = threading.Thread(target=_scan)
        reader.start()
        try:
            for chunk in (url[i : i + 3].encode() for i in range(0, len(url), 3)):
                scr.feed(chunk)
        finally:
            stop.set()
            reader.join(timeout=5)
        stop.clear()

    assert not errors, f"reader thread crashed: {errors[0]!r}"
    assert scr.find_authorize_url() == url


def test_find_authorize_url_prefers_authorize_path_over_same_host_decoy_after_it():
    # Greptile P1 "same-host decoy wins": the real authorize URL prints FIRST, then the CLI
    # later renders a same-host non-authorize page (e.g. a settings/account screen) — the
    # authorize-path match must still win over the later same-host URL.
    scr = PtyScreen(cols=120, rows=6)
    scr.feed(
        b"Open this URL to authorize: https://claude.com/cai/oauth/authorize?code=1\r\n"
        b"Manage your account at https://claude.com/account"
    )
    assert scr.find_authorize_url() == "https://claude.com/cai/oauth/authorize?code=1"


# --- OSC 8 hyperlink authorize-URL recovery (#905: setup-token under a Windows ConPTY) ------
#
# Under a ConPTY, `claude setup-token` emits its authorize URL as an OSC 8 hyperlink whose
# target lives ONLY in the escape; pyte (0.8.2) drops it, so a display scan returns None.
# find_authorize_url falls back to the URI captured from the raw byte stream.

_OSC8_AUTH = "https://claude.com/cai/oauth/authorize?code=abc123&state=xyz"


def _osc8(uri: str, label: str = "link", *, bel: bool = False, params: str = "") -> bytes:
    """Build an OSC 8 hyperlink (open + labelled text + close), ST- or BEL-terminated."""
    term = b"\x07" if bel else b"\x1b\\"
    return (
        b"\x1b]8;"
        + params.encode()
        + b";"
        + uri.encode()
        + term
        + label.encode()
        + b"\x1b]8;;"
        + term
    )


def test_extract_osc8_hyperlinks_captures_open_and_skips_close():
    # The open sequence carries the URI; the close sequence (empty URI) is skipped.
    assert extract_osc8_hyperlinks(_osc8(_OSC8_AUTH)) == [_OSC8_AUTH]


def test_extract_osc8_hyperlinks_bel_terminator_and_params():
    # BEL (\x07) is a valid terminator, and non-empty params (id=…) precede the URI.
    assert extract_osc8_hyperlinks(_osc8(_OSC8_AUTH, bel=True, params="id=1")) == [_OSC8_AUTH]


def test_extract_osc8_hyperlinks_none_when_no_hyperlink():
    assert extract_osc8_hyperlinks(b"just some \x1b[31mcolored\x1b[0m output, no hyperlink") == []


def test_extract_osc8_hyperlinks_stray_opener_does_not_swallow_the_real_one():
    # #1356: the params run was `[^;]*`, which excludes `;` but admits ESC — so an
    # unterminated `ESC]8;` ahead of a real hyperlink ate the real opener as its own params
    # and the URI matched one character early, coming back as `;https://…`. That leading `;`
    # then failed `_scan_osc8`'s `https://` filter, so the operator was offered no link at
    # all. Excluding ESC ends the stray opener at the next escape instead.
    stream = b"\x1b]8;junk" + _osc8(_OSC8_AUTH, bel=True)
    assert extract_osc8_hyperlinks(stream) == [_OSC8_AUTH]


def test_osc8_capture_is_chunk_invariant_across_a_stray_opener():
    # The operator-visible half of #1356, and the property `pty_screen_feed_fuzzer`'s oracle
    # asserts: the same bytes must give the same answer however a read() splits them. The
    # carry restarts at the last `ESC]8` opener, so before the fix a whole read lost the URL
    # while a byte-by-byte one recovered it — a login that worked or failed depending on the
    # chunking, and on ConPTY (#905) the hyperlink target is the only recoverable copy.
    stream = b"\x1b]8;junk" + _osc8(_OSC8_AUTH, bel=True)

    def authorize(chunks):
        scr = PtyScreen(cols=200, rows=6, capture_osc8=True)
        for chunk in chunks:
            scr.feed(chunk)
        return scr.find_authorize_url()

    assert authorize([stream]) == _OSC8_AUTH
    assert authorize([stream[i : i + 1] for i in range(len(stream))]) == _OSC8_AUTH


def test_extract_osc8_hyperlinks_params_never_span_a_line():
    # CR/LF are excluded from the params run alongside ESC: a real OSC 8 params field never
    # spans a line, and admitting one would let a stray opener reach across log lines to
    # capture something that was never a hyperlink target.
    assert extract_osc8_hyperlinks(b"\x1b]8;id=1\nnot-a-link;https://evil.example/\x07") == []


@pytest.mark.parametrize(
    ("label", "injected"),
    [
        # #1382, the sibling of the params-run bound above. The URI run used to be
        # `[^\x1b\x07]*`, which admits CR/LF, space and every byte above \x7f, so an
        # unterminated opener kept eating text until some later BEL closed it. RFC 3986 §2
        # makes a URI ASCII-only and admits no control and no space — all MUST be
        # percent-encoded — so ending the run at any of them cannot truncate a legal URI.
        ("crlf", b"\r\nnext line of output"),
        ("lf", b"\nnext line of output"),
        ("space", b" and then some prose"),
        # `ascii`/`replace` turns a stray high byte into U+FFFD, and `a�b.claude.com`
        # still endswith(".claude.com") — so an unresolvable host cleared the known-host bar.
        ("high byte", b"\xffmore"),
        ("del", b"\x7fmore"),
    ],
)
def test_extract_osc8_hyperlinks_uri_stops_at_a_byte_no_uri_may_contain(
    label: str, injected: bytes
):
    uri = b"https://claude.com/cai/oauth/authorize"
    # Positive control FIRST, so a green test can never mean "the regex matched nothing".
    assert extract_osc8_hyperlinks(b"\x1b]8;;" + uri + b"\x07") == [uri.decode()], label
    assert extract_osc8_hyperlinks(b"\x1b]8;;" + uri + injected + b"\x07") == [], label


def test_extract_osc8_hyperlinks_uri_keeps_a_legal_semicolon():
    # `;` is excluded from the PARAMS run but must NOT be excluded from the URI run: it is a
    # legal RFC 3986 sub-delimiter, so excluding it would truncate the URI, fail the
    # terminator match, and drop the hyperlink entirely — the exact harm #1382 fixes.
    uri = b"https://claude.com/cai/oauth/authorize;v=2?a=1"
    assert extract_osc8_hyperlinks(b"\x1b]8;;" + uri + b"\x07") == [uri.decode()]


def test_find_authorize_url_osc8_unterminated_link_is_not_handed_over_corrupted():
    # The operator-visible half of #1382. An unterminated opener carrying the REAL authorize
    # URL used to capture it welded to the next line; `urlsplit` strips CR/LF per WHATWG, so
    # the weld still parsed to the known host and the authorize path and cleared
    # find_authorize_url's strict hidden-target bar — the operator was handed a URL that
    # cannot be opened. Nothing is lost by refusing it: `redact._ANSI_RE`'s OSC branch also
    # excludes CR/LF, so the sequence keeps its payload through `strip_ansi` and the URL still
    # reaches the operator in login_shepherd's fail-closed "Captured output:" message.
    scr = PtyScreen(cols=200, rows=6, capture_osc8=True)
    scr.feed(b"\x1b]8;;" + _OSC8_AUTH.encode() + b"\r\nSetup interrupted\x07")
    assert scr.find_authorize_url() is None


def test_extract_osc8_hyperlinks_a_dropped_uri_does_not_cost_the_next_link():
    # The load-bearing safety claim of #1382 is monotonicity: a narrower URI run can only
    # match LESS, never more, and dropping a malformed opener must not take a later valid link
    # with it. #1356 proves the opposite failure is real in this exact regex — a malformed
    # opener costing the real link that follows it — so it is pinned rather than argued. The
    # first opener's run stops at the space, its next byte is not a terminator, the match
    # fails, and the scan resumes at the second opener.
    uri = b"https://claude.com/cai/oauth/authorize"
    stream = b"\x1b]8;;https://a b\x07label\x1b]8;;" + uri + b"\x07"
    assert extract_osc8_hyperlinks(stream) == [uri.decode()]


def test_osc8_crlf_weld_is_chunk_invariant():
    # The property that regressed last time (#1356): the same bytes must give the same answer
    # however a read() splits them, because `_scan_osc8`'s carry restarts at the last opener.
    # Both runs of `_OSC8_RE` exclude ESC, so a match can never span a later opener — matches
    # are per-opener, the whole opener->terminator span sits inside the carry window, and the
    # verdict cannot depend on the chunking. Pinned here for the #1382 bound as well.
    #
    # The well-formed hyperlink in front is the positive control: without it both sides assert
    # `[]` and the test stays green under ANY change that makes `_scan_osc8` retain nothing —
    # a broken `capture_osc8` wiring, a regex that stops matching at all. With it, a broken
    # carry shows up as a DIFFERENCE between the two chunkings, and a regressed weld shows up
    # as a second entry.
    weld = b"\x1b]8;;" + _OSC8_AUTH.encode() + b"\r\nSetup interrupted\x07"
    stream = _osc8(_OSC8_AUTH, bel=True) + weld

    def retained(chunks):
        scr = PtyScreen(cols=200, rows=6, capture_osc8=True)
        for chunk in chunks:
            scr.feed(chunk)
        return scr._osc8_urls

    assert retained([stream]) == [_OSC8_AUTH]
    assert retained([stream[i : i + 1] for i in range(len(stream))]) == [_OSC8_AUTH]


def test_find_authorize_url_recovers_conpty_osc8_hyperlink():
    # The URL is ONLY inside the hyperlink escape — the visible display shows just "Open link".
    scr = PtyScreen(cols=100, rows=6, capture_osc8=True)
    scr.feed(_osc8(_OSC8_AUTH, label="Open link"))
    assert _OSC8_AUTH not in "\n".join(scr._screen.display)  # pyte really did drop the URI
    assert scr.find_authorize_url() == _OSC8_AUTH


def test_osc8_capture_is_off_by_default_for_the_keeper_screen():
    # The long-lived keeper screen (default capture_osc8=False) never reads find_authorize_url,
    # so it must NOT accumulate hyperlinks (would be an unbounded leak — reviewer finding #1).
    scr = PtyScreen(cols=100, rows=6)  # default: no capture
    scr.feed(_osc8(_OSC8_AUTH, label="Open link"))
    assert scr._osc8_urls == []
    assert scr.find_authorize_url() is None


def test_find_authorize_url_osc8_split_across_feeds():
    # A hyperlink cut mid-URI across two feed() chunks still matches via the carry buffer.
    seq = _osc8(_OSC8_AUTH)
    scr = PtyScreen(cols=100, rows=6, capture_osc8=True)
    scr.feed(seq[: len(seq) // 2])
    assert scr.find_authorize_url() is None
    scr.feed(seq[len(seq) // 2 :])
    assert scr.find_authorize_url() == _OSC8_AUTH


def test_find_authorize_url_prefers_display_text_over_osc8():
    # When a plain-text authorize URL is on the rendered display, it wins over an OSC 8
    # decoy (display-first); the OSC 8 fallback only fires when the display scan is empty.
    scr = PtyScreen(cols=120, rows=6, capture_osc8=True)
    scr.feed(_osc8("https://docs.claude.com/help", label="docs"))
    scr.feed(b"Open this URL to authorize: https://claude.com/cai/oauth/authorize?code=disp")
    assert scr.find_authorize_url() == "https://claude.com/cai/oauth/authorize?code=disp"


def test_find_authorize_url_osc8_fallback_reuses_selection_rule():
    # Two OSC 8 links, a docs decoy before the real authorize link — the shared selection
    # rule (authorize-path preference) picks the real one, not merely the last hyperlink.
    scr = PtyScreen(cols=100, rows=6, capture_osc8=True)
    scr.feed(_osc8("https://docs.claude.com/help", label="docs"))
    scr.feed(_osc8(_OSC8_AUTH, label="authorize"))
    assert scr.find_authorize_url() == _OSC8_AUTH


def test_find_authorize_url_osc8_ignores_non_https_scheme():
    # Parity with the text path's https-only _URL_RE: a stray non-https TUI hyperlink
    # (file://, vscode://) must never surface as an authorize URL (reviewer finding #2).
    scr = PtyScreen(cols=100, rows=6, capture_osc8=True)
    scr.feed(_osc8("vscode://open?file=/etc/passwd", label="open"))
    scr.feed(_osc8("file:///home/user/token.txt", label="file"))
    assert scr._osc8_urls == []
    assert scr.find_authorize_url() is None


def test_osc8_urls_are_fifo_capped():
    # Accumulator is bounded (reviewer finding #1): more than _OSC8_MAX_URLS distinct links
    # evict the oldest, yet the most recent authorize URL still wins (selection is last-wins).
    scr = PtyScreen(cols=100, rows=6, capture_osc8=True)
    for i in range(pty_screen._OSC8_MAX_URLS + 25):
        scr.feed(_osc8(f"https://example.com/link/{i}", label=f"l{i}"))
    scr.feed(_osc8(_OSC8_AUTH, label="authorize"))
    assert len(scr._osc8_urls) <= pty_screen._OSC8_MAX_URLS
    assert scr.find_authorize_url() == _OSC8_AUTH


@pytest.mark.parametrize("cut", [1, 2])
def test_find_authorize_url_osc8_split_inside_the_opener(cut):
    # A chunk boundary landing INSIDE the 3-byte opener (ESC | ]8, or ESC] | 8) must still
    # reassemble — the carry retains the trailing partial opener (reviewer follow-up).
    seq = _osc8(_OSC8_AUTH)
    scr = PtyScreen(cols=100, rows=6, capture_osc8=True)
    scr.feed(seq[:cut])
    assert scr.find_authorize_url() is None
    scr.feed(seq[cut:])
    assert scr.find_authorize_url() == _OSC8_AUTH


def test_find_authorize_url_osc8_rejects_hidden_unknown_host():
    # An OSC 8 target is invisible to the operator (they see only the label), so a hidden
    # authorize-path link on an UNKNOWN host must never be handed back (Greptile P2 security).
    scr = PtyScreen(cols=100, rows=6, capture_osc8=True)
    scr.feed(_osc8("https://evil.example/cai/oauth/authorize?code=x", label="Click here"))
    assert scr.find_authorize_url() is None
    # ...but the real link (known auth host + authorize path) still resolves via the same fallback.
    scr.feed(_osc8(_OSC8_AUTH, label="Open"))
    assert scr.find_authorize_url() == _OSC8_AUTH


def test_find_authorize_url_osc8_rejects_hidden_known_host_non_authorize():
    # A hidden OSC 8 link on an ALLOWED host but NOT the authorize endpoint (status./marketing.
    # pass the host check yet aren't the setup-token authorize URL) must not be returned — a
    # hidden target requires BOTH a known auth host AND an authorize path (Greptile P1 security).
    scr = PtyScreen(cols=100, rows=6, capture_osc8=True)
    scr.feed(_osc8("https://status.claude.com/incidents/42", label="status"))
    scr.feed(_osc8("https://marketing.claude.com/promo", label="promo"))
    assert scr.find_authorize_url() is None
    scr.feed(_osc8(_OSC8_AUTH, label="authorize"))
    assert scr.find_authorize_url() == _OSC8_AUTH


def test_osc8_carry_bounded_under_an_unterminated_opener():
    # An unterminated OSC 8 opener with a pathologically long URI must not grow the carry
    # without bound (memory safety) — it is capped at _OSC8_MAX_CARRY, and captures nothing.
    scr = PtyScreen(cols=100, rows=6, capture_osc8=True)
    scr.feed(b"\x1b]8;;https://claude.com/" + b"a" * 20000)  # no ST/BEL terminator
    assert len(scr._osc8_carry) <= pty_screen._OSC8_MAX_CARRY
    assert scr.find_authorize_url() is None


def test_osc8_carry_empty_when_no_opener_in_flood():
    # A flood with no OSC 8 opener carries nothing — the carry targets `ESC ] 8`, so an
    # unrelated escape stream never accumulates (Greptile P2: carry not misdirected).
    scr = PtyScreen(cols=100, rows=6, capture_osc8=True)
    scr.feed(b"\x1b" + b"0011Ignore" * 20000)
    assert scr._osc8_carry == b""
    assert scr.find_authorize_url() is None


# --- find_authorize_url() at a NARROW width: hard-wrap reassembly (live-smoke regression) --
#
# Live-smoke-tested against real claude 2.1.201 (#846 follow-up): the setup-token PTY was
# opened via a bare `os.openpty()`, which defaults to 80 columns. Real `claude setup-token`
# wraps its ~450-char authorize URL at the terminal width, and the old
# `"\n".join(screen.display)` join left the wrapped URL split across rows — truncating it
# at the first row boundary (e.g. cut mid `client_id=...`). The fix widens the PTY itself
# (`login_shepherd._LOGIN_PTY_COLS`) AND makes `find_authorize_url`/`find_oauth_token`
# reassemble hard-wrapped rows before scanning, so the scan is correct at ANY width. These
# tests pin that reassembly directly at a narrow (80-col) width, independent of the caller's
# chosen PTY size.

_REALISTIC_AUTHORIZE_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88e4"
    "-1234567890ab&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference+user%3A"
    "model_registry&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2F"
    "callback&state=" + ("a" * 120) + "&code_challenge=" + ("b" * 80) + "&code_challenge_method"
    "=S256&final=true"
)


def test_find_authorize_url_reassembles_a_hard_wrapped_url_at_narrow_width():
    # ~450+ chars: WOULD wrap repeatedly at 80 columns (the pre-fix default winsize).
    assert len(_REALISTIC_AUTHORIZE_URL) > 450
    scr = PtyScreen(cols=80, rows=20)
    scr.feed(f"Open this URL to authorize: {_REALISTIC_AUTHORIZE_URL}\r\n".encode())
    found = scr.find_authorize_url()
    # Complete and untruncated: every query param survives, including the very last one.
    assert found == _REALISTIC_AUTHORIZE_URL
    assert found is not None and found.endswith("&final=true")
    assert "client_id=9d1c250a-e61b-44d9-88e4-1234567890ab" in found


def test_find_authorize_url_reassembles_url_fed_as_pre_wrapped_bytes():
    # Feed the URL pre-split into 80-byte chunks with NO separator between them — standing
    # in for a raw pty stream that already wrapped the line before this emulator ever sees
    # it (rather than relying on pyte's own autowrap to reproduce the split). Proves the
    # reassembly is robust to hard-wrapped input arriving in arbitrary chunk boundaries, not
    # just to pyte wrapping it itself in one feed() call.
    text = f"Open this URL to authorize: {_REALISTIC_AUTHORIZE_URL}"
    scr = PtyScreen(cols=80, rows=20)
    for i in range(0, len(text), 80):
        scr.feed(text[i : i + 80].encode())
    found = scr.find_authorize_url()
    assert found == _REALISTIC_AUTHORIZE_URL


def test_find_authorize_url_at_wide_width_never_wraps_in_the_first_place():
    # The primary fix: at the WIDE pty width login_shepherd now uses, the URL fits on one
    # rendered row and never wraps at all — the unwrap reassembly is defense in depth, not
    # what makes this case work.
    scr = PtyScreen(cols=1024, rows=10)
    scr.feed(f"Open this URL to authorize: {_REALISTIC_AUTHORIZE_URL}\r\n".encode())
    assert scr.find_authorize_url() == _REALISTIC_AUTHORIZE_URL


def test_find_oauth_token_reassembles_a_hard_wrapped_token_at_narrow_width():
    # The token line can also wrap at a narrow width; find_oauth_token must recover it whole.
    token = "tok-" + ("x" * 200)
    scr = PtyScreen(cols=80, rows=10)
    scr.feed(f"Login successful.\r\nCLAUDE_CODE_OAUTH_TOKEN={token}".encode())
    assert scr.find_oauth_token() == token


# --- _unwrap_display() pure-unit coverage --------------------------------------------


def test_unwrap_display_joins_a_full_width_row_with_no_separator():
    # A row that fills every column (last char non-space) was hard-wrapped -> no \n.
    # The trailing (last) row's own wrapped-ness only matters for what follows it, which
    # here is nothing, so no trailing newline is appended.
    assert pty_screen._unwrap_display(["abcde", "fghij"]) == "abcdefghij"


def test_unwrap_display_inserts_newline_after_a_short_row():
    # A row that does NOT reach the last column (trailing space) ends a logical line.
    assert pty_screen._unwrap_display(["ab   ", "cd   "]) == "ab   \ncd   \n"


def test_unwrap_display_handles_an_empty_row():
    assert pty_screen._unwrap_display(["", "next "]) == "\nnext \n"


def test_unwrap_display_mixed_wrapped_and_unwrapped_rows():
    # First row wrapped into the second (no separator); second row ends the line (newline).
    assert pty_screen._unwrap_display(["hello", "world ", "tail"]) == "helloworld \ntail"


# --- _url_host / _url_path: malformed-IPv6 authority falls back to empty ---------------


def test_url_host_returns_empty_for_malformed_ipv6_bracket_url():
    # urlsplit('http://[::1/authorize') raises ValueError('Invalid IPv6 URL'); the public
    # _url_host must swallow it and degrade to an empty host, never propagate.
    assert pty_screen._url_host("http://[::1/authorize") == ""


def test_url_path_returns_empty_for_malformed_ipv6_bracket_url():
    # Same malformed IPv6 authority through _url_path's urlsplit — degrades to empty path.
    assert pty_screen._url_path("http://[::1/authorize") == ""


def test_find_oauth_token_scrapes_the_rendered_screen():
    scr = PtyScreen(cols=80, rows=3)
    scr.feed(b"Login successful.\r\nCLAUDE_CODE_OAUTH_TOKEN=canned-token-value-xyz")
    assert scr.find_oauth_token() == "canned-token-value-xyz"


def test_find_oauth_token_none_when_absent():
    scr = PtyScreen(cols=60, rows=2)
    scr.feed(b"Login successful.")
    assert scr.find_oauth_token() is None


def test_title_is_never_serialized():
    # An OSC title sequence must not surface in any frame field — OSC 0/1/2 are a
    # data-exfiltration channel, so the title is rendered by pyte but never emitted.
    scr = PtyScreen(cols=20, rows=2)
    scr.feed(b"\x1b]0;SECRET-TITLE\x07visible")
    frame = scr.frame()
    assert "title" not in frame and "icon_name" not in frame
    assert "SECRET-TITLE" not in json.dumps(frame)
    assert frame["rows"][0].startswith("visible")


def test_feed_is_incremental():
    scr = PtyScreen(cols=10, rows=1)
    scr.feed(b"ab")
    scr.feed(b"cd")
    assert scr.frame()["rows"][0].startswith("abcd")


def test_default_geometry_is_120x40():
    frame = PtyScreen().frame()
    assert frame["cols"] == pty_screen.SCREEN_COLS == 120
    assert frame["rows_count"] == pty_screen.SCREEN_ROWS == 40
    assert len(frame["rows"]) == 40


def test_every_frame_row_is_exactly_cols_wide_after_redaction():
    # Redaction can SHORTEN a row (long secret -> 10-char `<redacted>`) or LENGTHEN it: a
    # clipped span-piece shorter than the token becomes the full token, so `Bearer env_01ABCDEF`
    # grows to `<redacted><redacted>` (one longer). frame() re-fits each row to exactly `cols`
    # so the client's fixed grid never wraps. Both directions are exercised; what the trim
    # itself can expose is covered by the test below.
    scr = PtyScreen(cols=24, rows=3)
    scr.feed(b"sk-abcdef0123456789 tail")  # long secret -> row shrinks
    scr.feed(b"\r\nBearer env_01ABCDEF")  # clipped `Bearer ` piece -> row grows
    frame = scr.frame()
    assert all(len(row) == 24 for row in frame["rows"])  # exact width, every row
    joined = "".join(frame["rows"])
    assert "sk-abcdef0123456789" not in joined and "env_01ABCDEF" not in joined
    assert "<redacted>" in joined


def test_frame_width_refit_cannot_shear_an_identifier():
    # #1359, safety invariant 4. This row does NOT grow (no clipping overlap), so the width
    # re-fit does not trim it and cannot manufacture the boundary a `\b`-anchored mask needs.
    # `env_ABCDEF` masks to the neutral token (same length); `cse_ABCDEFGH_zzz` is correctly not
    # an id (trailing `_zzz`) and stays readable. The growing-row shear is the test below.
    cols = 32
    row = "env_ABCDEF FAKE cse_ABCDEFGH_zzz"
    assert len(row) == cols
    scr = PtyScreen(cols=cols, rows=2)
    scr.feed(row.encode())

    delivered = scr.frame()["rows"][0]
    assert len(delivered) == cols
    assert not _BARE_ID_RE.search(delivered), f"the re-fit exposed an identifier: {delivered!r}"
    assert delivered == "<redacted> FAKE cse_ABCDEFGH_zzz"


def test_frame_re_redacts_a_row_the_width_refit_sheared():
    # #1359, safety invariant 4. `_apply_spans` clips the `Bearer ` piece to the full token, so
    # this 19-char row GROWS to `<redacted><redacted>` (20) and the re-fit trims it back to 19.
    # The re-redact belt (`_fit_redacted_row`) runs on the shortened row so the trim cannot
    # shear a bare identifier into the frame. Here the trim cuts inside a `<redacted>` token,
    # so this case does NOT make the belt load-bearing -- delete the belt and it stays green.
    # The real belt guard is test_frame_re_redacts_a_trailing_welded_id_the_width_refit_exposes
    # below, where the trim bares an id unless the belt re-redacts.
    scr = PtyScreen(cols=19, rows=1)
    scr.feed(b"Bearer env_01ABCDEF")
    delivered = scr.frame()["rows"][0]
    assert len(delivered) == 19
    assert not _BARE_ID_RE.search(delivered), f"the re-fit exposed an identifier: {delivered!r}"
    assert delivered == "<redacted><redacted"


def test_frame_re_redacts_a_trailing_welded_id_the_width_refit_exposes():
    # #1359/#1471, safety invariant 4. The sibling test above trims inside a `<redacted>`
    # token, so it stays green even if the re-redact belt is deleted -- a false guard. This
    # case makes the belt load-bearing: `cse_ABCDEFGH_` is welded to a trailing `_`, so the
    # `\b`-anchored id mask leaves it whole (no boundary between `H` and `_`) on the first
    # pass. `Bearer env_01ABCDEF` grows the row by one (`<redacted><redacted>`, 20 for 19), so
    # the re-fit trims exactly the trailing `_` and manufactures the boundary the mask needed,
    # baring `cse_ABCDEFGH`. Only `_fit_redacted_row`'s re-redact masks it; drop that pass and
    # this row ships `<redacted><redacted> cse_ABCDEFGH` and the assertions below go red.
    raw = "Bearer env_01ABCDEF cse_ABCDEFGH_"
    # Pin the BELT specifically (#1471 nit): the first redaction pass alone leaves the welded id
    # whole -- the trailing `_` blocks the `\b`-anchored mask -- so only `_fit_redacted_row`'s
    # post-trim re-redact can remove it. Without this, the exact-equality below could pass if a
    # future first-pass change masked the id pre-fit, silently retiring the belt from this guard.
    assert "cse_ABCDEFGH" in pty_screen.redact_screen_text([raw])[0]
    scr = PtyScreen(cols=len(raw), rows=1)
    scr.feed(raw.encode())
    delivered = scr.frame()["rows"][0]
    assert len(delivered) == len(raw)
    assert not _BARE_ID_RE.search(delivered), f"the re-fit exposed an identifier: {delivered!r}"
    assert "cse_ABCDEFGH" not in delivered
    assert delivered == "<redacted><redacted> <redacted>  "


def test_frame_leaves_an_unshorn_row_to_the_single_redaction_pass():
    # The other side of that branch: masking a long secret SHORTENS the row, so the re-fit
    # trims nothing, no word boundary can have been manufactured, and the row ships exactly
    # as the first redaction pass produced it — padded, not cut.
    scr = PtyScreen(cols=40, rows=2)
    scr.feed(b"token sk-abcdef0123456789 ok")
    assert scr.frame()["rows"][0] == "token <redacted> ok".ljust(40)


# --- #1487: a redactable token the terminal hard-wraps across the fixed width -----------
# Per-row redaction sees each pyte display row alone, so a token pyte wraps at column 120
# is split into two fragments that neither match -> it reaches the browser unmasked. frame()
# now joins a wrapped row with its continuation, redacts the whole logical line, and refits.

# Each token is long enough to straddle the 120-col wrap when pushed past column 110, and is
# split so NEITHER row fragment matches on its own: the row0 tail is too short after its
# prefix (or lacks the prefix), and the row1 head has no prefix at all.
_WRAP_LEAK_TOKENS = [
    "session_01ABCDEFGHIJKLMNOPqrstuvwx",  # `_ID_RE`: row0 tail `session_01` is too short
    "12345678-1234-1234-1234-123456789abc",  # `_UUID_RE`: row0 tail `12345678-1` is partial
    "sk-ABCDEFGHIJKLMNOPQRSTUV",  # `_SECRET_RES`: row0 tail `sk-ABCDEFG` is too short
]


@pytest.mark.parametrize("token", _WRAP_LEAK_TOKENS)
def test_frame_masks_a_token_that_wraps_across_the_fixed_width(token):
    # The bug (found dogfooding v1.2): at 120 cols a redactable id/UUID/key printed past
    # column 110 wraps, and per-row redaction masks neither fragment. Push it so the wrap
    # splits it after column 110, then prove no part of it survives on either row. The
    # prefix ends in a space so the `\b`-anchored UUID/key patterns have a real boundary
    # before the token (a word-char pad would weld it and defeat even the joined match).
    prefix = "A" * 109 + " "
    scr = PtyScreen(cols=120, rows=40)
    scr.feed((prefix + token).encode())
    rows = scr.frame()["rows"]
    # row0 fills the width (it wrapped) and row1 carries the continuation; joined edge-to-edge
    # they are the logical line the browser reconstructs.
    joined = rows[0] + rows[1]
    assert token not in joined  # reverting the wrap-join leaves the whole token here -> red
    # No fragment of any shape survives: check a distinctive interior slice, because
    # `_BARE_ID_RE` matches only the session/env/cse shape and is inert for the UUID and sk-
    # rows.
    assert token[10:26] not in joined
    assert "<redacted>" in rows[0]  # the token was masked, not merely trimmed off the edge


def test_frame_masks_a_fixed_length_token_welded_at_the_wrap_boundary():
    # Regression guard (#1487 review). A UUID that ends exactly at the wrap column, with a
    # word char starting the next row, welds into the joined line and loses the `\b` it needs.
    # The per-row pass must still mask it: on row0 alone the UUID ends at the row edge, a real
    # boundary. A joined-only mask leaks it.
    uuid = "12345678-1234-1234-1234-123456789abc"  # 36 chars, fixed-length `_UUID_RE`
    scr = PtyScreen(cols=120, rows=40)
    # A SPACE before the UUID gives it a clean leading boundary (a word-char pad would weld the
    # front and no pass could ever match it). 83 pad + space + 36 UUID fills row0 to exactly
    # column 120 (UUID at cols 84-119, ending the row), then `def` welds onto the UUID's tail
    # at the start of row1 -- the joined view loses the trailing `\b`, so only row0's own pass
    # keeps it masked.
    scr.feed(("A" * 83 + " " + uuid + "def").encode())
    rows = scr.frame()["rows"]
    joined = "".join(rows)
    assert uuid not in joined  # the welded UUID must not survive
    assert "123456789abc" not in joined  # nor its tail fragment on either row
    assert "<redacted>" in rows[0]


def test_frame_wrap_redaction_keeps_the_fixed_geometry():
    # The wire carries no resize negotiation, so every frame must stay exactly cols x rows
    # even after a wrapped token is masked and the group is refit.
    scr = PtyScreen(cols=120, rows=40)
    scr.feed(("A" * 109 + " session_01ABCDEFGHIJKLMNOPqrstuvwx").encode())
    frame = scr.frame()
    assert frame["cols"] == 120 and frame["rows_count"] == 40
    assert len(frame["rows"]) == 40
    assert all(len(row) == 120 for row in frame["rows"])


def test_frame_masks_a_secret_that_wraps_across_three_rows():
    # A token long enough to span more than two rows: the whole wrap group is joined,
    # redacted, and refit back into its own row count -- no fragment leaks on any row.
    secret = "sk-" + "B" * 260  # 263 chars: spans row0 tail, a full row1, into row2
    scr = PtyScreen(cols=120, rows=40)
    scr.feed(("prefix " + secret).encode())
    rows = scr.frame()["rows"]
    joined = "".join(rows[:3])
    assert secret not in joined
    assert "B" * 20 not in joined  # no run of the key body survives on any of the three rows
    assert "<redacted>" in joined
    assert all(len(row) == 120 for row in rows)


def test_frame_masks_an_id_welded_at_the_wrap_boundary():
    # #1487 review, finding 1, safety invariant 4. An id that is a WHOLE token on its own row,
    # ending exactly at the wrap column, welds on the joined line to the word char that starts
    # the next row (`session_01ABCDEFGH` + `_backup`). The joined pass loses the trailing `\b`
    # and matches nothing. The per-row pass must still mask it, because on row0 alone the id
    # ends at the row edge, a real boundary. A joined-only mask leaks the bare id here.
    scr = PtyScreen(cols=120, rows=40)
    # 101 pad + space + `session_01ABCDEFGH` (18) fills row0 to exactly column 120; `_backup`
    # welds onto the id at the start of row1.
    scr.feed(("A" * 101 + " session_01ABCDEFGH" + "_backup").encode())
    rows = scr.frame()["rows"]
    joined = "".join(rows)
    assert "session_01ABCDEFGH" not in joined  # the welded id must not survive on either row
    assert "<redacted>" in rows[0]


def test_redact_wrapped_rows_masks_inside_each_row_without_reflow():
    # #1487 review, finding 2. A mask that shrinks one row must not shift a neighbour row's
    # text. redact_wrapped_screen_rows returns one redacted row per input row, masked in place,
    # even for a false-positive group (row0 ends in a non-space, so it is grouped with an
    # unrelated row1). The old join-then-reslice path shifted row1's content left into the space
    # row0's shorter mask freed.
    from clauster import redact

    row0 = "session_01ABCDEFGHIJ" + " " * 20  # 40 wide; the id masks to a shorter <redacted>
    row1 = "K" * 40  # unrelated full-width content swept into the same group
    out = redact.redact_wrapped_screen_rows([row0, row1])
    assert out[1] == row1  # row1 verbatim -- not reflowed by row0's shorter mask
    assert "<redacted>" in out[0] and "session_01" not in out[0]


def test_redact_wrapped_rows_unions_welded_secret_tokens():
    # #1487 review, round 2, safety invariant 4. Two secrets welded across the wrap column:
    # `ghp_...` greedily extends over the next row's `sk` on the joined line, so its span, clipped
    # to row1 and applied in SEQUENCE, masks the `sk` and leaves the `sk-` tail bare. The joined
    # and per-row spans must be UNIONED and applied together, or the tail leaks.
    from clauster import redact

    out = redact.redact_wrapped_screen_rows(
        ["ghp_ABCDEFGHIJKLMNOPQRST", "sk-ABCDEFGHIJKLMNOPQRSTB", "l4zR"]
    )
    # The shared body appears in BOTH secrets; it must survive in neither row (sequential
    # application leaves the sk- tail, the union masks it).
    assert "ABCDEFGHIJKLMNOPQRST" not in "".join(out)


def test_frame_masks_a_bearer_value_that_wraps_at_its_internal_space():
    # #1487 final fuzz review. A bearer header can wrap at the space inside `bearer <value>`, so
    # the row ENDS in that space. A run heuristic that continues a group only on a non-space last
    # cell stops there and never joins the value, leaking it. Joining the whole display for
    # split-detection catches it (safety invariant 4).
    scr = PtyScreen(cols=80, rows=10)
    # 72 pad + space + "bearer" + space fills row0 to column 80, so the value lands on row1.
    scr.feed(("A" * 72 + " bearer TOKENVALUE123456").encode())
    joined = "".join(scr.frame()["rows"])
    assert "TOKENVALUE123456" not in joined
    assert "<redacted>" in joined


def test_frame_masks_a_uuid_whose_head_a_joined_greedy_match_would_cover():
    # #1487 review, rounds 3/5, safety invariant 4. A greedy `ghp_` on the JOINED line eats the
    # next row's UUID leading hex digits. With ONE shared coverage map that marks them before the
    # row-local pass can expose the UUID (which is welded to a trailing id), the UUID middle
    # leaks. Two INDEPENDENT maps -- per-row and joined, unioned only at render -- keep the
    # row-local pass able to mask the whole UUID.
    scr = PtyScreen(cols=120, rows=10)
    raw = (
        "A" * 99
        + " ghp_"
        + "B" * 16
        + "12345678-1234-1234-1234-123456789abc"
        + "session_01BBBBBBBB"
    )
    scr.feed(raw.encode())
    joined = "".join(scr.frame()["rows"])
    assert "-1234-" not in joined  # no fragment of the UUID survives on either row
    assert "123456789abc" not in joined


def test_frame_masks_a_uuid_a_greedy_secret_ate_and_the_wrap_split():
    # #1496 on the wrap path, driven through the real `frame()` surface (unlike the direct
    # `redact_wrapped_screen_rows` unit case in `tests/test_redact.py`). A greedy `ghp_` eats the
    # UUID's leading hex group and, with NO trailing id to anchor on (unlike
    # `test_frame_masks_a_uuid_whose_head_a_joined_greedy_match_would_cover`), the middle
    # `-1234-...` used to leak. Here pyte hard-wraps the UUID mid-token, so this exercises
    # `_redact_display`'s wrap grouping, `redact_wrapped_screen_rows`, and `_fit_redacted_row`.
    scr = PtyScreen(cols=80, rows=10)
    # 48 pad + space + `ghp_` + 16 B + the UUID = 80 cells before the UUID's tail, so the token
    # hard-wraps inside the UUID onto row1 with no trailing id to anchor on.
    raw = "A" * 48 + " ghp_" + "B" * 16 + "12345678-1234-1234-1234-123456789abc"
    scr.feed(raw.encode())
    joined = "".join(scr.frame()["rows"])
    assert "-1234-" not in joined  # no fragment of the UUID survives on either row
    assert "123456789abc" not in joined
    assert "<redacted>" in joined


def test_frame_masks_a_wrapped_look_alike_the_safe_direction():
    # #1487 review, finding 1 tradeoff. When `resolve_session_transcript` wraps so a row starts
    # with `session_transcript`, the per-row pass masks that fragment. It is a benign word, so
    # this over-masks a look-alike. That is the accepted SAFE direction on this surface: not
    # leaking a real id beats keeping a look-alike readable (safety invariant 4). Masking only
    # the joined line would leave a real welded id bare, the leak this path closes.
    word = "resolve_session_transcript"
    scr = PtyScreen(cols=120, rows=40)
    # Pad by 112 so the wrap falls right after `resolve_`, starting the next row with
    # `session_transcript`.
    scr.feed(("A" * 112 + word).encode())
    joined = "".join(scr.frame()["rows"])
    assert "session_transcript" not in joined  # the look-alike fragment is masked (safe)
    assert "<redacted>" in joined


def test_no_control_bytes_ever_reach_rows():
    # Lock the cells-only invariant against a future pyte change: a corpus of adversarial
    # control/escape sequences (7-bit + 8-bit C1 OSC, DCS, APC, CSI, lone ESC, NUL/BEL/DEL)
    # must never land a C0/C1 control byte in a rendered row. pyte owns escape parsing; this
    # pins the property at OUR egress so a pyte upgrade can't silently start leaking raw bytes.
    scr = PtyScreen(cols=30, rows=4)
    corpus = [
        b"\x1b]0;title\x07",  # 7-bit OSC title (BEL-terminated)
        b"\x1b]2;title\x1b\\",  # 7-bit OSC title (ST-terminated)
        b"\x1b]8;;https://evil/\x07",  # OSC 8 hyperlink
        b"\x1b]52;c;c2VjcmV0\x07",  # OSC 52 clipboard
        b"\x9d0;title\x9c",  # 8-bit C1 OSC + ST
        b"\x1bP1;2;3qpayload\x1b\\",  # DCS
        b"\x1b_application\x1b\\",  # APC
        b"\x1b",  # lone ESC
        b"\x00\x07\x7f\x08\x0c",  # NUL BEL DEL BS FF
        b"text\x9bafter",  # 8-bit CSI introducer
    ]
    for chunk in corpus:
        scr.feed(chunk)
    for row in scr.frame()["rows"]:
        for ch in row:
            o = ord(ch)
            assert o >= 0x20 and o != 0x7F and not (0x80 <= o <= 0x9F), (
                f"control byte {o:#x} leaked into a rendered row"
            )


def test_screen_sidecar_path_is_stem_dot_screen_json():
    # The shared naming helper (used by both the keeper-spawn writer and the /ws reader)
    # swaps the log's suffix for `.screen.json`, keyed off the stem so it sits beside its set.
    assert pty_screen.screen_sidecar_path(Path("/logs/alpha-1700000000000-1.log")) == Path(
        "/logs/alpha-1700000000000-1.screen.json"
    )


def test_read_screen_sidecar_roundtrips_a_frame(tmp_path: Path):
    p = tmp_path / "x.screen.json"
    frame = {"seq": 3, "state": "live", "error": None, "screen": {"rows": ["hi"]}}
    p.write_text(json.dumps(frame), encoding="utf-8")
    assert pty_screen.read_screen_sidecar(p) == frame


def test_read_screen_sidecar_missing_file_returns_none(tmp_path: Path):
    # The keeper may not have written the sidecar yet — a missing file is a wait, not a fail.
    assert pty_screen.read_screen_sidecar(tmp_path / "nope.screen.json") is None


def test_read_screen_sidecar_malformed_json_returns_none(tmp_path: Path):
    p = tmp_path / "x.screen.json"
    p.write_text("{not json", encoding="utf-8")
    assert pty_screen.read_screen_sidecar(p) is None


def test_read_screen_sidecar_non_object_returns_none(tmp_path: Path):
    # A valid-JSON but non-object payload (e.g. a bare list) is rejected like malformed input.
    p = tmp_path / "x.screen.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert pty_screen.read_screen_sidecar(p) is None


def test_missing_pyte_raises_clear_error(monkeypatch):
    # Without the optional `pty` extra, the lazy import fails -> a clear
    # PyteUnavailableError naming the extra, never a bare ImportError. Simulate the
    # absent dependency by poisoning sys.modules so `import pyte` re-raises ImportError.
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setitem(sys.modules, "pyte", None)
    with pytest.raises(PyteUnavailableError, match=r"clauster\[pty\]") as exc_info:
        PtyScreen()
    # Pin the non-frozen path independently — both messages name clauster[pty], so without
    # this the test would still pass if the sys.frozen branch were inverted or removed.
    assert "standalone binary" not in str(exc_info.value)


def test_missing_pyte_frozen_binary_message(monkeypatch):
    # On the standalone (frozen) binary, with the opt-in env var UNSET, pyte stays absent
    # (LGPL, not bundled), so the error must name the binary limitation AND both working paths
    # — the managed `clauster deps install pty` (#904 slice 2b) and the CLAUSTER_PYTE_PATH env
    # var — instead of the dead-end `install clauster[pty]` a binary user cannot act on (#699).
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv(pty_screen.PYTE_PATH_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "pyte", None)
    with pytest.raises(PyteUnavailableError) as exc_info:
        PtyScreen()
    msg = str(exc_info.value)
    assert "standalone binary" in msg
    assert "clauster deps install pty" in msg  # the managed install command (binary bundles pip)
    assert pty_screen.PYTE_PATH_ENV in msg


class _ExternalOnlyPyteFinder:
    """A meta-path finder that serves `pyte` ONLY from `external_dir`, masking site-packages.

    The dev/CI venv has the real `pyte` installed, and the #699 shim APPENDS the external
    dir to sys.path (so a bundled/earlier copy always wins) — which means in this venv the
    installed pyte would still win on the retry and the external-load path could never be
    proven. Inserting this finder at the FRONT of sys.meta_path makes `pyte` resolvable only
    from `external_dir`: before the shim runs `external_dir` is not on sys.path, so it raises
    ImportError (standing in for the frozen binary's absent pyte); after the shim appends
    `external_dir`, this finder loads the external copy from disk.
    """

    def __init__(self, external_dir: Path):
        self.external_dir = external_dir

    def find_spec(self, name, path, target=None):
        if name != "pyte":
            return None
        if str(self.external_dir) not in sys.path:
            raise ImportError("pyte unavailable until CLAUSTER_PYTE_PATH is on sys.path")
        src = self.external_dir / "pyte.py"
        return importlib.util.spec_from_file_location("pyte", src)


def test_external_pyte_path_loads_pyte_on_frozen_binary(monkeypatch, tmp_path):
    # The opt-in escape hatch (#699): on the frozen binary, CLAUSTER_PYTE_PATH pointing at a
    # directory holding an installed `pyte` lets `_import_pyte()` find it. The first import
    # is forced to fail (the venv has the real pyte; _ExternalOnlyPyteFinder stands in for the
    # frozen binary's absent pyte), so the shim appends tmp_path and the retry loads our copy.
    # Snapshot and restore sys.path + sys.modules["pyte"] so the fake module never leaks into
    # the rest of the suite (a leaked tmp dir on sys.path or a stale sys.modules entry would
    # corrupt isolation).
    sentinel = "EXTERNAL_PYTE_SENTINEL_699"
    (tmp_path / "pyte.py").write_text(
        "EXTERNAL_PYTE_SENTINEL_699 = True\n"
        "\n"
        "class Screen:\n"
        "    def __init__(self, cols, rows):\n"
        "        self.cols, self.rows = cols, rows\n"
        "\n"
        "class ByteStream:\n"
        "    def __init__(self, screen):\n"
        "        self.screen = screen\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(pty_screen.PYTE_PATH_ENV, str(tmp_path))

    original_path = list(sys.path)
    had_pyte = "pyte" in sys.modules
    prior_pyte = sys.modules.get("pyte")
    sys.modules.pop("pyte", None)  # force the lazy import to actually resolve from disk
    blocker = _ExternalOnlyPyteFinder(tmp_path)
    sys.meta_path.insert(0, blocker)
    try:
        mod = pty_screen._import_pyte()
        assert getattr(mod, sentinel, False) is True
        assert Path(mod.__file__).parent == tmp_path
        # The PtyScreen constructor uses pyte.Screen/ByteStream, so it must build too.
        assert pty_screen.PtyScreen(cols=10, rows=2) is not None
    finally:
        if blocker in sys.meta_path:
            sys.meta_path.remove(blocker)
        sys.path[:] = original_path
        sys.modules.pop("pyte", None)
        if had_pyte:
            sys.modules["pyte"] = prior_pyte


def test_external_pyte_path_nonexistent_dir_fails_closed(monkeypatch, tmp_path):
    # Fail-closed: a CLAUSTER_PYTE_PATH pointing at a non-existent path adds nothing to
    # sys.path and the import still fails cleanly with PyteUnavailableError, not a crash.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(pty_screen.PYTE_PATH_ENV, str(tmp_path / "does-not-exist"))
    monkeypatch.setitem(sys.modules, "pyte", None)
    original_path = list(sys.path)
    try:
        with pytest.raises(PyteUnavailableError, match=r"standalone binary"):
            PtyScreen()
        assert str(tmp_path / "does-not-exist") not in sys.path
    finally:
        sys.path[:] = original_path


def test_external_pyte_path_file_not_dir_fails_closed(monkeypatch, tmp_path):
    # Fail-closed: a CLAUSTER_PYTE_PATH pointing at a FILE (not a directory) is rejected by
    # the is_dir() check, adds nothing to sys.path, and the import fails cleanly.
    target = tmp_path / "pyte-but-a-file"
    target.write_text("not a package dir\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(pty_screen.PYTE_PATH_ENV, str(target))
    monkeypatch.setitem(sys.modules, "pyte", None)
    original_path = list(sys.path)
    try:
        with pytest.raises(PyteUnavailableError, match=r"standalone binary"):
            PtyScreen()
        assert str(target) not in sys.path
    finally:
        sys.path[:] = original_path


def test_external_pyte_path_ignored_when_not_frozen(monkeypatch, tmp_path):
    # The shim is a frozen-binary-only escape hatch: a non-frozen process must ignore
    # CLAUSTER_PYTE_PATH entirely and never touch sys.path, even when the dir exists.
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv(pty_screen.PYTE_PATH_ENV, str(tmp_path))
    original_path = list(sys.path)
    try:
        pty_screen._maybe_add_external_pyte_path()
        assert sys.path == original_path
    finally:
        sys.path[:] = original_path


def test_external_pyte_path_already_on_syspath_is_not_appended_twice(monkeypatch, tmp_path):
    # Idempotent: when the resolved CLAUSTER_PYTE_PATH dir is ALREADY on sys.path, the shim
    # takes the skip-the-append branch and leaves sys.path unchanged — no duplicate entry. A
    # second call (e.g. a retried import) must not keep growing sys.path.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(pty_screen.PYTE_PATH_ENV, str(tmp_path))

    original_path = list(sys.path)
    try:
        sys.path.append(str(tmp_path))  # pre-seed: the dir is already present
        before = list(sys.path)
        pty_screen._maybe_add_external_pyte_path()
        assert sys.path == before  # skip branch: nothing appended, no duplicate
        assert sys.path.count(str(tmp_path)) == 1
    finally:
        sys.path[:] = original_path


def test_external_pyte_path_expanduser_runtime_error_fails_closed(monkeypatch):
    # Fail-closed: Path.expanduser() raises RuntimeError (not OSError) when no home dir can
    # be resolved (no HOME, stripped container). The shim must swallow it and never raise,
    # leaving sys.path untouched — not let a RuntimeError escape into PtyScreen.__init__.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(pty_screen.PYTE_PATH_ENV, "~/pyte-lib")

    def boom(self):
        raise RuntimeError("could not determine home directory")

    monkeypatch.setattr(pty_screen.Path, "expanduser", boom)
    original_path = list(sys.path)
    try:
        pty_screen._maybe_add_external_pyte_path()  # must not raise
        assert sys.path == original_path
    finally:
        sys.path[:] = original_path


def test_external_pyte_path_malformed_module_fails_closed(monkeypatch, tmp_path):
    # Fail-closed: a CLAUSTER_PYTE_PATH dir holding a BROKEN pyte (e.g. a corrupted install
    # that raises SyntaxError on import) must surface PyteUnavailableError, not an opaque
    # traceback — the retry catches any exception, not just ImportError.
    (tmp_path / "pyte.py").write_text("def (this is not valid python\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(pty_screen.PYTE_PATH_ENV, str(tmp_path))

    original_path = list(sys.path)
    had_pyte = "pyte" in sys.modules
    prior_pyte = sys.modules.get("pyte")
    sys.modules.pop("pyte", None)
    blocker = _ExternalOnlyPyteFinder(tmp_path)
    sys.meta_path.insert(0, blocker)
    try:
        with pytest.raises(PyteUnavailableError, match=r"standalone binary"):
            pty_screen._import_pyte()
    finally:
        if blocker in sys.meta_path:
            sys.meta_path.remove(blocker)
        sys.path[:] = original_path
        sys.modules.pop("pyte", None)
        if had_pyte:
            sys.modules["pyte"] = prior_pyte
