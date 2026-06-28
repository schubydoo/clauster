"""Tests for the read-only ``clauster mcp`` stdio MCP server (#527).

Covers the JSON-RPC/MCP protocol surface (initialize, tools/list, tools/call for
both tools, and the error paths) driven directly against ``MCPServer`` and the
``serve`` loop — no real subprocess — plus a ``gather_sessions`` integration test
that seeds the same read sources the dashboard uses (a persisted bridge, a hosted
record, a background job) and asserts the summarized shapes and that no free-text
leaks. HOME is isolated by the autouse conftest fixture.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from clauster import __version__, mcp_server


def _handle(server: mcp_server.MCPServer, message: dict) -> dict | None:
    """Drive one message through the server's async handler synchronously."""
    return asyncio.run(server.handle(message))


async def _anoop(self, *args, **kwargs) -> None:
    """An async no-op used to neutralize the runner's reconcile calls in tests."""
    return None


@pytest.fixture
def cfg(runner_config):
    """The :class:`ClausterConfig` from ``runner_config`` (which yields ``(config, json)``)."""
    return runner_config[0]


@pytest.fixture
def server(cfg) -> mcp_server.MCPServer:
    """An ``MCPServer`` bound to the trusted tmp config."""
    return mcp_server.MCPServer(cfg)


# --------------------------------------------------------------------------- #
# Protocol: initialize / lifecycle
# --------------------------------------------------------------------------- #
def test_initialize_negotiates_requested_version_and_advertises_tools(server):
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
    }
    resp = _handle(server, msg)
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"] == {"name": "clauster", "version": __version__}
    assert result["capabilities"]["tools"] == {"listChanged": False}


def test_initialize_falls_back_to_server_protocol_when_unspecified(server):
    resp = _handle(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_initialized_notification_is_acked_without_reply(server):
    resp = _handle(server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp is None
    assert server._initialized is True


def test_ping_returns_empty_result(server):
    resp = _handle(server, {"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert resp == {"jsonrpc": "2.0", "id": 9, "result": {}}


# --------------------------------------------------------------------------- #
# Protocol: tools/list
# --------------------------------------------------------------------------- #
def test_tools_list_exposes_exactly_the_two_readonly_tools(server):
    resp = _handle(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in resp["result"]["tools"]]
    assert names == ["list_sessions", "session_status"]
    for tool in resp["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"
    # No mutation tool is ever advertised (read-only v1 invariant).
    assert not any(n in names for n in ("spawn_session", "stop_session", "resume_session"))


# --------------------------------------------------------------------------- #
# Protocol: tools/call (both tools)
# --------------------------------------------------------------------------- #
def test_tools_call_list_sessions_returns_structured_text(server, monkeypatch):
    fake = [{"id": "proj-a", "kind": "bridge", "status": "running"}]

    async def _fake_gather(config):
        return fake

    monkeypatch.setattr(mcp_server, "gather_sessions", _fake_gather)
    resp = _handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_sessions", "arguments": {}},
        },
    )
    result = resp["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"count": 1, "sessions": fake}


def test_tools_call_session_status_found(server, monkeypatch):
    async def _fake_gather(config):
        # A non-matching item first, so the lookup loop iterates past it.
        return [
            {"id": "other", "kind": "hosted", "status": "running"},
            {"id": "proj-a", "kind": "bridge", "status": "running"},
        ]

    monkeypatch.setattr(mcp_server, "gather_sessions", _fake_gather)
    resp = _handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "session_status", "arguments": {"id": "proj-a"}},
        },
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["found"] is True
    assert payload["session"]["id"] == "proj-a"


def test_tools_call_session_status_unknown_id_reports_not_found(server, monkeypatch):
    async def _fake_gather(config):
        return []

    monkeypatch.setattr(mcp_server, "gather_sessions", _fake_gather)
    resp = _handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "session_status", "arguments": {"id": "nope"}},
        },
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload == {"found": False, "id": "nope"}
    assert resp["result"]["isError"] is False


def test_tools_call_session_status_blank_id_is_an_iserror_result(server, monkeypatch):
    async def _fake_gather(config):  # pragma: no cover - must not be reached
        raise AssertionError("gather should not run for a blank id")

    monkeypatch.setattr(mcp_server, "gather_sessions", _fake_gather)
    resp = _handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "session_status", "arguments": {"id": "   "}},
        },
    )
    assert resp["result"]["isError"] is True
    assert "non-empty" in resp["result"]["content"][0]["text"]


def test_tools_call_unknown_tool_is_an_iserror_result(server):
    resp = _handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "spawn_session", "arguments": {}},
        },
    )
    assert resp["result"]["isError"] is True
    assert "unknown tool" in resp["result"]["content"][0]["text"]


def test_tools_call_non_object_arguments_is_an_iserror_result(server):
    resp = _handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "list_sessions", "arguments": [1, 2, 3]},
        },
    )
    assert resp["result"]["isError"] is True
    assert "must be an object" in resp["result"]["content"][0]["text"]


def test_tools_call_handler_exception_becomes_iserror_not_a_crash(server, monkeypatch):
    async def _boom(config):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mcp_server, "gather_sessions", _boom)
    resp = _handle(
        server,
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "list_sessions", "arguments": {}},
        },
    )
    assert resp["result"]["isError"] is True
    assert "list_sessions failed" in resp["result"]["content"][0]["text"]


# --------------------------------------------------------------------------- #
# Protocol: error handling
# --------------------------------------------------------------------------- #
def test_unknown_method_returns_method_not_found(server):
    resp = _handle(server, {"jsonrpc": "2.0", "id": 11, "method": "resources/list"})
    assert resp["error"]["code"] == mcp_server._METHOD_NOT_FOUND


def test_unknown_notification_is_silently_dropped(server):
    resp = _handle(server, {"jsonrpc": "2.0", "method": "notifications/cancelled"})
    assert resp is None


def test_wrong_jsonrpc_version_is_invalid_request(server):
    resp = _handle(server, {"jsonrpc": "1.0", "id": 12, "method": "ping"})
    assert resp["error"]["code"] == mcp_server._INVALID_REQUEST


def test_missing_method_is_invalid_request(server):
    resp = _handle(server, {"jsonrpc": "2.0", "id": 13})
    assert resp["error"]["code"] == mcp_server._INVALID_REQUEST


# --------------------------------------------------------------------------- #
# serve(): the stdio loop framing
# --------------------------------------------------------------------------- #
class _CollectingWriter:
    """A minimal stdout stand-in collecting newline-framed lines."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._buf = ""

    def write(self, text: str) -> None:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.lines.append(line)

    def flush(self) -> None:
        return None


async def _reader_from(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


def test_serve_handshake_then_tools_list_over_stdio(cfg):
    """Each request line yields exactly one single-line JSON-RPC response."""
    requests = (
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
    ).encode()
    writer = _CollectingWriter()

    async def _drive():
        reader = await _reader_from(requests)
        await mcp_server.serve(cfg, reader, writer)

    asyncio.run(_drive())
    # initialize + tools/list reply; the notification produces no line.
    assert len(writer.lines) == 2
    init = json.loads(writer.lines[0])
    assert init["result"]["serverInfo"]["name"] == "clauster"
    listed = json.loads(writer.lines[1])
    assert [t["name"] for t in listed["result"]["tools"]] == ["list_sessions", "session_status"]
    # Framing invariant: never an embedded newline inside a message.
    assert all("\n" not in line for line in writer.lines)


def test_serve_invalid_json_line_yields_parse_error(cfg):
    writer = _CollectingWriter()

    async def _drive():
        reader = await _reader_from(b"not json at all\n")
        await mcp_server.serve(cfg, reader, writer)

    asyncio.run(_drive())
    assert json.loads(writer.lines[0])["error"]["code"] == mcp_server._PARSE_ERROR


def test_serve_non_object_message_is_invalid_request(cfg):
    writer = _CollectingWriter()

    async def _drive():
        reader = await _reader_from(b"[1,2,3]\n")
        await mcp_server.serve(cfg, reader, writer)

    asyncio.run(_drive())
    assert json.loads(writer.lines[0])["error"]["code"] == mcp_server._INVALID_REQUEST


def test_serve_blank_lines_are_skipped_and_eof_ends_loop(cfg):
    writer = _CollectingWriter()

    async def _drive():
        reader = await _reader_from(b"\n\n")
        await mcp_server.serve(cfg, reader, writer)

    asyncio.run(_drive())
    assert writer.lines == []


def test_serve_handler_crash_becomes_internal_error_and_loop_survives(cfg, monkeypatch):
    """If the dispatcher itself raises, the loop emits an internal error, not a crash."""

    async def _boom(self, message):
        raise RuntimeError("dispatcher blew up")

    monkeypatch.setattr(mcp_server.MCPServer, "handle", _boom)
    writer = _CollectingWriter()

    async def _drive():
        reader = await _reader_from(
            b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
        )
        await mcp_server.serve(cfg, reader, writer)

    asyncio.run(_drive())
    # Both bad messages produce an internal-error reply; the loop never dies.
    assert len(writer.lines) == 2
    for line in writer.lines:
        assert json.loads(line)["error"]["code"] == mcp_server._INTERNAL_ERROR


def test_serve_oversized_line_is_a_parse_error_and_loop_survives(cfg, monkeypatch):
    """A line over the read limit is answered as a parse error, not a server crash."""
    # Shrink the cap so the test payload is small; the reader must share the limit.
    monkeypatch.setattr(mcp_server, "_MAX_LINE_BYTES", 256)
    huge = b'{"jsonrpc":"2.0","id":1,"method":"ping","x":"' + b"A" * 4096 + b'"}\n'
    follow = b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    writer = _CollectingWriter()

    async def _drive():
        reader = asyncio.StreamReader(limit=256)
        reader.feed_data(huge + follow)
        reader.feed_eof()
        await mcp_server.serve(cfg, reader, writer)

    asyncio.run(_drive())
    # First reply: the oversized line → parse error (id null). Then the loop realigns
    # on the next message and answers the follow-up ping normally.
    first = json.loads(writer.lines[0])
    assert first["error"]["code"] == mcp_server._PARSE_ERROR
    assert "too large" in first["error"]["message"]
    assert any(json.loads(line).get("id") == 2 for line in writer.lines)


def test_serve_stops_cleanly_when_client_closes_read_end(cfg):
    """A BrokenPipeError on write ends the loop without a traceback or extra reads."""

    class _BrokenWriter:
        def write(self, _text):
            raise BrokenPipeError("client gone")

        def flush(self):  # pragma: no cover - never reached after write raises
            return None

    async def _drive():
        reader = await _reader_from(
            b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
        )
        # Must return (not raise) even though the writer is broken.
        await mcp_server.serve(cfg, reader, _BrokenWriter())

    asyncio.run(_drive())  # no exception escapes


def test_write_returns_false_on_broken_pipe():
    class _BrokenWriter:
        def write(self, _text):
            raise BrokenPipeError

        def flush(self):  # pragma: no cover
            return None

    assert mcp_server._write(_BrokenWriter(), {"ok": True}) is False


class _BrokenWriter:
    """A writer that always reports a broken pipe — every error-reply site stops."""

    def write(self, _text):
        raise BrokenPipeError("client gone")

    def flush(self):  # pragma: no cover - never reached after write raises
        return None


@pytest.mark.parametrize(
    "payload",
    [
        b'{"jsonrpc":"2.0","id":1,"method":"ping","x":"' + b"A" * 4096 + b'"}\n',  # oversized
        b"not json\n",  # invalid JSON
        b"[1,2,3]\n",  # non-object message
    ],
)
def test_serve_error_reply_sites_stop_cleanly_on_broken_pipe(cfg, monkeypatch, payload):
    """Each error-reply write site returns the loop cleanly when the client hung up."""
    monkeypatch.setattr(mcp_server, "_MAX_LINE_BYTES", 256)

    async def _drive():
        reader = asyncio.StreamReader(limit=256)
        reader.feed_data(payload)
        reader.feed_eof()
        await mcp_server.serve(cfg, reader, _BrokenWriter())

    asyncio.run(_drive())  # no exception escapes


def test_handle_known_method_as_notification_gets_no_reply(server):
    """A request method (``ping``) sent without an id is a notification — no reply."""
    resp = _handle(server, {"jsonrpc": "2.0", "method": "ping"})
    assert resp is None


# --------------------------------------------------------------------------- #
# gather_sessions(): integration over the real read machinery
# --------------------------------------------------------------------------- #
def test_gather_sessions_empty_when_nothing_running(cfg):
    sessions = asyncio.run(mcp_server.gather_sessions(cfg))
    assert sessions == []


def test_gather_sessions_includes_persisted_bridge(cfg):
    """A persisted (resumable) bridge with no live process appears as a stopped card.

    Seeded through the runner's own persistence container (the DB-backed store the
    app uses) and read back by a *fresh* runner inside ``gather_sessions`` — the
    same restart-reattach path the dashboard relies on. ``alpha`` is the git
    project the ``projects_root`` fixture lays down, so ``rediscover`` resurrects it.
    """
    from clauster.runner import SessionRunner

    SessionRunner(cfg).persistence.state_store().save(
        {"alpha": {"label": "alpha", "intentional_stop": True}}
    )
    sessions = asyncio.run(mcp_server.gather_sessions(cfg))
    bridges = [s for s in sessions if s["kind"] == "bridge"]
    bridge = next(s for s in bridges if s["id"] == "alpha")
    assert bridge["channel"] == "remote-control"
    assert bridge["status"] in {"stopped", "starting", "running", "crashed", "error"}
    # Only structural fields — never raw log / transcript content.
    assert set(bridge) >= {"id", "kind", "channel", "project", "label", "status"}
    assert "error_detail" not in bridge
    assert "bridge_debug_log_path" not in bridge


def test_gather_sessions_includes_hosted_record(cfg):
    """A persisted hosted record is summarized via the existing record→instance mapper."""
    from clauster.runner import SessionRunner

    SessionRunner(cfg).persistence.hosted_state_store().save(
        {
            "abc123proc": {
                "project": "alpha",
                "label": "hosted:abc123",
                "claude_session_uuid": "11111111-2222-3333-4444-555555555555",
                "intentional_stop": False,
            }
        }
    )
    sessions = asyncio.run(mcp_server.gather_sessions(cfg))
    hosted = [s for s in sessions if s["kind"] == "hosted"]
    assert len(hosted) == 1
    assert hosted[0]["id"] == "abc123proc"
    assert hosted[0]["channel"] == "hosted"
    assert hosted[0]["claude_session_uuid"] == "11111111-2222-3333-4444-555555555555"


def test_gather_sessions_intentionally_stopped_hosted_is_stopped(cfg):
    from clauster.runner import SessionRunner

    SessionRunner(cfg).persistence.hosted_state_store().save(
        {"p": {"project": "alpha", "intentional_stop": True}}
    )
    sessions = asyncio.run(mcp_server.gather_sessions(cfg))
    hosted = next(s for s in sessions if s["kind"] == "hosted")
    assert hosted["status"] == "stopped"


def test_gather_sessions_includes_background_job_without_freetext(cfg, monkeypatch):
    """A background job is summarized to lifecycle fields only — no redacted prose leaks."""
    from clauster.models import BackgroundJob

    job = BackgroundJob(
        id="bgjob01",
        state="working",
        detail="SECRET progress prose that must not egress",
        intent="SECRET original prompt",
        name="SECRET display name",
        session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        worker_pid=4242,
        worker_alive=True,
    )
    monkeypatch.setattr(mcp_server, "gather_sessions", mcp_server.gather_sessions)
    monkeypatch.setattr("clauster.supervisor.list_background_jobs", lambda *a, **k: [job])
    sessions = asyncio.run(mcp_server.gather_sessions(cfg))
    bg = [s for s in sessions if s["kind"] == "background-agent"]
    assert len(bg) == 1
    assert bg[0]["id"] == "bgjob01"
    assert bg[0]["state"] == "working"
    assert bg[0]["worker_pid"] == 4242
    # No free-text field is surfaced, even though it is already redacted upstream.
    blob = json.dumps(bg[0])
    assert "SECRET" not in blob
    assert "detail" not in bg[0]
    assert "intent" not in bg[0]
    assert "name" not in bg[0]


def test_gather_sessions_summarizes_tracked_and_external_working_sessions(cfg, monkeypatch):
    """Tracked (under a bridge) and external working sessions are each summarized.

    The ``agents --json`` cross-check that populates these needs a live process, so
    we inject the already-attributed working sessions the runner would have built —
    exercising the bridge-session and external-session summary paths directly.
    """
    from clauster.models import Attribution, InstanceStatus, RemoteControlInstance, WorkingSession
    from clauster.runner import SessionRunner

    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.RUNNING)
    tracked_ws = WorkingSession(
        pid=111,
        cwd=cfg.projects_root / "alpha",
        kind="interactive",
        state="working",
        started_at=1700000000000,
        local_uuid="aaaaaaaa-1111-2222-3333-444444444444",
        parent_instance="alpha",
        attribution=Attribution.TRACKED,
    )
    external_ws = WorkingSession(
        pid=222,
        cwd=cfg.projects_root / "beta",
        kind="interactive",
        started_at=1700000001000,
        local_uuid="bbbbbbbb-1111-2222-3333-444444444444",
        attribution=Attribution.EXTERNAL,
    )
    monkeypatch.setattr(SessionRunner, "rediscover", _anoop)
    monkeypatch.setattr(SessionRunner, "poll_once", _anoop)
    monkeypatch.setattr(SessionRunner, "list_instances", lambda self: [inst])
    monkeypatch.setattr(
        SessionRunner, "tracked_sessions_by_instance", lambda self: {"alpha": [tracked_ws]}
    )
    monkeypatch.setattr(
        SessionRunner, "external_sessions_by_project", lambda self: {"beta": [external_ws]}
    )

    sessions = asyncio.run(mcp_server.gather_sessions(cfg))
    by_kind = {s["kind"]: s for s in sessions}
    assert by_kind["bridge"]["id"] == "alpha"
    tracked = by_kind["bridge-session"]
    assert tracked["id"] == "aaaaaaaa-1111-2222-3333-444444444444"
    assert tracked["attribution"] == "tracked"
    assert tracked["parent_instance"] == "alpha"
    ext = by_kind["external-session"]
    assert ext["id"] == "bbbbbbbb-1111-2222-3333-444444444444"
    assert ext["project"] == "beta"
    assert ext["attribution"] == "external"


def test_session_status_finds_background_job_by_id(cfg, monkeypatch):
    from clauster.models import BackgroundJob

    job = BackgroundJob(id="bgjob01", state="done")
    monkeypatch.setattr("clauster.supervisor.list_background_jobs", lambda *a, **k: [job])
    payload = asyncio.run(mcp_server._tool_session_status(cfg, {"id": "bgjob01"}))
    assert payload["found"] is True
    assert payload["session"]["kind"] == "background-agent"


# --------------------------------------------------------------------------- #
# CLI entry: clauster mcp
# --------------------------------------------------------------------------- #
def test_main_config_error_fails_closed(monkeypatch, capsys, tmp_path):
    """A bad/missing config exits non-zero with a stderr message, never serving."""
    monkeypatch.setattr(
        mcp_server, "load_config", lambda path: (_ for _ in ()).throw(FileNotFoundError("nope"))
    )
    rc = mcp_server.main(["-c", str(tmp_path / "absent.yml")])
    assert rc == 2
    assert "config error" in capsys.readouterr().err


def test_main_serves_then_exits_zero_on_eof(monkeypatch, cfg, capsys):
    """``main`` loads config, prints a stderr banner, serves, and returns 0 on EOF."""
    monkeypatch.setattr(mcp_server, "load_config", lambda path: cfg)

    served = {}

    async def _fake_run_stdio(config):
        served["config"] = config

    monkeypatch.setattr(mcp_server, "_run_stdio", _fake_run_stdio)
    rc = mcp_server.main([])
    assert rc == 0
    assert served["config"] is cfg
    err = capsys.readouterr().err
    assert "clauster mcp" in err and "read-only" in err
    # The banner must go to stderr, never stdout (stdout is the protocol channel).


def test_main_keyboard_interrupt_exits_zero(monkeypatch, cfg):
    monkeypatch.setattr(mcp_server, "load_config", lambda path: cfg)

    def _raise(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(mcp_server.asyncio, "run", _raise)
    assert mcp_server.main([]) == 0


def test_dispatch_from_main_module_routes_to_mcp(monkeypatch):
    """``clauster mcp`` routes through __main__ to the mcp_server entry point."""
    import clauster.__main__ as cli

    captured = {}

    def _fake_mcp_main(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr("clauster.mcp_server.main", _fake_mcp_main)
    rc = cli.main(["mcp", "-c", "/tmp/x.yml"])
    assert rc == 0
    assert captured["argv"] == ["-c", "/tmp/x.yml"]


def test_dispatch_from_main_module_without_config(monkeypatch):
    import clauster.__main__ as cli

    captured = {}
    monkeypatch.setattr(
        "clauster.mcp_server.main", lambda argv: captured.setdefault("argv", argv) or 0
    )
    assert cli.main(["mcp"]) == 0
    assert captured["argv"] == []


def test_run_stdio_uses_real_stdin_reader(monkeypatch, cfg):
    """``_run_stdio`` wires a reader to stdin and calls serve (covered with a stub stdin)."""
    calls = {}

    async def _fake_serve(config, reader, writer):
        calls["served"] = True

    monkeypatch.setattr(mcp_server, "serve", _fake_serve)
    # Feed an empty stdin so connect_read_pipe has a real fd to attach to.
    monkeypatch.setattr(mcp_server.sys, "stdin", io.StringIO(""))

    async def _drive():
        # connect_read_pipe needs a real OS pipe; use one so the call path runs.
        import os

        r_fd, w_fd = os.pipe()
        os.close(w_fd)
        rfile = os.fdopen(r_fd)
        monkeypatch.setattr(mcp_server.sys, "stdin", rfile)
        await mcp_server._run_stdio(cfg)
        rfile.close()

    asyncio.run(_drive())
    assert calls["served"] is True
