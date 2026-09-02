"""PTY keeper — the sidecar that owns an Interactive Session bridge's pseudo-terminal.

The ``claude --remote-control`` *flag* form (main command, not the
``remote-control`` subcommand) is the only entrypoint that genuinely restores
prior conversation context on restart (via ``--continue`` / ``--resume``). But
it is an interactive single session: spawned detached with no controlling
terminal it dies within seconds. So Clauster runs it under a real PTY.

This module is that PTY holder. Clauster launches it **detached**
(``start_new_session=True``), exactly like it launches a subcommand bridge, so
the keeper outlives a Clauster restart; the keeper in turn owns the bridge's
controlling terminal, so closing it would hang the bridge up — which is why the
keeper, not Clauster, must hold the master fd. The keeper:

1. opens a PTY and makes the slave its session's controlling terminal,
2. execs the bridge argv (passed after ``--``) with that PTY,
3. writes a JSON *sidecar* file that Clauster polls for the bridge pid and the
   ``claude.ai/code/session_<id>`` connect URL (discovery stays file-based, the
   same shape as the subcommand path's ``--debug-file`` marker parse),
4. drains the master to ``/dev/null`` so the bridge's writes never block, and
5. exits when the bridge exits.

Discovery is file-based and stop is a signal to the bridge pid, so Clauster
needs no socket to the keeper: a restarted Clauster re-reads the sidecar and
stops the bridge with ``SIGINT`` (twice — see :mod:`clauster.runner`). The POSIX
backend uses ``os.openpty``; Windows runs the same detached-keeper model over a
ConPTY pseudo-console (pywinpty) — see :func:`_run_keeper_conpty`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import select
import signal
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import procutil

# pty_screen is first-party and always importable (it lazy-imports the optional `pyte`
# itself, only when a PtyScreen is actually constructed), so importing it here never pulls
# in pyte and never fails for a missing extra.
from .pty_screen import SCREEN_COLS, SCREEN_ROWS, PtyScreen, PyteUnavailableError

# The flag form prints its connect URL as a session path, NOT the subcommand's
# `?environment=env_<ULID>` query form (verified, claude 2.1.159).
_RE_CONNECT_URL = re.compile(rb"https?://claude\.ai/code/(session_[A-Za-z0-9]+)")

# How long to wait for the bridge to print its connect URL before giving up on
# discovery (the bridge stays running regardless; this only bounds URL capture).
_URL_TIMEOUT = 30.0

#: The sidecar's advisory `note` when the URL timeout fires while a screen fault is still in
#: force (#1390). The keeper-path sibling of `login_shepherd._SCREEN_FAULT_NOTE`: a transient
#: fault clears on the next chunk and is only worth a log line, but one that outlives the
#: whole capture window is the reason the operator has no deep link, and a log file nobody
#: has a reason to open is not a report.
#:
#: A `note` is deliberately NOT the sidecar's `error`: `error` means the keeper failed, and
#: the runner reads it only for an ERROR row. This bridge is running fine — the note is an
#: advisory carried alongside a healthy `ready` row.
#:
#: "could not be read" covers BOTH screen faults this drain absorbs — a per-frame render
#: fault (`_scan_session_id`) and an emulator that was disabled outright by a `screen.feed`
#: failure (`feed`) — because they are the same fact to an operator and neither is
#: actionable beyond "the link is gone". The distinction is in `<sidecar>.log`.
#:
#: Fixed text, never interpolated: the exception's own message adds nothing an operator can
#: act on, and keeping the string constant means nothing from the screen can ride out here
#: (invariant 4). It is rendered verbatim on the card, so it is written to be read there.
_SCREEN_FAULT_NOTE = "Connect link unavailable — this session's screen could not be read."


def _worktree_from_argv(bridge_argv: list[str]) -> str | None:
    """Return the ``--worktree <name>`` the bridge was launched with, or ``None`` (#1241).

    A ``spawn_mode="worktree"`` interactive session runs in
    ``<repo>/.claude/worktrees/<name>``, and Clauster derives ``<name>`` from the
    instance_id. A keeper-only reattach (a live keeper found with no row whose identity
    it can be correlated to) mints a FRESH instance_id, so that derivation stops
    describing the worktree actually on disk — a resume would create a second one and
    orphan the first, and the stop-time ``git worktree unlock`` would target a path that
    does not exist. Recording it here makes the name recoverable from the sidecar, which
    is the one artifact that outlives the restart.

    Read off the argv the keeper is about to exec rather than passed as a separate flag:
    the value is definitionally whatever the bridge runs with, so there is no second
    source to disagree with. Returns ``None`` for the flag's absence and for a trailing
    ``--worktree`` with no value (malformed argv is not a name).
    """
    try:
        idx = bridge_argv.index("--worktree")
    except ValueError:
        return None
    return bridge_argv[idx + 1] if idx + 1 < len(bridge_argv) else None


def _write_sidecar(path: Path, data: dict[str, object]) -> None:
    """Atomically write the sidecar JSON so a polling reader never sees a partial file."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Best-effort: a transient write failure must not take the bridge down.
        pass


# Live-screen tap (#534): when a screen-sidecar path is given, the keeper feeds the same
# drained chunks into a server-side terminal emulator (pyte) and republishes a redacted,
# cells-only frame here for the dashboard's read-only live-terminal view. EVERYTHING about
# the tap is best-effort — it must never affect the bridge or the keeper's drain/discovery.
_SCREEN_FLUSH_INTERVAL = 0.25  # seconds; cap how often the screen sidecar is rewritten


def _write_screen_status(path: Path, seq: int, state: str, error: str | None) -> None:
    """Write a screen-sidecar status frame carrying no screen payload (best-effort).

    Used for non-``live`` states (``unavailable``/``error``). A consumer should treat any
    frame whose ``screen`` is null as a status, not a renderable baseline; the ``seq`` of
    a setup-time status is 0 (before any live frame), while a mid-session disable continues
    the monotonic live counter so a later WebSocket can still de-dup/skip-ahead correctly.
    """
    _write_sidecar(path, {"seq": seq, "state": state, "error": error, "screen": None})


def _make_screen(screen_sidecar: Path | None) -> PtyScreen | None:
    """Create the pyte emulator for URL reassembly + the optional live view, or None.

    A pyte screen serves two consumers: the connect-URL scrape (the raw byte stream fragments
    the URL with cursor-positioning escapes at the TUI winsize, #665) and, when
    ``screen_sidecar`` is given, the opt-in redacted live-screen view (#534).

    Returns None when no live view was requested OR ``pyte`` (the optional ``pty`` extra) is
    absent, so the keeper drains with no pyte dependency and scrapes the URL from the raw bytes
    instead. Coupling "build a screen" to ``screen_sidecar`` is deliberate: the pyte screen
    only earns its TUI winsize (what fragments the raw URL) when something consumes the
    reassembly; with the live view off the default winsize already yields a contiguous,
    raw-scrapable URL, so the keeper is left exactly as it was. A missing/failed ``pyte`` is
    recorded to the live-view sidecar (when one was requested) so a viewer can explain the
    dormant view; nothing here raises — screen setup must never take the bridge or keeper down.
    """
    if screen_sidecar is None:
        return None
    try:
        return PtyScreen()
    except PyteUnavailableError as exc:
        _write_screen_status(screen_sidecar, 0, "unavailable", str(exc))
        return None
    except Exception as exc:  # noqa: BLE001 — never let screen setup kill the keeper
        _write_screen_status(screen_sidecar, 0, "error", f"screen init: {exc}")
        return None


def _scan_connect_url(buf: bytes | bytearray, screen: PtyScreen | None) -> str | None:
    """Return the bridge's ``session_<id>`` from its PTY output, or None if not yet seen.

    Two sources, in order: the raw byte stream (a contiguous URL line — the no-pyte / plain
    default-winsize case), then the pyte-reassembled screen, which recovers the URL when the
    TUI winsize makes claude fragment it with cursor-positioning escapes the raw regex can't
    follow (#665). The id is returned UN-redacted for the keeper's private discovery sidecar.
    """
    raw_hit = _RE_CONNECT_URL.search(buf)
    if raw_hit is not None:
        return raw_hit.group(1).decode()
    if screen is not None:
        return screen.find_session_id()
    return None


def _write_screen_frame(path: Path, screen: PtyScreen, seq: int, state: str) -> int:
    """Write the current redacted screen frame to its sidecar (best-effort); return new seq."""
    seq += 1
    payload: dict[str, object] = {"seq": seq, "state": state, "error": None}
    try:
        payload["screen"] = screen.frame()
    except Exception as exc:  # noqa: BLE001 — a render hiccup must never affect the bridge
        payload.update(screen=None, state="error", error=f"screen render: {exc}")
    try:
        _write_sidecar(path, payload)
    except Exception:  # noqa: BLE001,S110 — a serialize/write failure must never crash the keeper
        # _write_sidecar only catches OSError; a non-OSError (e.g. a json.dumps TypeError from
        # a future non-serializable frame) would otherwise escape this best-effort write into
        # the drain loop's unguarded throttle/exit calls and hang the bridge (cardinal invariant).
        pass
    return seq


@dataclass
class _ScreenTap:
    """The active live-screen tap: a pyte emulator paired with its sidecar path.

    Bundling the two correlated non-optionals lets the drain loop hold a single
    ``tap is not None`` invariant — they are set together when the tap turns on and cleared
    together when a feed error disables it — instead of carrying a separate, always-true
    ``sidecar is not None`` guard beside every ``screen is not None`` check.
    """

    screen: PtyScreen
    sidecar: Path


def _proc_start_pair(pid: int) -> tuple[float | None, int | None]:
    """Return the bridge's ``(epoch, boot-relative ticks)`` start pair from ONE /proc read.

    Thin wrapper over :func:`clauster.procutil.proc_start_pair`, which owns the rule and
    the reasoning — derived rather than sampled twice, so a death plus pid recycle between
    two reads cannot leave a pair describing different processes (#1399). Shared with the
    runner's own spawn stamp so the sidecar and the registry cannot disagree about what a
    start pair means.

    Wrapped, because the keeper must never raise out of its startup path: an import or
    psutil failure degrades to the honest unknown rather than aborting the first sidecar
    write.
    """
    try:
        from clauster import procutil

        return procutil.proc_start_pair(pid)
    except Exception:  # noqa: BLE001 — discovery aid only; never fatal
        return None, None


class _KeeperDrain:
    """Shared drain/publish logic for both keeper backends (POSIX pty + Windows ConPTY).

    Each backend owns only its transport I/O and liveness check; it feeds drained bytes
    here, which runs the single copy of the pyte-screen feed, connect-URL scrape, and
    sidecar / live-screen-frame publishing. ``base`` is mutated in place and shared with
    the caller's sidecar dict.
    """

    def __init__(
        self,
        base: dict[str, object],
        sidecar: Path,
        screen: PtyScreen | None,
        screen_sidecar: Path | None,
    ) -> None:
        """Wire the drain to its sidecar and (optional) live-screen tap."""
        self._base = base
        self._sidecar = sidecar
        self._screen = screen
        self._tap: _ScreenTap | None = (
            _ScreenTap(screen, screen_sidecar)
            if screen is not None and screen_sidecar is not None
            else None
        )
        self._seq = 0
        self._dirty = False
        self._last_write = 0.0
        self._buf = bytearray()
        self._url_found = False
        self._screen_scan_failed = False
        # Latched by :meth:`feed` when the emulator itself failed and was disabled, which the
        # render-fault latch above cannot represent: with `_screen` gone the scrape can no
        # longer raise, so the very next chunk CLEARS `_screen_scan_failed` while URL capture
        # stays just as dead (the raw-bytes regex rarely survives a fragmented stream, which
        # is why the screen exists). One-way on purpose — a disabled screen never comes back.
        self._screen_feed_failed = False
        self._deadline = time.monotonic() + _URL_TIMEOUT

    def _scan_session_id(self) -> str | None:
        """Run one connect-URL scrape, degrading a pyte render fault to "no match yet".

        THE reader boundary for this module — the keeper-path sibling of
        `login_shepherd._read_screen` (#1358 / PR #1375, which named the credential path
        only). `pyte`'s ``Screen.display`` raises ``IndexError: string index out of range``
        when a double-width character is left half-overwritten, and a 13-byte stream
        reproduces it (found by ``fuzz/pty_screen_feed_fuzzer.py``).
        :meth:`PtyScreen.find_session_id` goes through ``display``, and
        :meth:`feed` guards only ``screen.feed`` — so that ``IndexError`` escaped the scrape,
        escaped both drain loops (the POSIX one catches ``OSError``/``BlockingIOError``, the
        ConPTY one guards only its read), and killed the keeper. That is worse than losing a
        deep link: :meth:`finish` never runs, so the discovery sidecar is stranded at
        ``starting``/``ready`` while the bridge is orphaned — a lifecycle error collapsing
        into a misleading state (invariant 1).

        Reachability differs by backend, and the ConPTY one is the worse of the two. POSIX
        builds a screen only when a live view was requested (:func:`_make_screen` is gated on
        ``screen_sidecar``), so the fault needed the tap on; :func:`_run_keeper_conpty` builds
        ``PtyScreen()`` unconditionally — precisely because the raw scrape rarely survives
        ConPTY — so on Windows the scrape ran on every chunk of every keeper and the crash
        needed nothing but the wrong bytes. Both are covered here: the two backends share
        this drain.

        The guard is deliberately narrow: only ``IndexError``, and only around the scrape
        helper, so a genuine defect anywhere else still propagates. (It spans
        ``_scan_connect_url`` whole rather than just its screen leg — a compiled regex
        ``search`` over a bytearray cannot raise ``IndexError``, so the wider span costs
        nothing and keeps the call site a single expression.) The degraded value is "no match
        on THIS chunk", not "give up" — ``_buf`` keeps accumulating and the next chunk retries,
        which usually works because the next ``feed`` overwrites the broken cell. The screen
        is NOT disabled, unlike the ``screen.feed`` failure above: a feed failure means the
        emulator itself is unusable, while a render fault is per-frame and transient.

        Never a silent skip: the fault is recorded on the drain and reported once to the
        keeper's stderr, which is captured to ``<sidecar>.log`` — the same file the escaping
        traceback used to land in. Once per *run* of faults, not per chunk: at drain cadence a
        persistent fault would otherwise fill that file, so the flag suppresses the repeat —
        but a scrape that renders cleanly clears it again, or a transient fault in the first
        second of a long-lived keeper would permanently silence a genuinely different one
        later, leaving the log describing a fault that recovered and nothing about the one
        that did not. The text carries no screen content, only the exception's own fixed
        message, so nothing unredacted rides out (invariant 4).
        """
        try:
            session_id = _scan_connect_url(self._buf, self._screen)
        except IndexError as exc:
            if not self._screen_scan_failed:
                self._screen_scan_failed = True
                print(
                    f"clauster.pty_keeper: screen could not be rendered for the connect-URL "
                    f"scrape, retrying on later output: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            return None
        # A clean render, whether or not it found a URL — the next fault is a new one.
        self._screen_scan_failed = False
        return session_id

    def feed(self, chunk: bytes) -> None:
        """Feed one drained chunk into the pyte screen + the connect-URL scrape."""
        if self._screen is not None:
            try:
                self._screen.feed(chunk)
                self._dirty = True
            except Exception as exc:  # noqa: BLE001 — best-effort, never kill the bridge
                # A feed failure disables both screen consumers for the rest of the session:
                # the live view (if any) reports a terminal `error`, and URL extraction falls
                # back to the raw-bytes regex below. The bridge is unaffected.
                if self._tap is not None:
                    self._seq += 1
                    _write_screen_status(
                        self._tap.sidecar, self._seq, "error", f"screen feed: {exc}"
                    )
                    self._tap = None
                self._screen = None
                # Latch it for the URL deadline (#1390). Without a tap this failure had NO
                # reader at all — no stderr line, no sidecar field — while producing exactly
                # the render fault's symptom: a `ready` row with no connect link and nothing
                # saying why. Reported here for the same reason the render fault is: a fault
                # is never a silent skip. Fires at most once: `_screen` is None from here on,
                # so no later chunk can re-enter this arm.
                self._screen_feed_failed = True
                print(
                    f"clauster.pty_keeper: the terminal emulator failed and was disabled; "
                    f"the connect-URL scrape falls back to the raw byte stream: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        if not self._url_found:
            self._buf.extend(chunk)
            session_id = self._scan_session_id()
            if session_id is not None:
                self._base.update(
                    connect_url=f"https://claude.ai/code/{session_id}",
                    session_id=session_id,
                    state="ready",
                )
                _write_sidecar(self._sidecar, self._base)
                self._url_found = True
                self._buf = bytearray()  # keep draining, stop accumulating

    def tick(self) -> None:
        """Between reads: throttle the live-screen frame and handle the URL timeout."""
        if self._tap is not None and self._dirty:
            now = time.monotonic()
            if now - self._last_write >= _SCREEN_FLUSH_INTERVAL:
                self._seq = _write_screen_frame(
                    self._tap.sidecar, self._tap.screen, self._seq, "live"
                )
                self._dirty = False
                self._last_write = now
        if not self._url_found and time.monotonic() > self._deadline:
            # The connect URL never appeared; stop accumulating and promote a still-alive
            # bridge to "ready" (the URL is a deep-link nicety, not a liveness signal — a
            # `--continue` resume or a newer claude build may never re-print it).
            self._url_found = True
            self._buf = bytearray()
            if self._screen_scan_failed or self._screen_feed_failed:
                # A screen fault still in force at the deadline is WHY there is no URL, so
                # record it as an advisory the runner lifts onto the card (#1390) — otherwise
                # the row promotes to `ready` with `connect_url: null` and the operator's
                # only trace is `<sidecar>.log`.
                #
                # BOTH latches, because both end URL capture: `_scan_session_id` clears the
                # render latch on any clean render, so it means "the last scrape of the
                # window could not read the screen", while a feed failure disabled the
                # emulator for good and no later scrape can re-raise to keep the first latch
                # set. Either alone would miss half the fault space.
                #
                # The claim is "the screen could not be read", which is what the latches
                # actually witness — not "and that is provably the only reason". A bridge
                # that fell silent right after a fault cannot be told apart from one whose
                # URL the fault hid, because a re-scrape here would read the same unchanged
                # `_buf` and `_screen` and return the same answer. Over-claiming causation
                # would be the wrong trade only if the note were alarming; it is advisory,
                # and silence is what #1390 is about.
                #
                # Set before the state check, not inside it: `base` is shared with the
                # backend and `finish` republishes it, so the note survives even on the
                # promotion path this `if` skips.
                #
                # Reaching the card also needs `claude.startup_grace_seconds` (default 60)
                # to exceed `_URL_TIMEOUT`; under a shorter configured grace the startup
                # watch marks the row ERROR before this note is ever written.
                self._base["note"] = _SCREEN_FAULT_NOTE
            if self._base.get("state") == "starting":  # pragma: no branch
                self._base["state"] = "ready"
                _write_sidecar(self._sidecar, self._base)

    def finish(self, rc: int) -> None:
        """Publish the terminal `exited` sidecar and a final live-screen frame."""
        self._base.update(state="exited", bridge_exit=rc)
        _write_sidecar(self._sidecar, self._base)
        if self._tap is not None:
            _write_screen_frame(self._tap.sidecar, self._tap.screen, self._seq, "exited")


def _load_pty_process() -> Any:
    """Return pywinpty's ``PtyProcess`` (the Windows ConPTY backend), or raise off-Windows.

    Isolated behind a seam so :func:`_run_keeper_conpty` is testable on POSIX — where the
    win32-only ``pywinpty`` isn't installed — by patching this with a fake. The early
    platform guard also keeps the type checker from resolving the win32-only import on a
    POSIX host.
    """
    if sys.platform != "win32":
        raise RuntimeError("the ConPTY keeper backend is Windows-only")
    from winpty import PtyProcess

    return PtyProcess


def _report_keeper_fault(message: str) -> None:
    """Print a keeper-level fault to stderr, which the launcher captures to ``<sidecar>.log``.

    The sidecar's ``error`` field alone is NOT a report. The runner reads it only inside the
    spawn-readiness window (``_await_ready_pty`` -> ``error_detail``); once a bridge is RUNNING
    nothing reads it again, so a mid-session ConPTY fault recorded only there would be silent —
    and this module's rule (see :meth:`_KeeperDrain._scan_session_id`) is that a fault is never
    a silent skip. This is the same channel the escaping traceback used to land in, which is
    what made the pre-#1389 crash at least diagnosable.

    Carries only Clauster's own text plus the exception's message. Screen/stream content never
    reaches here, so nothing unredacted rides out (invariant 4).
    """
    print(f"clauster.pty_keeper: {message}", file=sys.stderr, flush=True)


def _conpty_alive(proc: Any) -> tuple[bool, str | None]:
    """Report whether the ConPTY bridge is still running, reading a raise as "assume dead".

    ``isalive()`` is the exit-vs-idle decision on every empty read in
    :func:`_run_keeper_conpty`, and pywinpty raises out of it on a process handle it can
    no longer query. Unguarded, that escaped the drain loop and skipped
    :meth:`_KeeperDrain.finish`, so the discovery sidecar stayed at ``starting``/``ready``
    while the bridge was orphaned — a lifecycle error collapsing into a misleading state
    (invariant 1). Assuming dead is the fail-closed answer: the loop leaves by its ordinary
    exit, which terminates the bridge and publishes the terminal sidecar with the reason on
    it, rather than spinning on a handle that can no longer be read.

    Deliberately no retry: a fault here is not distinguishable from a genuinely gone handle,
    and treating "unsure" as alive is the open-failing half of the choice — it keeps the loop
    polling a bridge nobody can drive while the sidecar still reads live. A session lost to a
    transient fault is recoverable (``claude --continue``); a stranded card is not.

    Returns ``(alive, error)``. A non-None ``error`` ALWAYS comes with ``alive=False`` — the
    caller relies on that to break out of the loop, so never add a ``(True, warning)`` case.
    """
    try:
        return bool(proc.isalive()), None
    except Exception as exc:  # noqa: BLE001 — any pywinpty fault means "cannot observe"
        return False, f"conpty liveness check failed: {exc}"


def _run_keeper_conpty(
    bridge_argv: list[str],
    sidecar: Path,
    cwd: str | None,
    screen_sidecar: Path | None,
) -> int:
    """Windows ConPTY backend for :func:`run_keeper` (the analogue of its POSIX pty path).

    Spawns the bridge on a ConPTY pseudo-console via pywinpty, then drains it through the
    shared :class:`_KeeperDrain`. ConPTY always fragments output with cursor escapes (like
    the POSIX TUI-winsize case), so a pyte screen is built unconditionally for URL
    reassembly when pyte is present — the raw-byte scrape rarely survives ConPTY alone.
    Setup, spawn and reap failures are recorded in the sidecar and returned as a keeper code
    (71-73), never raised. The code is a hint only — a bridge may exit 73 itself, and nothing
    consumes either it or the sidecar's ``bridge_exit``; ``error`` is what says which happened.
    """
    base: dict[str, object] = {
        "keeper_pid": os.getpid(),
        "bridge_pid": None,
        "bridge_proc_start": None,
        "bridge_start_ticks": None,
        "connect_url": None,
        "session_id": None,
        "worktree_name": _worktree_from_argv(bridge_argv),
        "state": "starting",
        "error": None,
        # Advisory, NOT a failure: set when the bridge is fine but something degraded for
        # the operator (#1390). Distinct from `error` by design — the runner reads `error`
        # only on an ERROR row, and this rides a healthy one. Always present so the sidecar
        # keeps one shape whether or not a note was raised.
        "note": None,
    }
    _write_sidecar(sidecar, base)

    try:
        pty_process = _load_pty_process()
    except Exception as exc:  # noqa: BLE001 — record + return, never raise (see run_keeper)
        base.update(state="error", error=f"pywinpty unavailable: {exc}")
        _write_sidecar(sidecar, base)
        return 72
    os.environ["PYWINPTY_BLOCK"] = "0"  # non-blocking read() so the loop can also poll liveness

    # The live-view tap stays gated on screen_sidecar in the drain.
    # Screen setup must never take the keeper down: on failure the screen is simply absent
    # (raw-bytes scrape only) and, when a live view was requested, its sidecar explains why.
    screen: PtyScreen | None = None
    screen_error: tuple[str, str] | None = None
    try:
        screen = PtyScreen()
    except PyteUnavailableError as exc:
        screen_error = ("unavailable", str(exc))
    except Exception as exc:  # noqa: BLE001 — see above; degrade, never raise
        screen_error = ("error", f"screen init: {exc}")
    if screen_error is not None and screen_sidecar is not None:
        _write_screen_status(screen_sidecar, 0, screen_error[0], screen_error[1])

    dimensions = (SCREEN_ROWS, SCREEN_COLS) if screen is not None else (24, 80)
    try:
        proc = pty_process.spawn(
            bridge_argv, cwd=cwd, env=procutil.child_env(), dimensions=dimensions
        )
    except Exception as exc:  # noqa: BLE001 — record + return, never raise (see run_keeper)
        base.update(state="error", error=f"conpty spawn failed: {exc}")
        _write_sidecar(sidecar, base)
        return 71

    base["bridge_pid"] = proc.pid
    base["bridge_proc_start"], base["bridge_start_ticks"] = _proc_start_pair(proc.pid)
    _write_sidecar(sidecar, base)

    drain = _KeeperDrain(base, sidecar, screen, screen_sidecar)
    # Drain until the ConPTY closes. Gate the loop on the buffer, NOT on isalive(): a fast-exiting
    # bridge can leave a final chunk (its connect URL or exit banner) buffered after the process
    # handle already reports dead, so read that tail first and consult isalive() only on an EMPTY
    # read to decide exit-vs-idle.
    #
    # `drain.finish` sits in a `finally`, not after the loop: an exception escaping the drain
    # or the reap used to skip it, stranding the sidecar at `starting`/`ready` for an orphaned
    # bridge (#1389, the same failure mode #1388 closed on the scrape). Structural rather than
    # enumerated — the individual pywinpty guards below decide how well that terminal sidecar
    # is DESCRIBED; the `finally` decides that it gets written at all, including for faults
    # nothing here anticipates.
    loop_error: str | None = None
    rc = 73  # "no bridge exit status available"; replaced on every path that reaps one
    try:
        while True:
            try:
                data = proc.read(65536)  # str; "" when no data (PYWINPTY_BLOCK=0)
            except EOFError:
                # End-of-stream, or an idle non-blocking read some pywinpty builds surface as
                # EOFError. Which one it is, is exactly the question the empty-read branch below
                # already answers, so fold this into "no data" and let the single `_conpty_alive`
                # there decide: a still-alive bridge is idle (keep polling), a dead one is closed
                # and drained (break). Checking here as well would query the handle twice per
                # idle poll for one answer.
                data = ""
            except Exception as exc:  # noqa: BLE001 — see below: ANY read fault is fail-closed
                # A genuine read-channel error, NOT mere idle — the ConPTY is unusable, so the
                # bridge can be neither observed nor driven. Fail closed: stop and record it,
                # rather than spin converting the error to no-data and promoting a broken stream
                # to "ready".
                #
                # Deliberately NOT `except OSError`: pywinpty raises its own `WinptyError`, which
                # is not an OSError subclass, so the narrow form let the very failure this arm
                # exists for escape the loop instead of take it.
                # `login_shepherd._pump_conpty` already catches the read this widely, for this
                # reason.
                loop_error = f"conpty read failed: {exc}"
                break
            if data:
                drain.feed(data.encode("utf-8", "replace"))
            else:
                alive, loop_error = _conpty_alive(proc)
                if not alive:
                    break  # no buffered output, and the bridge exited or went unobservable
                time.sleep(0.05)  # alive but idle; yield before re-polling
            drain.tick()

        if loop_error is not None:
            # The stream broke, or liveness stopped answering. Terminate the (possibly still
            # running) bridge so we don't leak an undrivable process — and then do NOT reap it.
            # pywinpty's `wait()` has no timeout and polls `isalive()`, the very call that may
            # have just failed: a fault that then clears would block the keeper here for the
            # bridge's whole remaining life while nothing drains the ConPTY. A HUNG keeper is
            # worse than the crash this fixes, because the runner's stale-sidecar sweep is gated
            # on `is_keeper_process` — a dead keeper is swept, a live one holding a `ready`
            # sidecar never is. Nothing consumes the exit status (see the docstring), so leaving
            # rc at 73 costs nothing and the sidecar's `error` carries the real answer.
            base["error"] = loop_error
            _report_keeper_fault(loop_error)
            with contextlib.suppress(Exception):
                proc.terminate()
        else:
            try:
                rc = proc.wait() or 0
            except Exception as exc:  # noqa: BLE001 — the sidecar outranks the exit status
                # The loop exited cleanly, so `isalive()` had just reported the bridge dead and
                # this `wait()` should have returned its cached status without blocking. It
                # raised instead: keep rc at 73 and name it, rather than lose the terminal
                # sidecar in the gap between here and `finish`.
                wait_error = f"conpty wait failed: {exc}"
                base["error"] = wait_error
                _report_keeper_fault(wait_error)
    except BaseException as exc:
        # BaseException, not Exception: an interrupt unwinding the keeper (SIGINT ->
        # KeyboardInterrupt, SystemExit) would otherwise skip this and publish the one terminal
        # shape that reads as a CLEAN exit with no reason — `exited` / 73 / `error: null`. So
        # every abort is named, whether it is an unanticipated fault (a pywinpty build returning
        # bytes, a pyte fault escaping the drain) or a signal.
        #
        # Kill the bridge before unwinding: the `finally` below closes the ConPTY handle and the
        # keeper then dies, so a bridge left running here outlives the only process that could
        # observe or drive it. Then re-raise — nothing is swallowed, and the traceback still
        # reaches `<sidecar>.log` (B036 is satisfied by that `raise`).
        abort_error = f"conpty keeper aborted: {exc!r}"
        base["error"] = abort_error
        _report_keeper_fault(abort_error)
        with contextlib.suppress(Exception):
            proc.terminate()
        raise
    finally:
        drain.finish(rc)
        try:
            proc.close()
        except Exception:  # noqa: BLE001,S110 — teardown close must never crash the keeper
            pass
    return rc


def run_keeper(
    bridge_argv: list[str],
    sidecar: Path,
    cwd: str | None = None,
    screen_sidecar: Path | None = None,
) -> int:
    """Spawn ``bridge_argv`` under a PTY, publish a discovery sidecar, drain, and wait.

    Returns the bridge's exit status. Any setup failure is recorded in the
    sidecar (``state: "error"``) and returned as a non-zero code rather than
    raised, so the launching process always gets a diagnosable result.

    That covers the failures each backend anticipates; it is NOT a promise never to raise.
    :func:`_run_keeper_conpty` publishes the terminal sidecar and then re-raises anything it
    did not anticipate (#1389), because a keeper that swallowed an unknown fault would leave
    no traceback in ``<sidecar>.log`` at all. The POSIX path below keeps its narrower
    enumerated ``except`` shape unchanged — its transport is ``os.read`` on a plain fd, whose
    failure modes are the documented ``OSError`` family, not a third-party handle's.

    When ``screen_sidecar`` is given (the opt-in live-screen tap, #534), the same drained
    chunks are also fed into a pyte emulator and a redacted, cells-only frame is republished
    there — strictly best-effort, never affecting the drain, discovery, or the bridge.

    On Windows there is no POSIX pty; the ConPTY backend (:func:`_run_keeper_conpty`)
    serves the same contract via pywinpty.
    """
    if sys.platform == "win32":
        return _run_keeper_conpty(bridge_argv, sidecar, cwd, screen_sidecar)

    import fcntl
    import pty
    import termios

    keeper_pid = os.getpid()
    base: dict[str, object] = {
        "keeper_pid": keeper_pid,
        "bridge_pid": None,
        "bridge_proc_start": None,
        "bridge_start_ticks": None,
        "connect_url": None,
        "session_id": None,
        "worktree_name": _worktree_from_argv(bridge_argv),
        "state": "starting",
        "error": None,
        "note": None,  # advisory on a healthy row; see the ConPTY base dict above (#1390)
    }
    _write_sidecar(sidecar, base)

    screen: PtyScreen | None = None
    try:
        master, slave = pty.openpty()
        slave_name = os.ttyname(slave)
        # Build the pyte emulator (live view + connect-URL reassembly) BEFORE sizing the PTY so
        # the winsize can be coupled to it. pyte renders at a fixed SCREEN_COLS x SCREEN_ROWS
        # (no resize negotiation), so the bridge's PTY must match that geometry — otherwise the
        # TUI's cursor-addressed redraws (e.g. its bottom status bar) land at the wrong rows and
        # leave stale duplicate footers in the taller pyte screen (#534). That same TUI winsize
        # is also what makes claude print the connect URL via cursor-positioning escapes that
        # fragment the raw byte stream (#665), so set it ONLY when a pyte screen exists to
        # reassemble it: without pyte the PTY keeps the default winsize, where claude emits plain
        # output whose URL line stays contiguous for the raw-bytes scrape. Best-effort — a
        # winsize ioctl failure never aborts the spawn.
        screen = _make_screen(screen_sidecar)
        if screen is not None:
            try:
                fcntl.ioctl(
                    slave, termios.TIOCSWINSZ, struct.pack("HHHH", SCREEN_ROWS, SCREEN_COLS, 0, 0)
                )
            except OSError:
                pass
    except OSError as exc:
        base.update(state="error", error=f"openpty failed: {exc}")
        _write_sidecar(sidecar, base)
        return 70

    def _acquire_ctty() -> None:  # pragma: no cover — runs in the forked child, pre-exec
        """Lead a new session and claim the PTY slave as the child's controlling terminal."""
        # Make the bridge its own session leader FIRST: TIOCSCTTY only works for a
        # session leader with no controlling terminal. Without this setsid the
        # bridge stays in the launcher's session and never acquires the PTY as its
        # controlling terminal (the interactive flag form needs a real one).
        os.setsid()
        fd = os.open(slave_name, os.O_RDWR)
        try:
            fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
        except OSError:
            pass
        os.close(fd)

    try:
        proc = subprocess.Popen(
            bridge_argv,
            cwd=cwd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            # Scrub Clauster secrets at the layer that directly spawns the
            # project-code bridge, so it stays secret-free however the keeper
            # was launched (the launcher scrubs too — defense in depth).
            env=procutil.child_env(),
            preexec_fn=_acquire_ctty,
            close_fds=True,
        )
    except OSError as exc:
        os.close(master)
        os.close(slave)
        base.update(state="error", error=f"spawn failed: {exc}")
        _write_sidecar(sidecar, base)
        return 71
    os.close(slave)  # the child holds its own dup; we only need the master
    # Now that the bridge has been exec'd (it does NOT inherit this), make the
    # keeper itself impervious to a stray terminal hangup so it reliably outlives
    # a Clauster restart and keeps the bridge's master fd open.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    base["bridge_pid"] = proc.pid
    base["bridge_proc_start"], base["bridge_start_ticks"] = _proc_start_pair(proc.pid)
    _write_sidecar(sidecar, base)

    flags = fcntl.fcntl(master, fcntl.F_GETFL)
    fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    # poll(), not select(): the stdlib select() call raises "filedescriptor out of range"
    # once the master fd is >= FD_SETSIZE (1024) — a long-lived Clauster (many bridges/keepers,
    # a busy host, or accumulated fds) can reach that and crash the keeper. poll() has no
    # such ceiling. (This also fixed a -n0 full-suite test flake where the pty master fd
    # crept past 1024.)
    poller = select.poll()
    poller.register(master, select.POLLIN)

    # `screen` (built above for URL reassembly + the opt-in live view) and its drain state
    # now live in the shared _KeeperDrain — the same publish path the Windows ConPTY backend
    # feeds. The keeper here owns only the POSIX master-fd I/O: poll for data, read a chunk,
    # hand it to the drain; between reads, let the drain throttle the live view + time out the
    # URL scrape.
    drain = _KeeperDrain(base, sidecar, screen, screen_sidecar)
    while proc.poll() is None:
        try:
            if poller.poll(500):  # ms; truthy when the master fd has data (no FD_SETSIZE limit)
                chunk = os.read(master, 65536)
                if chunk:
                    drain.feed(chunk)
        except (OSError, BlockingIOError):
            time.sleep(0.2)
        drain.tick()

    rc = proc.poll() or 0
    drain.finish(rc)
    try:
        os.close(master)
    except OSError:
        pass
    return rc


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``python -m clauster.pty_keeper --sidecar P -- <bridge argv>``."""
    parser = argparse.ArgumentParser(prog="clauster.pty_keeper", description=__doc__)
    parser.add_argument("--sidecar", required=True, type=Path, help="JSON discovery file to write")
    parser.add_argument("--cwd", default=None, help="working directory for the bridge")
    parser.add_argument(
        "--screen-sidecar",
        default=None,
        type=Path,
        help="optional JSON file for the redacted live-screen frames (#534)",
    )
    parser.add_argument(
        "bridge_argv",
        nargs=argparse.REMAINDER,
        help="the bridge command line, after a `--` separator",
    )
    ns = parser.parse_args(argv)
    bridge_argv = ns.bridge_argv
    if bridge_argv and bridge_argv[0] == "--":
        bridge_argv = bridge_argv[1:]
    if not bridge_argv:
        parser.error("no bridge argv given after `--`")
    return run_keeper(bridge_argv, ns.sidecar, cwd=ns.cwd, screen_sidecar=ns.screen_sidecar)


# --- orphan-keeper management (#301) -----------------------------------------
#
# A keeper is a detached process that outlives a Clauster restart. The normal
# stop path (runner.stop -> _cleanup_keeper) covers a keeper still attached to a
# project card. But if the card is gone (the project was removed), no card can
# show or stop it — it is invisible and unkillable. These helpers let a sweep
# (the `clauster keepers` CLI) list and stop such orphans from the sidecar files
# alone, with no running runner.

# A keeper sidecar is `<name>-<ms>-<seq>.keeper.json`; this reverses the stem to the
# project name, used for display AND for orphan classification (find_orphan_keepers
# protects a keeper when its parsed name is a current card). The greedy `.+` strips
# exactly the two trailing numeric groups, so it recovers the original name even when
# the name contains `-` or trailing digits — the exact inverse of the runner's anchored
# `_keeper_sidecars_for` glob, so the two sides classify a keeper identically (#1181).
_KEEPER_STEM_RE = re.compile(r"^(?P<name>.+)-\d+-\d+$")
_KEEPER_SUFFIX = ".keeper.json"
_KEEPER_START_TOLERANCE = 2.0  # PID-reuse guard slack, matching runner's bridge tolerance


@dataclass(frozen=True)
class KeeperInfo:
    """A discovered keeper sidecar plus its derived liveness/identity (#301)."""

    sidecar: Path
    project: str | None
    keeper_pid: int | None
    bridge_pid: int | None
    session_id: str | None
    state: str | None
    alive: bool
    keeper_create_time: float | None  # the keeper PID's create-time (PID-reuse guard)
    # The boot-relative half of that guard (#1402): the create-time above is re-derived from
    # a `/proc/stat` btime NTP moves, so on its own a 2.0s bound has to stay wide enough to
    # absorb drift — wide enough to admit a pid recycled during `stop_keeper`'s own grace.
    keeper_start_ticks: int | None


def _read_sidecar(path: Path) -> dict:
    """Read a keeper sidecar, tolerating absent / mid-write / invalid files (-> {})."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _project_from_sidecar(filename: str) -> str | None:
    """Best-effort project name from a `<name>-<ms>-<seq>.keeper.json` filename."""
    stem = filename[: -len(_KEEPER_SUFFIX)] if filename.endswith(_KEEPER_SUFFIX) else filename
    m = _KEEPER_STEM_RE.match(stem)
    return m.group("name") if m else None


def _int_or_none(value: object) -> int | None:
    """Return ``value`` when it is a genuine ``int``, rejecting ``bool`` and everything else."""
    # ``bool`` is a subclass of ``int``, so a corrupt sidecar carrying e.g.
    # ``"keeper_pid": true`` would otherwise resolve to PID 1. Exclude it,
    # matching the convention in procutil.py.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def iter_keepers(log_dir: Path) -> list[KeeperInfo]:
    """Discover every `*.keeper.json` sidecar under ``log_dir`` with liveness (#301)."""
    try:
        files = sorted(log_dir.glob(f"*{_KEEPER_SUFFIX}"))
    except OSError:
        return []
    out: list[KeeperInfo] = []
    for f in files:
        data = _read_sidecar(f)
        keeper_pid = _int_or_none(data.get("keeper_pid"))
        # ONE `proc_start_pair` read for both halves (#1402), so they cannot describe two
        # different processes. Note the pair reports a real identity for a ZOMBIE where
        # `proc_create_time` answered None — `alive` below is unaffected, because
        # `is_keeper_process` fails closed on a zombie and is conjoined here.
        create_time, start_ticks = (
            procutil.proc_start_pair(keeper_pid) if keeper_pid is not None else (None, None)
        )
        state = data.get("state")
        session_id = data.get("session_id")
        out.append(
            KeeperInfo(
                sidecar=f,
                project=_project_from_sidecar(f.name),
                keeper_pid=keeper_pid,
                bridge_pid=_int_or_none(data.get("bridge_pid")),
                session_id=session_id if isinstance(session_id, str) else None,
                state=state if isinstance(state, str) else None,
                # A live PID alone isn't enough: confirm its cmdline is still our keeper,
                # so a PID the keeper left behind and the OS recycled onto an unrelated
                # process is never listed as a live orphan (#301 / RUNOPS-1). The leading
                # `keeper_pid is not None` also narrows the type for is_keeper_process.
                #
                # `is_keeper_process` is the WHOLE test now (#1402). It already proves alive,
                # non-zombie and keeper-cmdline, so the `create_time is not None` conjunct it
                # used to carry added no rejection — it only asked the clock a liveness
                # question, and on a procfs with no `btime` the answer is None for a keeper
                # that is plainly running. That made `find_orphan_keepers` list nothing there,
                # so `clauster keepers --kill` refused every pid and `stop_keeper` — the one
                # place the btime-less wind-down could happen from the CLI — was unreachable.
                alive=keeper_pid is not None and procutil.is_keeper_process(keeper_pid),
                keeper_create_time=create_time,
                keeper_start_ticks=start_ticks,
            )
        )
    return out


def find_orphan_keepers(log_dir: Path, carded_projects: set[str]) -> list[KeeperInfo]:
    """Return live keepers whose sidecar belongs to no current project card (#301).

    A keeper is protected when its parsed project name (the ``<name>`` of the
    ``<name>-<ms>-<seq>.keeper.json`` stem, via :func:`_project_from_sidecar`) is a current
    card. That anchored match is exact: the numeric ``-<ms>-<seq>`` suffix can't span a ``-``
    in the name, so a carded ``app`` protects only ``app``'s keepers, never a sibling
    ``app-staging``'s. A dead keeper is not an orphan (nothing to stop).

    Anchoring here matches the runner's ``<name>-<ms>-<seq>`` stem
    (``SessionRunner._keeper_sidecars_for``), closing the divergence #1177 introduced (#1181).
    The old unanchored ``glob("<project>-*.keeper.json")`` over-protected: with ``app`` carded
    and ``app-staging`` **removed**, the removed project's live keeper matched ``app-*`` and was
    filtered out — so ``clauster keepers`` never listed it and ``--kill`` refused it for as long
    as ``app`` stayed carded, hiding exactly the orphan #301 exists to surface. It is now listed
    and killable.

    A keeper whose filename doesn't parse (``project is None``) belongs to no card and so is
    an orphan — the same result the old glob gave it.
    """
    return [k for k in iter_keepers(log_dir) if k.alive and k.project not in carded_projects]


def _start_still_matches(
    observed: tuple[float | None, int | None],
    expect_create_time: float | None,
    expect_start_ticks: int | None,
) -> bool:
    """Whether an observed start pair is still the keeper that was classified (#1402).

    Exact on the boot-relative ticks wherever both sides have them, and the epoch within
    ``_KEEPER_START_TOLERANCE`` otherwise. That 2.0s bound cannot be tightened while the
    epoch stands alone — psutil re-derives it from a ``/proc/stat`` btime NTP moves — and it
    is wide enough to admit a pid recycled during :func:`stop_keeper`'s own ~2s grace, which
    is the hole this closes. Ticks are measured from the boot instant, so they do not move at
    all while a recycled pid differs by a whole ``CLK_TCK`` of them: drift-proof *and*
    stricter than the bound it replaces.

    With **nothing** comparable this answers False — "not proven to be ours", not "ours".
    An expectation was recorded and could not be checked, and this predicate fronts a
    ``force_kill_tree`` on a whole process tree, so the unproven case must not authorize it.
    Note that is a real change over the pre-#1402 code, which read the epoch alone: given
    a recorded ``expect_create_time`` and a host where the epoch is unreadable (a btime-less
    procfs), that code took ``proc_create_time is None`` as "exited during the grace" and
    returned True — a false report of success with no kill. False is the honest answer
    where True was a silent success; neither kills.
    :func:`_start_is_comparable` is how the post-kill poll asks the other question — whether
    the answer was reached from evidence — because there an unproven read must keep waiting
    rather than report a kill it cannot confirm.
    """
    epoch, ticks = observed
    if expect_start_ticks is not None and ticks is not None:
        return ticks == expect_start_ticks
    if expect_create_time is not None and epoch is not None:
        return abs(epoch - expect_create_time) <= _KEEPER_START_TOLERANCE
    return False


def _start_is_comparable(
    observed: tuple[float | None, int | None],
    expect_create_time: float | None,
    expect_start_ticks: int | None,
) -> bool:
    """Whether :func:`_start_still_matches` had any evidence to reach its answer on (#1402).

    Splits "definitely a different process" from "could not tell", which the two callers need
    to separate because they act on opposite ones. The pre-kill gate refuses on either. The
    post-kill poll may only treat a **definite** mismatch as proof the keeper it killed is
    gone: reporting success from an unreadable pair would turn an unconfirmed kill into a
    silent success, where waiting out the poll reports the honest failure instead.
    """
    epoch, ticks = observed
    return (expect_start_ticks is not None and ticks is not None) or (
        expect_create_time is not None and epoch is not None
    )


def stop_keeper(
    keeper_pid: int,
    *,
    expect_create_time: float | None = None,
    # Keyword-required, no default (#1402): this guard sits in front of a SIGKILL on a whole
    # process tree, and a caller that silently defaulted the drift-immune half would leave
    # the kill gated on the 2.0s epoch bound alone. A missed site is a type error instead.
    expect_start_ticks: int | None,
) -> bool:
    """Stop a keeper process: wait ~2s for it to exit on its own, then force-kill its tree.

    No stop signal is sent during the grace window — it only reaps an already-exited
    child and polls. That reap is a no-op for a keeper that is not the caller's child
    (the CLI is a separate process), so the force path is what actually stops a detached
    orphan and its bridge subtree. Returns True once the process is gone.

    ``expect_create_time`` and ``expect_start_ticks`` (both captured when the keeper was
    classified, from one :func:`procutil.proc_start_pair` read) are the PID-reuse guard: if,
    after the grace window, the PID's start identity no longer matches, the original keeper
    already exited and the PID was recycled onto an unrelated process — refuse the SIGKILL
    rather than kill a stranger. See :func:`_start_still_matches` for which half decides.

    The grace probe is :func:`procutil.proc_is_gone` rather than a create-time read (#1402):
    on a procfs with no ``btime`` the create-time is unavailable for a process that is
    plainly running, and this loop would have reported every lingering keeper as already
    gone without killing anything.
    """
    for _ in range(8):  # ~2s grace, mirroring runner._cleanup_keeper
        procutil.reap_if_exited(keeper_pid)
        if procutil.proc_is_gone(keeper_pid):
            return True
        time.sleep(0.25)
    if expect_create_time is not None or expect_start_ticks is not None:
        observed = procutil.proc_start_pair(keeper_pid)
        if observed == (None, None):
            return True  # exited during the grace window — nothing left to kill
        if not _start_still_matches(observed, expect_create_time, expect_start_ticks):
            return False  # PID reused onto another process — do not kill it
    # Final cmdline re-verify right before the SIGKILL (TOCTOU): the PID must still be
    # our keeper. If it exited and the OS recycled the PID onto an unrelated process
    # during the grace window, never hard-kill that stranger (#301 / RUNOPS-1).
    if not procutil.is_keeper_process(keeper_pid):
        return True  # keeper already gone (or PID recycled to a non-keeper)
    procutil.force_kill_tree(keeper_pid)
    # SIGKILL is asynchronous: the process may still be running/zombie for a beat
    # after force_kill_tree returns, so poll briefly (reaping our own child if it is
    # one) before reporting the outcome rather than racing the kill.
    # Same guard as the pre-kill gate above: with neither expectation recorded the pair
    # could not be comparable, so the `/proc` read would be discarded on every iteration.
    expecting = expect_create_time is not None or expect_start_ticks is not None
    for _ in range(10):
        procutil.reap_if_exited(keeper_pid)
        if procutil.proc_is_gone(keeper_pid):
            return True
        if expecting:
            observed = procutil.proc_start_pair(keeper_pid)
            if _start_is_comparable(
                observed, expect_create_time, expect_start_ticks
            ) and not _start_still_matches(observed, expect_create_time, expect_start_ticks):
                return True  # PID recycled onto a new process → the keeper we killed is gone
        time.sleep(0.1)
    return False


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    sys.exit(main())
