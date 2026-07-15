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
import threading
from collections.abc import Iterable
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

# Path segments that identify a genuine OAuth authorize *endpoint*, matched against the
# URL's PATH only (never the full URL string — a query string can contain the substring
# "authorize" too, e.g. inside a `redirect_uri=...%2Fauthorize` value, without the URL
# itself being an authorize link). The real `claude auth login`/`setup-token` link is
# `/cai/oauth/authorize` (live-verified 2026-07-03); a bare trailing `/authorize` is also
# accepted so a future CLI path change (e.g. dropping the `/cai` prefix) doesn't silently
# stop matching.
_AUTHORIZE_PATH_MARKERS = ("oauth/authorize",)

# `setup-token`'s printed long-lived credential (per the spike: `CLAUDE_CODE_OAUTH_TOKEN=...`).
# Matched permissively (any non-whitespace value) since the exact real-binary format is
# unverified — this is defensive, not assumed exact.
_TOKEN_RE = re.compile(r"CLAUDE_CODE_OAUTH_TOKEN=(\S+)")

# OSC 8 hyperlink: ``ESC ] 8 ; <params> ; <URI> (ST | BEL)``. Under a Windows ConPTY,
# ``claude setup-token`` emits its authorize URL as an OSC 8 hyperlink whose URI lives ONLY
# inside the escape — pyte (0.8.2, empirically verified) drops it entirely, rendering just
# the visible link label, so a display scan returns None. We capture the URI straight from
# the raw byte stream instead. ``<params>`` (e.g. ``id=foo``) is ``:``-separated and carries
# no ``;``, so ``[^;]*`` is a safe match for it; the URI capture stops at the ST (``ESC \``)
# or BEL terminator. The closing sequence ``ESC ] 8 ; ; ST`` carries an empty URI (skipped).
_OSC8_RE = re.compile(rb"\x1b\]8;[^;]*;([^\x1b\x07]*)(?:\x1b\\|\x07)")

# Bound on the raw-byte tail carried between :meth:`PtyScreen.feed` calls so a hyperlink
# split across a chunk boundary still matches — comfortably larger than the ~450-char
# authorize URL plus escape overhead, and a hard cap against an unterminated escape (e.g.
# the ConPTY output flood) growing the carry without bound.
_OSC8_MAX_CARRY = 4096

# Cap on retained OSC 8 URIs. Selection is "last wins", so a small tail suffices; the cap
# bounds memory on the login screen and is belt-and-suspenders for the keeper (which does
# not capture at all — see ``PtyScreen(capture_osc8=…)``).
_OSC8_MAX_URLS = 64


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
    except ValueError:
        return ""


def _url_path(url: str) -> str:
    """Return the (unlowered) path of ``url`` (empty string if unparseable).

    Deliberately the PATH component only, never the query string — see
    :data:`_AUTHORIZE_PATH_MARKERS` for why a query-string match would be wrong.
    """
    try:
        return urlsplit(url).path
    except ValueError:
        return ""


def _is_authorize_path(url: str) -> bool:
    """Whether ``url``'s PATH (not its full string) identifies an OAuth authorize endpoint.

    Matches the real ``/cai/oauth/authorize`` path, or any path ending in ``/authorize`` for
    resilience to a CLI path change. Checked against :func:`_url_path` only — a query string
    containing the word "authorize" (e.g. a ``redirect_uri`` value) must NOT count, or a
    same-host non-authorize page whose query happens to mention "authorize" would be
    mistaken for the real link.
    """
    path = _url_path(url)
    return any(marker in path for marker in _AUTHORIZE_PATH_MARKERS) or path.endswith("/authorize")


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
    * Selection among several candidate https URLs, in priority order:

      1. **Authorize-endpoint match** (:func:`_is_authorize_path`, checked against the URL's
         PATH only, never the full string): candidates whose path is the real OAuth
         authorize endpoint (``oauth/authorize``, or any path ending in ``/authorize``).
         This uniquely identifies the actionable link even when the CLI later prints a
         same-host non-authorize URL (an account/settings/status page, or a
         ``platform.claude.com`` redirect target) — a "same-host decoy" that a host-only
         check can't distinguish from the real link. Among authorize-path matches, one on a
         known auth host is preferred; the *last* remaining candidate wins (the CLI prints
         the actionable link after any preamble).
      2. **Known-auth-host fallback**: when no candidate's path matches an authorize
         endpoint, the LAST candidate whose host is a known Claude/Anthropic auth host
         (docs/marketing subdomains excluded — see :func:`_is_known_auth_host`) wins: a
         decoy docs/help link printed *before* the real authorize URL can't hijack the
         operator.
      3. **Last candidate overall**: with no known-auth-host match either, fall back to the
         *last* https URL overall.

    Deliberately defensive, not host-locked: the real-CLI format is unverified, so a URL
    on an unknown host is still returned rather than rejected.

    Shared by :mod:`login_shepherd`'s plain-pipe ``login`` reader and
    :meth:`PtyScreen.find_authorize_url` (the pty-backed ``setup-token`` reader) so the
    two never drift onto different selection rules.
    """
    cleaned_output = redact.strip_ansi(output)
    return _select_authorize_url(m.group(0) for m in _URL_RE.finditer(cleaned_output))


def _select_authorize_url(candidates: Iterable[str]) -> str | None:
    """Pick the best authorize URL from already-extracted candidate URLs, or None.

    The shared selection rule behind BOTH :func:`extract_authorize_url` (the text scan over
    a plain-pipe / rendered-display blob) and :meth:`PtyScreen.find_authorize_url`'s OSC 8
    hyperlink fallback, so the two can never drift onto different rules. Each candidate is
    trimmed of trailing punctuation/quotes; then, in priority order: (1) an authorize-endpoint
    path match (:func:`_is_authorize_path`), preferring a known auth host, last one wins;
    (2) the last known-auth-host candidate; (3) the last candidate overall.
    """
    cleaned = [c for c in (_clean_url(c) for c in candidates) if c]
    if not cleaned:
        return None
    authorize_matches = [url for url in cleaned if _is_authorize_path(url)]
    if authorize_matches:
        known_authorize = [url for url in authorize_matches if _is_known_auth_host(_url_host(url))]
        return (known_authorize or authorize_matches)[-1]
    known = [url for url in cleaned if _is_known_auth_host(_url_host(url))]
    if known:
        return known[-1]
    return cleaned[-1]


def extract_osc8_hyperlinks(data: bytes) -> list[str]:
    """Return the URIs of every complete OSC 8 hyperlink *open*-sequence in ``data``.

    The closing sequence (``ESC ] 8 ; ; ST``) carries an empty URI and is skipped. OSC 8
    URIs are ASCII per spec; a stray non-ASCII byte is replaced rather than raising. Used to
    recover ``claude setup-token``'s authorize URL under a ConPTY, where it is emitted as a
    hyperlink whose target pyte does not render (see :data:`_OSC8_RE`).
    """
    return [uri.decode("ascii", "replace") for m in _OSC8_RE.finditer(data) if (uri := m.group(1))]


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
    ``install clauster[pty]`` is a dead end. Name the working paths instead: the managed
    ``clauster deps install pty`` (the binary bundles pip, #904 slice 2b), or point
    :data:`PYTE_PATH_ENV` at a directory holding an installed ``pyte``.
    """
    if getattr(sys, "frozen", False):
        return (
            "the live terminal view is unavailable in the standalone binary: 'pyte' is "
            "LGPL-licensed and is not bundled. Install it with 'clauster deps install pty', "
            f"or set the {PYTE_PATH_ENV} environment variable to a directory containing an "
            "installed 'pyte'."
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

    **Thread-safe.** ``pyte`` is pure-Python and NOT reentrant, and this object is shared
    across threads: `login_shepherd`'s ``setup-token`` flow feeds from its reader thread
    (:func:`login_shepherd._pump_pty`) while the request thread concurrently scans it via
    :meth:`find_authorize_url` (and the #534 keeper likewise feeds while the WebSocket
    thread reads :meth:`frame`). A single lock serializes every mutate (:meth:`feed`)
    against every read so a scan can never observe a half-applied ``feed`` (an unlocked
    read racing pyte's buffer mutation can raise ``dictionary changed size during
    iteration`` or return a torn row). NB: the lock does not make a scan wait for the
    stream to be *complete* — a read that lands between two feeds still sees a legitimately
    partial screen; guarding against trusting a not-yet-finished authorize URL is the
    caller's job.
    """

    def __init__(
        self, cols: int = SCREEN_COLS, rows: int = SCREEN_ROWS, *, capture_osc8: bool = False
    ) -> None:
        """Build the emulator at a fixed ``cols`` x ``rows`` geometry (raises if no pyte).

        ``capture_osc8`` opts into OSC 8 hyperlink authorize-URL capture (#905), needed ONLY
        by `login_shepherd`'s short-lived ``setup-token`` screen. The long-lived #534 keeper
        screen leaves it off (default): it never reads :meth:`find_authorize_url`, so capturing
        would be dead weight that also accumulates every TUI hyperlink for the bridge lifetime.
        """
        pyte = _import_pyte()
        self.cols = cols
        self.rows = rows
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        # Serializes feed (mutate) against every reader below — pyte is not reentrant and
        # this screen is fed and scanned from different threads. No reader calls feed or
        # another reader, so a plain (non-reentrant) Lock cannot self-deadlock.
        self._lock = threading.Lock()
        # OSC 8 hyperlink capture (ConPTY authorize-URL recovery — see :data:`_OSC8_RE`).
        # pyte drops the hyperlink target, so we scan the raw byte stream in :meth:`feed`.
        # ``_osc8_carry`` holds a bounded tail so a hyperlink split across a chunk boundary
        # still matches; ``_osc8_urls`` retains the last :data:`_OSC8_MAX_URLS` deduped URIs.
        self._capture_osc8 = capture_osc8
        self._osc8_carry = b""
        self._osc8_urls: list[str] = []
        self._osc8_seen: set[str] = set()

    def feed(self, data: bytes) -> None:
        """Feed a chunk of raw pty bytes into the emulator (escape sequences consumed here)."""
        with self._lock:
            self._stream.feed(data)
            if self._capture_osc8:
                self._scan_osc8(data)

    def _scan_osc8(self, data: bytes) -> None:
        """Record OSC 8 hyperlink URIs from ``data`` (caller holds ``self._lock``).

        Scans ``carry + data`` so a hyperlink split across two :meth:`feed` chunks still
        matches, then carries the tail from the last OSC 8 **opener** (``ESC ] 8``) — the only
        sequence being reassembled, so a trailing non-OSC escape can't misdirect the carry —
        or a trailing *partial* opener when the chunk boundary splits the 3-byte marker itself
        (``…ESC`` / ``…ESC ]``), capped at :data:`_OSC8_MAX_CARRY`. ``_osc8_seen`` dedups so no
        URI is double-counted
        even if a completed sequence is re-carried, and ``_osc8_urls`` is FIFO-evicted at
        :data:`_OSC8_MAX_URLS`. Only ``https://`` URIs are retained — parity with the text
        path's ``https``-only :data:`_URL_RE`, keeping a stray TUI hyperlink (``file://``,
        ``vscode://``) from ever surfacing as an authorize URL.
        """
        buf = self._osc8_carry + data
        for url in extract_osc8_hyperlinks(buf):
            if url.startswith("https://") and url not in self._osc8_seen:
                self._osc8_seen.add(url)
                self._osc8_urls.append(url)
                if len(self._osc8_urls) > _OSC8_MAX_URLS:
                    self._osc8_seen.discard(self._osc8_urls.pop(0))
        opener = buf.rfind(b"\x1b]8")
        if opener != -1:
            self._osc8_carry = buf[opener:][-_OSC8_MAX_CARRY:]
        elif buf.endswith(b"\x1b]"):  # chunk boundary landed inside the 3-byte opener
            self._osc8_carry = b"\x1b]"
        elif buf.endswith(b"\x1b"):
            self._osc8_carry = b"\x1b"
        else:
            self._osc8_carry = b""

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
        with self._lock:
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

        Windows ConPTY fallback (#905): `claude setup-token` emits the authorize URL as an
        OSC 8 hyperlink whose target pyte drops from the rendered display, so the display
        scan returns None there. When it does, fall back to the URI captured from the raw
        byte stream (:meth:`_scan_osc8`), run through the SAME :func:`_select_authorize_url`
        rule so the two paths can never disagree.
        """
        with self._lock:
            display = list(self._screen.display)
            osc8_urls = list(self._osc8_urls)
        url = extract_authorize_url(_unwrap_display(display))
        if url is not None:
            return url
        # ConPTY renders the authorize URL as an OSC 8 hyperlink whose target pyte drops from
        # the display (#905); fall back to the URI captured from the raw OSC 8 sequences. An
        # OSC 8 target is HIDDEN from the operator (they see only the link label), so — unlike
        # the visible text path, which the operator can eyeball — a hidden target must clear a
        # STRICTER bar: it is handed back only if it is BOTH on a known Claude/Anthropic auth
        # host AND an authorize-endpoint path. That blocks a hidden decoy on an unknown host
        # AND a hidden non-authorize link on an allowed host (e.g. status./marketing.claude.com,
        # which pass the host check but are not the authorize endpoint). The real link is
        # `claude.com/cai/oauth/authorize`, which clears both, so nothing is lost.
        return _select_authorize_url(
            u for u in osc8_urls if _is_known_auth_host(_url_host(u)) and _is_authorize_path(u)
        )

    def find_oauth_token(self) -> str | None:
        """Scan the reassembled screen for ``setup-token``'s printed OAuth token, or None.

        Same reassembly rationale as :meth:`find_authorize_url`, including the
        :func:`_unwrap_display` hard-wrap reassembly: the token line is only whole in the
        pyte-rendered (and unwrapped) screen, not necessarily the raw byte stream or a
        naive newline-joined display.
        """
        with self._lock:
            display = list(self._screen.display)
        return extract_oauth_token(_unwrap_display(display))

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
        with self._lock:
            cursor_x, cursor_y = self._screen.cursor.x, self._screen.cursor.y
            display = list(self._screen.display)
        redacted = redact_screen_text(display)
        rows = [row[: self.cols].ljust(self.cols) for row in redacted]
        return {
            "rows": rows,
            "cursor": {"x": cursor_x, "y": cursor_y},
            "cols": self.cols,
            "rows_count": self.rows,
        }
