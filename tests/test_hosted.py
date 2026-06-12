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
from clauster.hosted import (
    HostedManager,
    HostedSession,
    HostedSessionError,
    build_hosted_argv,
)
from clauster.hosted_state import HostedStateStore
from clauster.models import InstanceStatus

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


async def test_redacts_secret_nested_in_list(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        frame = {"type": "assistant", "items": ["leak env_01ABCDEFGHIJKLMNOPQRSTUV"]}
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        event = await _drain_until(queue, "frame")
        assert event["frame"]["items"] == ["leak env_<redacted>"]


async def test_blank_stdout_line_is_ignored(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        await fake.emit(_PID, "stdout", b"   \n")  # whitespace-only → skipped
        await fake.emit(_PID, "stdout", (json.dumps({"type": "user", "n": 1}) + "\n").encode())
        event = await _drain_until(queue, "frame")
        assert event["frame"]["n"] == 1


async def test_control_request_without_id_is_ignored(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        frame = {"type": "control_request", "request": {"subtype": "initialize"}}  # no request_id
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await asyncio.sleep(0.05)
        assert session.pending_requests == []
        assert _stdin_frames(fake) == []  # not acked, not parked


async def test_unsubscribe_stops_delivery(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        session.unsubscribe(queue)
        await fake.emit(_PID, "stdout", (json.dumps({"type": "user", "n": 1}) + "\n").encode())
        await asyncio.sleep(0.05)
        assert queue.empty()


async def test_stop_before_start_is_safe(fake_claustrum):
    # A session that never started (status "starting", no stream) must stop cleanly.
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        await session.stop()
        assert "INT" in [k.get("signal") for k in fake.killed]


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
        # A control_resolved event fans out so reconnects see the request is answered.
        resolved = await _drain_until(queue, "control_resolved")
        assert resolved["request_id"] == "perm-1" and resolved["behavior"] == "allow"


async def test_respond_to_unknown_request_raises(fake_claustrum):
    async with _session(fake_claustrum) as (_fake, session):
        with pytest.raises(HostedSessionError):
            await session.respond_control("nope", {})


async def test_respond_after_exit_raises_not_claustrum_error(fake_claustrum):
    # A request parked while running, then the session exits before the operator
    # answers: respond must raise HostedSessionError (API → 409), not let the stdin
    # write throw a bare ClaustrumError (API → 500) with the request consumed.
    async with _session(fake_claustrum) as (fake, session):
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash"},
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await _drain_until(session.subscribe(), "control_request")
        await fake.emit_exit(_PID, 3)  # session crashes while the request is parked
        await asyncio.sleep(0.05)
        assert session.status == "crashed"
        with pytest.raises(HostedSessionError):
            await session.respond_control("perm-1", {"behavior": "allow"})
        # Request not consumed — still parked, just unanswerable on a dead session.
        assert [r.request_id for r in session.pending_requests] == ["perm-1"]


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


# -- HostedManager ---------------------------------------------------------


@asynccontextmanager
async def _manager(fake_factory):
    fake = await fake_factory()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager()
        try:
            yield fake, client, mgr
        finally:
            await mgr.aclose()


async def _spawn(mgr, client, *, project="proj"):
    return await mgr.spawn(
        client,
        project=project,
        label=f"hosted:{project}",
        cwd=f"/tmp/{project}",
        claude_binary=_BIN,
        permission_mode="acceptEdits",
    )


async def test_manager_spawn_registers_running_instance(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        assert inst.channel == "hosted"
        assert inst.status is InstanceStatus.RUNNING
        pid = inst.claustrum_process_id
        assert pid and len(fake.spawned) == 1 and fake.spawned[0]["id"] == pid
        assert mgr.get_instance(pid).project == "proj"
        assert mgr.session(pid) is not None
        assert [i.claustrum_process_id for i in mgr.list_instances()] == [pid]


async def test_manager_unknown_id_returns_none(fake_claustrum):
    async with _manager(fake_claustrum) as (_fake, _client, mgr):
        assert mgr.get_instance("nope") is None
        assert mgr.session("nope") is None


async def test_manager_send_routes_to_session(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        await mgr.send(inst.claustrum_process_id, "hello")
        await asyncio.sleep(0.05)
        frames = _stdin_frames(fake, inst.claustrum_process_id)
        assert frames == [{"type": "user", "message": {"role": "user", "content": "hello"}}]


async def test_manager_send_unknown_raises(fake_claustrum):
    async with _manager(fake_claustrum) as (_fake, _client, mgr):
        with pytest.raises(HostedSessionError):
            await mgr.send("nope", "hi")


async def test_manager_respond_routes_to_session(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash"},
        }
        await fake.emit(pid, "stdout", (json.dumps(frame) + "\n").encode())
        await asyncio.sleep(0.05)
        await mgr.respond(pid, "perm-1", {"behavior": "allow"})
        await asyncio.sleep(0.05)
        responses = [f for f in _stdin_frames(fake, pid) if f.get("type") == "control_response"]
        assert responses and responses[0]["response"]["request_id"] == "perm-1"


async def test_manager_respond_unknown_raises(fake_claustrum):
    async with _manager(fake_claustrum) as (_fake, _client, mgr):
        with pytest.raises(HostedSessionError):
            await mgr.respond("nope", "perm-1", {"behavior": "allow"})


async def test_manager_synced_reflects_exit(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        await fake.emit(
            pid, "stdout", (json.dumps({"type": "system", "session_id": "u-1"}) + "\n").encode()
        )
        await fake.emit_exit(pid, 0)
        await asyncio.sleep(0.05)
        synced = mgr.get_instance(pid)
        assert synced.status is InstanceStatus.STOPPED
        assert synced.claude_session_uuid == "u-1"
        assert synced.daemon_last_seq > 0


async def test_manager_stop_returns_synced_instance(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        await fake.emit_exit(pid, 0)  # exit first so stop() doesn't wait out the grace
        await asyncio.sleep(0.05)
        result = await mgr.stop(pid)
        assert result.status is InstanceStatus.STOPPED


async def test_manager_aclose_idempotent_over_exited(fake_claustrum):
    # aclose detaches (CL-6: leave running for reattach); over already-exited
    # sessions it's a harmless no-op and their STOPPED status is preserved.
    async with _manager(fake_claustrum) as (fake, client, mgr):
        a = await _spawn(mgr, client, project="a")
        b = await _spawn(mgr, client, project="b")
        for inst in (a, b):
            await fake.emit_exit(inst.claustrum_process_id, 0)
        await asyncio.sleep(0.05)
        await mgr.aclose()
        assert all(
            mgr.get_instance(i.claustrum_process_id).status is InstanceStatus.STOPPED
            for i in (a, b)
        )


async def test_manager_synced_tolerates_missing_session(fake_claustrum):
    async with _manager(fake_claustrum) as (_fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        mgr._sessions.pop(pid)  # session gone, instance row remains → synced is a no-op
        assert mgr.get_instance(pid) is not None


# -- reattach + persistence (CL-6) -----------------------------------------


async def _spawn_gen1(fake, store):
    """Spawn one hosted session through a first manager generation, then detach.

    Returns the process id. Mirrors a clauster shutdown: ``aclose`` detaches (leaves
    the daemon-owned agent running) and flushes the persisted reattach cursor.
    """
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        inst = await mgr.spawn(
            client,
            project="proj",
            label="hosted:proj",
            cwd="/tmp/proj",
            claude_binary=_BIN,
            permission_mode="acceptEdits",
        )
        pid = inst.claustrum_process_id
        await fake.emit(pid, "stdout", b'{"type":"system","subtype":"init"}\n')
        await asyncio.sleep(0.05)  # let the pump drain the frame + advance daemon_last_seq
        await mgr.aclose()  # detach (NOT kill) + persist the cursor
    return pid


async def test_session_detach_leaves_remote_running(fake_claustrum):
    # detach() drops the local pump/subscription but never signals the agent.
    async with _session(fake_claustrum) as (fake, session):
        await session.detach()
        assert fake.killed == []  # no process.kill was sent
        assert session.status == "running"  # remote process still alive


async def test_session_reattach_not_found_is_crashed(fake_claustrum):
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, "01UNKNOWNPROCESS000000000", _BIN)
        result = await session.reattach(0)
        assert result["found"] is False
        assert session.status == "crashed"
        assert session._pump_task is None  # nothing to pump for a lost session


async def test_manager_reattach_restores_running_session(fake_claustrum, tmp_path):
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = await _spawn_gen1(fake, store)
    # New generation: fresh manager + client reattaches from the persisted store.
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        restored = await mgr.reattach_all(client)
        assert [i.claustrum_process_id for i in restored] == [pid]
        inst = mgr.get_instance(pid)
        assert inst.status is InstanceStatus.RUNNING
        assert inst.project == "proj"
        assert mgr.session(pid) is not None  # a live, pumping session
        await mgr.aclose()


async def test_manager_reattach_intentional_stop_not_reattached(fake_claustrum, tmp_path):
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    # Generation 1: spawn then STOP (records intentional_stop).
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        inst = await mgr.spawn(
            client,
            project="proj",
            label="hosted:proj",
            cwd="/tmp/proj",
            claude_binary=_BIN,
            permission_mode="acceptEdits",
        )
        pid = inst.claustrum_process_id
        await mgr.stop(pid)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client)
        assert mgr.get_instance(pid).status is InstanceStatus.STOPPED
        assert mgr.session(pid) is None  # not reattached — it was stopped on purpose


async def test_manager_reattach_unknown_process_is_crashed(fake_claustrum, tmp_path):
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    # Persisted records the daemon no longer knows (session lost while we were
    # down). Two records exercise both started_at paths in _instance_from_record:
    # a bad ISO string (parse → None) and a missing one (skip parse → None).
    store.save(
        {
            "01GONEPROCESS00000000000": {
                "project": "proj",
                "label": "hosted:proj",
                "started_at": "not-a-date",  # unparseable → None
            },
            "01GONEPROCESS00000000001": {"project": "proj", "label": "hosted:b"},  # no started_at
        }
    )
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client)
        for pid in ("01GONEPROCESS00000000000", "01GONEPROCESS00000000001"):
            inst = mgr.get_instance(pid)
            assert inst.status is InstanceStatus.CRASHED
            assert "session lost" in (inst.error_detail or "")
            assert inst.started_at is None


async def test_session_reattach_already_started_rejected(fake_claustrum):
    async with _session(fake_claustrum) as (_fake, session):
        with pytest.raises(HostedSessionError):
            await session.reattach(0)  # already pumping from start()


async def test_pump_tolerates_event_without_seq(fake_claustrum):
    # An event with no/invalid seq (e.g. an overflow marker) must not advance — or
    # crash — the reattach cursor; the seq latch simply skips it.
    async with _session(fake_claustrum) as (_fake, session):
        before = session.daemon_last_seq
        session._source.put_nowait({"type": "line", "stream": "stdout", "line": "no-seq\n"})
        await asyncio.sleep(0.05)
        assert session.daemon_last_seq == before


async def test_manager_public_persist_invokes_store(fake_claustrum, tmp_path):
    store = HostedStateStore(tmp_path)
    async with _manager(fake_claustrum) as (_fake, client, mgr):
        mgr._store = store
        inst = await _spawn(mgr, client)
        await mgr.persist()  # public entry (the dashboard-poll path)
        assert inst.claustrum_process_id in store.load()


async def test_manager_reattach_daemon_error_is_recorded(fake_claustrum, tmp_path):
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = await _spawn_gen1(fake, store)
    # An unconnected client makes every reattach raise DaemonUnreachable — recorded
    # per-session as ERROR, never crashing reattach_all / startup.
    client = ClaustrumClient(fake.socket_path, fake.token)  # never connected
    mgr = HostedManager(store)
    await mgr.reattach_all(client)
    inst = mgr.get_instance(pid)
    assert inst.status is InstanceStatus.ERROR
    assert "reattach failed" in (inst.error_detail or "")


async def test_manager_persist_tolerates_write_failure(fake_claustrum, caplog):
    # A non-authoritative store: a save OSError must not break spawn or the poll —
    # it degrades to a stale cursor with a logged warning.
    class _BoomStore:
        def load(self):
            return {}

        def save(self, sessions):
            raise OSError("disk full")

    async with _manager(fake_claustrum) as (_fake, client, mgr):
        mgr._store = _BoomStore()
        mgr._last_saved = None
        with caplog.at_level("WARNING"):
            inst = await _spawn(mgr, client)  # spawn → _persist → save raises → swallowed
        assert inst.status is InstanceStatus.RUNNING  # spawn still succeeded
        assert any("could not persist" in r.message for r in caplog.records)
        assert mgr._last_saved is None  # not marked saved → retried next time


async def test_manager_aclose_detaches_without_killing(fake_claustrum, tmp_path):
    # aclose must leave sessions running for reattach (detach, not kill) — so a fresh
    # generation can reattach the same process afterward.
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = await _spawn_gen1(fake, store)
    assert fake.killed == []  # aclose sent no process.kill
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client)
        assert mgr.get_instance(pid).status is InstanceStatus.RUNNING
        await mgr.aclose()
