"""Detect whether the runtime ``claude`` account is actually logged in (#838).

``/healthz`` already reports ``claude_ok`` — the ``claude`` binary is invokable —
but not whether its OAuth login is still valid. An expired or absent login lets a
bridge spawn and then hang at "Starting" with no upfront signal, because the
subprocess itself is fine; only its credentials are stale.

This mirrors :func:`clauster.ops._check_claude_login` (the ``clauster doctor``
check) but is purpose-built for ``/healthz``: it returns a small structured
result instead of a human-readable ``Check``, and it also surfaces expiry
(``ops._check_claude_login`` only checks token presence).

The credentials file path is derived from the SAME resolved ``~/.claude.json``
every other config-write surface already uses (``claude_json.parent / ".claude"``
— see :mod:`clauster.claude_md`, :mod:`clauster.config_write_subagents`) rather
than a fresh ``Path.home()`` lookup, so this stays correct under HOME isolation
in tests and any future config-dir override.

Fails closed: every error path (missing file, unreadable, unparseable, no
``accessToken``) yields ``logged_in=False`` with a ``reason`` — never raises out
to the ``/healthz`` caller. The token value itself is never logged or returned.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("clauster.login_status")


@dataclass(frozen=True)
class LoginStatus:
    """Result of inspecting the runtime account's ``.credentials.json``.

    ``expires_at_ms`` and ``expired`` are only meaningful when the token was
    found — ``expires_at_ms`` is ``None`` and ``expired`` is ``False`` on every
    fail-closed path (missing/unreadable/malformed file, no ``accessToken``).
    """

    logged_in: bool
    expires_at_ms: int | None
    expired: bool
    reason: str


def credentials_path_for(claude_json: Path) -> Path:
    """Return the ``.credentials.json`` sibling of the resolved ``claude_json``'s dir.

    Reuses the existing ``claude_json.parent / ".claude"`` convention (the same
    directory :mod:`clauster.claude_md` and :mod:`clauster.config_write_subagents`
    derive their user-scope paths from) instead of a fresh ``Path.home()`` lookup.
    """
    return claude_json.parent / ".claude" / ".credentials.json"


def check_login_status(claude_json: Path, *, now_ms: int) -> LoginStatus:
    """Read ``.credentials.json`` (sibling of ``claude_json``) and report login state.

    ``now_ms`` is the caller's current time in ms epoch (injected, not read from
    the clock here, so this stays deterministically testable). Never raises: a
    missing file, an unreadable file, unparseable JSON, or a missing
    ``accessToken`` all fail closed to ``logged_in=False`` with a ``reason`` —
    none of them are a token value, so nothing secret ever appears in the result
    or in a log line.
    """
    creds_path = credentials_path_for(claude_json)
    try:
        raw = creds_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LoginStatus(False, None, False, f"no credentials file at {creds_path}")
    except OSError as exc:
        # Permission errors etc. — log the failure mode (never the path's contents)
        # and still fail closed rather than raising out to the /healthz caller.
        _log.warning("could not read %s: %s", creds_path, exc)
        return LoginStatus(False, None, False, f"could not read {creds_path}: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return LoginStatus(False, None, False, f"{creds_path} is not valid JSON: {exc}")

    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    oauth = oauth if isinstance(oauth, dict) else {}

    token = oauth.get("accessToken")
    if not token:
        return LoginStatus(False, None, False, f"no claudeAiOauth.accessToken in {creds_path}")

    expires_at = oauth.get("expiresAt")
    expires_at_ms = expires_at if isinstance(expires_at, int) else None
    expired = expires_at_ms is not None and now_ms >= expires_at_ms
    if expired:
        return LoginStatus(False, expires_at_ms, True, "access token has expired")
    return LoginStatus(True, expires_at_ms, False, "logged in")
