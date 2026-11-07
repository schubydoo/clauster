"""``POST /api/instances/{id}/restart``: stop + resume through each row's own path (TASK-459).

The bridge cases run the real runner against the fake ``claude`` (``runner_config``), so a
restart really stops one process and starts another. The hosted cases reuse the stub manager
and daemon from ``test_app_hosted`` so the focus is the route's ordering and error mapping.
"""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

import clauster.routes.instances as instances_module
from clauster import claude_cli
from clauster.app import create_app
from clauster.hosted import HostedSessionError
from clauster.runner import PermissionModeNotAllowed, SessionRunner, SpawnError, UnknownProject
from test_app_hosted import _HID, _app, _StubDaemon, _StubManager
from test_sandbox_removed import _argv

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="pty mode is POSIX-only")


def _bridge_app(runner_config):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    return create_app(config, runner=runner), runner


# -- bridges ---------------------------------------------------------------


def test_restart_standard_bridge_starts_a_new_process(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    app, _runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        first = client.post("/api/instances", json={"project": "alpha"})
        assert first.status_code == 201, first.text
        iid = first.json()["instance_id"]
        old_pid = first.json()["bridge_pid"]

        r = client.post(f"/api/instances/{iid}/restart")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] is True
        assert body["status"] == "running"
        assert body["resume_mode"] == "standard"
        assert body["bridge_pid"] != old_pid  # a new process, so the binary on disk now
        assert "remote-control" in _argv(body)  # the subcommand form, never the pty flag
        assert iid not in app.state.restarts_in_flight  # the slot is released
        client.delete(f"/api/instances/{iid}")


@_POSIX_ONLY
def test_restart_pty_bridge_continues_the_conversation(runner_config):
    app, _runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        first = client.post("/api/instances", json={"project": "alpha", "resume_mode": "pty"})
        assert first.status_code == 201, first.text
        iid = first.json()["instance_id"]

        r = client.post(f"/api/instances/{iid}/restart")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] is True
        assert body["resume_mode"] == "pty"
        assert "--continue" in _argv(body)  # the pty path resumes the same conversation
        client.delete(f"/api/instances/{iid}")


def test_restart_bridge_failing_start_leaves_a_true_stopped_row(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    app, runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        first = client.post("/api/instances", json={"project": "alpha"})
        iid = first.json()["instance_id"]

        async def _boom(_iid):
            raise SpawnError("bridge would not come back up")

        monkeypatch.setattr(runner, "resume_detailed", _boom)
        r = client.post(f"/api/instances/{iid}/restart")
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "stopped, but it did not start again" in detail
        assert "bridge would not come back up" in detail
        # The row says what is true: stopped (and so Resumable), never "restarting".
        assert client.get(f"/api/instances/{iid}").json()["status"] == "stopped"
        assert iid not in app.state.restarts_in_flight


def test_restart_bridge_missing_binary_refuses_before_the_stop(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    app, runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        first = client.post("/api/instances", json={"project": "alpha"})
        iid = first.json()["instance_id"]

        def _gone(_binary):
            raise claude_cli.ClaudeNotFound("claude binary not found")

        monkeypatch.setattr(claude_cli, "resolve_binary", _gone)
        r = client.post(f"/api/instances/{iid}/restart")
        assert r.status_code == 503, r.text
        row = runner.get_instance(iid)
        assert row is not None
        assert row.status.value == "running"  # never stopped
        monkeypatch.undo()
        client.delete(f"/api/instances/{iid}")


def test_restart_is_single_flight_per_row(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    app, runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        first = client.post("/api/instances", json={"project": "alpha"})
        iid = first.json()["instance_id"]
        app.state.restarts_in_flight.add(iid)  # a restart of this row is running

        async def _never(_iid):
            raise AssertionError("a second restart must not stop the row")

        monkeypatch.setattr(runner, "stop", _never)
        r = client.post(f"/api/instances/{iid}/restart")
        assert r.status_code == 409
        assert "already running" in r.json()["detail"]
        assert iid in app.state.restarts_in_flight  # the first restart still owns the slot
        monkeypatch.undo()
        app.state.restarts_in_flight.discard(iid)
        client.delete(f"/api/instances/{iid}")


def test_restart_unknown_row_is_404(runner_config):
    app, _runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        assert client.post("/api/instances/ghost/restart").status_code == 404


# -- hosted ----------------------------------------------------------------


def _hosted_ready(monkeypatch):
    monkeypatch.setattr(instances_module, "is_trusted", lambda *a, **k: True)
    monkeypatch.setattr(claude_cli, "resolve_binary", lambda b: "/usr/bin/claude")


def test_restart_hosted_stops_then_resumes(write_config, projects_root, monkeypatch):
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 200, r.text
    assert manager.stopped == [_HID]
    assert manager.resumed[0]["id"] == _HID
    assert manager.resumed[0]["binary"] == "/usr/bin/claude"
    assert r.json()["claustrum_process_id"] == "hid-2"
    assert r.json()["created"] is True


def test_restart_hosted_orphan_is_killed_then_resumed(write_config, projects_root, monkeypatch):
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    manager.seed()
    manager.sessions.pop(_HID)  # no live session: an orphan that survived a daemon restart
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 200, r.text
    assert manager.killed_orphans == [_HID]
    assert manager.stopped == []


def test_restart_hosted_failing_resume_names_the_stop(write_config, projects_root, monkeypatch):
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    manager.seed()
    manager.resume_error = HostedSessionError("no captured session uuid to resume from")
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 409, r.text
    assert "stopped, but it did not start again" in r.json()["detail"]
    assert manager.stopped == [_HID]
    assert manager.instances[_HID].status.value == "stopped"  # a true state, not "restarting"
    assert _HID not in app.state.restarts_in_flight


def test_restart_hosted_without_daemon_refuses_before_the_stop(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager, daemon=None)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 503
    assert manager.stopped == []  # the live session is untouched


def test_restart_hosted_with_a_degraded_project_is_409(write_config, projects_root):
    manager = _StubManager()
    manager.seed().project = ""
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 409
    assert manager.stopped == []


def test_restart_hosted_row_vanishing_at_stop_is_404(write_config, projects_root, monkeypatch):
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    manager.seed()

    async def _gone(hosted_id: str):
        raise HostedSessionError(f"no such hosted session: {hosted_id}")

    monkeypatch.setattr(manager, "stop", _gone)
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 404
    assert manager.resumed == []


def test_restart_bridge_vanishing_before_the_stop_is_404(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    app, runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        with monkeypatch.context() as m:
            m.setattr(runner, "get_instance", lambda _iid: None)
            assert client.post(f"/api/instances/{iid}/restart").status_code == 404

        async def _gone(instance_id: str):
            raise UnknownProject(f"no managed instance: {instance_id!r}")

        with monkeypatch.context() as m:
            m.setattr(runner, "stop", _gone)
            r = client.post(f"/api/instances/{iid}/restart")
        assert r.status_code == 404
        client.delete(f"/api/instances/{iid}")


def test_restart_bridge_precondition_failure_does_not_notify(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    app, runner = _bridge_app(runner_config)
    notified: list[tuple] = []
    monkeypatch.setattr(runner, "notify_app_event", lambda *a: notified.append(a))
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]

        async def _denied(_iid: str):
            raise PermissionModeNotAllowed("bypassPermissions is not allowed")

        monkeypatch.setattr(runner, "resume_detailed", _denied)
        r = client.post(f"/api/instances/{iid}/restart")
    assert r.status_code == 403
    assert "stopped, but it did not start again" in r.json()["detail"]
    assert notified == []  # only a genuine spawn failure (409) is a failed reconnect


def test_restart_bridge_spawn_failure_notifies(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    app, runner = _bridge_app(runner_config)
    notified: list[tuple] = []
    monkeypatch.setattr(runner, "notify_app_event", lambda *a: notified.append(a))
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]

        async def _boom(_iid: str):
            raise SpawnError("nope")

        monkeypatch.setattr(runner, "resume_detailed", _boom)
        assert client.post(f"/api/instances/{iid}/restart").status_code == 409
    assert notified and notified[0][0] == "reconnect-failed"
