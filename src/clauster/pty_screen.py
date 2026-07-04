"""Server-side terminal emulation for the read-only live pty-screen view (#534).

The pty keeper feeds the SAME raw byte chunks it already reads off the PTY master into a
:class:`PtyScreen`, which renders them through a ``pyte`` Screen and emits *redacted,
cells-only* frames (plaintext rows + cursor) for the ``/ws/pty-screen`` WebSocket. The
browser reconstructs the screen from those cells via the xterm.js API — the raw ANSI is
never sent, so OSC title/hyperlink/clipboard sequences can't re-leak through the client.

Two constraints, locked for the v1 read-only view:

- **Cells, never raw ANSI.** :meth:`PtyScreen.frame` returns rendered text + cursor only.
- **The terminal title is never serialized.** A frame carries no ``title`` / ``icon_name``
  (OSC 0/1/2 are a data-exfiltration channel); pyte may track ``screen.title`` internally
  but it never leaves this module.

``pyte`` is an OPTIONAL dependency (the ``pty`` extra). It is LGPL-licensed, so it is kept
out of the default install and the Apache-licensed standalone binary; it is imported
lazily here so importing this module — or running the app without the extra — never fails.

:mod:`login_shepherd` (#839/#846) reuses this same emulator for its ``claude setup-token``
flow, which is a full TUI that prints nothing on a plain pipe and only renders its authorize
URL under a real terminal — :meth:`PtyScreen.find_authorize_url` and
:meth:`PtyScreen.find_oauth_token` scan the reassembled screen the same way
:meth:`find_session_id` does for the pty bridge's connect URL. The shared URL/token
SELECTION logic (:func:`extract_authorize_url`, :func:`extract_oauth_token`) lives in this
module rather than ``login_shepherd`` so `login_shepherd` can import `PtyScreen` without an
import cycle.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import redact
from .redact import redact_screen_text

# Fixed v1 geometry. The keeper renders at this size and the client matches it; a
# resize/negotiation path is out of scope for the read-only first cut (#534).
SCREEN_COLS = 120
SCREEN_ROWS = 40

# The pty bridge's connect URL, as a session path (the flag form, not the subcommand's
# `?environment=` query form). The keeper's raw-bytes scrape uses the same pattern on the
# undecoded stream (`pty_keeper._RE_CONNECT_URL`); this str variant runs over the
# pyte-reassembled screen, where the URL is whole even when the raw stream fragments it
# with cursor-positioning escapes at the TUI winsize (#665).
_RE_CONNECT_URL = re.compile(r"https?://claude\.ai/code/(session_[A-Za-z0-9]+)")

# ---------------------------------------------------------------------------
# Shared authorize-URL / token selection (login_shepherd #839, #846).
#
# `login_shepherd`'s plain-pipe `claude auth login` path and the pty-backed
# `claude setup-token` path (see :meth:`PtyScreen.find_authorize_url` below)
# both need to pick the right authorize URL out of noisy CLI output, so the
# selection logic lives HERE (not in `login_shepherd`) and `login_shepherd`
# imports it — the reverse direction would create an import cycle, since
# `login_shepherd` already needs `PtyScreen` for the setup-token PTY spawn.

# The authorize URL `claude auth login`/`setup-token` prints for the operator to open.
# Deliberately greedy-then-trimmed: grab the whole https run, then `_clean_url` strips
# trailing punctuation/quotes the CLI may print around it (see `extract_authorize_url`).
_URL_RE = re.compile(r"https://\S+")

# Host suffixes that identify a genuine Claude/Anthropic OAuth authorize URL. Used to
# PREFER the real authorize link when the CLI prints more than one https URL (e.g. a docs
# link first). Suffix-matched (`endswith`) so subdomains like `console.anthropic.com`
# count. Not a hard requirement — a no-match falls back to the LAST https URL rather than
# failing. `claude.com` is where real `claude auth login` prints its authorize URL
# (live-verified against claude 2.1.200 on 2026-07-03: the URL is on `claude.com` with a
# `platform.claude.com` redirect_uri); `claude.ai` / `anthropic.com` are kept as
# additional/older hosts since the CLI's host is version-coupled and unpinned.
_KNOWN_AUTH_HOST_SUFFIXES = ("claude.ai", "claude.com", "anthropic.com")

# Subdomain prefixes that are documentation/marketing hosts, NOT auth endpoints — even
# on an otherwise-known parent domain (e.g. `docs.anthropic.com` is a subdomain of
# `anthropic.com` but is a docs page, not an authorize URL). Excluded so a help/docs link
# printed before the real authorize URL can't be mistaken for the auth host.
_NON_AUTH_HOST_PREFIXES = ("docs.", "help.", "support.", "www.")

# Trailing characters the CLI may print immediately after a URL (sentence punctuation,
# closing brackets/quotes) that are not part of the URL itself.
_URL_TRAILING = ".,;:!?)]}>\"'"

# `setup-token`'s printed long-lived credential (per the spike: `CLAUDE_CODE_OAUTH_TOKEN=...`).
# Matched permissively (any non-whitespace value) since the exact real-binary format is
# unverified — this is defensive, not assumed exact.
_TOKEN_RE = re.compile(r"CLAUDE_CODE_OAUTH_TOKEN=(\S+)")


def _unwrap_display(display: list[str]) -> str:
    r"""Reassemble ``display`` rows into text, joining hard-wrapped continuation lines.

    Defense in depth for `find_authorize_url`/`find_oauth_token` (#846 follow-up): the
    `setup-token` PTY is now sized wide enough (see `login_shepherd._LOGIN_PTY_COLS`) that
    its ~450-char authorize URL should never wrap in practice, but this makes the scan
    correct at ANY pty width, in case a URL ever exceeds it.

    Each pyte `display` row is padded to exactly ``cols`` characters (trailing spaces).
    When a terminal autowraps a long logical line, the wrapped row is filled edge-to-edge
    with real content — its LAST column is a non-space character — and the continuation
    resumes at the start of the next row with no separator of its own. A row that did NOT
    fill the width (its last character is a space, i.e. the terminal stopped short of the
    edge) ends a logical line, so a newline belongs there. This lets a caller join rows
    without a separator exactly where pyte's autowrap split them, and with ``\n`` everywhere
    else — recovering the original logical line before scanning it for a URL/token.

    Not perfect (a logical line that happens to end exactly at the last column with real
    content is indistinguishable from a wrap), but strictly better than always joining with
    ``\n``, which is what silently truncated the authorize URL in the first place.
    """
    pieces: list[str] = []
    for row in display:
        pieces.append(row)
        wrapped = bool(row) and not row[-1].isspace()
        if not wrapped:
            pieces.append("\n")
    return "".join(pieces)


def _clean_url(url: str) -> str:
    """Trim trailing sentence punctuation / closing quotes off a matched URL token."""
    return url.rstrip(_URL_TRAILING)


def _url_host(url: str) -> str:
    """Return the lowercased host of ``url`` (empty string if unparseable)."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:  # pragma: no cover - urlsplit is very lenient; defensive only
        return ""


def _is_known_auth_host(host: str) -> bool:
    """Whether ``host`` is a known Claude/Anthropic *auth* host (not a docs/marketing one).

    A host on a known parent domain (``claude.ai`` / ``claude.com`` / ``anthropic.com``,
    incl. subdomains like ``console.anthropic.com``) qualifies — EXCEPT documentation/
    marketing subdomains (``docs.``/``help.``/``support.``/``www.``), which are pages an
    operator would land on but never receive an OAuth code from. Excluding them stops a docs
    link printed before the real authorize URL from being mistaken for the auth endpoint.
    Real ``claude auth login`` authorizes on ``claude.com`` (live-verified 2026-07-03).
    """
    if host.startswith(_NON_AUTH_HOST_PREFIXES):
        return False
    return any(
        host == suffix or host.endswith("." + suffix) for suffix in _KNOWN_AUTH_HOST_SUFFIXES
    )


def extract_authorize_url(output: str) -> str | None:
    """Return the best authorize URL in ``output``, or None if none is present.

    Robust against the ways a real CLI's terminal output can mangle a URL:

    * ANSI/terminal escape sequences are stripped first (via :func:`redact.strip_ansi`)
      so a colored/reset-wrapped URL isn't polluted with escape bytes.
    * Each matched ``https://…`` token is trimmed of trailing punctuation/quotes.
    * When several https URLs are present, the LAST one whose host is a known Claude/
      Anthropic auth host (docs/marketing subdomains excluded — see
      :func:`_is_known_auth_host`) wins: a decoy docs/help link printed *before* the
      real authorize URL can't hijack the operator, and — since the CLI prints the actionable
      link after any preamble — the last known-host match is the authorize link. With no
      known-auth-host match it falls back to the *last* https URL overall.

    Deliberately defensive, not host-locked: the real-CLI format is unverified, so a URL
    on an unknown host is still returned rather than rejected.

    Shared by :mod:`login_shepherd`'s plain-pipe ``login`` reader and
    :meth:`PtyScreen.find_authorize_url` (the pty-backed ``setup-token`` reader) so the
    two never drift onto different selection rules.
    """
    cleaned_output = redact.strip_ansi(output)
    candidates = [_clean_url(m.group(0)) for m in _URL_RE.finditer(cleaned_output)]
    candidates = [c for c in candidates if c]
    if not candidates:
        return None
    known = [url for url in candidates if _is_known_auth_host(_url_host(url))]
    if known:
        return known[-1]
    return candidates[-1]


def extract_oauth_token(output: str) -> str | None:
    """Return ``setup-token``'s printed ``CLAUDE_CODE_OAUTH_TOKEN=...`` value, or None.

    Shared by :mod:`login_shepherd` so both the plain-pipe capture and the pty-rendered
    screen scrape the token with the exact same pattern.
    """
    match = _TOKEN_RE.search(output)
    return match.group(1) if match else None


# Opt-in escape hatch for the standalone (frozen) binary (#699). The binary deliberately
# omits LGPL ``pyte`` and ignores system site-packages / PYTHONPATH, so a side ``pip
# install pyte`` is otherwise invisible. Pointing this env var at a directory that holds an
# installed ``pyte`` lets a binary user enable the live terminal view without bundling any
# LGPL code into the Apache-licensed binary.
PYTE_PATH_ENV = "CLAUSTER_PYTE_PATH"


def screen_sidecar_path(log_path: Path) -> Path:
    """Return the screen-sidecar path beside a bridge ``log_path`` (``<stem>.screen.json``).

    The single source of truth for the live-screen sidecar's name, shared by the keeper-
    spawn path (the writer, in ``runner``) and the ``/ws/pty-screen`` reader so the two can
    never drift onto different filenames.
    """
    return log_path.with_name(log_path.stem + ".screen.json")


def read_screen_sidecar(path: Path) -> dict[str, Any] | None:
    """Read the keeper's screen-sidecar JSON, or None if absent/unreadable/malformed.

    The polling counterpart to the keeper's atomic ``os.replace`` writes, and best-effort
    in the same spirit: a missing file (keeper not up yet), a transient read error, or
    malformed JSON all map to None so the reader simply waits for the next frame instead of
    tearing down the live stream. A non-object payload is rejected the same way.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class PyteUnavailableError(RuntimeError):
    """Raised when the live pty-screen view is used without the optional ``pty`` extra.

    ``pyte`` (LGPL) is optional and not bundled in the default install/binary; install
    ``clauster[pty]`` to enable the live terminal view.
    """


def _pyte_unavailable_message() -> str:
    """Return a frozen-binary-aware explanation for the absent ``pyte`` dependency.

    On the standalone PyInstaller binary ``pyte`` is not bundled (LGPL-licensed) and a side
    ``pip``/``uv`` install lands in an environment the frozen binary never reads, so plain
    ``install clauster[pty]`` is a dead end. Name the two working paths instead: point
    :data:`PYTE_PATH_ENV` at a directory holding an installed ``pyte``, or run clauster from
    a ``pip``/``uv`` install with the ``[pty]`` extra.
    """
    if getattr(sys, "frozen", False):
        return (
            "the live terminal view is unavailable in the standalone binary: 'pyte' is "
            "LGPL-licensed and is not bundled. To enable it, either set the "
            f"{PYTE_PATH_ENV} environment variable to a directory containing an installed "
            "'pyte', or run clauster from a pip/uv install with the extra instead: "
            "pip install 'clauster[pty]'."
        )
    return "the live pty-screen view needs the optional 'pyte' dependency; install clauster[pty]"


def _maybe_add_external_pyte_path() -> None:
    """Append an opt-in external ``pyte`` directory to ``sys.path`` (frozen binary only).

    Honor :data:`PYTE_PATH_ENV` so a standalone-binary user can enable the live terminal
    view from a side ``pip install pyte`` (#699). Do nothing unless running frozen, since a
    normal install resolves ``pyte`` through the usual import machinery. Read the env var,
    expand ``~``, and require an existing directory; a non-directory value, an
    :class:`OSError`, or a :class:`RuntimeError` (``expanduser`` raises this when no home
    directory can be resolved — e.g. a stripped container with no ``HOME``) is swallowed
    (fail-closed — never raise from the shim). APPEND, never prepend, so a bundled module
    always wins and the external copy is consulted only when no bundled ``pyte`` exists. Skip
    the append if the path is already on ``sys.path``.
    """
    if not getattr(sys, "frozen", False):
        return
    raw = os.environ.get(PYTE_PATH_ENV, "").strip()
    if not raw:
        return
    try:
        resolved = Path(raw).expanduser()
        if not resolved.is_dir():
            return
        path_str = str(resolved)
    except (OSError, RuntimeError):
        return
    if path_str not in sys.path:
        sys.path.append(path_str)


def _import_pyte() -> Any:
    """Import ``pyte`` lazily, raising a clear error when the ``pty`` extra is absent.

    Try a plain import first; on failure, consult the opt-in external-path shim
    (:func:`_maybe_add_external_pyte_path`, frozen binary only) and retry once before
    raising :class:`PyteUnavailableError`. The retry catches any exception, not just
    :class:`ImportError`: a user-pointed external ``pyte`` can be malformed (a corrupted
    install raising ``SyntaxError`` etc.), and that must still surface as the helpful
    :class:`PyteUnavailableError`, never an opaque traceback.
    """
    try:
        import pyte
    except ImportError:
        _maybe_add_external_pyte_path()
        try:
            import pyte
        except Exception as exc:  # noqa: BLE001 — a broken external pyte must fail closed
            raise PyteUnavailableError(_pyte_unavailable_message()) from exc
    return pyte


class PtyScreen:
    """A pyte-backed terminal emulator that renders raw pty bytes into redacted cells.

    Pure (no I/O): :meth:`feed` consumes raw byte chunks and :meth:`frame` returns the
    current screen as a redacted, cells-only snapshot. Lazily imports ``pyte`` so the
    module is importable without the optional ``pty`` extra.
    """

    def __init__(self, cols: int = SCREEN_COLS, rows: int = SCREEN_ROWS) -> None:
        """Build the emulator at a fixed ``cols`` x ``rows`` geometry (raises if no pyte)."""
        pyte = _import_pyte()
        self.cols = cols
        self.rows = rows
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)

    def feed(self, data: bytes) -> None:
        """Feed a chunk of raw pty bytes into the emulator (escape sequences consumed here)."""
        self._stream.feed(data)

    def find_session_id(self) -> str | None:
        """Scan the reassembled screen for the bridge's ``session_<id>``, or None.

        The pty bridge prints its connect URL (``https://claude.ai/code/session_<id>``) with
        cursor-addressed positioning that fragments the raw PTY byte stream at the TUI winsize
        — the host even loses the literal ``code`` — so the keeper's raw-bytes regex misses it
        (#665). pyte honors that positioning, so the logical URL line is whole here and the id
        scrapes cleanly. Returns the id UN-redacted: the keeper writes it only to its private
        discovery sidecar (never a streamed frame), exactly like the raw-bytes scrape it backs
        up; :meth:`frame` stays redacted. Scanning the rendered ``display`` (not a redacted
        copy) is what makes the match possible — a redacted frame's mask token carries no
        ``session_`` prefix, so it could never yield the real id anyway.
        """
        match = _RE_CONNECT_URL.search("\n".join(self._screen.display))
        return match.group(1) if match else None

    def find_authorize_url(self) -> str | None:
        r"""Scan the reassembled screen for the best OAuth authorize URL, or None.

        ``claude setup-token`` is a full TUI: it prints nothing over a plain pipe (verified:
        1 byte in 12s) and only renders its ``https://claude.com/cai/oauth/authorize?...``
        link under a real terminal (#846). Feeding its raw pty bytes through this emulator
        reassembles the logical screen text exactly like :meth:`find_session_id` does for the
        pty-bridge connect URL, so the same reassembled-then-selected approach applies here —
        :func:`extract_authorize_url` (the shared claude.com/known-host preference,
        docs-decoy exclusion, last-match selection also used by `login_shepherd`'s plain-pipe
        `login` reader) runs over :func:`_unwrap_display` rather than a naive
        ``"\\n".join(display)`` — the authorize URL is ~450 chars, long enough to hard-wrap
        across several rows at a narrower pty width, and a plain newline join would leave it
        split (and thus truncated at the first row boundary) instead of reassembled whole.
        The pty is sized wide enough that this should rarely matter in practice (see
        `login_shepherd._LOGIN_PTY_COLS`), but `_unwrap_display` makes the scan correct at
        any width.
        """
        return extract_authorize_url(_unwrap_display(self._screen.display))

    def find_oauth_token(self) -> str | None:
        """Scan the reassembled screen for ``setup-token``'s printed OAuth token, or None.

        Same reassembly rationale as :meth:`find_authorize_url`, including the
        :func:`_unwrap_display` hard-wrap reassembly: the token line is only whole in the
        pyte-rendered (and unwrapped) screen, not necessarily the raw byte stream or a
        naive newline-joined display.
        """
        return extract_oauth_token(_unwrap_display(self._screen.display))

    def frame(self) -> dict[str, Any]:
        """Return the current screen as a redacted, cells-only frame.

        Shape: ``{"rows": [<redacted line>, ...], "cursor": {"x": int, "y": int},
        "cols": int, "rows_count": int}``. No raw ANSI and no terminal title ever appear:
        the rows are pyte-rendered plaintext run through :func:`redact_screen_text`.

        Each row is re-fit to exactly ``cols`` characters AFTER redaction — masking can
        shorten or lengthen a row (see :func:`redact_screen_text`), and the client draws a
        fixed ``cols`` x ``rows_count`` grid, so an off-width row would corrupt the
        geometry (a too-long row wraps). Truncation only trims the right edge, so it can
        never expose a redacted span.
        """
        cursor = self._screen.cursor
        redacted = redact_screen_text(list(self._screen.display))
        rows = [row[: self.cols].ljust(self.cols) for row in redacted]
        return {
            "rows": rows,
            "cursor": {"x": cursor.x, "y": cursor.y},
            "cols": self.cols,
            "rows_count": self.rows,
        }
