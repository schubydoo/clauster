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
stops the bridge with ``SIGINT`` (twice — see :mod:`clauster.runner`). POSIX
only; Windows has no ``pty`` and keeps the subcommand / recap path.
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


def _proc_start(pid: int) -> float | None:
    """Return the bridge's process start time for Clauster's PID-reuse defense, or None."""
    # Imported lazily so the keeper still runs if procutil grows heavier deps.
    try:
        from clauster import procutil

        return procutil.proc_create_time(pid)
    except Exception:  # noqa: BLE001 — discovery aid only; never fatal
        return None


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
        self._deadline = time.monotonic() + _URL_TIMEOUT

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
                if self._tap is not None:  # pragma: no cover - tap is set with screen
                    self._seq += 1
                    _write_screen_status(
                        self._tap.sidecar, self._seq, "error", f"screen feed: {exc}"
                    )
                    self._tap = None
                self._screen = None
        if not self._url_found:
            self._buf.extend(chunk)
            session_id = _scan_connect_url(self._buf, self._screen)
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
    from winpty import PtyProcess  # pragma: no cover - win32-only; exercised on the VM

    return PtyProcess  # pragma: no cover - win32-only


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
    Setup/spawn failures are recorded in the sidecar and returned as a code, never raised.
    """
    base: dict[str, object] = {
        "keeper_pid": os.getpid(),
        "bridge_pid": None,
        "bridge_proc_start": None,
        "connect_url": None,
        "session_id": None,
        "state": "starting",
        "error": None,
    }
    _write_sidecar(sidecar, base)

    try:
        pty_process = _load_pty_process()
    except Exception as exc:  # noqa: BLE001 — record + return, never raise (see run_keeper)
        base.update(state="error", error=f"pywinpty unavailable: {exc}")
        _write_sidecar(sidecar, base)
        return 72
    os.environ["PYWINPTY_BLOCK"] = "0"  # non-blocking read() so the loop can also poll liveness

    # Build a pyte screen for URL reassembly whenever pyte is available (ConPTY fragments the
    # URL regardless of size); the live-view tap stays gated on screen_sidecar in the drain.
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
    base["bridge_proc_start"] = _proc_start(proc.pid)
    _write_sidecar(sidecar, base)

    drain = _KeeperDrain(base, sidecar, screen, screen_sidecar)
    # Drain until the ConPTY closes. Gate the loop on the buffer, NOT on isalive(): a fast-exiting
    # bridge can leave a final chunk (its connect URL or exit banner) buffered after the process
    # handle already reports dead, so read that tail first and consult isalive() only on an EMPTY
    # read to decide exit-vs-idle.
    read_error: str | None = None
    while True:
        try:
            data = proc.read(65536)  # str; "" when no data (PYWINPTY_BLOCK=0)
        except EOFError:
            # End-of-stream, or an idle non-blocking read some pywinpty builds surface as EOFError:
            # a still-alive bridge is idle (keep polling); a dead one is closed and drained.
            if not proc.isalive():
                break
            data = ""
        except OSError as exc:
            # A genuine read-channel error, NOT mere idle — the ConPTY is unusable, so the bridge
            # can be neither observed nor driven. Fail closed: stop and record it, rather than spin
            # converting the error to no-data and promoting a broken stream to "ready".
            read_error = f"conpty read failed: {exc}"
            break
        if data:
            drain.feed(data.encode("utf-8", "replace"))
        elif not proc.isalive():
            break  # no buffered output and the bridge has exited
        else:
            time.sleep(0.05)  # alive but idle; yield before re-polling
        drain.tick()

    if read_error is not None:
        # The stream broke: terminate the (possibly still-running) bridge so we neither leak an
        # undrivable process nor block wait() below on one we can no longer observe.
        base["error"] = read_error
        with contextlib.suppress(Exception):
            proc.terminate()
    rc = proc.wait() or 0
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
        "connect_url": None,
        "session_id": None,
        "state": "starting",
        "error": None,
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
    base["bridge_proc_start"] = _proc_start(proc.pid)
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

# A keeper sidecar is `<name>-<ms>-<seq>.keeper.json`; this reverses the stem to
# the project name for display only — orphan classification uses the same
# forward glob the runner uses (see find_orphan_keepers), never this parse.
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
        create_time = procutil.proc_create_time(keeper_pid) if keeper_pid is not None else None
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
                alive=keeper_pid is not None
                and create_time is not None
                and procutil.is_keeper_process(keeper_pid),
                keeper_create_time=create_time,
            )
        )
    return out


def find_orphan_keepers(log_dir: Path, carded_projects: set[str]) -> list[KeeperInfo]:
    """Return live keepers whose sidecar belongs to no current project card (#301).

    Classification uses the same forward glob the runner uses to attach a keeper
    to a project (``<project>-*.keeper.json``), so it can't mis-attribute a
    keeper via an ambiguous filename reverse-parse. A dead keeper is not an
    orphan (nothing to stop).
    """
    carded_files: set[Path] = set()
    protected_names: set[str] = set()
    for project in carded_projects:
        try:
            carded_files.update(log_dir.glob(f"{project}-*{_KEEPER_SUFFIX}"))
        except OSError:
            # Can't enumerate this project's sidecars — protect it by parsed name
            # instead, so a live carded keeper can never surface as an orphan. This
            # over-protects (a name parse is approximate), never under-protects.
            protected_names.add(project)
    return [
        k
        for k in iter_keepers(log_dir)
        if k.alive and k.sidecar not in carded_files and k.project not in protected_names
    ]


def stop_keeper(keeper_pid: int, *, expect_create_time: float | None = None) -> bool:
    """Stop a keeper process (graceful reap, then force-kill its whole tree).

    Returns True once the process is gone. The reap loop is a no-op for a keeper
    that is not the caller's child (the CLI is a separate process), so the force
    path is what actually stops a detached orphan and its bridge subtree.

    ``expect_create_time`` (the create-time captured when the keeper was
    classified) is a PID-reuse guard: if, after the grace window, the PID's
    create-time no longer matches, the original keeper already exited and the PID
    was recycled onto an unrelated process — refuse the SIGKILL rather than kill a
    stranger.
    """
    for _ in range(8):  # ~2s grace, mirroring runner._cleanup_keeper
        procutil.reap_if_exited(keeper_pid)
        if procutil.proc_create_time(keeper_pid) is None:
            return True
        time.sleep(0.25)
    if expect_create_time is not None:
        current = procutil.proc_create_time(keeper_pid)
        if current is None:
            return True  # exited during the grace window — nothing left to kill
        if abs(current - expect_create_time) > _KEEPER_START_TOLERANCE:
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
    for _ in range(10):
        procutil.reap_if_exited(keeper_pid)
        current = procutil.proc_create_time(keeper_pid)
        if current is None:
            return True
        if (
            expect_create_time is not None
            and abs(current - expect_create_time) > _KEEPER_START_TOLERANCE
        ):
            return True  # PID recycled onto a new process → the keeper we killed is gone
        time.sleep(0.1)
    return False


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    sys.exit(main())
