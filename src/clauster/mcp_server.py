"""Read-only ``clauster mcp`` stdio MCP server (#527, v1).

Exposes Clauster's *observable* session state to an MCP client (Claude Desktop,
Claude Code, or any stdio MCP host) through two read-only tools:

``list_sessions``
    Every session Clauster can see — managed bridges, hosted (Direct Session)
    sessions, agent-view background jobs, and external (unmanaged) working
    sessions — summarized into one flat list.

``session_status``
    The detail of one session, looked up by its id.

Scope (v1, deliberately tight): **read-only**. There is no ``spawn_session`` /
``stop_session`` / ``resume_session`` and no token auth — both are explicitly
deferred to a follow-up. The server only *reports* what the dashboard's
``/api`` read routes already surface, reusing the very same read machinery
(:class:`~clauster.runner.SessionRunner`, :mod:`clauster.supervisor`, and the
persisted hosted-session store) rather than re-deriving any of it.

Transport / protocol. A minimal, dependency-free stdio JSON-RPC 2.0 server
implementing just the MCP messages a read-only tool host needs: ``initialize``,
the ``notifications/initialized`` ack, ``tools/list``, ``tools/call`` and
``ping``. We hand-roll this rather than pull in the official ``mcp`` SDK — for a
stdio-only, two-tool, read-only surface the SDK's async/SSE/Starlette dependency
tree is far more than is warranted, and a tiny in-tree server keeps the
distributed binary's supply-chain footprint unchanged and the whole path unit
testable without spawning a subprocess. Per the spec, messages are
newline-delimited single-line JSON on stdin/stdout and **all** human-readable
logging goes to stderr so it never corrupts the protocol stream.

Fail-closed posture. Mutation is structurally absent (no tool calls into a
spawn/stop path). A tool that raises is reported back as an ``isError`` tool
result — never a server crash and never a silent empty success. Session output
reuses Clauster's existing redaction: background-job free-text is already
redacted by :mod:`clauster.supervisor` before it reaches us, and the summaries
here surface only structural/lifecycle fields (ids, status, project, pids),
never raw transcript or log content.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from . import __version__
from .config import ClausterConfig, load_config

_log = logging.getLogger("clauster.mcp")

# The MCP revision this server implements. We echo back the client's requested
# version when we support it, else fall back to this (per the lifecycle spec).
PROTOCOL_VERSION = "2025-06-18"
_SERVER_NAME = "clauster"

# JSON-RPC 2.0 error codes we use.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603

# Max bytes for one JSON-RPC line. Our two read-only tools' replies are small, and
# a *request* is tiny (a method name + a short id) — so a generous-but-bounded cap
# keeps a single pathological/oversized line from growing the read buffer without
# limit, while never tripping on a legitimate message. An overlong line is drained
# and answered with a parse error rather than crashing the server (see ``serve``).
_MAX_LINE_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Session gathering — reuse the existing read machinery, never re-derive it.
# --------------------------------------------------------------------------- #
async def gather_sessions(config: ClausterConfig) -> list[dict[str, Any]]:
    """Collect every observable session into a flat list of plain dicts.

    Reuses the dashboard's read path verbatim: a :class:`SessionRunner` is built
    and reconciled once (``rediscover`` + ``poll_once``, the same liveness +
    ``agents --json`` cross-check the poll loop runs), then its managed bridges,
    tracked/external working sessions, the supervisor's background jobs, and the
    persisted hosted-session records are each mapped into a uniform summary. No
    session-listing logic is duplicated here — only assembled.

    Read-only: nothing is spawned, stopped, or mutated. Hosted sessions are read
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
    # Reconcile persisted bridges into live instances and refresh liveness +
    # the agents --json cross-check exactly as the FastAPI poll loop does, so a
    # one-shot process reports the same picture the dashboard would.
    await runner.rediscover()
    await runner.poll_once()

    tracked = runner.tracked_sessions_by_instance()
    for inst in runner.list_instances():
        sessions.append(_summarize_instance(inst, kind="bridge"))
        for ws in tracked.get(inst.project, []):
            sessions.append(_summarize_working(ws, kind="bridge-session"))

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


def _summarize_instance(inst: Any, *, kind: str) -> dict[str, Any]:
    """Summarize a :class:`RemoteControlInstance` (bridge or hosted) read-only.

    Only structural/lifecycle fields are surfaced — never log or transcript
    content. The id is the bridge's project (its registry key) for a bridge, or
    the ``claustrum_process_id`` for a hosted session.
    """
    is_hosted = kind == "hosted"
    session_id = inst.claustrum_process_id if is_hosted else inst.project
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


def _summarize_working(ws: Any, *, kind: str) -> dict[str, Any]:
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
        "started_at": ws.started_at,
    }


def _summarize_job(job: Any) -> dict[str, Any]:
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
_TOOLS: list[dict[str, Any]] = [
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
            "project name, a hosted claustrum_process_id, a background-agent id, "
            "or a working-session uuid). Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "The session id to look up (as returned by list_sessions).",
                }
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
    for session in await gather_sessions(config):
        if session.get("id") == wanted:
            return {"found": True, "session": session}
    return {"found": False, "id": wanted}


_TOOL_HANDLERS = {
    "list_sessions": _tool_list_sessions,
    "session_status": _tool_session_status,
}


# --------------------------------------------------------------------------- #
# JSON-RPC / MCP server
# --------------------------------------------------------------------------- #
class MCPServer:
    """A minimal stdio JSON-RPC 2.0 server speaking the read-only MCP subset.

    Drives the ``initialize`` handshake, answers ``tools/list`` / ``tools/call``
    for the two read-only tools, and replies to ``ping``. Unknown methods get a
    JSON-RPC ``method not found`` error; a tool that raises is returned as an
    ``isError`` tool result rather than crashing the server (fail-closed, never
    silently). Notifications (no ``id``) are acknowledged without a reply.
    """

    def __init__(self, config: ClausterConfig) -> None:
        """Bind the server to an already-loaded :class:`ClausterConfig`."""
        self._config = config
        self._initialized = False

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

        # Notifications (no ``id``) are never answered — a JSON-RPC peer treats a
        # reply to a notification as a protocol violation. Ack the one we track and
        # silently drop any other, before the request branches below can reply.
        if "id" not in message:
            if method == "notifications/initialized":
                self._initialized = True
            return None

        if method == "initialize":
            return self._on_initialize(msg_id, params)
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": _TOOLS})
        if method == "tools/call":
            return await self._on_tools_call(msg_id, params)
        return _error(msg_id, _METHOD_NOT_FOUND, f"unknown method: {method}")

    def _on_initialize(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Answer ``initialize``: negotiate the protocol version + advertise tools."""
        requested = params.get("protocolVersion")
        version = requested if isinstance(requested, str) and requested else PROTOCOL_VERSION
        return _result(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": _SERVER_NAME, "version": __version__},
            },
        )

    async def _on_tools_call(self, msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Answer ``tools/call``: run a read-only tool and wrap its result.

        A handler that raises is reported as an ``isError`` tool result (per the
        tools spec) so the client sees the failure as data, not a transport
        error — and never as a misleading empty success.
        """
        name = params.get("name")
        if not isinstance(name, str) or name not in _TOOL_HANDLERS:
            return _result(msg_id, _tool_error(f"unknown tool: {name!r}"))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _result(msg_id, _tool_error("tool arguments must be an object"))
        try:
            payload = await _TOOL_HANDLERS[name](self._config, arguments)
        except Exception as exc:  # noqa: BLE001 - surface as an isError tool result, never crash
            _log.warning("tool %s failed: %s", name, exc)
            return _result(msg_id, _tool_error(f"{name} failed: {exc}"))
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return _result(
            msg_id,
            {"content": [{"type": "text", "text": text}], "isError": False},
        )


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response."""
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response."""
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict[str, Any]:
    """Build a ``tools/call`` result marked ``isError`` (a tool-level failure)."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


async def serve(config: ClausterConfig, reader: asyncio.StreamReader, writer: Any) -> None:
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
            # An over-limit line: StreamReader raises ValueError / LimitOverrunError
            # when a single line exceeds its buffer cap, AND discards the overlong
            # data through the newline as it does so — so the buffer is already
            # realigned on the next message. Answer with a parse error and keep
            # serving; never crash the loop on oversized input.
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


def _write(writer: Any, payload: dict[str, Any]) -> bool:
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
        description="Run the read-only Clauster MCP server over stdio (#527).",
    )
    parser.add_argument("-c", "--config", help="path to clauster.yml")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"clauster: config error: {exc}", file=sys.stderr)
        return 2

    # All human-readable output goes to stderr — stdout is the protocol channel
    # and must carry nothing but MCP messages (stdio transport requirement).
    print(f"clauster mcp {__version__} | read-only | stdio", file=sys.stderr)
    try:
        asyncio.run(_run_stdio(config))
    except KeyboardInterrupt:
        return 0
    return 0
