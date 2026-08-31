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
import json
import logging
from pathlib import Path

from .environments import (
    API_BASE,
    CLAUDE_JSON_PATH,
    CREDENTIALS_PATH,
    Credentials,
    CredentialsError,
    EnvironmentsAPIError,
    Transport,
    _AnthropicHTTPClient,
    https_transport,
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


# A spawn preflight uses a short-timeout transport so a hung probe can't stall a launch.
_CODE_TRANSPORT = https_transport(timeout=_TIMEOUT_SECONDS)


def _is_session_not_found(raw: bytes) -> bool:
    """Whether a 404 body is an Anthropic *resource*-not-found error (vs a route 404).

    A deleted anchor returns ``type: not_found_error`` (and ``resource_type: session``); a
    shifted/removed route would 404 with a different (or non-JSON) body. Distinguishing them
    keeps a route change from clearing healthy pointers across every project.
    """
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        # RecursionError: deeply-nested JSON overflows CPython's recursive scanner
        # before json can raise JSONDecodeError, and it is not a ValueError — so a
        # hostile/garbled 404 body from the remote API would escape this handler.
        # Degrades to the same "not a resource-not-found body" answer as any other
        # unparseable response, which is the fail-closed one: pointers stay put.
        return False
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    error = error if isinstance(error, dict) else body
    # Require BOTH signals so a route-level 404 that merely mentions a session (or a
    # generic not_found_error for a different resource) can't clear a healthy pointer.
    return error.get("type") == "not_found_error" and error.get("resource_type") == "session"


class CodeSessionsClient(_AnthropicHTTPClient):
    """Minimal read-only client for the ``/v1/code/sessions`` bridge-session namespace."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        transport: Transport | None = None,
        base: str = API_BASE,
    ) -> None:
        """Bind to credentials; defaults to the short-timeout preflight transport."""
        super().__init__(credentials, transport=transport or _CODE_TRANSPORT, base=base)

    def _headers(self) -> dict:
        """Return the base headers plus the version header this beta namespace requires."""
        return {**super()._headers(), "anthropic-version": ANTHROPIC_VERSION}

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

    A missing/expired credential (``CredentialsError``) yields ``UNKNOWN`` so the caller
    simply leaves the pointer alone and lets the launch proceed unchanged. ``CredentialsError``
    now genuinely covers every malformed-credentials shape — including a valid-JSON root that
    is not an object, which used to escape as ``AttributeError`` — so this really is the
    never-raises contract its callers assume. That matters because ``runner``'s spawn
    preflight calls it unwrapped: anything escaping here fails a spawn outright.
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
