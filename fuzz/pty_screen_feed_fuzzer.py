"""Atheris fuzz harness for the stateful ``PtyScreen.feed`` seam.

``pty_login_scan_fuzzer`` drives the three *pure* scanners
(``extract_authorize_url`` / ``extract_osc8_hyperlinks`` / ``extract_oauth_token``) and
reconstructs the selection around them. Review of PR 1331 flagged exactly what that
leaves out — the review comment is worth restating, because it is this harness's whole
brief: *"the harness reconstructs the helper-level filter and selector instead of
exercising ``PtyScreen.feed()`` and ``find_authorize_url()``. This leaves stateful chunk
parsing, retention, and the production visible-to-hidden fallback outside the new fuzz
coverage."* That was accepted as deliberate for #1331 and tracked to issue #1333. This
is it.

The input is raw pty bytes from another process — ``claude setup-token``'s TUI, or a
bridge's terminal output — arriving in whatever chunk sizes a read happens to return.
Two configurations are driven, because production has exactly two:

* the **login screen** (``capture_osc8=True``), scanned for the authorize URL an operator
  is told to click and for the printed ``setup-token`` credential; and
* the **keeper screen** (``capture_osc8=False``), scraped for the connect-URL session id
  and rendered to the dashboard through ``frame()``.

The oracle is **chunk-boundary invariance**: the same bytes, fed as one chunk and as N,
must produce the same answer from every reader. That is not a restatement of anything —
it is a differential between two runs of the same code under different chunkings, and it
is precisely the contract ``_scan_osc8``'s carry logic exists to satisfy ("scans
``carry + data`` so a hyperlink split across two ``feed`` chunks still matches"). An OSC 8
authorize URL that is found when the pty happens to deliver it whole and missed when a
read splits it in the middle is a login that fails intermittently on one machine and not
another — the exact bug class a stateless harness cannot see.

⚠️ The total fed input is capped at :data:`_MAX_BYTES`, and the cap is **load-bearing for
soundness, not just for speed**. ``_scan_osc8`` retains at most ``_OSC8_MAX_CARRY`` bytes
between chunks, so a hyperlink separated from its opener by more than that is
*legitimately* dropped in the chunked run and found in the single-shot one. Fuzzing past
the carry bound would report that documented bound as a crash. The cap sits below it, so
inside this harness the invariance genuinely holds.

⚠️ **Invariance is asserted over the OSC 8 reassembly only — the part clauster owns — and
deliberately NOT over the pyte-rendered screen, because that one does not hold.** The
first runs of this harness established that ``pyte`` itself is not chunk-invariant, with
three separate triggers found in under four minutes:

* an unhandled **C0** control (``\\x01``–``\\x06``, ``\\x10``–``\\x1a``, ``\\x1c``–``\\x1f``):
  ``b"\\x03."`` fed whole renders an empty screen, split renders ``.``;
* an unhandled **C1** control (``U+0080``–``U+009F`` except ``CSI``/``OSC``), which arrives
  as two UTF-8 bytes: ``b"\\xc2\\x930"``, same shape;
* a **combining mark**: ``b"\\xde\\xa7A"`` (``U+07A7`` + ``A``) fed whole loses the ``A``.

The cause is common to all three — pyte matches a run of printable text in bulk and
discards the remainder of the run rather than skipping the one character it cannot place —
so an exclusion list would have to grow to most of the input space, and the fix belongs
upstream, not in an exclusion here.

This is **not** cosmetic, which is why it is reported as an open finding on the PR that
introduced this harness rather than written off. Feeding
``b"see \\x03https://claude.com/cai/oauth/authorize?client_id=..."`` in a single read makes
``find_authorize_url()`` return ``None``, while the same bytes delivered one at a time
return the URL — an operator who is never shown a login link, on a machine where the read
happened to land wrong. The same input shape loses ``pty_keeper``'s ``session_…`` connect-
URL scrape. ``tests/test_fuzz_harness_smoke.py`` pins all three triggers *and* the
authorize-URL loss, so a ``pyte`` upgrade that fixes any of them fails ``just check`` and
sends the reader back here to widen the oracle, instead of leaving a carve-out nobody
revisits.

What remains asserted is the seam the review comment was actually about — ``_scan_osc8``'s
carry across chunk boundaries, which is clauster's own code and runs on the raw bytes
before pyte sees them. It is asserted on every input.

That "every" is recent. This harness's first runs found a chunk dependence that *was*
clauster's rather than pyte's: ``_OSC8_RE``'s parameter run was ``[^;]*``, which excludes
``;`` but **not** ``ESC``, so a stray ``ESC]8;`` earlier in the stream swallowed the next
opener into its own parameters and the URI matched one character early. Meanwhile
``_scan_osc8``'s carry restarts at ``rfind(b"\\x1b]8")``, dropping the stray opener, so a
chunk boundary between them made the two scans see different text. Measured end to end::

    stray = b"\\x1b]8;junk" + b"\\x1b]8;;" + AUTHORIZE_URL + b"\\x07label\\x1b]8;;\\x07"
    one chunk      -> find_authorize_url() is None
    byte-by-byte   -> find_authorize_url() is the real URL

Whole, ``extract_osc8_hyperlinks`` returned ``";https://claude.com/cai/oauth/authorize?…"``
— note the leading ``;`` — which failed the ``https://`` filter in ``_scan_osc8`` and was
discarded, so the operator was shown no link at all. On the Windows/ConPTY path (#905) the
OSC 8 target is the *only* way the authorize URL is recoverable, which made this the
difference between a working login and a dead end, decided by where a ``read()`` happened
to land. Fixed in #1356 by excluding ESC/BEL/CR/LF from the parameter run, so a stray
opener now ends at the next escape instead of eating the real one. The
``_swallows_an_opener`` predicate that used to exempt these inputs from the invariance
oracle went with it; ``tests/test_fuzz_harness_smoke.py`` keeps the reproducer as a
regression test.

The remaining properties are the ones a browser depends on:

* ``frame()`` returns exactly ``rows_count`` rows of exactly ``cols`` characters — the
  client draws a fixed grid, and an off-width row corrupts the geometry.
* No bare ``env_``/``session_``/``cse_`` identifier appears in any rendered row —
  asserted on ``redact_screen_text``'s output for every row AND on every row ``frame``
  actually delivers. Delivered rows were once exempt wherever the width re-fit had
  shortened them, because that trim could shear an identifier into view; #1359 made
  ``frame`` redact the fitted row again, so the exemption is gone. ``find_session_id`` is
  deliberately **not** covered: it returns the id un-redacted by design, feeding the
  keeper's private sidecar rather than a browser.
* A returned authorize URL is always ``https://`` — both the visible-text path
  (``_URL_RE``) and the hidden OSC 8 path (filtered in ``_scan_osc8``) promise it.
* Retention is bounded: the screen never holds more than ``_OSC8_MAX_URLS`` hyperlinks,
  however many a hostile stream emits.

⚠️ Geometry is fuzzed over narrow-to-default widths and **not** set to the production
login width of ``login_shepherd._LOGIN_PTY_COLS`` (1024). Two reasons, in order:
rendering a 40x1024 display costs about 68 iterations/second, which would leave this
harness roughly a thousand executions per batch slice; and the wide pty exists precisely
so the ~450-character authorize URL does *not* wrap, which means the wide setting never
exercises ``_unwrap_display`` — the reassembly whose docstring says it "makes the scan
correct at any width". The narrow widths are where that code runs.
"""

import sys

import atheris

with atheris.instrument_imports():
    # In-block per fuzz/README.md, and here it matters more than usual: `pyte` is the
    # pure-Python terminal emulator under test, so instrumenting it is most of this
    # harness's edge signal, and `re` backs the leak matcher.
    import re

    import pyte  # noqa: F401  — instrumented for coverage; PtyScreen does the importing

    from clauster import pty_screen, redact

#: Mirrors ``redact._ID_RE`` exactly. Restated, not imported: an oracle that asks the
#: redactor whether it redacted cannot fail. Same trade as ``redact_fuzzer``.
_LEAK = re.compile(r"\b(env|session|cse)_[A-Za-z0-9]{6,}\b")

#: Total bytes fed per input. Below ``pty_screen._OSC8_MAX_CARRY`` so chunk-boundary
#: invariance is sound (see the module docstring), and small enough that a pure-Python
#: terminal emulator still runs at a useful rate.
_MAX_BYTES = 1024

#: Screen geometries, narrow-to-default. See the module docstring on why the production
#: 1024-column login width is not among them.
_GEOMETRIES = ((40, 6), (80, 24), (pty_screen.SCREEN_COLS, pty_screen.SCREEN_ROWS))

#: How many chunk boundaries an input may request — so at most ``_MAX_CUTS + 1`` chunks.
#: Bounded because each boundary costs a full ``feed`` call on a pure-Python emulator.
#: ⚠️ This does NOT reach byte-by-byte delivery for anything longer than 9 bytes, so the
#: densest real chunking is outside what the fuzzer explores; the reproducers in the
#: module docstring were minimised by hand rather than found at that density.
_MAX_CUTS = 8

#: The readers compared across chunkings. ``retained`` — the OSC 8 URIs ``_scan_osc8``
#: reassembled — is clauster's own code and is invariant; the pyte-rendered readers are
#: not, and the module docstring says why they are absent rather than merely omitted.
_INVARIANT_KEYS = ("retained",)


def _cut_points(cuts: list[int], length: int) -> list[int]:
    """Map fuzzer-chosen 0-255 fractions onto sorted, deduplicated offsets into ``length``.

    Fractions rather than absolute sizes so a mutation that changes the payload's length
    keeps the boundaries meaningful, and so one control byte cannot demand a cut past the
    end. These are the read sizes a pty actually returns — arbitrary, and never aligned to
    anything in the stream.
    """
    return sorted({cut * length // 256 for cut in cuts if 0 < cut * length // 256 < length})


def _drive(
    data_chunks: list[bytes], cols: int, rows: int, capture_osc8: bool, *, rendered: bool = True
) -> dict:
    """Feed the chunks into a fresh screen and read every scanner off it.

    An exception from ``feed`` is caught and recorded rather than raised, and that is
    **mirroring the production call sites, not hiding a crash**: both
    ``pty_keeper._KeeperDrain.feed`` and ``login_shepherd._pump_pty`` already wrap
    ``screen.feed`` in exactly this broad ``except Exception`` ("a render hiccup must
    never kill the reader"). A harness that crashed here would be reporting a deliberately
    guarded path as unguarded — so the contract worth fuzzing is not "``feed`` never
    raises", it is **"the screen is still sound afterwards"**: every property in
    :func:`check` is asserted on a screen that has already survived whatever ``feed`` did.

    That ``feed`` raises at all stays worth reporting, and is — as an open finding on the
    PR that introduced this harness — because of what the guard costs. ``pty_keeper``'s
    handler disables *both* screen consumers for the rest of the session, so one ordinary
    escape sequence silently ends the live terminal view and the pyte connect-URL scrape
    until the bridge restarts. Two distinct pyte defects reach it, neither exotic:

    * **CSI arity** — ``\\x1b[1;2C`` (a modified cursor key any real terminal emits) raises
      ``TypeError``; the same mismatch fires for ``A``/``B``/``D``/``G``/``H``/``@``/``L``/
      ``P``/``X``.
    * **Out-of-range erase** — ``\\x1b[4J`` raises ``UnboundLocalError``; likewise
      ``\\x1b[5J``, ``\\x1b[9J`` and ``\\x1b[4K``.

    Both are pinned in ``tests/test_fuzz_harness_smoke.py``, so a pyte upgrade that fixes
    either one fails ``just check`` rather than passing unnoticed.

    ⚠️ **The readers are guarded too, and like ``feed`` that guard now mirrors a production
    one.** Every reader below goes through ``pyte``'s ``Screen.display``, and a wide
    (double-width) character left half-overwritten makes it raise ``IndexError: string index
    out of range``. Minimal reproducer, 13 bytes::

        PtyScreen(cols=40, rows=6).feed(b"\\x1bH\\xad\\x80\\xe6\\x80\\xa0\\x1b[H\\xad\\x80\\xae")
        screen.find_authorize_url()          # -> IndexError

    ``pty_keeper`` wraps ``frame()`` ("a render hiccup must never affect the bridge"), and
    ``login_shepherd`` — which reached the caller on the CREDENTIAL path when this harness
    first reported it — now reads both ``find_authorize_url`` and ``find_oauth_token``
    through ``login_shepherd._read_screen`` (#1358), degrading the raise to "no match on
    this frame". ``pyte`` still raises, so this carve-out stays: it skips every input that
    trips the defect, which lets the harness assert everything else instead of reporting one
    known defect forever. Pinned in the suite, so a ``pyte`` release that fixes the raise
    fails ``just check`` and sends the next reader back to narrow this.
    """
    screen = pty_screen.PtyScreen(cols=cols, rows=rows, capture_osc8=capture_osc8)
    fed_cleanly = True
    for chunk in data_chunks:
        try:
            screen.feed(chunk)
        except Exception:  # noqa: BLE001 — mirrors the production guard; see the docstring
            fed_cleanly = False
    try:
        readings = {
            "fed_cleanly": fed_cleanly,
            "read_cleanly": True,
            "authorize": screen.find_authorize_url(),
            "token": screen.find_oauth_token(),
            "session": screen.find_session_id(),
            # Read off the object rather than recomputed, so the retention bound is
            # checked against the state the screen actually kept.
            "retained": list(screen._osc8_urls),
        }
        if rendered:
            # Only the chunked drive's frame is asserted on, and these two are the
            # expensive calls here — `frame` renders every row and `redact_screen_text`
            # runs the masks over all of them. Skipping them on the single-shot drive
            # measured 1.28x (40x6), 1.67x (80x24) and 1.74x (120x40) faster for THAT
            # drive; end to end the win is smaller, since the chunked drive still renders.
            # The `display` reads above still run on both drives, so `read_cleanly` keeps
            # meaning the same thing on either side.
            readings["frame"] = screen.frame()
            # The redaction step's own output, BEFORE `frame` re-fits each row to the
            # screen width. This is where the no-bare-identifier invariant is asserted —
            # see the note on the width re-fit in :func:`check`.
            readings["redacted"] = redact.redact_screen_text(list(screen._screen.display))
        return readings
    except IndexError:
        # ONLY the pyte `display` defect documented above. Deliberately not `Exception`:
        # a crash in `redact_screen_text` would otherwise be absorbed as "read not
        # clean" and silently skip the leak assertion that exists to catch it.
        return {"fed_cleanly": fed_cleanly, "read_cleanly": False}


def check(data: bytes, cuts: list[int], cols: int, rows: int, capture_osc8: bool) -> None:
    """Assert every property above for one byte stream and chunking.

    Split out of :func:`TestOneInput` so ``tests/test_fuzz_harness_smoke.py`` can drive
    the oracle from the suite: fuzz/README.md asks for proof that an assertion *can*
    fire, and an oracle that has never fired is indistinguishable from no oracle at all.
    """
    # Pairwise over the boundaries, so `strict=` is deliberately absent: the second
    # sequence is one shorter by construction.
    offsets = [0, *_cut_points(cuts, len(data)), len(data)]
    parts = [data[a:b] for a, b in zip(offsets, offsets[1:])]  # noqa: B905

    chunked = _drive(parts, cols, rows, capture_osc8)
    # `rendered=False`: nothing reads the single-shot drive's frame — see _drive.
    whole = _drive([data], cols, rows, capture_osc8, rendered=False)
    if not (chunked["read_cleanly"] and whole["read_cleanly"]):
        return  # the pyte `display` IndexError — see _drive's docstring

    # One skip, tied to a reported defect and no wider than its mechanism: a drive whose
    # feed raised left the screen mid-sequence by design, and where the boundary fell
    # decides how much got in — so the two runs are not comparable. Note this is not only
    # about the rendered screen: `feed` runs `_stream.feed` BEFORE `_scan_osc8`, so a pyte
    # raise also costs that chunk's OSC 8 scan (see `_drive`). There was a second skip —
    # inputs where a stray opener was swallowed by `_OSC8_RE`'s parameter run — and it went
    # away with the fix in #1356; see the module docstring.
    if chunked["fed_cleanly"] and whole["fed_cleanly"]:
        for key in _INVARIANT_KEYS:
            assert chunked[key] == whole[key], (
                f"chunk-boundary divergence in {key!r}: "
                f"{len(parts)} chunks gave {chunked[key]!r}, one chunk gave {whole[key]!r}"
            )

    frame = chunked["frame"]
    assert len(frame["rows"]) == rows, f"row count {len(frame['rows'])} != {rows}"
    for row in frame["rows"]:
        assert len(row) == cols, f"row is {len(row)} chars, not {cols}: {row!r}"

    # The no-bare-identifier invariant (safety invariant 4), asserted on BOTH
    # `redact_screen_text`'s output and the row `frame` actually delivers. They are separate
    # assertions because the second one used to fail: `frame` re-fit each row to `cols`
    # AFTER redacting, and this harness found that the trim can EXPOSE an identifier
    # redaction correctly left alone. `redact._ID_RE` ends in `\b`, so
    # `cse_01JABCDEFGHJKMN_more` is not an id and is not masked; trimming that row at the
    # column limit drops the `_more` and leaves a bare, matchable `cse_01JABCDEFGHJKMN` in
    # the frame delivered to the browser. Any word character does it — `_`, `é`, `д` — and a
    # cursor-addressed TUI puts text at the column boundary constantly. #1359 fixed it:
    # `frame` redacts the fitted row again, and the exemption that used to skip rows the
    # trim had shortened came out with the fix, so the egress surface is now asserted whole.
    for redacted_row, frame_row in zip(chunked["redacted"], frame["rows"], strict=True):
        assert not _LEAK.search(redacted_row), (
            f"redaction leak in a rendered row: {redacted_row!r}"
        )
        assert not _LEAK.search(frame_row), f"redaction leak in a delivered row: {frame_row!r}"
    # The cursor is reported but deliberately NOT bounds-asserted. `frame`'s contract is
    # about the row geometry ("the client draws a fixed cols x rows_count grid") and says
    # nothing about the cursor, and pyte will hand back an x past the column count — this
    # harness saw `{'x': 54}` on a 40-column screen. Asserting a bound the module never
    # promised would report a frontend nit as a fuzz crash every batch run; it is recorded
    # as an observation on the PR instead. Only the type is checked, since the frame is
    # JSON-serialized to the browser.
    assert isinstance(frame["cursor"]["x"], int), f"cursor x not an int: {frame['cursor']!r}"
    assert isinstance(frame["cursor"]["y"], int), f"cursor y not an int: {frame['cursor']!r}"

    url = chunked["authorize"]
    assert url is None or url.startswith("https://"), f"non-https authorize URL: {url!r}"
    assert len(chunked["retained"]) <= pty_screen._OSC8_MAX_URLS, (
        f"retained {len(chunked['retained'])} hyperlinks, cap is {pty_screen._OSC8_MAX_URLS}"
    )


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    # Layout matters here, because Atheris' provider does NOT draw every type from the
    # same end: `ConsumeBytes`, `ConsumeBool`, `ConsumeInt` and `ConsumeFloat` take from
    # the FRONT, while `ConsumeIntInRange` takes from the BACK (measured on atheris 3.1.0
    # — see fuzz/README.md). Either way the payload must leave bytes behind for the cut
    # fractions: `ConsumeBytes(remaining_bytes())` would take the whole buffer and starve
    # them. An earlier revision did exactly that, and the cut draws silently degenerated
    # to a constant — fixing every run to single-byte chunks and leaving boundary
    # *placement* unfuzzed.
    #
    # ⚠️ SEED AUTHORS: this layout means a seed file is NOT a raw pty capture. It is an
    # envelope — 1 leading byte (`ConsumeBool`, from the front) + the payload + a 9-byte
    # trailer the geometry and `_MAX_CUTS` cut draws take from the back. Paste a capture in
    # unwrapped and its leading `ESC` is eaten as the capture_osc8 flag, silently turning
    # an OSC 8 seed into plain text with nothing to show for it. Every file in
    # `fuzz/seeds/pty_screen_feed_fuzzer/` carries that framing, and
    # `tests/test_fuzz_harness_smoke.py` asserts each one still decodes to a non-empty
    # payload so a hand-added seed cannot quietly land empty.
    capture_osc8 = fdp.ConsumeBool()
    cols, rows = _GEOMETRIES[fdp.ConsumeIntInRange(0, len(_GEOMETRIES) - 1)]
    reserve = min(fdp.remaining_bytes(), _MAX_CUTS)
    raw = fdp.ConsumeBytes(min(max(fdp.remaining_bytes() - reserve, 0), _MAX_BYTES))
    cuts = [fdp.ConsumeIntInRange(0, 255) for _ in range(_MAX_CUTS)]
    check(raw, cuts, cols, rows, capture_osc8)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
