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
