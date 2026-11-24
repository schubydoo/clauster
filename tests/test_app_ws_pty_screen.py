"""WebSocket /ws/pty-screen — redacted, cells-only live-screen frames for the pty view (#534).

The keeper writes redacted frames to a screen sidecar (off by default); the endpoint polls
that file and forwards each new frame, de-duped by its monotonic ``seq``. These pin the
handler's gating (pty-only, feature-on, real sidecar) and its forward/de-dup behaviour. The
shared auth gate is pinned by ``test_app_auth.test_all_ws_endpoints_reject_unauthenticated``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import starlette.websockets
from fastapi.testclient import TestClient

from clauster import pty_screen
from clauster.app import create_app
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.routes import websockets as ws_module
from clauster.runner import SessionRunner


def _pty_running(name: str, log_path: Path) -> RemoteControlInstance:
    return RemoteControlInstance(
        project=name,
        label=name,
        status=InstanceStatus.RUNNING,
        bridge_debug_log_path=log_path,
        resume_mode="pty",
    )


def _client_with(
    runner_config, instance: RemoteControlInstance | None, *, screen_enabled: bool
) -> TestClient:
    config, claude_json = runner_config
    config.claude.pty_screen_enabled = screen_enabled
    runner = SessionRunner(config, claude_json=claude_json)
    if instance is not None:
        # Key by instance_id like the real registry (#777); the WS URL carries the
        # project name and is resolved via runner.resolve_bridge_id.
        runner._instances[instance.instance_id] = instance
    return TestClient(create_app(config, runner=runner))


def _write_frame(log_path: Path, seq: int, **extra) -> None:
    payload = {"seq": seq, "state": "live", "error": None, "screen": {"rows": ["hi"]}}
    payload.update(extra)
    pty_screen.screen_sidecar_path(log_path).write_text(json.dumps(payload), encoding="utf-8")


def test_streams_frames_and_dedups_by_seq(runner_config, tmp_path: Path):
    log = tmp_path / "alpha-1-1.log"
    _write_frame(log, 1)
    with _client_with(runner_config, _pty_running("alpha", log), screen_enabled=True) as client:
        with client.websocket_connect("/ws/pty-screen/alpha") as ws:
            first = ws.receive_json()
            assert first["seq"] == 1 and first["screen"]["rows"] == ["hi"]
            # Publish a newer frame; the next receive must be seq 2 — proving the reader
            # advances on a higher seq and never re-sends the frame already delivered.
            _write_frame(log, 2, screen={"rows": ["bye"]})
            second = ws.receive_json()
            assert second["seq"] == 2 and second["screen"]["rows"] == ["bye"]


def test_absent_sidecar_poll_is_skipped(runner_config, tmp_path: Path, monkeypatch):
    # The keeper may not have written the sidecar yet (first connect / pre-flush): the first
    # read returns None, so that poll is skipped and the loop waits; the next read yields a
    # real frame. Covers the `frame is not None` False arc. Poll interval zeroed for speed.
    monkeypatch.setattr(ws_module, "_SCREEN_POLL_INTERVAL", 0)
    calls = {"n": 0}

    def _read(_path, *, after_seq):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # nothing on disk yet → skip this tick
        return {"seq": 1, "state": "live", "error": None, "screen": {"rows": ["hi"]}}

    monkeypatch.setattr(pty_screen, "read_new_screen_frame", _read)
    log = tmp_path / "alpha-1-1.log"
    with _client_with(runner_config, _pty_running("alpha", log), screen_enabled=True) as client:
        with client.websocket_connect("/ws/pty-screen/alpha") as ws:
            assert ws.receive_json()["seq"] == 1


def test_unchanged_frame_is_not_resent_or_masked_again(runner_config, tmp_path: Path, monkeypatch):
    # When the keeper hasn't updated the sidecar between polls the reader sees the same seq
    # again and must skip it, forwarding only on a strictly higher seq. TASK-470: the skip
    # comes BEFORE the mask, so an unchanged frame is masked once, not on every poll.
    monkeypatch.setattr(ws_module, "_SCREEN_POLL_INTERVAL", 0)
    log = tmp_path / "alpha-1-1.log"
    _write_frame(log, 1, screen={"rows": ["a"]})
    polls, masks = {"n": 0}, {"n": 0}
    load, mask = pty_screen._load_sidecar, pty_screen._redact_known_frame

    def _counted_load(path):
        polls["n"] += 1
        if polls["n"] == 6:  # five polls of the same frame, then the keeper publishes seq 2
            _write_frame(log, 2, screen={"rows": ["b"]})
        return load(path)

    def _counted_mask(data):
        masks["n"] += 1
        return mask(data)

    monkeypatch.setattr(pty_screen, "_load_sidecar", _counted_load)
    monkeypatch.setattr(pty_screen, "_redact_known_frame", _counted_mask)
    with _client_with(runner_config, _pty_running("alpha", log), screen_enabled=True) as client:
        with client.websocket_connect("/ws/pty-screen/alpha") as ws:
            first = ws.receive_json()  # seq 1 (poll 1); polls 2-5 read it again and skip it
            second = ws.receive_json()  # seq 2 (poll 6)
            sent_masks = masks["n"]
    assert first["seq"] == 1 and first["screen"]["rows"] == ["a"]
    assert second["seq"] == 2 and second["screen"]["rows"] == ["b"]
    assert polls["n"] >= 6
    assert sent_masks == 2  # one mask per frame sent; masking per poll made it 6


def test_reader_skips_an_old_frame_before_it_masks(tmp_path: Path, monkeypatch):
    sidecar = tmp_path / "alpha.screen.json"
    masks = {"n": 0}
    mask = pty_screen._redact_known_frame

    def _counted_mask(data):
        masks["n"] += 1
        return mask(data)

    monkeypatch.setattr(pty_screen, "_redact_known_frame", _counted_mask)
    assert pty_screen.read_new_screen_frame(sidecar, after_seq=-1) is None  # no file yet
    for seq in (3, "3", None, True):
        sidecar.write_text(json.dumps({"seq": seq, "screen": None}), encoding="utf-8")
        assert pty_screen.read_new_screen_frame(sidecar, after_seq=3) is None
    sidecar.write_text(json.dumps({"seq": 4, "screen": {"rows": ["x"]}}), encoding="utf-8")
    assert pty_screen.read_new_screen_frame(sidecar, after_seq=3) == {
        "seq": 4,
        "screen": {"rows": ["x"]},
    }
    assert masks["n"] == 1  # only the frame that is returned was masked


def test_streams_status_frame_with_seq_zero(runner_config, tmp_path: Path):
    # A setup-time status (e.g. pyte unavailable) is seq 0 with screen=None; the reader must
    # still forward it (last_seq starts below 0) so the client can explain the missing screen.
    log = tmp_path / "alpha-1-1.log"
    pty_screen.screen_sidecar_path(log).write_text(
        json.dumps({"seq": 0, "state": "unavailable", "error": "no pyte", "screen": None}),
        encoding="utf-8",
    )
    with _client_with(runner_config, _pty_running("alpha", log), screen_enabled=True) as client:
        with client.websocket_connect("/ws/pty-screen/alpha") as ws:
            frame = ws.receive_json()
    assert frame["seq"] == 0 and frame["state"] == "unavailable" and frame["screen"] is None


def test_stream_teardown_returns_cleanly(runner_config, tmp_path: Path, monkeypatch):
    # Shared WS-teardown contract: a stream error mid-poll is caught by the handler's except,
    # which returns cleanly. One frame is delivered, then the next read raises; the poll
    # interval is zeroed so that raise fires in the same burst as the send — before the
    # client's disconnect can win the race — mirroring the proven
    # test_app_hosted.test_ws_hosted_handles_stream_teardown.
    monkeypatch.setattr(ws_module, "_SCREEN_POLL_INTERVAL", 0)
    calls = {"n": 0}

    def _read(_path, *, after_seq):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"seq": 1, "state": "live", "error": None, "screen": {"rows": ["hi"]}}
        raise RuntimeError("stream torn down")  # second read raises → except returns cleanly

    monkeypatch.setattr(pty_screen, "read_new_screen_frame", _read)
    log = tmp_path / "alpha-1-1.log"
    with _client_with(runner_config, _pty_running("alpha", log), screen_enabled=True) as client:
        with client.websocket_connect("/ws/pty-screen/alpha") as ws:
            assert ws.receive_json()["seq"] == 1


def test_unknown_instance_closed(runner_config):
    with _client_with(runner_config, None, screen_enabled=True) as client:
        with pytest.raises(starlette.websockets.WebSocketDisconnect):
            with client.websocket_connect("/ws/pty-screen/ghost") as ws:
                ws.receive_json()


def test_non_pty_instance_closed(runner_config, tmp_path: Path):
    # A standard bridge never gets a screen sidecar — refuse rather than poll a file forever.
    log = tmp_path / "alpha-1-1.log"
    _write_frame(log, 1)
    inst = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_debug_log_path=log,
        resume_mode="standard",
    )
    with _client_with(runner_config, inst, screen_enabled=True) as client:
        with pytest.raises(starlette.websockets.WebSocketDisconnect):
            with client.websocket_connect("/ws/pty-screen/alpha") as ws:
                ws.receive_json()


def test_feature_disabled_closed(runner_config, tmp_path: Path):
    # With the tap off (the default) there is no sidecar being written, so the socket closes.
    log = tmp_path / "alpha-1-1.log"
    _write_frame(log, 1)
    with _client_with(runner_config, _pty_running("alpha", log), screen_enabled=False) as client:
        with pytest.raises(starlette.websockets.WebSocketDisconnect):
            with client.websocket_connect("/ws/pty-screen/alpha") as ws:
                ws.receive_json()


def test_pty_instance_without_log_path_closed(runner_config):
    inst = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_debug_log_path=None,
        resume_mode="pty",
    )
    with _client_with(runner_config, inst, screen_enabled=True) as client:
        with pytest.raises(starlette.websockets.WebSocketDisconnect):
            with client.websocket_connect("/ws/pty-screen/alpha") as ws:
                ws.receive_json()
