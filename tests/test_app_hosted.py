"""Endpoint + WebSocket wiring for the hosted channel (CL-4b).

The HostedManager/HostedSession engine is unit-tested in ``test_hosted.py`` against
a real fake daemon. Here the manager and daemon are stubbed so the focus is the
app boundary: channel dispatch in ``POST /api/instances``, the message/stop
endpoints, and the ``/ws/hosted`` stream — no real socket or cross-loop client.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import clauster.app as app_module
from clauster.app import create_app
from clauster.claustrum_client import ClaustrumError
from clauster.config import load_config
from clauster.hosted import HostedSessionError
from clauster.models import InstanceStatus, RemoteControlInstance

_HID = "hid-1"


class _StubSession:
    """A hosted session that replays a fixed ring on subscribe."""

    def __init__(self) -> None:
        self.events = [{"event_seq": 1, "type": "frame", "frame": {"type": "system"}}]
        self.unsubscribed = False

    def subscribe(self, after_seq: int = 0) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        for event in self.events:
            if event["event_seq"] > after_seq:
                queue.put_nowait(event)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.unsubscribed = True


class _StubManager:
    """Records boundary calls; no real daemon involved."""

    def __init__(self) -> None:
        self.instances: dict[str, RemoteControlInstance] = {}
        self.sessions: dict[str, _StubSession] = {}
        self.spawn_calls: list[dict] = []
        self.sent: list[tuple[str, str]] = []
        self.stopped: list[str] = []
        self.spawn_error: Exception | None = None
        self.send_error: Exception | None = None

    def seed(self) -> RemoteControlInstance:
        inst = RemoteControlInstance(
            project="alpha",
            label="hosted:alpha",
            channel="hosted",
            claustrum_process_id=_HID,
            status=InstanceStatus.RUNNING,
        )
        self.instances[_HID] = inst
        self.sessions[_HID] = _StubSession()
        return inst

    async def spawn(
        self, client, *, project, label, cwd, claude_binary, permission_mode, resume_uuid=None
    ):
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawn_calls.append(
            {"project": project, "cwd": cwd, "binary": claude_binary, "pm": permission_mode}
        )
        return self.seed()

    def get_instance(self, hosted_id: str):
        return self.instances.get(hosted_id)

    def session(self, hosted_id: str):
        return self.sessions.get(hosted_id)

    def list_instances(self):
        return list(self.instances.values())

    async def send(self, hosted_id: str, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        if hosted_id not in self.sessions:
            raise HostedSessionError(f"no such hosted session: {hosted_id}")
        self.sent.append((hosted_id, text))

    async def stop(self, hosted_id: str) -> RemoteControlInstance:
        self.stopped.append(hosted_id)
        inst = self.instances[hosted_id]
        inst.status = InstanceStatus.STOPPED
        return inst

    async def aclose(self) -> None:
        pass


class _StubDaemon:
    def __init__(self, client: object | None = None) -> None:
        self._client = client if client is not None else object()

    @property
    def client(self):
        return self._client

    async def aclose(self) -> None:
        pass


def _app(write_config, *, manager: _StubManager | None = None, daemon: _StubDaemon | None = None):
    config = load_config(write_config(""))
    app = create_app(config)
    app.state.hosted = manager if manager is not None else _StubManager()
    app.state.claustrum_daemon = daemon
    return app


# -- spawn dispatch --------------------------------------------------------


def test_spawn_hosted_dispatches_to_manager(write_config, projects_root, monkeypatch):
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: True)
    monkeypatch.setattr(app_module.claude_cli, "resolve_binary", lambda b: "/usr/bin/claude")
    manager = _StubManager()
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "hosted"})
    assert r.status_code == 201
    assert r.json()["channel"] == "hosted"
    assert manager.spawn_calls[0]["project"] == "alpha"
    assert manager.spawn_calls[0]["binary"] == "/usr/bin/claude"
    assert manager.spawn_calls[0]["cwd"].endswith("/alpha")


def test_spawn_hosted_without_daemon_is_503(write_config, projects_root):
    app = _app(write_config, daemon=None)  # claustrum disabled → no daemon
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "hosted"})
    assert r.status_code == 503


def test_spawn_hosted_untrusted_is_409(write_config, projects_root, monkeypatch):
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: False)
    app = _app(write_config, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "hosted"})
    assert r.status_code == 409


def test_spawn_unknown_channel_is_422(write_config, projects_root):
    app = _app(write_config)
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "bogus"})
    assert r.status_code == 422


# -- message ---------------------------------------------------------------


def test_message_routes_to_manager(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/message", json={"text": "hello"})
    assert r.status_code == 202
    assert manager.sent == [(_HID, "hello")]


def test_message_empty_text_is_422(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/message", json={"text": ""})
    assert r.status_code == 422


def test_message_unknown_instance_is_404(write_config, projects_root):
    app = _app(write_config)
    with TestClient(app) as client:
        r = client.post("/api/instances/nope/message", json={"text": "hi"})
    assert r.status_code == 404


# -- stop dispatch ---------------------------------------------------------


def test_delete_routes_hosted_to_manager(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.delete(f"/api/instances/{_HID}")
    assert r.status_code == 200
    assert manager.stopped == [_HID]
    assert r.json()["status"] == "stopped"


# -- websocket -------------------------------------------------------------


def test_ws_hosted_streams_then_replays(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client, client.websocket_connect(f"/ws/hosted/{_HID}") as ws:
        event = ws.receive_json()
        assert event["type"] == "frame" and event["event_seq"] == 1


def test_ws_hosted_unknown_instance_closes(write_config, projects_root):
    app = _app(write_config)
    with TestClient(app) as client:
        with pytest.raises(Exception):  # noqa: B017 - server closes (1008) right after accept
            with client.websocket_connect("/ws/hosted/nope") as ws:
                ws.receive_json()


# -- spawn / message error branches ----------------------------------------


def test_spawn_hosted_binary_missing_is_503(write_config, projects_root, monkeypatch):
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: True)

    def _missing(_binary):
        raise FileNotFoundError("claude not found on PATH")

    monkeypatch.setattr(app_module.claude_cli, "resolve_binary", _missing)
    app = _app(write_config, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "hosted"})
    assert r.status_code == 503


def test_spawn_hosted_daemon_error_is_502(write_config, projects_root, monkeypatch):
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: True)
    monkeypatch.setattr(app_module.claude_cli, "resolve_binary", lambda b: "/usr/bin/claude")
    manager = _StubManager()
    manager.spawn_error = ClaustrumError("daemon went away")
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "hosted"})
    assert r.status_code == 502


def test_message_wrong_state_is_409(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    manager.send_error = HostedSessionError("cannot send to a stopped hosted session")
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/message", json={"text": "hi"})
    assert r.status_code == 409


def test_ws_hosted_invalid_after_defaults_to_zero(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/ws/hosted/{_HID}?after=oops") as ws,
    ):
        event = ws.receive_json()
        assert event["event_seq"] == 1  # bad cursor → replay from the start


def test_ws_hosted_unauthorized_closed(write_config, projects_root):
    config = load_config(write_config(""))
    config.auth.enabled = True  # WS must reject without a session (validate before accept)
    app = create_app(config)
    manager = _StubManager()
    manager.seed()
    app.state.hosted = manager
    with TestClient(app) as client:
        with pytest.raises(Exception):  # noqa: B017 - rejected handshake (1008)
            with client.websocket_connect(f"/ws/hosted/{_HID}") as ws:
                ws.receive_json()


class _BrokenQueue:
    """Yields one event, then raises — exercises the WS disconnect/teardown handler."""

    def __init__(self) -> None:
        self._n = 0

    async def get(self) -> dict:
        self._n += 1
        if self._n == 1:
            return {"event_seq": 1, "type": "frame", "frame": {}}
        raise RuntimeError("stream torn down")


class _BrokenSession:
    def subscribe(self, after_seq: int = 0) -> _BrokenQueue:
        return _BrokenQueue()

    def unsubscribe(self, queue: object) -> None:
        pass


def test_ws_hosted_handles_stream_teardown(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    manager.sessions[_HID] = _BrokenSession()  # second get() raises → handler returns cleanly
    app = _app(write_config, manager=manager)
    with TestClient(app) as client, client.websocket_connect(f"/ws/hosted/{_HID}") as ws:
        assert ws.receive_json()["event_seq"] == 1
