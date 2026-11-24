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
    [bridge:api] POST /v1/code/sessions/cse_<ULID>/archive
    [bridge:shutdown] SIGINT received, shutting down
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple


class _Marker(NamedTuple):
    r"""A marker pattern split in two: ``head`` is the tag, ``tail`` the rest of the pattern.

    The tail starts with ``[^\n]*`` or ``[^{}]*``: that leading star reads only up to the
    next ``edge`` (a line end, or a JSON object's brace), so the rest of the tail can start
    only before the edge (it can still read past it, as ``Unarchive\s+`` can). Joined into
    one pattern, ``search`` tries the tail again after each head, so a line of N heads costs
    N times its length (TASK-471). :func:`_first_hit` finds the same match and tries the
    tail once per segment.
    """

    head: re.Pattern[str]
    tail: re.Pattern[str]
    edge: re.Pattern[str]


_LINE_END = re.compile(r"\n")
_OBJECT_EDGE = re.compile(r"[{}]")

_BRIDGE_ID = _Marker(
    re.compile(r"\[bridge:init\]"),
    re.compile(r"[^\n]*\bbridgeId=([0-9a-fA-F-]{8,})"),
    _LINE_END,
)
_RE_ENV_ID = re.compile(r"\benvironment_id=(env_[A-Za-z0-9]+)")
_RE_ENV_ID_ALT = re.compile(r"\benvironmentId=(env_[A-Za-z0-9]+)")
_RE_STARTER = re.compile(r"Created initial session\s+(session_[A-Za-z0-9]+)")
# A `--continue` resume reconnects to an EXISTING session and never logs "Created
# initial session"; instead it logs the session it resumed as a `[remote-bridge]
# Unarchive session_<id>` line. Recovering it keeps the `session_url` deep link
# working after a true-resume (pty mode, where there's also no pointer to fall back on).
_RESUME_SESSION = _Marker(
    re.compile(r"\[remote-bridge\]"),
    re.compile(r"[^\n]*\bUnarchive\s+(session_[A-Za-z0-9]+)"),
    _LINE_END,
)
_POLL_LOOP = _Marker(
    re.compile(r"\[bridge:work\]"), re.compile(r"[^\n]*Starting poll loop"), _LINE_END
)
_RE_SPAWN_MODE = re.compile(r"\bspawnMode=([A-Za-z0-9-]+)")
_SHUTDOWN = _Marker(
    re.compile(r"\[bridge:shutdown\]"),
    re.compile(r"[^\n]*(?:SIGINT|SIGTERM|shutting down)"),
    _LINE_END,
)
_RE_TRUST_ERROR = re.compile(r"Workspace not trusted", re.IGNORECASE)
# #867 L3: on a warm reattach the server tears the re-adopted session down with an
# `end_session` control request whose reason is `archived`/`deleted` when the anchor is
# gone — the #671 dead-end (the bridge then idles with no session). This is the definitive
# poison signal (verified against a live 2.1.201 bridge log).
# Match either key order within a single JSON object ([^{}]* can't cross an object
# boundary), so the CLI reordering `reason`/`subtype` can't hide the poison. The two key
# orders are the two alternatives of one pattern: the hit that starts first wins.
_END_SESSION_GONE = (
    _Marker(
        re.compile(r'"subtype":\s*"end_session"'),
        re.compile(r'[^{}]*"reason":\s*"(archived|deleted)"'),
        _OBJECT_EDGE,
    ),
    _Marker(
        re.compile(r'"reason":\s*"(archived|deleted)"'),
        re.compile(r'[^{}]*"subtype":\s*"end_session"'),
        _OBJECT_EDGE,
    ),
)
# TASK-557: a multi-session bridge archives a session when it completes, and this request is
# the only line that names the session (the `exited status=` line masks the id). claude
# 2.1.330 posts to `/v1/sessions/session_<id>/archive` or `/v1/code/sessions/cse_<id>/archive`.
# Anchored to a whole log line, so session text quoted inside a `[bridge:ws]` frame cannot
# match. A forged line could only move the link to the environment link.
_RE_SESSION_ARCHIVED = re.compile(
    r"\S+ \[[A-Z]+\] \[bridge:api\] POST /v1/(?:code/)?sessions/"
    r"(?:cse|session)_([A-Za-z0-9_-]+)/archive\s*"
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


def _first_hit(text: str, marker: _Marker) -> tuple[re.Match[str], re.Match[str]] | None:
    """Return the head and tail matches of the first ``head + tail`` match in ``text``.

    This is the match that ``search`` finds with the joined pattern, at the same place and
    with the same groups. If the tail fails after the first head of a segment (the text up
    to the next ``edge``), it fails after every later head of that segment too: the tail's
    leading star reads only to the edge, so the rest of the tail can start only before it,
    and a later head only shrinks that set of starts. So the
    search goes on at the next segment, and the cost is linear in the length of ``text``.
    """
    pos = 0
    while (head := marker.head.search(text, pos)) is not None:
        if (tail := marker.tail.match(text, head.end())) is not None:
            return head, tail
        edge = marker.edge.search(text, head.end())
        if edge is None:
            return None
        pos = edge.end()
    return None


def _end_session_reason(text: str) -> str | None:
    """Return the ``reason`` of the first ``end_session`` request with a gone anchor, or None.

    The two key orders start with different keys, so two hits never start at one place.
    """
    subtype_first, reason_first = (_first_hit(text, marker) for marker in _END_SESSION_GONE)
    if subtype_first is not None and (
        reason_first is None or subtype_first[0].start() < reason_first[0].start()
    ):
        return subtype_first[1].group(1)  # the reason is in the tail
    return reason_first[0].group(1) if reason_first is not None else None


def parse_bridge_markers(text: str) -> BridgeMarkers:
    """Extract markers from (possibly partial) bridge-log text.

    First-wins for single-valued fields; tolerant of truncated / mid-write content.
    """
    m = BridgeMarkers()

    if (bridge := _first_hit(text, _BRIDGE_ID)) is not None:
        m.bridge_id = bridge[1].group(1)
    if (hit := _RE_ENV_ID.search(text)) is not None:
        m.environment_id = hit.group(1)
    elif (hit := _RE_ENV_ID_ALT.search(text)) is not None:
        m.environment_id = hit.group(1)
    if (hit := _RE_STARTER.search(text)) is not None:
        m.starter_session_id = hit.group(1)
    elif (resumed := _first_hit(text, _RESUME_SESSION)) is not None:
        m.starter_session_id = resumed[1].group(1)  # a --continue resume never logs "Created…"
    if (hit := _RE_SPAWN_MODE.search(text)) is not None:
        m.spawn_mode = hit.group(1)

    m.poll_loop_started = _first_hit(text, _POLL_LOOP) is not None
    m.clean_shutdown = _first_hit(text, _SHUTDOWN) is not None
    m.trust_error = _RE_TRUST_ERROR.search(text) is not None
    m.poison_reason = _end_session_reason(text)

    return m


def session_key(session_id: str) -> str:
    """Return the id part shared by the ``session_<id>`` and ``cse_<id>`` forms."""
    return session_id.removeprefix("session_").removeprefix("cse_")


def archived_session_key(line: str) -> str | None:
    """Return the :func:`session_key` a whole log line archives, or ``None``."""
    hit = _RE_SESSION_ARCHIVED.fullmatch(line)
    return hit.group(1) if hit is not None else None


def environment_ids_in(line: str) -> set[str]:
    """Return every environment id that one log line names."""
    return {*_RE_ENV_ID.findall(line), *_RE_ENV_ID_ALT.findall(line)}
