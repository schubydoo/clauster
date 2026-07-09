"""Gauge a bridge anchor's health via the ``/v1/code/sessions`` namespace (#867 L2).

Used pre-spawn to decide whether a preserved ``bridge-pointer.json``'s anchor session is
still reattachable, or was archived/deleted out from under its environment — the #671
dead-end, where a warm relaunch re-adopts a poisoned work item and the bridge comes back
idle with no session. When the anchor is gone, clearing the pointer forces a clean start.

The bridge session namespace (``/v1/code/sessions/{cse_…}``) is distinct from the
managed-agents ``/v1/sessions`` (``sesn_…``): a bridge anchor appears only in the former.
Empirically (claude 2.1.201) a live anchor reports ``status: active``, an archived one
``status: archived``, and a deleted one is ``404``. Everything is best-effort: any
uncertainty (no creds, network error, unexpected shape) yields ``UNKNOWN`` so the caller
leaves the pointer untouched and never blocks or fails a spawn on this probe.
"""

from __future__ import annotations

import enum
import http.client
import json
import logging
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from .environments import (
    API_BASE,
    BETA_HEADER,
    CLAUDE_JSON_PATH,
    CREDENTIALS_PATH,
    Credentials,
    CredentialsError,
    EnvironmentsAPIError,
    Transport,
    load_credentials,
)

_log = logging.getLogger("clauster.code_sessions")

# The bridge session API is a dated beta; it also requires an anthropic-version header
# (unlike the environments API). Re-verify against the installed `claude` if calls 400.
ANTHROPIC_VERSION = "2023-06-01"
_HEALTHY_STATUSES = frozenset({"active", "idle"})
# A spawn preflight must be quick — a hung probe must not stall a launch. Fail-safe to
# UNKNOWN on timeout (shorter than the environments client's 30s reaper timeout).
_TIMEOUT_SECONDS = 6


class AnchorHealth(enum.Enum):
    """The reattach-worthiness of a bridge's preserved anchor session."""

    HEALTHY = "healthy"  # exists and active/idle -> safe to reattach as-is
    POISONED = "poisoned"  # archived or gone (404) -> clear the pointer, start cold
    UNKNOWN = "unknown"  # indeterminate (no creds / network / unexpected) -> leave as-is


def code_session_id_for(starter_session_id: str) -> str:
    """Derive the bridge code-session id (``cse_<ULID>``) from a pointer's ``session_<ULID>``.

    The two ids share the ULID suffix (verified across bridges); only the prefix differs.
    """
    return "cse_" + starter_session_id.removeprefix("session_")


def _short_timeout_transport(  # pragma: no cover - live network I/O (fake injected in tests)
    method: str, url: str, headers: dict, body: bytes | None
) -> tuple[int, bytes]:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise EnvironmentsAPIError(0, f"refusing non-https URL: {url!r}")
    conn = http.client.HTTPSConnection(
        parts.netloc, timeout=_TIMEOUT_SECONDS, context=ssl.create_default_context()
    )
    try:
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _is_session_not_found(raw: bytes) -> bool:
    """Whether a 404 body is an Anthropic *resource*-not-found error (vs a route 404).

    A deleted anchor returns ``type: not_found_error`` (and ``resource_type: session``); a
    shifted/removed route would 404 with a different (or non-JSON) body. Distinguishing them
    keeps a route change from clearing healthy pointers across every project.
    """
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    error = error if isinstance(error, dict) else body
    # Require BOTH signals so a route-level 404 that merely mentions a session (or a
    # generic not_found_error for a different resource) can't clear a healthy pointer.
    return error.get("type") == "not_found_error" and error.get("resource_type") == "session"


class CodeSessionsClient:
    """Minimal read-only client for the ``/v1/code/sessions`` bridge-session namespace."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        transport: Transport | None = None,
        base: str = API_BASE,
    ) -> None:
        """Bind the client to credentials, with an optional (test) transport and API base."""
        self._cred = credentials
        self._transport = transport or _short_timeout_transport
        self._base = base.rstrip("/")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._cred.access_token}",
            "x-organization-uuid": self._cred.organization_uuid,
            "anthropic-beta": BETA_HEADER,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def anchor_health(self, cse_id: str) -> AnchorHealth:
        """GET the anchor session and classify it (fail-safe to ``UNKNOWN``)."""
        url = f"{self._base}/v1/code/sessions/{cse_id}"
        try:
            status, raw = self._transport("GET", url, self._headers(), None)
        except (OSError, EnvironmentsAPIError):
            return AnchorHealth.UNKNOWN
        if status == 404:
            # A deleted anchor 404s with a resource-not-found error body. Require that shape
            # so a *route-level* 404 (e.g. a shifted beta path) can't wrongly clear a
            # healthy pointer across every project — an unrecognized 404 is UNKNOWN.
            return AnchorHealth.POISONED if _is_session_not_found(raw) else AnchorHealth.UNKNOWN
        if status >= 400:
            return AnchorHealth.UNKNOWN  # auth/beta/transient -> don't destroy the pointer
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return AnchorHealth.UNKNOWN
        if not isinstance(data, dict):
            return AnchorHealth.UNKNOWN  # a bare null/list/scalar 2xx body -> indeterminate
        # A single GET wraps the session under "response_shape"; tolerate a flat body too.
        wrapped = data.get("response_shape")
        session = wrapped if isinstance(wrapped, dict) else data
        session_status = session.get("status")
        if session_status in _HEALTHY_STATUSES:
            return AnchorHealth.HEALTHY
        if session_status == "archived":
            return AnchorHealth.POISONED
        return AnchorHealth.UNKNOWN  # terminated/unknown -> conservative: leave the pointer


def anchor_health_for_pointer(
    starter_session_id: str,
    *,
    credentials_path: Path = CREDENTIALS_PATH,
    claude_json_path: Path = CLAUDE_JSON_PATH,
    transport: Transport | None = None,
) -> AnchorHealth:
    """Load credentials and classify a preserved anchor's health; fail-safe to ``UNKNOWN``.

    Never raises: a missing/expired credential (``CredentialsError``) yields ``UNKNOWN`` so
    the caller simply leaves the pointer alone and lets the launch proceed unchanged.
    """
    if not starter_session_id.startswith("session_"):
        # An unexpected id shape would mis-derive the cse id and 404 -> don't risk it.
        _log.debug("anchor health check skipped: unexpected session id %r", starter_session_id)
        return AnchorHealth.UNKNOWN
    try:
        creds = load_credentials(credentials_path, claude_json_path)
    except CredentialsError:
        _log.debug("anchor health check skipped: no usable credentials")
        return AnchorHealth.UNKNOWN
    return CodeSessionsClient(creds, transport=transport).anchor_health(
        code_session_id_for(starter_session_id)
    )
