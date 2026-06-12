"""Hosted-channel session logic (CL-4 — Stage B interactive hosted sessions).

clauster's *hosted* channel runs a headless ``claude`` stream-json agent on plain
pipes through the claustrum daemon — not a ``remote-control`` bridge. This module
is the channel's engine: it builds the observed claude-ssh spawn argv, drives one
process via :class:`~clauster.claustrum_client.ClaustrumClient`, routes the
agent's stdout NDJSON frames, sends user messages, and stops the session.

Frame routing splits the stdout stream two ways (per
``scratch/ccd-remote-protocol-spec.md`` §5–6 + ``scratch/hosted-protocol-empirical.md``):

* **control_request** frames are the control plane. The MCP ``initialize``
  handshake is auto-answered with an empty success so a session never wedges on
  it. *Every other* control_request — notably tool-permission prompts
  (``can_use_tool``) — is **parked** (recorded, surfaced, never auto-answered):
  fail-closed, the agent waits until something explicitly responds. CL-5 builds
  the approve/deny UI on top of :attr:`HostedSession.pending_requests`.
* **data** frames (``system``/``assistant``/``user``/``result``/…) are redacted
  (defense-in-depth, every string leaf), appended to a bounded ring buffer with a
  monotonic ``event_seq``, and fanned out to browser subscribers. The ``system``
  init frame yields :attr:`HostedSession.claude_session_uuid` (drives ``--resume``).

CL-4b (#231) wired this engine to the app: the app-layer ``channel`` dispatch in
``app.api_spawn`` routes ``channel="hosted"`` requests to :class:`HostedManager`
(a registry kept separate from the project-keyed bridge runner), plus the
``/api/instances/{id}/message`` endpoint and the ``/ws/hosted/{id}`` WebSocket.
Still ahead: the input/live-view UI (CL-4c), the permission approve/deny UI
(CL-5), and ``state.json`` persistence + reattach for clauster-restart resilience
(CL-6). This module stays a pure, daemon-driven library — the same posture CL-1
took for :mod:`clauster.claustrum_client`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .claustrum_client import ClaustrumClient, ClaustrumError, ProcessStream
from .models import InstanceStatus, RemoteControlInstance
from .redact import sanitize_line

logger = logging.getLogger(__name__)

# HostedSession status string → the dashboard's InstanceStatus enum.
_STATUS_MAP: dict[str, InstanceStatus] = {
    "starting": InstanceStatus.STARTING,
    "running": InstanceStatus.RUNNING,
    "stopped": InstanceStatus.STOPPED,
    "crashed": InstanceStatus.CRASHED,
    "error": InstanceStatus.ERROR,
}

# The observed claude-ssh headless spawn contract (the "minimal mimic": no
# --plugin-dir/--allowedTools/--settings, no --remote-control — adding
# stream-json output is exactly what flips off the cloud door, so this channel is
# local-only by construction). Captured from a live Desktop-driven session
# (hosted-protocol-empirical.md, probe Q3 / RESUME-UUID phase 4).
_STREAM_JSON_ARGS: tuple[str, ...] = (
    "--output-format",
    "stream-json",
    "--input-format",
    "stream-json",
    "--verbose",
    "--permission-prompt-tool",
    "stdio",
    "--include-partial-messages",
    "--replay-user-messages",
)

# Control-request subtypes we answer automatically. Everything else is parked —
# we never auto-respond to a tool-permission request (fail closed).
_AUTO_ACK_SUBTYPES: frozenset[str] = frozenset({"initialize", "mcp_message"})

# Browser-facing ring depth (parsed events, not raw bytes) and per-subscriber
# queue depth before a slow viewer starts dropping with an honest marker.
_DEFAULT_RING_SIZE = 4096
_DEFAULT_QUEUE_MAXSIZE = 2048

# How long stop() waits for a clean exit after SIGINT before escalating to KILL.
_STOP_GRACE_SECONDS = 5.0


def build_hosted_argv(
    claude_binary: str,
    *,
    permission_mode: str,
    resume_uuid: str | None = None,
) -> list[str]:
    """Build the headless stream-json spawn argv for a hosted session.

    ``claude_binary`` must already be an absolute, resolved path (validate-before-
    spawn is the caller's job). ``resume_uuid`` adds ``--resume <uuid>`` for
    deterministic conversation resume (CL-7); omit it for a fresh session.
    """
    argv = [claude_binary, *_STREAM_JSON_ARGS, "--permission-mode", permission_mode]
    if resume_uuid is not None:
        argv += ["--resume", resume_uuid]
    return argv


class HostedSessionError(ClaustrumError):
    """Raised when a hosted-session operation is invalid for the current state."""


@dataclass
class _Subscriber:
    """One browser watcher's bounded queue with a never-block overflow marker.

    Mirrors :class:`clauster.claustrum_client._Subscriber`: a full queue drops the
    event and counts it, and the next event the queue can take is preceded by a
    ``gap`` marker carrying the dropped count, so a slow viewer never stalls the
    single daemon reader and gaps stay honest.
    """

    queue: asyncio.Queue[dict[str, Any]]
    dropped: int = 0

    def offer(self, event: dict[str, Any]) -> None:
        """Enqueue ``event`` for this watcher, never blocking the caller."""
        if self.dropped:
            marker = {"type": "gap", "dropped": self.dropped}
            try:
                self.queue.put_nowait(marker)
            except asyncio.QueueFull:
                self.dropped += 1
                return
            self.dropped = 0
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1


@dataclass
class _ControlRequest:
    """A parked control-plane request awaiting an explicit response (CL-5)."""

    request_id: str
    subtype: str
    request: dict[str, Any]


class HostedSession:
    """One headless stream-json ``claude`` session driven over the claustrum daemon.

    Construct with a connected :class:`ClaustrumClient`, a client-chosen
    ``process_id`` (a ULID — claustrum's spawn contract is client-keyed), and the
    resolved ``claude`` binary; then :meth:`start` to spawn and begin pumping the
    stream. Read live events via :meth:`subscribe`, drive the conversation with
    :meth:`send_message`, answer parked control requests with
    :meth:`respond_control`, and end it with :meth:`stop`.
    """

    def __init__(
        self,
        client: ClaustrumClient,
        process_id: str,
        claude_binary: str,
        *,
        ring_size: int = _DEFAULT_RING_SIZE,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        stop_grace: float = _STOP_GRACE_SECONDS,
    ) -> None:
        """Configure the session; nothing spawns until :meth:`start`."""
        self._client = client
        self._process_id = process_id
        self._claude_binary = claude_binary
        self._queue_maxsize = queue_maxsize
        self._stop_grace = stop_grace
        self.status = "starting"
        self.exit_code: int | None = None
        self.claude_session_uuid: str | None = None
        self.agent_pid: int | None = None
        self.agent_proc_start: float | None = None
        self._ring: deque[dict[str, Any]] = deque(maxlen=ring_size)
        self._event_seq = 0
        self._subscribers: list[_Subscriber] = []
        self._pending: dict[str, _ControlRequest] = {}
        self._stream: ProcessStream | None = None
        self._source: asyncio.Queue[dict[str, Any]] | None = None
        self._pump_task: asyncio.Task[None] | None = None

    @property
    def process_id(self) -> str:
        """The client-chosen daemon process id for this session."""
        return self._process_id

    @property
    def pending_requests(self) -> list[_ControlRequest]:
        """Snapshot of parked control requests awaiting a response (CL-5 surfaces these)."""
        return list(self._pending.values())

    @property
    def last_event_seq(self) -> int:
        """Highest clauster-side ``event_seq`` emitted so far (the WS replay cursor)."""
        return self._event_seq

    async def start(
        self,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        permission_mode: str = "acceptEdits",
        resume_uuid: str | None = None,
        want_pid: bool = False,
    ) -> None:
        """Spawn the agent and begin pumping its stream.

        Subscribes to the process stream *before* spawning so no early frame is
        missed, then launches the background pump. ``want_pid`` opts into the
        claustrum CT-1 ``pid``/``startTime`` result fields when the daemon
        supports them (absent → :attr:`agent_pid` stays ``None``).
        """
        if self._pump_task is not None:
            raise HostedSessionError("hosted session already started")
        argv = build_hosted_argv(
            self._claude_binary, permission_mode=permission_mode, resume_uuid=resume_uuid
        )
        self._stream = self._client.stream(self._process_id)
        self._source = self._stream.subscribe()
        result = await self._client.spawn(
            self._process_id, argv[0], args=argv[1:], cwd=cwd, env=env, want_pid=want_pid
        )
        pid = result.get("pid")
        self.agent_pid = pid if isinstance(pid, int) else None
        start_time = result.get("startTime")
        self.agent_proc_start = float(start_time) if isinstance(start_time, (int, float)) else None
        self.status = "running"
        self._pump_task = asyncio.create_task(self._pump())

    async def send_message(self, text: str) -> None:
        """Send one user turn to the agent as a stream-json input frame.

        The minimal SDK input shape ``{"type":"user","message":{"role","content"}}``
        is what ``--input-format stream-json`` accepts; Desktop adds uuid/session_id/
        timestamp envelopes but the agent does not require them.
        """
        if self.status != "running":
            raise HostedSessionError(f"cannot send to a {self.status} hosted session")
        await self._write_stdin({"type": "user", "message": {"role": "user", "content": text}})

    async def respond_control(self, request_id: str, response: dict[str, Any]) -> None:
        """Answer a parked control request with a success ``control_response``.

        Removes it from :attr:`pending_requests` and fans out a ``control_resolved``
        event so other watchers (and browser reconnects replaying the ring) see the
        request is answered, not still actionable. CL-5 supplies the permission
        allow/deny ``response`` payload (``{"behavior": ...}``); this owns transport.
        """
        if request_id not in self._pending:
            raise HostedSessionError(f"no parked control request {request_id!r}")
        del self._pending[request_id]
        await self._send_control_response(request_id, response)
        self._emit(
            {
                "type": "control_resolved",
                "request_id": request_id,
                "behavior": response.get("behavior"),
            }
        )

    async def stop(self) -> None:
        """Interrupt the agent (SIGINT), then escalate to SIGKILL if it lingers.

        Idempotent: stopping an already-exited session is a no-op. The pump task
        is cancelled and the stream subscription dropped on the way out.
        """
        if self.status in ("stopped", "crashed", "error"):
            await self._teardown()
            return
        try:
            await self._client.kill(self._process_id, signal="INT")
            if self._stream is not None:
                try:
                    await asyncio.wait_for(self._stream.exited.wait(), timeout=self._stop_grace)
                except TimeoutError:
                    await self._client.kill(self._process_id, signal="KILL")
        except ClaustrumError as exc:  # pragma: no cover - daemon loss during stop (CL-4b)
            self.status = "error"
            self._emit({"type": "lost", "reason": f"stop failed: {exc}"})
        finally:
            await self._teardown()

    def subscribe(self, after_seq: int = 0) -> asyncio.Queue[dict[str, Any]]:
        """Register a browser watcher, replaying ring events past ``after_seq``.

        Returns a bounded queue pre-loaded with every retained event whose
        ``event_seq`` exceeds ``after_seq`` (a ``gap`` marker first if the ring has
        already evicted past the cursor), then fed live. :meth:`unsubscribe` when
        the consumer goes away.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_maxsize)
        sub = _Subscriber(queue)
        if self._ring and self._ring[0]["event_seq"] > after_seq + 1:
            sub.offer({"type": "gap", "from_seq": after_seq, "to_seq": self._ring[0]["event_seq"]})
        for event in self._ring:
            if event["event_seq"] > after_seq:
                sub.offer(event)
        self._subscribers.append(sub)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Drop a watcher's queue (called when its consumer disconnects)."""
        self._subscribers = [s for s in self._subscribers if s.queue is not queue]

    # -- internals ---------------------------------------------------------

    async def _pump(self) -> None:
        """Drain the process stream, routing each event until exit or cancel."""
        source = self._source
        if source is None:  # pragma: no cover - start() always sets it before pumping
            return
        try:
            while True:
                event = await source.get()
                etype = event.get("type")
                if etype == "line":
                    await self._on_line(event.get("stream"), event.get("line", ""))
                elif etype == "exit":
                    self._on_exit(event.get("exit_code"))
                    return
                elif etype == "overflow":  # pragma: no cover - client-queue overflow (CL-1 tests)
                    self._emit({"type": "gap", "dropped": event.get("dropped", 0)})
        except asyncio.CancelledError:
            raise
        except ClaustrumError as exc:  # pragma: no cover - daemon loss mid-pump (CL-4b)
            # A daemon-side failure surfaced through the reader; report it, don't swallow.
            self.status = "error"
            self._emit({"type": "lost", "reason": str(exc)})

    async def _on_line(self, stream: Any, line: str) -> None:
        """Route one reassembled output line: stderr/non-JSON as text, else by frame type."""
        if stream == "stderr":
            self._emit({"type": "stderr", "text": sanitize_line(line)})
            return
        if not line.strip():
            return
        try:
            frame = json.loads(line)
        except ValueError:
            # Not NDJSON — forward as opaque text rather than dropping it silently.
            self._emit({"type": "text", "text": sanitize_line(line)})
            return
        if not isinstance(frame, dict):
            self._emit({"type": "text", "text": sanitize_line(line)})
            return
        if frame.get("type") == "control_request":
            await self._handle_control_request(frame)
            return
        self._capture_session_uuid(frame)
        self._emit({"type": "frame", "frame": _redact_obj(frame)})

    async def _handle_control_request(self, frame: dict[str, Any]) -> None:
        """Auto-ack an MCP-handshake request; park anything else (fail-closed)."""
        request_id = frame.get("request_id")
        request = frame.get("request")
        request = request if isinstance(request, dict) else {}
        subtype = request.get("subtype")
        if not isinstance(request_id, str):
            logger.warning("hosted: control_request without a string request_id; ignoring")
            return
        if isinstance(subtype, str) and subtype in _AUTO_ACK_SUBTYPES:
            await self._send_control_response(request_id, {})
            self._emit({"type": "control_ack", "request_id": request_id, "subtype": subtype})
            return
        # Park everything else (tool-permission prompts, unknown subtypes): never
        # auto-answer a request that could grant a capability. CL-5 responds.
        parked = _ControlRequest(request_id, subtype if isinstance(subtype, str) else "", request)
        self._pending[request_id] = parked
        self._emit(
            {
                "type": "control_request",
                "request_id": request_id,
                "subtype": parked.subtype,
                "request": _redact_obj(request),
            }
        )

    def _capture_session_uuid(self, frame: dict[str, Any]) -> None:
        """Latch the first ``session_id`` seen (drives ``--resume``); never overwrite it."""
        if self.claude_session_uuid is not None:
            return
        sid = frame.get("session_id")
        if isinstance(sid, str) and sid:
            self.claude_session_uuid = sid

    def _on_exit(self, exit_code: Any) -> None:
        """Latch the terminal status (stopped/crashed) and emit the exit event."""
        self.exit_code = exit_code if isinstance(exit_code, int) else None
        self.status = "stopped" if self.exit_code == 0 else "crashed"
        self._emit({"type": "exit", "exit_code": self.exit_code})

    async def _write_stdin(self, frame: dict[str, Any]) -> None:
        """Serialize one NDJSON frame and write it to the agent's stdin."""
        data = (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")
        await self._client.stdin(self._process_id, data)

    async def _send_control_response(self, request_id: str, response: dict[str, Any]) -> None:
        """Write a success ``control_response`` for ``request_id`` to stdin."""
        await self._write_stdin(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": response,
                },
            }
        )

    def _emit(self, payload: dict[str, Any]) -> None:
        """Stamp an ``event_seq``, append to the ring, and fan out to subscribers."""
        self._event_seq += 1
        event = {"event_seq": self._event_seq, **payload}
        self._ring.append(event)
        for sub in self._subscribers:
            sub.offer(event)

    async def _teardown(self) -> None:
        """Cancel the pump task and drop the stream subscription (idempotent)."""
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, ClaustrumError):
                pass
            self._pump_task = None
        if self._stream is not None and self._source is not None:
            self._stream.unsubscribe(self._source)
            self._source = None


class HostedManager:
    """Owns the live :class:`HostedSession` objects and their dashboard instances.

    Kept separate from :class:`~clauster.runner.SessionRunner`'s bridge registry,
    which is project-keyed and bridge-shaped (one bridge per project): hosted
    sessions are keyed by a client-chosen id and there may be several per project.
    A deliberate divergence from the design doc's "single registry" — the bridge
    registry's invariants don't fit, and keeping them apart means hosted plumbing
    touches no bridge-lifecycle code. The dashboard unions both for display.
    """

    def __init__(self) -> None:
        """Create an empty manager (no sessions until :meth:`spawn`)."""
        self._sessions: dict[str, HostedSession] = {}
        self._instances: dict[str, RemoteControlInstance] = {}

    def list_instances(self) -> list[RemoteControlInstance]:
        """Snapshot of every hosted instance, each synced to its live session state."""
        return [self._synced(inst) for inst in self._instances.values()]

    def get_instance(self, hosted_id: str) -> RemoteControlInstance | None:
        """Return the hosted instance for ``hosted_id`` (status-synced), or None."""
        inst = self._instances.get(hosted_id)
        return self._synced(inst) if inst is not None else None

    def session(self, hosted_id: str) -> HostedSession | None:
        """Return the live :class:`HostedSession` for ``hosted_id``, or None."""
        return self._sessions.get(hosted_id)

    async def spawn(
        self,
        client: ClaustrumClient,
        *,
        project: str,
        label: str,
        cwd: str,
        claude_binary: str,
        permission_mode: str,
        resume_uuid: str | None = None,
    ) -> RemoteControlInstance:
        """Start a hosted session and register its dashboard instance.

        ``client`` is the connected daemon client (the caller sources it from
        ``app.state.claustrum_daemon``). Propagates :class:`ClaustrumError` if the
        spawn RPC fails — the caller maps it to an HTTP error and nothing is
        registered.
        """
        process_id = uuid.uuid4().hex
        session = HostedSession(client, process_id, claude_binary)
        await session.start(
            cwd=cwd, permission_mode=permission_mode, resume_uuid=resume_uuid, want_pid=True
        )
        instance = RemoteControlInstance(
            project=project,
            label=label,
            channel="hosted",
            permission_mode=permission_mode,  # type: ignore[arg-type]
            claustrum_process_id=process_id,
            agent_pid=session.agent_pid,
            agent_proc_start=session.agent_proc_start,
            status=InstanceStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        self._sessions[process_id] = session
        self._instances[process_id] = instance
        return self._synced(instance)

    async def send(self, hosted_id: str, text: str) -> None:
        """Route a user turn to a hosted session (404/409 mapping is the caller's)."""
        await self._require(hosted_id).send_message(text)

    async def respond(self, hosted_id: str, request_id: str, response: dict[str, Any]) -> None:
        """Answer a parked control request on a hosted session (caller maps 404/409)."""
        await self._require(hosted_id).respond_control(request_id, response)

    async def stop(self, hosted_id: str) -> RemoteControlInstance:
        """Stop a hosted session and return its final (status-synced) instance."""
        await self._require(hosted_id).stop()
        return self._synced(self._instances[hosted_id])

    async def aclose(self) -> None:
        """Stop every live hosted session (app shutdown)."""
        for session in list(self._sessions.values()):
            await session.stop()

    def _require(self, hosted_id: str) -> HostedSession:
        """Return the live session for ``hosted_id`` or raise ``HostedSessionError``."""
        session = self._sessions.get(hosted_id)
        if session is None:
            raise HostedSessionError(f"no such hosted session: {hosted_id}")
        return session

    def _synced(self, instance: RemoteControlInstance) -> RemoteControlInstance:
        """Reflect the live session's status + captured uuid onto its instance row."""
        session = self._sessions.get(instance.claustrum_process_id or "")
        if session is not None:
            instance.status = _STATUS_MAP.get(session.status, instance.status)
            if session.claude_session_uuid:
                instance.claude_session_uuid = session.claude_session_uuid
            instance.daemon_last_seq = max(instance.daemon_last_seq, session.last_event_seq)
        return instance


def _redact_obj(obj: Any) -> Any:
    """Recursively sanitize every string leaf of a parsed JSON frame.

    Defense-in-depth over the structured stream: the same redaction the WS bridge
    log applies (ANSI strip + id/secret masking via :func:`sanitize_line`) is run
    on each string value, so a session/env identifier or obvious secret embedded
    anywhere in tool output or assistant text never reaches a browser subscriber.
    """
    if isinstance(obj, str):
        return sanitize_line(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj
