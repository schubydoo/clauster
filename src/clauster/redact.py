"""Redaction + ANSI stripping for the WebSocket bridge-log stream (feature 6).

Per decision D11 the bridge log is *hybrid*: verbatim on disk, redacted over the
WebSocket. So whatever ``redact_session_url`` is set to, the WS stream always
strips the session/env identifiers (they are effectively bearer credentials —
anyone with ``env_<ULID>`` can open a New Session composer for that bridge).
"""

from __future__ import annotations

import re

# CSI / escape sequences (colors, cursor moves, OSC).
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07)")

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
    re.compile(r"\bsk-[A-Za-z0-9-]{16,}\b"),  # OpenAI/Anthropic-style
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack tokens
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{12,}\b"),  # Authorization: Bearer …
)

_REDACTED = "<redacted>"


def strip_ansi(text: str) -> str:
    """Remove ANSI/CSI escape sequences (colors, cursor moves, OSC) from ``text``."""
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
