"""``POST /api/instances/{id}/restart``: stop + resume through each row's own path (TASK-459).

The bridge cases run the real runner against the fake ``claude`` (``runner_config``), so a
restart really stops one process and starts another. The hosted cases reuse the stub manager
and daemon from ``test_app_hosted`` so the focus is the route's ordering and error mapping.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

import clauster.routes.instances as instances_module
from clauster import claude_cli, environments, pointers
from clauster.app import create_app
from clauster.claustrum_client import ClaustrumError
from clauster.hosted import HostedAgentMayBeRunning, HostedSessionError
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


def test_restart_never_passes_the_no_create_flag(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, _claude_json = runner_config
    config.instance_defaults.allow_force_restart = True  # even with Force restart on
    app, _runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        r = client.post(f"/api/instances/{iid}/restart")
        assert r.status_code == 200, r.text
        assert "--no-create-session-in-dir" not in _argv(r.json())
        # Only Force restart writes a pointer: a plain bridge writes its own.
        assert (
            pointers.pointer_for_project(
                _runner._project_path("alpha"), _runner.claude_projects_dir
            )
            is None
        )
        client.delete(f"/api/instances/{iid}")


# -- Force restart -----------------------------------------------------------


class _FakeEnvClient:
    """Stands in for environments.EnvironmentsClient: records archives, can fail."""

    def __init__(self, fail_status: int | None = None, raises: Exception | None = None) -> None:
        self.archived: list[str] = []
        self.fail_status = fail_status
        self.raises = raises

    def archive_environment(self, env_id: str) -> None:
        self.archived.append(env_id)
        if self.raises is not None:
            raise self.raises
        if self.fail_status is not None:
            raise environments.EnvironmentsAPIError(
                self.fail_status, f"Environment {env_id} not found."
            )


def _force_app(runner_config, monkeypatch, *, fail_status=None, raises=None, creds_error=False):
    """A bridge app with Force restart on and the environments API stubbed out."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    config.instance_defaults.allow_force_restart = True
    fake = _FakeEnvClient(fail_status, raises)

    def _load(credentials_path, claude_json_path, **_k):
        # The login beside the runner's own claude.json, never the real HOME's.
        assert credentials_path == claude_json.parent / ".claude" / ".credentials.json"
        assert claude_json_path == claude_json
        if creds_error:
            raise environments.CredentialsError("no credentials file")
        return object()

    monkeypatch.setattr(environments, "load_credentials", _load)
    monkeypatch.setattr(environments, "EnvironmentsClient", lambda _creds: fake)
    app, runner = _bridge_app(runner_config)
    return app, runner, fake


def _seed_pointer(runner, project="alpha"):
    """Write a non-live bridge pointer for ``project`` and return its path."""
    path = pointers.pointer_path_for(runner._project_path(project), runner.claude_projects_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "sessionId": "",
                "environmentId": "env_01RHE7cHW3DawXjGRp5Ae3va",
                "source": "standalone",
                "pid": 81750,  # long dead -> not live
                "procStart": "2590192",
            }
        )
    )
    return path


def test_force_restart_is_off_by_default(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    app, runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        first = client.post("/api/instances", json={"project": "alpha"}).json()
        r = client.post(f"/api/instances/{first['instance_id']}/force-restart")
        assert r.status_code == 403
        assert "allow_force_restart" in r.json()["detail"]
        assert runner.get_instance(first["instance_id"]).bridge_pid == first["bridge_pid"]
        client.delete(f"/api/instances/{first['instance_id']}")


def test_dashboard_shows_force_restart_only_when_on(write_config):
    from clauster.config import load_config

    def _page(extra: str) -> str:
        with TestClient(create_app(load_config(write_config(extra)))) as client:
            return client.get("/").text

    assert 'data-test="force-restart-session"' not in _page("")
    on = _page("instance_defaults:\n  allow_force_restart: true\n")
    assert 'data-test="force-restart-session"' in on
    assert "restart(i.rk, true)" in on
    assert "i.resume_mode !== 'pty' && !!i.environment_id" in on  # only where it can work
    assert "This ARCHIVES the bridge's cloud environment" in on  # the confirm says so
    # A bridge with no starter session has no session_url: the Open link and QR fall back to
    # the environment link, like the QR route, or the row spins on "Preparing connect link…".
    assert "const u = row.session_url || row.url;" in on


def test_force_restart_archives_env_drops_pointer_and_skips_session(runner_config, monkeypatch):
    app, runner, fake = _force_app(runner_config, monkeypatch)
    with TestClient(app) as client:
        first = client.post("/api/instances", json={"project": "alpha"})
        assert first.status_code == 201, first.text
        iid = first.json()["instance_id"]
        env_id = runner.get_instance(iid).environment_id
        assert env_id  # the fake claude logs one, as the real bridge does
        pointer = _seed_pointer(runner)

        r = client.post(f"/api/instances/{iid}/force-restart")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "running"
        assert body["bridge_pid"] != first.json()["bridge_pid"]
        assert fake.archived == [env_id]  # its own environment, nothing else
        assert pointer.with_name(pointer.name + ".bak").exists()
        # The old pointer is gone (or the next plain start revives the archived env). The new
        # bridge writes none itself, so clauster writes one naming the NEW environment and pid;
        # without it a clauster restart reattaches the live bridge as STARTING forever.
        new = runner.get_instance(iid)
        if new.bridge_start_ticks is None:  # macOS/Windows: no CLI-format procStart
            assert not pointer.exists()
            assert any("without a bridge pointer" in w for w in body["warnings"])
        else:
            written = pointers.load_pointer(pointer)
            assert written is not None
            assert written.environment_id == new.environment_id
            assert written.environment_id != "env_01RHE7cHW3DawXjGRp5Ae3va"  # not the old one
            assert written.session_id == ""
            assert written.pid == body["bridge_pid"]
            # The CLI's procStart format: boot-relative ticks, not our epoch float. A wrong
            # format still parses as "no comparable start" and is accepted, so pin it.
            assert written.proc_start == str(new.bridge_start_ticks)
            assert pointers.is_live(written)  # what the reattach reads to say RUNNING
            assert body["warnings"] == []
        argv = _argv(body)
        assert "--no-create-session-in-dir" in argv
        assert "remote-control" in argv
        assert iid not in app.state.restarts_in_flight

        # A later plain Resume is a normal start again: it keeps the pre-created session.
        assert client.delete(f"/api/instances/{iid}").status_code == 200
        resumed = client.post(f"/api/instances/{iid}/resume")
        assert resumed.status_code == 200, resumed.text
        assert "--no-create-session-in-dir" not in _argv(resumed.json())
        client.delete(f"/api/instances/{iid}")


def test_open_qr_panel_reloads_when_the_link_changes(runner_config):
    # TASK-557: an open panel must refetch when the link moves, or it keeps showing the code
    # of the session that ended. The link itself must stay out of the image URL.
    app, _runner = _bridge_app(runner_config)
    with TestClient(app) as client:
        page = client.get("/").text
    assert "this.refreshOpenQrs(this._byId, byId);" in page
    assert "this.refreshOpenQrs({ [body.rk]: this._byId[body.rk] }, { [body.rk]: body });" in page
    assert "(before.session_url || before.url) !== (row.session_url || row.url)" in page
    assert '"/qr?t=" + (this.qrNonce[name] || 0);' in page


def test_force_restart_carries_the_old_pointers_identity(runner_config, monkeypatch):
    # TASK-561: the CLI copies identity from the pointer it reuses, so a replacement written
    # without it would keep this environment on the 4-hour TTL for good.
    app, runner, _fake = _force_app(runner_config, monkeypatch)
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        pointer = _seed_pointer(runner)
        identity = {
            "dir": str(runner._project_path("alpha")),
            "machineId": "machine-01",
            "accountUuid": "11111111-2222-3333-4444-555555555555",
            "organizationUuid": "66666666-7777-8888-9999-000000000000",
        }
        pointer.write_text(json.dumps({**json.loads(pointer.read_text()), **identity}))

        r = client.post(f"/api/instances/{iid}/force-restart")
        assert r.status_code == 200, r.text
        if runner.get_instance(iid).bridge_start_ticks is None:
            pytest.skip("no CLI-format procStart on this OS, so no pointer is written")
        written = json.loads(pointer.read_text())
        assert written["sessionId"] == ""
        assert written["environmentId"] == r.json()["environment_id"]
        assert {key: written[key] for key in identity} == identity
        client.delete(f"/api/instances/{iid}")


def test_force_restarted_bridge_reattaches_running_after_a_clauster_restart(
    runner_config, monkeypatch
):
    # Seen live: after a clauster restart the force-restarted podspine row sat on "Starting"
    # forever, because the bridge writes no pointer and a reattach says RUNNING only from a
    # live one. A second runner on the same state is that restart.
    app, runner, _fake = _force_app(runner_config, monkeypatch)
    config, claude_json = runner_config
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        r = client.post(f"/api/instances/{iid}/force-restart")
        assert r.status_code == 200, r.text
        if runner.get_instance(iid).bridge_start_ticks is None:
            pytest.skip("no CLI-format procStart on this OS, so no pointer is written")

        restarted = SessionRunner(config, claude_json=claude_json)
        # The reattach reads the pointer from the default ~/.claude/projects, which in prod is
        # the runner's own dir; here claude.json sits in a tmp dir, so point it there.
        real_lookup = pointers.pointer_for_project
        monkeypatch.setattr(
            "clauster.rediscovery.pointers.pointer_for_project",
            lambda path, *_a: real_lookup(path, restarted.claude_projects_dir),
        )
        asyncio.run(restarted.rediscover(persist=False))
        again = restarted.get_instance(iid)
        assert again is not None
        assert again.status is InstanceStatus.RUNNING
        assert again.environment_id == r.json()["environment_id"]
        assert again.url == f"https://claude.ai/code?environment={again.environment_id}"
        assert again.starter_session_id is None  # the stripped pointer's "" is no session
        assert again.session_url is None  # never the broken https://claude.ai/code/?from=cli
        client.delete(f"/api/instances/{iid}")


def test_force_restart_without_start_ticks_warns_instead_of_writing(runner_config, monkeypatch):
    # macOS/Windows have no CLI-format procStart: no pointer, and the response says why.
    from clauster import procutil

    app, runner, _fake = _force_app(runner_config, monkeypatch)
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        real = procutil.proc_start_pair
        monkeypatch.setattr(procutil, "proc_start_pair", lambda pid: (real(pid)[0], None))
        r = client.post(f"/api/instances/{iid}/force-restart")
        assert r.status_code == 200, r.text
        assert any("without a bridge pointer" in w for w in r.json()["warnings"])
        assert (
            pointers.pointer_for_project(runner._project_path("alpha"), runner.claude_projects_dir)
            is None
        )
        client.delete(f"/api/instances/{iid}")


def test_force_restart_pointer_write_failure_is_a_warning(runner_config, monkeypatch):
    app, runner, _fake = _force_app(runner_config, monkeypatch)

    def _boom(*_a, **_k):
        raise OSError("read-only file system")

    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        monkeypatch.setattr(pointers, "write_reuse_pointer", _boom)
        r = client.post(f"/api/instances/{iid}/force-restart")
        assert r.status_code == 200, r.text  # the bridge runs; only the pointer is missing
        assert r.json()["status"] == "running"
        # A host without start ticks (Windows; macOS) never reaches the write: it gets the
        # no-ticks warning instead, which test_..._without_start_ticks_... covers on Linux.
        expected = (
            "without a bridge pointer"
            if runner.get_instance(iid).bridge_start_ticks is None
            else "could not write the bridge pointer"
        )
        assert any(expected in w for w in r.json()["warnings"]), r.json()["warnings"]
        client.delete(f"/api/instances/{iid}")


def test_force_restart_treats_a_gone_environment_as_clear(runner_config, monkeypatch):
    app, _runner, fake = _force_app(runner_config, monkeypatch, fail_status=404)
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        r = client.post(f"/api/instances/{iid}/force-restart")
        assert r.status_code == 200, r.text
        assert len(fake.archived) == 1
        assert "--no-create-session-in-dir" in _argv(r.json())
        client.delete(f"/api/instances/{iid}")


def test_force_restart_failed_archive_stops_without_starting(runner_config, monkeypatch):
    app, runner, _fake = _force_app(runner_config, monkeypatch, fail_status=500)
    with TestClient(app) as client:
        first = client.post("/api/instances", json={"project": "alpha"}).json()
        iid = first["instance_id"]
        env_id = runner.get_instance(iid).environment_id
        r = client.post(f"/api/instances/{iid}/force-restart")
        assert r.status_code == 502
        detail = r.json()["detail"]
        assert "stopped, but it did not start again" in detail
        assert "archive" in detail
        assert env_id not in detail  # an env_ id is a bearer credential: redacted
        row = runner.get_instance(iid)
        assert row.status is InstanceStatus.STOPPED
        assert str(row.bridge_debug_log_path) == first["bridge_debug_log_path"]  # no new spawn


def test_force_restart_network_error_stops_without_starting(runner_config, monkeypatch):
    # The transport raises timeouts and TLS errors unwrapped; they must not become a bare 500.
    app, runner, _fake = _force_app(runner_config, monkeypatch, raises=TimeoutError("timed out"))
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        r = client.post(f"/api/instances/{iid}/force-restart")
        assert r.status_code == 502
        assert "stopped, but it did not start again" in r.json()["detail"]
        assert runner.get_instance(iid).status is InstanceStatus.STOPPED


def test_force_restart_live_pointer_refuses_before_the_archive(runner_config, monkeypatch):
    # Another live bridge owns the folder's pointer: its environment is never archived.
    app, runner, fake = _force_app(runner_config, monkeypatch)
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        _seed_pointer(runner)
        monkeypatch.setattr(pointers, "is_live", lambda _ptr: True)
        r = client.post(f"/api/instances/{iid}/force-restart")
        assert r.status_code == 409
        assert "another live bridge" in r.json()["detail"]
        assert fake.archived == []
        assert runner.get_instance(iid).status is InstanceStatus.STOPPED


def test_force_restart_pointer_that_cannot_go_stops_without_starting(runner_config, monkeypatch):
    app, runner, fake = _force_app(runner_config, monkeypatch)

    def _boom(*_a, **_k):
        raise OSError("read-only file system")

    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        monkeypatch.setattr(pointers, "remove_pointer", _boom)
        r = client.post(f"/api/instances/{iid}/force-restart")
        assert r.status_code == 409
        assert "could not remove its bridge pointer" in r.json()["detail"]
        assert len(fake.archived) == 1
        assert runner.get_instance(iid).status is InstanceStatus.STOPPED


def test_force_restart_refuses_a_stopped_row(runner_config, monkeypatch):
    # A stopped row's recorded id may name an environment a later bridge reused and serves.
    app, runner, fake = _force_app(runner_config, monkeypatch)
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        assert client.delete(f"/api/instances/{iid}").status_code == 200
        r = client.post(f"/api/instances/{iid}/force-restart")
        assert r.status_code == 409
        assert "needs a running bridge" in r.json()["detail"]
        assert fake.archived == []


def test_force_restart_refuses_a_malformed_env_id(runner_config, monkeypatch):
    app, runner, fake = _force_app(runner_config, monkeypatch)
    with TestClient(app) as client:
        first = client.post("/api/instances", json={"project": "alpha"}).json()
        runner.get_instance(first["instance_id"]).environment_id = "env_x/../../v1/other"
        r = client.post(f"/api/instances/{first['instance_id']}/force-restart")
        assert r.status_code == 409
        assert fake.archived == []
        assert runner.get_instance(first["instance_id"]).bridge_pid == first["bridge_pid"]
        client.delete(f"/api/instances/{first['instance_id']}")


def test_force_restart_without_login_refuses_before_the_stop(runner_config, monkeypatch):
    app, runner, fake = _force_app(runner_config, monkeypatch, creds_error=True)
    with TestClient(app) as client:
        first = client.post("/api/instances", json={"project": "alpha"}).json()
        r = client.post(f"/api/instances/{first['instance_id']}/force-restart")
        assert r.status_code == 503
        assert "claude login" in r.json()["detail"]
        assert fake.archived == []
        assert runner.get_instance(first["instance_id"]).bridge_pid == first["bridge_pid"]
        client.delete(f"/api/instances/{first['instance_id']}")


def test_force_restart_without_env_id_refuses_before_the_stop(runner_config, monkeypatch):
    app, runner, fake = _force_app(runner_config, monkeypatch)
    with TestClient(app) as client:
        first = client.post("/api/instances", json={"project": "alpha"}).json()
        runner.get_instance(first["instance_id"]).environment_id = None
        r = client.post(f"/api/instances/{first['instance_id']}/force-restart")
        assert r.status_code == 409
        assert "no cloud environment" in r.json()["detail"]
        assert fake.archived == []
        assert runner.get_instance(first["instance_id"]).bridge_pid == first["bridge_pid"]
        client.delete(f"/api/instances/{first['instance_id']}")


@_POSIX_ONLY
def test_force_restart_refuses_a_pty_bridge_before_the_stop(runner_config, monkeypatch):
    app, runner, fake = _force_app(runner_config, monkeypatch)
    with TestClient(app) as client:
        first = client.post("/api/instances", json={"project": "alpha", "resume_mode": "pty"})
        assert first.status_code == 201, first.text
        iid = first.json()["instance_id"]
        r = client.post(f"/api/instances/{iid}/force-restart")
        assert r.status_code == 409
        assert "standard" in r.json()["detail"]
        assert fake.archived == []
        assert runner.get_instance(iid).status is InstanceStatus.RUNNING
        client.delete(f"/api/instances/{iid}")


def test_force_restart_refuses_a_hosted_row(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(
        write_config,
        manager=manager,
        daemon=_StubDaemon(),
        extra_config="instance_defaults:\n  allow_force_restart: true\n",
    )
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/force-restart")
    assert r.status_code == 409
    assert "standard" in r.json()["detail"]
    assert manager.stopped == []


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

        async def _boom(_iid, **_kw):
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
    # The real grace window is seconds; the start-watch logic is the same at 0.05 s.
    monkeypatch.setattr(instances_module, "_HOSTED_RESTART_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(instances_module, "_HOSTED_RESTART_POLL_SECONDS", 0.01)


@pytest.mark.parametrize("latched", [InstanceStatus.STOPPED, InstanceStatus.CRASHED])
def test_restart_hosted_stops_then_resumes(write_config, projects_root, monkeypatch, latched):
    # CRASHED is what a stop that had to escalate to SIGKILL latches; it must resume too.
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    inst = _seed(manager)

    async def _stop(hosted_id: str, *, connect):
        manager.stopped.append(hosted_id)
        inst.status = latched
        return inst

    monkeypatch.setattr(manager, "stop", _stop)
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 200, r.text
    assert manager.stopped == [_HID]
    assert manager.resumed[0]["id"] == _HID
    assert manager.resumed[0]["binary"] == "/usr/bin/claude"
    assert r.json()["claustrum_process_id"] == "hid-2"
    assert r.json()["created"] is True


@pytest.mark.parametrize("status", [InstanceStatus.STARTING, InstanceStatus.ERROR, None])
def test_restart_hosted_without_a_running_session_is_409(
    write_config, projects_root, monkeypatch, status
):
    # TASK-539 (review of PR 1848): a row with no running session (an orphan, an ERROR row,
    # or a row a restore pass is still reattaching) was stopped through kill_orphan, which
    # marks it stopped although its agent may still run. Restart now refuses; Resume handles it.
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    inst = _seed(manager)
    if status is None:
        manager.sessions.pop(_HID)  # no session at all: an orphan, or a row being reattached
        inst.status = InstanceStatus.STARTING
    else:
        manager.sessions[_HID].status = status
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 409, r.text
    assert "use Resume" in r.json()["detail"]
    assert manager.killed_orphans == [] and manager.stopped == [] and manager.resumed == []


def test_restart_hosted_failing_resume_names_the_stop(write_config, projects_root, monkeypatch):
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    _seed(manager)
    manager.resume_error = HostedSessionError("hosted session is still running")
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

    async def _gone(hosted_id: str, *, connect):
        manager.instances.pop(hosted_id, None)  # the row itself is gone (a concurrent forget)
        raise HostedSessionError(f"no such hosted session: {hosted_id}")

    monkeypatch.setattr(manager, "stop", _gone)
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 404
    assert manager.resumed == []


def test_restart_hosted_running_after_an_accepted_kill_is_still_502(
    write_config, projects_root, monkeypatch
):
    # The stop's SIGKILL was accepted but no exit frame came: the session still reads
    # running with its agent ended. It did not fail, so the "session failed" 409 is not
    # the answer; the existing 502 (the stop did not complete) stays.
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    inst = _seed(manager)

    async def _no_exit(hosted_id: str, *, connect):
        manager.sessions[hosted_id].agent_ended = True
        return inst  # still RUNNING

    monkeypatch.setattr(manager, "stop", _no_exit)
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 502, r.text
    assert manager.resumed == []


def test_restart_hosted_stop_refused_because_the_agent_may_run_is_409(
    write_config, projects_root, monkeypatch
):
    # TASK-539: the session failed during the prerequisite awaits, so the stop's kill-first
    # path ran and the daemon could not end the agent. That is a refusal, not a 404.
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    _seed(manager)
    daemon = _StubDaemon()
    seen: list = []

    async def _refused(hosted_id: str, *, connect):
        seen.append(connect)
        raise HostedAgentMayBeRunning("cannot stop: the agent may still be running")

    monkeypatch.setattr(manager, "stop", _refused)
    app = _app(write_config, manager=manager, daemon=daemon)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 409
    assert "may still be running" in r.json()["detail"]
    assert seen == [daemon.ensure]  # a fresh connection, not the cached client
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

        async def _denied(_iid: str, **_kw):
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

        async def _boom(_iid: str, **_kw):
            raise SpawnError("nope")

        monkeypatch.setattr(runner, "resume_detailed", _boom)
        assert client.post(f"/api/instances/{iid}/restart").status_code == 409
    assert notified and notified[0][0] == "reconnect-failed"


def test_restart_bridge_new_process_crashing_at_start_is_an_error(runner_config, monkeypatch):
    # The likeliest failure after an update: the new claude release dies at start. The
    # resume does not raise for that; it hands back an ERROR row. The restart must not
    # report it as a success.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    app, runner = _bridge_app(runner_config)
    notified: list[tuple] = []
    monkeypatch.setattr(runner, "notify_app_event", lambda *a: notified.append(a))
    with TestClient(app) as client:
        iid = client.post("/api/instances", json={"project": "alpha"}).json()["instance_id"]
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
        r = client.post(f"/api/instances/{iid}/restart")
        assert r.status_code == 409, r.text
        assert "stopped, but it did not start again" in r.json()["detail"]
        assert client.get(f"/api/instances/{iid}").json()["status"] == "error"
    assert notified and notified[0][0] == "reconnect-failed"


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
        deadline = time.monotonic() + 10  # fail, not hang, if the slot is never taken
        while not release.is_set() and time.monotonic() < deadline:
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

    async def _stuck(hosted_id: str, *, connect):
        manager.stopped.append(hosted_id)
        return inst  # still RUNNING: the kill's last wait timed out

    monkeypatch.setattr(manager, "stop", _stuck)
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 502
    assert "may still be running" in r.json()["detail"]
    assert manager.resumed == []


@pytest.mark.parametrize("left", [InstanceStatus.STARTING, InstanceStatus.ERROR])
def test_restart_hosted_stop_without_a_latched_exit_is_502(
    write_config, projects_root, monkeypatch, left
):
    # ERROR: the daemon link dropped during the signal, so the agent may still run. A resume
    # then would put a second agent on the same conversation.
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    inst = _seed(manager)

    async def _not_exited(hosted_id: str, *, connect):
        manager.stopped.append(hosted_id)
        inst.status = left
        return inst

    monkeypatch.setattr(manager, "stop", _not_exited)
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 502
    assert manager.resumed == []


def test_restart_hosted_slot_is_keyed_by_process_id(write_config, projects_root, monkeypatch):
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    inst = _seed(manager)
    # The real manager resolves the instance_id alias to the same row; the stub does not,
    # so register the row under it too. A slot keyed by the path id would miss the alias.
    manager.instances[inst.instance_id] = inst
    manager.sessions[inst.instance_id] = manager.sessions[_HID]
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    app.state.restarts_in_flight.add(_HID)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{inst.instance_id}/restart")
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
    assert "} else if (body && body.instance_id) {" in page
    # The error is on the row, in Active and in Recent.
    assert 'data-test="restart-error"' in page
    assert 'data-test="restart-error-recent"' in page
    assert "this.restartError[key] = null;  // a deliberate stop" in page
    assert (
        "x-show=\"!!restartError[i.rk] && !(displayStatus(i.rk) === 'error' && detailOf(i.rk))\""
        in page
    )
    assert 'x-data="{ recentOpen' not in page  # a local scope would shadow the root flag
    assert "recentOpen: false," in page  # on the root component, so restart() can open it


def test_dashboard_hosted_restart_button_and_guards(write_config, monkeypatch):
    page = _dashboard(write_config, monkeypatch)
    assert 'data-test="hosted-restart"' in page
    # Same uuid + project gate as Resume, so Restart is offered only where Resume can follow,
    # and only on a running session: from PR 1858 the server refuses a starting row with 409.
    assert "h.status === 'running' && h.claude_session_uuid_usable && h.project" in page
    assert "['running', 'starting'].includes(h.status) && h.claude_session_uuid_usable" not in page
    assert "if (this.hostedRestarting[id]) return;" in page
    assert "finally { this.hostedRestarting[id] = false; }" in page
    assert "|| !!this.hostedRestarting[h.claustrum_process_id]);" in page
    assert "&& !this.hostedRestarting[h.claustrum_process_id]);" in page
    assert 'data-test="hosted-restart-error"' in page
    assert 'const errId = res.headers.get("X-Clauster-Replacement-Id") || id;' in page
    assert "this.hostedRestartError[errId] = detail ||" in page
    assert "this.hostedRestartError[id] = null;  // a deliberate stop" in page
    assert "this.hostedRestartError[id] = null;  // a kill makes" in page
    # The button stays (disabled, "Restarting…") when a poll sees the row stopped mid-restart.
    assert '.project) || !!hostedRestarting[h.claustrum_process_id]"' in page
    # A failure after the stop opens Recent, where the stopped row and its error now are.
    assert (
        page.count('.includes("stopped, but it did not start again")) this.recentOpen = true;')
        == 2
    )


@pytest.mark.parametrize(
    ("ended", "error_detail", "expect"),
    [
        (InstanceStatus.CRASHED, None, "failed at start (crashed)"),
        (InstanceStatus.STOPPED, None, "failed at start (stopped)"),
        (InstanceStatus.ERROR, None, "failed at start (error)"),
        (InstanceStatus.ERROR, "daemon link lost", "daemon link lost"),
    ],
)
def test_restart_hosted_new_process_exiting_at_start_is_an_error(
    write_config, projects_root, monkeypatch, ended, error_detail, expect
):
    # The daemon answers the spawn before the child can fail; the exit lands a moment later.
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    _seed(manager)
    real_resume = manager.resume

    async def _resume_then_crash(*a, **k):
        fresh = await real_resume(*a, **k)

        async def _crash() -> None:
            await asyncio.sleep(0.01)
            fresh.status = ended
            fresh.error_detail = error_detail

        asyncio.get_running_loop().create_task(_crash())
        return fresh

    monkeypatch.setattr(manager, "resume", _resume_then_crash)
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    notified: list[tuple] = []
    monkeypatch.setattr(app.state.runner, "notify_app_event", lambda *a: notified.append(a))
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "stopped, but it did not start again" in detail
    assert expect in detail
    assert r.headers["X-Clauster-Replacement-Id"] == "hid-2"  # the card that still exists
    assert notified and notified[0][0] == "reconnect-failed"


def test_restart_hosted_failing_resume_notifies(write_config, projects_root, monkeypatch):
    _hosted_ready(monkeypatch)
    manager = _StubManager()
    _seed(manager)
    manager.resume_error = ClaustrumError("spawn failed")
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    notified: list[tuple] = []
    monkeypatch.setattr(app.state.runner, "notify_app_event", lambda *a: notified.append(a))
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/restart")
    assert r.status_code == 502
    assert notified and notified[0][0] == "reconnect-failed"
