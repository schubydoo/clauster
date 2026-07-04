"""Dashboard-driven `claude` account login shepherd (#839).

Today the only fix for a logged-out (or token-expired) runtime `claude` account is
to SSH in as the runtime user and run `claude auth login` interactively. This
module lets an operator complete that same login from the browser instead, by
owning a single long-lived `claude auth login` / `claude setup-token` subprocess,
streaming its output, and forwarding the operator-pasted OAuth code back to its
stdin — exactly what a human would type over SSH.

Two modes, both interactive OAuth flows that print an authorize `https://…` URL
and then read a pasted code back from stdin (no pty required — plain pipes work,
per the verified spike):

* ``login``       — ``claude auth login --claudeai`` (Claude subscription).
* ``setup-token`` — ``claude setup-token``, which additionally prints a
  long-lived ``CLAUDE_CODE_OAUTH_TOKEN=...`` on success. Its exact prompt/output
  shape is NOT assumed here — the reader is generic (scan for a URL, scan for a
  token pattern) so it degrades gracefully if the real CLI's wording differs from
  what the spike observed.

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

import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypeVar
from urllib.parse import urlsplit

from . import claude_cli, procutil, redact

_T = TypeVar("_T")

_log = logging.getLogger("clauster.login_shepherd")

Mode = Literal["login", "setup-token"]

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

# The authorize URL `claude auth login`/`setup-token` prints for the operator to open.
# Deliberately greedy-then-trimmed: grab the whole https run, then `_clean_url` strips
# trailing punctuation/quotes the CLI may print around it (see `_extract_authorize_url`).
_URL_RE = re.compile(r"https://\S+")

# Host suffixes that identify a genuine Claude/Anthropic OAuth authorize URL. Used to
# PREFER the real authorize link when the CLI prints more than one https URL (e.g. a docs
# link first). Suffix-matched (`endswith`) so subdomains like `console.anthropic.com`
# count. Not a hard requirement — the real-CLI host is unverified, so a no-match falls
# back to the LAST https URL rather than failing.
_KNOWN_AUTH_HOST_SUFFIXES = ("claude.ai", "anthropic.com")

# Trailing characters the CLI may print immediately after a URL (sentence punctuation,
# closing brackets/quotes) that are not part of the URL itself.
_URL_TRAILING = ".,;:!?)]}>\"'"

# `setup-token`'s printed long-lived credential (per the spike: `CLAUDE_CODE_OAUTH_TOKEN=...`).
# Matched permissively (any non-whitespace value) since the exact real-binary format is
# unverified — this is defensive, not assumed exact.
_TOKEN_RE = re.compile(r"CLAUDE_CODE_OAUTH_TOKEN=(\S+)")


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
    """Whether ``host`` is (a subdomain of) a known Claude/Anthropic auth host."""
    return any(
        host == suffix or host.endswith("." + suffix) for suffix in _KNOWN_AUTH_HOST_SUFFIXES
    )


def _extract_authorize_url(output: str) -> str | None:
    """Return the best authorize URL in ``output``, or None if none is present.

    Robust against the ways a real CLI's terminal output can mangle a URL:

    * ANSI/terminal escape sequences are stripped first (via :func:`redact.strip_ansi`)
      so a colored/reset-wrapped URL isn't polluted with escape bytes.
    * Each matched ``https://…`` token is trimmed of trailing punctuation/quotes.
    * When several https URLs are present, one whose host ends in a known Claude/Anthropic
      auth host wins over an arbitrary first match (a decoy docs link printed first can't
      hijack the operator); with no known-host match it falls back to the *last* https URL
      (the CLI prints the actionable link last far more often than first).

    Deliberately defensive, not host-locked: the real-CLI format is unverified, so a URL
    on an unknown host is still returned rather than rejected.
    """
    cleaned_output = redact.strip_ansi(output)
    candidates = [_clean_url(m.group(0)) for m in _URL_RE.finditer(cleaned_output)]
    candidates = [c for c in candidates if c]
    if not candidates:
        return None
    for url in candidates:
        if _is_known_auth_host(_url_host(url)):
            return url
    return candidates[-1]


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
    """State for the single active login subprocess."""

    mode: Mode
    proc: subprocess.Popen
    lock: threading.Lock = field(default_factory=threading.Lock)
    submit_lock: threading.Lock = field(default_factory=threading.Lock)
    buffer: list[str] = field(default_factory=list)
    reader_thread: threading.Thread | None = None
    stdin_closed: bool = False

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

    def start(self, mode: Mode) -> dict:
        """Spawn `claude auth login --claudeai` or `claude setup-token`.

        Blocks (synchronously — callers run this via `asyncio.to_thread`) until an
        authorize URL appears in the subprocess output, the process exits, or
        `START_TIMEOUT_SECONDS` elapses. Fails closed: if no URL ever appears, the
        raw captured output is returned in the raised error's message (never
        hung-forever) and the subprocess is reaped before raising.

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
            argv = (
                [resolved, "auth", "login", "--claudeai"]
                if mode == "login"
                else [
                    resolved,
                    "setup-token",
                ]
            )
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
            self._flow = flow

        url, output, exited = _wait_for(
            flow, _extract_authorize_url, timeout=START_TIMEOUT_SECONDS
        )
        # Fail closed whenever the process has EXITED during the start wait — even if a
        # URL was found. `_wait_for` breaks on a URL match OR process exit, so a process
        # that printed a URL and then died lands here with url set AND exited=True; handing
        # that URL back would strand the operator (they'd authorize, then `submit_code`
        # would find no live stdin). Only a URL found while the process is STILL RUNNING
        # (blocked on stdin, exited=False) is a usable login. This makes returning a URL
        # for a dead subprocess structurally impossible.
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
          down / kill it — leave the flow active and return a "still verifying" result so
          the operator can wait and re-check, or `cancel()` explicitly. Single-flight
          still blocks a new `start()` while it runs.

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

            try:
                if flow.proc.stdin is not None and not flow.stdin_closed:
                    flow.proc.stdin.write(code + "\n")
                    flow.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                # The subprocess may have already exited (closed pipe) — surface as a
                # failure rather than raising an unhandled error to the caller.
                _log.warning("login_shepherd: writing code to %s stdin failed: %s", flow.mode, exc)

            # Wait until the process exits, then read a FRESH poll() — never trust the
            # wait's own `exited` flag, which can lag a poll() that already reports the
            # exit code (the timeout-boundary race).
            _wait_for(flow, lambda _snap: None, timeout=SUBMIT_TIMEOUT_SECONDS)
            exit_code = flow.proc.poll()

            if exit_code is None:
                # Still running after the timeout: a slow-but-valid verification. Leave the
                # flow ACTIVE (don't kill it) so a genuine login isn't aborted; the operator
                # can wait + re-check or cancel. Return without tearing down.
                message = (
                    f"claude {flow.mode} is still verifying — the login is still running "
                    f"(no result within {SUBMIT_TIMEOUT_SECONDS:.0f}s). Wait and re-check "
                    "`claude auth status --json`, or cancel."
                )
                return {"ok": False, "message": message}

            # Exited: drain the reader (its last `readline()` may still be in flight in the
            # instant poll() first reported exit) so a final line — e.g. setup-token's token
            # — isn't missed, then classify off the real exit code.
            if flow.reader_thread is not None:
                flow.reader_thread.join(timeout=2)
            output = flow.snapshot()
            ok = exit_code == 0
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
        """Terminate + reap `flow`'s subprocess and clear it as the active flow."""
        try:
            if flow.proc.stdin is not None:
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
