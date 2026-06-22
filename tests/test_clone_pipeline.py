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

import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import load_config
from helpers import RecordingEmitter, assert_stays_empty, wait_for_calls

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _client(write_config, tmp_path, *, extra: str = "") -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}")
    return TestClient(create_app(load_config(cfg)))


# webhooks enabled with the #432 clone-done event opted in (it defaults OFF).
_WEBHOOKS_CLONE_DONE = (
    "webhooks:\n"
    "  enabled: true\n"
    "  urls: ['https://hook.test/h']\n"
    "  events:\n"
    "    clone-done: true\n"
)


def _drain_to_done(ws) -> list[dict]:
    """Read WS frames until (and including) the terminal ``done`` frame."""
    frames: list[dict] = []
    while not frames or frames[-1]["type"] != "done":
        frames.append(ws.receive_json())
    return frames


def test_clone_streams_progress_then_done(write_config, tmp_path, monkeypatch):
    # The clone blocks until the WS has subscribed (it can't emit any progress
    # before `connected` is set, and the test only sets it AFTER reading the
    # running snapshot). So the WS is provably in-flight — its first frame is the
    # running snapshot, and it then observes the FULL live 50% -> 100% -> done
    # stream deterministically (no reliance on snapshot-vs-progress race timing).
    connected = threading.Event()

    def fake_clone(root, name, url, *, cfg, shallow, progress_cb):
        assert connected.wait(timeout=5), "websocket never subscribed before progress"
        progress_cb("Receiving objects:  50% (5/10)")
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
            assert first["type"] == "progress"  # the running snapshot (job not finished)
            connected.set()  # WS is subscribed -> release the clone to emit progress
            frames = [first, *_drain_to_done(ws)]

    assert frames[-1] == {"type": "done", "status": "done", "error": None}
    streamed = [f.get("percent") for f in frames if f["type"] == "progress"]
    assert 50 in streamed and 100 in streamed  # the full live progress stream was observed


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


def test_clone_done_webhook_fires_on_success(write_config, tmp_path, monkeypatch):
    def fake_clone(root, name, url, *, cfg, shallow, progress_cb):
        return None

    monkeypatch.setattr("clauster.app.clone_project", fake_clone)
    monkeypatch.setattr("clauster.app.validate_clone_url", lambda url, cfg: None)

    with _client(write_config, tmp_path, extra=_WEBHOOKS_CLONE_DONE) as client:
        rec = RecordingEmitter()
        client.app.state.runner._webhooks = rec
        resp = client.post(
            "/api/projects/clone", json={"name": "cloned", "url": "https://example.com/r.git"}
        )
        job_id = resp.json()["job_id"]
        with client.websocket_connect(f"/ws/clone-progress/{job_id}") as ws:
            _drain_to_done(ws)
        calls = wait_for_calls(rec)

    assert len(calls) == 1
    event, payload = calls[0]
    assert event == "clone-done"
    assert payload == {
        "event_type": "clone-done",
        "project": "cloned",
        "status": "done",
        "error": None,
    }


def test_clone_done_webhook_redacts_error_and_omits_url(write_config, tmp_path, monkeypatch):
    from clauster.app import ProvisionError

    # A failure detail can echo a session/env id; it must be redacted before egress.
    def fake_clone(root, name, url, *, cfg, shallow, progress_cb):
        raise ProvisionError("clone failed for session_01ARZ3NDEKTSV4RRFFQ69G5FAV")

    monkeypatch.setattr("clauster.app.clone_project", fake_clone)
    monkeypatch.setattr("clauster.app.validate_clone_url", lambda url, cfg: None)

    with _client(write_config, tmp_path, extra=_WEBHOOKS_CLONE_DONE) as client:
        rec = RecordingEmitter()
        client.app.state.runner._webhooks = rec
        resp = client.post(
            "/api/projects/clone", json={"name": "broken", "url": "https://example.com/r.git"}
        )
        job_id = resp.json()["job_id"]
        with client.websocket_connect(f"/ws/clone-progress/{job_id}") as ws:
            _drain_to_done(ws)
        calls = wait_for_calls(rec)

    assert len(calls) == 1
    _event, payload = calls[0]
    assert payload["status"] == "error"
    # The raw session id is masked; the clone url never appears at all.
    assert "session_01ARZ3NDEKTSV4RRFFQ69G5FAV" not in (payload["error"] or "")
    assert "<redacted>" in (payload["error"] or "")
    assert "example.com" not in json.dumps(payload)


def test_clone_done_webhook_silent_when_event_default_off(write_config, tmp_path, monkeypatch):
    # webhooks enabled but clone-done NOT opted in -> the real emitter's wants() gate
    # drops it: no aemit task is ever scheduled (default off).
    def fake_clone(root, name, url, *, cfg, shallow, progress_cb):
        return None

    monkeypatch.setattr("clauster.app.clone_project", fake_clone)
    monkeypatch.setattr("clauster.app.validate_clone_url", lambda url, cfg: None)

    enabled_no_event = "webhooks:\n  enabled: true\n  urls: ['https://hook.test/h']\n"
    with _client(write_config, tmp_path, extra=enabled_no_event) as client:
        # Real WebhookEmitter (active, clone-done absent -> wants() False). Spy on aemit:
        # it must never be called for a default-off event.
        emitter = client.app.state.runner._webhooks
        assert emitter.active and emitter.wants("clone-done") is False
        aemit_calls: list = []
        orig_aemit = emitter.aemit

        async def _spy(event, payload):
            aemit_calls.append(event)
            await orig_aemit(event, payload)

        monkeypatch.setattr(emitter, "aemit", _spy)
        resp = client.post(
            "/api/projects/clone", json={"name": "cloned", "url": "https://example.com/r.git"}
        )
        job_id = resp.json()["job_id"]
        with client.websocket_connect(f"/ws/clone-progress/{job_id}") as ws:
            _drain_to_done(ws)
        # Negative assertion: confirm no emit fires across a window, failing fast if one does.
        assert_stays_empty(aemit_calls)
