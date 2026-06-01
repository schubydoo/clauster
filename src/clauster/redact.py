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

# A conservative set of obvious secret shapes, as defense-in-depth — the bridge
# already prints "[REDACTED]" for most secrets, but never rely on that alone.
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
    """Mask ``env_/session_/cse_`` identifiers while keeping the prefix readable."""
    return _ID_RE.sub(lambda m: f"{m.group(1)}_{_REDACTED}", text)


def redact_secrets(text: str) -> str:
    """Mask obvious secret shapes (API tokens, bearer headers) as defense-in-depth."""
    out = text
    for rx in _SECRET_RES:
        out = rx.sub(_REDACTED, out)
    return out


def sanitize_line(line: str, *, strip_ansi_seq: bool = True) -> str:
    """Full sanitization for one streamed log line."""
    if strip_ansi_seq:
        line = strip_ansi(line)
    return redact_secrets(redact_ids(line))
