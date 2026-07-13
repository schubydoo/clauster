"""Dashboard-driven `claude` account login shepherd (#839, #846).

Today the only fix for a logged-out (or token-expired) runtime `claude` account is
to SSH in as the runtime user and run `claude auth login` interactively. This
module lets an operator complete that same login from the browser instead, by
owning a single long-lived `claude auth login` / `claude setup-token` subprocess,
streaming its output, and forwarding the operator-pasted OAuth code back to its
stdin — exactly what a human would type over SSH.

Two modes, with DIFFERENT transports (live-verified, #846):

* ``login``       — ``claude auth login --claudeai`` (Claude subscription). A plain-pipe
  interactive OAuth flow: it prints an authorize `https://…` URL and reads a pasted code
  back from stdin over ordinary ``subprocess.PIPE``s — no pty required.
* ``setup-token`` — ``claude setup-token`` is a full TUI. It prints essentially nothing on
  a plain pipe (verified: 1 byte in 12s) and only renders its
  ``https://claude.com/cai/oauth/authorize?...`` URL under a real terminal. So this mode is
  spawned under a PTY (:func:`os.openpty`) and its raw bytes are fed through
  :class:`clauster.pty_screen.PtyScreen` (reusing the pyte emulator already built for the
  live pty-screen view, #534) — the RENDERED screen text is what gets scanned for the
  authorize URL and the printed ``CLAUDE_CODE_OAUTH_TOKEN=...`` token, exactly like
  :meth:`PtyScreen.find_session_id` reassembles the pty bridge's connect URL. ``pyte`` is
  the optional ``pty`` extra, so this mode fails closed with a clear message when it is
  absent (see :meth:`LoginShepherd._spawn_pty`) — ``login`` mode stays pyte-free.

Single-flight: at most one login subprocess is ever active. Starting a new flow
while one is already running is rejected (409) — the caller must `cancel()` first.
This is simpler and safer than silently replacing an in-flight login (which could
orphan a subprocess mid-OAuth-exchange or interleave two operators' pastes).

**Never logs the pasted code or the resulting token.** Both are treated as
secrets: they are never passed to a log call, and any captured subprocess output
that might echo them is redacted before it is logged (it is still returned
verbatim to the one authorized caller over the API response, which is the whole
point of the feature).
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from . import claude_cli, procutil
from .pty_screen import SCREEN_ROWS, PtyScreen, PyteUnavailableError, extract_authorize_url

_T = TypeVar("_T")

_log = logging.getLogger("clauster.login_shepherd")

Mode = Literal["login", "setup-token"]

#: PTY width (columns) for the `setup-token` transport (#846 follow-up). Real
#: `claude setup-token` wraps its printed `https://claude.com/cai/oauth/authorize?...`
#: URL at the terminal's column width — a plain `os.openpty()` defaults to 80 cols, which
#: is narrower than the ~450-char authorize URL and truncates it mid-query-string once
#: `PtyScreen.find_authorize_url()` joins the (now multi-line) rendered screen. Sizing the
#: pty (and the shepherd's own `PtyScreen`, see `_spawn_pty`) to a wide column count keeps
#: the whole URL on one rendered line so it never wraps in the first place. Rows reuse
#: `SCREEN_ROWS` (the pty-screen module's fixed geometry) — only the width matters here.
_LOGIN_PTY_COLS = 1024

#: How long `start()` waits for an authorize URL (or process exit) before giving
#: up and failing closed. The real flow should print the URL almost immediately;
#: generous enough to absorb a slow network probe without hanging the request
#: forever.
START_TIMEOUT_SECONDS = 30.0

#: How long `submit_code()` waits for the process to react to the pasted code
#: (verify + exit, or print a token) before returning whatever was captured so far.
SUBMIT_TIMEOUT_SECONDS = 45.0

#: Poll cadence for the bounded waits below.
_POLL_INTERVAL_SECONDS = 0.1

#: After `start()` sees an authorize URL while the process still *looks* alive, how long
#: to wait to confirm it stays alive (a genuine login BLOCKS on stdin here) before trusting
#: the URL. `poll()` can lag a just-returned `exit()` by a scheduling tick, so a process
#: that prints a URL and immediately dies can momentarily read as still-running; this brief
#: settle lets that exit surface so `start()` fails closed deterministically instead of
#: racily handing back a URL for a subprocess with no live stdin. An already-dead process is
#: reaped within the first wait tick (so the pathological case pays ~0ms); only the genuine
#: blocked-on-stdin login rides out the full grace once — invisible against a human login.
_URL_LIVENESS_GRACE_SECONDS = 0.25

# `setup-token`'s printed long-lived credential (per the spike: `CLAUDE_CODE_OAUTH_TOKEN=...`).
# Matched permissively (any non-whitespace value) since the exact real-binary format is
# unverified — this is defensive, not assumed exact. Kept local (not shared with
# `pty_screen`) because it backs ONLY this module's log-redaction helper below;
# `pty_screen.extract_oauth_token` is the shared *extraction* pattern the PTY path uses.
_TOKEN_RE = re.compile(r"CLAUDE_CODE_OAUTH_TOKEN=(\S+)")

# The URL/token SELECTION logic (`extract_authorize_url`/`extract_oauth_token`) lives in
# `pty_screen` (shared with `PtyScreen.find_authorize_url`/`find_oauth_token` for the
# setup-token PTY path) — `login_shepherd` already needs `PtyScreen` for that same PTY
# spawn, so keeping the shared helpers there avoids an import cycle. Re-exported under
# the old private name for backward compatibility with existing callers/tests.
_extract_authorize_url = extract_authorize_url


class LoginShepherdError(RuntimeError):
    """A login-shepherd operation failed (bad state, spawn failure, or no-URL fail-closed)."""


class AlreadyActiveError(LoginShepherdError):
    """A login flow is already in progress (single-flight — call `cancel()` first)."""


class NotActiveError(LoginShepherdError):
    """No login flow is currently in progress."""


def _is_win32() -> bool:
    """Whether we're on Windows (seam so `_spawn_pty`'s ConPTY branch is testable on POSIX).

    Isolated behind a function — like `pty_keeper._load_pty_process`'s platform guard — so a
    POSIX test can drive the win32 `setup-token` transport (`_spawn_conpty`) by patching this,
    without mutating the shared `sys.platform` singleton (which would also flip `shutil.which`).
    """
    return sys.platform == "win32"


def _redact(text: str, pasted_secrets: Iterable[str] = ()) -> str:
    """Best-effort mask of secret-shaped values in captured output before it is logged/returned.

    Defense in depth only: the primary guarantee is that the pasted code and the
    parsed token are never themselves passed to a logging call. This additionally
    scrubs a `CLAUDE_CODE_OAUTH_TOKEN=...` line (in case the raw subprocess output
    ever needs to be logged for diagnostics) so the secret value never lands in a
    log line even indirectly.

    `pasted_secrets` are the operator-pasted OAuth codes registered by `_write_code`. On
    the POSIX pty transport a `termios` ECHO-disable keeps a pasted code out of the output
    at the source (`_spawn_pty`), and a plain pipe never mirrors stdin into stdout — but a
    **Windows ConPTY echoes written input back into its read stream** and the parent can't
    disable that the way `termios` does, so the code can land in `flow.buffer`. Masking each
    registered code here closes that leak on the returned/logged "Captured output" too. A
    no-op on POSIX/pipe (nothing to match); empty codes are never registered so `str.replace`
    is never handed the empty string (which would splice the mask between every character).

    Masking is a plain substring `replace` over the RAW buffer, whereas URL/token *extraction*
    reads the pyte-RENDERED screen (ConPTY fragments those with cursor escapes). Console
    input-echo is contiguous, so the substring match is sufficient in practice; and this whole
    layer is defense-in-depth — the primary guarantee (the code is never handed to a logging
    call, and never itself returned except as the redacted "Captured output") stands regardless.
    Over-redaction (a code that also appears elsewhere) is harmless; the risk is only under-match.
    """
    text = _TOKEN_RE.sub("CLAUDE_CODE_OAUTH_TOKEN=<redacted>", text)
    for secret in pasted_secrets:
        if secret:
            text = text.replace(secret, "<redacted-code>")
    return text


class _ConPtyPopen:
    """Minimal `subprocess.Popen`-compatible lifecycle adapter over a pywinpty `PtyProcess`.

    The shepherd's flow lifecycle (`start`/`submit_code`/`poll`/`_teardown`/`_wait_for`) drives
    its subprocess through the `Popen` subset `poll()` / `wait(timeout)` / `terminate()` /
    `kill()` plus the `stdin`/`stdout` attributes. pywinpty's `PtyProcess` exposes the same
    intent through a different shape — `isalive()`, a no-timeout blocking `wait()`, and
    `terminate(force=)` for a hard kill — so this adapts it so the existing, delicately-ordered
    teardown code runs UNCHANGED on the ConPTY transport (only spawn/read/write get a win32
    branch). Reads and writes go through the raw `PtyProcess` (`flow.pty_process`), never this
    adapter; `stdin`/`stdout` are `None` exactly as they are for the POSIX pty `_Flow`.

    Only the confirmed `PtyProcess` surface is used (`isalive`/`wait`/`terminate`) — no reliance
    on `exitstatus` — so it matches the fake used to cover this path on POSIX.

    **All handle access is serialized behind a shared lock.** The reader thread (`_pump_conpty`)
    and the control thread (this shim's `poll`/`wait`/`terminate`/`kill`, plus `_write_code` and
    `_teardown`) both touch the SAME pywinpty `PtyProcess` — unlike `pty_keeper`, whose ConPTY
    loop is single-threaded and so never overlaps a `read` with a `terminate`/`close`. pywinpty
    doesn't document thread-safety for concurrent native handle calls, so `_spawn_conpty` builds
    one lock and hands it to this shim AND stores it on `flow.pty_lock`; every native call takes
    it, restoring `pty_keeper`'s one-op-at-a-time invariant. Deadlock-free by construction: the
    lock is never held across a blocking call (`read()` is non-blocking under `PYWINPTY_BLOCK=0`)
    nor across the `wait()` sleep, and it's never acquired re-entrantly.
    """

    stdin = None
    stdout = None

    def __init__(self, proc: Any, lock: threading.Lock | None = None) -> None:
        """Wrap the live pywinpty `PtyProcess`, serializing handle access behind `lock`."""
        self._proc = proc
        self._lock = lock or threading.Lock()

    def poll(self) -> int | None:
        """Return the exit code if the process has exited, else None (never blocks)."""
        with self._lock:
            if self._proc.isalive():
                return None
            # Dead → `wait()` returns the cached status immediately without blocking. It may
            # return None on some pywinpty builds → coerce to 0.
            return self._proc.wait() or 0

    def wait(self, timeout: float | None = None) -> int:
        """Block until the process exits (or raise `subprocess.TimeoutExpired`).

        pywinpty's `wait()` has no timeout, so poll `isalive()` on the shared cadence and
        raise the same `TimeoutExpired` the callers already handle once the deadline passes.
        The lock is dropped across the sleep so the reader thread can make progress.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if not self._proc.isalive():
                    return self._proc.wait() or 0
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd="claude setup-token", timeout=timeout or 0)
            time.sleep(_POLL_INTERVAL_SECONDS)

    def terminate(self) -> None:
        """Signal the process to terminate (graceful)."""
        with self._lock:
            self._proc.terminate()

    def kill(self) -> None:
        """Force-kill the process (pywinpty's `terminate(force=True)`)."""
        with self._lock:
            self._proc.terminate(force=True)


@dataclass
class _Flow:
    """State for the single active login subprocess.

    ``screen`` is set for BOTH `setup-token` PTY transports (#846 POSIX pty and #905 Windows
    ConPTY); ``master_fd`` is the POSIX-pty control fd and ``pty_process`` is its Windows-ConPTY
    analogue (the pywinpty `PtyProcess`). Exactly one of them is set on a `setup-token` flow and
    both are None on a plain-pipe `login` flow — their presence is the single source of truth
    for which transport a flow uses, so every read/write/teardown site below branches on them
    rather than carrying a separate is-pty flag that could drift out of sync.

    ``pasted_secrets`` are the operator-pasted OAuth codes `_write_code` registers so `_redact`
    can scrub them from any returned/logged output — load-bearing only on the ConPTY transport,
    which echoes written input back (see `_redact`), and harmless defense-in-depth elsewhere.
    ``pty_lock`` serializes native access to ``pty_process`` between the reader and control
    threads (used only by the ConPTY transport; see `_ConPtyPopen`), a no-op cost elsewhere.
    """

    mode: Mode
    proc: subprocess.Popen | _ConPtyPopen
    lock: threading.Lock = field(default_factory=threading.Lock)
    submit_lock: threading.Lock = field(default_factory=threading.Lock)
    buffer: list[str] = field(default_factory=list)
    reader_thread: threading.Thread | None = None
    stdin_closed: bool = False
    screen: PtyScreen | None = None
    master_fd: int | None = None
    pty_process: Any = None
    pty_lock: threading.Lock = field(default_factory=threading.Lock)
    pasted_secrets: list[str] = field(default_factory=list)

    def snapshot(self) -> str:
        """Return everything captured from the subprocess so far."""
        with self.lock:
            return "".join(self.buffer)

    def append(self, chunk: str) -> None:
        with self.lock:
            self.buffer.append(chunk)


class LoginShepherd:
    """Owns at most one active `claude auth login` / `claude setup-token` subprocess."""

    def __init__(self, binary: str) -> None:
        """Store the configured `claude` binary name/path (resolved lazily at spawn)."""
        self._binary = binary
        self._flow: _Flow | None = None
        self._flow_lock = threading.Lock()

    # ----- lifecycle -------------------------------------------------------

    def is_active(self) -> bool:
        """Whether a login subprocess is currently running (or awaiting a code)."""
        with self._flow_lock:
            return self._flow is not None

    def _spawn(self, mode: Mode, resolved_binary: str) -> _Flow:
        """Spawn `mode`'s subprocess and return its `_Flow`. Called under `_flow_lock`.

        Dispatches to the plain-pipe transport (`login`, verified) or the PTY transport
        (`setup-token`, #846) — see the module docstring for why the two differ.
        """
        if mode == "login":
            return self._spawn_pipe(mode, resolved_binary)
        return self._spawn_pty(mode, resolved_binary)

    def _spawn_pipe(self, mode: Mode, resolved_binary: str) -> _Flow:
        """Spawn `claude auth login --claudeai` over plain pipes (the verified path)."""
        argv = [resolved_binary, "auth", "login", "--claudeai"]
        try:
            proc = subprocess.Popen(  # noqa: S603 — list-argv, absolute binary, no shell
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=procutil.child_env(),
            )
        except OSError as exc:
            raise LoginShepherdError(f"failed to start claude {mode}: {exc}") from exc
        flow = _Flow(mode=mode, proc=proc)
        reader = threading.Thread(
            target=_pump_stdout, args=(flow,), name="login-shepherd-reader", daemon=True
        )
        flow.reader_thread = reader
        reader.start()
        return flow

    def _spawn_pty(self, mode: Mode, resolved_binary: str) -> _Flow:
        r"""Spawn `claude setup-token` under a PTY, reusing `pty_screen.PtyScreen` (#846).

        `claude setup-token` is a full TUI: it prints essentially nothing on a plain pipe
        and only renders its authorize URL under a real terminal, so it needs a real PTY
        rather than the `login` mode's `subprocess.PIPE`s. Builds the `PtyScreen` emulator
        FIRST — before opening the pty or spawning anything — so a missing `pyte` (the
        optional `pty` extra) fails closed with a clear, actionable message and leaves no
        subprocess and no flow behind. `start_new_session=True` gives the child its own
        session (mirroring `pty_keeper`'s detached-bridge spawn); `close_fds=True` keeps
        the child from inheriting unrelated open fds. The slave fd is closed in the
        parent right after spawn — the child holds its own dup via stdin/stdout/stderr,
        so the parent only needs the master to read/write the session.

        **Local echo is disabled on the slave BEFORE spawn (security-critical, not
        cosmetic).** A pty's line discipline echoes back whatever is written to it by
        default — unlike the `login` mode's plain `subprocess.PIPE`, where a write to
        `stdin` is never mirrored into `stdout`. Since `submit_code()` writes the
        operator-pasted code to this same fd (`os.write`, standing in for a human's
        keystrokes), an un-tweaked pty would echo that code straight back through the
        master into `flow.buffer` — and from there into the redacted-output failure
        message `_finalize_exited`/`start()` return to the caller. `_TOKEN_RE`-based
        redaction only masks a `CLAUDE_CODE_OAUTH_TOKEN=...` line, not an arbitrary
        pasted code, so that would be a real secret leak into the API response (and
        anywhere that response is logged upstream). Disabling `ECHO` closes it at the
        source — this fails closed too: a `termios` failure aborts the spawn rather
        than risk running with echo silently left on.

        **The pty is sized WIDE (`_LOGIN_PTY_COLS`), not left at the `os.openpty()`
        default of 80x24 (live-smoke-test regression, #846 follow-up).** Real `claude
        setup-token` wraps its authorize URL at the terminal's column width; at the
        default 80 cols a ~450-char URL wraps across multiple screen rows, and
        `PtyScreen.find_authorize_url()` (over `"\\n".join(display)`) then truncates it
        at the first wrap point. Sizing the slave's winsize wide keeps the URL on one
        rendered row, and `screen` is built at the SAME width (`PtyScreen(cols=...)`) so
        pyte doesn't re-wrap/re-truncate it down to its own default geometry. Best-effort
        like `pty_keeper`'s existing winsize ioctl (a failure here only risks the
        rare-but-now-defended-against wrap case, never a crash) — the ECHO-disable above
        stays fail-closed since it is security-critical, not cosmetic.
        """
        try:
            screen = PtyScreen(cols=_LOGIN_PTY_COLS, rows=SCREEN_ROWS, capture_osc8=True)
        except PyteUnavailableError as exc:
            raise LoginShepherdError(
                "the long-lived-token mode needs the `pty` extra (pyte) — use "
                f"subscription sign-in instead, or install it: {exc}"
            ) from exc

        # Windows has no `os.openpty`/`termios`; the setup-token TUI runs on a ConPTY instead
        # (#905). The `screen` above is transport-agnostic (both paths scan the pyte-rendered
        # screen for the URL/token), so hand it to the ConPTY spawn and return early.
        if _is_win32():
            return self._spawn_conpty(mode, resolved_binary, screen)

        import fcntl
        import struct
        import termios

        argv = [resolved_binary, "setup-token"]
        try:
            master_fd, slave_fd = os.openpty()
        except OSError as exc:
            raise LoginShepherdError(f"failed to open a pty for claude {mode}: {exc}") from exc
        try:
            fcntl.ioctl(
                slave_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", SCREEN_ROWS, _LOGIN_PTY_COLS, 0, 0),
            )
        except OSError as exc:
            # Best-effort, like pty_keeper's winsize ioctl: a failure here means the
            # slave stays at whatever default winsize the pty was opened with, so a very
            # long authorize URL *could* still wrap and get truncated — but it is never
            # fatal to the login flow, so log at debug and keep going rather than abort
            # the spawn (unlike the ECHO disable below, which IS security-critical).
            _log.debug("login_shepherd: failed to widen pty winsize for %s: %s", mode, exc)
        try:
            attrs = termios.tcgetattr(slave_fd)
            attrs[3] &= ~termios.ECHO  # lflags &= ~ECHO
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        except termios.error as exc:
            os.close(master_fd)
            os.close(slave_fd)
            raise LoginShepherdError(
                f"failed to disable pty echo for claude {mode}: {exc}"
            ) from exc
        try:
            proc = subprocess.Popen(  # noqa: S603 — list-argv, absolute binary, no shell
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
                env=procutil.child_env(),
            )
        except OSError as exc:
            # Popen never spawned (or failed after forking but before the child dup'd
            # the slave) — both fds are still ours alone here, so both must be reclaimed
            # on this one path. This is mutually exclusive with the success-path close
            # below (never both), so neither fd is ever double-closed.
            os.close(master_fd)
            os.close(slave_fd)
            raise LoginShepherdError(f"failed to start claude {mode}: {exc}") from exc
        # The child dup'd the slave onto its stdio; the parent never reads/writes it
        # directly and must close its own copy so the master sees EOF/EIO once the
        # child's copies are the last ones open (otherwise the parent's lingering fd
        # would keep the pty "half-open" and the read side would never signal EOF).
        os.close(slave_fd)

        flow = _Flow(mode=mode, proc=proc, screen=screen, master_fd=master_fd)
        reader = threading.Thread(
            target=_pump_pty, args=(flow,), name="login-shepherd-pty-reader", daemon=True
        )
        flow.reader_thread = reader
        reader.start()
        return flow

    def _spawn_conpty(self, mode: Mode, resolved_binary: str, screen: PtyScreen) -> _Flow:
        r"""Spawn `claude setup-token` on a Windows ConPTY (pywinpty) — win32 `_spawn_pty` (#905).

        Windows has no `os.openpty`/`termios`, so the setup-token TUI runs on a ConPTY
        pseudo-console via pywinpty, reusing `pty_keeper._load_pty_process` (the same seam the
        keeper's ConPTY backend uses, so this is testable on POSIX with a fake). The already-built
        `screen` is shared — ConPTY fragments the authorize URL with cursor escapes exactly like a
        TUI over a POSIX pty, so both paths scan the pyte-RENDERED screen. `PYWINPTY_BLOCK=0` makes
        `read()` non-blocking so `_pump_conpty` can also poll liveness. The `PtyProcess` is wrapped
        in `_ConPtyPopen` so `start`/`submit_code`/`poll`/`_teardown`'s `Popen`-shaped lifecycle
        runs unchanged.

        **No `termios` ECHO-disable here — the POSIX security backstop does not exist on Windows.**
        A ConPTY echoes written input back into its output as a property of the *child's* console
        input mode, which the parent keeper can't clear the way `_spawn_pty` clears the slave's
        `ECHO` lflag. So the operator-pasted code could be echoed into `flow.buffer`; the
        echo-redaction defense (`_write_code` registers the code in `flow.pasted_secrets`, and
        `_redact` masks it) keeps it out of any returned/logged surface instead. Spawn failure
        fails closed with a clear message and leaves no flow behind, mirroring the POSIX path.
        """
        from . import pty_keeper

        try:
            pty_process_cls = pty_keeper._load_pty_process()
        except Exception as exc:  # noqa: BLE001 — RuntimeError off-win32 / ImportError if absent
            raise LoginShepherdError(
                "the long-lived-token mode needs the `pty` extra (pywinpty) on Windows — use "
                f"subscription sign-in instead, or install it: {exc}"
            ) from exc
        # Non-blocking reads so the reader loop can poll liveness (as `pty_keeper` does); a
        # process-global toggle scoped to pywinpty, harmless to set unconditionally here.
        os.environ["PYWINPTY_BLOCK"] = "0"
        argv = [resolved_binary, "setup-token"]
        try:
            pty_process = pty_process_cls.spawn(
                argv,
                env=procutil.child_env(),
                dimensions=(SCREEN_ROWS, _LOGIN_PTY_COLS),
            )
        except Exception as exc:  # noqa: BLE001 — any pywinpty spawn error → fail closed, no flow
            raise LoginShepherdError(f"failed to start claude {mode}: {exc}") from exc

        # One lock guards every native `pty_process` call — the reader (`_pump_conpty`) and the
        # control thread (`_ConPtyPopen`/`_write_code`/`_teardown`) both touch this handle. See
        # `_ConPtyPopen`'s docstring for why (pywinpty concurrency) and why it can't deadlock.
        handle_lock = threading.Lock()
        flow = _Flow(
            mode=mode,
            proc=_ConPtyPopen(pty_process, handle_lock),
            screen=screen,
            pty_process=pty_process,
            pty_lock=handle_lock,
        )
        reader = threading.Thread(
            target=_pump_conpty, args=(flow,), name="login-shepherd-conpty-reader", daemon=True
        )
        flow.reader_thread = reader
        reader.start()
        return flow

    def start(self, mode: Mode) -> dict:
        """Spawn `claude auth login --claudeai` or `claude setup-token`.

        Blocks (synchronously — callers run this via `asyncio.to_thread`) until an
        authorize URL appears in the subprocess output, the process exits, or
        `START_TIMEOUT_SECONDS` elapses. Fails closed: if no URL ever appears, the
        raw captured output is returned in the raised error's message (never
        hung-forever) and the subprocess is reaped before raising.

        ``mode == "login"`` uses the verified plain-pipe transport (`_spawn_pipe`),
        unchanged. ``mode == "setup-token"`` is a full TUI (#846) that needs a real
        terminal to print anything, so it is spawned under a PTY (`_spawn_pty`) and its
        output is read through a `PtyScreen` instead of a plain-pipe buffer — see the
        module docstring. That mode fails closed with a clear message (never a crash)
        when the optional `pyte` dependency is unavailable, BEFORE any subprocess is
        spawned, so a `PyteUnavailableError` never leaves a phantom flow behind either.

        Raises `AlreadyActiveError` if a flow is already in progress.
        """
        with self._flow_lock:
            if self._flow is not None:
                raise AlreadyActiveError("a login flow is already in progress; cancel it first")
            try:
                resolved = claude_cli.resolve_binary(self._binary)
            except claude_cli.ClaudeNotFound as exc:
                # An unresolvable binary is a login-flow failure (→ 400 at the route),
                # not an unhandled 500. `self._flow` is still unset here — the fail path
                # never leaves a phantom active flow behind.
                raise LoginShepherdError(f"claude binary not found: {exc}") from exc

            flow = self._spawn(mode, resolved)
            self._flow = flow

        condition: Callable[[str], str | None]
        if flow.screen is not None:
            # setup-token PTY path: require the authorize URL to be STABLE across two polls,
            # so a URL caught mid-render (split across the reader's `feed()` chunks) is never
            # trusted as final (#852). The plain-pipe `login` reader is line-buffered — a URL
            # line only appears in the buffer once whole — so it needs no such guard.
            condition = _stable_match_finder(flow.screen.find_authorize_url)
        else:
            condition = _extract_authorize_url
        url, output, exited = _wait_for(flow, condition, timeout=START_TIMEOUT_SECONDS)
        if url is not None and not exited:
            # A URL appeared while the process still looked alive — but `_wait_for` breaks
            # the instant a URL matches, and `poll()` can lag a just-returned `exit()` by a
            # scheduling tick, so a process that printed a URL then immediately died can read
            # as still-running for that one iteration. Confirm liveness with a short bounded
            # wait before trusting the URL: a genuine login BLOCKS on stdin and rides out the
            # grace (still running → usable), while a mid-exit process is reaped within it →
            # exited=True → fail closed. Makes the "URL then died" case deterministic, not racy.
            try:
                flow.proc.wait(timeout=_URL_LIVENESS_GRACE_SECONDS)
                exited = True
            except subprocess.TimeoutExpired:
                pass
        # Fail closed whenever the process has EXITED during the start wait — even if a URL
        # was found. A process that printed a URL and then died lands here with url set AND
        # exited=True (the liveness settle above guarantees that exit is observed); handing
        # that URL back would strand the operator (they'd authorize, then `submit_code` would
        # find no live stdin). Only a URL found while the process is STILL RUNNING (blocked on
        # stdin, exited=False) is a usable login — returning a URL for a dead subprocess is
        # structurally impossible.
        if url is None or exited:
            self._teardown(flow)
            if exited:
                # `_teardown` joined the reader thread, so a final in-flight line is now
                # captured — refresh the snapshot so the error message shows the real
                # last output.
                output = flow.snapshot()
            if exited and url is not None:
                reason = "the process exited before the login could proceed"
            elif exited:
                reason = "the process exited before printing an authorize URL"
            else:
                reason = f"no authorize URL appeared within {START_TIMEOUT_SECONDS:.0f}s"
            raise LoginShepherdError(
                f"claude {mode} did not produce a usable login: {reason}. "
                f"Captured output:\n{_redact(output, flow.pasted_secrets)}"
            )
        return {"authorize_url": url, "output": _redact(output, flow.pasted_secrets)}

    def submit_code(self, code: str) -> dict:
        """Write the operator-pasted `code` to the active subprocess's stdin.

        Waits (bounded by `SUBMIT_TIMEOUT_SECONDS`) for the login to react, then keys
        the outcome off a FRESH `poll()`, never a stale wait flag:

        * **Exited** (`poll()` is not None): classify success by the exit code, extract
          `setup-token`'s printed `CLAUDE_CODE_OAUTH_TOKEN=...` (the ONE time it is
          surfaced), tear the flow down, and return the result. A process that exited
          right at the wait boundary is thus correctly seen as done, not failed.
        * **Still running** (`poll()` is None after the timeout): the login is genuinely
          in-flight (a slow provider verification can exceed the wait). Do NOT tear it
          down / kill it — leave the flow active and return a "still verifying" result
          (`pending: true`) so the caller can poll :meth:`poll` for the eventual result,
          or `cancel()` explicitly. Single-flight still blocks a new `start()` while it runs.

        Never logs `code` or the extracted token. Raises `NotActiveError` if no flow is
        in progress. Serialized per-flow (``flow.submit_lock``) so two concurrent submits
        can't interleave writes to the same stdin: the second blocks, then finds the flow
        already reaped and re-raises `NotActiveError`.
        """
        with self._flow_lock:
            flow = self._flow
        if flow is None:
            raise NotActiveError("no login flow is in progress")

        with flow.submit_lock:
            # Re-check under the per-flow lock: a concurrent submit may have already
            # driven this flow to a terminal outcome and torn it down while we waited.
            # Defensive concurrency guard — deterministically exercising the race that
            # trips it isn't practical with a C-level lock, so the reject arm is
            # pragma-excluded rather than tested with a brittle timing hack.
            with self._flow_lock:
                if self._flow is not flow:  # pragma: no cover - concurrent-teardown guard
                    raise NotActiveError("no login flow is in progress")

            self._write_code(flow, code)

            # Wait until the process exits, then read a FRESH poll() — never trust the
            # wait's own `exited` flag, which can lag a poll() that already reports the
            # exit code (the timeout-boundary race).
            _wait_for(flow, lambda _snap: None, timeout=SUBMIT_TIMEOUT_SECONDS)
            exit_code = flow.proc.poll()
            if exit_code is None:
                # Still running after the timeout: a slow-but-valid verification. Leave the
                # flow ACTIVE (don't kill it) so a genuine login isn't aborted; the operator
                # can wait + re-check via `poll()` or cancel. `pending: true` keeps the UI in
                # the IN-PROGRESS state instead of "finished" (which would orphan the flow).
                return self._pending_result(flow)
            # Exited: build the terminal result (drains the reader, extracts the token,
            # classifies off the real exit code) and reap the flow.
            return self._finalize_exited(flow, exit_code)

    def poll(self) -> dict:
        """Re-check the active flow's outcome without submitting a code (the `pending` poll).

        After :meth:`submit_code` returns ``pending: true`` (a slow verification), the caller
        polls this to fetch the eventual result:

        * **Still running** (`poll()` is None): returns the same ``pending: true`` shape.
        * **Completed** (`poll()` is not None): builds the TERMINAL result — extracts
          `setup-token`'s token, classifies by exit code — reaps the flow, and returns it
          (no ``pending``). This is what both surfaces the eventual result AND reaps the
          completed flow so it is no longer active-forever.

        Raises :class:`NotActiveError` if no flow is in progress (→ 409: stop polling).
        Serialized per-flow (``flow.submit_lock``) like :meth:`submit_code`, so a poll and a
        concurrent submit/poll can't race on the same process; a poll that loses the race
        finds the flow reaped and re-raises `NotActiveError`.
        """
        with self._flow_lock:
            flow = self._flow
        if flow is None:
            raise NotActiveError("no login flow is in progress")

        with flow.submit_lock:
            with self._flow_lock:
                if self._flow is not flow:  # pragma: no cover - concurrent-teardown guard
                    raise NotActiveError("no login flow is in progress")
            exit_code = flow.proc.poll()
            if exit_code is None:
                return self._pending_result(flow)
            return self._finalize_exited(flow, exit_code)

    def _write_code(self, flow: _Flow, code: str) -> None:
        """Write the operator-pasted `code` to `flow`'s active subprocess.

        Branches on transport: a Windows ConPTY flow (`flow.pty_process` set) writes through
        pywinpty's `PtyProcess.write`; a POSIX PTY flow (`flow.master_fd` set) writes raw bytes
        to the pty master with `os.write` (what a human's terminal keystrokes would do); a
        plain-pipe flow writes through `proc.stdin` as before. Every side swallows a failed write
        (the subprocess may have already exited) and surfaces it as a log warning rather than
        raising past the caller — never logs `code` itself.

        Registers `code` in `flow.pasted_secrets` FIRST — before any write — so `_redact` can
        scrub it from returned/logged output even if the write only partially lands. Load-bearing
        on the ConPTY transport (which echoes the write back into the read stream, see `_redact`)
        and harmless defense-in-depth on POSIX/pipe. Empty codes are never registered so the
        `str.replace` in `_redact` is never handed the empty string.
        """
        if code:
            flow.pasted_secrets.append(code)
        if flow.pty_process is not None:
            if flow.stdin_closed:
                return
            try:
                with flow.pty_lock:  # serialize with the reader/teardown (see `_ConPtyPopen`)
                    flow.pty_process.write(code + "\n")
            except Exception as exc:  # noqa: BLE001 — a dead ConPTY can raise pywinpty-specific errors
                _log.warning(
                    "login_shepherd: writing code to %s conpty failed: %s", flow.mode, exc
                )
            return
        if flow.master_fd is not None:
            if flow.stdin_closed:
                return
            try:
                os.write(flow.master_fd, (code + "\n").encode())
            except OSError as exc:
                _log.warning("login_shepherd: writing code to %s pty failed: %s", flow.mode, exc)
            return
        try:
            if flow.proc.stdin is not None and not flow.stdin_closed:
                flow.proc.stdin.write(code + "\n")
                flow.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            # The subprocess may have already exited (closed pipe) — surface as a
            # failure rather than raising an unhandled error to the caller.
            _log.warning("login_shepherd: writing code to %s stdin failed: %s", flow.mode, exc)

    def _pending_result(self, flow: _Flow) -> dict:
        """Build the "still verifying" (in-progress) result; leaves the flow ACTIVE."""
        message = (
            f"claude {flow.mode} is still verifying — the login is still running "
            f"(no result within {SUBMIT_TIMEOUT_SECONDS:.0f}s). Wait and re-check, or cancel."
        )
        return {"ok": False, "pending": True, "message": message}

    def _finalize_exited(self, flow: _Flow, exit_code: int) -> dict:
        """Build the TERMINAL result for an exited flow, then reap it.

        Drains the reader (its last `readline()`/pty read may still be in flight in the
        instant poll() first reported exit) so a final line — e.g. setup-token's token —
        isn't missed, classifies off the real exit code, extracts the token, tears the
        flow down, and returns ``{"ok": bool, "message": str, "token"?: str}`` (never
        ``pending``). For a PTY flow the token is scraped from the pyte-RENDERED screen
        (`flow.screen.find_oauth_token()`), the same reassembly `find_authorize_url` uses
        — the raw pty byte stream can fragment the token line with cursor-positioning
        escapes exactly like it does the authorize URL, so the plain-pipe regex-over-
        buffer approach isn't reliable there. Never logs the token. Called only under
        ``flow.submit_lock``.
        """
        if flow.reader_thread is not None:
            flow.reader_thread.join(timeout=2)
        output = flow.snapshot()
        ok = exit_code == 0
        token = flow.screen.find_oauth_token() if flow.screen is not None else None
        if token is None:
            token_match = _TOKEN_RE.search(output)
            token = token_match.group(1) if token_match else None

        self._teardown(flow)

        if ok:
            message = (
                "Login succeeded."
                if flow.mode == "login"
                else (
                    "Token created."
                    if token
                    else "setup-token exited successfully, but no "
                    "token was found in its output — check `claude auth status --json`."
                )
            )
        else:
            message = (
                f"claude {flow.mode} exited with code {exit_code}. "
                f"Captured output:\n{_redact(output, flow.pasted_secrets)}"
            )
        result: dict = {"ok": ok, "message": message}
        if token:
            result["token"] = token
        return result

    def cancel(self) -> None:
        """Terminate and reap the active subprocess (if any); always safe to call."""
        with self._flow_lock:
            flow = self._flow
            self._flow = None
        if flow is not None:
            self._teardown(flow, already_cleared=True)

    def _teardown(self, flow: _Flow, *, already_cleared: bool = False) -> None:
        """Terminate + reap `flow`'s subprocess and clear it as the active flow.

        Teardown order is portability-critical for the PTY transport. The pty reader
        thread blocks in ``os.read(master_fd)``, and closing that fd from another thread
        does NOT interrupt an in-flight read on macOS/BSD (it does on Linux). Closing the
        master while the reader is mid-read would therefore leak that thread — and its fd,
        which a later ``openpty()`` can reuse, cross-wiring a subsequent flow's reader onto
        stale bytes (the macOS CI hang: the whole xdist worker stalled on the pileup).
        So the child is stopped FIRST: its exit closes the pty slave, and a *still-open*
        master then reliably returns EOF/EIO on every POSIX platform, unblocking the
        reader. Only AFTER the reader has joined is our control fd closed — the pty master,
        or (for a plain-pipe flow, whose reader watches stdout and so was never affected)
        the child's stdin. Closing it last means it is never pulled from under a live read.

        The Windows ConPTY reader (`_pump_conpty`) isn't blocked on a read — pywinpty reads are
        non-blocking and it polls `isalive()` — so it exits on its own once the child dies; the
        same stop-child-first-then-join-then-close order still holds, and the pywinpty handle is
        closed last (in place of the pty master). Its `close()` is broadly guarded below since a
        stale ConPTY can raise a pywinpty-specific error, not just `OSError`.
        """
        # Stop the child FIRST — for a PTY flow this is what unblocks the reader: closing
        # master_fd out from under a blocked os.read does not interrupt it on macOS/BSD,
        # but the child's exit closes the pty slave and a still-open master then returns
        # EOF/EIO on every POSIX platform. (A plain-pipe reader watches the child's stdout,
        # which the child's exit likewise EOFs — it was never affected.)
        if flow.proc.poll() is None:
            flow.proc.terminate()
            try:
                flow.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                flow.proc.kill()
                # Swallow a second timeout too: `_ConPtyPopen.wait` genuinely raises
                # `TimeoutExpired` (unlike a POSIX `Popen` after SIGKILL, which reaps at once),
                # and an unguarded raise here would skip the reader join, the control-end close,
                # AND the `self._flow = None` clear below — stranding the flow `active` forever.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    flow.proc.wait(timeout=5)
        # The reader is now unblocked by the child's EOF/EIO on the still-open fd.
        if flow.reader_thread is not None:
            flow.reader_thread.join(timeout=5)
        # Close our write/control end LAST — the ConPTY handle, else the pty master, else the
        # plain-pipe stdin — once the reader has joined, so it is never pulled from under an
        # active read. `stdin_closed` guards idempotency (and doubles as "no more writes owed").
        try:
            if flow.pty_process is not None:
                if not flow.stdin_closed:
                    flow.stdin_closed = True
                    with flow.pty_lock:  # serialize the close with any last reader access
                        flow.pty_process.close()
            elif flow.master_fd is not None:
                if not flow.stdin_closed:
                    flow.stdin_closed = True
                    os.close(flow.master_fd)
            elif flow.proc.stdin is not None:
                flow.stdin_closed = True
                flow.proc.stdin.close()
        except Exception as exc:  # noqa: BLE001 — a teardown close (incl. pywinpty) must never raise
            # Never silently: a close failure can't be surfaced to the caller mid-teardown, but
            # log it at debug so it isn't wholly invisible (the flow is cleared either way).
            _log.debug("login_shepherd: closing the %s control end failed: %s", flow.mode, exc)
        if not already_cleared:
            with self._flow_lock:
                if self._flow is flow:
                    self._flow = None


def _pump_stdout(flow: _Flow) -> None:
    """Background-thread target: append subprocess stdout to `flow`'s buffer until EOF."""
    stdout = flow.proc.stdout
    if stdout is None:
        return
    try:
        for line in stdout:
            flow.append(line)
    except (OSError, ValueError):
        # The pipe can close out from under us during teardown — nothing more to read.
        pass


def _pump_pty(flow: _Flow) -> None:
    """Background-thread target: feed `flow.master_fd`'s raw bytes into its `PtyScreen`.

    The `setup-token` PTY reader (#846): reads raw bytes off the pty master and
    `screen.feed()`s them (so `find_authorize_url`/`find_oauth_token` can scan the
    pyte-RENDERED screen), and also best-effort-decodes+appends the same bytes to
    `flow.buffer` so `snapshot()`/`_redact()` — used for error messages and the
    fail-closed no-URL path — keep working uniformly across both transports.

    **PTY EOF/EIO edge case (POSIX, known and handled):** once the child exits and
    closes its end, `os.read` on a pty master typically raises `OSError(EIO)` rather
    than returning `b""` (a plain pipe's clean-EOF signal) — some platforms/kernels may
    still return `b""` first. Both are treated as end-of-stream: an `OSError` is caught
    and inspected (only `EIO` is expected/benign here; any other `OSError` is also
    treated as end-of-stream rather than crashing this daemon thread, since a reader
    thread has no one to propagate an exception to), and an empty read breaks the loop
    the same way a plain pipe's EOF would. Either path exits the loop cleanly — never
    raises past this thread — so a normal child exit can never look like a crash.
    """
    master_fd = flow.master_fd
    screen = flow.screen
    if master_fd is None or screen is None:  # pragma: no cover - defensive, always paired
        return
    while True:
        try:
            chunk = os.read(master_fd, 65536)
        except OSError as exc:
            # EIO is the expected "child exited, pty half-closed" signal on Linux; any
            # other OSError (e.g. the fd was already closed by a concurrent teardown)
            # is likewise treated as end-of-stream rather than propagated — a reader
            # thread crashing has no caller to observe it, and the flow is being torn
            # down either way.
            if exc.errno not in (errno.EIO,):
                _log.debug("login_shepherd: pty read ended for %s: %s", flow.mode, exc)
            break
        if not chunk:
            break
        try:
            screen.feed(chunk)
        except Exception as exc:  # noqa: BLE001 — a render hiccup must never kill the reader
            _log.debug("login_shepherd: pty screen feed failed for %s: %s", flow.mode, exc)
        flow.append(chunk.decode("utf-8", errors="replace"))


def _pump_conpty(flow: _Flow) -> None:
    """Background-thread reader for the Windows ConPTY transport (#905, analogue of `_pump_pty`).

    pywinpty's non-blocking `PtyProcess.read()` (with `PYWINPTY_BLOCK=0`) returns a `str` and
    surfaces both EOF and — on some builds — an idle no-data read as `EOFError`, unlike the
    POSIX `os.read` bytes/`EIO` contract `_pump_pty` handles. Each chunk is `screen.feed()`d
    (so `find_authorize_url`/`find_oauth_token` scan the pyte-RENDERED screen — ConPTY fragments
    the URL with cursor escapes) and best-effort-appended to `flow.buffer` for
    `snapshot()`/`_redact()`, matching `_pump_pty`'s uniform buffer.

    The loop ends only when the process is no longer alive: an idle `EOFError`/empty read on a
    still-alive process yields and keeps polling, while the same on a dead one breaks. Every
    native handle call (`read`/`isalive`) takes `flow.pty_lock` so it never overlaps the control
    thread's `terminate`/`wait`/`close` on the same handle (see `_ConPtyPopen`). It is also
    extra-defensive about read errors — a read can still fail with a pywinpty-specific error, not
    just `OSError`; ANY such error is treated as end-of-stream and breaks the loop rather than
    propagating (a daemon reader has no caller to observe an exception).
    """
    pty_process = flow.pty_process
    screen = flow.screen
    if pty_process is None or screen is None:  # pragma: no cover - defensive, always paired
        return
    lock = flow.pty_lock  # serialize native handle access with the control thread
    while True:
        try:
            with lock:
                data = pty_process.read(65536)  # str; "" when no data (PYWINPTY_BLOCK=0)
        except EOFError:
            # EOF, or an idle non-blocking read some pywinpty builds surface as EOFError: a
            # still-alive process is merely idle (keep polling); a dead one is drained → break.
            with lock:
                alive = pty_process.isalive()
            if not alive:
                break
            data = ""
        except Exception as exc:  # noqa: BLE001 — genuine read error or ConPTY closed under us
            _log.debug("login_shepherd: conpty read ended for %s: %s", flow.mode, exc)
            break
        if not data:
            with lock:
                alive = pty_process.isalive()
            if not alive:
                break
            time.sleep(_POLL_INTERVAL_SECONDS)  # alive but idle; yield before re-polling
            continue
        try:
            screen.feed(data.encode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 — a render hiccup must never kill the reader
            _log.debug("login_shepherd: conpty screen feed failed for %s: %s", flow.mode, exc)
        flow.append(data)


def _stable_match_finder(find: Callable[[], _T | None]) -> Callable[[str], _T | None]:
    """Adapt a zero-arg finder into a `_wait_for` condition that fires only on a STABLE match.

    A match is reported only once the finder returns the same value on two consecutive polls.
    The setup-token PTY reader feeds the pty in `os.read` chunks, so a poll can land between
    two `feed()`s and see a syntactically-complete-but-TRUNCATED authorize URL mid-render;
    `_wait_for` would latch that first match and hand back a broken URL (#852). Requiring the
    same value on two consecutive polls rejects a still-growing partial (its length keeps
    changing) and trusts only the settled line — the reader stops feeding once `claude
    setup-token` has drawn the URL and blocked on stdin, so the final URL repeats and is
    accepted one poll (~`_POLL_INTERVAL_SECONDS`) later. The `_snapshot` arg is ignored:
    unlike the plain-pipe reader (which scans the captured buffer), this finder reads the
    pyte-rendered screen directly.
    """
    prev: list[_T | None] = [None]

    def _condition(_snapshot: str) -> _T | None:
        current = find()
        was, prev[0] = prev[0], current
        return current if current is not None and current == was else None

    return _condition


def _wait_for(
    flow: _Flow, condition: Callable[[str], _T | None], *, timeout: float
) -> tuple[_T | None, str, bool]:
    """Poll `flow`'s captured output against `condition(snapshot)` until match/exit/timeout.

    `condition` receives the buffer captured so far and returns a truthy match (or
    None to keep waiting). Stops early once the subprocess has exited, even if
    `condition` never matches (e.g. it crashed before printing anything). Returns
    `(match, captured_output_so_far, process_exited)` — the caller always gets the
    final snapshot regardless of which condition fired.
    """
    deadline = time.monotonic() + timeout
    match: _T | None = None
    exited = False
    snapshot = ""
    while time.monotonic() < deadline:
        snapshot = flow.snapshot()
        match = condition(snapshot)
        exited = flow.proc.poll() is not None
        if match or exited:
            break
        time.sleep(_POLL_INTERVAL_SECONDS)
    else:
        # The deadline elapsed with no match/exit. Give the condition ONE final look so a
        # value first observed on the last in-window poll is not lost to the deadline: a
        # stability-gated condition (the setup-token URL finder, #852) needs a second
        # observation to confirm, and without this the confirming poll would fall just past
        # the deadline and an already-visible URL would be discarded as "never appeared"
        # (#856). Cheap and idempotent for the stateless plain-pipe condition.
        snapshot = flow.snapshot()
        match = condition(snapshot)
        exited = flow.proc.poll() is not None
    return match, snapshot, exited
