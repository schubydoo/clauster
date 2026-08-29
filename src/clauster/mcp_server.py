"""``clauster mcp`` stdio MCP server (#527).

Exposes Clauster's session lifecycle to an MCP client (Claude Desktop, Claude
Code, or any stdio MCP host) so an assistant can drive Clauster directly.

Read tools:

``list_sessions``
    Every session Clauster can see — managed bridges, hosted (Direct Session)
    sessions, agent-view background jobs, and external (unmanaged) working
    sessions — summarized into one flat list.

``session_status``
    The detail of one session, looked up by its id.

Write tools (#527 write slice) — **gated behind ``mcp.allow_writes``** (#1010,
default off), so the surface is read-only until the operator opts in. The **bridge**
channel only, thin wrappers over
the #775 :class:`~clauster.engine.ClausterEngine` facade (the same
``spawn_detailed``/``stop``/``resume`` the dashboard ``/api`` routes call, so the
per-mode policy, standard-singleton cap, option validation, and per-project spawn
lock are identical headless or from the browser):

``spawn_session``
    Start a bridge for a project. ``resume_mode`` picks standard vs pty with no
    hidden coercion, exactly like the launch picker.

``stop_session`` / ``resume_session``
    Stop, or resume into its prior conversation, the bridge named by an id (as
    returned by ``list_sessions``) — resolved through the same
    ``resolve_bridge_id`` the ``/api`` DELETE/resume routes use.

Auth. **Local-privileged, no token auth** — the stdio transport is reachable only
by a process the operator already launched on the host (the maintainer's
2026-07-11 design pass); a future daemon-socket transport can add token auth.
**Trust is NOT auto-granted:** ``spawn_session`` exposes an explicit ``trust``
argument that defaults to *false*, so an untrusted directory raises rather than
being silently trusted from an MCP client — the headless equivalent of the CLI's
``--trust`` / the dashboard's explicit Trust action.

Concurrency with a running web app. A headless write runs its own short-lived
:class:`ClausterEngine` (a fresh ``SessionRunner``) — the **same pattern the CLI
``clauster start``/``stop`` already use**, sequenced after the #775 facade for
exactly this reason. Each tool call :meth:`~clauster.engine.ClausterEngine.hydrate`
s first, so ``rediscover`` reconciles the shared persisted state with the **live
processes** (the ground truth, via on-disk bridge pointers) before acting — the
idempotency check then sees a bridge the web app already started and hands it back
instead of double-spawning.

Serialization is cross-process. The runner's per-project spawn/stop/forget/adopt
sections hold a deployment-wide ``flock`` (``atomicio.cross_process_lock``) under
their in-process ``asyncio.Lock``, so this headless writer mutually excludes the
running web-app process, not just its own concurrent calls (#949, PR #951). That
lock closed the two races this pattern used to carry (both pre-existing for the
shipped CLI headless-write path; the MCP tools inherit the fix for free):

1. **Duplicate-bridge TOCTOU** — the spawn idempotency check now also probes the
   on-disk bridge pointer under the cross-process lock, positively cwd-attributed,
   so a bridge the web app already started is reattached and handed back rather than
   double-launched.
2. **Stale-snapshot resurrection** — ``_persist`` refreshes its merge-base from the
   store under a store-wide lock before its full-replace save, tracks row-backedness
   as the ownership signal, and aborts on a failed refresh — so a row another process
   forgot can no longer be written back.

Shared **state** is not byte-corrupted regardless — persistence is SQLite (WAL +
busy-timeout), so even a contended write waits then raises a real error (surfaced as
``isError``), never a torn write.

Transport / protocol. A minimal, dependency-free stdio JSON-RPC 2.0 server
implementing just the MCP messages a stdio tool host needs: ``initialize``, the
``notifications/initialized`` ack, ``tools/list``, ``tools/call`` and ``ping``.
We hand-roll this rather than pull in the official ``mcp`` SDK — its
async/SSE/Starlette dependency tree is far more than a stdio surface warrants,
and a tiny in-tree server keeps the distributed binary's supply-chain footprint
unchanged and the whole path unit testable without spawning a subprocess (so the
write slice needs no new ``[mcp]`` extra). Per the spec, messages are
newline-delimited single-line JSON on stdin/stdout and **all** human-readable
logging goes to stderr so it never corrupts the protocol stream.

Fail-closed posture. A tool that raises — a bad option (``InvalidSpawnOption``),
an untrusted directory (``NotTrusted``), an unknown project/id, a spawn failure
(``SpawnError``) — is reported back as an ``isError`` tool result carrying the
message, never a server crash and never a silent empty success. Read output
reuses Clauster's existing redaction (background-job free-text is redacted by
:mod:`clauster.supervisor` upstream); the write tools return only structural
lifecycle fields (ids, status, project, modes), never raw transcript or log
content.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from typing import IO, TYPE_CHECKING, Any

import yaml

from . import __version__
from .config import ClausterConfig, load_config

if TYPE_CHECKING:
    from .models import BackgroundJob, RemoteControlInstance, WorkingSession

_log = logging.getLogger("clauster.mcp")

# The MCP revision this server implements — the only one it speaks, and the version
# `initialize` always replies with (per the lifecycle spec: never claim one we don't
# implement).
PROTOCOL_VERSION = "2025-06-18"
_SERVER_NAME = "clauster"

# JSON-RPC 2.0 error codes we use.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603

# Max bytes for one JSON-RPC line. Replies are small (session summaries / structural
# lifecycle fields) and requests tinier, so this generous-but-bounded cap never trips on a
# legitimate message while stopping one pathological line from growing the read buffer
# without limit. An overlong line is answered with a parse error, not a crash (see ``serve``).
_MAX_LINE_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Session gathering — reuse the existing read machinery, never re-derive it.
# --------------------------------------------------------------------------- #
async def gather_sessions(config: ClausterConfig) -> list[dict[str, Any]]:
    """Collect every observable session into a flat list of plain dicts.

    Reuses the dashboard's read path: a :class:`SessionRunner` is built and reconciled
    once — ``rediscover(persist=False)`` + ``poll_once(side_effects=False)``, the same
    liveness + ``agents --json`` cross-check the poll loop runs, minus its writes and
    lifecycle events (#1104) — then its managed bridges,
    tracked/external working sessions, the supervisor's background jobs, and the
    persisted hosted-session records are each mapped into a uniform summary. No
    session-listing logic is duplicated here — only assembled.

    Read-only, and enforced rather than asserted: no bridge is spawned or stopped, the
    shared ``state.json`` is not written, and no lifecycle event, webhook, or
    notification fires. Instance status IS reconciled in memory, so a bridge that died
    is reported as crashed — observed, not announced. Hosted sessions are read
    from the runner's persistence container (the same DB-backed store the app
    uses; no live daemon connection is opened by this short-lived process), so
    they appear with their last-known status.
    """
    # Imported lazily so the rest of the CLI/app never pays for the runner import
    # graph (psutil, sqlalchemy, …) just to parse ``--help``.
    from .hosted import HostedManager
    from .models import InstanceStatus
    from .runner import SessionRunner
    from .supervisor import list_background_jobs

    sessions: list[dict[str, Any]] = []

    runner = SessionRunner(config)
    # Why no writes: this read tool commonly runs against a host where the live service
    # owns that state (#1104) — `persist=True` would rewrite the shared `state.json` on
    # every session list, and the crash arm would fire a duplicate webhook + notification
    # for a bridge the service is already tracking.
    #
    # Why `poll_once` is called at all rather than dropped: `tracked_sessions_by_instance`
    # below reads the cross-check it computes, so skipping it would silently report every
    # bridge with zero sessions. `side_effects=False` keeps the observation, drops the
    # announcements.
    await runner.rediscover(persist=False)
    await runner.poll_once(side_effects=False)

    tracked = runner.tracked_sessions_by_instance()
    for inst in runner.list_instances():
        sessions.append(_summarize_instance(inst, kind="bridge"))
        # Keyed by instance_id, not project (#1020 A3): a project-keyed lookup inside this
        # per-instance loop emitted every session once per bridge on that project, so a
        # project running a standard bridge plus two interactive ones reported each session
        # three times.
        for ws in tracked.get(inst.instance_id, []):
            summary = _summarize_working(ws, kind="bridge-session")
            summary["project"] = (
                inst.project
            )  # the working session belongs to this bridge's project
            sessions.append(summary)

    for project, working in runner.external_sessions_by_project().items():
        for ws in working:
            summary = _summarize_working(ws, kind="external-session")
            summary["project"] = project
            sessions.append(summary)

    # Hosted (Direct Session) sessions, read from the *same* persistence container
    # store the app uses (``runner.persistence.hosted_state_store()``) — building
    # the runner already migrated any legacy ``hosted_state.json`` into the DB, so a
    # bare file-backed store would see nothing. The static record→instance mapper is
    # reused so we never re-derive the row shape.
    hosted_store = runner.persistence.hosted_state_store()
    for process_id, fields in hosted_store.load().items():
        inst = HostedManager._instance_from_record(process_id, fields)
        if fields.get("intentional_stop"):
            inst.status = InstanceStatus.STOPPED
        sessions.append(_summarize_instance(inst, kind="hosted"))

    # Agent-view background jobs (`claude --bg`). The supervisor has already
    # redacted every free-text field on these before they reach us.
    for job in list_background_jobs():
        sessions.append(_summarize_job(job))

    return sessions


def _summarize_instance(inst: RemoteControlInstance, *, kind: str) -> dict[str, Any]:
    """Summarize a :class:`RemoteControlInstance` (bridge or hosted) read-only.

    Only structural/lifecycle fields are surfaced — never log or transcript
    content. The id is the bridge's ``instance_id`` for a bridge, or the
    ``claustrum_process_id`` for a hosted session.

    The bridge id was the project name until #1020. A project may run several bridges
    (#778), so that id was not unique: every bridge on a project serialized the SAME id,
    `session_status` could only ever return whichever was registered first, and — once
    a working session's ``parent_instance`` became an instance_id — nothing a client
    could see joined a child session back to its owning bridge. ``project`` is still
    reported as its own field, so the human-meaningful name is not lost, and
    `session_status` still accepts a project name when it names exactly one bridge.
    """
    is_hosted = kind == "hosted"
    session_id = inst.claustrum_process_id if is_hosted else inst.instance_id
    summary: dict[str, Any] = {
        "id": session_id,
        "kind": kind,
        "channel": inst.channel,
        "project": inst.project,
        "label": inst.label,
        "status": inst.status.value,
        "resume_mode": inst.resume_mode,
        "started_at": inst.started_at.isoformat() if inst.started_at else None,
    }
    if is_hosted:
        summary["claude_session_uuid"] = inst.claude_session_uuid
        summary["is_orphan"] = inst.is_orphan
    else:
        summary["bridge_pid"] = inst.bridge_pid
        summary["keeper_pid"] = inst.keeper_pid
        summary["starter_session_id"] = inst.starter_session_id
    return summary


def _ms_to_iso(ms: int | None) -> str | None:
    """Convert an epoch-millisecond timestamp to an ISO-8601 UTC string (None passthrough).

    ``WorkingSession.started_at`` is epoch ms (an int), but bridge/hosted sessions emit
    ``started_at`` as an ISO string; normalize the working-session field so every session
    kind reports ``started_at`` in the same shape for an MCP client.
    """
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def _summarize_working(ws: WorkingSession, *, kind: str) -> dict[str, Any]:
    """Summarize a :class:`WorkingSession` (`claude agents --json` item) read-only."""
    return {
        "id": ws.local_uuid,
        "kind": kind,
        "channel": "remote-control",
        "project": None,
        "pid": ws.pid,
        "cwd": str(ws.cwd),
        "session_kind": ws.kind,
        "state": ws.state or None,
        "attribution": ws.attribution.value,
        "parent_instance": ws.parent_instance,
        "started_at": _ms_to_iso(ws.started_at),
    }


def _summarize_job(job: BackgroundJob) -> dict[str, Any]:
    """Summarize a :class:`BackgroundJob` (`claude --bg`) read-only.

    Free-text fields (``detail``, ``intent``, ``name``, …) are already redacted
    by :mod:`clauster.supervisor`; we surface only the lifecycle/structural ones
    and omit the redacted prose so the MCP egress carries no free-text at all.
    """
    return {
        "id": job.id,
        "kind": "background-agent",
        "channel": "background",
        "project": None,
        "cwd": str(job.cwd) if job.cwd is not None else None,
        "state": job.state or None,
        "tempo": job.tempo or None,
        "session_id": job.session_id,
        "worker_pid": job.worker_pid,
        "worker_alive": job.worker_alive,
        "created_at": job.created_at or None,
        "updated_at": job.updated_at or None,
    }


# --------------------------------------------------------------------------- #
# Tool definitions
# --------------------------------------------------------------------------- #
# The read tools are always exposed; the write tools below are gated behind
# ``mcp.allow_writes`` (#1010) so the default surface is read-only.
_READ_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_sessions",
        "title": "List Clauster sessions",
        "description": (
            "List every session Clauster can observe: managed bridges, hosted "
            "(Direct Session) sessions, agent-view background jobs, and external "
            "(unmanaged) working sessions. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "session_status",
        "title": "Clauster session status",
        "description": (
            "Report the status of one Clauster session by its id (a bridge "
            "instance id or project name, a hosted claustrum_process_id, a "
            "background-agent id, or a working-session uuid). A UNIQUE prefix of a "
            "bridge instance id also resolves; a prefix matching several bridges is "
            "reported as found: false with an 'ambiguous' list rather than guessing. "
            "Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "The session id to look up (as returned by list_sessions), a "
                        "bridge's project name, or a unique prefix of a bridge id."
                    ),
                }
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]

# Write tools (#950) — gated behind ``mcp.allow_writes`` (#1010, default off). They
# mutate bridge state, so the default read-only surface never advertises or dispatches
# them; an operator opts in explicitly.
_WRITE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "spawn_session",
        "title": "Start a Clauster bridge",
        "description": (
            "Start a claude bridge for a project (the bridge channel). resume_mode "
            "picks 'standard' (multi-session server) or 'pty' (single interactive "
            "session, true-resume). trust (default false) accepts the workspace-trust "
            "dialog — an untrusted directory is refused unless trust is true, exactly "
            "like the dashboard's Trust action. Returns the started (or already-live) "
            "session summary; created is false when an existing bridge was handed back."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "The project name to start in."},
                "spawn_mode": {"type": "string", "description": "same-dir | worktree | session."},
                "permission_mode": {
                    "type": "string",
                    "description": "default | plan | acceptEdits | auto | dontAsk | "
                    "bypassPermissions | inherit. 'inherit' is Clauster's own sentinel "
                    "for passing no --permission-mode flag at all, not a claude mode.",
                },
                "resume_mode": {"type": "string", "description": "standard | pty."},
                "custom_name": {"type": "string", "description": "Display name (standard only)."},
                "sandbox": {
                    "type": "string",
                    "description": "default | on | off (standard only). Disabled in this release "
                    "(#1037) — accepted but inert; returns via #1046.",
                },
                "trust": {
                    "type": "boolean",
                    "description": "Accept workspace-trust for this project (default false).",
                },
            },
            "required": ["project"],
            "additionalProperties": False,
        },
    },
    {
        "name": "stop_session",
        "title": "Stop a Clauster bridge",
        "description": (
            "Stop the bridge named by an id (a project name, a full instance id, or a "
            "UNIQUE prefix of one, as returned by list_sessions). Returns stopped: false "
            "when no managed bridge matches. A reference matching several bridges — an "
            "id prefix, or a project name with more than one instance — is REFUSED, not "
            "guessed: the reply is stopped: false with an 'ambiguous' list of the full "
            "ids. Retry with a specific instance id rather than treating the bridge as "
            "already stopped."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "The bridge to stop: project name, full instance id, or a unique "
                        "prefix of one."
                    ),
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "resume_session",
        "title": "Resume a Clauster bridge",
        "description": (
            "Resume a stopped/crashed bridge into its prior conversation, reusing its "
            "stored spawn/permission/resume modes. id is a project name, a full instance "
            "id, or a UNIQUE prefix of one (as returned by list_sessions). Returns "
            "resumed: false when nothing was revived — no bridge matched, or the "
            "project's one-live-standard-bridge cap handed back the already-live bridge "
            "instead of reviving the target (reason says which; session is that live "
            "bridge, NOT necessarily the one you named). Every reply echoes the id you "
            "asked for as id, so you can compare it against session.id to tell whether "
            "the live bridge is the one you named. A reference matching several bridges — "
            "an id prefix, or a project name with more than one instance — is REFUSED with "
            "an 'ambiguous' list of the full ids rather than guessing. Bridge channel "
            "only — hosted-session resume is not exposed here."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The bridge id to resume."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]


async def _tool_list_sessions(config: ClausterConfig, _args: dict[str, Any]) -> dict[str, Any]:
    """Run the ``list_sessions`` tool: gather and return every observable session."""
    sessions = await gather_sessions(config)
    return {"count": len(sessions), "sessions": sessions}


async def _tool_session_status(config: ClausterConfig, args: dict[str, Any]) -> dict[str, Any]:
    """Run the ``session_status`` tool: return one session by id.

    Raises :class:`ValueError` for a missing/blank id (surfaced to the client as
    an ``isError`` tool result) and reports ``found: false`` for an unknown id —
    never inventing a session that isn't there.
    """
    raw = args.get("id")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("session_status requires a non-empty string 'id'")
    wanted = raw.strip()
    sessions = await gather_sessions(config)
    for session in sessions:
        if session.get("id") == wanted:
            return {"found": True, "session": session}
    # A bridge's id is its instance_id since #1020, but a project name was the documented
    # way to name a bridge before that, so keep accepting one — while a project running
    # SEVERAL bridges (#778) genuinely doesn't name a single session. Report that instead
    # of returning whichever happened to be registered first, and hand back the ids the
    # caller can retry with.
    bridges = [s for s in sessions if s.get("kind") == "bridge" and s.get("project") == wanted]
    if len(bridges) == 1:
        return {"found": True, "session": bridges[0]}
    if bridges:
        return {"found": False, "id": wanted, "ambiguous": [s["id"] for s in bridges]}
    # A unique id PREFIX resolves too (#1099), in the same order the engine resolver uses:
    # exact id, then exact project name, then prefix — an abbreviation must never outrank
    # either exact form. Restricted to bridges because a bridge id is the only one the
    # tooling abbreviates (`clauster status` prints 8 characters); hosted / background /
    # working ids are echoed verbatim by `list_sessions`, so a partial one is a typo.
    # `wanted` is non-empty (validated above), so there is no prefixes-everything case.
    prefixed = sorted(
        (
            s
            for s in sessions
            if s.get("kind") == "bridge" and str(s.get("id", "")).startswith(wanted)
        ),
        key=lambda s: str(s.get("id", "")),
    )
    if len(prefixed) == 1:
        return {"found": True, "session": prefixed[0]}
    if prefixed:
        # Refused, not guessed — reporting the candidates so the caller can retry with a
        # longer prefix, exactly as `stop_session` / `resume_session` do.
        return {
            "found": False,
            "id": wanted,
            "ambiguous": [str(s.get("id", "")) for s in prefixed],
        }
    return {"found": False, "id": wanted}


def _require_id(tool: str, args: dict[str, Any]) -> str:
    """Return a non-empty string ``id`` arg or raise (surfaced as an isError result)."""
    raw = args.get("id")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{tool} requires a non-empty string 'id'")
    return raw.strip()


def _optional_str(tool: str, args: dict[str, Any], key: str) -> Any:
    """Return ``args[key]`` if a non-blank string, ``None`` if absent, else raise.

    This only guards the wire TYPE (so a JSON number/object can't reach the runner);
    the engine/runner validates the VALUE (a bad spawn_mode etc. → InvalidSpawnOption
    → isError). Return type is ``Any`` — same as ``dict.get`` in the ``/api`` spawn
    route — because a checked-but-unvalidated string feeds the engine's ``Literal``
    mode params (``SpawnMode`` etc.), which the runner narrows at runtime.
    """
    if key not in args or args[key] is None:
        return None
    val = args[key]
    if not isinstance(val, str):
        raise ValueError(f"{tool} '{key}' must be a string")
    stripped = val.strip()
    return stripped or None


async def _tool_spawn_session(config: ClausterConfig, args: dict[str, Any]) -> dict[str, Any]:
    """Run ``spawn_session``: start a bridge via the engine facade (fail-closed trust).

    A thin wrapper over :meth:`ClausterEngine.start` — the same ``spawn_detailed``
    the ``POST /api/instances`` route calls. ``trust`` defaults to False, so an
    untrusted directory raises :class:`NotTrusted` (an isError result) instead of
    being silently trusted from an MCP client.
    """
    from .engine import ClausterEngine

    project = args.get("project")
    if not isinstance(project, str) or not project.strip():
        raise ValueError("spawn_session requires a non-empty string 'project'")
    trust = args.get("trust", False)
    if not isinstance(trust, bool):
        raise ValueError("spawn_session 'trust' must be a boolean")
    with ClausterEngine(config) as engine:
        await engine.hydrate()
        outcome = await engine.start(
            project.strip(),
            spawn_mode=_optional_str("spawn_session", args, "spawn_mode"),
            permission_mode=_optional_str("spawn_session", args, "permission_mode"),
            resume_mode=_optional_str("spawn_session", args, "resume_mode"),
            custom_name=_optional_str("spawn_session", args, "custom_name"),
            sandbox=_optional_str("spawn_session", args, "sandbox"),
            trust=trust,
        )
    return {
        "created": outcome.created,
        "reason": outcome.reason,
        "warnings": list(outcome.warnings),
        "session": _summarize_instance(outcome.instance, kind="bridge"),
    }


async def _tool_stop_session(config: ClausterConfig, args: dict[str, Any]) -> dict[str, Any]:
    """Run ``stop_session``: stop the bridge resolved from ``id`` (bridge channel)."""
    from .engine import ClausterEngine

    wanted = _require_id("stop_session", args)
    with ClausterEngine(config) as engine:
        await engine.hydrate()
        inst = await engine.stop(wanted)
        # An ambiguous id prefix resolves to nothing rather than guessing (#1099).
        # Reported as `ambiguous`, matching `session_status`, because a bare
        # `{"stopped": false}` reads to an agent as "already stopped" — it would report
        # the bridge as down while it is still running, and never retry with a longer id.
        ambiguous = engine.bridge_id_candidates(wanted) if inst is None else []
    if inst is None:
        return {"stopped": False, "id": wanted, **({"ambiguous": ambiguous} if ambiguous else {})}
    return {"stopped": True, "session": _summarize_instance(inst, kind="bridge")}


async def _tool_resume_session(config: ClausterConfig, args: dict[str, Any]) -> dict[str, Any]:
    """Run ``resume_session``: resume the bridge resolved from ``id`` into its prior context."""
    from .engine import ClausterEngine

    wanted = _require_id("resume_session", args)
    with ClausterEngine(config) as engine:
        await engine.hydrate()
        outcome = await engine.resume_detailed(wanted)
        # An ambiguous id prefix resolves to nothing rather than guessing (#1099).
        ambiguous = engine.bridge_id_candidates(wanted) if outcome is None else []  # see stop
    if outcome is None:
        return {"resumed": False, "id": wanted, **({"ambiguous": ambiguous} if ambiguous else {})}
    body: dict[str, Any] = {
        "resumed": outcome.created,
        "id": wanted,
        "warnings": list(outcome.warnings),
        "session": _summarize_instance(outcome.instance, kind="bridge"),
    }
    # #1148: a standard bridge is capped at one live per project, and the cap declines a
    # resume by HANDING BACK the already-live bridge (usually a different instance_id;
    # the pty path can hand back the target itself) rather than raising. Answering that
    # with `resumed: true` told the agent it revived the session it asked for — it never
    # did, and it then addresses a bridge it never chose. Read `created`, and name why
    # with `reason`, exactly as `POST /api/instances/{id}/resume` does (#1145).
    if not outcome.created and outcome.reason:
        body["reason"] = outcome.reason
    return body


_READ_TOOL_HANDLERS = {
    "list_sessions": _tool_list_sessions,
    "session_status": _tool_session_status,
}
_WRITE_TOOL_HANDLERS = {
    "spawn_session": _tool_spawn_session,
    "stop_session": _tool_stop_session,
    "resume_session": _tool_resume_session,
}


def tools_for(*, allow_writes: bool) -> list[dict[str, Any]]:
    """Return the tool definitions advertised by ``tools/list`` for the active mode.

    Read tools always; the #950 write tools only when ``allow_writes`` (#1010).
    Kept in lockstep with :func:`handlers_for` so the advertised surface can never
    drift from the dispatchable one (asserted by the capability-sync test).
    """
    return [*_READ_TOOLS, *_WRITE_TOOLS] if allow_writes else list(_READ_TOOLS)


def handlers_for(*, allow_writes: bool) -> dict[str, Any]:
    """Return the tool handlers dispatchable by ``tools/call`` for the active mode.

    The write handlers are present only when ``allow_writes`` — so a ``spawn_session``
    call on a read-only server is an *unknown tool* (fail-closed), not a silent no-op.
    """
    return (
        {**_READ_TOOL_HANDLERS, **_WRITE_TOOL_HANDLERS}
        if allow_writes
        else dict(_READ_TOOL_HANDLERS)
    )


def capability_label(*, allow_writes: bool) -> str:
    """One-line description of the active tool surface (startup banner / logs)."""
    if allow_writes:
        return "read+write (list/status + spawn/stop/resume)"
    return "read-only (list/status; writes gated by mcp.allow_writes)"


# --------------------------------------------------------------------------- #
# JSON-RPC / MCP server
# --------------------------------------------------------------------------- #
class MCPServer:
    """A minimal stdio JSON-RPC 2.0 server speaking the MCP tool subset.

    Drives the ``initialize`` handshake, answers ``tools/list`` / ``tools/call``
    for the active tool set — the read tools always, the #950 write tools only when
    ``mcp.allow_writes`` is on (#1010, default off) — and replies to ``ping``.
    Unknown methods get a JSON-RPC ``method not found`` error; a tool that raises is
    returned as an ``isError`` tool result rather than crashing the server
    (fail-closed, never silently). On a read-only server a write-tool call is simply
    an unknown tool. Notifications (no ``id``) are acknowledged without a reply.
    """

    def __init__(self, config: ClausterConfig) -> None:
        """Bind the server to an already-loaded :class:`ClausterConfig`.

        The advertised tool set and the dispatch table are both derived from the
        one ``mcp.allow_writes`` flag (#1010), so ``tools/list`` and ``tools/call``
        agree on the active surface — read-only by default, +write when opted in.
        """
        self._config = config
        self._initialized = False
        self._allow_writes = config.mcp.allow_writes
        self._tools = tools_for(allow_writes=self._allow_writes)
        self._handlers = handlers_for(allow_writes=self._allow_writes)

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one parsed JSON-RPC message, returning a response or ``None``.

        ``None`` means "no reply" — the right behaviour for a JSON-RPC
        notification (a message with no ``id``), including
        ``notifications/initialized``.
        """
        if message.get("jsonrpc") != "2.0":
            return _error(message.get("id"), _INVALID_REQUEST, "jsonrpc must be '2.0'")
        method = message.get("method")
        if not isinstance(method, str):
            return _error(message.get("id"), _INVALID_REQUEST, "missing method")
        msg_id = message.get("id")
        params = message.get("params") or {}

        # Well-formed notifications (no ``id``) are never answered — a JSON-RPC peer treats
        # a reply to a notification as a protocol violation. (A malformed message is still
        # answered with an ``id: null`` error by the two validation branches above.) Ack the
        # one we track and silently drop any other, before the request branches can reply.
        if "id" not in message:
            if method == "notifications/initialized":
                self._initialized = True
            return None

        if method == "initialize":
            return self._on_initialize(msg_id, params)
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": self._tools})
        if method == "tools/call":
            return await self._on_tools_call(msg_id, params)
        return _error(msg_id, _METHOD_NOT_FOUND, f"unknown method: {method}")

    def _on_initialize(self, msg_id: int | str | None, params: dict[str, Any]) -> dict[str, Any]:
        """Answer ``initialize``: reply with the one protocol version we implement.

        Plus the tools *capability* flag and ``serverInfo`` — the tool list itself comes
        from ``tools/list``.
        """
        # Per the MCP lifecycle spec, never claim a version we don't implement: we speak
        # exactly one, so the reply is always PROTOCOL_VERSION whatever was requested.
        requested = params.get("protocolVersion")
        version = requested if requested == PROTOCOL_VERSION else PROTOCOL_VERSION
        return _result(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": _SERVER_NAME, "version": __version__},
            },
        )

    async def _on_tools_call(
        self, msg_id: int | str | None, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Answer ``tools/call``: run the named tool and wrap its result.

        A handler that raises is reported as an ``isError`` tool result (per the
        tools spec) so the client sees the failure as data, not a transport
        error — and never as a misleading empty success.
        """
        name = params.get("name")
        if not isinstance(name, str) or name not in self._handlers:
            return _result(msg_id, _tool_error(f"unknown tool: {name!r}"))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _result(msg_id, _tool_error("tool arguments must be an object"))
        try:
            payload = await self._handlers[name](self._config, arguments)
        except Exception as exc:  # noqa: BLE001 - surface as an isError tool result, never crash
            _log.warning("tool %s failed: %s", name, exc)
            return _result(msg_id, _tool_error(f"{name} failed: {exc}"))
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return _result(
            msg_id,
            {"content": [{"type": "text", "text": text}], "isError": False},
        )


def _result(msg_id: int | str | None, result: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response."""
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: int | str | None, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response."""
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict[str, Any]:
    """Build a ``tools/call`` result marked ``isError`` (a tool-level failure)."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


async def serve(config: ClausterConfig, reader: asyncio.StreamReader, writer: IO[str]) -> None:
    """Run the stdio MCP loop until EOF on ``reader``.

    Reads newline-delimited JSON-RPC messages, dispatches each through
    :class:`MCPServer`, and writes any response back as a single line. A line
    that isn't valid JSON yields a JSON-RPC parse error (never a crash); EOF ends
    the loop cleanly.
    """
    server = MCPServer(config)
    while True:
        try:
            line = await reader.readline()
        except ValueError:
            # An over-limit line: StreamReader raises ValueError when a single line exceeds
            # its buffer cap, and drops the buffered data. The tail of the oversized line
            # may still arrive and read as a fragment (answered with a further parse error),
            # but the loop always recovers — never crash on oversized input.
            if not _write(writer, _error(None, _PARSE_ERROR, "message too large")):
                return  # client hung up
            continue
        if not line:
            return  # EOF — client closed stdin
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError:
            if not _write(writer, _error(None, _PARSE_ERROR, "invalid JSON")):
                return
            continue
        if not isinstance(message, dict):
            if not _write(writer, _error(None, _INVALID_REQUEST, "message must be a JSON object")):
                return
            continue
        try:
            response = await server.handle(message)
        except Exception as exc:  # noqa: BLE001 - one bad message must not kill the loop
            _log.warning("dispatch failed: %s", exc)
            response = _error(message.get("id"), _INTERNAL_ERROR, "internal error")
        if response is not None and not _write(writer, response):
            return  # client hung up mid-stream — stop cleanly


def _write(writer: IO[str], payload: dict[str, Any]) -> bool:
    """Write one JSON-RPC message as a single newline-terminated stdout line.

    Returns ``True`` on success and ``False`` if the client has closed its read end
    (a normal hang-up): a ``BrokenPipeError``/``OSError`` on write is the signal to
    stop the loop cleanly rather than crash with a traceback and a non-zero exit.

    ``ensure_ascii`` is irrelevant to framing; the key invariant is one compact
    line with no embedded newline, which ``json.dumps`` (no ``indent``) gives us.
    """
    try:
        writer.write(json.dumps(payload, ensure_ascii=False) + "\n")
        writer.flush()
    except (BrokenPipeError, OSError):
        return False
    return True


async def _run_stdio(config: ClausterConfig) -> None:
    """Wire stdin to an asyncio reader and run :func:`serve` over real stdio."""
    loop = asyncio.get_running_loop()
    # An explicit, bounded line limit (vs the 64 KiB StreamReader default) so a
    # legitimate message is never rejected, while an unbounded line still can't grow
    # the buffer without limit — ``serve`` turns the over-limit case into a parse
    # error, not a crash.
    reader = asyncio.StreamReader(limit=_MAX_LINE_BYTES)
    protocol = asyncio.StreamReaderProtocol(reader)
    # Connect the reader to stdin; stdout is written synchronously (line-buffered,
    # tiny messages) so we don't need a transport for the write side.
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    await serve(config, reader, sys.stdout)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``clauster mcp``: load config, then serve stdio MCP.

    Returns a process exit code. Config errors fail closed with a clear stderr
    message and a non-zero code; a clean client disconnect (EOF) exits 0.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="clauster mcp",
        description="Run the Clauster MCP server over stdio (#527/#950). Read-only by "
        "default (list_sessions / session_status); set `mcp.allow_writes: true` to also "
        "expose the write tools (spawn/stop/resume_session).",
    )
    parser.add_argument("-c", "--config", help="path to clauster.yml")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        # yaml.YAMLError is not a ValueError; without it a malformed config tracebacks out
        # of the stdio server, where the traceback would also land on the wrong channel.
        print(f"clauster: config error: {exc}", file=sys.stderr)
        return 2

    # All human-readable output goes to stderr — stdout is the protocol channel
    # and must carry nothing but MCP messages (stdio transport requirement).
    label = capability_label(allow_writes=config.mcp.allow_writes)
    print(f"clauster mcp {__version__} | {label} | stdio", file=sys.stderr)
    try:
        asyncio.run(_run_stdio(config))
    except KeyboardInterrupt:
        return 0
    return 0
