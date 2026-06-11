"""Scriptable in-process fake claustrum daemon for client tests.

The ``fake_claude`` pattern, but for the NDJSON JSON-RPC daemon protocol: an
``asyncio`` ``AF_UNIX`` server that speaks just enough of
``claustrum/docs/PROTOCOL.md`` to exercise :mod:`clauster.claustrum_client` —
auth gating, the ``server.*``/``process.*`` methods, per-process ``seq`` stream
frames with replay-on-reattach, and a hook to drop a connection mid-stream.

Tests drive output explicitly via :meth:`FakeClaustrum.emit` /
:meth:`FakeClaustrum.emit_exit` so line-splitting, ordering, and overflow are all
deterministic. CL-3 will seed canned scripts here from captured live frames.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any

# The 18 methods server.capabilities self-describes, in returned order.
_METHODS = [
    "server.ping",
    "server.version",
    "server.capabilities",
    "server.shutdown",
    "files.list",
    "files.validate",
    "files.stat",
    "files.read",
    "files.extract_tar",
    "git.info",
    "git.status",
    "git.list_branches",
    "git.worktree_create",
    "git.worktree_remove",
    "process.spawn",
    "process.stdin",
    "process.kill",
    "process.reattach",
]

# Match the daemon's 1 MiB line cap so large stdin chunks aren't truncated by
# asyncio's 64 KiB default StreamReader limit.
_MAX_LINE_BYTES = 1024 * 1024


@dataclass
class _Process:
    """One spawned process's replay buffer + the connections watching it."""

    seq: int = 0
    frames: list[dict[str, Any]] = field(default_factory=list)
    running: bool = True
    subscribers: list[asyncio.StreamWriter] = field(default_factory=list)


class FakeClaustrum:
    """A minimal, scriptable claustrum daemon over a unix socket."""

    def __init__(
        self,
        socket_path: str,
        token: str,
        *,
        version: str = "fake-claustrum-0",
        support_want_pid: bool = False,
    ) -> None:
        """Configure the fake; nothing listens until :meth:`start`."""
        self.socket_path = socket_path
        self.token = token
        self.version = version
        self.support_want_pid = support_want_pid
        # Introspection for assertions.
        self.spawned: list[dict[str, Any]] = []
        self.stdin_received: dict[str, bytes] = {}
        self.killed: list[dict[str, Any]] = []
        # Methods that receive no reply (to exercise client request timeouts).
        self.hang_methods: set[str] = set()
        self._processes: dict[str, _Process] = {}
        self._writers: set[asyncio.StreamWriter] = set()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        """Begin listening on the unix socket."""
        self._server = await asyncio.start_unix_server(
            self._handle, path=self.socket_path, limit=_MAX_LINE_BYTES
        )

    async def stop(self) -> None:
        """Close every connection and stop the server."""
        for writer in list(self._writers):
            self._safe_close(writer)
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # -- scripting ---------------------------------------------------------

    async def emit(self, process_id: str, stream: str, data: bytes) -> int:
        """Emit one stdout/stderr frame; buffer it and push to subscribers.

        Returns the assigned per-process ``seq``.
        """
        proc = self._processes.setdefault(process_id, _Process())
        proc.seq += 1
        frame = {
            "type": "stream",
            "processId": process_id,
            "stream": stream,
            "seq": proc.seq,
            "data": base64.b64encode(data).decode("ascii"),
        }
        proc.frames.append(frame)
        await self._push(proc, frame)
        return proc.seq

    async def emit_exit(self, process_id: str, exit_code: int) -> int:
        """Emit the terminal ``exit`` frame for a process."""
        proc = self._processes.setdefault(process_id, _Process())
        proc.seq += 1
        proc.running = False
        frame = {
            "type": "stream",
            "processId": process_id,
            "stream": "exit",
            "seq": proc.seq,
            "exitCode": exit_code,
        }
        proc.frames.append(frame)
        await self._push(proc, frame)
        return proc.seq

    def disconnect_all(self) -> None:
        """Drop every live connection (simulates a mid-stream daemon loss)."""
        for writer in list(self._writers):
            self._safe_close(writer)
        self._writers.clear()
        for proc in self._processes.values():
            proc.subscribers.clear()

    async def _push(self, proc: _Process, frame: dict[str, Any]) -> None:
        line = (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")
        for writer in list(proc.subscribers):
            if writer.is_closing():
                proc.subscribers.remove(writer)
                continue
            writer.write(line)
            try:
                await writer.drain()
            except (OSError, ConnectionError):
                proc.subscribers.remove(writer)

    # -- connection handling ----------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                await self._dispatch(raw, writer)
        except (asyncio.CancelledError, OSError):
            pass
        finally:
            self._writers.discard(writer)
            for proc in self._processes.values():
                if writer in proc.subscribers:
                    proc.subscribers.remove(writer)
            self._safe_close(writer)

    async def _dispatch(self, raw: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            req = json.loads(raw)
        except ValueError:
            await self._reply(writer, None, error=(-32700, "Parse error"))
            return
        request_id = req.get("id")
        if req.get("auth") != self.token:
            await self._reply(writer, request_id, error=(-32001, "Unauthorized: invalid or missing auth token"))
            return
        method = req.get("method")
        if method in self.hang_methods:
            return  # deliberately no reply
        params = req.get("params") or {}
        handler = getattr(self, f"_m_{method.replace('.', '_')}", None) if isinstance(method, str) else None
        if handler is None:
            if method == "server.shutdown":
                self._safe_close(writer)  # no response, connection closes
                return
            await self._reply(writer, request_id, error=(-32601, f"Unknown method: {method}"))
            return
        await handler(request_id, params, writer)

    # -- methods -----------------------------------------------------------

    async def _m_server_ping(self, request_id: Any, _params: dict, writer: asyncio.StreamWriter) -> None:
        await self._reply(writer, request_id, result={"pong": True})

    async def _m_server_version(self, request_id: Any, _params: dict, writer: asyncio.StreamWriter) -> None:
        await self._reply(
            writer, request_id, result={"version": self.version, "platform": "linux", "arch": "amd64"}
        )

    async def _m_server_capabilities(
        self, request_id: Any, _params: dict, writer: asyncio.StreamWriter
    ) -> None:
        await self._reply(writer, request_id, result={"version": self.version, "methods": list(_METHODS)})

    async def _m_process_spawn(
        self, request_id: Any, params: dict, writer: asyncio.StreamWriter
    ) -> None:
        process_id = params.get("id")
        if not isinstance(process_id, str):
            await self._reply(writer, request_id, error=(-32602, "Process ID is required"))
            return
        if not isinstance(params.get("command"), str):
            await self._reply(writer, request_id, error=(-32602, "Command is required"))
            return
        self.spawned.append(params)
        proc = self._processes.setdefault(process_id, _Process())
        proc.running = True
        if writer not in proc.subscribers:
            proc.subscribers.append(writer)
        result: dict[str, Any] = {"success": True}
        if self.support_want_pid and params.get("wantPid") is True:
            result["pid"] = 4242
            result["startTime"] = 1717000000
        await self._reply(writer, request_id, result=result)

    async def _m_process_stdin(
        self, request_id: Any, params: dict, writer: asyncio.StreamWriter
    ) -> None:
        data = params.get("data")
        try:
            decoded = base64.b64decode(data, validate=True) if isinstance(data, str) else None
        except (ValueError, TypeError):
            decoded = None
        if decoded is None:
            await self._reply(writer, request_id, error=(-32602, "Invalid base64 data"))
            return
        process_id = params.get("id")
        if not isinstance(process_id, str) or process_id not in self._processes:
            await self._reply(writer, request_id, error=(-32602, "Process not found"))
            return
        if not self._processes[process_id].running:
            await self._reply(writer, request_id, error=(-32602, "Process not running"))
            return
        self.stdin_received[process_id] = self.stdin_received.get(process_id, b"") + decoded
        await self._reply(writer, request_id, result={"success": True})

    async def _m_process_kill(
        self, request_id: Any, params: dict, writer: asyncio.StreamWriter
    ) -> None:
        self.killed.append(params)
        process_id = params.get("id")
        if isinstance(process_id, str) and process_id in self._processes:
            self._processes[process_id].running = False
        await self._reply(writer, request_id, result={"success": True})

    async def _m_process_reattach(
        self, request_id: Any, params: dict, writer: asyncio.StreamWriter
    ) -> None:
        process_id = params.get("id")
        from_seq = params.get("fromSeq", 0)
        if not isinstance(from_seq, int):
            from_seq = 0
        proc = self._processes.get(process_id) if isinstance(process_id, str) else None
        if proc is None:
            await self._reply(
                writer,
                request_id,
                result={"found": False, "running": False, "firstSeq": 0, "lastSeq": 0},
            )
            return
        replayed = [f for f in proc.frames if f["seq"] > from_seq]
        for frame in replayed:
            line = (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")
            writer.write(line)
        if writer not in proc.subscribers:
            proc.subscribers.append(writer)
        await writer.drain()
        first_seq = proc.frames[0]["seq"] if proc.frames else 0
        await self._reply(
            writer,
            request_id,
            result={
                "found": True,
                "running": proc.running,
                "firstSeq": first_seq,
                "lastSeq": proc.seq,
            },
        )

    # -- helpers -----------------------------------------------------------

    async def _reply(
        self,
        writer: asyncio.StreamWriter,
        request_id: Any,
        *,
        result: dict[str, Any] | None = None,
        error: tuple[int, str] | None = None,
    ) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            message["error"] = {"code": error[0], "message": error[1]}
        else:
            message["result"] = result if result is not None else {}
        if writer.is_closing():
            return
        writer.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        try:
            await writer.drain()
        except (OSError, ConnectionError):
            pass

    @staticmethod
    def _safe_close(writer: asyncio.StreamWriter) -> None:
        if not writer.is_closing():
            writer.close()
