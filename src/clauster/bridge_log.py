"""Pure parser for the ``claude remote-control`` debug log (spec §7, source #1).

No I/O — operates on text so it is trivially testable against the fixtures in
``tests/fixtures/bridge-logs/``. Marker formats verified 2026-05-29 from a real
bridge debug log:

    [bridge:init] bridgeId=<uuid> dir=… branch=… gitRepoUrl=… machine=…
    [bridge:api]  POST /v1/environments/bridge -> 200 environment_id=env_<ULID>
    [bridge:init] Created initial session session_<ULID>
    [bridge:work] Starting poll loop spawnMode=same-dir maxSessions=32 environmentId=env_<ULID>
    [bridge:session] sessionId=[REDACTED] pid=<pid>
    [bridge:session] sessionId=[REDACTED] failed exit_code=<n> pid=<pid>
    [bridge:shutdown] SIGINT received, shutting down
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_RE_BRIDGE_ID = re.compile(r"\[bridge:init\][^\n]*\bbridgeId=([0-9a-fA-F-]{8,})")
_RE_ENV_ID = re.compile(r"\benvironment_id=(env_[A-Za-z0-9]+)")
_RE_ENV_ID_ALT = re.compile(r"\benvironmentId=(env_[A-Za-z0-9]+)")
_RE_STARTER = re.compile(r"Created initial session\s+(session_[A-Za-z0-9]+)")
# A `--continue` resume reconnects to an EXISTING session and never logs "Created
# initial session"; instead it logs the session it resumed as a `[remote-bridge]
# Unarchive session_<id>` line. Recovering it keeps the `session_url` deep link
# working after a true-resume (pty mode, where there's also no pointer to fall back on).
_RE_RESUME_SESSION = re.compile(r"\[remote-bridge\][^\n]*\bUnarchive\s+(session_[A-Za-z0-9]+)")
_RE_POLL_LOOP = re.compile(r"\[bridge:work\][^\n]*Starting poll loop")
_RE_SPAWN_MODE = re.compile(r"\bspawnMode=([A-Za-z0-9-]+)")
_RE_SHUTDOWN = re.compile(r"\[bridge:shutdown\][^\n]*(?:SIGINT|SIGTERM|shutting down)")
_RE_TRUST_ERROR = re.compile(r"Workspace not trusted", re.IGNORECASE)
# #867 L3: on a warm reattach the server tears the re-adopted session down with an
# `end_session` control request whose reason is `archived`/`deleted` when the anchor is
# gone — the #671 dead-end (the bridge then idles with no session). This is the definitive
# poison signal (verified against a live 2.1.201 bridge log).
# Match either key order within a single JSON object ([^{}]* can't cross an object
# boundary), so the CLI reordering `reason`/`subtype` can't hide the poison.
_RE_END_SESSION_GONE = re.compile(
    r'"subtype":\s*"end_session"[^{}]*"reason":\s*"(archived|deleted)"'
    r'|"reason":\s*"(archived|deleted)"[^{}]*"subtype":\s*"end_session"'
)


@dataclass
class BridgeMarkers:
    """Everything extractable from a bridge debug log so far."""

    bridge_id: str | None = None
    environment_id: str | None = None
    starter_session_id: str | None = None
    spawn_mode: str | None = None
    poll_loop_started: bool = False  # the RUNNING signal
    clean_shutdown: bool = False
    trust_error: bool = False
    # Non-None (``"archived"``/``"deleted"``) when a reattached session was torn down as
    # gone — a poisoned reattach that would otherwise idle with no session (#671).
    poison_reason: str | None = None

    @property
    def is_ready(self) -> bool:
        """A bridge is usable once it has registered an env and begun polling."""
        return self.poll_loop_started and self.environment_id is not None


def parse_bridge_markers(text: str) -> BridgeMarkers:
    """Extract markers from (possibly partial) bridge-log text.

    First-wins for single-valued fields; tolerant of truncated / mid-write content.
    """
    m = BridgeMarkers()

    if (hit := _RE_BRIDGE_ID.search(text)) is not None:
        m.bridge_id = hit.group(1)
    if (hit := _RE_ENV_ID.search(text)) is not None:
        m.environment_id = hit.group(1)
    elif (hit := _RE_ENV_ID_ALT.search(text)) is not None:
        m.environment_id = hit.group(1)
    if (hit := _RE_STARTER.search(text)) is not None:
        m.starter_session_id = hit.group(1)
    elif (hit := _RE_RESUME_SESSION.search(text)) is not None:
        m.starter_session_id = hit.group(1)  # a --continue resume never logs "Created…"
    if (hit := _RE_SPAWN_MODE.search(text)) is not None:
        m.spawn_mode = hit.group(1)

    m.poll_loop_started = _RE_POLL_LOOP.search(text) is not None
    m.clean_shutdown = _RE_SHUTDOWN.search(text) is not None
    m.trust_error = _RE_TRUST_ERROR.search(text) is not None
    if (hit := _RE_END_SESSION_GONE.search(text)) is not None:
        m.poison_reason = hit.group(1) or hit.group(2)

    return m
