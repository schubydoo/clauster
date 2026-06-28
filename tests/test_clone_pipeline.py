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

    def fake_clone(root, name, url, *, cfg, shallow, progress_cb, on_proc=None):
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


def test_clone_cancel_streams_cancelled_terminal_frame(write_config, tmp_path, monkeypatch):
    # #573 wiring: POST /clone/{job_id}/cancel must actually stop the in-flight git
    # transfer, not just close the client WS. The fake clone hands its "process" to
    # on_proc (the route's _register_proc -> job.register_terminate(proc.terminate)),
    # signals it spawned, then blocks until terminate() fires. terminate() is what the
    # cancel endpoint reaches through the registered hook; it raises ProvisionError out
    # of the worker (a terminated git exits non-zero -> CloneFailed). Because the job's
    # cancel_requested flag is set, the route reports a clean `cancelled` over the WS.
    from clauster.app import ProvisionError

    spawned = threading.Event()
    terminated = threading.Event()

    class _FakeProc:
        def terminate(self) -> None:
            terminated.set()

    def fake_clone(root, name, url, *, cfg, shallow, progress_cb, on_proc=None):
        on_proc(_FakeProc())  # hand the process to the job so cancel can terminate it
        spawned.set()  # the git "process" is live -> a cancel now has something to kill
        assert terminated.wait(timeout=5), "clone never terminated by cancel"
        raise ProvisionError("clone failed: terminated")  # terminated git -> non-zero

    monkeypatch.setattr("clauster.app.clone_project", fake_clone)
    monkeypatch.setattr("clauster.app.validate_clone_url", lambda url, cfg: None)

    with _client(write_config, tmp_path) as client:
        resp = client.post(
            "/api/projects/clone", json={"name": "cancelme", "url": "https://example.com/r.git"}
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]

        with client.websocket_connect(f"/ws/clone-progress/{job_id}") as ws:
            first = ws.receive_json()
            assert first["type"] == "progress"  # the running snapshot (job not finished)
            assert spawned.wait(timeout=5), "clone never spawned its process"
            cancel = client.post(f"/api/projects/clone/{job_id}/cancel")
            assert cancel.status_code == 202, cancel.text
            assert cancel.json() == {"job_id": job_id, "cancelling": True}
            frames = [first, *_drain_to_done(ws)]

    assert frames[-1] == {"type": "done", "status": "cancelled", "error": None}


def test_clone_cancel_after_completion_cleans_up_and_reports_cancelled(
    write_config, tmp_path, monkeypatch
):
    # #573 race: a cancel arrives but `proc.terminate()` is a no-op because git already
    # finished and the dir landed — so the worker takes the SUCCESS path, not the abort
    # path. The success branch must still honor cancel_requested: tear down the just-
    # created project and broadcast `cancelled` (not `done`), matching the 202 contract.
    spawned = threading.Event()
    terminated = threading.Event()

    class _FakeProc:
        def terminate(self) -> None:
            terminated.set()  # no-op kill: git already exited, the dir is already on disk

    def fake_clone(root, name, url, *, cfg, shallow, progress_cb, on_proc=None):
        target = Path(root) / name
        target.mkdir()  # the clone "succeeded": the project dir is on disk
        on_proc(_FakeProc())
        spawned.set()
        assert terminated.wait(timeout=5), "cancel never reached the terminate hook"
        return target  # success path: git finished before terminate() could stop it

    monkeypatch.setattr("clauster.app.clone_project", fake_clone)
    monkeypatch.setattr("clauster.app.validate_clone_url", lambda url, cfg: None)

    with _client(write_config, tmp_path) as client:
        projects_root = client.app.state.config.projects_root
        resp = client.post(
            "/api/projects/clone", json={"name": "racey", "url": "https://example.com/r.git"}
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]

        with client.websocket_connect(f"/ws/clone-progress/{job_id}") as ws:
            first = ws.receive_json()
            assert first["type"] == "progress"
            assert spawned.wait(timeout=5), "clone never spawned its process"
            cancel = client.post(f"/api/projects/clone/{job_id}/cancel")
            assert cancel.status_code == 202, cancel.text
            frames = [first, *_drain_to_done(ws)]

    # The terminal frame is `cancelled`, not `done`, and the landed dir was torn down.
    assert frames[-1] == {"type": "done", "status": "cancelled", "error": None}
    assert not (projects_root / "racey").exists()


def test_clone_error_streams_terminal_error_frame(write_config, tmp_path, monkeypatch):
    from clauster.app import ProvisionError

    def fake_clone(root, name, url, *, cfg, shallow, progress_cb, on_proc=None):
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
    def fake_clone(root, name, url, *, cfg, shallow, progress_cb, on_proc=None):
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
    def fake_clone(root, name, url, *, cfg, shallow, progress_cb, on_proc=None):
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
    def fake_clone(root, name, url, *, cfg, shallow, progress_cb, on_proc=None):
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


def test_clone_active_lists_running_job_and_reattach_streams(write_config, tmp_path, monkeypatch):
    # #659 cross-tab: a clone started in one tab is discoverable by another via
    # GET /api/projects/clone/active, which carries the job id + name + live progress
    # (never the URL). A second watcher then reattaches to the same /ws/clone-progress
    # and sees the full live stream — the WS fans out to every subscriber.
    connected = threading.Event()

    def fake_clone(root, name, url, *, cfg, shallow, progress_cb, on_proc=None):
        progress_cb("Receiving objects:  40% (4/10)")  # so active/ reports a non-null percent
        assert connected.wait(timeout=5), "second watcher never subscribed"
        progress_cb("Receiving objects: 100% (10/10)")

    monkeypatch.setattr("clauster.app.clone_project", fake_clone)
    monkeypatch.setattr("clauster.app.validate_clone_url", lambda url, cfg: None)

    with _client(write_config, tmp_path) as client:
        resp = client.post(
            "/api/projects/clone",
            json={"name": "shared", "url": "https://secret:token@example.com/r.git"},
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]

        with client.websocket_connect(f"/ws/clone-progress/{job_id}") as first_ws:
            first_ws.receive_json()  # drain the running snapshot for the first watcher
            # A SECOND tab loads and polls the active-clones endpoint.
            active = client.get("/api/projects/clone/active")
            assert active.status_code == 200, active.text
            jobs = active.json()["jobs"]
            assert len(jobs) == 1
            job = jobs[0]
            assert job["job_id"] == job_id and job["name"] == "shared"
            assert "url" not in job  # the credential-bearing clone URL is never surfaced
            assert "token" not in active.text  # nothing leaks the URL anywhere in the body
            # That second tab reattaches to the live stream using the discovered id.
            with client.websocket_connect(f"/ws/clone-progress/{job_id}") as second_ws:
                snap = second_ws.receive_json()
                assert snap["type"] == "progress"  # the live snapshot on reconnect
                connected.set()  # release the clone to finish -> both watchers see done
                frames = _drain_to_done(second_ws)
            assert frames[-1] == {"type": "done", "status": "done", "error": None}


def test_clone_active_empty_when_no_clone_in_flight(write_config, tmp_path):
    # With nothing cloning, the endpoint returns an empty list (not 404) so a fresh tab's
    # reattach check is a cheap no-op.
    with _client(write_config, tmp_path) as client:
        resp = client.get("/api/projects/clone/active")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"jobs": []}


def test_clone_active_excludes_finished_job(write_config, tmp_path, monkeypatch):
    # A completed clone lingers in the registry for the reconnect-TTL window, but the
    # active list must exclude it — a fresh tab must not reattach to a finished clone.
    def fake_clone(root, name, url, *, cfg, shallow, progress_cb, on_proc=None):
        (Path(root) / name).mkdir()  # land the dir so the success path runs

    monkeypatch.setattr("clauster.app.clone_project", fake_clone)
    monkeypatch.setattr("clauster.app.validate_clone_url", lambda url, cfg: None)

    with _client(write_config, tmp_path) as client:
        resp = client.post(
            "/api/projects/clone", json={"name": "quick", "url": "https://example.com/r.git"}
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        # Drain the WS to its terminal frame so the job is provably finished before we poll.
        with client.websocket_connect(f"/ws/clone-progress/{job_id}") as ws:
            frames = _drain_to_done(ws)
        assert frames[-1]["status"] == "done"
        active = client.get("/api/projects/clone/active")
        assert active.status_code == 200, active.text
        assert active.json() == {"jobs": []}  # the finished job is not listed
