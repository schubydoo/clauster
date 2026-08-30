"""Redaction + ANSI stripping for the WebSocket bridge-log stream (feature 6).

Per decision D11 the bridge log is *hybrid*: verbatim on disk, redacted over the
WebSocket. So whatever ``redact_session_url`` is set to, the WS stream always
strips the session/env identifiers (they are effectively bearer credentials —
anyone with ``env_<ULID>`` can open a New Session composer for that bridge).
"""

from __future__ import annotations

import re

# CSI / escape sequences (colors, cursor moves, OSC).
#
# ⚠️ The alternation ORDER is load-bearing. `]` is 0x5D, which falls inside the
# two-character alternative's `\\-_` (0x5C-0x5F) range, and Python alternation is
# ordered first-match-wins — so while that alternative came first it consumed `ESC ]`
# on its own and the OSC alternative was unreachable, leaving every OSC payload
# (terminal title, hyperlink target, clipboard) in the text as readable junk (#1329).
# Both 7-bit terminators are accepted: BEL, and ST (`ESC \`). The 8-bit C1 forms
# (0x9B CSI / 0x9D OSC / 0x9C ST) are deliberately NOT matched. A raw C1 byte from the
# bridge never survives to get here — every path into redact decodes with
# ``errors="replace"`` (logstream.py:118, runner.py:2346), which turns a lone 0x9C into
# U+FFFD — and a deliberately UTF-8-encoded U+009C (`C2 9C` on the wire) that did arrive
# would only keep its payload as plain text, which the id/secret masks below still see.
#
# The OSC body excludes ESC and BEL so an *unterminated* OSC can only scan to the next
# escape rather than to the end of the input, and excludes CR/LF because a real OSC never
# spans a line: without that, `redact_for_disk` (which runs over a multi-line chunk, not a
# single line) would let one stray `ESC ]` swallow every line up to the next BEL and
# delete them from the public log mirror and from `error_detail`. Together they keep the
# pattern linear: every star is followed by a class disjoint from it, so no alternative
# can backtrack into another (see test_strip_ansi_is_linear_on_osc).
_ANSI_RE = re.compile(
    r"\x1B(?:"
    r"\][^\x07\x1B\r\n]*(?:\x07|\x1B\\)"  # OSC … BEL / OSC … ST (never spans a line)
    r"|\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|[@-Z\\-_]"  # two-character escapes (incl. a bare, unterminated `ESC ]`)
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

_REDACTED = "<redacted>"


def strip_ansi(text: str) -> str:
    r"""Remove ANSI/CSI escape sequences (colors, cursor moves, OSC) from ``text``.

    OSC sequences are removed whole — introducer, payload and terminator — in both the
    BEL and 7-bit ST (``ESC \``) terminated forms. An unterminated OSC keeps its payload:
    only the two-character ``ESC ]`` introducer goes, as for any other bare escape.
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


def sanitize_line(line: str, *, strip_ansi_seq: bool = True) -> str:
    r"""Full sanitization for one streamed log line.

    Redaction always runs against an ANSI-stripped view, so escape sequences can
    never split an identifier past the ``\b``-anchored regexes and smuggle a
    secret through. When ``strip_ansi_seq`` is False the colored line is kept in
    the output — but only if it is provably as redacted as the stripped view;
    otherwise the stripped+redacted form is emitted (color sacrificed for safety
    on that one line).
    """
    stripped_safe = redact_secrets(redact_ids(strip_ansi(line)))
    if strip_ansi_seq:
        return stripped_safe
    colored = redact_secrets(redact_ids(line))
    # If stripping the colored result reveals a secret the colored pass missed
    # (ANSI bytes split the identifier), the colored line is unsafe — fall back.
    return colored if strip_ansi(colored) == stripped_safe else stripped_safe


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
    return redact_secrets(redact_ids(strip_ansi(text)))


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
