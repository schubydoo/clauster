"""End-to-end clone pipeline: POST /clone -> bg task -> WS progress stream.

The route validation + job-registry branches are covered elsewhere; this exercises
the *wiring* that nothing else does — the 202 POST schedules ``_run_clone``, whose
``progress_cb`` hops back onto the loop (``call_soon_threadsafe``) into the
``CloneJob`` queue, which ``/ws/clone-progress`` streams as ``progress`` frames
followed by a terminal ``done`` frame. ``clone_project`` is stubbed (the real git +
``parse_progress`` are tested in test_provisioning); WS + background tasks require the
TestClient lifespan, so each test uses ``with _client(...)``.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import load_config

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _client(write_config, tmp_path) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n")
    return TestClient(create_app(load_config(cfg)))


def _drain_to_done(ws) -> list[dict]:
    """Read WS frames until (and including) the terminal ``done`` frame."""
    frames: list[dict] = []
    while not frames or frames[-1]["type"] != "done":
        frames.append(ws.receive_json())
    return frames


def test_clone_streams_progress_then_done(write_config, tmp_path, monkeypatch):
    # A gate lets the WS connect and read the running snapshot before the clone
    # finishes, so we deterministically observe a progress stream + the done frame.
    gate = threading.Event()

    def fake_clone(root, name, url, *, cfg, shallow, progress_cb):
        progress_cb("Receiving objects:  50% (5/10)")
        gate.wait(timeout=5)
        progress_cb("Receiving objects: 100% (10/10)")

    monkeypatch.setattr("clauster.app.clone_project", fake_clone)
    monkeypatch.setattr("clauster.app.validate_clone_url", lambda url, cfg: None)

    with _client(write_config, tmp_path) as client:
        resp = client.post(
            "/api/projects/clone", json={"name": "cloned", "url": "https://example.com/r.git"}
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]

        with client.websocket_connect(f"/ws/clone-progress/{job_id}") as ws:
            first = ws.receive_json()
            assert first["type"] == "progress"  # the running snapshot
            gate.set()  # release the clone to finish
            frames = [first, *_drain_to_done(ws)]

    assert frames[-1] == {"type": "done", "status": "done", "error": None}
    assert any(f.get("percent") == 100 for f in frames)  # the queued 100% frame streamed


def test_clone_error_streams_terminal_error_frame(write_config, tmp_path, monkeypatch):
    from clauster.app import ProvisionError

    def fake_clone(root, name, url, *, cfg, shallow, progress_cb):
        raise ProvisionError("clone failed: remote hung up")

    monkeypatch.setattr("clauster.app.clone_project", fake_clone)
    monkeypatch.setattr("clauster.app.validate_clone_url", lambda url, cfg: None)

    with _client(write_config, tmp_path) as client:
        resp = client.post(
            "/api/projects/clone", json={"name": "broken", "url": "https://example.com/r.git"}
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]

        with client.websocket_connect(f"/ws/clone-progress/{job_id}") as ws:
            frames = _drain_to_done(ws)

    assert frames[-1]["type"] == "done"
    assert frames[-1]["status"] == "error"
    assert frames[-1]["error"] == "clone failed: remote hung up"
