"""Live-streaming WebSocket routes, split from ``create_app`` (#1156).

The four sockets here tail a bridge debug log, a hosted session's event ring, a
clone job's progress, and a pty bridge's live screen. Every one gates the
handshake through :func:`_ws_gate` BEFORE :meth:`WebSocket.accept` (auth before
accept, invariant 1/D12), and everything that reaches the wire passes through
:func:`clauster.redact.sanitize_line` or a redacted-at-source sidecar
(invariant 4).

The injected dependencies (``ConfigDep``, ``RunnerDep``/``HostedDep``/``CloneJobsDep``,
``AuthenticateDep``, ``AllowedOriginsDep``) resolve before the handler body, so they run
ahead of ``_ws_gate``. Today they only read ``app.state`` objects a wired app always
publishes; a dependency added here that inspects the request would be making a pre-gate
decision, so keep any such check inside ``_ws_gate`` instead.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import auth, logstream, pty_screen
from ..dependencies import (
    AllowedOriginsDep,
    AuthenticateDep,
    CloneJobsDep,
    ConfigDep,
    HostedDep,
    RunnerDep,
)
from ..redact import sanitize_line

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import HTTPConnection

    from ..config import ClausterConfig

logger = logging.getLogger(__name__)

router = APIRouter()

# How often the /ws/pty-screen reader re-reads the keeper's screen sidecar. Matched to the
# keeper's _SCREEN_FLUSH_INTERVAL (0.25s) so the poll roughly tracks the publish cadence
# without busy-spinning; frames already seen are skipped by their monotonic ``seq``.
_SCREEN_POLL_INTERVAL = 0.25


def _reap_ws_task(task: asyncio.Task) -> None:
    """Retrieve a finished WS helper task's outcome so the loop never warns about it."""
    if not task.cancelled() and task.exception() is not None:
        logger.debug("ws stream helper task ended with %r", task.exception())


async def stream_until_disconnect(
    websocket: WebSocket, stream: Callable[[], Awaitable[None]]
) -> None:
    """Run a send-only WebSocket ``stream`` until it finishes or the client goes away.

    A send-only handler never awaits ``receive()``, so it cannot observe the
    client's disconnect (or the server's shutdown close): blocked on its idle event
    source, it becomes a ghost ASGI task that uvicorn's graceful shutdown waits on
    forever — and its subscription leaks. Race the stream against a receive loop:
    the first ``websocket.disconnect`` cancels the stream; anything else the client
    sends is ignored. Errors from the stream itself (e.g. send-after-close) still
    propagate to the caller.
    """
    stream_task = asyncio.ensure_future(stream())
    recv_task = asyncio.ensure_future(websocket.receive())
    try:
        while True:
            done, _ = await asyncio.wait(
                {stream_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stream_task in done:
                stream_task.result()  # propagate the stream's exception, if any
                return
            if recv_task.result()["type"] == "websocket.disconnect":
                return
            recv_task = asyncio.ensure_future(websocket.receive())
    finally:
        for task in (stream_task, recv_task):
            task.cancel()
            # Reap via callback instead of awaiting here: an await inside this
            # finally re-receives the handler's own in-flight cancellation (the
            # test client / server re-delivers it until its scope exits) and turns
            # a clean close into a cancelled ASGI task.
            task.add_done_callback(_reap_ws_task)


async def _ws_authorized(
    websocket: WebSocket,
    authenticate: Callable[[HTTPConnection], Awaitable[tuple[str | None, bool, bool]]],
    allowed_origins: set[str],
) -> bool:
    """Session/proxy/token auth, BEFORE accepting (D12).

    The strict Origin allowlist applies to ambient (cookie/proxy) credentials only;
    the Bearer-token path is exempt.
    """
    user, _via_proxy, via_token = await authenticate(websocket)
    if user is None:
        return False
    # The Origin allowlist is a cross-site WS-hijack defense for ambient
    # (cookie) credentials — browsers always send Origin. A Bearer-token
    # client (headless/API) carries no ambient credential and sends no
    # Origin, so the check would wrongly reject it; exempt the token path
    # exactly as the HTTP CSRF gate does. Cookie/proxy auth still needs it.
    if via_token:
        return True
    origin = websocket.headers.get("origin")
    return bool(origin) and auth.normalize_origin(origin) in allowed_origins


async def _ws_gate(
    websocket: WebSocket,
    config: ClausterConfig,
    authenticate: Callable[[HTTPConnection], Awaitable[tuple[str | None, bool, bool]]],
    allowed_origins: set[str],
) -> bool:
    """Whether the WS handshake may proceed, BEFORE accept() (D12).

    The Origin allowlist is a cross-site WS-hijack defence, NOT an
    authentication method, so it must run even when ``config.auth.enabled``
    is false (the shipped default). Tying it to the auth master switch let
    ``config.auth.enabled and ...`` short-circuit, so ``_ws_authorized``
    never ran and any page the operator visited could open a socket to the
    loopback service and read-stream live output (CWE-1385). A browser
    WebSocket ALWAYS sends Origin, so an absent Origin means a non-browser
    client (never the cross-site attack) and passes; a present,
    non-allowlisted Origin is rejected. When auth is enabled the full
    session/proxy/token gate applies unchanged (it also rejects an absent
    Origin for ambient cookie credentials, and exempts the Bearer path).
    """
    if config.auth.enabled:
        return await _ws_authorized(websocket, authenticate, allowed_origins)
    origin = websocket.headers.get("origin")
    return origin is None or auth.normalize_origin(origin) in allowed_origins


@router.websocket("/ws/bridge-log/{instance_id}")
async def ws_bridge_log(
    websocket: WebSocket,
    instance_id: str,
    config: ConfigDep,
    runner: RunnerDep,
    authenticate: AuthenticateDep,
    allowed_origins: AllowedOriginsDep,
) -> None:
    """Tail the bridge debug log — ID-redacted, and ANSI-stripped by default (feature 6).

    Redaction is unconditional; ANSI stripping follows ``logs.strip_ansi_in_stream``
    (D11).
    """
    if not await _ws_gate(websocket, config, authenticate, allowed_origins):
        await websocket.close(code=1008)  # validate before accept — never open an unauthed socket
        return
    await websocket.accept()
    # The client tags the tail with the project name (#777); resolve it to the
    # registry's instance_id before lookup so the socket doesn't 1008 for a live bridge.
    resolved = runner.resolve_bridge_id(instance_id)
    instance = runner.get_instance(resolved) if resolved is not None else None
    if instance is None or instance.bridge_debug_log_path is None:
        await websocket.close(code=1008)  # nothing to stream
        return
    # Stream the verbatim raw parse-source (== the debug log unless on-disk
    # redaction split it off), sanitizing each line in-flight as always — so the
    # live stream stays current regardless of the at-rest mirror's refresh cadence.
    path = instance.bridge_raw_log_path or instance.bridge_debug_log_path
    strip = config.logs.strip_ansi_in_stream
    offset = await asyncio.to_thread(logstream.initial_offset, path)

    async def _stream() -> None:
        """Tail the bridge log from the opening offset, pushing each line to the socket."""
        carry = ""
        local_offset = offset
        while True:
            local_offset, text = await asyncio.to_thread(logstream.read_new, path, local_offset)
            if text:
                # Buffer whole lines so redaction never misses an id split
                # across two reads.
                *lines, carry = (carry + text).split("\n")
                for line in lines:
                    await websocket.send_text(sanitize_line(line, strip_ansi_seq=strip))
            await asyncio.sleep(0.5)

    try:
        await stream_until_disconnect(websocket, _stream)
    except (WebSocketDisconnect, RuntimeError):
        return


@router.websocket("/ws/hosted/{instance_id}")
async def ws_hosted(
    websocket: WebSocket,
    instance_id: str,
    config: ConfigDep,
    hosted: HostedDep,
    authenticate: AuthenticateDep,
    allowed_origins: AllowedOriginsDep,
) -> None:
    """Stream a hosted session's live events, replaying the ring past ``?after=``."""
    if not await _ws_gate(websocket, config, authenticate, allowed_origins):
        await websocket.close(code=1008)  # validate before accept
        return
    await websocket.accept()
    session = hosted.session(instance_id)
    if session is None:
        await websocket.close(code=1008)  # unknown / already gone
        return
    try:
        after = int(websocket.query_params.get("after", "0"))
    except (TypeError, ValueError):
        after = 0
    queue = session.subscribe(after_seq=after)

    async def _stream() -> None:
        """Forward every queued hosted-session event to the socket."""
        while True:
            await websocket.send_json(await queue.get())

    try:
        await stream_until_disconnect(websocket, _stream)
    except (WebSocketDisconnect, RuntimeError):
        return
    finally:
        session.unsubscribe(queue)


@router.websocket("/ws/clone-progress/{job_id}")
async def ws_clone_progress(
    websocket: WebSocket,
    job_id: str,
    config: ConfigDep,
    clone_jobs: CloneJobsDep,
    authenticate: AuthenticateDep,
    allowed_origins: AllowedOriginsDep,
) -> None:
    """Stream a clone job's ``{phase, percent}`` progress, then a terminal frame."""
    if not await _ws_gate(websocket, config, authenticate, allowed_origins):
        await websocket.close(code=1008)  # validate before accept
        return
    await websocket.accept()
    job = clone_jobs.get(job_id)
    if job is None:
        await websocket.close(code=1008)  # unknown / already pruned
        return
    # Subscribe before the status check so a terminal that fires between the
    # check and the snapshot send below lands in our queue, not the void.
    queue = job.subscribe()

    async def _stream() -> None:
        """Send the current clone snapshot, then stream progress until the job ends."""
        if job.status != "running":
            # Already finished (e.g. a reconnect after completion).
            await websocket.send_json(job.terminal_event())
            return
        await websocket.send_json(job.progress_event())  # current snapshot
        while True:
            event = await queue.get()
            await websocket.send_json(event)
            if event.get("type") == "done":
                break

    try:
        await stream_until_disconnect(websocket, _stream)
    except (WebSocketDisconnect, RuntimeError):
        return
    finally:
        job.unsubscribe(queue)


@router.websocket("/ws/pty-screen/{instance_id}")
async def ws_pty_screen(
    websocket: WebSocket,
    instance_id: str,
    config: ConfigDep,
    runner: RunnerDep,
    authenticate: AuthenticateDep,
    allowed_origins: AllowedOriginsDep,
) -> None:
    """Stream a pty bridge's redacted, cells-only live-screen frames (read-only, #534).

    The keeper publishes redacted frames to a screen sidecar (off by default); this polls
    that file and forwards each new frame — de-duped by its monotonic ``seq`` — as JSON.
    The wire carries only pyte-rendered cells + cursor + state, never raw ANSI, so the
    at-rest redaction invariant holds end to end.
    """
    if not await _ws_gate(websocket, config, authenticate, allowed_origins):
        await websocket.close(code=1008)  # validate before accept
        return
    await websocket.accept()
    # The client tags the view with the project name (#777); resolve to instance_id.
    resolved = runner.resolve_bridge_id(instance_id)
    instance = runner.get_instance(resolved) if resolved is not None else None
    # The live screen exists only for a pty bridge with the (default-off) tap enabled;
    # anything else has no sidecar to stream, so refuse rather than hang silently.
    if (
        instance is None
        or instance.resume_mode != "pty"
        or instance.bridge_debug_log_path is None
        or not config.claude.pty_screen_enabled
    ):
        await websocket.close(code=1008)
        return
    sidecar = pty_screen.screen_sidecar_path(instance.bridge_debug_log_path)

    async def _stream() -> None:
        """Poll the pty screen sidecar and push each newer frame to the socket."""
        last_seq = -1
        while True:
            frame = await asyncio.to_thread(pty_screen.read_screen_sidecar, sidecar)
            if frame is not None:
                seq = frame.get("seq")
                if isinstance(seq, int) and seq > last_seq:
                    last_seq = seq
                    await websocket.send_json(frame)
            await asyncio.sleep(_SCREEN_POLL_INTERVAL)

    try:
        await stream_until_disconnect(websocket, _stream)
    except (WebSocketDisconnect, RuntimeError):
        return
