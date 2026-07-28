"""Hosted-channel session logic (CL-4 — Stage B interactive hosted sessions).

clauster's *hosted* channel runs a headless ``claude`` stream-json agent on plain
pipes through the claustrum daemon — not a ``remote-control`` bridge. This module
is the channel's engine: it builds the observed claude-ssh spawn argv, drives one
process via :class:`~clauster.claustrum_client.ClaustrumClient`, routes the
agent's stdout NDJSON frames, sends user messages, and stops the session.

Frame routing splits the stdout stream two ways (per
``scratch/ccd-remote-protocol-spec.md`` §5–6 + ``scratch/hosted-protocol-empirical.md``):

* **control_request** frames are the control plane. The MCP handshake/transport
  subtypes (``initialize``, ``mcp_message`` — :data:`_AUTO_ACK_SUBTYPES`) are
  auto-answered with an empty success so a session never wedges on them. *Every
  other* control_request — notably tool-permission prompts (``can_use_tool``) — is
  **parked** (recorded, surfaced, never auto-answered): fail-closed, the agent waits
  until something explicitly responds. The approve/deny UI (CL-5) is built on
  :attr:`HostedSession.pending_requests`.
* **data** frames (``system``/``assistant``/``user``/``result``/…) are redacted
  (defense-in-depth, every string leaf), appended to a bounded ring buffer with a
  monotonic ``event_seq``, and fanned out to browser subscribers. The ``system``
  init frame yields :attr:`HostedSession.claude_session_uuid` (drives ``--resume``).

CL-4b (#231) wired this engine to the app: the app-layer ``channel`` dispatch in
``app.api_spawn`` routes ``channel="hosted"`` requests to :class:`HostedManager`
(a registry kept separate from the project-keyed bridge runner), plus the
``/api/instances/{id}/message`` endpoint and the ``/ws/hosted/{id}`` WebSocket.
The input/live-view UI (CL-4c), the permission approve/deny UI (CL-5), and
persistence + reattach for clauster-restart resilience (CL-6,
:mod:`clauster.hosted_state`) have since shipped on top of it. This module stays a
pure, daemon-driven library — the same posture CL-1 took for
:mod:`clauster.claustrum_client`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import procutil
from .claustrum_client import ClaustrumClient, ClaustrumError, ProcessStream, _Subscriber
from .config import PermissionMode
from .hosted_events import GapRangeEvent, HostedEvent, StdinFrame
from .models import InstanceStatus, RemoteControlInstance
from .redact import sanitize_line
from .state import KeyedStore

logger = logging.getLogger(__name__)

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

# Browser-facing ring depth (parsed events, not raw bytes). The per-subscriber queue
# default is sized to hold a full ring snapshot — the ring plus the one leading "gap"
# marker subscribe() may prepend — so a first-view reconnect never drops the newest
# events. HostedSession.__init__ raises any smaller queue to this floor (#422).
_DEFAULT_RING_SIZE = 4096
_DEFAULT_QUEUE_MAXSIZE = _DEFAULT_RING_SIZE + 1

# How long stop() waits for a clean exit after SIGINT before escalating to KILL.
_STOP_GRACE_SECONDS = 5.0


def build_hosted_argv(
    claude_binary: str,
    *,
    permission_mode: PermissionMode,
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
        stop_grace: float | None = None,
        on_permission_needed: Callable[[str, str], None] | None = None,
    ) -> None:
        """Configure the session; nothing spawns until :meth:`start`.

        ``stop_grace`` defaults to the module-level :data:`_STOP_GRACE_SECONDS`,
        resolved at construction (not import) so a test can shorten it via the
        global — including for sessions that :class:`HostedManager` builds internally.

        ``on_permission_needed`` is an optional best-effort callback invoked with
        ``(process_id, subtype)`` whenever a control request is *parked* (a tool-
        permission prompt or unknown subtype that is never auto-answered) — the #432
        "come look" hook. It must not raise and must not block: it is called inline on
        the stream-pump path, so the wiring schedules a fire-and-forget webhook and
        returns. ``None`` (the default, used in unit tests) disables it.
        """
        self._client = client
        self._process_id = process_id
        self._claude_binary = claude_binary
        self._on_permission_needed = on_permission_needed
        # Raise any caller-supplied queue to the full-snapshot floor: a smaller queue would
        # keep the OLDEST replayed events and drop the freshest on a first-view reconnect
        # (_Subscriber.offer drops the NEW event when full). See _DEFAULT_QUEUE_MAXSIZE (#422).
        self._queue_maxsize = max(queue_maxsize, ring_size + 1)
        self._stop_grace = stop_grace if stop_grace is not None else _STOP_GRACE_SECONDS
        self.status: InstanceStatus = InstanceStatus.STARTING
        self.exit_code: int | None = None
        self.claude_session_uuid: str | None = None
        self.agent_pid: int | None = None
        self.agent_proc_start: float | None = None
        # Highest *daemon* frame seq drained — the reattach replay cursor across
        # clauster restarts (distinct from _event_seq, the clauster-side ring seq).
        self.daemon_last_seq = 0
        self._ring: deque[Mapping[str, Any]] = deque(maxlen=ring_size)
        self._event_seq = 0
        # Subscriber list: the DEPTH-bound (per-subscriber queue) is bounded above by
        # _queue_maxsize; the BREADTH-bound (list length) is the count of live WS
        # connections to this one session — unsubscribe() drops each on disconnect, so
        # it tracks concurrent viewers and is not an unbounded accumulation.
        self._subscribers: list[_Subscriber] = []
        self._pending: dict[str, _ControlRequest] = {}
        # Latched at the start of stop() so a concurrent respond_control fails closed
        # rather than writing a control_response into a session being killed.
        self._stopping = False
        self._stream: ProcessStream | None = None
        self._source: asyncio.Queue[Mapping[str, Any]] | None = None
        self._pump_task: asyncio.Task[None] | None = None

    @property
    def process_id(self) -> str:
        """The client-chosen daemon process id for this session."""
        return self._process_id

    @property
    def pending_requests(self) -> list[_ControlRequest]:
        """Snapshot of parked control requests awaiting a response (CL-5 surfaces these)."""
        return list(self._pending.values())

    async def start(
        self,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        permission_mode: PermissionMode = "acceptEdits",
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
        try:
            result = await self._client.spawn(
                self._process_id, argv[0], args=argv[1:], cwd=cwd, env=env, want_pid=want_pid
            )
        except BaseException:
            # spawn RPC failed / timed out / was cancelled — drop the subscription we just made
            # so we don't leak an undrained subscriber on the stream (mirrors reattach not-found).
            self._stream.unsubscribe(self._source)
            self._stream, self._source = None, None
            raise
        pid = result.get("pid")
        self.agent_pid = pid if isinstance(pid, int) else None
        start_time = result.get("startTime")
        self.agent_proc_start = float(start_time) if isinstance(start_time, (int, float)) else None
        self.status = InstanceStatus.RUNNING
        self._pump_task = asyncio.create_task(self._pump())

    async def reattach(self, from_seq: int = 0) -> dict[str, Any]:
        """Reattach to an already-running daemon process, replaying frames past ``from_seq``.

        Used on clauster restart (CL-6): the agent kept running on the daemon while
        we were down. Subscribes before the ``process.reattach`` RPC so no replayed
        frame is missed, then — if the process is *found* — pumps the replay + live
        tail. A not-found process means the session was lost while we were down, so
        status latches to ``crashed`` and nothing is pumped. Returns the daemon's
        ``{found, running, firstSeq, lastSeq}`` result. ``from_seq`` is the persisted
        :attr:`daemon_last_seq`; a stale/zero value only costs replay overlap (the
        client de-dupes by seq), never a double-emit.
        """
        if self._pump_task is not None:
            raise HostedSessionError("hosted session already started")
        self._stream = self._client.stream(self._process_id)
        self._source = self._stream.subscribe()
        try:
            result = await self._client.reattach(self._process_id, from_seq)
        except BaseException:
            # reattach RPC failed / cancelled — drop the subscription we just made, mirroring
            # start(); reattach_all() discards the session on error, so nothing else would.
            self._stream.unsubscribe(self._source)
            self._stream, self._source = None, None
            raise
        if not result.get("found"):
            # Session gone while we were down — drop the subscription we just made.
            self._stream.unsubscribe(self._source)
            self._stream, self._source = None, None
            self.status = InstanceStatus.CRASHED
            return result
        self.daemon_last_seq = max(self.daemon_last_seq, from_seq)
        # If not running, the exit frame (seq > from_seq) replays through the pump,
        # which latches the terminal status; "stopped" is the neutral default until.
        self.status = InstanceStatus.RUNNING if result.get("running") else InstanceStatus.STOPPED
        self._pump_task = asyncio.create_task(self._pump())
        return result

    async def detach(self) -> None:
        """Drop the local pump + stream subscription *without* killing the agent.

        Clauster-shutdown counterpart to :meth:`stop`: the daemon owns the agent and
        survives our restart (it ``setsid``s away from our cgroup), so we leave it
        running for :meth:`reattach` next start rather than sending it a signal.
        Idempotent; leaves status untouched (the remote process is still alive).
        """
        await self._teardown()

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
        if self.status != "running":
            # Guard before consuming the parked request: a crashed/stopped session
            # would make the stdin write raise ClaustrumError (which the API doesn't
            # map), and the request would be consumed with no control_response sent.
            raise HostedSessionError(f"cannot respond on a {self.status} hosted session")
        if self._stopping:
            # stop() is mid-flight (status is still "running" during the SIGINT grace);
            # don't write a response into a session we're tearing down.
            raise HostedSessionError("hosted session is stopping")
        if request_id not in self._pending:
            raise HostedSessionError(f"no parked control request {request_id!r}")
        # Claim the request atomically (no await between the check and the pop, so
        # under asyncio's cooperative model this is exclusive): a concurrent
        # responder would now fail the existence check rather than racing us past
        # it and writing a second control_response for the same request_id.
        # Popping BEFORE the await below is also load-bearing for the failure path:
        # a concurrent _on_exit/_resolve_parked() then can't see this id in _pending,
        # so the except branch is the SOLE emitter of its control_resolved (no dupe).
        parked = self._pending.pop(request_id)
        if response.get("behavior") == "allow" and "updatedInput" not in response:
            # The CLI's can_use_tool response schema requires updatedInput on every
            # allow (a bare {"behavior": "allow"} fails its union validation and the
            # tool call errors out as if denied). Default it to the parked request's
            # original input — "allow unchanged".
            tool_input = parked.request.get("input")
            response = {
                **response,
                "updatedInput": tool_input if isinstance(tool_input, dict) else {},
            }
        try:
            await self._send_control_response(request_id, response)
        except ClaustrumError:
            # Restore the claim if the transport write fails: the request must stay
            # parked and retryable — but only while the session is still answerable. If a
            # concurrent exit/error drained _pending during the await (or stop() latched),
            # re-parking would resurrect a dead prompt that 409s forever with no
            # resolution; resolve it as interrupted instead so the live UI drops it.
            if self.status == "running" and not self._stopping:
                self._pending.setdefault(request_id, parked)
            else:
                self._emit(
                    {
                        "type": "control_resolved",
                        "request_id": request_id,
                        "behavior": "interrupted",
                    }
                )
            raise
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
        self._stopping = True  # reject any concurrent respond_control while we tear down
        try:
            await self._client.kill(self._process_id, signal="INT")
            if self._stream is not None:
                try:
                    await asyncio.wait_for(self._stream.exited.wait(), timeout=self._stop_grace)
                except TimeoutError:
                    await self._client.kill(self._process_id, signal="KILL")
                    try:
                        # Give the daemon's exit frame for the KILL a chance to land,
                        # so the terminal latch below has an exit code to work with.
                        await asyncio.wait_for(
                            self._stream.exited.wait(), timeout=self._stop_grace
                        )
                    except TimeoutError:
                        pass
        except ClaustrumError as exc:  # daemon loss during stop (CL-4b)
            self.status = InstanceStatus.ERROR
            self._resolve_parked()  # a dead session must not leave a parked request stranded
            self._emit({"type": "lost", "reason": f"stop failed: {exc}"})
        finally:
            await self._teardown()
            if (
                self.status == "running"
                and self._stream is not None
                and self._stream.exited.is_set()
            ):
                # The stream latches `exited` before the exit event reaches the pump's
                # queue, and the teardown above cancels the pump — live, stop() wins
                # that race and the row would stay "running" forever (blocking resume).
                # Latch the terminal status here from the stream's recorded exit code.
                self._on_exit(self._stream.exit_code)

    def subscribe(self, after_seq: int = 0) -> asyncio.Queue[Mapping[str, Any]]:
        """Register a browser watcher, replaying ring events past ``after_seq``.

        Returns a bounded queue pre-loaded with every retained event whose
        ``event_seq`` exceeds ``after_seq`` (a ``gap`` marker first if the ring has
        already evicted past the cursor), then fed live. :meth:`unsubscribe` when
        the consumer goes away.
        """
        queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize=self._queue_maxsize)
        sub = _Subscriber(queue, overflow_type="gap")
        if self._ring and self._ring[0]["event_seq"] > after_seq + 1:
            gap: GapRangeEvent = {
                "type": "gap",
                "from_seq": after_seq,
                "to_seq": self._ring[0]["event_seq"],
            }
            sub.offer(gap)
        for event in self._ring:
            if event["event_seq"] > after_seq:
                sub.offer(event)
        self._subscribers.append(sub)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Mapping[str, Any]]) -> None:
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
                seq = event.get("seq")
                if isinstance(seq, int) and seq > self.daemon_last_seq:
                    self.daemon_last_seq = seq  # advance the reattach replay cursor
                if etype == "line":
                    await self._on_line(event.get("stream"), event.get("line", ""))
                elif etype == "exit":
                    self._on_exit(event.get("exit_code"))
                    return
                elif etype == "overflow":
                    self._emit({"type": "gap", "dropped": event.get("dropped", 0)})
        except asyncio.CancelledError:
            raise
        except ClaustrumError as exc:  # daemon loss mid-pump (CL-4b)
            # A daemon-side failure surfaced through the reader; report it, don't swallow.
            self.status = InstanceStatus.ERROR
            self._resolve_parked()  # resolve any parked request so it isn't left stranded
            self._emit({"type": "lost", "reason": str(exc)})
        finally:
            # The pump owns the subscription it drains; drop it whenever the pump exits on its
            # own (natural exit, daemon loss, or cancel) so a subscriber is never left on the
            # stream until a later stop()/detach(). _teardown()'s unsubscribe is guarded on
            # `self._source`, so this stays idempotent when both run.
            if self._stream is not None and self._source is source:
                self._stream.unsubscribe(source)
                self._source = None

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
        """Auto-ack the MCP handshake/transport subtypes; park anything else (fail-closed).

        The auto-answered set is exactly :data:`_AUTO_ACK_SUBTYPES` (``initialize`` and
        ``mcp_message``); every other subtype — notably ``can_use_tool`` — is parked.
        """
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
        # Never auto-answer a request that could grant a capability.
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
        self._notify_permission_needed(parked.subtype)

    def _notify_permission_needed(self, subtype: str) -> None:
        """Best-effort #432 "come look" callback on a parked permission prompt.

        Runs inline on the stream pump, so it must never raise or block — it only
        schedules a fire-and-forget webhook. We send the session's process id and the
        request subtype (e.g. ``can_use_tool``), never the request body: the prompt's
        tool input can carry a path or argument and the operator already sees the
        redacted detail in the live view. Any callback error is logged and swallowed
        so a webhook problem can't stall or kill the session's stream.
        """
        if self._on_permission_needed is None:
            return
        try:
            self._on_permission_needed(self._process_id, subtype)
        except Exception as exc:  # noqa: BLE001 - a notify error must never reach the pump
            logger.warning("hosted: permission-needed callback failed: %s", exc)

    def _capture_session_uuid(self, frame: dict[str, Any]) -> None:
        """Latch the first ``session_id`` seen (drives ``--resume``); never overwrite it."""
        if self.claude_session_uuid is not None:
            return
        sid = frame.get("session_id")
        if isinstance(sid, str) and sid:
            self.claude_session_uuid = sid

    def _resolve_parked(self, behavior: str = "interrupted") -> None:
        """Clear every parked control request, fanning out a resolution for each.

        Called on any terminal/error transition: the agent is gone, so a parked
        permission request can never be answered (respond_control fails closed on a
        non-running session). Clearing it and telling watchers via control_resolved
        keeps the live UI from showing a dead Allow/Deny that 409s forever, and a
        reattach from replaying it through the ring as still-actionable.
        """
        parked_ids = list(self._pending)
        self._pending.clear()
        for request_id in parked_ids:
            self._emit(
                {"type": "control_resolved", "request_id": request_id, "behavior": behavior}
            )

    def _on_exit(self, exit_code: Any) -> None:
        """Latch the terminal status, resolve parked requests, emit the exit event."""
        self.exit_code = exit_code if isinstance(exit_code, int) else None
        self.status = InstanceStatus.STOPPED if self.exit_code == 0 else InstanceStatus.CRASHED
        self._resolve_parked()
        self._emit({"type": "exit", "exit_code": self.exit_code})

    async def _write_stdin(self, frame: StdinFrame) -> None:
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

    def _emit(self, payload: HostedEvent) -> None:
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

    def __init__(
        self,
        store: KeyedStore | None = None,
        *,
        on_permission_needed: Callable[[str, str], None] | None = None,
    ) -> None:
        """Create an empty manager (no sessions until :meth:`spawn`/:meth:`reattach_all`).

        ``store`` enables CL-6 restart resilience: spawn/stop persist the registry and
        :meth:`reattach_all` restores it on startup. ``None`` (the default, used in
        unit tests) keeps the manager purely in-memory — persistence calls no-op.

        ``on_permission_needed`` is the optional best-effort #432 hook, forwarded to
        every :class:`HostedSession` the manager builds (spawn, resume, reattach), so a
        parked tool-permission prompt can fire a "come look" webhook. The app wires it
        to the runner's emitter; it stays ``None`` in unit tests.
        """
        self._sessions: dict[str, HostedSession] = {}
        self._instances: dict[str, RemoteControlInstance] = {}
        self._store = store
        self._on_permission_needed = on_permission_needed
        self._last_saved: dict[str, dict] | None = None
        # Serialize persist: spawn/stop/aclose/poll can race, and an older snapshot
        # finishing last would overwrite a newer file (dropping a session / regressing
        # the cursor). The lock makes snapshot→save→_last_saved atomic per writer.
        self._persist_lock = asyncio.Lock()
        # Per-hosted_id lifecycle lock — mirrors SessionRunner._spawn_lock_for. The
        # lifecycle ops (stop/resume/forget/kill_orphan) each guard an entry-time
        # registry check, then await (kill/spawn/detach/to_thread), then mutate the
        # registry. Without serialization two concurrent resume(id) both pass the
        # running-check and both spawn → two live processes for one conversation
        # (the #715-shaped race, structurally prevented here rather than relied-upon-
        # .get()/.pop()-staying-KeyError-safe). Distinct from _persist_lock: never
        # held together, so no lock-ordering inversion.
        self._id_locks: dict[str, asyncio.Lock] = {}

    def list_instances(self) -> list[RemoteControlInstance]:
        """Snapshot of every hosted instance, each synced to its live session state."""
        return [self._synced(inst) for inst in self._instances.values()]

    def get_instance(self, hosted_id: str) -> RemoteControlInstance | None:
        """Return the hosted instance for ``hosted_id`` (status-synced), or None.

        ``hosted_id`` may be the registry key (``claustrum_process_id``) or the row's
        ``instance_id`` — resolved via :meth:`_key_for` (#834).
        """
        key = self._key_for(hosted_id)
        inst = self._instances.get(key) if key is not None else None
        return self._synced(inst) if inst is not None else None

    def session(self, hosted_id: str) -> HostedSession | None:
        """Return the live :class:`HostedSession` for ``hosted_id``, or None.

        Accepts the registry key or the row's ``instance_id`` (see :meth:`_key_for`, #834).
        """
        key = self._key_for(hosted_id)
        return self._sessions.get(key) if key is not None else None

    async def spawn(
        self,
        client: ClaustrumClient,
        *,
        project: str,
        label: str,
        cwd: str,
        claude_binary: str,
        permission_mode: PermissionMode,
        resume_uuid: str | None = None,
    ) -> RemoteControlInstance:
        """Start a hosted session and register its dashboard instance.

        ``client`` is the connected daemon client (the caller sources it from
        ``app.state.claustrum_daemon``). Propagates :class:`ClaustrumError` if the
        spawn RPC fails — the caller maps it to an HTTP error and nothing is
        registered.
        """
        instance = await self._spawn_session(
            client,
            project=project,
            label=label,
            cwd=cwd,
            claude_binary=claude_binary,
            permission_mode=permission_mode,
            resume_uuid=resume_uuid,
        )
        await self._persist()  # record the new session so a restart can reattach it
        return self._synced(instance)

    async def _spawn_session(
        self,
        client: ClaustrumClient,
        *,
        project: str,
        label: str,
        cwd: str,
        claude_binary: str,
        permission_mode: PermissionMode,
        resume_uuid: str | None = None,
    ) -> RemoteControlInstance:
        """Spawn + register a hosted session WITHOUT persisting (caller persists).

        Split out so :meth:`resume` can spawn the fresh process first, then retire
        the dead row and persist *once* — the first on-disk write after a resume
        contains only the resumed row, never both (no duplicate-card window on a
        crash mid-resume). A spawn failure here registers nothing.
        """
        process_id = uuid.uuid4().hex
        session = HostedSession(
            client,
            process_id,
            claude_binary,
            on_permission_needed=self._on_permission_needed,
        )
        await session.start(
            cwd=cwd, permission_mode=permission_mode, resume_uuid=resume_uuid, want_pid=True
        )
        # Measure our OWN psutil create_time of the (CT-1-reported) pid — that's what
        # CL-8 orphan validation compares against, NOT the daemon's startTime token
        # (decision (b)). None when there's no pid (pre-CT-1 daemon) or it's unreadable.
        proc_start = procutil.proc_create_time(session.agent_pid) if session.agent_pid else None
        instance = RemoteControlInstance(
            project=project,
            label=label,
            channel="hosted",
            permission_mode=permission_mode,
            claustrum_process_id=process_id,
            agent_pid=session.agent_pid,
            agent_proc_start=proc_start,
            status=InstanceStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        self._sessions[process_id] = session
        self._instances[process_id] = instance
        return instance

    async def send(self, hosted_id: str, text: str) -> None:
        """Route a user turn to a hosted session (404/409 mapping is the caller's)."""
        await self._require(hosted_id).send_message(text)

    async def respond(self, hosted_id: str, request_id: str, response: dict[str, Any]) -> None:
        """Answer a parked control request on a hosted session (caller maps 404/409)."""
        await self._require(hosted_id).respond_control(request_id, response)

    async def stop(self, hosted_id: str) -> RemoteControlInstance:
        """Stop a hosted session and return its final (status-synced) instance.

        Serialized per id (see :meth:`_lock_for`): a concurrent forget/resume on the
        same id waits rather than racing the stop's grace-window awaits.
        """
        key = self._key_for(hosted_id)
        if key is None:
            raise HostedSessionError(f"no such hosted session: {hosted_id}")
        async with self._lock_for(key):
            await self._require(key).stop()
            # The per-id lock now bars a concurrent forget()/resume() from popping the
            # row mid-grace; the re-fetch + absent→404 mapping stays as defense-in-depth
            # (and still covers the no-session-but-row corner) rather than a 500.
            instance = self._instances.get(key)
            if instance is None:
                raise HostedSessionError(f"no such hosted session: {hosted_id}")
            # Mark the intent so a restart shows it as stopped, not "lost"/crashed.
            instance.intentional_stop = True
            await self._persist()
            return self._synced(instance)

    async def resume(
        self,
        client: ClaustrumClient,
        hosted_id: str,
        *,
        cwd: str,
        claude_binary: str,
    ) -> RemoteControlInstance:
        """Respawn a lost/ended hosted session from its captured ``claude_session_uuid``.

        The original daemon process is gone (stopped/crashed/error/daemon-restart), so
        this starts a *fresh* process with ``--resume <uuid>`` to reload the
        conversation — keyed by a new ``claustrum_process_id``; the dead row is retired
        once the resumed one is live. Raises :class:`HostedSessionError` if the session
        is unknown, still running, or has no captured uuid to resume from. A
        :class:`ClaustrumError` from the spawn propagates (the caller maps it).

        Serialized per id (see :meth:`_lock_for`): two concurrent resume(id) can't
        both pass the running-check and both spawn — the second blocks at the lock,
        then sees the row already retired and 404s, so exactly one process results.
        """
        key = self._key_for(hosted_id)
        if key is None:
            raise HostedSessionError(f"no such hosted session: {hosted_id}")
        async with self._lock_for(key):
            return await self._resume_locked(client, key, cwd=cwd, claude_binary=claude_binary)

    async def _resume_locked(
        self,
        client: ClaustrumClient,
        hosted_id: str,
        *,
        cwd: str,
        claude_binary: str,
    ) -> RemoteControlInstance:
        """Body of :meth:`resume`, always run under the per-id lifecycle lock.

        ``hosted_id`` is the resolved registry key (``claustrum_process_id``) — the
        caller applies :meth:`_key_for` before taking the lock (#834).
        """
        old = self._instances.get(hosted_id)
        if old is None:
            raise HostedSessionError(f"no such hosted session: {hosted_id}")
        # Sync first so the captured uuid lands on the row regardless of whether a
        # poll/get_instance happened since the init frame (don't depend on the caller).
        old = self._synced(old)
        old_session = self._sessions.get(hosted_id)
        if old_session is not None and old_session.status in ("running", "starting"):
            raise HostedSessionError(f"hosted session {hosted_id} is still running")
        resume_uuid = old.claude_session_uuid
        if not resume_uuid:
            raise HostedSessionError("no captured session uuid to resume from")
        # Spawn the fresh process FIRST, without persisting — a spawn failure then
        # leaves the dead row untouched and retryable.
        instance = await self._spawn_session(
            client,
            project=old.project,
            label=old.label,
            cwd=cwd,
            claude_binary=claude_binary,
            permission_mode=old.permission_mode,
            resume_uuid=resume_uuid,
        )
        # Carry the uuid forward so the resumed row is itself re-resumable before its
        # first frame re-captures it.
        instance.claude_session_uuid = resume_uuid
        # Atomically retire the dead row (detach a same-runtime dead session), THEN
        # persist once — so the first on-disk write holds only the resumed row and a
        # crash mid-resume can't restore both as duplicate cards.
        if old_session is not None:
            await old_session.detach()
            self._sessions.pop(hosted_id, None)
        elif old.is_orphan and old.agent_pid is not None:
            # Orphan survivor still running on this conversation — kill it (gated on a
            # pid+create_time match) so the resumed agent doesn't share its session.
            await asyncio.to_thread(procutil.kill_if_match, old.agent_pid, old.agent_proc_start)
        self._instances.pop(hosted_id, None)
        # Prune this id's lock too: resume retires the old hosted_id permanently (the
        # resumed agent runs under a fresh id), so the old lock would otherwise strand
        # in _id_locks. Safe under the held lock, same as forget().
        self._id_locks.pop(hosted_id, None)
        await self._persist()
        return self._synced(instance)

    async def kill_orphan(self, hosted_id: str) -> RemoteControlInstance:
        """Terminate a survived-but-orphaned hosted agent and mark its row stopped (CL-8).

        For an orphan (a live agent the restarted daemon no longer manages): hard-kill
        the survivor pid+tree — gated on a pid + create_time + hosted-cmdline match so a
        reused/unrelated PID is never touched — then mark the row stopped. Raises
        :class:`HostedSessionError` for an unknown id.

        Serialized per id (see :meth:`_lock_for`) so a concurrent resume can't spawn
        a fresh agent off this row while we're hard-killing the survivor.
        """
        key = self._key_for(hosted_id)
        if key is None:
            raise HostedSessionError(f"no such hosted session: {hosted_id}")
        async with self._lock_for(key):
            inst = self._instances.get(key)
            if inst is None:
                raise HostedSessionError(f"no such hosted session: {hosted_id}")
            if inst.agent_pid is not None:
                await asyncio.to_thread(
                    procutil.kill_if_match, inst.agent_pid, inst.agent_proc_start
                )
            inst.is_orphan = False
            inst.intentional_stop = True
            inst.status = InstanceStatus.STOPPED
            # Clear any orphan/loss recovery prompt — the row is now a clean stop, not a
            # "Resume or Kill" survivor, so a stale detail would mislead the UI.
            inst.error_detail = None
            await self._persist()
            return self._synced(inst)

    async def forget(self, hosted_id: str) -> None:
        """Drop a stopped hosted session's record so it leaves the Recent list (fail closed).

        Lets the operator clear an ended (stopped/crashed/error) hosted session out of
        the Recent/resumable list to start fresh. ``detach``es any lingering session
        handle (drops its client-side stream subscription — its pump already completed
        when the session reached a terminal state), removes the registry entry, then
        re-persists — ``_persist`` writes the full registry, so the dropped row is gone
        from ``hosted_state.json``.

        Fail closed: a running/starting session, or a live orphan survivor, is refused
        with :class:`HostedSessionError` — Stop it (or Kill the orphan) first; forget
        never terminates a process. Raises :class:`HostedSessionError` for an unknown id.

        Serialized per id (see :meth:`_lock_for`): the still-running guard and the
        registry-pop are held together, so a concurrent stop→running-transition or a
        resume can't slip between the check and the drop.
        """
        key = self._key_for(hosted_id)
        if key is None:
            raise HostedSessionError(f"no such hosted session: {hosted_id}")
        async with self._lock_for(key):
            inst = self._instances.get(key)
            if inst is None:
                raise HostedSessionError(f"no such hosted session: {hosted_id}")
            inst = self._synced(inst)
            session = self._sessions.get(key)
            if (
                session is not None and session.status in ("running", "starting")
            ) or inst.status in (
                InstanceStatus.RUNNING,
                InstanceStatus.STARTING,
            ):
                raise HostedSessionError(
                    f"hosted session {hosted_id} is still running — Stop it first"
                )
            if inst.is_orphan:
                raise HostedSessionError(
                    f"hosted session {hosted_id} is a live orphan — Kill it first"
                )
            # Detach the terminal session like resume() retires a dead row — drops the
            # client-side ProcessStream subscriber deterministically instead of leaking it.
            if session is not None:
                await session.detach()
            self._sessions.pop(key, None)
            self._instances.pop(key, None)
            # Prune the per-id lock so _id_locks doesn't grow unbounded — it's keyed by
            # the fresh per-session UUID, not by a finite stable set like the runner's
            # spawn locks. Safe under the held lock: a coroutine already waiting on this
            # lock object still acquires it, finds no instance, and raises; a later
            # arrival mints a fresh lock and hits the same not-found result.
            self._id_locks.pop(key, None)
            await self._persist()

    @staticmethod
    def _is_orphan(instance: RemoteControlInstance) -> bool:
        """Whether a not-found instance is a recoverable (killable) survivor.

        Uses the same fail-closed predicate as the guarded kill, so a row is only
        classified as an orphan when we actually have killable ``(pid, create_time)``
        evidence. A survivor we can't safely kill (e.g. a pre-CL-8 row with no recorded
        ``agent_proc_start``) is treated as lost, not as a recoverable orphan — otherwise
        ``kill_orphan``/resume would transition the row to a clean stop while the process
        kept running.
        """
        return instance.agent_pid is not None and procutil.is_killable_hosted(
            instance.agent_pid, instance.agent_proc_start
        )

    async def aclose(self) -> None:
        """Detach every live hosted session at app shutdown — leave them running.

        Clauster restart resilience (CL-6): the daemon owns the agents and survives
        our restart, so we drop our local pump/subscription (``detach``) rather than
        killing them (``stop``). :meth:`reattach_all` restores them next start. A
        final persist flushes the freshest ``daemon_last_seq`` cursor for each.
        """
        for session in list(self._sessions.values()):
            await session.detach()
        await self._persist()

    async def reattach_all(self, client: ClaustrumClient) -> list[RemoteControlInstance]:
        """Restore persisted hosted sessions on startup, reattaching the live ones.

        For each persisted record: an intentionally-stopped one is rebuilt as a
        stopped row (no reattach); otherwise we ``process.reattach`` from its saved
        ``daemon_last_seq``. Found+running → a live, pumping session; found+exited →
        finalized via the replayed exit; not-found → a CRASHED "session lost" row.
        Tolerates a daemon error per session (records it, keeps going — one bad
        reattach never blocks the rest or startup). Returns the restored instances.
        """
        records = self._store.load() if self._store is not None else {}
        for process_id, fields in records.items():
            instance = self._instance_from_record(process_id, fields)
            self._instances[process_id] = instance
            if fields.get("intentional_stop"):
                instance.status = InstanceStatus.STOPPED
                continue
            # reattach() never builds argv (it binds by process id), so the binary is
            # unused here — a respawn path (CL-7) re-resolves it from config.
            session = HostedSession(
                client,
                process_id,
                "",
                on_permission_needed=self._on_permission_needed,
            )
            session.claude_session_uuid = fields.get("claude_session_uuid")
            try:
                result = await session.reattach(int(fields.get("daemon_last_seq") or 0))
            except ClaustrumError as exc:
                instance.status = InstanceStatus.ERROR
                instance.error_detail = f"reattach failed: {exc}"
                logger.warning("hosted: reattach of %s failed: %s", process_id, exc)
                continue
            if not result.get("found"):
                # The new daemon doesn't know this process. If -keep-children left the
                # agent running (CL-8), its (pid, create_time) still matches a live
                # process → it's an orphan we can recover; otherwise it's truly lost.
                instance.status = InstanceStatus.CRASHED
                if self._is_orphan(instance):
                    instance.is_orphan = True
                    instance.error_detail = (
                        "survived a daemon restart — Resume to recover, or Kill"
                    )
                else:
                    instance.error_detail = "daemon restarted; session lost"
                continue
            self._sessions[process_id] = session
        await self._persist()
        return [self._synced(inst) for inst in self._instances.values()]

    async def persist(self) -> None:
        """Public debounced persist — the dashboard poll calls this to refresh cursors."""
        await self._persist()

    def _key_for(self, hosted_id: str) -> str | None:
        """Resolve a caller-supplied id to the registry key, or None if unknown (#834).

        The hosted registry keys ``_instances``/``_sessions`` by ``claustrum_process_id``
        (client-chosen at spawn), but API clients naturally reach for the row's
        ``instance_id`` (a dashed UUID) — the field the dashboard and standard bridges
        expose. Accept either: a direct key hit wins (the common path), otherwise scan
        for the row whose ``instance_id`` matches. The two id formats never collide
        (32-hex vs. dashed UUID), and the registry is small (a handful of sessions), so
        the linear fallback is cheap. Returns the ``claustrum_process_id`` to key by.
        """
        if hosted_id in self._instances:
            return hosted_id
        for pid, inst in self._instances.items():
            if inst.instance_id == hosted_id:
                return pid
        return None

    def _require(self, hosted_id: str) -> HostedSession:
        """Return the live session for ``hosted_id`` or raise ``HostedSessionError``.

        Resolves ``hosted_id`` (registry key or the row's ``instance_id``) via
        :meth:`_key_for` (#834).
        """
        key = self._key_for(hosted_id)
        session = self._sessions.get(key) if key is not None else None
        if session is None:
            raise HostedSessionError(f"no such hosted session: {hosted_id}")
        return session

    def _lock_for(self, hosted_id: str) -> asyncio.Lock:
        """Return the per-id lifecycle lock, creating it on first use.

        Synchronous (no ``await``) so the get-or-create can't itself race on the
        loop — two coroutines for the same id can never each mint a distinct lock.
        Mirrors :meth:`SessionRunner._spawn_lock_for`.
        """
        lock = self._id_locks.get(hosted_id)
        if lock is None:
            lock = self._id_locks[hosted_id] = asyncio.Lock()
        return lock

    def _synced(self, instance: RemoteControlInstance) -> RemoteControlInstance:
        """Reflect the live session's status + captured uuid + reattach cursor onto the row."""
        session = self._sessions.get(instance.claustrum_process_id or "")
        if session is not None:
            instance.status = session.status
            if session.claude_session_uuid:
                instance.claude_session_uuid = session.claude_session_uuid
            # The reattach cursor is the *daemon* seq, not the clauster ring seq.
            instance.daemon_last_seq = max(instance.daemon_last_seq, session.daemon_last_seq)
        return instance

    async def _persist(self) -> None:
        """Write the registry's persisted subset, but only when it actually changed.

        Best-effort: the store is non-authoritative, so a write failure (disk full,
        revoked perms) degrades to a stale reattach cursor — never a failed spawn/stop
        or a 500 on the dashboard poll. ``_last_saved`` is left unchanged on failure so
        the next persist retries.
        """
        if self._store is None:
            return
        async with self._persist_lock:
            subset = {pid: self._record(self._synced(i)) for pid, i in self._instances.items()}
            if subset == self._last_saved:
                return
            try:
                await asyncio.to_thread(self._store.save, subset)
            except OSError as exc:
                logger.warning("hosted: could not persist session state: %s", exc)
                return
            self._last_saved = subset

    @staticmethod
    def _record(instance: RemoteControlInstance) -> dict[str, Any]:
        """Project a hosted instance to its JSON-safe persisted record (Path/datetime → str)."""
        return {
            "project": instance.project,
            "label": instance.label,
            "permission_mode": instance.permission_mode,
            "claude_session_uuid": instance.claude_session_uuid,
            "daemon_last_seq": instance.daemon_last_seq,
            "hosted_log_path": str(instance.hosted_log_path) if instance.hosted_log_path else None,
            "agent_pid": instance.agent_pid,
            "agent_proc_start": instance.agent_proc_start,
            "started_at": instance.started_at.isoformat() if instance.started_at else None,
            "intentional_stop": instance.intentional_stop,
            "instance_id": instance.instance_id,
        }

    @staticmethod
    def _instance_from_record(process_id: str, fields: dict) -> RemoteControlInstance:
        """Rebuild a hosted instance row from a persisted record (inverse of _record)."""
        started_at = fields.get("started_at")
        parsed_start: datetime | None = None
        if isinstance(started_at, str):
            try:
                parsed_start = datetime.fromisoformat(started_at)
            except ValueError:
                parsed_start = None
        log_path = fields.get("hosted_log_path")
        instance = RemoteControlInstance(
            project=fields.get("project", ""),
            label=fields.get("label", f"hosted:{process_id[:8]}"),
            channel="hosted",
            permission_mode=fields.get("permission_mode", "default"),
            claustrum_process_id=process_id,
            claude_session_uuid=fields.get("claude_session_uuid"),
            daemon_last_seq=int(fields.get("daemon_last_seq") or 0),
            hosted_log_path=Path(log_path) if isinstance(log_path, str) and log_path else None,
            agent_pid=fields.get("agent_pid"),
            agent_proc_start=fields.get("agent_proc_start"),
            started_at=parsed_start,
            intentional_stop=bool(fields.get("intentional_stop", False)),
            status=InstanceStatus.STARTING,
        )
        # Restore the per-runtime instance_id (#834/#840) so a client that cached
        # it before the restart still resolves via HostedManager._key_for instead
        # of hitting the freshly-minted default_factory id (#841). Constructed
        # then set rather than passed to the constructor — a bare **fields unpack
        # would let heterogeneous/unknown persisted keys reach the model directly.
        if fields.get("instance_id"):
            instance.instance_id = fields["instance_id"]
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
