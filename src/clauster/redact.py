"""Redaction + ANSI stripping for the WebSocket bridge-log stream (feature 6).

Per decision D11 the bridge log is *hybrid*: verbatim on disk, redacted over the
WebSocket. So whatever ``redact_session_url`` is set to, the WS stream always
strips the session/env identifiers (they are effectively bearer credentials —
anyone with ``env_<ULID>`` can open a New Session composer for that bridge).
"""

from __future__ import annotations

import bisect
import functools
import heapq
import itertools
import re
from collections.abc import Callable, Iterator
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
#
# The second run is the Unicode Default_Ignorable_Code_Point set — the property whose whole
# meaning is "a conforming renderer shows nothing here": zero-width spaces and joiners, the
# bidirectional controls, the word joiner, the BOM, the soft hyphen, the variation selectors,
# the combining grapheme joiner, the Hangul fillers and the plane-14 tag block. One inside an
# identifier splits it past the `\b`-anchored masks exactly as a C0 control does (#1434, the
# #1370 shape: `env_01AB<U+200B>CDEFGHJK` reaches the reader whole while its BEL sibling is
# masked). Removing each here turns it into a cut the cut-anchored pass masks, and the union of
# cuts can only ever mask MORE (#1379), never legitimate text.
#
# This is the RIGHT property, not the `Cf` (Format) category, which is both too wide and too
# narrow. Too wide: the prepended-concatenation marks (U+0600, U+06DD ARABIC END OF AYAH, ...)
# are `Cf` but DO draw a sign, so stripping them would delete visible text; Default_Ignorable
# excludes them. Too narrow: the variation selectors (U+FE0F), U+034F and the Hangul fillers are
# invisible joiners that are NOT `Cf`, so a `Cf`-only strip would leave the same leak one code
# point over. The ranges are `(Cf | Variation_Selector | Other_Default_Ignorable)` minus the
# code points Unicode excludes because they render: the prepended-concatenation marks, the
# interlinear-annotation controls (U+FFF9-FFFB) and the Egyptian format controls (U+13430-1343F).
# `test_invisible_pattern_is_default_ignorable_not_cf` rebuilds that set from `unicodedata` and
# reds if a Unicode bump adds a member outside these frozen ranges. U+2028/U+2029 are
# deliberately absent: they are LINE separators (Zl/Zp) a `<pre>` renders as a line break, so
# they SEPARATE rather than weld.
# WARNING: a variation selector or ZWJ is stripped too, so a joined emoji sequence in the log
# renders as its separate base glyphs — accepted: redaction (invariant 4) beats emoji fidelity.
_INVISIBLE_PATTERN = (
    r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F"
    r"\u00ad\u034f\u061c\u115f-\u1160\u17b4-\u17b5\u180b-\u180f"
    r"\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    r"\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff"
    r"]"
)

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
#: ``+``), not a fixed ``{n}`` count, and matches no whitespace at all. The check reads the
#: pattern TEXT, so it must reject both the ``\s`` escape AND a literal whitespace character.
_GREEDY_TAIL_RE = re.compile(r"(?:\{\d+,\}|\+)$")
_WS_RE = re.compile(r"\s")


def _is_single_class_run(core: str) -> bool:
    r"""Report whether ``core`` is one greedy character-class run that matches no whitespace.

    Only then can :func:`_cut_spans` mask the whole run from a cut and skip the cuts it
    covers: a same-shape match that starts inside one greedy class run ends no later than the
    run. A FIXED-length core (``_UUID_CORE``, the AWS ``AKIA`` key) is not such a run -- a
    second match can start inside one and end past it (two ``UUID``s that share eight hex
    digits), so ``opened``'s end is not the run end. A core that can match whitespace (the
    ``bearer`` header, whether written ``\s`` or with a literal space) can resume past the run
    on that whitespace. Both stay precise, mask only to ``closed``'s end and never advance
    ``reach`` (#1379). Both are cheap to leave unskipped -- a fixed core scans O(1) per cut,
    and ``bearer``'s whitespace bounds every scan.

    Rejecting a literal-space core (``bearer +...``) as well as the ``\s`` escape is a guard
    on a future core: a literal space would otherwise pass and make the skip unsound.
    """
    return (
        r"\s" not in core
        and _WS_RE.search(core) is None
        and _GREEDY_TAIL_RE.search(core) is not None
    )


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

#: The tail every secret core ends in: one character class with a ``{n}`` or ``{n,}`` count.
#: :func:`_secret_start` cuts a core there into its literal prefix and that tail.
_CORE_TAIL_RE = re.compile(r"\[[^\]]+\]\{(\d+),?\}\Z")


class _SecretStart(NamedTuple):
    """One secret (or id) core, set up so its match is found at EVERY start in a text."""

    prefix: re.Pattern[str]  #: the core up to its class tail: ``ghp_``, ``bearer\s+``
    opened: re.Pattern[str]  #: the whole core with no ``\b`` on either end
    single_run: bool  #: see :func:`_is_single_class_run`
    tail_min: int  #: the ``n`` of the tail's count


def _secret_start(core: str, flags: int, opened: re.Pattern[str]) -> _SecretStart:
    """Split ``core`` into its prefix and its counted class tail, for the every-start scan.

    That is :data:`_SECRET_STARTS` and :data:`_ID_START`. Raise :class:`ValueError` at import
    for a core of any other shape, so a new core cannot slip past the scan unnoticed.
    """
    tail = _CORE_TAIL_RE.search(core)
    if tail is None:
        raise ValueError(f"secret core {core!r} does not end in one counted character class")
    prefix = re.compile(core[: tail.start()], flags)
    return _SecretStart(prefix, opened, _is_single_class_run(core), int(tail.group(1)))


#: Every secret core, in :data:`_SECRET_CORES` order. ``opened`` is reused from :data:`_MASKS`,
#: so it stays inside the anti-drift guard rather than becoming another compile of the core.
_SECRET_STARTS = tuple(
    _secret_start(core, flags, mask[1])
    for (core, flags), mask in zip(_SECRET_CORES, _MASKS[2:], strict=True)
)

#: The id core, set up the same way, for an id welded onto a masked token (#1617). Its prefix
#: ends in ``_``, which is outside its class run, so no two of its starts share a run.
_ID_START = _secret_start(_ID_CORE, 0, _MASKS[0][1])


def _every_start(rx: re.Pattern[str], text: str) -> Iterator[re.Match[str]]:
    """Yield the match of ``rx`` at every start in ``text``, overlapping ones included.

    ``finditer`` resumes after each match, so it skips a match that starts inside the one
    before it. Resuming one character after each START finds those too. Every pattern this
    walks begins with a literal or a fixed-length shape, so the search stays fast.
    """
    hit = rx.search(text)
    while hit is not None:
        yield hit
        hit = rx.search(text, hit.start() + 1)


def _uuid_spans(text: str) -> list[tuple[int, int]]:
    r"""Return every UUID in ``text``, at every start, with no ``\b`` on either end (#1615).

    A UUID is masked wherever it appears: welded onto the word before it (``run_<UUID>``), onto
    the word after it, or onto another UUID, overlapping ones included. The 8-4-4-4-12 hex shape
    with its four hyphens is distinctive enough that no ordinary word matches it. It is
    fixed-length, so the walk is linear: each start reads at most 36 characters.
    """
    return [hit.span() for hit in _every_start(_MASKS[1][1], text)]


def _secret_candidates(text: str) -> Iterator[tuple[int, int]]:
    """Yield every match of every secret core in ``text``, overlapping ones included, in order.

    A plain ``finditer`` resumes after each match, so it skips a token that starts INSIDE the
    one before it. That is the welded chain (#1615): ``ghp_<a>ghp_<b>`` reads as ``ghp_<a>ghp``
    up to the next ``_``, ``AKIA<a>AKIA<b>`` as two fixed-length keys end to end, and
    ``bearer <a>bearer <b>`` as ``bearer <a>bearer`` up to the space. Each prefix is found at
    every start (:func:`_every_start`) and the core is matched there on its own. The matches
    come as ``(start, end)`` in sorted order, merged from one stream per core, so a long run of
    them is never held in memory.
    """
    return heapq.merge(*(_core_candidates(secret, text) for secret in _SECRET_STARTS))


def _core_candidates(secret: _SecretStart, text: str) -> Iterator[tuple[int, int]]:
    """Yield the match of one secret core at every start in ``text``, in order.

    The cost stays linear. A core whose prefix leaves its class run (the ``_`` of ``ghp_``, the
    space of ``bearer``) or that has a fixed length (``AKIA``) cannot start two matches in one
    run, so each run is read once. A :func:`_is_single_class_run` core whose prefix is inside
    its own class (``sk-``, ``xoxb-``) CAN: ``("sk-" + "A" * 16) * n`` has ``n`` starts in one
    run. A start whose prefix ends inside the run the last match of that core read ends where
    that match ended, so its end is taken from there and the run is not read again.
    """
    run_lo = run_hi = -1  # the class run the last single-run match read
    for start in _every_start(secret.prefix, text):
        q, body = start.span()
        if secret.single_run and run_lo <= body <= run_hi:
            if run_hi - body >= secret.tail_min:
                yield q, run_hi
            continue
        hit = secret.opened.match(text, q)
        if hit is None:
            continue
        yield hit.span()
        if secret.single_run:
            run_lo, run_hi = body, hit.end()


def _open_spans(
    text: str, cuts: tuple[int, ...], seeds: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    r"""Return every UUID in ``text``, and every secret or id that starts where a mask may start.

    Both paths union these with their other spans (#1615). A secret here has no trailing ``\b``,
    so one welded to the word after it (``ghp_<a>_x``) masks too. It is kept when its start is:

    * a word boundary, as for the anchored mask,
    * a position in ``cuts``, where a removed escape stood (the log path's #1379 signal), or
    * inside, or right at the end of, a span in ``seeds`` or one kept before it. That is a
      token welded onto a masked token: the second secret of ``ghp_<a>ghp_<b>``, a key after
      a UUID. A kept secret is a span for the next one, so a whole chain masks in one pass.

    An ``env_``/``session_``/``cse_`` id with no trailing ``\b`` is kept by the third test only
    (#1617): ``AKIA<a>session_01<b>``, ``<UUID>env_<b>``. At a word boundary it would mask
    ordinary names such as ``session_timeout_ms``, and an id at a boundary or a cut already has
    its anchored and cut-anchored masks. A kept id is a span for the next token too.

    A secret welded onto an ordinary word (``agentghp_<a>``) has none of these and stays
    visible, the residue both paths document.

    The spans come back MERGED, sorted and disjoint, and the kept secrets are merged as they
    are found. Every start of ``("sk-" + "A" * 16) * n`` is its own span to the end of the run,
    so unmerged, a coverage test per span (:func:`_apply_spans`) would read the run ``n`` times.
    """
    uuids = _uuid_spans(text)
    masked = sorted(seeds + uuids)
    at_cut = frozenset(cuts)
    kept: list[tuple[int, int]] = []
    reach = -1  # the furthest end of a masked span that starts before `q`, or of a kept one
    i = 0
    secrets = ((q, end, False) for q, end in _secret_candidates(text))
    ids = ((q, end, True) for q, end in _core_candidates(_ID_START, text))
    for q, end, welded_only in heapq.merge(secrets, ids):
        while i < len(masked) and masked[i][0] < q:
            reach = max(reach, masked[i][1])
            i += 1
        if q <= reach or not welded_only and (q in at_cut or _BOUNDARY_RE.match(text, q)):
            # A kept span counts for the next start at once. A later candidate at this same
            # start is a secret kept or not by the same three tests, so nothing changes for it:
            # no secret prefix and no id prefix can match at one position.
            reach = max(reach, end)
            if kept and q <= kept[-1][1]:
                kept[-1] = (kept[-1][0], max(kept[-1][1], end))
            else:
                kept.append((q, end))
    return _merged(uuids + kept)


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return the union of ``spans`` as sorted, disjoint spans; touching ones are joined."""
    out: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _fast_path_misses(text: str) -> list[tuple[int, int]] | None:
    r"""Return :func:`_open_spans` for an escape-free ``text``, or None if the anchors cover it.

    None means the sequential path masks every character the union path would, so
    :func:`_sanitize` keeps it. That needs two things. First, every character those spans mask
    is inside an anchored match. Second, no two anchored matches overlap (#1617). The
    sequential path masks the ids first, and a mask it has applied can break a later match
    that overlaps it. In ``Bearer <UUID>.<tail>`` the UUID becomes ``<redacted>``, the bearer
    mask then finds no value, and ``.<tail>`` shows. The union of the same anchored matches
    masks all of it. Otherwise the spans are handed on, so the union path does not find them a
    second time. With no escape, that path's other span sources are exactly the anchored
    matches, the seeds used here.

    With no secret shape in the line, only the UUIDs are left to compare: a UUID with a ``\b``
    on both sides is exactly an anchored ``_UUID_RE`` match, because two UUIDs that both start
    at a ``\b`` cannot overlap. An anchored id cannot overlap one either: an id holds no ``-``,
    and a UUID holds no ``_``. Nor can an id be welded onto either of them, because each ends
    at a ``\b`` before a character that no id starts with. So a line of ordinary bridge output,
    UUIDs included, does not pay for a second run of the anchored masks. A line with a welded
    UUID gets every open span, because an id can be welded onto that UUID.
    """
    if next(_secret_candidates(text), None) is None:
        uuids = _uuid_spans(text)
        if all(_BOUNDARY_RE.match(text, s) and _BOUNDARY_RE.match(text, e) for s, e in uuids):
            return None
        return _open_spans(text, (), [])
    anchored = sorted(m.span() for mask in _MASKS for m in mask[0].finditer(text))
    spans = _open_spans(text, (), anchored)
    if any(start < end for (_, end), (start, _) in itertools.pairwise(anchored)):
        return spans  # two anchored matches overlap: the sequential path can break the later one
    covered = bytearray(len(text))
    for start, end in anchored:
        covered[start:end] = b"\x01" * (end - start)
    return spans if any(covered.find(0, s, e) >= 0 for s, e in spans) else None


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

    Spans are offered in mask order -- ids, then UUIDs, then each secret shape -- so an id
    keeps its readable prefix rather than collapsing into a wider secret match: the id span
    lands first and reports ``env_<redacted>``. A wider secret span that overlaps it (a
    ``Bearer env_<ULID>`` header) is then clipped, so its uncovered prefix becomes a SECOND
    neutral ``<redacted>``: ``hdr:Bearer env_<ULID>`` on the union path reads
    ``hdr:<redacted>env_<redacted>``, not ``hdr:Bearer env_<redacted>``. The clipped remainder
    uses the neutral token because the readable prefix is only correct for a whole id span.
    A line with no escape renders the same way, because two overlapping anchored matches take
    it off the sequential fast path (:func:`_fast_path_misses`, #1617).

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

    A fifth source, :func:`_open_spans`, adds every UUID wherever it appears and every secret
    whose start is a word boundary, a cut, or inside a masked token, with no trailing ``\b``
    (#1615). That masks each secret of a welded chain (``ghp_<a>ghp_<b>``, ``AKIA<a>AKIA<b>``,
    ``bearer <a>bearer <b>``) and a UUID welded onto the word before it (``run_<UUID>``). It
    also adds every id that starts inside a masked token (``AKIA<a>session_01<b>``, #1617).

    A line with no escapes and no invisible controls takes the old path verbatim unless the
    fifth source masks a character the anchored masks leave, or two anchored matches overlap
    (:func:`_fast_path_misses`). So an ordinary line costs a few ``search`` calls and nothing
    else.

    KNOWN RESIDUE (by design, #1379): an identifier whose start in ``visible`` is neither a
    word boundary nor a cut stays visible -- one an attacker wrote with the preceding
    characters as literal bytes (``xyzenv_<ULID>``). That is a different threat: producing
    it means already controlling the line, and an attacker who can print arbitrary text
    beside an identifier has no need to smuggle it past the mask. The escape-weld case is
    the one that matters, because there clauster's OWN bridge prints the identifier and the
    injected escape only deletes the boundary. The same holds for a secret welded onto an
    ordinary word (``agentghp_<a>``); one welded onto a masked token is not residue.
    """
    missed: list[tuple[int, int]] | None = None
    if _ANSI_RE.search(text) is None and _INVISIBLE_RE.search(text) is None:
        missed = _fast_path_misses(text)
        if missed is None:
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
    if missed is None:  # an escape: the seeds are every span above, cut-anchored ones included
        missed = _open_spans(visible, cuts, [(start, end) for start, end, _ in spans])
    spans += [(start, end, _REDACTED) for start, end in missed]
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


#: The screen's one UNANCHORED match: a real id a consumed escape welded onto the word before
#: it. The pty screen has no cut to confirm a weld, so a glued id is indistinguishable from a
#: literal compound word. Requiring the real id shape -- the ``01`` an Anthropic ULID carries
#: after the prefix, then eight or more characters -- keeps ordinary names like
#: ``resolve_session_transcript`` and ``venv_project1`` readable. The anchored ``_ID_RE`` still
#: masks a standalone id of any shape; RESIDUE: a welded id that lacks the ``01`` shape is not
#: caught, and (see :func:`_redact_screen_row`) neither is a secret welded onto an ordinary word.
_SCREEN_GLUED_ID_RE = re.compile(r"(env|session|cse)_01[A-Za-z0-9]{8,}\b")

#: The real id shape of :data:`_SCREEN_GLUED_ID_RE` with no ``\b`` on either end, the open-tail
#: id scan (#1508): ``session_01<...>_backup`` has no trailing boundary. The plain id core is
#: left out: a look-alike such as ``session_timeout_ms`` must stay readable.
#:
#: It finds EVERY start, overlapping ones included, through the zero-width lookahead (#1612).
#: A greedy match runs over the prefix of the next id in a welded chain (``env_01<a>env_01<b>``
#: reads as ``env_01<a>env`` up to the ``_``), so a plain ``finditer`` resumed after it and
#: skipped every second id. The anchored fixed point does not catch those when nothing after
#: the chain gives its last id a trailing ``\b``. The cost stays linear: a match can start only
#: after a ``_01`` it owns, so no two matches share the run of characters after it.
_SCREEN_OPEN_TAIL_ID_RE = re.compile(r"(?=((?:env|session|cse)_01[A-Za-z0-9]{8,}))")

#: The UUID shape with NO ``\b`` on either end, for finding a UUID that a greedy core welded
#: onto (#1496). Reused from the UUID mask's cut-supplied (core-alone) variant rather than
#: recompiled, so it stays inside the anti-drift guard
#: (:func:`test_cut_masks_cannot_drift_from_the_anchored_ones`) instead of becoming a fourth
#: independent compile of the same core. See :func:`_screen_welded_uuid_spans`.
_UUID_CORE_RE = _MASKS[1][1]


def _screen_welded_uuid_spans(
    text: str, greedy_spans: list[tuple[int, int]]
) -> list[tuple[int, int, str]]:
    r"""Mask a UUID whose leading hex group a greedy core consumed (#1496).

    A greedy core such as ``(env|session|cse)_[A-Za-z0-9]{6,}`` (an id), ``ghp_[A-Za-z0-9]{16,}``
    or ``github_pat_[A-Za-z0-9_]{20,}`` (a secret) runs over a class that INCLUDES the UUID's
    leading ``[0-9a-fA-F]{8}`` group but NOT the ``-`` that separates it, so welded with no
    separator (``ghp_<16 hex chars>12345678-...``) it eats that first group and stops at the
    ``-``. The anchored ``_UUID_RE`` needs eight leading hex digits behind a ``\b``, and neither
    is left, so it never matches and the middle ``-1234-...`` reaches the browser. The id or
    secret itself still masks (it stays anchored -- the residue policy the issue keeps intact);
    this only adds the UUID the greedy match would otherwise hide. ``greedy_spans`` are the
    anchored id and secret matches AND the unanchored ``_SCREEN_GLUED_ID_RE`` matches that
    :func:`_screen_spans` already found, passed in so this does not re-scan ``_ID_RE``,
    ``_SECRET_RES`` or ``_SCREEN_GLUED_ID_RE``. The glued-id shape is another greedy id core
    (an open-ended ``[A-Za-z0-9]{8,}`` run, no leading ``\b``), so a UUID welded onto it eats
    the leading hex group exactly the same way (#1496).

    This masks EVERY UUID whose start falls strictly inside a greedy span, which never masks
    LESS than before in either direction. A pure-word-char greedy core (an id, a glued id,
    ``ghp_``, ``github_pat_``) stops at the ``-`` and leaves the UUID's middle bare -- the
    residue this helper exists to cover. A core whose run carries an interior boundary -- a
    hyphen-bearing class (``sk-``, ``glpat-``, ``xox``, ``clauster_pat_``) or ``bearer``'s
    ``\s+`` -- has already swallowed the whole UUID, so re-masking that already-covered UUID
    through :func:`_apply_spans` is a harmless union. A standalone UUID at a real boundary is
    caught by the anchored ``_UUID_RE`` and is not welded, so it is not this helper's concern.

    A UUID that starts exactly where a span ENDS is masked too, and so is a UUID that starts
    exactly where a UUID this helper masked ends. That unwinds a chain of UUIDs welded end to
    end in one pass, left to right (#1612).

    This helper feeds the fixed points only. ``_UUID_CORE_RE.finditer`` is non-overlapping, so
    here a second UUID that overlaps the first by its leading hex group is missed (two all-hex
    UUIDs sharing eight digits). The open-tail pass (:func:`_screen_open_tail_spans`) masks every
    UUID at every start, overlapping ones included, and joins every path at render (#1615).
    """
    if not greedy_spans:
        return []
    spans: list[tuple[int, int, str]] = []
    chained = -1  # the end of the last UUID masked here
    for uuid in _UUID_CORE_RE.finditer(text):
        start = uuid.start()
        if start == chained or any(s < start <= e for s, e in greedy_spans):
            spans.append((start, uuid.end(), _REDACTED))
            chained = uuid.end()
    return spans


def _screen_spans(text: str) -> list[tuple[int, int, str]]:
    """Return every redaction span for one rendered screen line.

    The spans are the anchored id/secret masks (:data:`_MASKS`), the one unanchored welded-id
    shape (:data:`_SCREEN_GLUED_ID_RE`), and a UUID that a greedy id or secret core welded onto
    (:func:`_screen_welded_uuid_spans`). The caller unions them through :func:`_apply_spans`.
    See :func:`_redact_screen_row` for why each mask stays anchored.
    """
    spans: list[tuple[int, int, str]] = []
    greedy_spans: list[tuple[int, int]] = []
    for anchored, *_rest in _MASKS:
        matches = [(m.start(), m.end()) for m in anchored.finditer(text)]
        spans += [(s, e, _REDACTED) for s, e in matches]
        if anchored is _ID_RE or anchored in _SECRET_RES:  # reuse these; do not re-scan in helper
            greedy_spans += matches
    glued = [(m.start(), m.end()) for m in _SCREEN_GLUED_ID_RE.finditer(text)]
    spans += [(s, e, _REDACTED) for s, e in glued]
    greedy_spans += glued  # a glued id core is greedy too: a UUID can weld onto it (#1496)
    spans += _screen_welded_uuid_spans(text, greedy_spans)
    return spans


def _screen_open_tail_spans(text: str, *, cuts: tuple[int, ...]) -> list[tuple[int, int, str]]:
    """Return the open-tail spans in ``text``: tokens with no trailing boundary (#1508).

    The spans are :data:`_SCREEN_OPEN_TAIL_ID_RE` and :func:`_open_spans`, seeded with those ids
    and the anchored ``_ID_RE`` matches: every UUID wherever it appears, and every secret that
    starts at a word boundary, at a soft-wrap seam in ``cuts``, or inside a masked token, so
    each secret of a welded chain masks (#1615). A seam counts because the soft-wrap view joins
    the rows with nothing between them: a chain the TUI moved onto its own rows is welded onto
    the last word of the row above. A token welded to the word after it (``<UUID>zz``) has no
    trailing boundary, so the anchored mask never matches it. It used to mask only when the
    caller's width-refit trim happened to cut the following word away, so whether it showed
    depended on how long the rest of the row rendered.

    These are scanned ONCE over the unmasked text and unioned at render; they never feed a
    fixed point. A greedy open-tail match runs over the prefix of the next token in a welded
    chain (``env_01<a>env_01<b>...`` reads as ``env_01<a>env`` up to the ``_``). Fed back as
    masked cells, it would erase the prefixes the anchored fixed point needs to unwind that
    chain from its end, and the chain would show.
    """
    ids = [m.span(1) for m in _SCREEN_OPEN_TAIL_ID_RE.finditer(text)]
    seeds = ids + [m.span() for m in _ID_RE.finditer(text)]
    return [(s, e, _REDACTED) for s, e in ids + _open_spans(text, cuts, seeds)]


def _fixed_point_coverage(
    text: str,
    ranges: list[tuple[int, int]],
    scan: Callable[[str], list[tuple[int, int, str]]],
    *,
    max_scans: int,
) -> tuple[bytearray, bool]:
    r"""Scan each range of ``text`` to a fixed point; return the coverage and if it settled.

    Stop after ``max_scans`` scans and report ``False`` if the last one still added coverage.
    """
    # Iterate the screen scan over each range until nothing new is covered, marking a map fed
    # ONLY by its own coverage, so no other scan can remove a boundary this one relies on. NUL
    # stands in for a masked cell: it preserves length and matches no core, and gives its
    # neighbours the `\b` a freshly-masked run exposes.
    cov = bytearray(len(text))
    scans = 0
    while scans < max_scans:
        scans += 1
        probe = "".join("\x00" if cov[i] else ch for i, ch in enumerate(text))
        added = False
        for lo, hi in ranges:
            for s, e, _ in scan(probe[lo:hi]):
                s, e = lo + s, lo + e
                if cov.find(0, s, e) >= 0:
                    cov[s:e] = b"\x01" * (e - s)
                    added = True
        if not added:
            return cov, True
    return cov, False


#: The most scans the per-row and hard-run screen fixed points get before they fail closed
#: (#1612): the same cap, for the same reason, as :data:`_SEAM_MAX_SCANS` for the soft-wrap
#: views. Uncapped, a hard-wrapped 40 x 120 chain of welded ids took about 230 ms per frame.
_SCREEN_MAX_SCANS = 16


def _capped_coverage(
    text: str,
    ranges: list[tuple[int, int]],
    scan: Callable[[str], list[tuple[int, int, str]]],
    *,
    max_scans: int,
) -> bytearray:
    """Scan each range of ``text`` to its own capped fixed point; fail closed where one is cut.

    A range still adding coverage after ``max_scans`` scans has every non-space character
    masked, so the cap only bounds the cost: it never leaves visible a non-space character the
    uncapped scan would have hidden. (Whitespace inside a ``bearer`` match can show; no value
    class holds a space, so no secret character does.) Each range is
    independent (the scan sees only its own slice), so capping them one at a time masks
    exactly what one multi-range fixed point masks, and a crafted range fails closed alone.
    """
    cov = bytearray(len(text))
    for lo, hi in ranges:
        part, settled = _fixed_point_coverage(
            text[lo:hi], [(0, hi - lo)], scan, max_scans=max_scans
        )
        cov[lo:hi] = part if settled else _fail_closed(text[lo:hi])
    return cov


def _fail_closed(text: str) -> bytearray:
    """Return the coverage a capped scan falls back to: every non-space character of ``text``."""
    return bytearray(0 if ch.isspace() else 1 for ch in text)


def _render_coverage(text: str, cov: bytearray) -> str:
    """Replace each maximal run of covered cells in ``text`` with one ``<redacted>`` token."""
    runs: list[tuple[int, int, str]] = []
    i = 0
    while i < len(text):
        if cov[i]:
            j = i + 1
            while j < len(text) and cov[j]:
                j += 1
            runs.append((i, j, _REDACTED))
            i = j
        else:
            i += 1
    return _apply_spans(text, runs)


def _redact_screen_row(row: str) -> str:
    r"""Mask id/secret shapes in one pyte-rendered row, plus a real id welded onto a word.

    This surface has no cut signal. pyte renders the grid before redaction runs and erases
    the escape that welded a word onto an identifier: ``agent\x1b[32menv_<ULID>`` arrives as
    the row ``agentenv_<ULID>``, byte-identical to a literal ``userenv_<ULID>``. So the
    log-path cut-anchored pass (:func:`_cut_spans`) cannot help here -- there is nothing left
    to anchor on.

    Every mask runs ANCHORED, exactly as the old ``redact_secrets(redact_ids(row))`` pass, so
    a standalone id or secret masks as before and no ordinary word is over-masked. Two
    UNANCHORED matches join them: :data:`_SCREEN_GLUED_ID_RE`, the real id shape, which covers
    an id a consumed escape welded onto the word before it (``agentenv_01<...>``); and
    :func:`_screen_welded_uuid_spans`, a UUID whose leading hex group a greedy id, glued id or
    secret core ate, welding it onto that core (``ghp_<16 hex chars>12345678-...``, or the
    glued ``agentenv_01<...>12345678-...``, #1496). The secrets stay anchored on purpose: a
    glued secret core (``sk-``, ``glpat-``, ``xoxb-``) matches inside an
    ordinary hyphenated word (``risk-assessment-checklist`` -> ``ri<redacted>``), so with no
    cut to confirm a real weld, unanchoring them destroys readable text.

    Masks replace with the NEUTRAL ``<redacted>`` token here, dropping the readable
    ``env_``/``session_``/``cse_`` prefix the log path keeps. That is what closes an id welded
    to another id: ``env_01<a>session_01<b>`` masks the second id first, and the ``<`` of its
    neutral token gives the first id the trailing boundary it lacked, so the fixed point below
    masks it too. Keeping the prefix would leave ``session_`` (word characters) there and the
    first id would stay bare. The screen is a display surface, so the missing marker is only
    cosmetic; the log path keeps it.

    The spans are UNIONED through :func:`_apply_spans`, not applied by sequential ``sub`` (a
    sequential sub can mask LESS -- a mask inserts a ``<`` that shortens a later match below
    its minimum), and the union runs to a FIXED POINT (a ``<redacted>`` token masking inserts
    is a boundary that can expose a neighbour). It terminates because each pass masks strictly
    more and a ``<redacted>`` token never matches a core. It is also BOUNDED: a row still
    masking after :data:`_SCREEN_MAX_SCANS` passes (a crafted chain of welded tokens needs one
    pass each) fails closed, with every non-space character masked (#1612).

    The TRAILING anchor is a different matter (#1508). A UUID, a secret or a real ``01``-shape
    id welded to the word AFTER it (``<UUID>zz``) is masked by :func:`_screen_open_tail_spans`,
    which keeps the leading ``\b`` and drops the trailing one. Before, such a token masked only
    when the caller's width-refit trim happened to cut the following word away. Those spans
    are unioned with the fixed point's coverage, never fed into it; a row they add nothing to
    renders exactly as the fixed point alone renders it. The same pass masks every UUID
    wherever it appears (``run_<UUID>``, ``agent<UUID>``, a chain of UUIDs) and every secret
    that starts inside a masked token, so each secret of a welded chain (``ghp_<a>ghp_<b>``,
    ``AKIA<a>AKIA<b>``, ``bearer <a>bearer <b>``) masks (#1615).

    RESIDUE on this surface, stated because there is no cut to distinguish it: a SECRET welded
    onto an ordinary word before it (``agentghp_<a>``; secrets keep their leading anchor) and
    a welded id that lacks the ``01`` shape are not masked. (An id of any shape that starts
    inside a masked token, or right at its end, is masked, #1617.) The second includes a look-alike
    welded to the word after it (``session_ABCDEF_x``). The pty screen's width-refit trim can
    still happen to cut the ``_x`` away and mask it, so whether such a look-alike shows depends
    on the row's rendered length; a real ``01``-shape id does not. A welded chain of real
    ``01``-shape ids is masked whole (#1612).

    All of these need an attacker-influenced escape from Clauster's own bridge, and the endpoint
    is AUTH-gated. The split case (a control char INSIDE an id) is not a gap: pyte joins the
    halves into one matchable run the anchored pass catches.
    """
    masked = row
    for _ in range(_SCREEN_MAX_SCANS):
        again = _apply_spans(masked, _screen_spans(masked))
        if again == masked:
            break
        masked = again
    else:  # still masking at the cap: a crafted chain, so fail closed (#1612)
        return _render_coverage(row, _fail_closed(row))
    tail = _screen_open_tail_spans(row, cuts=())
    if not tail:
        return masked
    # The NUL-probe fixed point covers the same cells as the rewrite loop above: no core can
    # match a character of `<redacted>`, and NUL and `<`/`>` give the same `\b` and `\s` answer.
    cov = _capped_coverage(row, [(0, len(row))], _screen_spans, max_scans=_SCREEN_MAX_SCANS)
    if all(cov.find(0, s, e) < 0 for s, e, _ in tail):
        return masked  # the open-tail spans add nothing: keep the row exactly as it was
    for s, e, _ in tail:
        cov[s:e] = b"\x01" * (e - s)
    return _render_coverage(row, cov)


def redact_screen_text(rows: list[str]) -> list[str]:
    r"""Redact a rendered terminal screen (already-plaintext cells) row by row.

    The live pty-screen view (#534) feeds pyte-RENDERED rows here, never raw bytes — pyte has
    already consumed every escape sequence, so this does NOT :func:`strip_ansi`. Each row is
    masked by :func:`_redact_screen_row`. Beyond the standalone id/secret masks the old pass
    ran, it also masks a real id a consumed escape welded onto the word before it (the tight
    :data:`_SCREEN_GLUED_ID_RE` shape), so ``agentenv_01<...>`` masks while an ordinary name
    such as ``resolve_session_transcript`` stays readable (#1433). See that helper for what is
    covered and what is residue on this cut-less surface.

    Row COUNT is preserved (the mask runs per row), but a row's LENGTH can change in EITHER
    direction. A mask usually shrinks a match to the ten-character ``<redacted>`` token, but
    :func:`_apply_spans` also replaces a CLIPPED span-piece shorter than the token with the
    full token, so a row can GROW (``Bearer env_01ABCDEF`` -> ``<redacted><redacted>``, one
    longer). Re-fitting each row to the exact terminal width, and re-redacting whatever a trim
    shears, is the caller's job (:meth:`clauster.pty_screen.PtyScreen.frame` and
    :meth:`~clauster.pty_screen.PtyScreen._fit_redacted_row`), not this text-only helper's.

    Best-effort defense-in-depth, like the rest of this module: a novel high-entropy value
    can still slip through (see the ``_SECRET_RES`` note), and this row-at-a-time helper does
    not see a secret that wraps onto the next row -- :func:`redact_wrapped_screen_rows` does,
    for the rows the caller groups. AUTH-gating the pty-screen endpoint is the *primary*
    control; this only narrows the obvious-identifier surface a live screen exposes.
    """
    return [_redact_screen_row(row) for row in rows]


#: The one core that can match across whitespace is ``bearer\s+<value>``, so it is the only
#: one a soft wrap at a SPACE can split between its keyword and its value. A seam whose left
#: side ends in this word keeps one space in :func:`_seam_view`'s ``spaced`` view.
#: ``test_bearer_is_the_only_whitespace_core`` fails if a second such core is added.
#:
#: No leading ``\b``: the text before the word may be a welded neighbour row, so a boundary
#: the screen shows (the row's own indent) can be missing here. A space inserted after a
#: ``bearer`` that was only the tail of a longer word costs nothing, because the plain view
#: still reads that seam with no space.
_SCREEN_BEARER_SEAM_RE = re.compile(r"bearer\Z", re.IGNORECASE)

#: The most scans one soft-wrap view gets in :func:`redact_wrapped_screen_rows` before it fails
#: closed. Ordinary output settles in two or three: one scan per token a freshly masked
#: neighbour exposes, plus one that finds nothing new. Only a crafted chain of tokens welded end
#: to end needs more, one scan each, and a full 40 x 120 screen of them took about 250 ms per
#: view (#1508).
_SEAM_MAX_SCANS = 16


def _screen_seam_spans(text: str, cuts: tuple[int, ...]) -> list[tuple[int, int, str]]:
    r"""Return :func:`_screen_spans` plus every mask anchored at a soft-wrap seam in ``cuts``.

    :func:`_seam_view` drops the layout whitespace at a seam, so the text either side is
    adjacent. That is right when the terminal broke a token mid-way, and wrong when it broke
    at a space: then the seam was a word boundary, and the join has welded two words. A seam
    is therefore treated as a POSSIBLE boundary, exactly as the log path treats the position
    of a removed escape (#1379): the masks are retried starting at each seam
    (:func:`_cut_spans`) and ending at each seam (:func:`_trailing_cut_spans`), and every
    span is unioned. A union can only mask more.

    An id or secret found at a seam is a greedy match too, so it is passed to
    :func:`_screen_welded_uuid_spans` with the anchored ones: a UUID welded onto it loses its
    leading hex group exactly as it does after an anchored match (#1496).
    """
    spans = _screen_spans(text)
    greedy_spans: list[tuple[int, int]] = []
    for anchored, opened, closed, _keeps_prefix, single_run in _MASKS:
        found = _cut_spans(text, cuts, opened, closed, False, single_run)
        found += _trailing_cut_spans(text, cuts, opened, False)
        spans += found
        if anchored is _ID_RE or anchored in _SECRET_RES:
            greedy_spans += [(s, e) for s, e, _ in found]
    spans += _screen_welded_uuid_spans(text, greedy_spans)
    return spans


def _seam_view(
    rows: list[str], soft_seams: list[bool], *, spaced: bool
) -> tuple[str, list[int], tuple[int, ...]]:
    r"""Rebuild the logical line across the soft-wrap seams of ``rows`` (#1508).

    Returns ``(text, cells, cuts)``. ``text`` is the rows joined with the layout whitespace
    removed at every soft seam: the trailing padding of the upper row and the hanging indent
    of the lower one. A hard seam (``soft_seams[k]`` False) joins verbatim, as the join scan in
    :func:`redact_wrapped_screen_rows` does. ``cells[i]`` is the offset of ``text[i]`` in the
    verbatim join of ``rows``, or ``-1`` for an inserted separator. ``cuts`` are the offsets in
    ``text`` where a soft seam joined.

    With ``spaced`` set, a soft seam whose upper side ends in ``bearer`` keeps one space, so a
    ``bearer`` header the terminal wrapped at its internal space still matches with a value
    that ALSO wraps mid-token further on. Every other seam joins with nothing in both views.
    """
    text = ""
    cells: list[int] = []
    cuts: list[int] = []
    offset = 0
    for k, row in enumerate(rows):
        soft_above = k > 0 and soft_seams[k - 1]
        soft_below = k < len(soft_seams) and soft_seams[k]
        start = len(row) - len(row.lstrip()) if soft_above else 0
        end = len(row.rstrip()) if soft_below else len(row)
        text += row[start:end]
        cells.extend(range(offset + start, offset + end))
        offset += len(row)
        if soft_below:
            if spaced and _SCREEN_BEARER_SEAM_RE.search(text[-6:]):
                text += " "
                cells.append(-1)
            cuts.append(len(text))
    return text, cells, tuple(cuts)


def redact_wrapped_screen_rows(
    rows: list[str], *, hard_seams: list[bool], soft_seams: list[bool]
) -> list[str]:
    r"""Redact a wrapped run of screen rows, returning one redacted row per input row.

    pyte fills a hard-wrapped row edge-to-edge and continues the text on the next row, so a
    token the wrap breaks becomes two fragments that neither row matches, and it reaches the
    browser unmasked (#1487, safety invariant 4). Per-row redaction, on the other hand, masks
    every WHOLE token a row holds -- including one welded at the row's own edge, because the
    edge supplies the boundary -- so the only gap it leaves is the SPLIT token.

    So this masks BOTH, but as TWO independent fixed points that meet only at render. A single
    shared map would be order-dependent: a greedy joined match could mark a row-local token's head
    before the round that would match it, and a covered cell never un-covers, so the tail would
    leak. Instead:

    * ``row_cov`` scans each row on its OWN edges to a fixed point, over a probe built from
      ``row_cov`` alone. This reproduces :func:`_redact_screen_row` per row exactly, so by
      construction it can never mask LESS than the per-row pass -- including a token welded at the
      row's edge, and one a neighbour's mask exposes.
    * ``join_cov`` scans each hard-wrapped run of rows, joined, to a fixed point, over a probe
      built from ``join_cov`` alone. This is the SPLIT-token catch the wrap needs.

    Each scan uses a LENGTH-PRESERVING probe (a masked cell reads as NUL: it matches no core and
    gives its neighbours the ``\b`` a freshly-masked run exposes). Because neither map feeds the
    other, neither can delete a boundary the other needs. Each row is then rendered from the UNION
    of the two maps -- every masked run becomes ``<redacted>`` inside that row -- so a mask that
    shortens or grows a row never shifts a NEIGHBOUR row, and the caller fits each returned row to
    the fixed width.

    A benign word the wrap splits so a fragment looks like a token (``resolve_`` on one row,
    ``session_transcript`` on the next) is masked by ``row_cov``, exactly as it was before the
    wrap-aware path existed. That is the safe direction: on this surface, not leaking beats
    keeping a look-alike readable (safety invariant 4).

    Each seam between ``rows[k]`` and ``rows[k + 1]`` carries two flags, and at least one is
    normally set. ``hard_seams[k]`` is True when pyte wrapped the row itself (the caller's
    edge-to-edge test); ``join_cov`` joins only the runs of rows these seams connect.
    ``soft_seams[k]`` is True when the seam can be a SOFT wrap: the program in the terminal
    (Claude's TUI) broke the line itself, so the upper row can stop short of the edge and the
    lower row can start after a hanging indent (#1508). A verbatim join puts that whitespace
    inside a token the wrap split, so it cannot match. When any seam is soft, a third map,
    ``seam_cov``, is built from the :func:`_seam_view` views of the logical line (the layout
    whitespace removed), each scanned to its own fixed point with every soft seam as a possible
    boundary (:func:`_screen_seam_spans`). It joins the union at render. With no soft seam the
    result is exactly what the hard-wrap path gave before.

    A fourth map, ``tail_cov``, holds :func:`_screen_open_tail_spans` over each row, each hard
    run and each soft-wrap view: a UUID, secret or real id welded to the word AFTER it, every
    UUID wherever it appears, and each secret of a welded chain (#1615). It is scanned once and
    never fed into a fixed point, and it joins the union at render.

    A row the soft-wrap and open-tail maps add no cell to renders byte for byte as before: a
    row no hard seam touches as :func:`_redact_screen_row` renders it alone, and a row in a hard
    run from the same two maps as before. A row they add cells to masks a superset of the cells
    it masked before, but the rendered string can differ, so the caller's width-refit trim
    (:meth:`clauster.pty_screen.PtyScreen._fit_redacted_row`) may no longer happen to cut it.
    No real ``01``-shape id, UUID or secret depends on that trim any more, because ``tail_cov``
    masks one welded to the word after it (``<UUID>zz``) whatever the row's rendered length.
    An id look-alike without the ``01`` shape still can (see :func:`_redact_screen_row`).

    Every fixed point here is BOUNDED, because it runs in the keeper's PTY drain loop and a
    crafted screen (a long chain of welded ids) needs one full scan per id. Each soft-wrap view
    gets at most :data:`_SEAM_MAX_SCANS` scans, and each row (``row_cov``) and each hard run
    (``join_cov``) gets at most :data:`_SCREEN_MAX_SCANS` (#1612). A view, row or run that has
    not settled by then FAILS CLOSED: every non-space character in it is masked
    (:func:`_capped_coverage`). Falling back to the other maps alone would let a crafted chain
    switch off the catch for a secret beside it. A row or run that settles is masked exactly as
    before the cap, and :func:`_redact_screen_row` fails closed on the same cap, so ``row_cov``
    still reproduces it.
    """
    seams = max(len(rows) - 1, 0)
    if len(hard_seams) != seams or len(soft_seams) != seams:
        raise ValueError(
            f"{len(rows)} rows need {seams} seams, got {len(hard_seams)} hard"
            f" and {len(soft_seams)} soft"
        )
    joined = "".join(rows)
    n = len(joined)
    bounds: list[tuple[int, int]] = []
    offset = 0
    for row in rows:
        bounds.append((offset, offset + len(row)))
        offset += len(row)

    # Each run of rows the hard seams join, as one line: catches a token pyte's wrap SPLIT. The
    # runs are exactly the groups a caller made before soft seams existed, so this map is the
    # one those groups got. A whole-group verbatim join would NOT be: a `bearer` on an earlier
    # row could then take a later row's `bearer` as its value and hide that header (#1508).
    hard_runs: list[tuple[int, int]] = []
    first = 0
    for k in range(len(rows)):
        if k == len(rows) - 1 or not hard_seams[k]:
            if k > first:  # a single row is already `row_cov`
                hard_runs.append((bounds[first][0], bounds[k][1]))
            first = k + 1

    # Each row on its own edges: reproduces the per-row pass exactly.
    row_cov = _capped_coverage(joined, bounds, _screen_spans, max_scans=_SCREEN_MAX_SCANS)
    join_cov = _capped_coverage(joined, hard_runs, _screen_spans, max_scans=_SCREEN_MAX_SCANS)

    # A token welded to the word after it, scanned once and never fed into a fixed point (see
    # `_screen_open_tail_spans`), over each row, each hard run and each soft-wrap view below.
    tail_cov = bytearray(n)
    for lo, hi in bounds + hard_runs:
        for s, e, _ in _screen_open_tail_spans(joined[lo:hi], cuts=()):
            tail_cov[lo + s : lo + e] = b"\x01" * (e - s)

    # The logical line across a soft wrap (#1508), projected back onto the same cells.
    seam_cov = bytearray(n)
    if any(soft_seams):
        views = [_seam_view(rows, soft_seams, spaced=False)]
        spaced_view = _seam_view(rows, soft_seams, spaced=True)
        if spaced_view[0] != views[0][0]:  # no seam ends in `bearer`: the same view twice
            views.append(spaced_view)
        for text, cells, cuts in views:
            scan = functools.partial(_screen_seam_spans, cuts=cuts)
            # Fails closed: a view that does not settle is masked whole, rather than stop masking.
            cov = _capped_coverage(text, [(0, len(text))], scan, max_scans=_SEAM_MAX_SCANS)
            for s, e, _ in _screen_open_tail_spans(text, cuts=cuts):
                cov[s:e] = b"\x01" * (e - s)
            for i, cell in enumerate(cells):
                if cov[i] and cell >= 0:
                    seam_cov[cell] = 1

    out: list[str] = []
    for k, (lo, hi) in enumerate(bounds):
        cov = bytearray(
            row_cov[i] or join_cov[i] or seam_cov[i] or tail_cov[i] for i in range(lo, hi)
        )
        extra = any(cov[i - lo] and not (row_cov[i] or join_cov[i]) for i in range(lo, hi))
        if not extra and not (k and hard_seams[k - 1]) and not (k < seams and hard_seams[k]):
            # A row no hard seam touches was rendered alone before soft seams existed. Unless the
            # soft-wrap or open-tail maps mask more of it, render it exactly that way, token for
            # token: the union render below can merge overlapping masks into fewer tokens.
            out.append(_redact_screen_row(rows[k]))
            continue
        out.append(_render_coverage(joined[lo:hi], cov))
    return out
