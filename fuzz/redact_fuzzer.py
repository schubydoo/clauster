"""Atheris fuzz harness for ``redact.sanitize_line``.

``sanitize_line`` runs ANSI-stripping + ID/secret redaction regexes over
untrusted bridge-log lines before they're streamed to the browser. It must never
raise or hang (catastrophic-backtracking / ReDoS) on adversarial input, and the
output must not still contain a bare ``env_``/``session_``/``cse_`` id — a
redaction leak. Both are checked: exceptions/hangs surface as fuzzer crashes, and
the leak property is asserted.

Two leak properties, because the first one alone is structurally blind to the bug
issue 1379 describes. Asserting ``\\b(env|session|cse)_…`` against the OUTPUT can only
see an identifier that still has a word boundary in front of it — but the whole point
of an escape weld is that stripping DELETED that boundary, so a leaked
``agentenv_<ULID>`` sails past it. The second property therefore judges the INPUT: it
models what a browser ``<pre>`` renders (escape sequences and invisible control
characters contribute nothing) and asks whether an identifier is *reachable* there —
its start is a word boundary, or the exact offset where rendering removed something.
Every reachable identifier must be gone from the output.

The render model is restated here rather than imported from :mod:`clauster.redact`, so a
mistake inside the module cannot move both sides of the comparison together. The one
documented residue is excluded by construction: an identifier whose only occurrence
starts mid-token with no cut is text the attacker wrote literally, and stays visible.
"""

import re
import sys

import atheris

with atheris.instrument_imports():
    from clauster import redact

# A surviving bare id in redacted output is a leak. Mirror redact._ID_RE exactly
# (env_/session_/cse_ + {6,} chars) so the harness can't false-negative on a leaked
# 6-7 char id the production matcher would have caught.
_LEAK = re.compile(r"\b(env|session|cse)_[A-Za-z0-9]{6,}\b")

# The same shape with the leading \b dropped: what the reader SEES joined, wherever it sits.
# `_OPEN` drops the trailing \b too, for collecting the input's residue runs.
_BARE = re.compile(r"(env|session|cse)_[A-Za-z0-9]{6,}\b")
_OPEN = re.compile(r"(env|session|cse)_[A-Za-z0-9]{6,}")

#: Mirrors ``redact._REDACTED``. Restated, not imported, for the same reason as the patterns.
_MASK = "<redacted>"

# The render model, restated. ESC-introduced sequences first (an ESC that introduces one
# is part of it), then any invisible character — C0 minus the visible separators TAB/CR/LF,
# DEL, the 8-bit C1 range, AND the Unicode Default_Ignorable_Code_Point set (zero-width,
# bidirectional, variation-selector, joiner, filler and tag code points a `<pre>` shows
# nothing for, #1434). The ID/secret PATTERNS and the ESC alternation are the parts restated
# independently of the module, so a mask-logic bug cannot move both sides together. The
# invisible ranges are the ground truth of what renders as nothing, so they are necessarily the
# same set the module strips — a frozen copy of it, kept in sync by hand. Two tests guard that:
# `test_redact.py::test_invisible_pattern_is_default_ignorable_not_cf` rebuilds the property from
# `unicodedata` and reds if the MODULE table drifts from it, and
# `test_fuzz_harness_smoke.py::test_redact_fuzzer_render_matches_the_module_invisible_set` reds if
# this copy drifts from the module. Without this run the oracle is blind to a zero-width leak
# exactly as it was to the #1370 control split before C0 was added.
_RENDER = re.compile(
    r"\x1B(?:[\]PX^_][^\x07\x1B\r\n]*(?:\x07|\x1B\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])"
    r"|[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F"
    r"\u00ad\u034f\u061c\u115f-\u1160\u17b4-\u17b5\u180b-\u180f"
    r"\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    r"\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)

_WORD = re.compile(r"\w")

# Every identifier starts with one of three literal prefixes, and none of them can overlap
# another, so a plain scan finds EVERY start. Driving the search from the prefixes and
# matching at each is what keeps overlapping occurrences visible: a `finditer` of the whole
# shape returns non-overlapping matches, and a leaked identifier nested inside a longer
# unreachable one would simply not be seen (it hid two real findings during this fix).
_PREFIX = re.compile(r"env_|session_|cse_")


def _render_with_cuts(text: str) -> tuple[str, set[int]]:
    """Return what a ``<pre>`` shows for ``text`` plus the offsets where bytes vanished."""
    parts: list[str] = []
    cuts: set[int] = set()
    pos = kept = 0
    for m in _RENDER.finditer(text):
        parts.append(text[pos : m.start()])
        kept += m.start() - pos
        cuts.add(kept)
        pos = m.end()
    parts.append(text[pos:])
    return "".join(parts), cuts


def _occurrences(text: str, shape: re.Pattern[str]) -> list[re.Match[str]]:
    """Return every identifier occurrence in ``text``, overlapping ones included."""
    found = (shape.match(text, m.start()) for m in _PREFIX.finditer(text))
    return [m for m in found if m is not None]


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    line = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    # Judge what the browser SHOWS, not the bytes: a control character the sanitizer left
    # in place is invisible in a `<pre>`, so asserting on the raw string is exactly what let
    # the split case (issue 1370) hide from this harness.
    out = _render_with_cuts(redact.sanitize_line(line))[0]
    assert not _LEAK.search(out), f"redaction leak: {out!r}"

    # Judge the OUTPUT, and require every identifier still readable in it to be explained
    # by an unreachable occurrence in the input. Keying on the character to the identifier's
    # left, not on the identifier alone, is what keeps this sound: the same literal can
    # appear twice on one line, once reachable and once not, and can also appear inside a
    # longer token that legitimately survives. A welded leak never matches a residue key,
    # because the character to its left is exactly the one the removed escape sat beside.
    #
    # Two allowances, both because redaction MANUFACTURES word boundaries out of its own
    # `<redacted>` token (the same effect `redact_secret_lines_fuzzer` documents):
    #   * residue runs are collected WITHOUT a trailing \b and matched by prefix, since a
    #     mask starting just after an identifier can end it where the input did not;
    #   * an identifier sitting immediately after a mask token is accepted, since the mask
    #     supplied its leading boundary. A genuine weld cannot hide there: a welded
    #     identifier starts at a cut, and one that starts at a cut is masked, not left
    #     standing beside a mask.
    rendered, cuts = _render_with_cuts(line)
    residue: dict[str, list[str]] = {}
    for m in _occurrences(rendered, _OPEN):
        start = m.start()
        if start > 0 and _WORD.match(rendered[start - 1]) and start not in cuts:
            residue.setdefault(rendered[start - 1], []).append(m.group(0))
    for m in _occurrences(out, _BARE):
        start = m.start()
        left = out[start - 1] if start else ""
        if start >= len(_MASK) and out[start - len(_MASK) : start] == _MASK:
            continue
        assert any(run.startswith(m.group(0)) for run in residue.get(left, ())), (
            f"weld/split leak: {m.group(0)!r} after {left!r} survived in {out!r}"
        )

    # Both properties above are STRING properties, and one under-masking shape is invisible
    # to them: when a mask consumes a reachable identifier's leading prefix and leaves its
    # core (`cse_<ULID>` welded so a longer `session_...` mask eats the `cse`, streaming the
    # bare core). The survivor is not identifier-shaped, so no output scan can flag it; only
    # its ORIGINAL position tells you it should have gone. That is a positional property, and
    # it is guarded differentially in tests/test_redact.py
    # (test_reach_skip_never_masks_less_than_anchoring_at_every_cut), not here.


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
