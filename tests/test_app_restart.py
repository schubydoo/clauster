"""``POST /api/instances/{id}/restart``: stop + resume through each row's own path (TASK-459).

The bridge cases run the real runner against the fake ``claude`` (``runner_config``), so a
restart really stops one process and starts another. The hosted cases reuse the stub manager
and daemon from ``test_app_hosted`` so the focus is the route's ordering and error mapping.
"""

from __future__ import annotations

import asyncio
import sys
import threading

import pytest
from fastapi.testclient import TestClient

import clauster.routes.instances as instances_module
from clauster import claude_cli
from clauster.app import create_app
from clauster.hosted import HostedSessionError
from clauster.models import InstanceStatus
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


_UUID = "0b7a3c1e-2f4d-4e6a-9b8c-1d2e3f4a5b6c"


def _seed(manager: _StubManager):
    inst = manager.seed()
    inst.claude_session_uuid = _UUID  # a usable --resume token, so a restart can resume
    return inst


def _hosted_ready(monkeypatch):
    monkeypatch.setattr(instances_module, "is_trusted", lambda *a, **k: True)
    monkeypatch.setattr(claude_cli, "resolve_binary", lambda b: "/usr/bin/claude")


def test_restart_hosted_stops_then_resumes(write_config, projects_root, monkeypatch):
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    _seed(manager)
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
    _seed(manager)
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
    _seed(manager)
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
    _seed(manager)
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
    _seed(manager)

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


def test_restart_bridge_new_process_crashing_at_start_is_an_error(runner_config, monkeypatch):
    # The likeliest failure after an update: the new claude release dies at start. The
    # resume does not raise for that; it hands back an ERROR row. The restart must not
    # report it as a success.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    app, _runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
        r = client.post(f"/api/instances/{iid}/restart")
        assert r.status_code == 409, r.text
        assert "stopped, but it did not start again" in r.json()["detail"]
        assert client.get(f"/api/instances/{iid}").json()["status"] == "error"


def test_restart_bridge_untrusted_refuses_before_the_stop(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    app, runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        with monkeypatch.context() as m:
            m.setattr(instances_module, "is_trusted", lambda *a, **k: False)
            r = client.post(f"/api/instances/{iid}/restart")
        assert r.status_code == 409, r.text
        assert "not trusted" in r.json()["detail"]
        row = runner.get_instance(iid)
        assert row is not None
        assert row.status is InstanceStatus.RUNNING  # never stopped
        client.delete(f"/api/instances/{iid}")


def test_restart_bridge_bypass_ceiling_refuses_before_the_stop(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, _ = runner_config
    app, runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        with monkeypatch.context() as m:
            m.setattr(type(config), "bypass_denied", lambda self, project, mode: True)
            r = client.post(f"/api/instances/{iid}/restart")
        assert r.status_code == 403, r.text
        row = runner.get_instance(iid)
        assert row is not None
        assert row.status is InstanceStatus.RUNNING
        client.delete(f"/api/instances/{iid}")


def test_restart_concurrent_second_request_is_409(runner_config, monkeypatch):
    # No hand-seeded slot: the first restart takes it, and a second POST while the first is
    # inside its stop is refused.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    app, runner = _bridge_app(runner_config)
    entered, release = threading.Event(), threading.Event()
    real_stop = runner.stop

    async def _held_stop(instance_id: str):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return await real_stop(instance_id)

    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        monkeypatch.setattr(runner, "stop", _held_stop)
        first: dict = {}
        worker = threading.Thread(
            target=lambda: first.update(r=client.post(f"/api/instances/{iid}/restart"))
        )
        worker.start()
        assert entered.wait(10)
        second = client.post(f"/api/instances/{iid}/restart")
        release.set()
        worker.join(30)
        assert second.status_code == 409
        assert "already running" in second.json()["detail"]
        assert first["r"].status_code == 200, first["r"].text
        monkeypatch.undo()
        client.delete(f"/api/instances/{iid}")


def test_restart_hosted_without_a_usable_uuid_refuses_before_the_stop(
    write_config, projects_root, monkeypatch
):
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    manager.seed()  # still starting: no conversation id captured yet
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 409
    assert "no usable conversation id" in r.json()["detail"]
    assert manager.stopped == []


def test_restart_hosted_bypass_ceiling_refuses_before_the_stop(
    write_config, projects_root, monkeypatch
):
    from clauster.config import ClausterConfig

    _hosted_ready(monkeypatch)
    manager = _StubManager()
    _seed(manager)
    monkeypatch.setattr(ClausterConfig, "bypass_denied", lambda self, project, mode: True)
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 403
    assert manager.stopped == []


def test_restart_hosted_stop_that_does_not_complete_is_502(
    write_config, projects_root, monkeypatch
):
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    inst = _seed(manager)

    async def _stuck(hosted_id: str):
        manager.stopped.append(hosted_id)
        return inst  # still RUNNING: the kill's last wait timed out

    monkeypatch.setattr(manager, "stop", _stuck)
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 502
    assert "may still be running" in r.json()["detail"]
    assert manager.resumed == []


def test_restart_hosted_slot_is_keyed_by_process_id(write_config, projects_root, monkeypatch):
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    _seed(manager)
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    app.state.restarts_in_flight.add(_HID)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 409
    assert manager.stopped == []


# -- dashboard pins --------------------------------------------------------
# Substring pins, brittle by design: there is no JS harness, so each one is the only thing
# that notices the matching branch being deleted.


def _dashboard(write_config, monkeypatch) -> str:
    import clauster.app as app_module
    from clauster.config import load_config
    from test_app_hosted import _NoopDaemon

    monkeypatch.setattr(app_module, "ClaustrumDaemon", _NoopDaemon)
    app = create_app(load_config(write_config("claustrum:\n  enabled: true\n")))
    app.state.hosted = _StubManager()
    with TestClient(app) as client:
        return client.get("/").text


def test_dashboard_bridge_restart_button_and_guards(write_config, monkeypatch):
    page = _dashboard(write_config, monkeypatch)
    assert 'data-test="restart-session"' in page
    # Single-flight, and never on a row still starting or stopping.
    assert ':disabled="!!restarting[i.rk] || isStopping(i.rk) || isBusy(i.rk)"' in page
    assert ':disabled="isStopping(i.rk) || !!restarting[i.rk]"' in page  # Stop waits too
    assert "if (this.restarting[key]) return;" in page
    assert "finally { this.restarting[key] = false; }" in page
    # A row mid-restart stays in Active and out of Recent (no racing Resume/Forget there).
    assert "(i) => live.includes(i.status) || !!this.restarting[i.rk]" in page
    assert "&& !this.restarting[i.rk]" in page
    assert ': a restart is running"' in page
    # The confirm names what each mode loses.
    assert "Every live remote session on this bridge ends." in page
    assert "comes back in the same conversation (--continue)." in page
    # A 2xx without a row never reaches _absorbRow.
    assert "} else if (body.instance_id) {" in page
    # The error is on the row, in Active and in Recent.
    assert 'data-test="restart-error"' in page
    assert 'data-test="restart-error-recent"' in page


def test_dashboard_hosted_restart_button_and_guards(write_config, monkeypatch):
    page = _dashboard(write_config, monkeypatch)
    assert 'data-test="hosted-restart"' in page
    # Same uuid + project gate as Resume, so Restart is offered only where Resume can follow.
    assert (
        "['running', 'starting'].includes(h.status) && h.claude_session_uuid_usable"
        " && h.project" in page
    )
    assert "if (this.hostedRestarting[id]) return;" in page
    assert "finally { this.hostedRestarting[id] = false; }" in page
    assert "|| !!this.hostedRestarting[h.claustrum_process_id]);" in page
    assert "&& !this.hostedRestarting[h.claustrum_process_id]);" in page
    assert 'data-test="hosted-restart-error"' in page
