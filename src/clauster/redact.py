"""Redaction + ANSI stripping for the WebSocket bridge-log stream (feature 6).

Per decision D11 the bridge log is *hybrid*: verbatim on disk, redacted over the
WebSocket. So whatever ``redact_session_url`` is set to, the WS stream always
strips the session/env identifiers (they are effectively bearer credentials —
anyone with ``env_<ULID>`` can open a New Session composer for that bridge).
"""

from __future__ import annotations

import bisect
import re
from typing import NamedTuple

# CSI / escape sequences (colors, cursor moves, and the C1 string sequences OSC / DCS /
# SOS / PM / APC).
#
# ⚠️ The alternation ORDER is load-bearing. `]` is 0x5D, which falls inside the
# two-character alternative's `\\-_` (0x5C-0x5F) range, and Python alternation is
# ordered first-match-wins — so while that alternative came first it consumed `ESC ]`
# on its own and the OSC alternative was unreachable, leaving every OSC payload
# (terminal title, hyperlink target, clipboard) in the text as readable junk (#1329).
# `P` (DCS, 0x50), `X` (SOS, 0x58), `^` (PM, 0x5E) and `_` (APC, 0x5F) sit in that same
# range and had exactly the same bug (#1344), so they share the OSC alternative's class
# and its position ahead of the two-character branch.
# Both 7-bit terminators are accepted: BEL, and ST (`ESC \`). The 8-bit C1 introducers
# (0x9B CSI / 0x9D OSC / 0x9C ST) are deliberately NOT matched *here* — a raw C1 byte
# from the bridge never survives to get here (every path into redact decodes with
# ``errors="replace"``: logstream.py:118, runner.py:2346), and a deliberately
# UTF-8-encoded U+009B that did arrive keeps its payload as plain text. The redaction
# view below still treats such a character as a boundary-destroying cut.
#
# The string-sequence body excludes ESC and BEL so an *unterminated* one can only scan to
# the next escape rather than to the end of the input, and excludes CR/LF because a real
# OSC/DCS never spans a line: without that, `redact_for_disk` (which runs over a multi-line
# chunk, not a single line) would let one stray `ESC ]` swallow every line up to the next
# BEL and delete them from the public log mirror and from `error_detail`. Together they keep
# the pattern linear: every star is followed by a class disjoint from it, so no alternative
# can backtrack into another (see test_strip_ansi_is_linear_on_osc).
_ANSI_PATTERN = (
    r"\x1B(?:"
    r"[\]PX^_][^\x07\x1B\r\n]*(?:\x07|\x1B\\)"  # OSC/DCS/SOS/PM/APC … BEL or ST
    r"|\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|[@-Z\\-_]"  # two-character escapes (incl. a bare, unterminated `ESC ]`)
    r")"
)
_ANSI_RE = re.compile(_ANSI_PATTERN)

# Control characters that a browser `<pre>` renders as NOTHING, so removing one joins the
# text either side of it exactly as the reader already sees it. TAB/CR/LF are excluded —
# they are visible separators, and CR/LF additionally carry the line structure
# `redact_for_disk` must preserve. DEL (0x7F) and the 8-bit C1 range (0x80-0x9F) are
# included for the same reason: they print nothing, and a C1 introducer that arrived
# UTF-8-encoded is exactly the boundary-destroying byte `_ANSI_PATTERN` declines to parse.
_INVISIBLE_PATTERN = r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]"

# Each mask below is written ONCE, as a bare CORE with no `\b` on either end, and compiled
# from that core three ways: `\b`core`\b` (what the existing passes use), core`\b` (a start
# supplied by a cut) and core alone (both ends supplied by a cut). One source means the
# variants can never drift apart — `test_cut_masks_cannot_drift_from_the_anchored_ones`
# re-derives each from the others.

# API identifiers that act as bearer credentials in a URL.
_ID_CORE = r"(env|session|cse)_[A-Za-z0-9]{6,}"
_ID_RE = re.compile(rf"\b{_ID_CORE}\b")

# Bare UUIDs (e.g. organization_uuid, bridgeId) — account/instance identifiers
# the bridge prints in full. Not bearer credentials, but we still don't surface
# them over the WS stream (the on-disk log keeps them verbatim per D11).
_UUID_CORE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_RE = re.compile(rf"\b{_UUID_CORE}\b")

# A conservative set of obvious secret shapes, as defense-in-depth — the bridge
# already prints "[REDACTED]" for most secrets, but never rely on that alone.
#
# KNOWN LIMITATION (by design): this is a shape ALLOW-LIST anchored on word
# boundaries. It will NOT catch a novel/unstructured high-entropy secret — a
# bearer value that isn't literally "Bearer …", a raw JWT, or a vendor token whose
# prefix isn't listed below all pass through. That is acceptable because this layer
# is defense-in-depth: the *primary* WS guarantees are the env_/session_/cse_ + UUID
# redaction above (D11, the bearer-equivalent identifiers) and the bridge's own
# "[REDACTED]". Add new shapes here as they appear rather than assuming coverage.
_SECRET_CORES: tuple[tuple[str, int], ...] = (
    (r"gh[pousr]_[A-Za-z0-9]{16,}", 0),  # GitHub tokens
    (r"github_pat_[A-Za-z0-9_]{20,}", 0),  # GitHub fine-grained PAT
    (r"glpat-[A-Za-z0-9_-]{16,}", 0),  # GitLab PAT
    (r"AKIA[0-9A-Z]{16}", 0),  # AWS access key id
    (r"sk-[A-Za-z0-9_-]{16,}", 0),  # OpenAI/Anthropic-style (keys can contain `_`)
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", 0),  # Slack tokens
    (r"clauster_pat_[A-Za-z0-9_-]{16,}", 0),  # clauster API token (#360)
    (r"bearer\s+[A-Za-z0-9._-]{12,}", re.IGNORECASE),  # Authorization: Bearer …
)
_SECRET_RES = tuple(re.compile(rf"\b{core}\b", flags) for core, flags in _SECRET_CORES)

#: A core is one greedy class run only when it ends in an OPEN-ended quantifier (``{n,}`` or
#: ``+``), not a fixed ``{n}`` count, and contains no ``\s`` escape. The check reads the
#: pattern text, so ``\s`` is the two-character escape, not a literal space.
_GREEDY_TAIL_RE = re.compile(r"(?:\{\d+,\}|\+)$")


def _is_single_class_run(core: str) -> bool:
    r"""Report whether ``core`` is one greedy character-class run with no internal whitespace.

    Only then can :func:`_cut_spans` mask the whole run from a cut and skip the cuts it
    covers: a same-shape match that starts inside one greedy class run ends no later than the
    run. A FIXED-length core (``_UUID_CORE``, the AWS ``AKIA`` key) is not such a run -- a
    second match can start inside one and end past it (two ``UUID``s that share eight hex
    digits), so ``opened``'s end is not the run end. A core with an ``\s`` escape (the
    ``bearer`` header) can resume past the run on its next whitespace. Both stay precise, mask
    only to ``closed``'s end and never advance ``reach`` (#1379). Both are cheap to leave
    unskipped -- a fixed core scans O(1) per cut, and ``bearer``'s ``\s+`` bounds every scan.
    """
    return r"\s" not in core and _GREEDY_TAIL_RE.search(core) is not None


#: Every mask, in the order the sequential passes apply them, as
#: ``(anchored, opened, closed, keeps_prefix, single_run)``: ``\b``core``\b`` for an ordinary
#: match, core alone and core``\b`` for a match whose start a cut supplies. Only the id mask
#: keeps a readable ``env_``/``session_``/``cse_`` prefix; ``single_run`` marks a core
#: :func:`_cut_spans` may mask a whole run of and skip.
_MASKS: tuple[tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str], bool, bool], ...] = (
    (
        _ID_RE,
        re.compile(_ID_CORE),
        re.compile(rf"{_ID_CORE}\b"),
        True,
        _is_single_class_run(_ID_CORE),
    ),
    (
        _UUID_RE,
        re.compile(_UUID_CORE),
        re.compile(rf"{_UUID_CORE}\b"),
        False,
        _is_single_class_run(_UUID_CORE),
    ),
    *(
        (
            anchored,
            re.compile(core, flags),
            re.compile(rf"{core}\b", flags),
            False,
            _is_single_class_run(core),
        )
        for anchored, (core, flags) in zip(_SECRET_RES, _SECRET_CORES, strict=True)
    ),
)

_INVISIBLE_RE = re.compile(_INVISIBLE_PATTERN)

#: Zero-width probe: ``.match(text, i)`` is truthy exactly when ``\b`` holds at ``i``. Asking
#: the engine avoids re-deriving "word character" by hand and getting `-`/`.` wrong.
_BOUNDARY_RE = re.compile(r"\b")

_REDACTED = "<redacted>"


def strip_ansi(text: str) -> str:
    r"""Remove ANSI/CSI escape sequences (colors, cursor moves, OSC/DCS/SOS/PM/APC).

    String sequences are removed whole — introducer, payload and terminator — in both the
    BEL and 7-bit ST (``ESC \``) terminated forms. An unterminated one keeps its payload:
    only the two-character ``ESC ]`` introducer goes, as for any other bare escape.

    This is the *display* strip other modules scan against (``pty_screen``,
    ``login_shepherd``). Redaction uses the wider :func:`_views`, which also
    removes invisible control characters and records where it cut.
    """
    return _ANSI_RE.sub("", text)


class _Removals(NamedTuple):
    """Where one strip pass deleted characters, as parallel run lists in SOURCE offsets."""

    starts: list[int]
    ends: list[int]
    before: list[int]  #: characters already removed before this run


def _strip_runs(text: str, rx: re.Pattern[str]) -> tuple[str, _Removals]:
    """Remove every ``rx`` match from ``text`` and record the runs that went."""
    parts: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    before: list[int] = []
    pos = removed = 0
    for m in rx.finditer(text):
        parts.append(text[pos : m.start()])
        starts.append(m.start())
        ends.append(m.end())
        before.append(removed)
        removed += m.end() - m.start()
        pos = m.end()
    parts.append(text[pos:])
    return "".join(parts), _Removals(starts, ends, before)


def _map_offset(offset: int, runs: _Removals) -> int:
    """Re-express a SOURCE offset in the stripped text ``runs`` describes.

    An offset inside a removed run collapses to that run's cut position, which is what a
    span endpoint touching deleted characters should become. A bisect keeps this O(log n)
    per lookup, so nothing here walks the text a second time.
    """
    index = bisect.bisect_right(runs.starts, offset) - 1
    if index < 0:
        return offset
    return offset - (runs.before[index] + min(offset, runs.ends[index]) - runs.starts[index])


def _views(text: str) -> tuple[str, str, tuple[int, ...], _Removals]:
    r"""Split ``text`` into the two views redaction needs, plus the cuts between them.

    ``strip_ansi`` alone throws away the one signal that separates a real welded identifier
    from ordinary compound text: *where* a sequence was removed. After stripping,
    ``user\x1bP...\x07env_<ULID>`` is byte-identical to a literal ``userenv_<ULID>`` and no
    ``\b``-anchored mask can match either (#1379).

    * ``stripped`` -- escape sequences removed. This is exactly the view every release up to
      now masked against, and masking it again is what makes this change unable to mask
      LESS than before.
    * ``visible`` -- ``stripped`` with invisible control characters removed too: what a
      browser ``<pre>`` actually shows the operator, and the string that is returned. It is
      the only view in which a control-split identifier (#1370) is one token.
    * ``cuts`` -- offsets in ``visible`` where something vanished, from either removal. A
      cut is a word boundary and nothing else is; that is the whole of the fix.
    * the removals of the second pass, for re-expressing a ``stripped`` span in ``visible``.
    """
    stripped, ansi = _strip_runs(text, _ANSI_RE)
    visible, invisible = _strip_runs(stripped, _INVISIBLE_RE)
    cuts = {_map_offset(_map_offset(start, ansi), invisible) for start in ansi.starts}
    cuts |= {_map_offset(start, invisible) for start in invisible.starts}
    return stripped, visible, tuple(sorted(cuts)), invisible


def _visible(text: str) -> str:
    """Return only what a browser ``<pre>`` shows for ``text`` (no cut bookkeeping)."""
    return _INVISIBLE_RE.sub("", _ANSI_RE.sub("", text))


def _span(hit: re.Match[str], end: int, keeps_prefix: bool) -> tuple[int, int, str]:
    """Build one ``(start, end, replacement)`` mask span for :func:`_apply_spans`."""
    return (hit.start(), end, f"{hit.group(1)}_{_REDACTED}" if keeps_prefix else _REDACTED)


def _cut_spans(
    visible: str,
    cuts: tuple[int, ...],
    opened: re.Pattern[str],
    closed: re.Pattern[str],
    keeps_prefix: bool,
    single_run: bool,
) -> list[tuple[int, int, str]]:
    r"""Find matches of one mask that begin exactly where :func:`_views` cut.

    Anchoring at each cut in turn, rather than scanning for the pattern and filtering, is
    deliberate: a scan returns non-overlapping matches, so a longer match starting at a
    position with no cut would swallow -- and hide -- the welded identifier inside it. The
    fuzz harness found exactly that (``sesession_01Yaenv_Giscon``, where the leaked
    ``env_Giscon`` sat inside a longer unreachable ``session_...``).

    For a ``single_run`` mask (one greedy character-class run, see
    :func:`_is_single_class_run`), a ``closed`` match means a real trailing ``\b`` is
    reachable, so the whole greedy run from the cut is maskable. Masking it as ONE span -- to
    ``opened``'s greedy end, not to ``closed``'s -- and skipping the cuts it covers is what
    keeps the pass linear: without it every cut in ``("\x01sk-" + "A" * 16) * n`` starts a
    greedy match that scans to the end of the input (640 KB took 8.3 seconds, and
    ``redact_for_disk`` is handed a whole bridge log, 10 MB by default). Skipping the covered
    cuts is safe because a same-shape match that starts inside one greedy class run ends no
    later than that run, so the single span already covers it. Masking to the run end (not
    ``closed``'s, which BACKTRACKS onto an interior ``-`` for a ``-``-bearing class like
    ``sk-``/``glpat-``/``xoxb-``) is what stops a second token welded further along the same
    run (``xoxb-...-xoxb-...``) from leaking. Masking the whole run can only ever mask MORE.

    A mask that is NOT ``single_run`` stays precise -- it masks to ``closed``'s end and never
    advances ``reach``. Two shapes need this, and both are already linear without a skip:

    * a FIXED-length core (``_UUID_CORE``, the AWS ``AKIA`` key). ``opened``'s end is the
      pattern's end, not the class run's, so a second match can start inside one run and end
      past it -- two ``UUID``s that share eight hex digits leak the tail of the second. A
      fixed core scans O(1) per cut, so it needs no skip.
    * a core with internal whitespace (the ``bearer`` header). It can RESUME past the run on
      its next ``\s+``, so a later cut is not contained. Its ``\s+`` bounds every scan.

    ``closed`` failing means no real trailing ``\b`` is reachable; an end supplied by a later
    cut is accepted instead. Testing only the LARGEST cut in range suffices -- each such core
    is a ``{n,}`` run over a single class, so if a shorter prefix matches a longer one does
    too. A cut-bounded span never advances ``reach``, because the greedy run continued past
    that cut and a differently-prefixed match can begin at an interior cut and reach a real
    ``\b`` beyond it (``xsession_AAAcse_<ULID>`` fullmatches ``session_AAAcse`` to the cut
    before ``_``, with ``cse_<ULID>`` welded to start at an interior cut).
    """
    spans: list[tuple[int, int, str]] = []
    reach = 0
    for cut in cuts:
        if cut < reach:
            continue
        loose = opened.match(visible, cut)
        if loose is None:
            continue
        hit = closed.match(visible, cut)
        if hit is not None:
            if single_run:
                spans.append(_span(hit, loose.end(), keeps_prefix))
                reach = loose.end()
            else:
                spans.append(_span(hit, hit.end(), keeps_prefix))
            continue
        candidate = bisect.bisect_right(cuts, loose.end()) - 1
        if candidate < 0 or cuts[candidate] <= cut:
            continue
        hit = opened.fullmatch(visible, cut, cuts[candidate])
        if hit is not None:
            spans.append(_span(hit, cuts[candidate], keeps_prefix))
    return spans


def _trailing_cut_spans(
    visible: str, cuts: tuple[int, ...], opened: re.Pattern[str], keeps_prefix: bool
) -> list[tuple[int, int, str]]:
    r"""Find matches that start at a real ``\b`` but whose trailing ``\b`` a cut deleted.

    The mirror of :func:`_cut_spans`, so ``env_<ULID><BEL>_x`` masks whether the escape sits
    before the identifier or after it. A scan is enough here where it was not there: this
    only ever ADDS a span, and :func:`_sanitize`'s fourth source already covers whatever the
    previous pipeline masked.
    """
    spans: list[tuple[int, int, str]] = []
    for candidate in opened.finditer(visible):
        start = candidate.start()
        if not _BOUNDARY_RE.match(visible, start):
            continue
        index = bisect.bisect_right(cuts, candidate.end()) - 1
        if index < 0 or cuts[index] <= start:
            continue
        hit = opened.fullmatch(visible, start, cuts[index])
        if hit is not None:
            spans.append(_span(hit, cuts[index], keeps_prefix))
    return spans


def _apply_spans(visible: str, spans: list[tuple[int, int, str]]) -> str:
    """Replace the UNION of ``spans`` in ``visible``, clipping rather than dropping.

    A span that overlaps an accepted one is clipped to its uncovered sub-ranges, never
    discarded. Discarding it was a real leak: in ``clauster_pat_...<ESC>[menv_AAAAAA-Xenv_``
    the short id span lands inside the long token span, and dropping the long one left
    everything past the id unmasked -- masking LESS than the previous pipeline, which is the
    one thing this design must never do. Every offered byte ends up covered, so the result
    is a union in the literal sense and cannot fall below any single source.

    Spans are offered in mask order -- ids, then UUIDs, then each secret shape -- so the
    common nesting keeps its readable form: ``Bearer env_<ULID>`` masks the id first and
    reports ``env_<redacted>``. A clipped remainder uses the neutral token, since the
    readable prefix is only correct for a whole id span.

    ``bytearray.find`` rather than a slice test: ``any(covered[start:end])`` copies the
    slice before ``any`` can short-circuit, which is quadratic once many spans cover one
    large region.
    """
    if not spans:
        return visible
    covered = bytearray(len(visible))
    pieces: list[tuple[int, int, str]] = []
    for start, end, replacement in spans:
        if covered.find(0, start, end) < 0:
            continue  # every byte already masked
        if covered.find(1, start, end) < 0:
            pieces.append((start, end, replacement))
            covered[start:end] = b"\x01" * (end - start)
            continue
        pos = start
        while pos < end:
            gap = covered.find(0, pos, end)
            if gap < 0:
                break
            stop = covered.find(1, gap, end)
            stop = end if stop < 0 else stop
            pieces.append((gap, stop, _REDACTED))
            covered[gap:stop] = b"\x01" * (stop - gap)
            pos = stop
    pieces.sort()
    parts: list[str] = []
    pos = 0
    for start, end, replacement in pieces:
        parts.append(visible[pos:start])
        parts.append(replacement)
        pos = end
    parts.append(visible[pos:])
    return "".join(parts)


def _sanitize(text: str) -> str:
    r"""Mask every id/secret an operator can READ in ``text``, and return what they read.

    Four sources of mask spans, unioned -- never counted, never compared. A union can only
    mask more than any one source, which is what makes the fix unable to introduce a leak,
    and it is why the rejected two-view *count* approach is not used here.

    #. the ``\b``-anchored masks over ``visible`` -- catches an identifier a control
       character used to split into two too-short fragments (#1370);
    #. the same masks anchored at a cut, and
    #. the same masks ending at a cut -- together these catch an identifier a removed
       sequence welded to the word before or after it, deleting a boundary the masks need
       (#1379);
    #. the same masks over ``stripped``, mapped across -- byte-for-byte the view every
       earlier release masked, so nothing that was masked before can stop being.

    A line with no escapes and no invisible controls skips all of it and takes the old path
    verbatim, so the change costs two ``search`` calls there and nothing else.

    KNOWN RESIDUE (by design, #1379): an identifier whose start in ``visible`` is neither a
    word boundary nor a cut stays visible -- one an attacker wrote with the preceding
    characters as literal bytes (``xyzenv_<ULID>``). That is a different threat: producing
    it means already controlling the line, and an attacker who can print arbitrary text
    beside an identifier has no need to smuggle it past the mask. The escape-weld case is
    the one that matters, because there clauster's OWN bridge prints the identifier and the
    injected escape only deletes the boundary.
    """
    if _ANSI_RE.search(text) is None and _INVISIBLE_RE.search(text) is None:
        return redact_secrets(redact_ids(text))
    stripped, visible, cuts, invisible = _views(text)
    spans: list[tuple[int, int, str]] = []
    for anchored, opened, closed, keeps_prefix, single_run in _MASKS:
        spans += [_span(m, m.end(), keeps_prefix) for m in anchored.finditer(visible)]
        spans += _cut_spans(visible, cuts, opened, closed, keeps_prefix, single_run)
        spans += _trailing_cut_spans(visible, cuts, opened, keeps_prefix)
        spans += [
            (
                _map_offset(m.start(), invisible),
                _map_offset(m.end(), invisible),
                f"{m.group(1)}_{_REDACTED}" if keeps_prefix else _REDACTED,
            )
            for m in anchored.finditer(stripped)
        ]
    return _apply_spans(visible, spans)


def redact_ids(text: str) -> str:
    """Mask ``env_/session_/cse_`` identifiers (prefix kept readable) and bare UUIDs."""
    text = _ID_RE.sub(lambda m: f"{m.group(1)}_{_REDACTED}", text)
    return _UUID_RE.sub(_REDACTED, text)


def redact_secrets(text: str) -> str:
    """Mask obvious secret shapes (API tokens, bearer headers) as defense-in-depth."""
    out = text
    for rx in _SECRET_RES:
        out = rx.sub(_REDACTED, out)
    return out


def sanitize_line(line: str, *, strip_ansi_seq: bool = True) -> str:
    r"""Full sanitization for one streamed log line.

    Redaction always runs against the *rendered* view (:func:`_views`) — escape
    sequences and invisible control characters removed — so neither can split an identifier
    past the ``\b``-anchored regexes, and a sequence removed between a word character and an
    identifier cannot weld the two into something those regexes decline to match (#1379,
    #1370). When ``strip_ansi_seq`` is False the colored line is kept in the output — but
    only if it is provably as redacted as that view; otherwise the stripped+redacted form is
    emitted (color sacrificed for safety on that one line).
    """
    stripped_safe = _sanitize(line)
    if strip_ansi_seq:
        return stripped_safe
    colored = redact_secrets(redact_ids(line))
    # If rendering the colored result reveals a secret the colored pass missed
    # (ANSI bytes split or welded the identifier), the colored line is unsafe — fall back.
    return colored if _visible(colored) == stripped_safe else stripped_safe


def redact_for_disk(text: str) -> str:
    r"""Redact a multi-line chunk of bridge/agent text for at-rest storage and other egress.

    Named for its original caller — the ``logs.redact_session_url`` on-disk mirror, where the
    bridge writes a verbatim private parse-source (which Clauster still reads for readiness
    markers + the session-URL deep-link recovery) and this produces the public copy. It is now
    also the general chunk-at-a-time redactor for text leaving over the API/WS rather than
    line-by-line (clone-job errors, ``instance.error_detail``, agent result text), so a change
    here is NOT confined to the disk mirror.

    Unlike :func:`sanitize_line` it works over a multi-line chunk, but applies the identical
    pipeline — mask against the rendered view (:func:`_views`) so no escape sequence
    or invisible control character can split *or* weld an ``env_/session_/cse_`` id (or a
    secret) past the ``\b``-anchored regexes — so the output never carries a bearer-equivalent
    session/env identifier or a listed secret shape (the ``_SECRET_RES`` allow-list note above
    bounds what "secret" covers). Line structure survives: CR/LF are never cut.
    """
    return _sanitize(text)


def redact_screen_text(rows: list[str]) -> list[str]:
    r"""Redact a rendered terminal screen (already-plaintext cells) row by row.

    The live pty-screen view (#534) feeds pyte-RENDERED rows here, never raw bytes —
    pyte has already consumed every escape sequence, so this applies only the id +
    secret masks. It deliberately does NOT :func:`strip_ansi`: there are no escapes
    left to strip.

    Row COUNT is preserved (the mask runs per row), but a row's LENGTH can change — a
    mask is the fixed ``<redacted>`` token, so a long secret shrinks the row while a
    short ``env_``/``session_`` id can lengthen it. Re-fitting rows to the exact
    terminal width is the caller's job (:meth:`clauster.pty_screen.PtyScreen.frame`
    re-fits each row to the screen width), not this text-only helper's.

    Best-effort defense-in-depth, like the rest of this module: a secret that wraps
    across the fixed column width, or a novel high-entropy value, can still slip through
    (see the ``_SECRET_RES`` note). AUTH-gating the pty-screen endpoint is the *primary*
    control; this only narrows the obvious-secret surface a live screen exposes.
    """
    return [redact_secrets(redact_ids(row)) for row in rows]
