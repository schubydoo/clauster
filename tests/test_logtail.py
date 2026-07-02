"""Feature 6 — WebSocket bridge-log tail (redacted, ANSI-stripped)."""

from __future__ import annotations

from pathlib import Path

import starlette.websockets
from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import SessionRunner


def _running(name: str, **kw) -> RemoteControlInstance:
    return RemoteControlInstance(project=name, label=name, status=InstanceStatus.RUNNING, **kw)


def _client_with(runner_config, instance: RemoteControlInstance | None) -> TestClient:
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    if instance is not None:
        runner._instances[instance.project] = instance
    return TestClient(create_app(config, runner=runner))


def test_ws_log_stream_strips_ids_and_ansi(runner_config, tmp_path: Path):
    logf = tmp_path / "bridge.log"
    logf.write_text(
        "\x1b[32m[bridge:api] environment_id=env_01ABCDEFGHIJKLMNOPQRSTUVWX\x1b[0m\n"
        "[bridge:init] Created initial session session_01ZZZZZZZZZZZZZZZZZZZZZZ\n"
    )
    inst = _running("alpha", bridge_debug_log_path=logf)
    with _client_with(runner_config, inst) as client:
        with client.websocket_connect("/ws/bridge-log/alpha") as ws:
            joined = ws.receive_text() + "\n" + ws.receive_text()
    assert "\x1b" not in joined
    assert "env_01ABCDEFGHIJKLMNOPQRSTUVWX" not in joined
    assert "env_<redacted>" in joined
    assert "session_01ZZZ" not in joined
    assert "session_<redacted>" in joined


def test_ws_unknown_instance_closed(runner_config):
    with _client_with(runner_config, None) as client:
        try:
            with client.websocket_connect("/ws/bridge-log/ghost") as ws:
                ws.receive_text()
            raise AssertionError("expected the socket to be closed")
        except starlette.websockets.WebSocketDisconnect:
            pass


def test_ws_live_instance_without_log_path_closed(runner_config):
    """A RUNNING instance whose tail source is None closes the socket — this is exactly
    the #584 failure: a reattached bridge left without a `bridge_debug_log_path` 1008s
    every connect, so the live tail flickers and gives up. The reattach paths now bind a
    real path (see test_runner_pty / test_runner); this pins the handler's contract."""
    inst = _running("alpha", bridge_debug_log_path=None)
    with _client_with(runner_config, inst) as client:
        try:
            with client.websocket_connect("/ws/bridge-log/alpha") as ws:
                ws.receive_text()
            raise AssertionError("expected the socket to be closed")
        except starlette.websockets.WebSocketDisconnect:
            pass


# ----- audited coverage gaps (2026-07 audit) ----------------------------


def test_ws_idle_iteration_sends_nothing_then_streams_new_content(runner_config, tmp_path: Path):
    # app.py 2581->2587: a tail iteration with no new bytes sends nothing and just
    # sleeps (the `if text:` idle leg) — then picks up content on a later pass, so
    # an idle bridge holds the socket open without spurious empty frames.
    import time

    logf = tmp_path / "bridge.log"
    logf.write_text("")  # exists but empty -> the first read yields no text
    inst = _running("alpha", bridge_debug_log_path=logf)
    with _client_with(runner_config, inst) as client:
        with client.websocket_connect("/ws/bridge-log/alpha") as ws:
            time.sleep(0.3)  # let at least one idle loop iteration run
            logf.write_text("late line\n")
            line = ws.receive_text()
    assert "late line" in line


def test_ws_log_stream_abrupt_disconnect_is_swallowed(runner_config, tmp_path: Path, monkeypatch):
    # app.py 2591-2592: a client disconnect surfacing as WebSocketDisconnect out of
    # the stream helper ends the handler cleanly — no unhandled exception escapes
    # the ASGI task for a torn-down live tail.
    logf = tmp_path / "bridge.log"
    logf.write_text("one line\n")
    inst = _running("alpha", bridge_debug_log_path=logf)

    async def _abrupt(websocket, stream):
        raise starlette.websockets.WebSocketDisconnect(1006)

    monkeypatch.setattr("clauster.app.stream_until_disconnect", _abrupt)
    with _client_with(runner_config, inst) as client:
        with client.websocket_connect("/ws/bridge-log/alpha"):
            pass  # the handler hit the disconnect arm; no server error escaped
