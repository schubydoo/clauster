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
import re
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, TypeGuard, cast, get_args

from pydantic import ValidationError

from . import procutil
from .claustrum_client import ClaustrumClient, ClaustrumError, ProcessStream, _Subscriber
from .config import INHERIT_PERMISSION_MODE, PermissionMode
from .hosted_events import GapRangeEvent, HostedEvent, StdinFrame
from .models import InstanceStatus, RemoteControlInstance, new_instance_id
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

# Newest transcript turns a reattach restores into the ring (#1045). A conversation's
# on-disk JSONL grows without bound and the ring holds _DEFAULT_RING_SIZE events for
# everything, so the restored history is capped rather than replayed whole — enough to
# recognise the conversation, never an unbounded read into the ring.
_REHYDRATE_MAX_TURNS = 200

# Transcript roles a restored turn may be rendered as. The role becomes the synthetic
# frame's `type`, which the browser dispatches on, so it is whitelisted rather than
# forwarded — an unexpected role must not be able to impersonate a control frame.
_REHYDRATE_ROLES: frozenset[str] = frozenset({"user", "assistant", "system"})

# The shape a `claude_session_uuid` must have before it may become the `--resume` value
# token. `claude`'s `--resume [sessionId]` takes an *optional* value, so a flag-shaped
# token in that slot is read as a fresh FLAG rather than consumed as data — a persisted
# string would then contribute an ARGUMENT to the spawn argv, which invariant 2 forbids
# (#1392). The leading-alnum class is what closes that; the body class also rules out
# whitespace, path separators and control characters, and the 64-char cap matches the
# `String(64)` column the value round-trips through. `\A`/`\Z`, never `^`/`$`: `$` also
# matches *before* a trailing newline, so `$` would admit "uuid\n".
#
# Deliberately a SHAPE, not the 8-4-4-4-12 format `runner._SESSION_UUID_RE` and
# `supervisor.valid_session_id` demand — hence the different name. Those two guard an
# *operator-supplied* id that must name a transcript file on disk, so they can insist on
# claude's current filename format. This one guards an id claude *itself* minted and
# handed us in an init frame; re-specifying the format here would silently cost a whole
# session its resume the day claude changes it (a ULID, say). Keeping the token out of
# the flag namespace is the whole job. Not format-*independence*, though: the length cap
# is a narrower bet in the same direction, so an id longer than the column could hold
# would be dropped on reload too. Widen the cap with the column if that day comes.
_SESSION_UUID_SHAPE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class HostedSessionError(ClaustrumError):
    """Raised when a hosted-session operation is invalid for the current state."""


def _refused_uuid_shape(value: Any) -> str:
    """Describe a refused ``claude_session_uuid`` WITHOUT reproducing any of its bytes.

    Named, not typed, for the two string cases: the empty string IS a ``str``, so a bare
    type name would read as a type complaint about one of the shapes this refusal exists
    for. ``_restore_instance_id`` draws the same distinction for a falsy instance_id.

    The bytes never appear, in any form (#1392). Two sinks, both outside redaction's normal
    reach: a ``logger.warning`` and the message of the :class:`HostedSessionError` that
    :func:`build_hosted_argv` raises, which the resume route renders as a 409 ``detail`` in
    the browser. A session id is exactly the class :mod:`~clauster.redact` masks, and the
    day this refusal fires on a *real* id is the day claude changed its format — so passing
    the value through ``sanitize_line`` would not redact it, only strip its ANSI. Invariant
    4 says nothing sensitive reaches a log or the browser unmasked, and a shape name is the
    only description that satisfies it for every input at once. The length is safe to state
    and is what tells an operator whether the record holds a truncated id or something else
    entirely.

    The operator does not need the token to act. The 409 is raised about one known
    instance, and the log line names the field and its store the same way its three
    siblings (:func:`_as_pid`, :func:`_as_proc_start`, :func:`_as_permission_mode`) name
    theirs — none of them reproduces a value either. The record is a JSON object with one
    ``claude_session_uuid``; whoever opens it reads the token there, where it was already
    readable, instead of from a log a wider audience can see.
    """
    if not isinstance(value, str):
        return type(value).__name__
    return "empty string" if not value else f"malformed, {len(value)} chars"


def _is_session_uuid(value: Any) -> TypeGuard[str]:
    """Report whether ``value`` is a str shaped like a session id.

    The shape, and why it is this one, is :data:`_SESSION_UUID_SHAPE_RE`.
    """
    return isinstance(value, str) and _SESSION_UUID_SHAPE_RE.match(value) is not None


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

    ``resume_uuid`` is shape-checked *here* as well as where it is read off a persisted
    record (:func:`_as_session_uuid`), because this is the last seam before the value
    becomes an argv token: a future path that reaches the spawn without going through the
    mapper still cannot put a flag-shaped string next to ``--resume`` (#1392). A refusal
    **raises** :class:`HostedSessionError` rather than dropping the flag — dropping it
    would silently start a *fresh* conversation under a name the operator asked to
    resume, and the resume route already maps this error to 409. The refusal is described
    by :func:`_refused_uuid_shape`, the same helper the persisted-read refusal logs with,
    so the two say the same thing about the same value — and, critically, neither of them
    reproduces its bytes: this message is rendered in the browser as that 409's ``detail``,
    and a session id is exactly what invariant 4 keeps out of a log or a page. It also must
    not assume a ``str``: a caller bypassing the mapper is exactly what this seam exists
    for, and the helper answers a non-string with its type name.

    ⚠️ The check runs BEFORE :meth:`HostedSession.start` subscribes to the process
    stream, which is why a refusal leaks nothing. Subscribing first would leak an
    undrained subscriber, and ``start``'s ``except BaseException`` cleanup would not
    cover it — the raise happens outside that ``try``.

    ``permission_mode`` of :data:`~clauster.config.INHERIT_PERMISSION_MODE` emits **no**
    ``--permission-mode`` flag (#1231), so the session starts in its own default mode
    instead of one forced into its spawn-time system prompt. The sentinel is a Clauster
    value only and never reaches the argv.
    """
    argv = [claude_binary, *_STREAM_JSON_ARGS]
    if permission_mode != INHERIT_PERMISSION_MODE:
        argv += ["--permission-mode", permission_mode]
    if resume_uuid is not None:
        if not _is_session_uuid(resume_uuid):
            raise HostedSessionError(
                f"refusing an unusable resume session id: {_refused_uuid_shape(resume_uuid)}"
            )
        argv += ["--resume", resume_uuid]
    return argv


def _as_seq(value: Any) -> int | None:
    """Return ``value`` as a daemon frame seq, or ``None`` when it isn't one.

    ``bool`` is rejected explicitly: it subclasses ``int``, so a daemon answering
    ``true`` would otherwise be read as seq 1 and silently bound a suppression or
    gap window. Seqs arrive as untrusted JSON, so every read goes through here.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


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
        # Daemon seq through which replayed DATA frames are already represented by
        # transcript history this session rehydrated on reattach (#1045). Frames at or
        # below it are dropped so the seam can't double-render a turn; 0 suppresses
        # nothing, which is the state of every session that did not rehydrate.
        self._rehydrated_through = 0
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
            self._drop_subscription()
            raise
        pid = result.get("pid")
        self.agent_pid = pid if isinstance(pid, int) else None
        start_time = result.get("startTime")
        self.agent_proc_start = float(start_time) if isinstance(start_time, (int, float)) else None
        self.status = InstanceStatus.RUNNING
        self._pump_task = asyncio.create_task(self._pump())

    async def reattach(
        self,
        from_seq: int = 0,
        *,
        history_loader: Callable[[], Awaitable[list[dict[str, Any]] | None]] | None = None,
    ) -> dict[str, Any]:
        """Reattach to an already-running daemon process, replaying frames past ``from_seq``.

        Used on clauster restart (CL-6): the agent kept running on the daemon while
        we were down. Subscribes before the ``process.reattach`` RPC so no replayed
        frame is missed, then — if the process is *found* — pumps the replay + live
        tail. A not-found process means the session was lost while we were down, so
        status latches to ``crashed`` and nothing is pumped. Returns the daemon's
        ``{found, running, firstSeq, lastSeq}`` result. ``from_seq`` is the persisted
        :attr:`daemon_last_seq`; a stale/zero value only costs replay overlap (the
        client de-dupes by seq), never a double-emit.

        The opposite direction — the daemon's capped replay buffer having *evicted*
        frames past ``from_seq`` — is reported rather than absorbed: see
        :meth:`_note_replay_gap` (#1175).
        ``history`` is the session's prior conversation read from claude's on-disk
        transcript (already redacted); when supplied it is restored into the ring
        before the pump starts, so a reattached session's view shows the conversation
        ``history_loader`` reads the session's prior conversation from claude's
        on-disk transcript, so a reattached session's view shows the conversation it
        had before the restart instead of an empty pane (#1045). It is a *callable*,
        not a pre-read list, because **it must run after the reattach RPC** — see
        :meth:`_rehydrate` for why reading first loses data.
        """
        if self._pump_task is not None:
            raise HostedSessionError("hosted session already started")
        self._stream = self._client.stream(self._process_id)
        self._source = self._stream.subscribe()
        try:
            result = await self._client.reattach(self._process_id, from_seq)
            if not result.get("found"):
                self._drop_subscription()  # session gone while we were down
                self.status = InstanceStatus.CRASHED
                return result
            # RING ORDER — history -> gap marker -> replayed frames, all before the
            # pump starts. Restored history is the OLDEST content; the gap sits
            # between it and the surviving tail. The marker is deliberately NOT
            # suppressed for a session that rehydrated: the transcript restores
            # conversation *turns* only, so stderr and control-plane frames inside
            # the evicted range are still gone — dropping it would claim a
            # completeness we don't have (the restart docs state this composition).
            backlog: list[Mapping[str, Any]] = []
            if history_loader is not None:
                # The subscriber queue is BOUNDED (ring + 1), and the transcript read
                # takes real time on a big .jsonl — a large replay plus live tail can
                # outrun the queue in that window and drop frames before the pump
                # exists (review catch: a dropped control_request parks claude on a
                # prompt the dashboard never shows). Spill the queue into an
                # unbounded local backlog while the read runs; the pump routes it
                # first, so nothing is lost and ring order is unchanged.
                source = self._source

                async def _spill() -> None:
                    while True:
                        backlog.append(await source.get())

                spill = asyncio.create_task(_spill())
                try:
                    history = await history_loader()
                finally:
                    spill.cancel()
                    try:
                        await spill
                    except asyncio.CancelledError:
                        pass
                self._rehydrate(history, result.get("lastSeq"))
            gap_first = self._note_replay_gap(result.get("firstSeq"), from_seq)
            # SYNCHRONOUS with the report (#1175 review catch): the evicted range is
            # unrecoverable by definition, so the cursor jumps past the hole here
            # rather than waiting for the pump's first drained frame — a crash in
            # that window re-reported the same gap and re-replayed the survivors.
            self.daemon_last_seq = max(
                self.daemon_last_seq,
                from_seq,
                (gap_first - 1) if gap_first is not None else 0,
            )
        except BaseException:
            # RPC failed, or the history read raised/was cancelled — drop the
            # subscription we made above, mirroring start(); reattach_all() discards
            # the session on error, so nothing else would.
            self._drop_subscription()
            raise
        # If not running, the exit frame (seq > from_seq) replays through the pump,
        # which latches the terminal status; "stopped" is the neutral default until.
        self.status = InstanceStatus.RUNNING if result.get("running") else InstanceStatus.STOPPED
        self._pump_task = asyncio.create_task(self._pump(backlog))
        return result

    def _drop_subscription(self) -> None:
        """Release the stream subscription for a start/reattach that will never pump."""
        if self._stream is not None and self._source is not None:
            self._stream.unsubscribe(self._source)
        self._stream, self._source = None, None

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

    def _note_replay_gap(self, first_seq: Any, from_seq: int) -> int | None:
        """Report a daemon replay range that was evicted before we could read it (#1175).

        The daemon caps its per-process replay buffer (the reference ``claude-ssh``
        at 16 MiB), so a chatty agent can outrun it while clauster is down. Its
        reattach result then carries a ``firstSeq`` *above* our cursor, meaning
        ``from_seq + 1 .. firstSeq - 1`` no longer exist anywhere — not on the
        daemon, and never on us.

        Those frames are unrecoverable, so the hole is reported rather than absorbed:
        it is logged with its exact range and emitted into the ring as a ``gap``
        marker, which the caller orders ahead of the replayed frames so a watcher
        sees the break in position rather than a seamless transcript.

        **The cursor advance is synchronous with the report** (review catch): the
        caller uses the returned ``firstSeq`` to jump :attr:`daemon_last_seq` past
        the hole in the same block that emits the marker. Leaving the advance to the
        pump's first drained frame meant a crash between the report and that frame
        re-reported the same gap and re-replayed the survivors on the next restart.
        The evicted range is unrecoverable *by definition*, so jumping to
        ``firstSeq - 1`` loses nothing — the replay then delivers from ``firstSeq``
        exactly once. Refusing to advance at all would instead re-replay the whole
        surviving buffer every restart and duplicate the transcript.

        **Why an empty buffer is genuinely not a gap.** Verified against the
        claustrum daemon's source (``process.go`` reattach, current main; the v1.10
        cut's handler is byte-identical, its change surface being the
        ``server.version`` removal): ``firstSeq``/``lastSeq`` are only assigned when
        the buffer is non-empty, so an empty buffer puts ``firstSeq: 0`` on the wire
        (the fields are not ``omitempty``). That reads as "no gap" here — correctly,
        because eviction happens only on *append* and always keeps the frame just
        added. A process that emitted anything since our detach therefore has a
        non-empty buffer and a truthful ``firstSeq``; an empty buffer means nothing
        was emitted, so there is nothing to have lost. The fake daemon models this
        (``FakeClaustrum._m_process_reattach``) — don't "fix" either side to report a
        gap for an empty buffer.

        ``first_seq`` is untrusted daemon-supplied JSON, so it goes through
        :func:`_as_seq`; a non-int (or an in-range value) means no gap to report.
        """
        first = _as_seq(first_seq)
        if first is None or first <= from_seq + 1:
            return None
        logger.warning(
            "hosted: daemon replay buffer evicted frames %d-%d for process %s; "
            "that output is lost and cannot be replayed",
            from_seq + 1,
            first - 1,
            self._process_id,
        )
        gap: GapRangeEvent = {"type": "gap", "from_seq": from_seq, "to_seq": first}
        self._emit(gap)
        return first

    def _rehydrate(self, history: list[dict[str, Any]] | None, last_seq: Any) -> None:
        """Restore a reattached session's prior conversation into the ring (#1045).

        The hosted transcript lives only in this process's ring, so a clauster
        restart used to leave a reattached Direct session's view empty even though
        the agent — and claude's own ``.jsonl`` — carried the whole conversation.
        ``history`` is that transcript, read **read-only** by the caller (invariant 5:
        claude owns its transcripts) and already redacted by ``usage``; each turn is
        re-emitted here in the same ``{"type": role, "message": {...}}`` shape the live
        stream uses, so the browser renders restored and live turns identically. It is
        passed back through :func:`_redact_obj` regardless — invariant 4 is enforced at
        this module's own boundary, not trusted from the reader.

        **The seam.** The daemon's replay buffer also still holds frames for turns that
        completed while we were down, so restoring the transcript *and* replaying them
        would double-render exactly those turns. The transcript is the authoritative
        record of the conversation up to ``last_seq``, so once it has been restored the
        replay's **data** frames through that cursor are suppressed
        (:meth:`_suppressed_by_history`) — every conversation turn is rendered exactly
        once, from one source.

        **Why the caller reads the transcript AFTER the reattach RPC, never before.**
        ``last_seq`` is the daemon's ``lastSeq`` as of the RPC. Reading the file first
        and reattaching second would let the agent emit frames *during* the read (a
        multi-MB file, read sequentially per session) that land at or below the
        later-captured cursor — suppressed as "already in the transcript" when the
        transcript was snapshotted before they existed. They would be shown nowhere,
        permanently. Reading afterwards inverts the error: the transcript is a superset
        of the replay window, so the failure mode becomes a duplicated turn, which is
        the safe direction.

        One residual race remains and is accepted: claude writes the stream frame and
        flushes the transcript line independently, so a turn that reached the daemon
        just before ``last_seq`` may not be in the file we then read. Its frame is
        suppressed while the transcript lacks it. The window is one turn wide and only
        opens at the instant of reattach; closing it would need a per-turn identity the
        transcript reader does not expose.

        Suppression is skipped entirely when ``last_seq`` is unusable (absent, non-int,
        ``bool``, or zero — which is what the daemon answers for an empty replay
        buffer): on an uncertain cursor the safe direction is a possible duplicate,
        never lost output.
        """
        if not history:
            return
        turns = history[-_REHYDRATE_MAX_TURNS:]
        restored = (
            f"restored {len(turns)} of {len(history)} turns from transcript"
            if len(history) > len(turns)
            else f"restored {len(turns)} turns from transcript"
        )
        self._emit({"type": "frame", "frame": {"type": "system", "subtype": restored}})
        for turn in turns:
            role = turn.get("role")
            content = turn.get("content")
            if not isinstance(content, str) or not content:
                continue  # not a renderable turn — skip rather than emit an empty bubble
            if not isinstance(role, str) or role not in _REHYDRATE_ROLES:
                # The role becomes the frame's `type`, which is what the browser
                # dispatches on. Whitelist it so an unexpected role out of the
                # transcript can never render as a control_request or other
                # privileged frame kind.
                continue
            frame = {"type": role, "message": {"role": role, "content": content}}
            self._emit({"type": "frame", "frame": _redact_obj(frame)})
        bound = _as_seq(last_seq)
        if bound is not None and bound > 0:
            self._rehydrated_through = bound
        logger.info(
            "hosted: restored %d transcript turns for process %s (replay data frames "
            "through seq %d are covered by them)",
            len(turns),
            self._process_id,
            self._rehydrated_through,
        )

    def _suppressed_by_history(self, seq: Any, frame: dict[str, Any]) -> bool:
        """Whether a replayed data frame is already covered by rehydrated history (#1045).

        True only for a session that actually rehydrated, and only for frames at or
        below the daemon ``lastSeq`` captured at reattach — a live frame always renders.

        ``result`` frames are exempt. The transcript reader only yields records that
        carry a ``message``, so it structurally cannot regenerate a ``result`` frame —
        and a ``result`` with ``is_error`` is how a failed turn (auth failure, quota,
        teardown) reaches the operator. Suppressing one would swallow an error state
        into a silently-truncated transcript, which invariant 1 forbids.
        """
        if frame.get("type") == "result":
            return False
        seq_value = _as_seq(seq)
        return (
            self._rehydrated_through > 0
            and seq_value is not None
            and seq_value <= self._rehydrated_through
        )

    async def _pump(self, backlog: list[Mapping[str, Any]] | None = None) -> None:
        """Drain the process stream, routing each event until exit or cancel.

        ``backlog`` carries the frames :meth:`reattach` spilled out of the bounded
        subscriber queue while the transcript was being read (review catch): they are
        routed first, in arrival order, through exactly the same loop — so ring order
        and the suppression/gap logic see one uninterrupted sequence.
        """
        source = self._source
        if source is None:  # pragma: no cover - start() always sets it before pumping
            return
        pending = deque(backlog or ())
        try:
            while True:
                event = pending.popleft() if pending else await source.get()
                etype = event.get("type")
                seq = event.get("seq")
                seq_value = _as_seq(seq)
                if seq_value is not None and seq_value > self.daemon_last_seq:
                    self.daemon_last_seq = seq_value  # advance the reattach replay cursor
                if (
                    self._rehydrated_through
                    and seq_value is not None
                    and seq_value > self._rehydrated_through
                ):
                    # Past the rehydration window: the replay it covered is drained, so
                    # stop filtering. Makes the window terminal rather than a standing
                    # filter, so a daemon that under-reported `lastSeq` self-corrects on
                    # the first live frame instead of blanking the pane for good (#1045).
                    self._rehydrated_through = 0
                if etype == "line":
                    await self._on_line(event.get("stream"), event.get("line", ""), seq)
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

    async def _on_line(self, stream: Any, line: str, seq: Any = None) -> None:
        """Route one reassembled output line: stderr/non-JSON as text, else by frame type.

        ``seq`` is the daemon frame seq the line arrived on; it gates only the
        rehydration seam (:meth:`_suppressed_by_history`, #1045). It defaults to
        ``None`` rather than 0 so an unseq'd call fails *away* from suppression —
        0 would compare below every window and silently drop the line.
        """
        if stream == "stderr":
            self._emit({"type": "stderr", "text": sanitize_line(line)})
            return
        if not line.strip():
            return
        try:
            frame = json.loads(line)
        except (ValueError, RecursionError):
            # Not NDJSON — forward as opaque text rather than dropping it silently.
            # RecursionError: a deeply-nested line overflows CPython's recursive JSON
            # scanner, and it is not a ValueError — so it escaped this handler into
            # `_pump`, which catches only CancelledError/ClaustrumError. The pump task
            # died and the session went dark with no `lost` event (a fail-silent that
            # invariant 1 forbids). The scanner's ceiling is version-dependent — ~994 on
            # the 3.11 floor (a ~1 KB line), ~3000 on Windows and ~10000 elsewhere from
            # 3.12 — and the agent output line is unbounded, so every leg is reachable.
            # Degrading to opaque text isolates the bad frame instead of the stream.
            self._emit({"type": "text", "text": sanitize_line(line)})
            return
        if not isinstance(frame, dict):
            self._emit({"type": "text", "text": sanitize_line(line)})
            return
        if frame.get("type") == "control_request":
            await self._handle_control_request(frame)
            return
        # Capture the uuid BEFORE any suppression: the replayed `system` init frame is
        # where it comes from, and a rehydrated session still needs it for --resume.
        self._capture_session_uuid(frame)
        if self._suppressed_by_history(seq, frame):
            return
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
        """Latch the first ``session_id`` seen (drives ``--resume``); never overwrite it.

        Deliberately NOT shape-checked (:func:`_is_session_uuid`), unlike the persisted
        read and the argv seam (#1392). This is the value's *source* — claude's own init
        frame — so refusing it here would mean capturing nothing at all the day claude
        changes its id format, and a session that simply has no Resume is the silent
        failure invariant 1 exists to prevent. Refusing at the argv seam instead makes
        that same day produce a named 409 the operator can read. The latch cannot strand
        a good id behind a bad one either: every frame of one session carries the same
        ``session_id``, so "off-shape now, well-formed later" is not a state that occurs.
        """
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
        # Measure our OWN start identity for the (CT-1-reported) pid — that's what
        # CL-8 orphan validation compares against, NOT the daemon's startTime token
        # (decision (b)). Both halves in ONE `proc_start_pair` read (#1404): sampling the
        # epoch and the ticks separately puts a suspension point between two `/proc` reads,
        # and an agent that dies in that window can have its pid recycled, leaving a pair
        # whose halves describe DIFFERENT processes — which `is_live_process` would then
        # authenticate, because it matches the ticks exactly and the epoch only coarsely.
        # `(None, None)` when there's no pid (pre-CT-1 daemon) or it's unreadable.
        proc_start, start_ticks = (
            procutil.proc_start_pair(session.agent_pid) if session.agent_pid else (None, None)
        )
        instance = RemoteControlInstance(
            project=project,
            label=label,
            channel="hosted",
            permission_mode=permission_mode,
            claustrum_process_id=process_id,
            agent_pid=session.agent_pid,
            agent_proc_start=proc_start,
            agent_start_ticks=start_ticks,
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
        is unknown, still running, has no captured uuid to resume from, has a captured
        uuid that :func:`build_hosted_argv` refuses as malformed (#1392), or has no
        ``project`` to respawn into — the last of which is what a record that degraded on
        ``project`` leaves behind (#1381). A :class:`ClaustrumError` from the spawn
        propagates (the caller maps it).

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
        if not old.project:
            # A record that degraded on `project` gets `project=""` (`_degraded_row`). It
            # still reattaches — that binds by claustrum_process_id — but a respawn cannot:
            # the caller resolves the spawn cwd from this name, and the row below would carry
            # an empty project forward (#1381).
            #
            # The HTTP route checks this FIRST, before it resolves the project path, because
            # otherwise an empty name 404s there as "project '' not found" — which reads as
            # "that project is gone" and sends the operator looking in the wrong place. This
            # guard is the defence-in-depth half, for a caller that is not that route.
            raise HostedSessionError(NO_PROJECT_RESUME_DETAIL)
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
            await asyncio.to_thread(
                partial(
                    procutil.kill_if_match,
                    old.agent_pid,
                    old.agent_proc_start,
                    start_ticks=old.agent_start_ticks,
                )
            )
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
                    partial(
                        procutil.kill_if_match,
                        inst.agent_pid,
                        inst.agent_proc_start,
                        start_ticks=inst.agent_start_ticks,
                    )
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

        Both halves of the start identity are handed over (#1404). Without
        ``agent_start_ticks`` the epoch is compared against a 0.05s bound while psutil
        re-derives it from a ``/proc/stat`` btime that NTP moves by seconds, so on a
        drifting host a survivor reads as lost — the row loses Kill and Resume, and a
        Resume off a *different* row spawns a second agent beside the one still running.
        """
        return instance.agent_pid is not None and procutil.is_killable_hosted(
            instance.agent_pid, instance.agent_proc_start, start_ticks=instance.agent_start_ticks
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

    async def reattach_all(
        self,
        client: ClaustrumClient,
        *,
        history_for: Callable[[RemoteControlInstance], list[dict[str, Any]]] | None = None,
    ) -> list[RemoteControlInstance]:
        """Restore persisted hosted sessions on startup, reattaching the live ones.

        For each persisted record: an intentionally-stopped one is rebuilt as a
        stopped row (no reattach); otherwise we ``process.reattach`` from its saved
        ``daemon_last_seq``. Found+running → a live, pumping session; found+exited →
        finalized via the replayed exit; not-found → a CRASHED "session lost" row.
        Tolerates a daemon error per session (records it, keeps going — one bad
        reattach never blocks the rest or startup), and likewise an unreadable
        persisted record: :meth:`_instance_from_record` degrades that one row to
        defaults and logs it rather than raising through this loop (#1343). One
        cross-record repair happens here rather than in the mapper, because it needs
        the whole file: :meth:`_unique_instance_id` breaks an ``instance_id`` collision
        that would otherwise make :meth:`_key_for` resolve to the wrong session (#1381).
        Returns the restored instances.

        ``history_for`` resolves a row to its prior conversation, read from claude's
        on-disk transcript, so a reattached session's view isn't empty (#1045); the app
        supplies it, unit tests leave it ``None`` for a purely in-memory manager. It is
        blocking (it reads a file), so :meth:`_load_history` runs it off the loop and
        treats any failure as "no history" — rehydration must never fail a reattach.
        """
        records = self._store.load() if self._store is not None else {}
        # Seeded from the live registry rather than empty, so a second call cannot mint an id
        # that already belongs to a row this pass is not touching. Rows these records are
        # about to REPLACE are excluded, or a re-run would find every row's own id in `seen`,
        # re-mint all of them, warn a false collision each time, and persist the churn —
        # destroying exactly the cached client handles the keep-first rule protects.
        seen_instance_ids = {
            inst.instance_id for pid, inst in self._instances.items() if pid not in records
        }
        for process_id, fields in records.items():
            instance = self._instance_from_record(process_id, fields)
            self._unique_instance_id(instance, seen_instance_ids, records.keys())
            self._instances[process_id] = instance
            # From the row, not the raw record, for the same reason as the uuid below:
            # the mapper is the one place a persisted value is type-checked. Both agree
            # for every value today (`bool(...)` vs truthiness), so this is consistency,
            # not a fix — but the raw-record read is the pattern that produced one.
            if instance.intentional_stop:
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
            # From the rebuilt row, NOT the raw record: `_instance_from_record` is the
            # one place a persisted value is type-checked, and this assignment is not
            # validated (no `validate_assignment` on the model). Reading `fields` here
            # let a non-string uuid past the mapper, where it (a) latched
            # `_capture_session_uuid` shut so the real id from the replayed init frame
            # was discarded for the process lifetime, and (b) reached `--resume` in
            # `build_hosted_argv` — a persisted value the model rejected arriving in
            # spawn argv, which invariant 2 forbids.
            session.claude_session_uuid = instance.claude_session_uuid
            # A loader, not a pre-read list: reattach runs it AFTER the RPC has fixed
            # the daemon's lastSeq. Reading first would suppress frames emitted during
            # the read — see HostedSession._rehydrate.
            loader = partial(self._load_history, history_for, instance)
            try:
                # Reuse the cursor `_instance_from_record` already coerced rather than
                # re-deriving it from the raw record: a bare `int()` here raised
                # ValueError/TypeError/OverflowError on a junk persisted value, which is
                # not a ClaustrumError, so it escaped the handler below AND the lifespan's
                # (app.py) — a junk cursor failed the whole app boot. One coercion site.
                result = await session.reattach(instance.daemon_last_seq, history_loader=loader)
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
                    # A salvaged row can be an orphan AND have lost its `project` — the
                    # per-field salvage keeps the pid evidence while a model-rejected project
                    # falls back to empty. Naming Resume for such a row points at a button the
                    # dashboard does not render and an endpoint that answers 409 (#1381), so
                    # the offer is dropped and only the action that works is named.
                    #
                    # Both halves of the dashboard's gate, not just `project`: it renders
                    # Resume on `claude_session_uuid && project`, and a row can lose the uuid
                    # the same per-field way — `_as_session_uuid` drops one that is not
                    # session-shaped (#1392), which widened that set well past the empty
                    # string. Testing one half named an action the other half hid.
                    instance.error_detail = (
                        "survived a daemon restart — Resume to recover, or Kill"
                        if instance.project and instance.claude_session_uuid
                        else "survived a daemon restart — Kill to clean up"
                    )
                else:
                    instance.error_detail = "daemon restarted; session lost"
                continue
            # A clean reattach: drop the salvage's "record was unreadable" note so it does
            # not later render as this running session's ENDING reason (the WARNING logged at
            # salvage time stays the durable record). Only that note is cleared.
            if instance.error_detail == _UNREADABLE_RECORD_DETAIL:
                instance.error_detail = None
            self._sessions[process_id] = session
        await self._persist()
        return [self._synced(inst) for inst in self._instances.values()]

    @staticmethod
    async def _load_history(
        history_for: Callable[[RemoteControlInstance], list[dict[str, Any]]] | None,
        instance: RemoteControlInstance,
    ) -> list[dict[str, Any]] | None:
        """Read one reattaching session's prior transcript off the event loop (#1045).

        Rehydration is a convenience layered on top of reattach, so it degrades rather
        than propagates: a missing transcript, an unreadable one, or a resolver that
        raises leaves the view exactly as it was before this feature (empty, plus
        whatever the daemon replays) and is logged — never a failed reattach or a
        blocked startup. ``history_for`` reads a file, so it runs in a thread.

        The catch is deliberately broad (same posture as :meth:`_notify_permission_needed`):
        this runs inside the app lifespan, so *any* resolver fault — a decode error, a
        ``MemoryError`` on an oversized transcript, a bug in the resolver — must degrade
        to "no history" rather than abort startup and take every session down with it.
        ``CancelledError`` is a ``BaseException``, so shutdown cancellation still
        propagates and this stays cancel-safe.
        """
        if history_for is None:
            return None
        try:
            return await asyncio.to_thread(history_for, instance)
        except Exception as exc:  # noqa: BLE001 - a transcript fault must not fail startup
            logger.warning(
                "hosted: could not restore the transcript for %s: %s",
                instance.claustrum_process_id,
                exc,
            )
            return None

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
            "agent_start_ticks": instance.agent_start_ticks,
            "started_at": instance.started_at.isoformat() if instance.started_at else None,
            "intentional_stop": instance.intentional_stop,
            "instance_id": instance.instance_id,
        }

    @staticmethod
    def _instance_from_record(process_id: str, fields: dict) -> RemoteControlInstance:
        """Rebuild a hosted instance row from a persisted record (inverse of _record).

        Total over the record map (#1343). :func:`_coerce_seq` made one field total;
        every *other* persisted field still reached the model unguarded, so a junk
        ``project``/``label``/``permission_mode``/``agent_pid``/… raised
        ``ValidationError`` — which is not a :class:`ClaustrumError`, so it escaped
        both :meth:`reattach_all`'s per-session handler and the lifespan's in
        ``app.py``: one corrupt record failed the whole app boot. Such a record now
        degrades to :meth:`_degraded_row` and is logged at WARNING — visibly, not
        silently, and per-record, so the other sessions still reattach.
        """
        try:
            return HostedManager._row_from_record(process_id, fields)
        except ValidationError as exc:
            # Field names + pydantic's error *codes* only. The values are what the
            # daemon and claude wrote; echoing them into the log would put session
            # metadata in a file redaction never sees.
            bad = ", ".join(
                f"{'.'.join(str(part) for part in err['loc'])}:{err['type']}"
                for err in exc.errors()
            )
            logger.warning(
                "hosted: persisted record for %s is unreadable (%s); reattaching it with "
                "default metadata rather than failing startup",
                process_id,
                bad or "unknown field",
            )
            return HostedManager._degraded_row(process_id, fields)

    @staticmethod
    def _degraded_row(process_id: str, fields: dict) -> RemoteControlInstance:
        """Rebuild the row field by field, keeping every value that checks out.

        The salvage half of the guard above, and the reason a rejected record degrades
        rather than being skipped: reattach binds by ``claustrum_process_id``, so the
        row this returns still reattaches the live daemon session. Dropping it would
        orphan a running agent — the failure hosted persistence exists to prevent.

        Per field rather than wholesale, because ``reattach_all`` ends in a
        :meth:`_persist` that rewrites the record from this row: defaulting the other
        eleven fields because one is junk would *destroy* them on the first boot after
        corruption, leaving nothing to repair by hand. Four of them are load-bearing
        beyond display — ``agent_pid``/``agent_proc_start``/``agent_start_ticks`` are the
        only evidence :meth:`_is_orphan` has, so losing them downgrades a recoverable CL-8
        survivor to "session lost" and lets ``forget`` drop clauster's last record of a
        live process, and ``claude_session_uuid`` is what drives ``--resume``. Only the
        value the model actually rejected falls back to its default.

        This function must not raise, or it reopens the very escape it exists to close
        (its caller catches ``ValidationError`` and nothing else). Every value is
        therefore type-tested against the model's own annotation — which
        ``RemoteControlInstance`` constrains no further — and the two coercions that
        can fail on a well-typed value, :func:`_as_permission_mode` (a membership test
        *hashes*) and :func:`_as_proc_start` (``float()`` overflows), swallow their own
        errors. Verified by sweeping every field against every JSON shape, not asserted.
        """
        project = fields.get("project")
        label = fields.get("label")
        mode = fields.get("permission_mode")
        session_uuid = fields.get("claude_session_uuid")
        agent_pid = fields.get("agent_pid")
        proc_start = fields.get("agent_proc_start")
        instance = RemoteControlInstance(
            project=project if isinstance(project, str) else "",
            label=label if isinstance(label, str) else f"hosted:{process_id[:8]}",
            channel="hosted",
            permission_mode=_as_permission_mode(mode),
            claustrum_process_id=process_id,
            claude_session_uuid=_as_session_uuid(session_uuid),
            daemon_last_seq=_coerce_seq(fields.get("daemon_last_seq")),
            hosted_log_path=_as_log_path(fields.get("hosted_log_path")),
            agent_pid=_as_pid(agent_pid),
            agent_proc_start=_as_proc_start(proc_start),
            agent_start_ticks=_as_start_ticks(fields.get("agent_start_ticks")),
            started_at=_as_started_at(fields.get("started_at")),
            intentional_stop=bool(fields.get("intentional_stop", False)),
            status=InstanceStatus.STARTING,
            error_detail=_UNREADABLE_RECORD_DETAIL,
        )
        return HostedManager._restore_instance_id(instance, fields)

    @staticmethod
    def _row_from_record(process_id: str, fields: dict) -> RemoteControlInstance:
        """Map a well-formed record onto the model; raises ``ValidationError`` if it isn't."""
        instance = RemoteControlInstance(
            project=fields.get("project", ""),
            label=fields.get("label", f"hosted:{process_id[:8]}"),
            channel="hosted",
            permission_mode=fields.get("permission_mode", "default"),
            claustrum_process_id=process_id,
            # Every evidence field below is coerced by the SAME helper on both paths, so
            # they cannot disagree about the same bytes and leave a degraded row without the
            # CL-8 orphan evidence (or the resume uuid) a healthy one would have kept.
            claude_session_uuid=_as_session_uuid(fields.get("claude_session_uuid")),
            daemon_last_seq=_coerce_seq(fields.get("daemon_last_seq")),
            hosted_log_path=_as_log_path(fields.get("hosted_log_path")),
            agent_pid=_as_pid(fields.get("agent_pid")),
            agent_proc_start=_as_proc_start(fields.get("agent_proc_start")),
            agent_start_ticks=_as_start_ticks(fields.get("agent_start_ticks")),
            started_at=_as_started_at(fields.get("started_at")),
            intentional_stop=bool(fields.get("intentional_stop", False)),
            status=InstanceStatus.STARTING,
        )
        return HostedManager._restore_instance_id(instance, fields)

    @staticmethod
    def _unique_instance_id(
        instance: RemoteControlInstance, seen: set[str], keys: Collection[str] = ()
    ) -> None:
        """Mint a fresh ``instance_id`` for ``instance`` if it would be ambiguous.

        :meth:`_restore_instance_id` type-checks a persisted id but cannot check
        UNIQUENESS: it sees one record at a time, and uniqueness is a property of the whole
        file. A hand-edited ``hosted_state.json`` with two records sharing an ``instance_id``
        therefore restored both, and :meth:`_key_for` resolves that id by scanning
        ``_instances`` and returning the FIRST match — so a client that cached the id could
        be handed the wrong session, and every lifecycle call keyed by it (stop, kill, resume,
        input) would land on that wrong session. Silently, since dict iteration order makes
        "first" the insertion order of a file the operator hand-edited (#1381).

        BOTH halves of the namespace :meth:`_key_for` searches are checked, not just the id
        one. That method tries the registry KEYS first, so an ``instance_id`` equal to another
        row's ``claustrum_process_id`` misroutes exactly the same way — and the two formats
        colliding is only implausible, not impossible, in a file someone edited by hand.
        ``keys`` carries the record keys, which ``reattach_all`` knows upfront. A row matching
        its OWN key is harmless (``_key_for`` returns that same row) and is left alone.

        Keep-first, mint-for-the-rest: the earlier record keeps the id a client may already
        hold. Minting rather than dropping the row matters for the same reason
        :meth:`_degraded_row` exists — reattach binds by ``claustrum_process_id``, so the row
        still reattaches its live daemon session; only the cached handle is lost, which is
        exactly what :meth:`_restore_instance_id` already warns about for an unusable id. The
        repair is durable: ``reattach_all`` ends in a :meth:`_persist` that writes the new id
        back, so the collision does not return on the next boot.

        Mutates in place and warns; the caller owns ``seen``.
        """
        own_key = instance.claustrum_process_id
        shadows_a_key = instance.instance_id in keys and instance.instance_id != own_key
        if instance.instance_id in seen or shadows_a_key:
            instance.instance_id = new_instance_id()
            # The colliding value is not echoed because it adds nothing: the process id below
            # is what identifies the row an operator has to go and fix, and the id they wrote
            # is already in front of them in the file. (It is not a redaction claim — the key
            # logged here comes from the same operator-editable store.)
            logger.warning(
                "hosted: the persisted instance_id for %s is already in use by another record "
                "or is another record's process id; minting a fresh one — a client's cached "
                "id will no longer resolve to this session",
                instance.claustrum_process_id,
            )
        seen.add(instance.instance_id)

    @staticmethod
    def _restore_instance_id(
        instance: RemoteControlInstance, fields: dict
    ) -> RemoteControlInstance:
        """Reinstate the persisted per-runtime ``instance_id`` on a rebuilt row.

        Restores it (#834/#840) so a client that cached the id before the restart still
        resolves via :meth:`HostedManager._key_for` instead of hitting the freshly-minted
        ``default_factory`` id (#841). Set after construction rather than passed to it —
        a bare ``**fields`` unpack would let heterogeneous/unknown persisted keys reach
        the model directly. An assignment is NOT validated (the model does not set
        ``validate_assignment``), so this ``str`` check is the only thing standing between
        a hand-edited record and a non-string key in the registry; the
        ``ValidationError`` guard cannot see an assignment. A dropped id is logged, since
        it costs a client its cached handle.
        """
        instance_id = fields.get("instance_id")
        if isinstance(instance_id, str) and instance_id:
            instance.instance_id = instance_id
        elif instance_id is not None:
            logger.warning(
                "hosted: persisted instance_id for %s is unusable (%s); minting a "
                "fresh one — a client's cached id will no longer resolve",
                instance.claustrum_process_id,
                "empty string" if isinstance(instance_id, str) else type(instance_id).__name__,
            )
        return instance


#: Why a hosted row whose persisted ``project`` degraded to ``""`` cannot be resumed (#1381).
#: One constant because two layers must agree on it: the HTTP route checks before resolving
#: the project path (an empty name would 404 there as "project '' not found", which reads as
#: "that project is gone"), and :meth:`HostedManager._resume_locked` checks again for any
#: caller that is not that route. Carries no persisted value — fixed text only.
NO_PROJECT_RESUME_DETAIL = (
    "cannot resume: this session's saved record was unreadable and its project is unknown — "
    "Kill it and start a new session in the right project"
)

#: The permission modes the model accepts, read off the ``Literal`` itself so a mode
#: added to the config can never silently become "unsalvageable" here.
_PERMISSION_MODES = frozenset(get_args(PermissionMode))

#: Carried on a row rebuilt by :meth:`HostedManager._degraded_row`, so the degradation
#: travels with the API row and not only in the journal warning. Deliberately says
#: nothing about the reattach outcome: the row this lands on may go on to be reattached,
#: rebuilt as an intentionally-stopped row, or CRASHED — and in the last two cases
#: ``reattach_all`` overwrites this detail with the more actionable message anyway.
#: (No template renders a *running* hosted row's ``error_detail`` today; a stopped or
#: ended row's live view does. Treat the log line as the reliable channel.)
_UNREADABLE_RECORD_DETAIL = (
    "part of this session's saved record was unreadable and was reset to defaults"
)


def _as_started_at(value: Any) -> datetime | None:
    """Parse a persisted ISO-8601 ``started_at``; ``None`` if absent or unparseable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _as_permission_mode(value: Any) -> PermissionMode:
    """Keep a persisted permission mode only if the config still defines it.

    Anything else becomes ``"default"`` — the restrictive direction, so a junk mode can
    never widen what the reattached session is allowed to do without asking.

    The ``isinstance`` is load-bearing, not belt-and-braces: ``x in frozenset`` *hashes*
    ``x``, so a persisted ``{}``/``[]`` would raise ``TypeError: unhashable type`` — an
    exception the caller's ``except ValidationError`` does not catch, i.e. the same
    escape to the lifespan that #1343 exists to close.
    """
    if isinstance(value, str) and value in _PERMISSION_MODES:
        # The membership test IS the validation — `_PERMISSION_MODES` is built from the
        # Literal's own members, so a hit is one of them by construction.
        return cast(PermissionMode, value)
    return "default"


def _as_proc_start(value: Any) -> float | None:
    """Coerce a persisted ``agent_proc_start`` to a float, falling back to ``None``.

    Used by BOTH mapping paths so they cannot disagree about the same bytes — the twin of
    :func:`_as_pid` for the other half of the orphan-recovery evidence pair. Stricter than
    pydantic's lax coercion on purpose: that would accept a numeric string (``"1234.5"`` →
    ``1234.5``) and ``true`` → ``1.0``, so handing the raw value to pydantic on the healthy
    path let it KEEP values the salvage path drops. ``_record`` only ever writes a float, so
    nothing legitimate is refused; a refusal is logged. And the *fallback* must not raise:
    JSON can hold an int too large for a float, and a bare ``float()`` on it raises
    ``OverflowError``, which the caller's ``except ValidationError`` does not catch — the
    escape #1343 exists to close, reopened inside its own fix. ``None`` means "unknown start
    time", which :func:`procutil.is_killable_hosted` treats as not-safely-killable: the
    fail-closed direction for orphan recovery.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        if value is not None:
            logger.warning(
                "hosted: refusing a non-numeric agent_proc_start (%s) from a persisted "
                "record; the row loses its orphan-recovery start-time evidence",
                type(value).__name__,
            )
        return None
    try:
        return float(value)
    except (OverflowError, ValueError):
        return None


def _as_session_uuid(value: Any) -> str | None:
    """Coerce a persisted ``claude_session_uuid`` to a session-id-shaped str, else ``None``.

    Used by BOTH mapping paths so they cannot disagree about the same bytes — the third of
    the :func:`_as_pid` / :func:`_as_proc_start` family, and the last evidence field that was
    not symmetric (#1380). The salvage path always normalized ``""`` away while the healthy
    path handed the raw value to pydantic, which accepts ``""`` for a ``str | None`` field.
    An empty uuid is ``not None``, so it latched :meth:`HostedSession._capture_session_uuid`
    shut and the real id from the replayed init frame was discarded for the process lifetime
    — taking ``--resume`` with it.

    Stricter than pydantic on purpose, for the same reason as the siblings: lax validation
    lets the healthy path KEEP a value the salvage path drops. ``_capture_session_uuid``
    stores only a non-empty ``str``, so nothing legitimate is refused; a refusal is logged,
    because ``_persist`` writes the ``None`` back and a resume the row could once have
    offered disappears with nothing else to explain it.

    Non-empty is not enough on its own (#1392): the value's *executing* consumer is
    :func:`build_hosted_argv`'s ``--resume`` slot (the rest — the transcript lookup, the
    MCP summary, the dashboard's Resume gate — only read it), so it must also be *shaped*
    like a session id. See :data:`_SESSION_UUID_SHAPE_RE` for why a flag-shaped token
    there is an argv-injection seam rather than a cosmetic complaint.
    ``ops restore`` from a tampered backup reaches this function with no code execution,
    which is what makes the record an untrusted input in the first place.
    """
    if _is_session_uuid(value):
        return value
    if value is not None:
        logger.warning(
            # Colon, not the `(%s)` its three siblings use: they interpolate a bare type
            # name, which reads fine parenthesised, while this helper's answer carries its
            # own punctuation and would nest a parenthesis inside one.
            "hosted: refusing a claude_session_uuid from a persisted record: %s; the row "
            "loses its resume evidence",
            _refused_uuid_shape(value),
        )
    return None


def _as_log_path(value: Any) -> Path | None:
    """Rebuild a persisted ``hosted_log_path``; ``None`` unless it is a non-empty string."""
    return Path(value) if isinstance(value, str) and value else None


def _is_plain_int(value: Any) -> TypeGuard[int]:
    """Report whether ``value`` is an ``int`` and not a ``bool`` (which subclasses ``int``)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _as_pid(value: Any) -> int | None:
    """Coerce a persisted ``agent_pid`` to an int, falling back to ``None``.

    Used by both mapping paths so they cannot disagree about the same bytes. Stricter
    than pydantic's lax coercion on purpose: that accepts ``true`` as pid **1** — init
    on every POSIX host — and ``4242.0`` as 4242. ``_record`` only ever writes an int,
    so nothing legitimate is refused; a refusal is logged. ``None`` means "no pid
    evidence", which :meth:`HostedManager._is_orphan` reads as not-recoverable: the
    fail-closed direction, since the alternative is offering Kill against a pid we cannot
    vouch for. The WARNING matters because ``_persist`` writes the ``None`` back, so the
    orphan evidence a prior release kept (lax ``"4242"`` → 4242) is dropped for good and a
    row can start reading "session lost" with nothing else to explain it.
    """
    if _is_plain_int(value):
        return value
    if value is not None:
        logger.warning(
            "hosted: refusing a non-integer agent_pid (%s) from a persisted record; the "
            "row loses its orphan-recovery pid evidence",
            type(value).__name__,
        )
    return None


def _as_start_ticks(value: Any) -> int | None:
    """Coerce a persisted ``agent_start_ticks`` to an int, falling back to ``None``.

    The fourth member of the :func:`_as_pid` / :func:`_as_proc_start` / :func:`_as_session_uuid`
    family, and used by BOTH mapping paths for the same reason they are: a value the healthy
    path kept and the salvage path dropped would leave a degraded row without the
    drift-immune half of its orphan evidence, and ``_persist`` writes that loss back.

    Stricter than pydantic's lax coercion on purpose, exactly as :func:`_as_pid` is: that
    accepts ``true`` as tick count **1** and ``770579.0`` as 770579. ``_record`` only ever
    writes an int (or ``None``), so nothing legitimate is refused.

    Negative is refused too, which :func:`_as_pid` has no equivalent of. Field 22 of
    ``/proc/<pid>/stat`` is an unsigned count of ticks since boot, so a negative value cannot
    have come from :func:`procutil.proc_start_ticks`; it can only come from a hand-edited row
    or a tampered ``ops restore`` backup. Keeping it would not be dangerous — the comparison
    is an equality test against a value read live, which no real process can match — but
    dropping it is what makes the row fall back to the epoch it still has rather than to a
    tick compare that is guaranteed to fail and would refuse every kill.

    A refusal is logged: ``_persist`` writes the ``None`` back, so the row silently reverts
    to the drifting epoch-only comparison #1404 exists to end, with nothing else to explain
    it.
    """
    if _is_plain_int(value) and value >= 0:
        return value
    if value is not None:
        logger.warning(
            "hosted: refusing an unusable agent_start_ticks (%s) from a persisted record; "
            "the row falls back to the drift-prone epoch-only liveness compare",
            type(value).__name__,
        )
    return None


def _coerce_seq(value: Any) -> int:
    """Coerce a persisted ``daemon_last_seq`` to an int, falling back to ``0``.

    This keeps **this one field** of the on-disk ``hosted_state.json`` record map
    out of the mapper's fallback path. A bare ``int(...)``
    was not: a non-numeric string (``ValueError``), a dict/list (``TypeError``),
    a NaN (``ValueError``) or an ``inf`` (``OverflowError``) each aborted the
    whole reattach on restart. (The mapper's other persisted fields are covered
    separately, by :meth:`HostedManager._instance_from_record`'s ``ValidationError``
    guard — #1343. This helper is still the *only* thing that keeps a junk cursor
    from costing the rest of the row its metadata.) An uncoercible
    cursor degrades to 0 — replay from the
    start of the retained window, the fail-*visible* direction: the client sees
    frames it may already have, rather than the session silently vanishing.

    ``bool`` is rejected for the same reason :func:`_as_seq` rejects it: it
    subclasses ``int``, so a persisted ``true`` would read as seq 1 and *skip*
    frame 1 — a silent missed frame, the one direction this must not fail in.
    A negative cursor is clamped to 0 rather than passed through, so
    :meth:`HostedSession._note_replay_gap` cannot report a fabricated eviction
    range for frames that never existed.
    """
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


# Depth cap for the recursive walk in :func:`_redact_obj`. `json.loads` in `_on_line`
# parses frames nested far deeper than this walker can descend, so an unguarded
# recursion raised RecursionError inside `_pump` — which catches only CancelledError
# and ClaustrumError. The pump task died and the hosted session went dark with no
# `lost` event: a fail-*silent*, which invariant 1 forbids. Real stream-json frames
# nest a handful of levels; this cap is orders of magnitude above that and far below
# CPython's recursion limit, so a legitimate frame can never reach it.
_REDACT_MAX_DEPTH = 100

# What a past-the-cap subtree is replaced by. A constant carrying no input bytes: the
# redactor must still return a value (the frame has to reach the browser), and dropping
# the subtree wholesale is the only way to keep invariant 4 — no unredacted leaf escapes.
_REDACT_TOO_DEEP = "<clauster: frame nesting too deep to redact>"


def _redact_obj(obj: Any, _depth: int = 0) -> Any:
    """Recursively sanitize every string leaf of a parsed JSON frame.

    Defense-in-depth over the structured stream: the same redaction the WS bridge
    log applies (ANSI strip + id/secret masking via :func:`sanitize_line`) is run
    on each string value, so a session/env identifier or obvious secret embedded
    anywhere in tool output or assistant text never reaches a browser subscriber.

    Never raises on a deeply-nested frame: a container nested past
    :data:`_REDACT_MAX_DEPTH` is replaced by :data:`_REDACT_TOO_DEEP` rather than
    recursed into. ``_depth`` is internal bookkeeping — callers pass one argument.
    """
    if isinstance(obj, str):
        return sanitize_line(obj)
    if isinstance(obj, dict | list):
        if _depth >= _REDACT_MAX_DEPTH:
            return _REDACT_TOO_DEEP
        if isinstance(obj, dict):
            return {k: _redact_obj(v, _depth + 1) for k, v in obj.items()}
        return [_redact_obj(v, _depth + 1) for v in obj]
    return obj
