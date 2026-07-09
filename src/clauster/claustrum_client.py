"""Async unix-socket NDJSON JSON-RPC client for the claustrum daemon.

claustrum (`/mnt/nas/projects/claustrum`, the maintainer's Go ``claude-ssh``
reimpl) speaks newline-delimited JSON-RPC 2.0 over an ``AF_UNIX`` stream socket.
This module is clauster's foundation for the *hosted* channel (CL-1): a
dependency-free :class:`ClaustrumClient` that owns one persistent connection,
correlates id-bearing responses to their requests, and demuxes the id-less
``type:"stream"`` notifications into per-process :class:`ProcessStream` fan-out.

What this slice deliberately does NOT do (later slices own these): connect-or-
spawn daemon lifecycle (CL-2 ``claustrum_daemon``), auto-reconnect-with-backoff
+ reattach-on-startup (CL-6), and the hosted-session spawn/redact/broadcast
pipeline (CL-4 ``hosted``). The :meth:`ClaustrumClient.reattach` RPC — the
mechanism CL-6 builds on — is provided here.

Wire contract (``claustrum/docs/PROTOCOL.md``), the parts this client honors:

* Every request carries a top-level ``"auth":"<token>"``; a bad/missing token is
  ``-32001``. Auth is injected at one chokepoint (:meth:`_send`) and the token is
  **never** logged.
* One JSON object per line; a request line is capped at **1 MiB** — larger
  ``process.stdin`` payloads must be chunked (handled in :meth:`stdin`).
* The connection is persistent and requests are dispatched concurrently, so
  responses may arrive out of order — they are matched by ``id``.
* Stream frames are ``{"type":"stream","processId","stream":"stdout|stderr|exit",
  "seq","data":"<base64>","exitCode"}``; ``seq`` is per-process, monotonic across
  the three streams, starting at 1. ``data`` is base64; an stdout/stderr frame
  carries at most one 32 KiB read, so lines split across frames and a client
  reassembles by concatenating ``data`` in ``seq`` order (per stream).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# A request line is capped at 1 MiB by the daemon (bufio max token). The reader's
# buffer must be able to hold a max-size line (request or large stream frame), so
# match that ceiling rather than asyncio's 64 KiB default.
_MAX_LINE_BYTES = 1024 * 1024

# base64 inflates bytes 4/3, and the JSON envelope adds a little; chunk raw stdin
# well under the 1 MiB cap so each frame's encoded line stays serveable.
_STDIN_CHUNK_BYTES = 700_000

# Per-subscriber queue depth before a slow viewer starts dropping (with a marker
# rather than blocking the single socket reader).
_DEFAULT_QUEUE_MAXSIZE = 2048

# Unauthorized; surfaced as AuthRejected rather than a generic RpcError.
_AUTH_ERROR_CODE = -32001


class ClaustrumError(Exception):
    """Base class for every claustrum-client failure."""


class DaemonUnreachable(ClaustrumError):
    """The daemon socket could not be dialed, or the connection dropped."""


class AuthRejected(ClaustrumError):
    """The daemon rejected the request's auth token (``-32001``)."""


class RpcError(ClaustrumError):
    """The daemon returned a JSON-RPC ``error`` frame for a request."""

    def __init__(self, code: int, message: str) -> None:
        """Store the daemon's error ``code`` and ``message``."""
        super().__init__(f"claustrum rpc error {code}: {message}")
        self.code = code
        self.message = message


@dataclass
class _Subscriber:
    """One watcher's bounded queue plus a never-block overflow accumulator.

    The socket reader must never stall on a slow viewer, so a full queue drops
    the event and counts it; the next event the queue *can* take is preceded by
    an overflow marker carrying the dropped count, so gaps are honest. The marker
    ``type`` is parameterized (``overflow_type``) because consumers distinguish
    the channels: the claustrum client uses ``"overflow"`` and the hosted channel
    uses ``"gap"``.
    """

    queue: asyncio.Queue[dict[str, Any]]
    dropped: int = 0
    overflow_type: str = "overflow"

    def offer(self, event: dict[str, Any]) -> None:
        """Enqueue ``event`` for this watcher, never blocking the caller."""
        if self.dropped:
            marker = {"type": self.overflow_type, "dropped": self.dropped}
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


class ProcessStream:
    """Demux + line-reassembly + fan-out for one daemon process's stream frames.

    Frames are fed in by the client's reader (:meth:`feed`); they decode the
    base64 ``data``, reassemble newline-delimited lines per stream channel
    (stdout/stderr buffered separately so their bytes never interleave), and
    fan each completed line out to every subscriber. The ``exit`` frame latches
    :attr:`exited` and records :attr:`exit_code`.
    """

    def __init__(self, process_id: str, *, queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> None:
        """Create an empty stream for ``process_id``."""
        self.process_id = process_id
        self.exit_code: int | None = None
        self.exited = asyncio.Event()
        self.last_seq = 0
        self._queue_maxsize = queue_maxsize
        # Subscriber list: the DEPTH-bound (per-subscriber queue) is _queue_maxsize; the
        # BREADTH-bound (list length) is the number of live attachers to this process —
        # in practice one pump per HostedSession plus any reattach overlap — each dropped
        # by unsubscribe() on teardown, so it is bounded by live attachers, not unbounded.
        self._subscribers: list[_Subscriber] = []
        # Per-channel byte buffers for line re-assembly across split frames.
        self._buffers: dict[str, bytes] = {"stdout": b"", "stderr": b""}

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a watcher and return its private bounded event queue."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._subscribers.append(_Subscriber(queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Drop a watcher's queue (called when its consumer goes away)."""
        self._subscribers = [s for s in self._subscribers if s.queue is not queue]

    def _broadcast(self, event: dict[str, Any]) -> None:
        for sub in self._subscribers:
            sub.offer(event)

    def feed(self, frame: dict[str, Any]) -> None:
        """Route one ``type:"stream"`` ``frame`` into lines + the exit latch.

        Out-of-order or duplicate frames (``seq`` <= the highest already seen)
        are ignored so a replay overlap after reattach can't double-emit.
        """
        seq = frame.get("seq")
        if not isinstance(seq, int) or seq <= self.last_seq:
            return
        self.last_seq = seq
        stream = frame.get("stream")
        if stream == "exit":
            # Flush any unterminated partial line on each channel first, so a
            # final write without a trailing newline (common for CLI tools) is
            # delivered before the terminal exit event rather than lost.
            for channel in ("stdout", "stderr"):
                leftover = self._buffers[channel]
                if leftover:
                    self._buffers[channel] = b""
                    self._emit_line(channel, seq, leftover)
            code = frame.get("exitCode")
            self.exit_code = code if isinstance(code, int) else None
            self.exited.set()
            self._broadcast({"type": "exit", "seq": seq, "exit_code": self.exit_code})
            return
        if stream not in ("stdout", "stderr"):
            return  # unknown stream kind — ignore rather than guess
        chunk = self._decode(frame.get("data"))
        if chunk is None:
            return
        buf = self._buffers[stream] + chunk
        *lines, rest = buf.split(b"\n")
        self._buffers[stream] = rest
        for line in lines:
            self._emit_line(stream, seq, line)

    def _emit_line(self, stream: str, seq: int, line: bytes) -> None:
        self._broadcast(
            {"type": "line", "stream": stream, "seq": seq, "line": line.decode("utf-8", "replace")}
        )

    @staticmethod
    def _decode(data: object) -> bytes | None:
        if not isinstance(data, str):
            return None
        try:
            return base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            logger.warning("claustrum: dropping stream frame with undecodable base64")
            return None


class ClaustrumClient:
    """One persistent NDJSON JSON-RPC connection to a claustrum daemon.

    Construct with the socket path and auth token, then :meth:`connect`; issue
    RPCs (:meth:`ping`, :meth:`spawn`, :meth:`stdin`, :meth:`kill`,
    :meth:`reattach`, …) and read each spawned process's output via
    :meth:`stream`. :meth:`close` cancels the reader and drops the connection.
    Usable as an async context manager.
    """

    def __init__(
        self,
        socket_path: str,
        token: str,
        *,
        request_timeout: float = 30.0,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        """Store connection params; no socket is dialed until :meth:`connect`."""
        self._socket_path = socket_path
        self._token = token
        self._request_timeout = request_timeout
        self._queue_maxsize = queue_maxsize
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._streams: dict[str, ProcessStream] = {}
        self._closed = False

    async def __aenter__(self) -> ClaustrumClient:
        """Connect and return self for ``async with`` use."""
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Close the connection on context exit."""
        await self.close()

    async def connect(self) -> None:
        """Dial the daemon socket and start the background reader task."""
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                self._socket_path, limit=_MAX_LINE_BYTES
            )
        except (OSError, TimeoutError) as exc:
            raise DaemonUnreachable(f"cannot connect to claustrum at {self._socket_path}") from exc
        self._closed = False
        self._reader_task = asyncio.ensure_future(self._read_loop())

    async def close(self) -> None:
        """Cancel the reader, close the socket, and fail any pending requests."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (OSError, TimeoutError):
                pass
            self._writer = None
        self._reader = None
        self._fail_pending(DaemonUnreachable("connection closed"))

    # -- streams -----------------------------------------------------------

    def stream(self, process_id: str) -> ProcessStream:
        """Return (creating if needed) the :class:`ProcessStream` for a process."""
        stream = self._streams.get(process_id)
        if stream is None:
            stream = ProcessStream(process_id, queue_maxsize=self._queue_maxsize)
            self._streams[process_id] = stream
        return stream

    # -- RPC methods -------------------------------------------------------

    async def ping(self) -> dict[str, Any]:
        """Call ``server.ping`` → ``{"pong":true}``."""
        return await self.call("server.ping", None)

    async def version(self) -> dict[str, Any]:
        """Call ``server.version`` → ``{"version","platform","arch"}``."""
        return await self.call("server.version", None)

    async def capabilities(self) -> dict[str, Any]:
        """Call ``server.capabilities`` → ``{"version","methods":[…]}``."""
        return await self.call("server.capabilities", None)

    async def spawn(
        self,
        process_id: str,
        command: str,
        *,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        want_pid: bool = False,
    ) -> dict[str, Any]:
        """Call ``process.spawn``; stream frames then arrive on :meth:`stream`.

        ``want_pid`` is the CT-1 opt-in param: a CT-1+ daemon returns ``pid`` +
        ``startTime`` in the result, an older one ignores the unknown param and
        returns the bare ``{"success":true}`` — the caller treats the absence as
        "old daemon".
        """
        params: dict[str, Any] = {"id": process_id, "command": command}
        if args is not None:
            params["args"] = args
        if cwd is not None:
            params["cwd"] = cwd
        if env is not None:
            params["env"] = env
        if want_pid:
            params["wantPid"] = True
        # Ensure the stream exists before output can race in on the reader.
        self.stream(process_id)
        return await self.call("process.spawn", params)

    async def stdin(self, process_id: str, data: bytes) -> dict[str, Any]:
        """Write ``data`` to a process's stdin, chunked under the 1 MiB line cap.

        The payload is split into raw chunks small enough that each base64-encoded
        ``process.stdin`` request line stays well under the daemon's cap; every
        chunk is sent in order and the last response is returned.
        """
        result: dict[str, Any] = {"success": True}
        for start in range(0, max(len(data), 1), _STDIN_CHUNK_BYTES):
            chunk = data[start : start + _STDIN_CHUNK_BYTES]
            encoded = base64.b64encode(chunk).decode("ascii")
            result = await self.call("process.stdin", {"id": process_id, "data": encoded})
        return result

    async def kill(self, process_id: str, *, signal: str | None = None) -> dict[str, Any]:
        """Call ``process.kill`` (best-effort; tears down the child tree)."""
        params: dict[str, Any] = {"id": process_id}
        if signal is not None:
            params["signal"] = signal
        return await self.call("process.kill", params)

    async def reattach(self, process_id: str, from_seq: int = 0) -> dict[str, Any]:
        """Call ``process.reattach`` → ``{found,running,firstSeq,lastSeq}``.

        Buffered frames with ``seq > from_seq`` replay onto this connection and
        feed the process's :class:`ProcessStream` (duplicates are de-duped by the
        stream's monotonic ``seq`` guard). The mechanism CL-6 uses on restart.
        """
        self.stream(process_id)
        return await self.call("process.reattach", {"id": process_id, "fromSeq": from_seq})

    async def shutdown(self) -> None:
        """Send ``server.shutdown`` (the daemon stops; there is no response)."""
        await self._send("server.shutdown", None, request_id=self._allocate_id())

    # -- core --------------------------------------------------------------

    async def call(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """Send a request and await its correlated ``result`` (or raise).

        Raises :class:`AuthRejected` on ``-32001``, :class:`RpcError` on any
        other error frame, and :class:`DaemonUnreachable` if the connection is
        gone or the response times out.
        """
        request_id = self._allocate_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._send(method, params, request_id=request_id)
            return await asyncio.wait_for(future, self._request_timeout)
        except TimeoutError as exc:
            raise DaemonUnreachable(f"claustrum request {method} timed out") from exc
        finally:
            self._pending.pop(request_id, None)

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(self, method: str, params: dict[str, Any] | None, *, request_id: int) -> None:
        """Serialize one request with auth injected, and write it as a line.

        This is the single chokepoint where the auth token is attached — and it
        is never logged: failures mention only the method.
        """
        if self._writer is None or self._closed:
            raise DaemonUnreachable("claustrum connection is not open")
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        message["auth"] = self._token
        line = json.dumps(message, separators=(",", ":")) + "\n"
        try:
            self._writer.write(line.encode("utf-8"))
            await self._writer.drain()
        except (OSError, ConnectionError) as exc:
            raise DaemonUnreachable(f"claustrum write failed for {method}") from exc

    async def _read_loop(self) -> None:
        """Read frames until EOF, dispatching responses and stream notifications."""
        reader = self._reader
        if reader is None:  # pragma: no cover - reader is set before the task starts
            return
        try:
            while True:
                try:
                    raw = await reader.readline()
                except ValueError:
                    # A frame larger than the StreamReader's _MAX_LINE_BYTES limit makes
                    # readline() raise ValueError (it wraps asyncio's LimitOverrunError). A
                    # conforming daemon never sends one — every line is well under the 1 MiB
                    # cap — so treat it as a fatal protocol violation and stop the reader
                    # cleanly here. Otherwise the ValueError escapes this task as a
                    # never-retrieved exception. The finally below fails pending calls;
                    # future calls then time out and the daemon health probe reconnects.
                    logger.warning(
                        "claustrum: frame exceeded the %d-byte line limit; closing reader",
                        _MAX_LINE_BYTES,
                    )
                    break
                if not raw:
                    break
                # Fault-isolate per-frame dispatch: one malformed frame (or a bug in stream
                # feed / response resolution) must never kill the reader task — that would
                # silently stall every pending call and stop all hosted stream fan-out, with
                # no restart. CancelledError is a BaseException, so it still propagates past
                # `except Exception`; a genuine connection error from readline() above does
                # too (and tears down via _fail_pending).
                try:
                    self._dispatch(raw)
                except Exception:
                    logger.warning("claustrum: error dispatching frame; skipping", exc_info=True)
        except (asyncio.CancelledError, OSError):
            raise
        finally:
            if not self._closed:
                self._fail_pending(DaemonUnreachable("claustrum connection lost"))

    def _dispatch(self, raw: bytes) -> None:
        try:
            frame = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            logger.warning("claustrum: dropping unparseable frame")
            return
        if not isinstance(frame, dict):
            return
        if frame.get("type") == "stream":
            process_id = frame.get("processId")
            if isinstance(process_id, str):
                self.stream(process_id).feed(frame)
            return
        request_id = frame.get("id")
        if isinstance(request_id, int):
            self._resolve(request_id, frame)

    def _resolve(self, request_id: int, frame: dict[str, Any]) -> None:
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        error = frame.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = str(error.get("message", ""))
            if code == _AUTH_ERROR_CODE:
                future.set_exception(AuthRejected(message))
            else:
                future.set_exception(RpcError(int(code) if isinstance(code, int) else 0, message))
            return
        result = frame.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)
