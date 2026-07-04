"""Detect whether the runtime ``claude`` account is actually logged in (#838).

``/healthz`` already reports ``claude_ok`` — the ``claude`` binary is invokable —
but not whether the account is authenticated. An expired or absent login lets a
bridge spawn and then hang at "Starting" with no upfront signal, because the
subprocess itself is fine; only its auth is stale.

The authoritative, mechanism-agnostic signal is ``claude auth status --json``:
its ``loggedIn`` reflects *whichever* auth mechanism is in effect — claude.ai
OAuth, ``apiKeyHelper``, ``ANTHROPIC_API_KEY``, ``CLAUDE_CODE_OAUTH_TOKEN``, or a
console/API key. Parsing ``~/.claude/.credentials.json`` directly would only
detect the OAuth-subscription path and would false-alarm "not logged in" on a
perfectly-authenticated API-key/helper deployment, which is worse than no signal
— so this deliberately drives the CLI instead.

Fails closed: a command error, timeout, non-zero exit, or non-JSON output all
yield ``logged_in=False`` with a ``reason`` — never raises out to the ``/healthz``
caller. Only non-PII fields are surfaced (``loggedIn`` + ``authMethod``); the
CLI's ``email`` / ``orgId`` / ``orgName`` / ``subscriptionType`` are PII and this
repo is public, so they are never read, logged, or returned. The OAuth token
value is likewise never logged or returned.

The optional ``expires_at_ms`` is a proactive extra: only when the auth method is
claude.ai OAuth *and* ``.credentials.json`` exists is its ``expiresAt`` read (to
warn of imminent OAuth expiry). ``loggedIn`` is always the core signal.

Because the probe is a subprocess (up to a few hundred ms, and capped at a 5s
timeout), ``/healthz`` must never run it inline — the dashboard's 4s
``Promise.all`` poll awaits ``/healthz``, so a slow probe would stall every other
panel, and each poll × each open tab would spawn an overlapping subprocess.
:class:`LoginStatusCache` fixes both: ``/healthz`` reads a cached value that is
served immediately (stale-while-revalidate) and the probe runs at most once per
TTL, single-flight, on a background thread — never on the request path.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import claude_cli, procutil

_log = logging.getLogger("clauster.login_status")

#: Bound the ``claude auth status`` probe so a wedged CLI can't hang the background
#: refresh thread forever. The command is local (no network round-trip) and returns
#: in well under a second on a healthy host; 5s is generous headroom before we fail
#: closed. (This bounds the background thread, not ``/healthz`` — the request path
#: never waits on the probe.)
_AUTH_STATUS_TIMEOUT_S = 5.0

#: How long a cached :class:`LoginStatus` is served before a read triggers a single
#: background refresh. A stale value is still returned during the refresh
#: (stale-while-revalidate), so ``/healthz`` stays fast and the subprocess runs at
#: most once per this window regardless of poll rate or tab count.
_CACHE_TTL_S = 30.0

#: ``authMethod`` values that mean the login is a claude.ai OAuth subscription, for
#: which ``.credentials.json`` carries an ``expiresAt`` worth surfacing. Any other
#: method (apiKeyHelper / apiKey / console / env token) has no such file to read.
_OAUTH_METHODS = frozenset({"claude.ai", "claudeai", "oauth"})


@dataclass(frozen=True)
class LoginStatus:
    """Result of probing ``claude auth status`` for the runtime account.

    ``logged_in`` is the mechanism-agnostic core signal. ``method`` is the
    non-PII ``authMethod`` string (or ``None`` when unknown / the probe failed).
    ``expires_at_ms`` is populated only for a claude.ai OAuth login whose
    ``.credentials.json`` was readable; it is ``None`` for every other method and
    on every fail-closed path.

    ``known`` distinguishes a *real* probe result from the neutral "haven't probed
    yet" placeholder (:func:`unknown`). A caller must only raise a "not logged in"
    alarm when ``known`` is true and ``logged_in`` is false — an unknown state is
    reported as ``logged_in=True`` precisely so a cold start never cries wolf.
    """

    logged_in: bool
    method: str | None
    expires_at_ms: int | None
    reason: str
    known: bool = True


def unknown() -> LoginStatus:
    """Return the neutral "probe hasn't completed yet" status (renders quiet).

    ``logged_in=True`` + ``known=False``: ``/healthz`` reports the account as OK
    (so the dashboard shows no logged-out pill) while signalling that this is not
    yet a confirmed result. Used for the cache's cold-start value before the first
    background probe lands.
    """
    return LoginStatus(True, None, None, "login status not yet determined", known=False)


def _oauth_expires_at_ms(claude_json: Path) -> int | None:
    """Return the OAuth token's ``expiresAt`` (ms epoch) from ``.credentials.json``.

    Best-effort and fail-quiet: this only enriches the result with a proactive
    expiry warning, so any problem (missing file, unreadable, malformed, missing
    field, non-int) simply yields ``None`` rather than affecting the login verdict
    or raising. The token value itself is never read into a returned field or log.
    """
    creds_path = claude_json.parent / ".claude" / ".credentials.json"
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    oauth = oauth if isinstance(oauth, dict) else {}
    expires_at = oauth.get("expiresAt")
    return expires_at if isinstance(expires_at, int) else None


def check_login_status(binary: str, claude_json: Path) -> LoginStatus:
    """Probe ``claude auth status --json`` and report the account's login state.

    Runs the resolved ``binary`` (absolute path via the shared PATH resolver) with
    list-argv (never ``shell=True``), the scrubbed child env, and a bounded
    timeout. Synchronous and blocking — call it off the request path (see
    :class:`LoginStatusCache`), never inline in ``/healthz``. Never raises: a
    missing binary, a timeout, a non-zero exit, or non-JSON output all fail closed
    to ``logged_in=False`` with a ``reason``. Only the non-PII ``loggedIn`` /
    ``authMethod`` are surfaced.
    """
    try:
        resolved = claude_cli.resolve_binary(binary)
    except claude_cli.ClaudeNotFound as exc:
        return LoginStatus(False, None, None, f"claude binary not found: {exc}")

    try:
        proc = subprocess.run(
            [resolved, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_AUTH_STATUS_TIMEOUT_S,
            env=procutil.child_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        _log.warning("`claude auth status` timed out after %ss", _AUTH_STATUS_TIMEOUT_S)
        return LoginStatus(False, None, None, "`claude auth status` timed out")
    except OSError as exc:
        _log.warning("`claude auth status` could not be run: %s", exc)
        return LoginStatus(False, None, None, f"`claude auth status` could not be run: {exc}")

    if proc.returncode != 0:
        # A non-zero exit typically IS the "not logged in" signal on some CLI
        # versions; treat it as logged-out rather than an internal error.
        return LoginStatus(False, None, None, f"`claude auth status` exited {proc.returncode}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return LoginStatus(False, None, None, f"`claude auth status` returned non-JSON: {exc}")
    if not isinstance(data, dict):
        return LoginStatus(False, None, None, "`claude auth status` returned non-object JSON")

    logged_in = bool(data.get("loggedIn"))
    method_raw = data.get("authMethod")
    method = method_raw if isinstance(method_raw, str) and method_raw else None

    if not logged_in:
        return LoginStatus(False, method, None, "claude reports not logged in")

    # Optional proactive OAuth-expiry enrichment — never changes the verdict.
    expires_at_ms = None
    if method is not None and method.lower() in _OAUTH_METHODS:
        expires_at_ms = _oauth_expires_at_ms(claude_json)
    return LoginStatus(True, method, expires_at_ms, "logged in")


class LoginStatusCache:
    """Stale-while-revalidate cache so ``/healthz`` never blocks on the probe.

    :meth:`read` returns the last known :class:`LoginStatus` *immediately* — the
    subprocess never runs on the caller's thread. When the cached value is missing
    or older than the TTL, ``read`` kicks a single background daemon thread to run
    the probe and update the cache, then returns the current (stale, or cold-start
    :func:`unknown`) value. Single-flight: at most one refresh thread runs at a
    time, so the subprocess fires at most once per TTL regardless of how many tabs
    poll or how fast they poll.

    The ``clock`` and ``probe`` seams are injectable so tests can drive the TTL and
    the probe result deterministically without real sleeps or a real subprocess.
    """

    def __init__(
        self,
        binary: str,
        claude_json: Path,
        *,
        ttl_s: float = _CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        probe: Callable[[str, Path], LoginStatus] = check_login_status,
    ) -> None:
        """Bind the cache to the runtime ``binary`` / ``claude_json`` and its seams."""
        self._binary = binary
        self._claude_json = claude_json
        self._ttl_s = ttl_s
        self._clock = clock
        self._probe = probe
        self._lock = threading.Lock()
        self._value: LoginStatus = unknown()
        self._stamp: float | None = None  # None until the first probe lands
        self._refreshing = False
        self._thread: threading.Thread | None = None  # the active refresh thread, if any

    def read(self) -> LoginStatus:
        """Return the cached status now; kick a background refresh if it's stale.

        Fast and non-blocking — safe to call on the ``/healthz`` request path: it
        never raises (a failed thread spawn is swallowed) and never runs the probe
        inline. The probe (a subprocess) only ever runs on the spawned refresh
        thread, and only one such thread exists at a time (single-flight).
        """
        with self._lock:
            fresh = self._stamp is not None and (self._clock() - self._stamp) < self._ttl_s
            value = self._value
            if not fresh and not self._refreshing:
                thread = threading.Thread(
                    target=self._refresh, name="login-status-refresh", daemon=True
                )
                try:
                    thread.start()
                except RuntimeError:
                    # Thread/memory exhaustion (`can't start new thread`) is most likely
                    # under exactly the load this cache guards against. Swallow it: never
                    # 500 `/healthz`, and — because `_refreshing`/`_thread` are only set
                    # AFTER a successful start — never wedge the single-flight flag `True`
                    # so a later read still retries. The stale/`unknown()` value is served.
                    _log.exception("could not start login-status refresh thread")
                else:
                    self._refreshing = True
                    self._thread = thread
        return value

    def wait_for_pending_refresh(self, timeout: float = 5.0) -> None:
        """Block until any in-flight refresh thread finishes (test/shutdown helper).

        The request path never calls this — ``/healthz`` uses :meth:`read` and
        returns immediately. It exists so a test can deterministically wait for the
        single background probe to land (rather than sleeping a fixed interval).
        """
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _refresh(self) -> None:
        """Run the probe (blocking) and store its result; clears the single-flight flag.

        Runs only on the background thread spawned by :meth:`read`. The probe never
        raises (see :func:`check_login_status`), but the broad ``except`` still
        converts any unexpected failure into a fail-closed result so the store +
        flag-clear below always runs — a one-off failure can't wedge the cache into
        "never refresh again".
        """
        try:
            result = self._probe(self._binary, self._claude_json)
        except Exception:  # pragma: no cover - check_login_status never raises
            _log.exception("login-status refresh probe raised unexpectedly")
            result = LoginStatus(False, None, None, "login-status probe failed")
        with self._lock:
            self._value = result
            self._stamp = self._clock()
            self._refreshing = False
