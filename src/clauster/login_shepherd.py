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

import errno
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypeVar

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


def _redact(text: str) -> str:
    """Best-effort mask of a token-shaped value in captured output before it is logged.

    Defense in depth only: the primary guarantee is that the pasted code and the
    parsed token are never themselves passed to a logging call. This additionally
    scrubs a `CLAUDE_CODE_OAUTH_TOKEN=...` line (in case the raw subprocess output
    ever needs to be logged for diagnostics) so the secret value never lands in a
    log line even indirectly.
    """
    return _TOKEN_RE.sub("CLAUDE_CODE_OAUTH_TOKEN=<redacted>", text)


@dataclass
class _Flow:
    """State for the single active login subprocess.

    ``screen``/``master_fd`` are set ONLY for a `setup-token` PTY flow (#846); both are
    None for a plain-pipe `login` flow. Their presence is the single source of truth for
    which transport a flow uses — every read/write/teardown site below branches on
    ``flow.screen is not None`` rather than carrying a second is-pty flag that could drift
    out of sync with it.
    """

    mode: Mode
    proc: subprocess.Popen
    lock: threading.Lock = field(default_factory=threading.Lock)
    submit_lock: threading.Lock = field(default_factory=threading.Lock)
    buffer: list[str] = field(default_factory=list)
    reader_thread: threading.Thread | None = None
    stdin_closed: bool = False
    screen: PtyScreen | None = None
    master_fd: int | None = None

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
            screen = PtyScreen(cols=_LOGIN_PTY_COLS, rows=SCREEN_ROWS)
        except PyteUnavailableError as exc:
            raise LoginShepherdError(
                "the long-lived-token mode needs the `pty` extra (pyte) — use "
                f"subscription sign-in instead, or install it: {exc}"
            ) from exc

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

        condition = (
            (lambda _snap: flow.screen.find_authorize_url())  # type: ignore[union-attr]
            if flow.screen is not None
            else _extract_authorize_url
        )
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
                f"Captured output:\n{_redact(output)}"
            )
        return {"authorize_url": url, "output": _redact(output)}

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

        Branches on transport: a PTY flow (`flow.master_fd` set) writes raw bytes to the
        pty master with `os.write` (what a human's terminal keystrokes would do); a
        plain-pipe flow writes through `proc.stdin` as before. Both sides swallow a
        failed write (the subprocess may have already exited) and surface it as a log
        warning rather than raising past the caller — never logs `code` itself.
        """
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
                f"Captured output:\n{_redact(output)}"
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

        For a PTY flow this also closes `flow.master_fd` (once, guarded by
        `stdin_closed` — it doubles as "no more writes/close-owed" for both transports)
        so the pty is never leaked across a teardown.
        """
        try:
            if flow.master_fd is not None:
                if not flow.stdin_closed:
                    flow.stdin_closed = True
                    os.close(flow.master_fd)
            elif flow.proc.stdin is not None:
                flow.stdin_closed = True
                flow.proc.stdin.close()
        except (OSError, ValueError):
            pass
        if flow.proc.poll() is None:
            flow.proc.terminate()
            try:
                flow.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                flow.proc.kill()
                flow.proc.wait(timeout=5)
        if flow.reader_thread is not None:
            flow.reader_thread.join(timeout=5)
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
    return match, snapshot, exited
