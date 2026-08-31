"""Tests for the hosted-channel session engine (CL-4a).

Driven against the in-process ``fake_claustrum`` daemon: the stream-json spawn
contract, stdout-frame routing (data → redact → ring → fan-out; control_request
MCP-ack vs fail-closed permission parking), session-uuid capture, user-message
send, exit handling, stop (SIGINT → SIGKILL escalation), and ring replay.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import clauster.hosted as hosted
from clauster.claustrum_client import ClaustrumClient, ClaustrumError, DaemonUnreachable
from clauster.hosted import (
    HostedManager,
    HostedSession,
    HostedSessionError,
    _redact_obj,
    build_hosted_argv,
)
from clauster.hosted_state import HostedStateStore
from clauster.models import InstanceStatus, RemoteControlInstance
from conftest import wait_until

# Runs on Windows too: the fake daemon serves a named pipe there and the hosted
# engine drives it over the same client transport (no AF_UNIX dependency in the
# engine itself — the stop() escalation sends "INT"/"KILL" as RPC strings).

_PID = "01HOSTEDSESSION0000000000"
_BIN = "/usr/bin/claude"


@pytest.fixture(autouse=True)
def _fast_stop_grace(monkeypatch):
    """Shrink the stop grace so stop()'s INT→KILL escalation runs fast in tests.

    A handful of tests call ``stop()`` purely as teardown without first emitting an
    exit frame (the fake daemon must NOT auto-emit on kill — that deadlocks ``_push``
    against the awaited kill reply). Those otherwise burn the full 2×5s grace each.
    ``HostedSession.__init__`` resolves the grace from this global at construction, so
    patching it covers both direct and ``HostedManager``-built sessions; tests that
    pre-emit their exit are unaffected (``exited`` is already set, so the wait returns
    immediately). Tests that need a real grace pass ``stop_grace=`` explicitly.
    """
    monkeypatch.setattr(hosted, "_STOP_GRACE_SECONDS", 0.05)


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


def test_build_hosted_argv_omits_permission_mode_for_inherit():
    # #1231: hosted spawns honor the sentinel too — no --permission-mode at all, and the
    # stream-json contract flags are otherwise unchanged.
    argv = build_hosted_argv(_BIN, permission_mode="inherit", resume_uuid="abc-123")
    assert "--permission-mode" not in argv
    assert "inherit" not in argv  # the sentinel never reaches the subprocess
    assert "--output-format" in argv and "stream-json" in argv
    assert argv[argv.index("--resume") + 1] == "abc-123"


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


async def test_start_drops_stream_subscription_when_spawn_fails(fake_claustrum, monkeypatch):
    # If the spawn RPC fails after we've already subscribed to the stream, start() must drop the
    # subscription so it doesn't leak an undrained subscriber on the ProcessStream — the cleanup
    # reattach() already does on a not-found process.
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        captured: list = []
        orig_stream = client.stream

        def _capture(pid):
            stream = orig_stream(pid)
            captured.append(stream)
            return stream

        async def _boom(*args, **kwargs):
            raise ClaustrumError("spawn failed")

        monkeypatch.setattr(client, "stream", _capture)
        monkeypatch.setattr(client, "spawn", _boom)
        with pytest.raises(ClaustrumError):
            await session.start()
        assert captured and not captured[0]._subscribers  # no leaked subscriber
        assert session._stream is None and session._source is None


async def test_reattach_drops_stream_subscription_when_rpc_fails(fake_claustrum, monkeypatch):
    # Same leak guard as start(): if the reattach RPC fails after we've subscribed, reattach() must
    # drop the subscription — reattach_all() discards the session on error, so nothing else would.
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        captured: list = []
        orig_stream = client.stream

        def _capture(pid):
            stream = orig_stream(pid)
            captured.append(stream)
            return stream

        async def _boom(*args, **kwargs):
            raise ClaustrumError("reattach failed")

        monkeypatch.setattr(client, "stream", _capture)
        monkeypatch.setattr(client, "reattach", _boom)
        with pytest.raises(ClaustrumError):
            await session.reattach()
        assert captured and not captured[0]._subscribers  # no leaked subscriber
        assert session._stream is None and session._source is None
        session._drop_subscription()  # idempotent: a second release is a no-op
        assert session._stream is None and session._source is None


async def test_pump_drops_stream_subscription_on_exit(fake_claustrum):
    # When the pump exits on its own (here: a natural agent exit), its `finally` drops the stream
    # subscription immediately rather than leaving it until a later stop()/detach().
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        await session.start()
        stream = session._stream
        assert len(stream._subscribers) == 1  # the pump's own source
        await fake.emit_exit(_PID, 3)
        await wait_until(lambda: session.status == "crashed")
        await wait_until(lambda: not stream._subscribers)  # the finally dropped the subscription
        await session.stop()  # idempotent: no double-unsubscribe


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


async def test_deeply_nested_stdout_line_does_not_kill_the_pump(fake_claustrum):
    """A frame too deep for ``json.loads`` degrades to text; the pump keeps running.

    ``json.loads`` raises RecursionError — not a ValueError — on a deeply-nested line,
    so it escaped ``_on_line``'s handler into ``_pump``, which catches only
    CancelledError/ClaustrumError. The pump task died and the session went dark with
    **no** ``lost`` event: the fail-silent invariant 1 forbids. The scanner's ceiling is
    version-dependent (~994 on the 3.11 floor), so a ~40 KB line reaches it everywhere.
    """
    async with _session(fake_claustrum) as (fake, session):
        queue = session.subscribe()
        deep = ("[" * 20_000 + "]" * 20_000).encode()
        await fake.emit(_PID, "stdout", deep + b"\n")
        assert (await _drain_until(queue, "text"))["text"].startswith("[[[")
        # The pump survived: a normal frame after the hostile one still arrives.
        await fake.emit(_PID, "stdout", (json.dumps({"type": "user", "n": 1}) + "\n").encode())
        assert (await _drain_until(queue, "frame"))["frame"]["n"] == 1


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
        await wait_until(
            lambda: any(
                f.get("type") == "control_response"
                and f["response"]["subtype"] == "success"
                and f["response"]["request_id"] == "init-1"
                for f in _stdin_frames(fake)
            )
        )
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
        await wait_until(
            lambda: [f for f in _stdin_frames(fake) if f.get("type") == "control_response"]
        )
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


# -- permission-needed callback (#432 webhook hook) ------------------------


@asynccontextmanager
async def _session_with_perm_cb(fake_factory, calls, *, stop_grace: float = 0.1):
    """Like ``_session`` but wires an ``on_permission_needed`` callback recording calls."""
    fake = await fake_factory()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(
            client,
            _PID,
            _BIN,
            stop_grace=stop_grace,
            on_permission_needed=lambda pid, subtype: calls.append((pid, subtype)),
        )
        await session.start()
        try:
            yield fake, session
        finally:
            await session.stop()


async def test_parked_permission_fires_on_permission_needed_callback(fake_claustrum):
    calls: list[tuple[str, str]] = []
    async with _session_with_perm_cb(fake_claustrum, calls) as (fake, session):
        queue = session.subscribe()
        frame = {
            "request_id": "perm-cb-1",
            "type": "control_request",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash"},
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await _drain_until(queue, "control_request")
        # The callback fired once with (process_id, subtype) — never the prompt body.
        assert calls == [(_PID, "can_use_tool")]


async def test_auto_acked_request_does_not_fire_callback(fake_claustrum):
    # An MCP-handshake `initialize` is auto-acked (not parked), so no "come look" signal.
    calls: list[tuple[str, str]] = []
    async with _session_with_perm_cb(fake_claustrum, calls) as (fake, _session):
        frame = {
            "request_id": "init-cb",
            "type": "control_request",
            "request": {"subtype": "initialize"},
        }
        await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
        await wait_until(
            lambda: any(
                f.get("type") == "control_response" and f["response"]["request_id"] == "init-cb"
                for f in _stdin_frames(fake)
            )
        )
        assert calls == []


async def test_permission_callback_error_is_swallowed(fake_claustrum):
    # A throwing callback must never reach the stream pump (fail-open).
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:

        def _boom(_pid, _subtype):
            raise RuntimeError("notify boom")

        session = HostedSession(client, _PID, _BIN, stop_grace=0.1, on_permission_needed=_boom)
        await session.start()
        try:
            queue = session.subscribe()
            frame = {
                "request_id": "perm-boom",
                "type": "control_request",
                "request": {"subtype": "can_use_tool", "tool_name": "Bash"},
            }
            await fake.emit(_PID, "stdout", (json.dumps(frame) + "\n").encode())
            # The request still parks and fans out despite the callback raising.
            event = await _drain_until(queue, "control_request")
            assert event["request_id"] == "perm-boom"
            assert [r.request_id for r in session.pending_requests] == ["perm-boom"]
        finally:
            await session.stop()


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
        await wait_until(
            lambda: [f for f in _stdin_frames(fake) if f.get("type") == "control_response"]
        )
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
        await wait_until(lambda: not session.pending_requests)
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
        await wait_until(lambda: not session.pending_requests)
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
        await wait_until(
            lambda: [f for f in _stdin_frames(fake) if f.get("type") == "control_response"]
        )
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
        await wait_until(
            lambda: [f for f in _stdin_frames(fake) if f.get("type") == "control_response"]
        )
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
        await wait_until(lambda: session.status == "crashed")
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
        await wait_until(lambda: session._stopping)
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
        await wait_until(lambda: _stdin_frames(fake))
        frames = _stdin_frames(fake)
        assert frames == [{"type": "user", "message": {"role": "user", "content": "hello there"}}]


async def test_send_message_rejected_after_exit(fake_claustrum):
    async with _session(fake_claustrum) as (fake, session):
        await fake.emit_exit(_PID, 0)
        await wait_until(lambda: session.status == "stopped")
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

    # The hosted channel constructs its subscribers with the "gap" overflow marker
    # (the shared _Subscriber defaults to "overflow" for the claustrum-client path).
    sub = _Subscriber(asyncio.Queue(maxsize=2), overflow_type="gap")
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


async def test_subscribe_replay_keeps_newest_when_ring_exceeds_queue(fake_claustrum):
    # Issue #422, the real scenario: a first-view reconnect (after=0) on a session that
    # produced MORE events than the ring holds. The replay snapshot is a leading "gap"
    # marker (for the evicted prefix) plus every retained event; _Subscriber.offer drops
    # the NEW event on a full queue, so a queue smaller than the snapshot keeps the OLDEST
    # and drops the freshest. The queue is sized to hold the whole snapshot (gap + full
    # ring), so every retained event — the newest included — survives. With queue < ring
    # (and even queue == ring, because the gap marker takes a slot) the newest was dropped.
    fake = await fake_claustrum()
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN, ring_size=4, queue_maxsize=2)
        await session.start()
        warm = session.subscribe()
        for i in range(6):  # 6 > ring_size(4): the oldest events are evicted from the ring
            await fake.emit(_PID, "stdout", (json.dumps({"type": "user", "n": i}) + "\n").encode())
            await _drain_until(warm, "frame")  # drain as we emit so warm never overflows
        retained = [e["event_seq"] for e in session._ring if e.get("type") == "frame"]

        late = session.subscribe(after_seq=0)  # first-view replay of the whole ring
        saw_gap = False
        replay: list[int] = []
        while not late.empty():
            event = late.get_nowait()
            if event.get("type") == "gap":
                saw_gap = True
            elif event.get("type") == "frame":
                replay.append(event["event_seq"])
        await session.stop()

    assert saw_gap  # the evicted prefix is honestly reported
    assert replay == retained  # every retained frame replayed — the newest NOT dropped
    assert replay[-1] == retained[-1]  # explicitly: the freshest retained event survives


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
        await wait_until(lambda: _stdin_frames(fake, inst.claustrum_process_id))
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
        await wait_until(lambda: mgr.session(pid) and mgr.session(pid).pending_requests)
        await mgr.respond(pid, "perm-1", {"behavior": "allow"})
        await wait_until(
            lambda: [f for f in _stdin_frames(fake, pid) if f.get("type") == "control_response"]
        )
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
        await wait_until(lambda: mgr.get_instance(pid).status is InstanceStatus.STOPPED)
        synced = mgr.get_instance(pid)
        assert synced.status is InstanceStatus.STOPPED
        assert synced.claude_session_uuid == "u-1"
        assert synced.daemon_last_seq > 0


async def test_manager_stop_returns_synced_instance(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        await fake.emit_exit(pid, 0)  # exit first so stop() doesn't wait out the grace
        await wait_until(lambda: mgr.get_instance(pid).status is InstanceStatus.STOPPED)
        result = await mgr.stop(pid)
        assert result.status is InstanceStatus.STOPPED


async def test_manager_stop_row_popped_mid_grace_raises_not_keyerror(fake_claustrum):
    # A concurrent forget()/resume() can pop the registry row during stop()'s grace
    # window; stop() must surface that as HostedSessionError (caller maps 404), never
    # an unmapped KeyError 500.
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        await fake.emit_exit(pid, 0)  # exit first so the real stop() returns at once
        await wait_until(lambda: mgr.get_instance(pid).status is InstanceStatus.STOPPED)
        session = mgr.session(pid)
        real_stop = session.stop

        async def _stop_then_evict():
            await real_stop()
            mgr._instances.pop(pid, None)  # a concurrent forget()/resume() wins the row

        session.stop = _stop_then_evict
        with pytest.raises(HostedSessionError):
            await mgr.stop(pid)


async def test_manager_forget_drops_stopped_session(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        await fake.emit_exit(pid, 0)
        await wait_until(lambda: mgr.get_instance(pid).status is InstanceStatus.STOPPED)
        await mgr.stop(pid)
        assert mgr.get_instance(pid) is not None  # a stopped, resumable row

        assert pid in mgr._id_locks  # the lifecycle ops minted a per-id lock
        await mgr.forget(pid)
        assert mgr.get_instance(pid) is None
        assert mgr.session(pid) is None
        assert mgr.list_instances() == []
        assert pid not in mgr._id_locks  # forget prunes it so _id_locks stays bounded


async def test_manager_forget_with_no_session_handle(fake_claustrum):
    # A reattached row can carry an instance but no live session handle (its pump
    # already completed / a clauster restart rebuilt the instance only). forget must
    # skip the detach branch and still drop the row.
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        await fake.emit_exit(pid, 0)
        await wait_until(lambda: mgr.get_instance(pid).status is InstanceStatus.STOPPED)
        await mgr.stop(pid)
        mgr._sessions.pop(pid, None)  # drop the session handle, keep the instance
        assert mgr.session(pid) is None

        await mgr.forget(pid)
        assert mgr.get_instance(pid) is None


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
        await wait_until(lambda: mgr.get_instance(pid).status is InstanceStatus.STOPPED)
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
        await wait_until(
            lambda: all(
                mgr.get_instance(i.claustrum_process_id).status is InstanceStatus.STOPPED
                for i in (a, b)
            )
        )
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


# -- #834: lifecycle ops resolve the row's instance_id, not just the registry key --
# Hosted rows are keyed internally by ``claustrum_process_id`` but also expose an
# ``instance_id`` (dashed UUID) — the field API clients naturally reach for. Every
# lookup must resolve either id to the same session; an unknown id still misses.


async def test_manager_lookup_by_instance_id(fake_claustrum):
    async with _manager(fake_claustrum) as (_fake, client, mgr):
        inst = await _spawn(mgr, client)
        assert inst.instance_id != inst.claustrum_process_id  # two distinct id fields
        # get_instance + session both resolve via the instance_id, not just the key.
        got = mgr.get_instance(inst.instance_id)
        assert got is not None and got.claustrum_process_id == inst.claustrum_process_id
        assert mgr.session(inst.instance_id) is mgr.session(inst.claustrum_process_id)
        assert mgr.get_instance("no-such-id") is None  # an unknown id still misses


async def test_manager_send_by_instance_id(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        await mgr.send(inst.instance_id, "hello")  # routed by the dashed UUID
        await wait_until(lambda: _stdin_frames(fake, inst.claustrum_process_id))
        frames = _stdin_frames(fake, inst.claustrum_process_id)
        assert frames == [{"type": "user", "message": {"role": "user", "content": "hello"}}]


async def test_manager_stop_by_instance_id_locks_on_canonical_key(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        await fake.emit_exit(pid, 0)  # exit first so stop() doesn't wait out the grace
        await wait_until(lambda: mgr.get_instance(pid).status is InstanceStatus.STOPPED)
        result = await mgr.stop(inst.instance_id)  # stop by the dashed UUID
        assert result.status is InstanceStatus.STOPPED
        # The per-id lifecycle lock is keyed by the canonical process_id regardless of
        # which id the caller passed — so a mixed-id concurrent pair serializes on one
        # lock rather than each minting its own and both entering the critical section.
        assert pid in mgr._id_locks
        assert inst.instance_id not in mgr._id_locks


async def test_manager_forget_by_instance_id(fake_claustrum):
    async with _manager(fake_claustrum) as (fake, client, mgr):
        inst = await _spawn(mgr, client)
        pid = inst.claustrum_process_id
        await fake.emit_exit(pid, 0)
        await wait_until(lambda: mgr.get_instance(pid).status is InstanceStatus.STOPPED)
        await mgr.stop(pid)
        await mgr.forget(inst.instance_id)  # forget by the dashed UUID
        assert mgr.get_instance(pid) is None
        assert mgr.list_instances() == []


async def test_manager_resume_by_instance_id(fake_claustrum):
    uuid = "11111111-2222-4333-8444-555555555555"
    async with _manager(fake_claustrum) as (fake, client, mgr):
        old_id = await _crash_with_uuid(fake, mgr, client, uuid)
        old_instance_id = mgr.get_instance(old_id).instance_id
        # Resume addressed by the pre-resume instance_id, not the process_id.
        resumed = await mgr.resume(client, old_instance_id, cwd="/tmp/proj", claude_binary=_BIN)
        assert resumed.status is InstanceStatus.RUNNING
        assert resumed.claustrum_process_id != old_id  # fresh daemon process
        assert resumed.claude_session_uuid == uuid
        assert mgr.get_instance(old_id) is None  # dead row retired
        await mgr.aclose()


async def test_manager_kill_orphan_by_instance_id(monkeypatch):
    killed: list[tuple] = []
    monkeypatch.setattr(
        "clauster.procutil.kill_if_match", lambda pid, ps: killed.append((pid, ps))
    )
    mgr = HostedManager()
    inst = _orphan_instance()
    mgr._instances[inst.claustrum_process_id] = inst
    result = await mgr.kill_orphan(inst.instance_id)  # kill by the dashed UUID
    assert killed == [(4242, 1000.0)]  # match-gated kill still issued for the survivor
    assert result.status is InstanceStatus.STOPPED


async def test_manager_stop_unknown_raises(fake_claustrum):
    # An unknown id fails the _key_for resolve → 404 before any lock is taken.
    async with _manager(fake_claustrum) as (_fake, _client, mgr):
        with pytest.raises(HostedSessionError):
            await mgr.stop("no-such-id")


async def test_manager_kill_orphan_row_evicted_after_resolve_raises(monkeypatch):
    # A concurrent forget/resume can pop the row between _key_for() and lock
    # acquisition; kill_orphan must re-check under the lock and raise (caller maps
    # 404), never AttributeError on a None row.
    mgr = HostedManager()
    inst = _orphan_instance()
    mgr._instances[inst.claustrum_process_id] = inst
    real_key_for = mgr._key_for

    def _resolve_then_evict(hid):
        key = real_key_for(hid)
        mgr._instances.pop(inst.claustrum_process_id, None)  # a concurrent op wins the row
        return key

    monkeypatch.setattr(mgr, "_key_for", _resolve_then_evict)
    with pytest.raises(HostedSessionError):
        await mgr.kill_orphan(inst.claustrum_process_id)


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
        await wait_until(lambda: mgr.get_instance(pid).daemon_last_seq > 0)
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


async def _emit_frames(fake, process_id, count):
    """Emit ``count`` trivial assistant frames, returning the last assigned seq."""
    seq = 0
    for n in range(count):
        payload = json.dumps({"type": "assistant", "n": n}).encode("utf-8") + b"\n"
        seq = await fake.emit(process_id, "stdout", payload)
    return seq


def _gap_events(session):
    """The `gap` markers currently in a session's ring, in order."""
    return [e for e in session._ring if e["type"] == "gap"]


async def test_reattach_reports_an_evicted_replay_range(fake_claustrum):
    # #1175: the daemon's capped replay buffer evicted frames past our cursor. The
    # range must be announced as a leading gap marker, not skipped.
    fake = await fake_claustrum()
    await _emit_frames(fake, _PID, 5)  # seqs 1..5
    fake.evict_through(_PID, 3)  # only 4 and 5 survive → firstSeq 4
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        result = await session.reattach(1)  # our persisted cursor was seq 1
        assert result["firstSeq"] == 4
        queue = session.subscribe()
        gap = await _drain(queue)
        # The marker LEADS the surviving replay, so a watcher sees the break in
        # position; from_seq/to_seq are the cursors either side of the hole.
        assert gap["type"] == "gap"
        assert (gap["from_seq"], gap["to_seq"]) == (1, 4)
        assert gap["event_seq"] == 1
        survivor = await _drain(queue)
        assert survivor["type"] == "frame" and survivor["frame"]["n"] == 3
        await wait_until(lambda: session.daemon_last_seq == 5)
        await session.detach()


async def test_reattach_with_a_complete_replay_reports_no_gap(fake_claustrum):
    # The overlap direction is unchanged: a fully-retained buffer emits no marker.
    fake = await fake_claustrum()
    await _emit_frames(fake, _PID, 3)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        result = await session.reattach(1)
        assert result["firstSeq"] == 1
        queue = session.subscribe()
        first = await _drain(queue)
        assert first["type"] == "frame"  # no gap marker ahead of the replay
        await session.detach()


async def test_reattach_gap_is_logged_with_its_exact_range(fake_claustrum, caplog):
    # A silent skip was the bug; the log names the lost range and the process.
    fake = await fake_claustrum()
    await _emit_frames(fake, _PID, 5)
    fake.evict_through(_PID, 3)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        with caplog.at_level(logging.WARNING, logger="clauster.hosted"):
            await session.reattach(1)
        assert "evicted frames 2-3" in caplog.text
        assert _PID in caplog.text
        await session.detach()


async def test_reattach_ignores_a_non_int_first_seq(fake_claustrum, monkeypatch):
    # firstSeq is untrusted daemon JSON: a non-int must not be read as a gap.
    fake = await fake_claustrum()
    await _emit_frames(fake, _PID, 2)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        real = client.reattach

        async def _bogus(process_id, from_seq=0):
            return {**(await real(process_id, from_seq)), "firstSeq": "nope"}

        monkeypatch.setattr(client, "reattach", _bogus)
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(0)
        assert not _gap_events(session)  # nothing reported
        await session.detach()


async def test_reattach_ignores_a_bool_first_seq(fake_claustrum, monkeypatch):
    # bool subclasses int: a daemon answering `true` must not be read as seq 1.
    fake = await fake_claustrum()
    await _emit_frames(fake, _PID, 2)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        real = client.reattach

        async def _bool_first(process_id, from_seq=0):
            return {**(await real(process_id, from_seq)), "firstSeq": True}

        monkeypatch.setattr(client, "reattach", _bool_first)
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(0)
        assert not _gap_events(session)
        await session.detach()


async def test_reattach_to_an_empty_replay_buffer_reports_no_gap(fake_claustrum):
    # An empty buffer puts firstSeq 0 on the wire (claustrum assigns it only when the
    # buffer is non-empty). That is genuinely "nothing was emitted", not a missed gap:
    # eviction only ever happens on append and always keeps the frame just added.
    fake = await fake_claustrum()
    await _emit_frames(fake, _PID, 3)
    fake.evict_through(_PID, 3)  # total eviction — nothing survives
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        result = await session.reattach(3)  # our cursor already covered all three
        assert result["firstSeq"] == 0
        assert not _gap_events(session)
        await session.detach()


async def test_a_reported_gap_advances_the_cursor_synchronously(fake_claustrum):
    # The review catch: the advance must be atomic with the REPORT, not left to the
    # pump's first drained frame — a crash in that window re-reported the same gap
    # and re-replayed the survivors on the next restart. So the cursor is already
    # past the hole the moment reattach() returns, before anything drains.
    fake = await fake_claustrum()
    await _emit_frames(fake, _PID, 5)
    fake.evict_through(_PID, 3)  # firstSeq 4, frames 2..3 evicted
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(1)
        assert len(_gap_events(session)) == 1
        assert session.daemon_last_seq >= 3, "cursor must clear the hole at report time"
        await wait_until(lambda: session.daemon_last_seq == 5)  # survivors still drain
        await session.detach()


async def test_manager_reattach_all_surfaces_an_evicted_range(fake_claustrum, tmp_path):
    # The startup sweep goes through HostedSession.reattach, so it inherits the fix.
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = await _spawn_gen1(fake, store)  # persisted cursor = seq 1
    await _emit_frames(fake, pid, 3)  # seqs 2..4 landed while we were down
    fake.evict_through(pid, 3)  # firstSeq 4 → frames 2..3 evicted
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client)
        session = mgr.session(pid)
        assert (await _drain(session.subscribe()))["type"] == "gap"


# -- transcript rehydration on reattach (#1045) ----------------------------


_HISTORY = [
    {"role": "user", "content": "add a changelog", "model": None, "timestamp": "t1"},
    {"role": "assistant", "content": "done", "model": "claude", "timestamp": "t2"},
]


def _frames(session):
    """The `frame` payloads currently in a session's ring, in order."""
    return [e["frame"] for e in session._ring if e["type"] == "frame"]


async def _seed_process(fake, process_id, count):
    """Buffer ``count`` assistant frames on the fake daemon for ``process_id``."""
    for n in range(count):
        payload = json.dumps({"type": "assistant", "n": n}).encode("utf-8") + b"\n"
        await fake.emit(process_id, "stdout", payload)


def _loader(turns):
    """A history_loader returning fixed ``turns`` (the reattach-time transcript read)."""

    async def _load():
        return turns

    return _load


async def test_reattach_rehydrates_prior_conversation(fake_claustrum):
    # #1045: a restart left the reattached view empty. The on-disk transcript is
    # restored into the ring, ahead of anything the daemon replays.
    fake = await fake_claustrum()
    await _seed_process(fake, _PID, 1)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(0, history_loader=_loader(_HISTORY))
        frames = _frames(session)
        assert frames[0] == {"type": "system", "subtype": "restored 2 turns from transcript"}
        # Rendered in the same shape the live stream uses, so the browser needs no
        # special case for restored turns.
        user_turn = {"role": "user", "content": "add a changelog"}
        assert frames[1] == {"type": "user", "message": user_turn}
        assert frames[2] == {
            "type": "assistant",
            "message": {"role": "assistant", "content": "done"},
        }
        await session.detach()


async def test_reattach_without_a_history_loader_restores_nothing(fake_claustrum):
    # Rehydration is opt-in: with no loader the replay renders exactly as it always did.
    fake = await fake_claustrum()
    await _seed_process(fake, _PID, 2)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(0)
        assert session._rehydrated_through == 0
        await wait_until(lambda: len(_frames(session)) == 2)  # both replayed frames
        assert all("subtype" not in f for f in _frames(session))  # no restored marker
        await session.detach()


async def test_frames_emitted_during_the_transcript_read_are_not_suppressed(fake_claustrum):
    # The snapshot-order invariant: the transcript is read AFTER the reattach RPC has
    # fixed lastSeq. A frame the agent emits WHILE we're reading therefore lands past
    # that cursor and renders. Reading first (the earlier design) would have snapshotted
    # the transcript without it, then suppressed its frame — shown nowhere, forever.
    fake = await fake_claustrum()
    await _seed_process(fake, _PID, 2)  # seqs 1..2 → lastSeq is 2 at RPC time
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)

        async def _load_slowly():
            await fake.emit(_PID, "stdout", b'{"type":"assistant","during_read":true}\n')
            return _HISTORY

        await session.reattach(0, history_loader=_load_slowly)
        assert session._rehydrated_through == 2  # the window closed before that frame
        await wait_until(lambda: any(f.get("during_read") for f in _frames(session)))
        await session.detach()


async def test_transcript_read_cannot_overflow_the_bounded_queue(fake_claustrum):
    # The review catch on the loader design: the pre-pump buffer is the CLIENT's
    # bounded stream-subscriber queue, and the transcript read takes real time —
    # frames arriving in that window used to overflow it before the pump existed,
    # dropping output (or a parked control_request, leaving claude waiting on a
    # prompt the dashboard never shows). reattach() now spills the queue into an
    # unbounded backlog while the read runs.
    fake = await fake_claustrum()
    await _emit_frames(fake, _PID, 1)  # seq 1: our persisted cursor
    async with ClaustrumClient(fake.socket_path, fake.token, queue_maxsize=8) as client:
        session = HostedSession(client, _PID, _BIN)

        async def _emits_while_reading():
            # One frame per loop turn, yielding between: the production loader is a
            # to_thread file read (a true suspension), so the read loop and the
            # spill interleave with the arriving frames — the in-process fake's
            # fast-path awaits would otherwise run everything in one task step.
            for _ in range(200):  # seqs 2..201 land mid-"file read"
                await _emit_frames(fake, _PID, 1)
                await asyncio.sleep(0)
            return None

        await session.reattach(1, history_loader=_emits_while_reading)
        await wait_until(lambda: session.daemon_last_seq == 201)
        # Nothing dropped: no overflow marker, and every emitted frame reached the
        # ring — a pre-pump drop would leave holes or a marker instead.
        assert not any(e["type"] == "overflow" for e in session._ring)
        assert len([f for f in _frames(session) if "n" in f]) == 200
        await session.detach()


async def test_rehydration_suppresses_the_replayed_data_frames_it_covers(fake_claustrum):
    # The seam: the daemon still buffers frames for turns the transcript already
    # holds. Restoring AND replaying them would double-render, so the replay's data
    # frames through the daemon's reattach-time lastSeq are dropped.
    fake = await fake_claustrum()
    await _seed_process(fake, _PID, 4)  # seqs 1..4, all covered by the transcript
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(1, history_loader=_loader(_HISTORY))
        assert session._rehydrated_through == 4
        await wait_until(lambda: session.daemon_last_seq == 4)  # the replay WAS drained
        assert len(_frames(session)) == 3  # marker + 2 restored turns, nothing replayed
        # ...and a LIVE frame past that cursor still renders normally.
        await fake.emit(_PID, "stdout", b'{"type":"assistant","live":true}\n')
        await wait_until(lambda: len(_frames(session)) == 4)
        assert _frames(session)[-1] == {"type": "assistant", "live": True}
        # Passing the window also CLEARS it, so a later in-window seq can't re-suppress.
        assert session._rehydrated_through == 0
        await session.detach()


async def test_rehydration_never_suppresses_a_result_frame(fake_claustrum):
    # A `result` frame has no `message`, so the transcript reader structurally cannot
    # regenerate it — and is_error is how a failed turn reaches the operator.
    # Suppressing it would swallow an error state (invariant 1).
    fake = await fake_claustrum()
    err = json.dumps({"type": "result", "is_error": True, "result": "Not logged in"})
    await fake.emit(_PID, "stdout", err.encode("utf-8") + b"\n")
    await _seed_process(fake, _PID, 1)  # a data frame that IS covered, for contrast
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(0, history_loader=_loader(_HISTORY))
        assert session._rehydrated_through == 2  # both frames are inside the window
        await wait_until(lambda: any(f.get("type") == "result" for f in _frames(session)))
        # ...while the ordinary data frame beside it stayed suppressed.
        assert not any(f.get("type") == "assistant" and "n" in f for f in _frames(session))
        await session.detach()


async def test_rehydration_rejects_a_bool_last_seq(fake_claustrum, monkeypatch):
    # bool subclasses int: a daemon answering `true` must not bound the window at 1.
    fake = await fake_claustrum()
    await _seed_process(fake, _PID, 1)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        real = client.reattach

        async def _bool_last_seq(process_id, from_seq=0):
            return {**(await real(process_id, from_seq)), "lastSeq": True}

        monkeypatch.setattr(client, "reattach", _bool_last_seq)
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(0, history_loader=_loader(_HISTORY))
        assert session._rehydrated_through == 0  # suppress nothing
        await wait_until(lambda: len(_frames(session)) == 4)  # marker + 2 turns + replay
        await session.detach()


async def test_rehydration_only_renders_whitelisted_roles(fake_claustrum):
    # The role becomes the frame `type` the browser dispatches on, so an unexpected
    # role must not be able to render as a control frame.
    fake = await fake_claustrum()
    await _seed_process(fake, _PID, 1)
    history = [
        {"role": "control_request", "content": "not a real turn"},
        {"role": "assistant", "content": "a real turn"},
    ]
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(0, history_loader=_loader(history))
        frames = _frames(session)
        assert [f["type"] for f in frames] == ["system", "assistant"]
        await session.detach()


async def test_rehydration_never_suppresses_control_requests_or_stderr(fake_claustrum):
    # Fail-closed: a permission prompt raised while we were down is still unanswered,
    # so it must survive the seam. stderr isn't in the transcript either.
    fake = await fake_claustrum()
    prompt = json.dumps(
        {"type": "control_request", "request_id": "r1", "request": {"subtype": "can_use_tool"}}
    ).encode("utf-8")
    await fake.emit(_PID, "stdout", prompt + b"\n")
    await fake.emit(_PID, "stderr", b"a warning\n")
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(0, history_loader=_loader(_HISTORY))
        assert session._rehydrated_through == 2  # both frames are inside the window
        await wait_until(lambda: [p.request_id for p in session.pending_requests] == ["r1"])
        types = await wait_until(
            lambda: [e["type"] for e in session._ring if e["type"] != "frame"] or None
        )
        assert types == ["control_request", "stderr"]
        await session.detach()


async def test_rehydration_caps_the_restored_turns(fake_claustrum):
    # An unbounded transcript must not be poured into the bounded ring.
    fake = await fake_claustrum()
    await _seed_process(fake, _PID, 1)
    history = [{"role": "user", "content": f"turn {n}"} for n in range(250)]
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(0, history_loader=_loader(history))
        frames = _frames(session)
        assert frames[0]["subtype"] == "restored 200 of 250 turns from transcript"
        assert len(frames) == 1 + hosted._REHYDRATE_MAX_TURNS
        assert frames[1]["message"]["content"] == "turn 50"  # the NEWEST 200, not the oldest
        await session.detach()


async def test_rehydration_skips_unrenderable_turns(fake_claustrum):
    # A malformed/empty transcript record must be skipped, not emitted as a blank bubble.
    fake = await fake_claustrum()
    await _seed_process(fake, _PID, 1)
    history = [
        {"role": None, "content": "no role"},
        {"role": "user", "content": ""},
        {"role": "user", "content": None},
        {"role": "user", "content": "the only real turn"},
    ]
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(0, history_loader=_loader(history))
        frames = _frames(session)
        assert len(frames) == 2  # the marker plus one renderable turn
        assert frames[1]["message"]["content"] == "the only real turn"
        await session.detach()


async def test_rehydration_without_a_usable_last_seq_suppresses_nothing(
    fake_claustrum, monkeypatch
):
    # lastSeq is untrusted daemon JSON. On an unusable cursor the safe direction is a
    # possible duplicate, never dropped output — so nothing is suppressed.
    fake = await fake_claustrum()
    await _seed_process(fake, _PID, 2)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        real = client.reattach

        async def _no_last_seq(process_id, from_seq=0):
            return {**(await real(process_id, from_seq)), "lastSeq": None}

        monkeypatch.setattr(client, "reattach", _no_last_seq)
        session = HostedSession(client, _PID, _BIN)
        await session.reattach(0, history_loader=_loader(_HISTORY))
        assert session._rehydrated_through == 0
        # marker + 2 restored turns + both replayed frames
        await wait_until(lambda: len(_frames(session)) == 5)
        await session.detach()


async def test_manager_reattach_all_rehydrates_from_the_history_resolver(fake_claustrum, tmp_path):
    # The startup sweep hands each row's transcript to reattach.
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = await _spawn_gen1(fake, store)
    seen: list[str] = []

    def _history_for(instance):
        seen.append(instance.claustrum_process_id)
        return _HISTORY

    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client, history_for=_history_for)
        assert seen == [pid]
        session = mgr.session(pid)
        assert [f.get("message", {}).get("content") for f in _frames(session)[1:]] == [
            "add a changelog",
            "done",
        ]
        await mgr.aclose()


async def test_manager_reattach_all_survives_a_failing_history_resolver(
    fake_claustrum, tmp_path, caplog
):
    # Rehydration is a convenience layered on reattach: an unreadable transcript
    # degrades to today's empty view, it never fails the reattach.
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = await _spawn_gen1(fake, store)

    def _boom(instance):
        # Not an OSError: this runs in the startup lifespan, so ANY resolver fault —
        # a decode error, a MemoryError on an oversized transcript, a resolver bug —
        # must degrade to "no history" rather than abort startup.
        raise MemoryError("transcript too large to parse")

    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        with caplog.at_level(logging.WARNING, logger="clauster.hosted"):
            restored = await mgr.reattach_all(client, history_for=_boom)
        assert [i.claustrum_process_id for i in restored] == [pid]
        assert mgr.get_instance(pid).status is InstanceStatus.RUNNING  # reattach still worked
        assert "could not restore the transcript" in caplog.text
        assert _frames(mgr.session(pid)) == []
        await mgr.aclose()


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


async def test_manager_reattach_all_survives_a_junk_persisted_cursor(fake_claustrum, tmp_path):
    """A junk ``daemon_last_seq`` on disk must not abort reattach — or the app boot.

    ``reattach_all`` re-derived the cursor from the raw record with a bare ``int()``,
    so a persisted ``"abc"`` raised ValueError. That is not a ClaustrumError, so it
    escaped both the handler here and the lifespan's in ``app.py``: clauster failed to
    start. The cursor now comes from the already-coerced instance (one coercion site),
    degrading to 0 — replay the whole retained window rather than lose the session.
    """
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = await _spawn_gen1(fake, store)
    records = store.load()
    records[pid]["daemon_last_seq"] = "abc"  # e.g. a legacy import that kept it as TEXT
    store.save(records)
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        restored = await mgr.reattach_all(client)  # the regression: this used to raise
        assert [i.claustrum_process_id for i in restored] == [pid]
        inst = mgr.get_instance(pid)
        assert inst.status is InstanceStatus.RUNNING
        # Pins the semantics, not just the absence of a raise: the junk cursor coerced
        # to 0, so reattach replayed the WHOLE retained window — generation 1's frame
        # comes back — rather than resuming from a guessed position.
        init = {"type": "system", "subtype": "init"}
        await wait_until(lambda: _frames(mgr.session(pid)) == [init])
        await mgr.aclose()


def test_record_projects_instance_id():
    """``_record`` includes ``instance_id`` so a save doesn't drop it (#841)."""
    inst = RemoteControlInstance(
        project="proj", label="hosted:proj", channel="hosted", claustrum_process_id=_PID
    )
    record = HostedManager._record(inst)
    assert record["instance_id"] == inst.instance_id


def test_instance_from_record_restores_persisted_instance_id():
    """A persisted ``instance_id`` is restored onto the rebuilt row (#841).

    Guards the exact regression: pre-#841, ``_instance_from_record`` never set
    ``instance_id``, so the model's ``default_factory`` minted a fresh one on every
    restart and a client's cached id 404'd.
    """
    persisted_iid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    inst = HostedManager._instance_from_record(
        _PID, {"project": "proj", "label": "hosted:proj", "instance_id": persisted_iid}
    )
    assert inst.instance_id == persisted_iid


def test_instance_from_record_without_instance_id_mints_a_fresh_one():
    """Backward compat: a record with no ``instance_id`` (older save, or a
    pre-migration DB row) still loads — the model's ``default_factory`` mints a
    fresh id exactly as it did before #841, never a crash or a blank id.
    """
    inst = HostedManager._instance_from_record(_PID, {"project": "proj", "label": "hosted:proj"})
    assert inst.instance_id  # minted, non-empty
    assert inst.instance_id != "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


@pytest.mark.parametrize(
    "junk",
    [
        "abc",  # ValueError: invalid literal for int()
        {"x": 1},  # TypeError: int() argument must be a string...
        [1, 2],  # TypeError
        float("nan"),  # ValueError: cannot convert float NaN to integer
        float("inf"),  # OverflowError: cannot convert float infinity to integer
        True,  # bool subclasses int; `true` would read as seq 1 and SKIP frame 1
        -5,  # a negative cursor makes _note_replay_gap report a fabricated eviction
        "-5",
    ],
)
def test_instance_from_record_tolerates_junk_daemon_last_seq(junk):
    """A junk ``daemon_last_seq`` in ``hosted_state.json`` degrades to 0, never raises.

    This covers ``_instance_from_record``'s handling of that ONE field of the on-disk
    record map; its other persisted fields are covered by the ValidationError guard
    exercised below (#1343). Coercing the cursor here rather than leaning on that
    guard is what keeps a junk cursor from costing the row the REST of its metadata.
    The bare ``int(...)`` was not total:
    each value below aborted the whole reattach on restart with a different exception.
    0 means "replay from the start of the retained window" — the fail-visible
    direction, versus the session silently vanishing.
    """
    inst = HostedManager._instance_from_record(
        _PID, {"project": "proj", "label": "hosted:proj", "daemon_last_seq": junk}
    )
    assert inst.daemon_last_seq == 0


def test_instance_from_record_keeps_a_valid_daemon_last_seq():
    # The guard must not flatten a real cursor: an int and its digit-string form both
    # survive, so a legitimate reattach still resumes where it left off.
    for value, expected in ((42, 42), ("42", 42), (None, 0), (0, 0)):
        inst = HostedManager._instance_from_record(
            _PID, {"project": "proj", "label": "hosted:proj", "daemon_last_seq": value}
        )
        assert inst.daemon_last_seq == expected


# -- #1343: a record the model rejects degrades its own row, not the whole boot ----


#: Records whose value is well-formed JSON but the wrong type for the model. Each is a
#: value a hand-edited (or partially-written) ``hosted_state.json`` can hold.
_REJECTED_RECORDS = [
    pytest.param({"project": {}}, id="project-not-a-string"),
    pytest.param({"label": 7}, id="label-not-a-string"),
    pytest.param({"permission_mode": "nope"}, id="permission-mode-not-in-the-literal"),
    # Truthy on purpose: an empty list is the one non-string `_synced` skips anyway, so
    # it would let the uuid path below pass vacuously.
    pytest.param({"claude_session_uuid": ["not-a-uuid"]}, id="uuid-not-a-string"),
    pytest.param({"agent_proc_start": {}}, id="proc-start-not-a-float"),
]


def test_a_junk_agent_pid_never_reaches_the_model_on_either_path():
    """``agent_pid`` is coerced identically by both mappings, so they cannot disagree.

    Defense in depth rather than a second guard for its own sake. Pydantic's lax
    coercion accepts ``true`` as pid **1** — init on every POSIX host — and ``"4242"``
    as 4242, while the salvage takes plain ints only. Left asymmetric, a record that
    degraded for an unrelated reason would drop a pid the healthy path would have kept,
    and ``_persist`` would then write that loss to disk — costing ``_is_orphan`` the
    only evidence it has.
    """
    for junk in ("4242", True, 4242.0, {}):
        record = {"project": "proj", "label": "hosted:proj", "agent_pid": junk}
        assert HostedManager._row_from_record(_PID, record).agent_pid is None
        assert HostedManager._degraded_row(_PID, record).agent_pid is None
    kept = {"project": "proj", "label": "hosted:proj", "agent_pid": 4242}
    assert HostedManager._row_from_record(_PID, kept).agent_pid == 4242
    assert HostedManager._degraded_row(_PID, kept).agent_pid == 4242


@pytest.mark.parametrize("junk", _REJECTED_RECORDS)
def test_instance_from_record_tolerates_a_record_the_model_rejects(junk):
    """A persisted value pydantic refuses degrades the row instead of raising (#1343).

    ``ValidationError`` is not a ``ClaustrumError``, so pre-fix it escaped both
    ``reattach_all``'s per-session handler and the lifespan's in ``app.py`` — one
    corrupt record failed clauster's whole boot. The first assertion is the
    reproducer: it pins that the raw mapping really does still reject the value, so
    the second assertion cannot pass vacuously if the fixture stops being junk.
    """
    record = {"project": "proj", "label": "hosted:proj", **junk}
    with pytest.raises(ValidationError):
        HostedManager._row_from_record(_PID, record)

    inst = HostedManager._instance_from_record(_PID, record)
    assert inst.claustrum_process_id == _PID  # still reattachable — that is the point
    assert inst.channel == "hosted"


#: A record where every field is usable except ``project``. Low-entropy placeholder
#: values throughout — a fixture is scanned by gitleaks like any other committed line.
_ONE_BAD_FIELD = {
    "project": {},  # the only value the model rejects
    "label": "hosted:proj",
    "permission_mode": "acceptEdits",
    "claude_session_uuid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    "daemon_last_seq": 12,
    "hosted_log_path": "/tmp/proj/hosted.log",
    "agent_pid": 4242,
    "agent_proc_start": 1234.5,
    "started_at": "2026-08-30T10:00:00",
    "intentional_stop": True,
    "instance_id": "11111111-2222-4333-8444-555555555555",
}


def test_degraded_row_salvages_every_field_the_model_did_not_reject():
    """Only the rejected value is reset; the other ten survive.

    Not cosmetic. ``reattach_all`` ends in a ``_persist`` that rewrites the record from
    this row, so a wholesale default would DESTROY the recoverable fields on the first
    boot after corruption — turning a repairable state file into an unrepairable one,
    the opposite of fail-visible. ``agent_pid``/``agent_proc_start`` matter twice over:
    they are the only evidence ``_is_orphan`` has (see the CL-8 test below).
    """
    inst = HostedManager._instance_from_record(_PID, _ONE_BAD_FIELD)
    assert inst.project == ""  # the rejected field, and only it, falls back
    assert inst.label == "hosted:proj"
    assert inst.permission_mode == "acceptEdits"
    assert inst.claude_session_uuid == "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert inst.daemon_last_seq == 12
    assert inst.hosted_log_path == Path("/tmp/proj/hosted.log")
    assert inst.agent_pid == 4242
    assert inst.agent_proc_start == 1234.5
    assert inst.started_at == datetime(2026, 8, 30, 10, 0)
    assert inst.intentional_stop is True
    assert inst.instance_id == "11111111-2222-4333-8444-555555555555"


@pytest.mark.parametrize(
    ("field", "junk", "expected"),
    [
        ("agent_pid", True, None),  # bool subclasses int — a persisted `true` is not pid 1
        ("agent_pid", 1.5, None),
        ("agent_proc_start", True, None),
        ("agent_proc_start", 10**400, None),  # int too large for a float
        ("claude_session_uuid", ["not-a-uuid"], None),
        ("hosted_log_path", 7, None),
        ("permission_mode", "nope", "default"),
        ("permission_mode", {}, "default"),  # unhashable: `in frozenset` would raise
        ("started_at", "not-a-date", None),
        ("instance_id", "", None),  # falsy: a fresh id is minted instead
    ],
)
def test_degraded_row_drops_a_value_it_cannot_type_check(field, junk, expected):
    # The salvage is a type test per field, never a pass-through: a second junk value
    # alongside the rejecting one is dropped rather than smuggled onto the model by
    # an unvalidated assignment. Each expected value is the field's own model default,
    # spelled out per case so a regression cannot hide behind a disjunction.
    inst = HostedManager._instance_from_record(_PID, {**_ONE_BAD_FIELD, field: junk})
    if field == "instance_id":
        assert isinstance(inst.instance_id, str) and inst.instance_id
        assert inst.instance_id != _ONE_BAD_FIELD["instance_id"]
        return
    assert getattr(inst, field) == expected


@pytest.mark.parametrize(
    "record",
    [
        pytest.param({"permission_mode": {}}, id="unhashable-permission-mode"),
        pytest.param({"permission_mode": ["acceptEdits"]}, id="unhashable-mode-list"),
        pytest.param({"agent_proc_start": 10**400}, id="proc-start-overflows-a-float"),
        pytest.param({"agent_proc_start": -(10**400)}, id="proc-start-underflows-a-float"),
    ],
)
def test_degraded_row_cannot_raise_on_the_values_that_bypass_validation_error(record):
    """The salvage must not reopen the escape it exists to close.

    Two coercions in the salvage are not ``isinstance`` tests and can raise on a value
    pydantic would merely reject: ``value in frozenset`` HASHES its operand
    (``TypeError: unhashable type``) and ``float()`` overflows on a large int
    (``OverflowError``). Neither is a ``ValidationError``, so neither is caught by
    ``_instance_from_record``, ``reattach_all`` or the lifespan — a record holding one
    would fail clauster's boot exactly as #1343 describes.
    """
    inst = HostedManager._instance_from_record(_PID, {**_ONE_BAD_FIELD, **record})
    assert inst.claustrum_process_id == _PID


def test_degraded_row_defaults_unusable_display_strings():
    # Both required strings are junk, so both fall back — the label to the same
    # `hosted:<pid prefix>` default the healthy mapping uses.
    inst = HostedManager._instance_from_record(_PID, {"project": {}, "label": 7})
    assert inst.project == ""
    assert inst.label == f"hosted:{_PID[:8]}"


def test_degraded_row_carries_the_reason_on_the_row():
    # The journal warning is for whoever tails logs; error_detail is the same fact for
    # whoever is looking at the dashboard. Degrading has to be visible on both.
    inst = HostedManager._instance_from_record(_PID, {"project": {}, "label": "hosted:proj"})
    assert "unreadable" in (inst.error_detail or "")


def test_instance_from_record_logs_the_degradation(caplog):
    """Degrading must be visible, never silent (invariant 1).

    The warning names the process id and the offending field so an operator can find
    the row, and carries pydantic's error CODE rather than the rejected value — a log
    file is not a redacted surface, so the value must not travel into it.
    """
    with caplog.at_level(logging.WARNING, logger="clauster.hosted"):
        HostedManager._instance_from_record(
            _PID, {"project": {"placeholder-key": "placeholder-value"}, "label": "hosted:proj"}
        )
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert _PID in message
    assert "project" in message
    assert "unreadable" in message
    assert "placeholder-value" not in message  # the rejected value never reaches the log


def test_instance_from_record_ignores_a_non_string_instance_id(caplog):
    """A non-string ``instance_id`` is dropped — and said out loud, not dropped silently.

    The mapper sets this one AFTER construction, so pydantic never sees it (the model
    does not enable ``validate_assignment``) — the ValidationError guard cannot catch it
    and a hand-edited record would otherwise put a non-string key in the registry.
    Dropping it costs a client its cached id (#841), so it warrants a warning of its own.
    """
    with caplog.at_level(logging.WARNING, logger="clauster.hosted"):
        inst = HostedManager._instance_from_record(
            _PID, {"project": "proj", "label": "hosted:proj", "instance_id": {"x": 1}}
        )
    assert isinstance(inst.instance_id, str) and inst.instance_id
    message = caplog.records[0].getMessage()
    assert _PID in message
    assert "dict" in message  # the TYPE, never the value


async def test_manager_reattach_survives_one_unreadable_record(fake_claustrum, tmp_path):
    """A corrupt record must not orphan the live session it belongs to, nor its peers.

    The end-to-end shape of #1343: generation 1 spawns a real session, the persisted
    record is then hand-corrupted, and generation 2 still reattaches it — pre-fix
    ``reattach_all`` raised ``ValidationError`` out of the loop, so neither this
    session nor any other was reattached and the daemon-owned agent was orphaned.
    """
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = await _spawn_gen1(fake, store)
    records = store.load()
    persisted_iid = records[pid]["instance_id"]
    records[pid]["label"] = 7  # what a hand-edited state file can hold
    records["01GONEPROCESS00000000000"] = {"project": "proj", "label": "hosted:b"}
    store.save(records)

    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client)
        assert mgr.session(pid) is not None  # reattached despite the corrupt field
        assert mgr.get_instance(pid).status is not InstanceStatus.CRASHED
        # The healthy neighbour was processed too — one bad record is not global.
        assert mgr.get_instance("01GONEPROCESS00000000000") is not None
        await mgr.aclose()

    # `reattach_all` persists, so the file is rewritten from the degraded row. Only
    # the field that was junk may have changed — anything else being reset here would
    # mean the first boot after corruption destroyed a recoverable value.
    rewritten = store.load()[pid]
    assert rewritten["label"] == f"hosted:{pid[:8]}"  # the junk one, reset to its default
    assert rewritten["project"] == "proj"
    assert rewritten["permission_mode"] == "acceptEdits"
    assert rewritten["instance_id"] == persisted_iid
    assert rewritten["daemon_last_seq"] >= 1  # the replay cursor survived intact


async def test_manager_reattach_degraded_row_can_still_be_an_orphan(
    fake_claustrum, tmp_path, monkeypatch
):
    """A degraded row keeps the CL-8 orphan evidence, so Resume/Kill stay reachable.

    ``_is_orphan`` has only ``agent_pid`` + ``agent_proc_start`` to go on. If the
    salvage dropped them, a survivor of a daemon restart would be filed as "session
    lost" and ``forget`` — which refuses only for ``is_orphan`` rows — would then throw
    away clauster's last record of a live ``claude`` process.
    """
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    monkeypatch.setattr(hosted.procutil, "is_killable_hosted", lambda pid, start: True)
    store.save(
        {
            "01GONEPROCESS00000000000": {
                "project": {},  # rejected by the model → degraded row
                "label": "hosted:proj",
                "agent_pid": 4242,
                "agent_proc_start": 1234.5,
            }
        }
    )
    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client)
        inst = mgr.get_instance("01GONEPROCESS00000000000")
        assert inst.status is InstanceStatus.CRASHED  # the daemon doesn't know it
        assert inst.is_orphan is True  # ...but the pid evidence survived the degrade
        assert "Resume to recover" in (inst.error_detail or "")


async def test_manager_reattach_never_takes_the_session_uuid_from_the_raw_record(
    fake_claustrum, tmp_path
):
    """A non-string persisted uuid must not reach the live session (invariant 2).

    ``reattach_all`` used to read ``claude_session_uuid`` straight out of the record,
    bypassing the mapper's type check and the model alike (assignments are
    unvalidated). A truthy non-string then latched ``_capture_session_uuid`` shut — the
    real id from the replayed init frame was discarded for the process lifetime — and
    reached ``build_hosted_argv``'s ``--resume``, i.e. a rejected persisted value in
    spawn argv.
    """
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = await _spawn_gen1(fake, store)
    records = store.load()
    records[pid]["claude_session_uuid"] = ["not-a-uuid"]
    store.save(records)

    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client)
        assert mgr.session(pid).claude_session_uuid is None  # dropped, not smuggled
        # ...so the latch is open and a replayed init frame still supplies the real id.
        await fake.emit(pid, "stdout", b'{"type":"system","subtype":"init","session_id":"s-1"}\n')
        await wait_until(lambda: mgr.session(pid).claude_session_uuid == "s-1")
        await mgr.aclose()
    assert store.load()[pid]["claude_session_uuid"] == "s-1"


async def test_manager_reattach_restores_persisted_instance_id(fake_claustrum, tmp_path):
    """A hosted session's ``instance_id`` survives a persist → reattach cycle.

    Regression guard for #841: pre-fix, ``reattach_all`` re-minted a fresh
    ``instance_id`` on every restart even though the JSON store round-tripped it,
    because ``_instance_from_record`` silently dropped the field. A client that
    resolved the row via its (now stale) cached instance_id would 404 after a
    restart despite the underlying session having reattached successfully.
    """
    fake = await fake_claustrum()
    store = HostedStateStore(tmp_path)
    pid = await _spawn_gen1(fake, store)
    persisted_iid = store.load()[pid]["instance_id"]
    assert persisted_iid  # the JSON store round-tripped it (generation 1 already closed)

    async with ClaustrumClient(fake.socket_path, fake.token) as client:
        mgr = HostedManager(store)
        await mgr.reattach_all(client)
        inst = mgr.get_instance(pid)
        assert inst.instance_id == persisted_iid
        # The lifecycle lookup by instance_id (#834/#840) still resolves post-restart.
        assert mgr.get_instance(persisted_iid) is not None
        assert mgr.get_instance(persisted_iid).claustrum_process_id == pid
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


async def test_pump_overflow_event_emits_gap(fake_claustrum):
    # An "overflow" event on the source stream (the daemon dropped N frames for a slow
    # client) is surfaced to watchers as a {"type": "gap", "dropped": N} marker, not
    # swallowed, so the UI can flag the discontinuity.
    async with _session(fake_claustrum) as (_fake, session):
        queue = session.subscribe()
        session._source.put_nowait({"type": "overflow", "dropped": 3})
        gap = await _drain_until(queue, "gap")
        assert gap["type"] == "gap"
        assert gap["dropped"] == 3


async def test_pump_ignores_unknown_event_type(fake_claustrum):
    # An event whose type is neither line/exit/overflow falls through the dispatch and is
    # skipped without crashing; the pump keeps draining, so a following overflow still
    # surfaces its gap. Covers the elif-chain fall-through arc.
    async with _session(fake_claustrum) as (_fake, session):
        queue = session.subscribe()
        session._source.put_nowait({"type": "heartbeat", "seq": 1})
        session._source.put_nowait({"type": "overflow", "dropped": 2})
        gap = await _drain_until(queue, "gap")
        assert gap["dropped"] == 2


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
    await wait_until(lambda: mgr.get_instance(pid).status is InstanceStatus.CRASHED)
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
        assert old_id not in mgr._id_locks  # resume prunes the retired id's lock
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
        await wait_until(lambda: mgr.get_instance(old_id).status is InstanceStatus.CRASHED)
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
        await wait_until(lambda: mgr.get_instance(pid).status is InstanceStatus.CRASHED)
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


# -- per-id lifecycle serialization (#734) ---------------------------------


async def test_manager_concurrent_resume_yields_one_process(fake_claustrum, monkeypatch):
    # The #715-shaped race, structurally: two concurrent resume(id) must NOT both pass
    # the running-check and both _spawn_session (two live processes for one
    # conversation). The per-id lock serializes them — the first resumes, retires the
    # row; the second blocks at the lock, then sees the row gone and 409s. Exactly one
    # fresh spawn results.
    uuid = "11111111-2222-4333-8444-555555555555"
    async with _manager(fake_claustrum) as (fake, client, mgr):
        old_id = await _crash_with_uuid(fake, mgr, client, uuid)
        spawns_before = len(fake.spawned)

        # Gate the first resume INSIDE its critical section so the second resume is
        # provably racing it at the lock, not merely running after it finished.
        entered = asyncio.Event()
        release = asyncio.Event()
        real_spawn = mgr._spawn_session

        async def gated_spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real_spawn(*args, **kwargs)

        monkeypatch.setattr(mgr, "_spawn_session", gated_spawn)

        first = asyncio.create_task(
            mgr.resume(client, old_id, cwd="/tmp/proj", claude_binary=_BIN)
        )
        await asyncio.wait_for(entered.wait(), timeout=1.0)  # first holds the lock + is spawning
        second = asyncio.create_task(
            mgr.resume(client, old_id, cwd="/tmp/proj", claude_binary=_BIN)
        )
        # The second is blocked at the per-id lock: it has not (and must not) start a
        # second spawn while the first holds the section.
        await asyncio.sleep(0.05)
        assert not second.done()
        assert len(fake.spawned) == spawns_before  # neither spawn completed yet

        release.set()
        resumed = await asyncio.wait_for(first, timeout=1.0)
        with pytest.raises(HostedSessionError):  # row already retired → no double-spawn
            await asyncio.wait_for(second, timeout=1.0)

        assert len(fake.spawned) == spawns_before + 1  # exactly ONE fresh process
        assert mgr.get_instance(resumed.claustrum_process_id) is not None
        assert mgr.get_instance(old_id) is None
        await mgr.aclose()


async def test_manager_forget_then_resume_serialize(fake_claustrum, monkeypatch):
    # forget+resume on the same id is the cross-method shape of the same race: the
    # registry-pop and the spawn must not interleave. Whichever wins the lock runs to
    # completion; the loser sees the post-mutation state. Exactly one outcome, never a
    # spawned process left orphaned behind a forgotten row.
    uuid = "11111111-2222-4333-8444-555555555555"
    async with _manager(fake_claustrum) as (fake, client, mgr):
        old_id = await _crash_with_uuid(fake, mgr, client, uuid)
        spawns_before = len(fake.spawned)

        entered = asyncio.Event()
        release = asyncio.Event()
        real_spawn = mgr._spawn_session

        async def gated_spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real_spawn(*args, **kwargs)

        monkeypatch.setattr(mgr, "_spawn_session", gated_spawn)

        resume_task = asyncio.create_task(
            mgr.resume(client, old_id, cwd="/tmp/proj", claude_binary=_BIN)
        )
        await asyncio.wait_for(entered.wait(), timeout=1.0)  # resume holds the lock
        forget_task = asyncio.create_task(mgr.forget(old_id))
        await asyncio.sleep(0.05)
        assert not forget_task.done()  # forget waits behind the lock, can't pop mid-resume

        release.set()
        resumed = await asyncio.wait_for(resume_task, timeout=1.0)
        # forget now runs against the retired old row → unknown id → raises (the resumed
        # row lives under a NEW id, so it is never forgotten out from under its process).
        with pytest.raises(HostedSessionError):
            await asyncio.wait_for(forget_task, timeout=1.0)

        assert len(fake.spawned) == spawns_before + 1
        assert mgr.get_instance(resumed.claustrum_process_id) is not None
        await mgr.aclose()


def test_lock_for_returns_same_lock_per_id():
    # The get-or-create is synchronous (no await between .get and the assignment), so two
    # callers for the same id always observe the SAME lock object — the lock-creation race
    # that would let two coroutines each mint a private lock can't happen.
    mgr = HostedManager()
    a = mgr._lock_for("id-1")
    b = mgr._lock_for("id-1")
    c = mgr._lock_for("id-2")
    assert a is b
    assert a is not c


def test_redact_obj_sanitizes_nested_string_leaves():
    # Direct coverage for the structured-frame redactor (#549): every string leaf at any
    # depth is sanitized; non-string scalars pass through with type + value intact.
    frame = {
        "type": "assistant",
        "text": "worker cse_01XYZABCDEFGHIJ started",
        "meta": {"session": "session_01ZZZZZZZZZZZZZZZZZZZZZZ", "n": 3, "ok": True},
        "items": ["env_01BCDEFGHIJKLMNOPQRSTUVWX", 42, None],
    }
    out = _redact_obj(frame)
    assert "cse_01" not in out["text"]
    assert "session_01" not in out["meta"]["session"]
    assert "env_01" not in out["items"][0]
    # Non-string scalars are returned unchanged (type preserved).
    assert out["meta"]["n"] == 3
    assert out["meta"]["ok"] is True
    assert out["items"][1] == 42
    assert out["items"][2] is None


def test_redact_obj_passes_through_bare_scalars():
    # Only string leaves are sanitized; a bare non-str scalar is returned as-is, and a bare
    # secret string is still masked.
    assert _redact_obj(7) == 7
    assert _redact_obj(True) is True
    assert _redact_obj(None) is None
    assert "sk-" not in _redact_obj("token sk-ABCDEFGHIJKLMNOPQRST rest")


def test_redact_obj_truncates_past_the_depth_cap_instead_of_raising():
    """A frame nested past the cap is truncated, never a RecursionError.

    ``json.loads`` in ``_on_line`` parses frames nested far deeper than this recursive
    walker could descend, so a deeply-nested frame raised RecursionError inside
    ``_pump`` — which catches only ``CancelledError``/``ClaustrumError``. The pump task
    died and the session went dark with **no** ``lost`` event: a fail-silent, which
    invariant 1 forbids. The redactor must still return a value (invariant 4: nothing
    unredacted may reach a subscriber), so the over-deep subtree is replaced wholesale.
    """
    frame: dict = {}
    cursor = frame
    for _ in range(5_000):  # far past both the cap and CPython's recursion limit
        cursor["next"] = {"leak": "session_01ZZZZZZZZZZZZZZZZZZZZZZ"}
        cursor = cursor["next"]

    out = _redact_obj(frame)  # the regression: this used to raise RecursionError

    # Walk down to the cap: every level within it is a real dict whose leaf is masked.
    cursor = out
    for _ in range(hosted._REDACT_MAX_DEPTH - 1):
        cursor = cursor["next"]
        assert "session_01" not in cursor["leak"]
    # One level further is the constant marker — no input bytes survive past the cap.
    assert cursor["next"] == hosted._REDACT_TOO_DEEP


def test_redact_obj_leaves_a_realistic_frame_untruncated():
    # The cap sits orders of magnitude above real stream-json nesting, so a normal
    # frame is redacted in full and the marker never appears.
    frame = {
        "type": "assistant",
        "message": {"content": [{"text": "env_01BCDEFGHIJKLMNOPQRSTUVWX"}]},
    }
    out = _redact_obj(frame)
    assert hosted._REDACT_TOO_DEEP not in json.dumps(out)
    assert "env_01" not in out["message"]["content"][0]["text"]
