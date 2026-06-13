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

# The claustrum client speaks JSON-RPC over an AF_UNIX socket; asyncio has no
# start_unix_server/open_unix_connection on Windows (same posture as the pty
# tests). The Windows CI cell runs with --cov-fail-under=0, so skipping here
# costs no coverage there; the gate is enforced on the Linux cell.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="claustrum client is POSIX-only (AF_UNIX)"
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
        version = await client.version()
        assert version["version"] == fake.version
        caps = await client.capabilities()
        assert len(caps["methods"]) == 18
        assert "process.spawn" in caps["methods"]


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


async def test_stdin_roundtrip_and_chunking(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        await client.spawn("p1", "claude")
        payload = b"hello\n"
        await client.stdin("p1", payload)
        assert fake.stdin_received["p1"] == payload

        big = b"Z" * (_STDIN_CHUNK_BYTES * 2 + 17)
        await client.stdin("p1", big)
        assert fake.stdin_received["p1"] == payload + big


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
    await asyncio.sleep(0.05)
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
