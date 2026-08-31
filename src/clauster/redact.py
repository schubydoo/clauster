"""Redaction + ANSI stripping for the WebSocket bridge-log stream (feature 6).

Per decision D11 the bridge log is *hybrid*: verbatim on disk, redacted over the
WebSocket. So whatever ``redact_session_url`` is set to, the WS stream always
strips the session/env identifiers (they are effectively bearer credentials —
anyone with ``env_<ULID>`` can open a New Session composer for that bridge).
"""

from __future__ import annotations

import re

# CSI / escape sequences (colors, cursor moves, and the C1 *string* sequences).
#
# ⚠️ The alternation ORDER is load-bearing. Every string-sequence introducer falls inside
# the two-character alternative's own ranges — `]` (0x5D), `^` (0x5E) and `_` (0x5F) inside
# `\\-_` (0x5C-0x5F), `P` (0x50) and `X` (0x58) inside `@-Z` — and Python alternation is
# ordered first-match-wins, so while that alternative came first it consumed the two-byte
# introducer on its own and the string alternative was unreachable, leaving the payload
# (terminal title, hyperlink target, clipboard, device-control/application data) in the text
# as readable junk (#1329 for OSC; #1344 for DCS/SOS/PM/APC, which the OSC fix left behind).
#
# All five accept BOTH 7-bit terminators here: BEL, and ST (`ESC \`). For OSC that is the
# spec; for the other four it is a deliberate OVER-strip, not a claim about terminals — the
# VT500 state machine exits on BEL only from `osc_string`, and pyte 0.8.2 (this project's own
# emulator) likewise treats BEL as payload in DCS/SOS/PM/APC. The cost is real and bounded: a
# literal `ESC P` in log text followed by a BEL LATER ON THE SAME LINE deletes what lies
# between, which the CR/LF exclusion caps at one line. Taken because this module's job is to
# strip, and a payload left readable is the defect being fixed; the same trade was accepted
# for OSC in #1329. Note `strip_ansi` also feeds two NON-streaming readers —
# `extract_authorize_url` (pty_screen) and login_shepherd's `_redact_captured` — where an
# over-strip would cost the login URL / captured code rather than only colour. Accepted: a
# DCS/SOS/PM/APC introducer plus a same-line BEL bracketing a printed authorize URL is not a
# real terminal pattern (those C1 strings carry device/application data, never a printed URL),
# so the streaming-safety win dominates; revisit only if a real login flow emits one.
# The 8-bit C1 forms (0x9B CSI / 0x9D OSC / 0x90 DCS / 0x9C ST …) are
# deliberately NOT matched. A raw C1 byte from the bridge never survives to get here — every
# path into redact decodes with ``errors="replace"`` (logstream.py:118, runner.py:2346), which
# turns a lone 0x9C into U+FFFD — and a deliberately UTF-8-encoded U+009C (`C2 9C` on the wire)
# that did arrive would only keep its payload as plain text, which the id/secret masks below
# still see.
#
# The string body excludes ESC and BEL so an *unterminated* sequence can only scan to the next
# escape rather than to the end of the input, and excludes CR/LF because a real string sequence
# never spans a line: without that, `redact_for_disk` (which runs over a multi-line chunk, not a
# single line) would let one stray `ESC ]` swallow every line up to the next BEL and
# delete them from the public log mirror and from `error_detail`. Together they keep the
# pattern linear: every star is followed by a class disjoint from it, so no alternative
# can backtrack into another (see test_strip_ansi_is_linear_on_osc).
_ANSI_RE = re.compile(
    r"\x1B(?:"
    # OSC / DCS / SOS / PM / APC … BEL|ST — the whole string sequence, never spanning a line.
    r"[\]PX^_][^\x07\x1B\r\n]*(?:\x07|\x1B\\)"
    r"|\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|[@-Z\\-_]"  # two-character escapes (incl. a bare, unterminated string introducer)
    r")"
)

# API identifiers that act as bearer credentials in a URL.
_ID_RE = re.compile(r"\b(env|session|cse)_[A-Za-z0-9]{6,}\b")

# Bare UUIDs (e.g. organization_uuid, bridgeId) — account/instance identifiers
# the bridge prints in full. Not bearer credentials, but we still don't surface
# them over the WS stream (the on-disk log keeps them verbatim per D11).
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

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
_SECRET_RES = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),  # GitHub fine-grained PAT
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),  # GitLab PAT
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),  # OpenAI/Anthropic-style (keys can contain `_`)
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack tokens
    re.compile(r"\bclauster_pat_[A-Za-z0-9_-]{16,}\b"),  # clauster API token (#360)
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{12,}\b"),  # Authorization: Bearer …
)

# The FRAMING bytes of a C1 string sequence — the two-character introducer and both
# terminators — with no payload class, so a substitution keeps the payload and drops only the
# frame. :func:`sanitize_line`'s colored path needs a view of a line in which a payload's word
# boundaries are intact; see the second guard there for why the equality guard cannot do it.
#
# Substituting a space can MANUFACTURE a match the raw line does not contain — most easily
# `bearer\s+…`, the one pattern whose match may hold whitespace. That direction is fail-closed
# by construction: a manufactured match can only cost color (one more fallback to the stripped
# form), never hide anything, because the view is used to REJECT a colored line, never to
# build one.
_SEQ_FRAME_RE = re.compile(r"\x1B[\]PX^_]|\x1B\\|\x07")

_REDACTED = "<redacted>"

# Every shape this module masks, in one tuple, for the splice re-scan below. Kept beside the
# patterns themselves so a shape added above cannot be forgotten here — a miss would be
# silent, since the re-scan simply would not look for it.
_SHAPE_RES = (_ID_RE, _UUID_RE, *_SECRET_RES)


def strip_ansi(text: str) -> str:
    r"""Remove ANSI/CSI escape sequences (colors, cursor moves, string sequences) from ``text``.

    The C1 *string* sequences — OSC (``ESC ]``), DCS (``ESC P``), SOS (``ESC X``), PM
    (``ESC ^``) and APC (``ESC _``) — are removed whole, introducer, payload and terminator,
    in both the BEL and 7-bit ST (``ESC \``) terminated forms. An unterminated one keeps its
    payload: only the two-character introducer goes, as for any other bare escape.
    """
    return _ANSI_RE.sub("", text)


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


def _shape_hits(text: str):
    r"""Yield ``(token, mask)`` for every id / UUID / secret shape found in ``text``.

    ``token`` is the match's last whitespace-separated piece — the whole match for every
    pattern but ``bearer\s+…``, where it is the credential rather than the keyword, and in
    both cases whitespace-free (so a substituted space can never land inside it). The mask is
    ``<prefix>_<redacted>`` for an id and ``<redacted>`` otherwise.
    """
    for rx in _SHAPE_RES:
        for match in rx.finditer(text):
            # `.split()[-1]` is never empty: every shape match is non-whitespace (even the
            # bearer pattern's tail is its 12+-char credential), so no truthiness guard.
            token = match.group(0).split()[-1]
            yield token, (f"{match.group(1)}_{_REDACTED}" if rx is _ID_RE else _REDACTED)


def _scrub_spliced_shapes(cleaned: str, text: str) -> str:
    r"""Mask shapes that survived only because stripping SPLICED two word characters.

    :func:`strip_ansi` removes a sequence outright, which welds the character before its
    introducer onto the character after its terminator. When both are word characters that
    destroys the ``\b`` every pattern here anchors on and the mask silently stops applying:
    ``"user\x1bPx\x07env_01ABCDEFGHIJKLMNOP"`` collapses to ``userenv_01ABCDEFGHIJKLMNOP``
    and the identifier streams bare. It bites the id, UUID and secret shapes alike.

    Deleting is still the right OUTPUT — it is what rejoins an identifier a color escape
    deliberately split, the older attack this module defends against and one an either/or
    choice of view would reopen. So the fix is a second VIEW, not a different output: re-scan
    the line with each sequence replaced by a space, and mask anything only that view can see.

    The token replaced is the match's last whitespace-separated piece. For every pattern but
    ``bearer\s+…`` that is the whole match; for that one it is the credential rather than the
    keyword. Either way it is whitespace-free, so it cannot contain a substituted space and
    therefore appears verbatim in the deleted view even when the match around it does not.
    As at :data:`_SEQ_FRAME_RE`, a space can manufacture a match the delivered text lacks —
    which can only mask more, never less.
    """
    if "\x1b" not in text:  # nothing was removed, so nothing can have been spliced
        return cleaned
    # The gate is EXACTLY "is this shape's token still present RAW in `cleaned`?" — i.e. did
    # the first pass miss it because stripping welded it past its `\b`. That predicate is not
    # negotiable: a COUNT of space-view vs delete-view matches is NOT a valid substitute,
    # because the two views tally DIFFERENT occurrences — a string-sequence splice hides a
    # token from the delete view while a colour escape mid-token REJOINS one there, so the
    # counts can tie while a genuinely welded copy still sits raw in `cleaned` (a leak).
    # The only safe economy is to DEDUPE the space-view tokens: `str.replace` already masks
    # every occurrence, so the `in cleaned` scan need run at most once per DISTINCT token
    # rather than once per occurrence — which is what keeps `redact_for_disk`, run over the
    # whole multi-line bridge log each poll, off an O(occurrences x len) scan-per-shape.
    seen: dict[str, str] = {}
    for tok, mask in _shape_hits(_ANSI_RE.sub(" ", text)):
        seen.setdefault(tok, mask)
    for tok, mask in seen.items():
        if tok in cleaned:
            cleaned = cleaned.replace(tok, mask)
    return cleaned


def redact_stripped(text: str) -> str:
    r"""Strip ANSI, then mask ids and secrets — the safety order every egress path shares.

    The single implementation of "redact against an ANSI-stripped view", used by
    :func:`sanitize_line`, :func:`redact_for_disk` and ``logging_config``'s formatters. It was
    duplicated as a one-liner in all three until :func:`_scrub_spliced_shapes` had to be added
    behind it; a fourth copy would have been a silent hole rather than a style problem.
    """
    return _scrub_spliced_shapes(redact_secrets(redact_ids(strip_ansi(text))), text)


def sanitize_line(line: str, *, strip_ansi_seq: bool = True) -> str:
    r"""Full sanitization for one streamed log line.

    Redaction always runs against an ANSI-stripped view, so escape sequences can
    never split an identifier past the ``\b``-anchored regexes and smuggle a
    secret through. When ``strip_ansi_seq`` is False the colored line is kept in
    the output — but only if it clears BOTH guards below: it must be as redacted as
    the stripped view, AND carry nothing unmasked inside a string-sequence payload,
    which the first guard structurally cannot see. Otherwise the stripped+redacted
    form is emitted (color sacrificed for safety on that one line).
    """
    stripped_safe = redact_stripped(line)
    if strip_ansi_seq:
        return stripped_safe
    colored = redact_secrets(redact_ids(line))
    # If stripping the colored result reveals a secret the colored pass missed
    # (ANSI bytes split the identifier), the colored line is unsafe — fall back.
    if strip_ansi(colored) != stripped_safe:
        return stripped_safe
    # That comparison is blind INSIDE a string sequence: strip_ansi deletes the whole
    # sequence on both sides, so the two agree whatever the payload holds, and the colored
    # pass above is the only thing masking it there. That pass fails exactly when the
    # introducer is a WORD character — `ESC P`/`ESC X`/`ESC _` kill the `\b` the id and
    # secret patterns anchor on, while `ESC ]`/`ESC ^` (both non-word) leave it firing. So
    # re-check over a view that KEEPS the payload and neutralizes only the framing: a bare
    # id inside a DCS/SOS/APC payload then falls back to the stripped form instead of being
    # streamed verbatim (#1344 — the four introducers added there made this reachable).
    peeled = _SEQ_FRAME_RE.sub(" ", colored)
    return colored if redact_secrets(redact_ids(peeled)) == peeled else stripped_safe


def redact_for_disk(text: str) -> str:
    r"""Redact a multi-line chunk of bridge/agent text for at-rest storage and other egress.

    Named for its original caller — the ``logs.redact_session_url`` on-disk mirror, where the
    bridge writes a verbatim private parse-source (which Clauster still reads for readiness
    markers + the session-URL deep-link recovery) and this produces the public copy. It is now
    also the general chunk-at-a-time redactor for text leaving over the API/WS rather than
    line-by-line (clone-job errors, ``instance.error_detail``, agent result text), so a change
    here is NOT confined to the disk mirror.

    Unlike :func:`sanitize_line` it works over a multi-line chunk, but applies the same safety
    order — strip ANSI first so an escape sequence can never split an ``env_/session_/cse_`` id
    (or a secret) past the ``\b``-anchored regexes — so the output never carries a
    bearer-equivalent session/env identifier or a listed secret shape (the ``_SECRET_RES``
    allow-list note above bounds what "secret" covers).
    """
    return redact_stripped(text)


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
