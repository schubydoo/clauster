"""Tests for the hosted-channel session engine (CL-4a).

Driven against the in-process ``fake_claustrum`` daemon: the stream-json spawn
contract, stdout-frame routing (data → redact → ring → fan-out; control_request
MCP-ack vs fail-closed permission parking), session-uuid capture, user-message
send, exit handling, stop (SIGINT → SIGKILL escalation), and ring replay.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager

import pytest

from clauster.claustrum_client import ClaustrumClient
from clauster.hosted import HostedSession, HostedSessionError, build_hosted_argv

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="claustrum hosted channel is POSIX-only (AF_UNIX)"
)

_PID = "01HOSTEDSESSION0000000000"
_BIN = "/usr/bin/claude"


async def _drain(queue: asyncio.Queue, *, timeout: float = 1.0) -> dict:
    return await asyncio.wait_for(queue.get(), timeout=timeout)


async def _drain_until(queue: asyncio.Queue, etype: str, *, timeout: float = 1.0) -> dict:
    """Pull events until one of type ``etype`` arrives (skips acks/markers)."""

    async def _loop() -> dict:
        while True:
            event = await queue.get()
            if event.get("type") == etype:
                return event

    return await asyncio.wait_for(_loop(), timeout=timeout)


def _stdin_frames(fake, pid: str = _PID) -> list[dict]:
    """Parse every NDJSON line the session wrote to the process's stdin."""
    raw = fake.stdin_received.get(pid, b"")
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


@asynccontextmanager
async def _session(fake_factory, *, stop_grace: float = 5.0, **start_kwargs):
    fake = await fake_factory()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN, stop_grace=stop_grace)
        await session.start(**start_kwargs)
        try:
            yield fake, session
        finally:
            await session.stop()


# -- argv contract ---------------------------------------------------------


def test_build_hosted_argv_minimal_contract():
    argv = build_hosted_argv(_BIN, permission_mode="acceptEdits")
    assert argv[0] == _BIN
    # The headless stream-json flag set, and explicitly NOT a bridge.
    assert "--output-format" in argv and "stream-json" in argv
    assert "--input-format" in argv
    assert "--permission-prompt-tool" in argv and "stdio" in argv
    assert "--include-partial-messages" in argv
    assert "--replay-user-messages" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--remote-control" not in argv
    assert "--resume" not in argv


def test_build_hosted_argv_with_resume():
    argv = build_hosted_argv(_BIN, permission_mode="default", resume_uuid="abc-123")
    assert argv[argv.index("--resume") + 1] == "abc-123"


# -- spawn -----------------------------------------------------------------


async def test_start_spawns_stream_json_contract(fake_claustrum):
    async with _session(fake_claustrum, permission_mode="plan") as (fake, session):
        assert session.status == "running"
        assert session.process_id == _PID
        assert len(fake.spawned) == 1
        spawned = fake.spawned[0]
        assert spawned["id"] == _PID
        assert spawned["command"] == _BIN
        assert "--output-format" in spawned["args"]
        assert spawned["args"][spawned["args"].index("--permission-mode") + 1] == "plan"


async def test_start_twice_is_rejected(fake_claustrum):
    async with _session(fake_claustrum) as (_fake, session):
        with pytest.raises(HostedSessionError):
            await session.start()


async def test_want_pid_populates_agent_pid(fake_claustrum):
    fake = await fake_claustrum(support_want_pid=True)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        await session.start(want_pid=True)
        assert session.agent_pid == 4242
        assert session.agent_proc_start == 1717000000.0
        await session.stop()


# -- data-frame routing + redaction ---------------------------------------


async def test_data_frame_redacted_and_session_uuid_captured(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        uuid = "11111111-2222-4333-8444-555555555555"
        frame = {
            "type": "system",
            "subtype": "init",
            "session_id": uuid,
            "text": "token ghp_ABCDEFGHIJKLMNOPQRST here",
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        event = await _drain_until(queue, "frame")
        # Raw uuid captured before redaction; delivered copy is masked.
        assert session.claude_session_uuid == uuid
        assert event["frame"]["session_id"] == "<redacted>"
        assert "ghp_ABCDEFGHIJKLMNOPQRST" not in json.dumps(event["frame"])
        assert "<redacted>" in event["frame"]["text"]


async def test_non_json_stdout_forwarded_as_text(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        await fake.emit(_PID, "stdout", b"not json at all\n")
        event = await _drain_until(queue, "text")
        assert "not json" in event["text"]


async def test_non_dict_json_forwarded_as_text(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        await fake.emit(_PID, "stdout", b"[1, 2, 3]\n")  # valid JSON, not an object
        event = await _drain_until(queue, "text")
        assert "[1, 2, 3]" in event["text"]


async def test_session_uuid_not_overwritten(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        first = "11111111-2222-4333-8444-555555555555"
        second = "99999999-8888-4777-8666-555555555555"
        await fake.emit(
            _PID, "stdout", (json.dumps({"type": "system", "session_id": first}) + "\n").encode()
        )
        await _drain_until(queue, "frame")
        await fake.emit(
            _PID, "stdout", (json.dumps({"type": "user", "session_id": second}) + "\n").encode()
        )
        await _drain_until(queue, "frame")
        assert session.claude_session_uuid == first


async def test_stderr_emitted_and_redacted(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        await fake.emit(_PID, "stderr", b"warn env_01ABCDEFGHIJKLMNOPQRSTUV\n")
        event = await _drain_until(queue, "stderr")
        assert "env_<redacted>" in event["text"]


# -- control plane ---------------------------------------------------------


async def test_initialize_control_request_auto_acked(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        frame = {
            "request_id": "init-1",
            "type": "control_request",
            "request": {"subtype": "initialize"},
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        # Auto-acked: a control_response success lands on stdin and nothing parks.
        await asyncio.sleep(0.05)
        frames = _stdin_frames(fake)
        assert any(
            f.get("type") == "control_response"
            and f["response"]["subtype"] == "success"
            and f["response"]["request_id"] == "init-1"
            for f in frames
        )
        assert session.pending_requests == []


async def test_permission_request_is_parked_not_auto_answered(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash"},
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        event = await _drain_until(queue, "control_request")
        assert event["request_id"] == "perm-1"
        # Fail-closed: nothing written to stdin, request is parked.
        assert _stdin_frames(fake) == []
        assert [r.request_id for r in session.pending_requests] == ["perm-1"]

        await session.respond_control("perm-1", {"behavior": "allow"})
        await asyncio.sleep(0.05)
        responses = [f for f in _stdin_frames(fake) if f.get("type") == "control_response"]
        assert responses and responses[0]["response"]["request_id"] == "perm-1"
        assert session.pending_requests == []


async def test_respond_to_unknown_request_raises(fake_claustrum):
    async with _session(fake_claustrum) as (_fake, session):
        with pytest.raises(HostedSessionError):
            await session.respond_control("nope", {})


# -- input + lifecycle -----------------------------------------------------


async def test_send_message_writes_user_frame(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        await session.send_message("hello there")
        await asyncio.sleep(0.05)
        frames = _stdin_frames(fake)
        assert frames == [{"type": "user", "message": {"role": "user", "content": "hello there"}}]


async def test_send_message_rejected_after_exit(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        await fake.emit_exit(_PID, 0)
        await asyncio.sleep(0.05)
        assert session.status == "stopped"
        with pytest.raises(HostedSessionError):
            await session.send_message("too late")


async def test_exit_nonzero_is_crashed(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        await fake.emit_exit(_PID, 3)
        event = await _drain_until(queue, "exit")
        assert event["exit_code"] == 3
        assert session.status == "crashed"


async def test_stop_sends_sigint_then_settles(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        # An exit frame arrives within the grace window → no SIGKILL escalation.
        async def _exit_soon():
            await asyncio.sleep(0.02)
            await fake.emit_exit(_PID, 0)

        task = asyncio.create_task(_exit_soon())
        await session.stop()
        await task
        signals = [k.get("signal") for k in fake.killed]
        assert "INT" in signals
        assert "KILL" not in signals


async def test_stop_escalates_to_sigkill_on_grace_timeout(fake_claustrum):
    async with _session(fake_claustrum, stop_grace=0.05) as (fake, session):
        # No exit frame → grace expires → SIGKILL.
        await session.stop()
        signals = [k.get("signal") for k in fake.killed]
        assert signals[0] == "INT"
        assert "KILL" in signals


# -- ring replay -----------------------------------------------------------


async def test_subscribe_replays_ring_past_cursor(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        for i in range(3):
            await fake.emit(_PID, "stdout", (json.dumps({"type": "user", "n": i}) + "\n").encode())
        # Let the pump ring all three before a late subscriber joins.
        first = session.subscribe()
        await _drain_until(first, "frame")  # ensure at least one is ringed

        late = session.subscribe(after_seq=1)
        seqs = []
        for _ in range(2):
            event = await _drain(late)
            if event.get("type") == "frame":
                seqs.append(event["event_seq"])
        assert all(s > 1 for s in seqs)


async def test_subscribe_emits_gap_when_ring_already_evicted(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN, ring_size=2)
        await session.start()
        warm = session.subscribe()
        for i in range(5):
            await fake.emit(_PID, "stdout", (json.dumps({"type": "user", "n": i}) + "\n").encode())
        for _ in range(5):  # let the pump ring all five (only the last 2 survive)
            await _drain_until(warm, "frame")

        late = session.subscribe(after_seq=0)  # cursor older than the oldest retained event
        marker = await _drain(late)
        assert marker["type"] == "gap"
        assert marker["from_seq"] == 0 and marker["to_seq"] > 1
        await session.stop()


def test_subscriber_overflow_inserts_gap_marker():
    # Unit-level: a full queue drops + counts, then prepends a gap once it drains.
    from clauster.hosted import _Subscriber

    sub = _Subscriber(asyncio.Queue(maxsize=2))
    sub.offer({"event_seq": 1})  # queued
    sub.offer({"event_seq": 2})  # queued (full now)
    sub.offer({"event_seq": 3})  # dropped
    sub.offer({"event_seq": 4})  # dropped (dropped == 2)
    assert sub.dropped == 2
    assert sub.queue.get_nowait() == {"event_seq": 1}
    assert sub.queue.get_nowait() == {"event_seq": 2}
    sub.offer({"event_seq": 5})  # room now → gap marker first, then the event
    assert sub.queue.get_nowait() == {"type": "gap", "dropped": 2}
    assert sub.queue.get_nowait() == {"event_seq": 5}
    assert sub.dropped == 0
