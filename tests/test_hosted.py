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

from clauster.claustrum_client import ClaustrumClient, ClaustrumError, DaemonUnreachable
from clauster.hosted import (
    HostedManager,
    HostedSession,
    HostedSessionError,
    build_hosted_argv,
)
from clauster.hosted_state import HostedStateStore
from clauster.models import InstanceStatus, RemoteControlInstance

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
async def _session(fake_factory, *, stop_grace: float = 0.1, **start_kwargs):
    # Small default grace: the fake's process.kill never emits a terminal exit
    # frame, so a default-grace teardown otherwise waits out the full SIGINT +
    # SIGKILL windows (~10s/test). Tests that assert on the escalation timing pass
    # their own stop_grace; functional tests only need stop() to tear down.
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
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "echo hi"},
            },
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
        # The CLI requires updatedInput on every allow — defaulted from the parked
        # request's original input ("allow unchanged").
        assert responses[0]["response"]["response"] == {
            "behavior": "allow",
            "updatedInput": {"command": "echo hi"},
        }
        assert session.pending_requests == []
        # A control_resolved event fans out so reconnects see the request is answered.
        resolved = await _drain_until(queue, "control_resolved")
        assert resolved["request_id"] == "perm-1" and resolved["behavior"] == "allow"


@pytest.mark.parametrize(
    "request_payload",
    [
        {"subtype": "can_use_tool", "tool_name": "Bash"},  # no input at all
        {"subtype": "can_use_tool", "tool_name": "Bash", "input": "bogus"},  # non-dict
    ],
    ids=["missing-input", "non-dict-input"],
)
async def test_allow_without_parked_input_sends_empty_record(fake_claustrum, request_payload):
    # The schema wants a record; a parked request with no (or non-dict) input still
    # needs updatedInput on allow — default to {} rather than omit it.
    async with _session(fake_claustrum) as (fake, session):
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": request_payload,
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await _drain_until(session.subscribe(), "control_request")
        await session.respond_control("perm-1", {"behavior": "allow"})
        await asyncio.sleep(0.05)
        responses = [f for f in _stdin_frames(fake) if f.get("type") == "control_response"]
        assert responses[0]["response"]["response"] == {"behavior": "allow", "updatedInput": {}}


async def test_failed_response_write_leaves_request_parked(fake_claustrum, monkeypatch):
    # The stdin write fails while the session is still running: the error must
    # surface AND the request must stay parked so the operator can retry —
    # never consumed with the agent left waiting unanswered.
    async with _session(fake_claustrum) as (fake, session):
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "echo hi"},
            },
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await _drain_until(session.subscribe(), "control_request")

        async def _boom(request_id, response):
            raise ClaustrumError("stdin write failed")

        monkeypatch.setattr(session, "_send_control_response", _boom)
        with pytest.raises(ClaustrumError):
            await session.respond_control("perm-1", {"behavior": "allow"})
        assert [r.request_id for r in session.pending_requests] == ["perm-1"]

        # Retry once the transport recovers: the same request answers cleanly.
        monkeypatch.undo()
        await session.respond_control("perm-1", {"behavior": "allow"})
        await asyncio.sleep(0.05)
        assert session.pending_requests == []
        responses = [f for f in _stdin_frames(fake) if f.get("type") == "control_response"]
        assert responses and responses[-1]["response"]["request_id"] == "perm-1"


async def test_concurrent_responders_answer_a_request_once(fake_claustrum, monkeypatch):
    # Two responders race to answer the same parked request. The claim is atomic
    # (pop before the await), so exactly one control_response is written and the
    # loser fails the existence check instead of writing a duplicate frame.
    async with _session(fake_claustrum) as (fake, session):
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "echo hi"},
            },
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await _drain_until(session.subscribe(), "control_request")

        original = session._send_control_response

        async def _slow(request_id, response):
            # Yield so both coroutines are in flight across the write boundary.
            await asyncio.sleep(0.01)
            await original(request_id, response)

        monkeypatch.setattr(session, "_send_control_response", _slow)
        results = await asyncio.gather(
            session.respond_control("perm-1", {"behavior": "allow"}),
            session.respond_control("perm-1", {"behavior": "allow"}),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        assert len(errors) == 1 and isinstance(errors[0], HostedSessionError)
        await asyncio.sleep(0.05)
        responses = [f for f in _stdin_frames(fake) if f.get("type") == "control_response"]
        assert len(responses) == 1
        assert session.pending_requests == []


async def test_allow_with_explicit_updated_input_is_preserved(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "echo hi"},
            },
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await _drain_until(session.subscribe(), "control_request")
        await session.respond_control(
            "perm-1", {"behavior": "allow", "updatedInput": {"command": "echo edited"}}
        )
        await asyncio.sleep(0.05)
        responses = [f for f in _stdin_frames(fake) if f.get("type") == "control_response"]
        assert responses[0]["response"]["response"]["updatedInput"] == {"command": "echo edited"}


async def test_deny_response_is_not_modified(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "echo hi"},
            },
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await _drain_until(session.subscribe(), "control_request")
        await session.respond_control("perm-1", {"behavior": "deny", "message": "no"})
        await asyncio.sleep(0.05)
        responses = [f for f in _stdin_frames(fake) if f.get("type") == "control_response"]
        assert responses[0]["response"]["response"] == {"behavior": "deny", "message": "no"}


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
        # Exit clears the parked request (resolved as interrupted), so respond raises at
        # the status guard without consuming anything — and no dead Allow/Deny lingers.
        assert session.pending_requests == []


async def test_exit_resolves_parked_permission_requests(fake_claustrum):
    # A parked permission request can never be answered once the agent exits, so exit
    # clears it and fans out a `control_resolved` (interrupted) — the live UI drops the
    # dead Allow/Deny instead of leaving a button that 409s forever, and a reattach
    # replays it as already-resolved.
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash"},
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await _drain_until(queue, "control_request")
        assert [r.request_id for r in session.pending_requests] == ["perm-1"]
        await fake.emit_exit(_PID, 0)
        resolved = await _drain_until(queue, "control_resolved")
        assert resolved["request_id"] == "perm-1" and resolved["behavior"] == "interrupted"
        assert session.pending_requests == []


async def test_respond_rejected_while_stopping(fake_claustrum):
    # stop() latches _stopping before its SIGINT grace; a respond that races in while
    # the status is still "running" must fail closed rather than write a control_response
    # into a session being torn down.
    async with _session(fake_claustrum, stop_grace=0.2) as (fake, session):
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash"},
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await _drain_until(session.subscribe(), "control_request")
        stop_task = asyncio.create_task(session.stop())
        await asyncio.sleep(0.01)  # let stop() latch _stopping before we respond
        assert session.status == "running" and session._stopping
        with pytest.raises(HostedSessionError, match="stopping"):
            await session.respond_control("perm-1", {"behavior": "allow"})
        await stop_task


async def test_exit_resolves_parked_requests_on_daemon_loss(fake_claustrum, monkeypatch):
    # Daemon loss during stop() drives status="error" WITHOUT an exit frame (the pump
    # never calls _on_exit), so the error path must resolve parked requests itself —
    # the same invariant as a clean exit, not a dead Allow/Deny left actionable.
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash"},
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await _drain_until(queue, "control_request")
        assert [r.request_id for r in session.pending_requests] == ["perm-1"]

        async def _boom(*_a, **_k):
            raise ClaustrumError("daemon vanished mid-stop")

        monkeypatch.setattr(session._client, "kill", _boom)
        await session.stop()
        assert session.status == "error"
        resolved = await _drain_until(queue, "control_resolved")
        assert resolved["request_id"] == "perm-1" and resolved["behavior"] == "interrupted"
        assert session.pending_requests == []


async def test_parked_control_request_payload_is_redacted(fake_claustrum):
    # Negative control-plane redaction (audit #5): a secret-shaped value embedded in
    # a parked permission control_request payload must be masked before it reaches
    # the ring/fan-out — the raw secret must NOT appear in what subscribers (or a
    # ring-replaying reconnect) see. The control plane gets the same defense-in-depth
    # `_redact_obj` pass as data frames, not a bypass.
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWX"
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": f"curl -H 'auth: {secret}' example.com"},
                "context": {"token": secret},
            },
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        event = await _drain_until(queue, "control_request")
        # The fanned-out event's request payload is masked end-to-end.
        assert secret not in json.dumps(event["request"])
        assert "<redacted>" in event["request"]["context"]["token"]
        assert "<redacted>" in event["request"]["input"]["command"]
        # And the same masked copy is what a ring-replaying reconnect would receive.
        replayed = await _drain_until(session.subscribe(), "control_request")
        assert secret not in json.dumps(replayed)
        # The PARKED request keeps the raw input so an "allow unchanged" replays the
        # real tool input back to the agent (redaction is browser-facing only).
        assert [r.request_id for r in session.pending_requests] == ["perm-1"]
        parked_raw = json.dumps(session.pending_requests[0].request)
        assert secret in parked_raw  # the unredacted secret survives in the parked copy
        assert (
            session.pending_requests[0].request["input"]["command"]
            == frame["request"]["input"]["command"]
        )


async def test_bare_uuid_in_frame_leaf_is_masked_end_to_end(fake_claustrum):
    # Audit #28: a bare UUID (account/instance identifier) appearing in a data-frame
    # leaf — not just an env_/session_ prefixed id — is masked by `_redact_obj` before
    # fan-out. The capture of session_id for --resume happens on the RAW frame, so the
    # delivered copy can be fully redacted without losing resume.
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        bare = "abcdef01-2345-4678-89ab-cdef01234567"
        frame = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"org {bare} done"}]},
            "nested": {"deep": [{"org_uuid": bare}]},
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        event = await _drain_until(queue, "frame")
        assert bare not in json.dumps(event["frame"])  # masked everywhere it appears
        assert event["frame"]["nested"]["deep"][0]["org_uuid"] == "<redacted>"
        assert "<redacted>" in event["frame"]["message"]["content"][0]["text"]


async def test_failed_write_resolves_instead_of_reparking_on_dead_session(
    fake_claustrum, monkeypatch
):
    # A respond whose write fails AFTER a concurrent exit drained _pending must not
    # resurrect the popped request onto a dead session (it would 409 forever with no
    # resolution). The except path resolves it as interrupted instead of re-parking.
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash"},
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await _drain_until(queue, "control_request")

        async def _exit_then_fail(request_id, response):
            session.status = "crashed"  # a concurrent exit lands during the write
            raise ClaustrumError("daemon lost mid-write")

        monkeypatch.setattr(session, "_send_control_response", _exit_then_fail)
        with pytest.raises(ClaustrumError):
            await session.respond_control("perm-1", {"behavior": "allow"})
        # Not re-parked onto the dead session — resolved as interrupted instead.
        assert session.pending_requests == []
        resolved = await _drain_until(queue, "control_resolved")
        assert resolved["request_id"] == "perm-1" and resolved["behavior"] == "interrupted"


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
        assert session.status == "stopped"


async def test_stop_latches_status_when_pump_misses_the_exit(fake_claustrum):
    # Live, the stream latches `exited` before the exit event reaches the pump's
    # queue; stop() wakes on the latch and its teardown cancels the pump before it
    # can route the event — the row then stays "running" forever and resume 409s.
    # Simulate the lost race by cancelling the pump outright before the exit lands.
    async with _session(fake_claustrum) as (fake, session):
        session._pump_task.cancel()

        async def _exit_soon():
            await asyncio.sleep(0.02)
            await fake.emit_exit(_PID, 0)

        task = asyncio.create_task(_exit_soon())
        await session.stop()
        await task
        assert session.status == "stopped"
        assert session.exit_code == 0


async def test_stop_escalates_to_sigkill_on_grace_timeout(fake_claustrum):
    async with _session(fake_claustrum, stop_grace=0.05) as (fake, session):
        # No exit frame → grace expires → SIGKILL.
        await session.stop()
        signals = [k.get("signal") for k in fake.killed]
        assert signals[0] == "INT"
        assert "KILL" in signals


async def test_stop_second_grace_expires_when_kill_yields_no_exit(fake_claustrum):
    # Audit #31: after SIGINT times out we SIGKILL and wait a SECOND grace window for
    # the daemon's exit frame. If even that never lands (the daemon never reports the
    # exit), the second `wait_for` times out and stop() falls through cleanly — no
    # hang, no crash, both signals were issued. This drives the second-grace TimeoutError
    # branch (formerly pragma'd) with NO exit frame ever emitted.
    async with _session(fake_claustrum, stop_grace=0.02) as (fake, session):
        await session.stop()
        signals = [k.get("signal") for k in fake.killed]
        assert signals[0] == "INT"
        assert "KILL" in signals
        # The daemon never reported an exit, so there's no terminal code to latch.
        assert session.exit_code is None
        assert session._pump_task is None  # pump torn down regardless


async def test_pump_surfaces_daemon_loss_as_error_and_resolves_parked(fake_claustrum):
    # Audit #32: a daemon-side failure surfacing through the stream reader (a
    # ClaustrumError out of the pump's source.get) must fail closed — status flips to
    # "error", any parked request is resolved (never left as a dead Allow/Deny), and a
    # `lost` event is fanned out (surfaced, not swallowed). Drives the _pump
    # `except ClaustrumError` branch (formerly pragma'd).
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        frame = {
            "request_id": "perm-1",
            "type": "control_request",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash"},
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await _drain_until(queue, "control_request")
        assert [r.request_id for r in session.pending_requests] == ["perm-1"]

        # Inject a daemon-loss fault into the live pump's source: the next get() raises.
        class _BoomSource:
            async def get(self):
                raise ClaustrumError("daemon vanished mid-pump")

        session._source = _BoomSource()
        # Nudge the pump: cancel the current get() so it loops back into _BoomSource.get.
        session._pump_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session._pump_task
        # Re-run the pump body against the boom source to exercise the except branch.
        await session._pump()
        assert session.status == "error"
        assert session.pending_requests == []  # resolved on loss, not stranded
        lost = await _drain_until(queue, "lost")
        assert "daemon vanished" in lost["reason"]
        resolved = await _drain_until(session.subscribe(), "control_resolved")
        assert resolved["request_id"] == "perm-1"


async def test_stop_kill_escalation_waits_for_exit_and_latches(fake_claustrum):
    # The exit frame only lands after the KILL — stop() must wait for it (second
    # grace window) and latch the terminal status rather than tear down with the
    # row still "running".
    async with _session(fake_claustrum, stop_grace=0.2) as (fake, session):

        async def _exit_after_kill():
            while not any(k.get("signal") == "KILL" for k in fake.killed):
                await asyncio.sleep(0.01)
            await fake.emit_exit(_PID, 9)

        task = asyncio.create_task(_exit_after_kill())
        await session.stop()
        await task
        assert "KILL" in [k.get("signal") for k in fake.killed]
        assert session.status == "crashed"
        assert session.exit_code == 9


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


async def test_manager_forget_drops_stopped_session(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        await fake.emit_exit(pid, 0)
        await asyncio.sleep(0.05)
        await mgr.stop(pid)
        assert mgr.get_instance(pid) is not None  # a stopped, resumable row

        await mgr.forget(pid)
        assert mgr.get_instance(pid) is None
        assert mgr.session(pid) is None
        assert mgr.list_instances() == []


async def test_manager_forget_refuses_running(fake_claustrum):
    async with _manager(fake_claustrum) as (_fake, client, mgr):
        inst = await _spawn(mgr, client)
        with pytest.raises(HostedSessionError, match="still running"):
            await mgr.forget(inst.claustrum_process_id)
        assert mgr.get_instance(inst.claustrum_process_id) is not None  # left intact


async def test_manager_forget_refuses_orphan(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        await fake.emit_exit(pid, 0)
        await asyncio.sleep(0.05)
        await mgr.stop(pid)
        mgr.get_instance(pid).is_orphan = True  # a live survivor — must be Killed, not forgotten
        with pytest.raises(HostedSessionError, match="orphan"):
            await mgr.forget(pid)


async def test_manager_forget_unknown_raises(fake_claustrum):
    async with _manager(fake_claustrum) as (_fake, _client, mgr):
        with pytest.raises(HostedSessionError):
            await mgr.forget("nope")


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
        # Isolate the public path: drop the file + the diff-check cache so persist()
        # can't early-return, then assert the public call itself rewrote the record.
        (tmp_path / "hosted_state.json").unlink()
        mgr._last_saved = None
        await mgr.persist()  # public entry (the dashboard-poll path)
        assert inst.claustrum_process_id in store.load()


class _BoomReattachClient:
    """A stub daemon client whose reattach raises — isolates the error path."""

    def stream(self, process_id):
        class _Stream:
            def subscribe(self):
                return asyncio.Queue()

            def unsubscribe(self, queue):
                pass

        return _Stream()

    async def reattach(self, process_id, from_seq=0):
        raise DaemonUnreachable("daemon gone")


async def test_manager_reattach_daemon_error_is_recorded(fake_claustrum, tmp_path):
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = await _spawn_gen1(fake, store)
    # A daemon error on reattach is recorded per-session as ERROR, never crashing
    # reattach_all / startup.
    mgr = HostedManager(store)
    await mgr.reattach_all(_BoomReattachClient())
    inst = mgr.get_instance(pid)
    assert inst.status is InstanceStatus.ERROR
    assert "reattach failed" in (inst.error_detail or "")


async def test_concurrent_persist_serializes_writes_no_lost_update(fake_claustrum, tmp_path):
    # Audit #30: spawn/stop/aclose/poll can all call _persist concurrently. The persist
    # lock must serialize snapshot→save→_last_saved so a slower writer carrying an OLDER
    # snapshot can't finish last and overwrite a newer file (a lost update / regressed
    # cursor). Race many persists against a deliberately slow, interleaving store and
    # assert the final on-disk state matches the final in-memory registry exactly.
    saved_snapshots: list[dict] = []

    class _SlowStore:
        """Records every save and yields mid-write so writers interleave."""

        def __init__(self) -> None:
            self._last: dict | None = None

        def load(self) -> dict:
            return {}

        def save(self, sessions: dict) -> None:
            # Snapshot the payload, then yield the event loop *inside* the critical
            # section the lock is meant to protect — if the lock were missing, a
            # second writer would clobber `self._last` here.
            import time

            saved_snapshots.append(dict(sessions))
            time.sleep(0)  # cooperative point is the to_thread hop itself
            self._last = dict(sessions)

    async with _manager(fake_claustrum) as (fake, client, mgr):
        store = _SlowStore()
        mgr._store = store
        # Register several instances, mutating the registry between persists so each
        # snapshot legitimately differs (the diff-check can't early-return them all).
        insts = [await _spawn(mgr, client, project=f"p{i}") for i in range(4)]
        # Fire many persists concurrently; each takes the lock in turn.
        await asyncio.gather(*(mgr.persist() for _ in range(12)))
        # The final saved snapshot must equal the final registry projection — no writer
        # regressed it to an older view.
        final = {pid: mgr._record(mgr._synced(i)) for pid, i in mgr._instances.items()}
        assert store._last == final
        assert mgr._last_saved == final
        # Every pid is present and intact in the last write (no dropped/corrupt entry).
        assert set(store._last) == {i.claustrum_process_id for i in insts}


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


# -- resume (CL-7) ---------------------------------------------------------


async def _crash_with_uuid(fake, mgr, client, uuid):
    """Spawn a session, capture a session uuid, then crash it — returns its id."""
    inst = await _spawn(mgr, client)
    pid = inst.claustrum_process_id
    await fake.emit(
        pid, "stdout", (json.dumps({"type": "system", "session_id": uuid}) + "\n").encode()
    )
    await fake.emit_exit(pid, 1)  # nonzero → crashed
    await asyncio.sleep(0.05)
    return pid


async def test_manager_resume_respawns_with_uuid(fake_claustrum):
    uuid = "11111111-2222-4333-8444-555555555555"
    async with _manager(fake_claustrum) as (fake, client, mgr):
        old_id = await _crash_with_uuid(fake, mgr, client, uuid)
        assert mgr.get_instance(old_id).status is InstanceStatus.CRASHED
        assert mgr.get_instance(old_id).claude_session_uuid == uuid

        resumed = await mgr.resume(client, old_id, cwd="/tmp/proj", claude_binary=_BIN)
        assert resumed.status is InstanceStatus.RUNNING
        assert resumed.claustrum_process_id != old_id  # fresh daemon process
        assert resumed.claude_session_uuid == uuid
        assert mgr.get_instance(old_id) is None  # dead row retired
        # The fresh spawn carried --resume <uuid>.
        args = fake.spawned[-1]["args"]
        assert args[args.index("--resume") + 1] == uuid
        await mgr.aclose()


async def test_manager_resume_persists_only_resumed_row(fake_claustrum, tmp_path):
    # The retire-then-persist contract: after a resume, hosted_state.json holds ONLY
    # the resumed row (never both → no duplicate card if a restart reattaches).
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    uuid = "11111111-2222-4333-8444-555555555555"
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        inst = await mgr.spawn(
            client,
            project="proj",
            label="hosted:proj",
            cwd=str(tmp_path),
            claude_binary=_BIN,
            permission_mode="acceptEdits",
        )
        old_id = inst.claustrum_process_id
        await fake.emit(
            old_id, "stdout", (json.dumps({"type": "system", "session_id": uuid}) + "\n").encode()
        )
        await fake.emit_exit(old_id, 1)
        await asyncio.sleep(0.05)
        resumed = await mgr.resume(client, old_id, cwd=str(tmp_path), claude_binary=_BIN)
        new_id = resumed.claustrum_process_id

        persisted = store.load()
        assert new_id in persisted and old_id not in persisted  # only the resumed row
        assert persisted[new_id]["claude_session_uuid"] == uuid
        await mgr.aclose()


async def test_manager_resume_after_reattach_loss(fake_claustrum, tmp_path):
    # An instance restored as CRASHED on startup (daemon lost it → no live session)
    # is still resumable from its persisted uuid — exercises the no-old-session path.
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    uuid = "11111111-2222-4333-8444-555555555555"
    old_id = "01GONEPROCESS00000000000"
    store.save({old_id: {"project": "proj", "label": "hosted:proj", "claude_session_uuid": uuid}})
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client)  # not-found → CRASHED row, no session
        assert mgr.get_instance(old_id).status is InstanceStatus.CRASHED
        assert mgr.session(old_id) is None

        resumed = await mgr.resume(client, old_id, cwd=str(tmp_path), claude_binary=_BIN)
        assert resumed.status is InstanceStatus.RUNNING
        assert resumed.claude_session_uuid == uuid
        assert mgr.get_instance(old_id) is None  # dead row retired
        await mgr.aclose()


async def test_manager_resume_unknown_raises(fake_claustrum):
    async with _manager(fake_claustrum) as (_fake, client, mgr):
        with pytest.raises(HostedSessionError):
            await mgr.resume(client, "nope", cwd="/tmp/x", claude_binary=_BIN)


async def test_manager_resume_running_raises(fake_claustrum):
    async with _manager(fake_claustrum) as (_fake, client, mgr):
        inst = await _spawn(mgr, client)
        with pytest.raises(HostedSessionError):  # still live → not resumable
            await mgr.resume(
                client, inst.claustrum_process_id, cwd="/tmp/proj", claude_binary=_BIN
            )


async def test_manager_resume_without_uuid_raises(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        await fake.emit_exit(pid, 1)  # crashed, but no session_id was ever seen
        await asyncio.sleep(0.05)
        with pytest.raises(HostedSessionError):
            await mgr.resume(client, pid, cwd="/tmp/proj", claude_binary=_BIN)


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


# -- orphan detection + recovery (CL-8) ------------------------------------


def _orphan_instance(pid=4242, uuid="11111111-2222-4333-8444-555555555555"):
    """A crashed hosted row that survived a daemon restart (live pid, no session)."""
    return RemoteControlInstance(
        project="proj",
        label="hosted:proj",
        channel="hosted",
        claustrum_process_id="01ORPHAN0000000000000000",
        agent_pid=pid,
        agent_proc_start=1000.0,
        claude_session_uuid=uuid,
        status=InstanceStatus.CRASHED,
        is_orphan=True,
    )


async def test_manager_reattach_marks_live_survivor_as_orphan(
    fake_claustrum, tmp_path, monkeypatch
):
    monkeypatch.setattr("clauster.procutil.is_live_process", lambda *a, **k: True)
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = "01GONEPROCESS00000000000"
    store.save(
        {
            pid: {
                "project": "proj",
                "label": "hosted:proj",
                "agent_pid": 4242,
                "agent_proc_start": 1000.0,
                "claude_session_uuid": "u-1",
            }
        }
    )
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client)  # daemon doesn't know it → not-found
        inst = mgr.get_instance(pid)
        assert inst.status is InstanceStatus.CRASHED
        assert inst.is_orphan is True
        assert "survived a daemon restart" in (inst.error_detail or "")


async def test_manager_reattach_lost_when_no_survivor(fake_claustrum, tmp_path, monkeypatch):
    monkeypatch.setattr("clauster.procutil.is_live_process", lambda *a, **k: False)
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = "01GONEPROCESS00000000000"
    store.save(
        {
            pid: {
                "project": "proj",
                "label": "hosted:proj",
                "agent_pid": 4242,
                "agent_proc_start": 1000.0,
            }
        }
    )
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client)
        inst = mgr.get_instance(pid)
        assert inst.status is InstanceStatus.CRASHED
        assert inst.is_orphan is False
        assert "session lost" in (inst.error_detail or "")


async def test_manager_reattach_lost_when_survivor_not_killable(
    fake_claustrum, tmp_path, monkeypatch
):
    # A pre-CL-8 row has no agent_proc_start, so we have no create-time evidence to
    # safely kill the survivor. Even though the pid is alive, it must be classified
    # LOST — never a recoverable orphan — so kill_orphan/resume can't report a clean
    # stop while the process keeps running.
    monkeypatch.setattr("clauster.procutil.is_live_process", lambda *a, **k: True)
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = "01GONEPROCESS00000000000"
    store.save(
        {
            pid: {
                "project": "proj",
                "label": "hosted:proj",
                "agent_pid": 4242,
                # no agent_proc_start → uncomparable → not killable
            }
        }
    )
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client)
        inst = mgr.get_instance(pid)
        assert inst.status is InstanceStatus.CRASHED
        assert inst.is_orphan is False  # unkillable survivor is lost, not orphan
        assert "session lost" in (inst.error_detail or "")


async def test_manager_kill_orphan_terminates_and_stops(monkeypatch):
    killed: list[tuple] = []
    monkeypatch.setattr(
        "clauster.procutil.kill_if_match", lambda pid, ps: killed.append((pid, ps))
    )
    mgr = HostedManager()
    inst = _orphan_instance()
    inst.error_detail = "Resume to recover, or Kill"  # stale orphan recovery prompt
    mgr._instances[inst.claustrum_process_id] = inst
    result = await mgr.kill_orphan(inst.claustrum_process_id)
    assert killed == [(4242, 1000.0)]  # match-gated kill issued for the survivor
    assert result.status is InstanceStatus.STOPPED
    assert result.intentional_stop is True
    assert result.is_orphan is False
    assert result.error_detail is None  # stale recovery prompt cleared on clean stop


async def test_manager_kill_orphan_without_pid_skips_kill(monkeypatch):
    # A row with no agent_pid (no survivor to terminate) still stops cleanly and never
    # issues a kill — covers the pid-absent branch of kill_orphan.
    killed: list[tuple] = []
    monkeypatch.setattr(
        "clauster.procutil.kill_if_match", lambda pid, ps: killed.append((pid, ps))
    )
    mgr = HostedManager()
    inst = _orphan_instance(pid=None)
    mgr._instances[inst.claustrum_process_id] = inst
    result = await mgr.kill_orphan(inst.claustrum_process_id)
    assert killed == []  # no pid → no kill attempted
    assert result.status is InstanceStatus.STOPPED
    assert result.is_orphan is False


async def test_manager_kill_orphan_unknown_raises():
    mgr = HostedManager()
    with pytest.raises(HostedSessionError):
        await mgr.kill_orphan("nope")


async def test_manager_resume_kills_orphan_survivor(fake_claustrum, monkeypatch):
    killed: list[tuple] = []
    monkeypatch.setattr(
        "clauster.procutil.kill_if_match", lambda pid, ps: killed.append((pid, ps))
    )
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = _orphan_instance()
        mgr._instances[inst.claustrum_process_id] = inst  # orphan: no live session
        resumed = await mgr.resume(
            client, inst.claustrum_process_id, cwd="/tmp/proj", claude_binary=_BIN
        )
        assert killed == [(4242, 1000.0)]  # survivor killed as part of the resume
        assert resumed.status is InstanceStatus.RUNNING
        assert resumed.claude_session_uuid == inst.claude_session_uuid
        assert mgr.get_instance("01ORPHAN0000000000000000") is None  # dead row retired
        await mgr.aclose()
