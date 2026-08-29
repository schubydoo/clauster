"""Tests for the async claustrum NDJSON JSON-RPC client (CL-1).

Driven against the in-process ``fake_claustrum`` daemon fixture: auth gating,
the server/process methods, per-process ``seq`` stream demux with line
re-assembly across split frames, replay-on-reattach, slow-subscriber overflow,
stdin chunking under the 1 MiB cap, and mid-stream disconnect.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys

import pytest

from clauster.claustrum_client import (
    _STDIN_CHUNK_BYTES,
    AuthRejected,
    ClaustrumClient,
    DaemonUnreachable,
    ProcessStream,
    RpcError,
)
from conftest import wait_until

# On Windows the fake daemon serves a named pipe (advertised via rpc.pipe) and
# the client dials it, so these run there too — same transport the real daemon
# uses. A single frame larger than the pipe's message buffer can't cross it,
# though, so the few big-payload cases below carry their own win32 skip.
_WIN_MSG_PIPE_CAP = pytest.mark.skipif(
    sys.platform == "win32",
    reason="frame exceeds the Windows message-mode pipe buffer (fake-fixture limit)",
)


async def _drain(queue: asyncio.Queue, *, timeout: float = 1.0) -> dict:
    return await asyncio.wait_for(queue.get(), timeout=timeout)


def _frame(stream: str, seq: int, data: bytes) -> dict:
    """Build a base64-encoded stdout/stderr stream frame for ProcessStream.feed."""
    return {
        "type": "stream",
        "stream": stream,
        "seq": seq,
        "data": base64.b64encode(data).decode(),
    }


async def test_server_methods_roundtrip(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        assert await client.ping() == {"pong": True}
        caps = await client.capabilities()
        assert caps["version"] == fake.version
        assert len(caps["methods"]) == 18
        assert "process.spawn" in caps["methods"]
        assert "process.killAndWait" in caps["methods"]
        # server.version was removed in claustrum v1.10 — no longer advertised or served.
        assert "server.version" not in caps["methods"]
        with pytest.raises(RpcError) as excinfo:
            await client.call("server.version", None)
        assert excinfo.value.code == -32601


async def test_capabilities_against_legacy_daemon(fake_claustrum):
    """A pre-v1.10 daemon still advertising server.version works via capabilities."""
    fake = await fake_claustrum(legacy_version=True)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        caps = await client.capabilities()
        assert caps["version"] == fake.version
        # The legacy daemon advertises and still serves the extra method...
        assert len(caps["methods"]) == 19
        assert "server.version" in caps["methods"]
        legacy = await client.call("server.version", None)
        assert legacy["version"] == fake.version
        # ...but clauster reads its version from capabilities either way.


async def test_auth_rejected(fake_claustrum):
    fake = await fake_claustrum(token="right")
    async with ClaustrumClient(fake.socket_path, "wrong") as client:
        with pytest.raises(AuthRejected):
            await client.ping()


async def test_unknown_method_is_rpc_error(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        with pytest.raises(RpcError) as excinfo:
            await client.call("bogus.method", {})
        assert excinfo.value.code == -32601


async def test_connect_unreachable_raises():
    client = ClaustrumClient("/nonexistent/claustrum.sock", "tok")
    with pytest.raises(DaemonUnreachable):
        await client.connect()


async def test_connect_wraps_oserror_as_daemon_unreachable(monkeypatch):
    # claustrum_client.py 333-334: an OSError dialing the daemon is wrapped as
    # DaemonUnreachable (fail-closed). Monkeypatch the connection opener to raise OSError
    # so this runs cross-platform, independent of the POSIX-socket vs Windows-pipe branch.
    from clauster import claustrum_client as cc

    async def boom(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(cc, "_open_claustrum_connection", boom)
    client = ClaustrumClient("/some/claustrum.sock", "tok")
    with pytest.raises(DaemonUnreachable):
        await client.connect()


# ----- transport selection: AF_UNIX socket vs Windows named pipe (#891) --------
# claustrum #134 adds an opt-in Windows named-pipe transport, discovered via an
# `rpc.pipe` file beside the socket. These exercise the client's platform branch on
# Linux (mocked); the real `create_pipe_connection` dial is validated on the Windows VM.


def test_read_pipe_name_reads_and_strips(tmp_path):
    """`_read_pipe_name` returns the trimmed rpc.pipe contents beside the socket."""
    from clauster import claustrum_client as cc

    pipe = r"\\.\pipe\claustrum-deadbeef"
    (tmp_path / "rpc.pipe").write_text(pipe + "\n", encoding="utf-8")
    assert cc._read_pipe_name(str(tmp_path / "rpc.sock")) == pipe


def test_read_pipe_name_absent_or_empty_is_none(tmp_path):
    """An absent or whitespace-only rpc.pipe yields None (nothing to dial)."""
    from clauster import claustrum_client as cc

    sock = str(tmp_path / "rpc.sock")
    assert cc._read_pipe_name(sock) is None  # absent
    (tmp_path / "rpc.pipe").write_text("   \n", encoding="utf-8")
    assert cc._read_pipe_name(sock) is None  # empty after strip


async def test_open_connection_posix_dials_unix_socket(monkeypatch, tmp_path):
    """On POSIX, `_open_claustrum_connection` dials the AF_UNIX socket."""
    from clauster import claustrum_client as cc

    monkeypatch.setattr(cc.sys, "platform", "linux")
    seen = {}

    async def fake_unix(path, *, limit):
        seen.update(path=path, limit=limit)
        return ("R", "W")

    # raising=False: asyncio has no open_unix_connection attribute on Windows, but the
    # monkeypatched platform="linux" still routes through it — inject the mock either way.
    monkeypatch.setattr(cc.asyncio, "open_unix_connection", fake_unix, raising=False)
    sock = str(tmp_path / "rpc.sock")
    assert await cc._open_claustrum_connection(sock, limit=123) == ("R", "W")
    assert seen == {"path": sock, "limit": 123}


async def test_open_connection_windows_dials_named_pipe(monkeypatch, tmp_path):
    """On win32, it discovers the pipe via rpc.pipe and dials it — never the socket."""
    from clauster import claustrum_client as cc

    monkeypatch.setattr(cc.sys, "platform", "win32")
    pipe = r"\\.\pipe\claustrum-abc"
    (tmp_path / "rpc.pipe").write_text(pipe + "\n", encoding="utf-8")
    seen = {}

    async def fake_pipe(name, *, limit):
        seen.update(name=name, limit=limit)
        return ("PR", "PW")

    async def boom_unix(*_a, **_k):
        raise AssertionError("AF_UNIX socket dialed on Windows")

    monkeypatch.setattr(cc, "_open_windows_pipe_connection", fake_pipe)
    # raising=False: no open_unix_connection on Windows; the win32 branch must never
    # reach it, and this guard asserts exactly that on both platforms.
    monkeypatch.setattr(cc.asyncio, "open_unix_connection", boom_unix, raising=False)
    assert await cc._open_claustrum_connection(str(tmp_path / "rpc.sock"), limit=99) == (
        "PR",
        "PW",
    )
    assert seen == {"name": pipe, "limit": 99}


async def test_open_connection_windows_without_pipe_raises(monkeypatch, tmp_path):
    """On win32 with no rpc.pipe, connecting fails closed with DaemonUnreachable."""
    from clauster import claustrum_client as cc

    monkeypatch.setattr(cc.sys, "platform", "win32")
    with pytest.raises(cc.DaemonUnreachable, match="named pipe unavailable"):
        await cc._open_claustrum_connection(str(tmp_path / "rpc.sock"), limit=1)


async def test_open_connection_windows_selector_loop_raises_daemon_unreachable(
    monkeypatch, tmp_path
):
    """A loop without create_pipe_connection surfaces as DaemonUnreachable, not AttributeError."""
    from clauster import claustrum_client as cc

    monkeypatch.setattr(cc.sys, "platform", "win32")
    (tmp_path / "rpc.pipe").write_text(r"\\.\pipe\claustrum-x", encoding="utf-8")

    async def no_pipe_support(*_a, **_k):  # what a non-Proactor loop does: no such method
        raise AttributeError(
            "'_WindowsSelectorEventLoop' object has no attribute 'create_pipe_connection'"
        )

    monkeypatch.setattr(cc, "_open_windows_pipe_connection", no_pipe_support)
    with pytest.raises(cc.DaemonUnreachable, match="no named-pipe support"):
        await cc._open_claustrum_connection(str(tmp_path / "rpc.sock"), limit=1)


def test_read_pipe_name_propagates_read_error(monkeypatch, tmp_path):
    """A read failure on an EXISTING rpc.pipe propagates — it doesn't masquerade as 'no pipe'."""
    from clauster import claustrum_client as cc

    (tmp_path / "rpc.pipe").write_text("whatever", encoding="utf-8")

    def boom(*_a, **_k):
        raise PermissionError("locked")

    monkeypatch.setattr("pathlib.Path.read_text", boom)
    with pytest.raises(PermissionError):
        cc._read_pipe_name(str(tmp_path / "rpc.sock"))


async def test_open_windows_pipe_connection_wires_reader_writer(monkeypatch):
    """The real Windows pipe helper builds a working reader/writer over create_pipe_connection.

    The ONLY win32-specific dependency is ``loop.create_pipe_connection``; inject a fake one onto
    the running (POSIX) loop and drive the REAL helper unpatched — asserting the StreamReader /
    StreamWriter pair actually reads, writes, and honors the caller's limit.
    """
    from clauster import claustrum_client as cc

    seen: dict = {}

    class _FakePipeTransport(asyncio.Transport):
        # Subclasses asyncio.Transport so pause_reading/resume_reading raise NotImplementedError
        # (the flow-control contract StreamReader expects), mirroring a real one-way pipe read.
        def __init__(self):
            super().__init__()
            self.writes: list[bytes] = []
            self._closing = False

        def write(self, data):
            self.writes.append(bytes(data))

        def is_closing(self):
            return self._closing

        def close(self):
            self._closing = True

        def get_extra_info(self, name, default=None):
            return default

    async def fake_create_pipe_connection(protocol_factory, name):
        seen["name"] = name
        proto = protocol_factory()
        transport = _FakePipeTransport()
        proto.connection_made(transport)
        seen["transport"] = transport
        return transport, proto

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "create_pipe_connection", fake_create_pipe_connection, raising=False)

    pipe = r"\\.\pipe\claustrum-wire"
    reader, writer = await cc._open_windows_pipe_connection(pipe, limit=16)

    assert isinstance(reader, asyncio.StreamReader)
    assert isinstance(writer, asyncio.StreamWriter)
    assert seen["name"] == pipe  # dialed the exact pipe name

    # The writer really wraps the injected transport.
    writer.write(b"ping\n")
    assert seen["transport"].writes == [b"ping\n"]

    # The reader really receives bytes over the same pipe.
    reader.feed_data(b"pong\n")
    assert await reader.readline() == b"pong\n"

    # The StreamReader was built with the caller's limit: a line past it raises (limit honored).
    reader.feed_data(b"x" * 64)
    with pytest.raises(ValueError, match="chunk exceed the limit"):
        await reader.readline()


async def test_read_loop_survives_oversized_frame(caplog):
    # A frame larger than _MAX_LINE_BYTES makes readline() raise ValueError (wrapping
    # asyncio's LimitOverrunError). The reader must tear down cleanly via _fail_pending —
    # not die as a never-retrieved task exception — so pending calls fail and the daemon
    # health probe can reconnect. (A tiny StreamReader limit exercises the same readline
    # ValueError path as a real >1 MiB frame, without allocating one.)
    client = ClaustrumClient("/unused.sock", "tok")
    reader = asyncio.StreamReader(limit=64)
    client._reader = reader
    fut = asyncio.get_running_loop().create_future()
    client._pending[1] = fut
    reader.feed_data(b"x" * 1000)  # > limit, no newline → readline raises ValueError
    reader.feed_eof()
    with caplog.at_level(logging.WARNING, logger="clauster.claustrum_client"):
        await client._read_loop()  # returns cleanly; no exception escapes the task
    assert "exceeded the" in caplog.text
    assert fut.done()
    with pytest.raises(DaemonUnreachable):
        fut.result()


async def test_call_before_connect_raises(fake_claustrum):
    client = ClaustrumClient("/unused.sock", "tok")
    with pytest.raises(DaemonUnreachable):
        await client.ping()


async def test_spawn_then_stream_lines(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        result = await client.spawn("p1", "claude", args=["--print"])
        assert result == {"success": True}
        assert fake.spawned[0]["command"] == "claude"
        assert fake.spawned[0]["args"] == ["--print"]

        queue = client.stream("p1").subscribe()
        await fake.emit("p1", "stdout", b'{"type":"system"}\n')
        event = await _drain(queue)
        assert event == {"type": "line", "stream": "stdout", "seq": 1, "line": '{"type":"system"}'}


async def test_line_reassembly_across_split_frames(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        await client.spawn("p1", "claude")
        queue = client.stream("p1").subscribe()
        # A single logical line delivered across three frames; only the newline
        # in the last frame completes it.
        await fake.emit("p1", "stdout", b'{"part":')
        await fake.emit("p1", "stdout", b'"one two')
        await fake.emit("p1", "stdout", b' three"}\nleftover')
        event = await _drain(queue)
        assert event["line"] == '{"part":"one two three"}'
        assert event["seq"] == 3
        # "leftover" has no newline yet -> still buffered, nothing more delivered.
        with pytest.raises(asyncio.TimeoutError):
            await _drain(queue, timeout=0.2)


async def test_stdout_stderr_buffers_do_not_interleave(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        await client.spawn("p1", "claude")
        queue = client.stream("p1").subscribe()
        await fake.emit("p1", "stdout", b"OUT-")
        await fake.emit("p1", "stderr", b"ERR\n")
        await fake.emit("p1", "stdout", b"LINE\n")
        first = await _drain(queue)
        second = await _drain(queue)
        assert first == {"type": "line", "stream": "stderr", "seq": 2, "line": "ERR"}
        assert second == {"type": "line", "stream": "stdout", "seq": 3, "line": "OUT-LINE"}


async def test_exit_latch_and_event(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        await client.spawn("p1", "claude")
        stream = client.stream("p1")
        queue = stream.subscribe()
        await fake.emit_exit("p1", 0)
        event = await _drain(queue)
        assert event == {"type": "exit", "seq": 1, "exit_code": 0}
        await asyncio.wait_for(stream.exited.wait(), timeout=1)
        assert stream.exit_code == 0


async def test_partial_line_flushed_on_exit():
    stream = ProcessStream("p1")
    queue = stream.subscribe()
    # A final write with no trailing newline, then exit -> the partial line must
    # be flushed before the terminal exit event rather than swallowed.
    stream.feed(_frame("stdout", 1, b"no newline here"))
    stream.feed(_frame("stderr", 2, b"warn tail"))
    stream.feed({"type": "stream", "stream": "exit", "seq": 3, "exitCode": 1})
    flushed_out = await _drain(queue)
    flushed_err = await _drain(queue)
    exit_event = await _drain(queue)
    assert flushed_out == {"type": "line", "stream": "stdout", "seq": 3, "line": "no newline here"}
    assert flushed_err == {"type": "line", "stream": "stderr", "seq": 3, "line": "warn tail"}
    assert exit_event == {"type": "exit", "seq": 3, "exit_code": 1}


async def test_duplicate_and_stale_seq_ignored():
    stream = ProcessStream("p1")
    queue = stream.subscribe()
    stream.feed(_frame("stdout", 5, b"a\n"))
    # A frame with seq <= last_seq (replay overlap) must not re-emit.
    stream.feed(_frame("stdout", 5, b"b\n"))
    stream.feed(_frame("stdout", 3, b"c\n"))
    event = await _drain(queue)
    assert event["line"] == "a"
    assert stream.last_seq == 5
    with pytest.raises(asyncio.TimeoutError):
        await _drain(queue, timeout=0.2)


async def test_bad_base64_frame_dropped():
    stream = ProcessStream("p1")
    queue = stream.subscribe()
    stream.feed({"type": "stream", "stream": "stdout", "seq": 1, "data": "!!not base64!!"})
    assert stream.last_seq == 1  # seq advances (frame consumed) but nothing emitted
    with pytest.raises(asyncio.TimeoutError):
        await _drain(queue, timeout=0.2)


async def test_overflow_marker_for_slow_subscriber():
    stream = ProcessStream("p1", queue_maxsize=2)
    queue = stream.subscribe()
    for seq in range(1, 6):  # 5 lines into a depth-2 queue, never drained
        stream.feed(_frame("stdout", seq, b"x\n"))
    # First two lines fit; the rest are dropped and accounted.
    a = await _drain(queue)
    b = await _drain(queue)
    assert a["line"] == "x" and b["line"] == "x"
    # Now there's room; the next real feed is preceded by an overflow marker.
    stream.feed(_frame("stdout", 6, b"y\n"))
    marker = await _drain(queue)
    assert marker == {"type": "overflow", "dropped": 3}


async def test_unsubscribe_stops_delivery():
    stream = ProcessStream("p1")
    queue = stream.subscribe()
    stream.unsubscribe(queue)
    stream.feed(_frame("stdout", 1, b"a\n"))
    with pytest.raises(asyncio.TimeoutError):
        await _drain(queue, timeout=0.2)


async def test_stdin_roundtrip(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        await client.spawn("p1", "claude")
        payload = b"hello\n"
        await client.stdin("p1", payload)
        assert fake.stdin_received["p1"] == payload


@_WIN_MSG_PIPE_CAP  # each 700 KiB chunk's base64 line exceeds the message-pipe buffer
async def test_stdin_large_payload_chunks(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        await client.spawn("p1", "claude")
        big = b"Z" * (_STDIN_CHUNK_BYTES * 2 + 17)
        await client.stdin("p1", big)
        assert fake.stdin_received["p1"] == big


async def test_stdin_unknown_process_errors(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        with pytest.raises(RpcError) as excinfo:
            await client.stdin("ghost", b"x")
        assert excinfo.value.code == -32602


async def test_kill_marks_not_running(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        await client.spawn("p1", "claude")
        assert await client.kill("p1", signal="SIGINT") == {"success": True}
        assert fake.killed[0] == {"id": "p1", "signal": "SIGINT"}
        with pytest.raises(RpcError):  # stdin to a killed process -> not running
            await client.stdin("p1", b"x")


async def test_reattach_replays_from_seq(fake_claustrum):
    fake = await fake_claustrum()
    # First client spawns and produces output, then disconnects.
    client_a = ClaustrumClient(fake.socket_path, fake.token)
    await client_a.connect()
    await client_a.spawn("p1", "claude")
    await fake.emit("p1", "stdout", b"first\n")
    await fake.emit("p1", "stdout", b"second\n")
    await client_a.close()

    # A fresh client reattaches from seq 1 -> replays seq 2 onward.
    async with ClaustrumClient(fake.socket_path, fake.token) as client_b:
        queue = client_b.stream("p1").subscribe()
        result = await client_b.reattach("p1", from_seq=1)
        assert result == {"found": True, "running": True, "firstSeq": 1, "lastSeq": 2}
        event = await _drain(queue)
        assert event["line"] == "second" and event["seq"] == 2


async def test_reattach_unknown_process(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        result = await client.reattach("ghost", from_seq=0)
        assert result == {"found": False, "running": False, "firstSeq": 0, "lastSeq": 0}


async def test_want_pid_opt_in(fake_claustrum):
    fake = await fake_claustrum(support_want_pid=True)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        with_pid = await client.spawn("p1", "claude", want_pid=True)
        assert with_pid["pid"] == 4242 and "startTime" in with_pid
        assert fake.spawned[0]["wantPid"] is True
        # Without the opt-in, the bare success result comes back (old-daemon shape).
        without = await client.spawn("p2", "claude")
        assert without == {"success": True}


async def test_old_daemon_ignores_want_pid(fake_claustrum):
    fake = await fake_claustrum(support_want_pid=False)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        result = await client.spawn("p1", "claude", want_pid=True)
        assert result == {"success": True}  # absence of pid -> treat as old daemon


async def test_disconnect_fails_pending_and_close_is_idempotent(fake_claustrum):
    fake = await fake_claustrum()
    client = ClaustrumClient(fake.socket_path, fake.token)
    await client.connect()
    await client.spawn("p1", "claude")

    # Daemon drops the connection mid-stream -> the reader sees EOF.
    fake.disconnect_all()
    await wait_until(lambda: client._reader_task is not None and client._reader_task.done())
    with pytest.raises(DaemonUnreachable):
        await client.ping()
    await client.close()
    await client.close()  # idempotent


async def test_shutdown_sends_without_awaiting_response(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        await client.ping()
        await client.shutdown()  # no response frame; must not hang


async def test_spawn_with_cwd_and_env(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        await client.spawn("p1", "claude", cwd="/work", env={"K": "V"})
        assert fake.spawned[0]["cwd"] == "/work"
        assert fake.spawned[0]["env"] == {"K": "V"}


async def test_kill_without_signal(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        await client.spawn("p1", "claude")
        await client.kill("p1")
        assert fake.killed[0] == {"id": "p1"}


async def test_request_timeout(fake_claustrum):
    fake = await fake_claustrum()
    fake.hang_methods.add("server.ping")
    async with ClaustrumClient(fake.socket_path, fake.token, request_timeout=0.1) as client:
        with pytest.raises(DaemonUnreachable):
            await client.ping()


async def test_unknown_stream_kind_and_nonstr_data_ignored():
    stream = ProcessStream("p1")
    queue = stream.subscribe()
    stream.feed(_frame("weird", 1, b"x\n"))
    stream.feed({"type": "stream", "stream": "stdout", "seq": 2, "data": None})
    assert stream.last_seq == 2  # both frames consumed, neither emitted a line
    with pytest.raises(asyncio.TimeoutError):
        await _drain(queue, timeout=0.2)


def test_dispatch_ignores_garbage_and_nondict_and_bad_ids():
    client = ClaustrumClient("/unused.sock", "tok")
    # None of these should raise; all are silently ignored.
    client._dispatch(b"not json at all\n")
    client._dispatch(b"123\n")  # valid JSON, not a dict
    client._dispatch(b'{"type":"stream","processId":123,"seq":1,"stream":"stdout"}\n')  # bad id
    client._dispatch(b'{"id":"abc","result":{}}\n')  # non-int id, no matching request
    client._resolve(999, {"result": {}})  # unknown request id -> no-op


async def test_fail_pending_sets_exception():
    client = ClaustrumClient("/unused.sock", "tok")
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    already_done: asyncio.Future = loop.create_future()
    already_done.set_result({})  # an already-settled pending entry must be skipped
    client._pending[1] = future
    client._pending[2] = already_done
    client._fail_pending(DaemonUnreachable("boom"))
    with pytest.raises(DaemonUnreachable):
        await future
    assert already_done.result() == {}


async def test_read_loop_isolates_a_bad_frame(caplog):
    # A frame whose dispatch raises a NON-OSError (here: a stream feed raising ValueError)
    # must be logged and skipped, not kill the reader task. Otherwise the loop dies, no one
    # restarts it, and every subsequent call() hangs to its timeout while stream fan-out
    # stops silently. Assert the loop survives: a later response frame still resolves its
    # pending future, and the bad frame is logged.
    client = ClaustrumClient("/unused.sock", "tok")
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    client._pending[7] = fut

    class _RaisingStream:
        def feed(self, frame):
            raise ValueError("boom")

    client.stream = lambda pid: _RaisingStream()  # type: ignore[assignment]

    class _FakeReader:
        def __init__(self, frames):
            self._frames = list(frames)

        async def readline(self):
            return self._frames.pop(0) if self._frames else b""

    bad = json.dumps({"type": "stream", "processId": "p1", "seq": 1}).encode() + b"\n"
    good = json.dumps({"id": 7, "result": {"ok": True}}).encode() + b"\n"
    client._reader = _FakeReader([bad, good])  # type: ignore[assignment]
    client._closed = False

    with caplog.at_level(logging.WARNING):
        await client._read_loop()

    assert fut.done() and fut.result() == {"ok": True}  # loop survived the bad frame
    assert any("dispatching frame" in r.getMessage() for r in caplog.records)
