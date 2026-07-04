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
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import claude_cli, procutil

_log = logging.getLogger("clauster.login_status")

#: Bound the ``claude auth status`` probe so a wedged CLI can't hang ``/healthz``.
#: The command is local (no network round-trip) and returns in well under a second
#: on a healthy host; 5s is generous headroom before we fail closed.
_AUTH_STATUS_TIMEOUT_S = 5.0

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
    """

    logged_in: bool
    method: str | None
    expires_at_ms: int | None
    reason: str


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
    timeout. Synchronous — the caller runs it off the event loop via
    ``asyncio.to_thread``. Never raises: a missing binary, a timeout, a non-zero
    exit, or non-JSON output all fail closed to ``logged_in=False`` with a
    ``reason``. Only the non-PII ``loggedIn`` / ``authMethod`` are surfaced.
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
