from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import SessionRunner


class _FakePtr:
    """Stand-in for a live Anthropic bridge-pointer.json, for adoption tests (#330)."""

    pid, proc_start, environment_id, session_id = 4242, "1000", "env_x", "session_x"


def _client(runner_config) -> TestClient:
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    return TestClient(create_app(config, runner=runner))


def test_instances_empty_initially(runner_config):
    with _client(runner_config) as client:
        resp = client.get("/api/instances")
        assert resp.status_code == 200
        assert resp.json() == []


def test_sessions_empty_initially(runner_config):
    with _client(runner_config) as client:
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == {}


def test_tracked_sessions_empty_initially(runner_config):
    with _client(runner_config) as client:
        resp = client.get("/api/sessions/tracked")
        assert resp.status_code == 200
        assert resp.json() == {}


def test_tracked_sessions_endpoint_groups_by_instance(runner_config, monkeypatch):
    """/api/sessions/tracked returns each bridge's live sessions, keyed by instance (#570)."""
    from pathlib import Path

    from clauster.models import Attribution, WorkingSession

    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    runner._sessions = [
        WorkingSession(
            pid=11,
            cwd=Path("/tmp/a"),
            kind="interactive",
            started_at=100,
            local_uuid="11111111-aaaa",
            parent_instance="alpha",
            attribution=Attribution.TRACKED,
        ),
        WorkingSession(
            pid=12,
            cwd=Path("/tmp/a"),
            kind="interactive",
            started_at=200,
            local_uuid="22222222-bbbb",
            parent_instance="alpha",
            attribution=Attribution.TRACKED,
        ),
    ]

    # The lifespan starts a background poll loop whose first poll_once() reconciles against
    # `agents --json` and would overwrite this injected snapshot with [] — a race the endpoint
    # read can lose (intermittent macOS/3.11 failures). Stub it so the injected sessions are
    # stable; this test exercises the grouping endpoint, not the poll/reconcile path.
    async def _no_poll() -> None:
        return None

    monkeypatch.setattr(runner, "poll_once", _no_poll)
    with TestClient(create_app(config, runner=runner)) as client:
        resp = client.get("/api/sessions/tracked")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"alpha"}
        assert [s["local_uuid"] for s in body["alpha"]] == ["11111111-aaaa", "22222222-bbbb"]


def test_spawn_and_stop_via_api(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": "alpha"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "running"
        assert body["environment_id"] == "env_01TESTENVAAAAAAAAAAAAAAAA"
        instance_id = body["instance_id"]

        health = client.get("/healthz").json()
        assert health["instances_running"] == 1

        stop = client.delete(f"/api/instances/{instance_id}")
        assert stop.status_code == 200
        assert stop.json()["status"] == "stopped"


def test_forget_stopped_bridge_via_api(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        spawn = client.post("/api/instances", json={"project": "alpha"})
        instance_id = spawn.json()["instance_id"]
        client.delete(f"/api/instances/{instance_id}")  # stop -> a stopped, resumable card
        assert any(i["project"] == "alpha" for i in client.get("/api/instances").json())

        forget = client.post(f"/api/instances/{instance_id}/forget")
        assert forget.status_code == 200, forget.text
        assert forget.json() == {"id": instance_id, "forgotten": True}
        assert client.get("/api/instances").json() == []  # gone from the list


def test_forget_running_bridge_409(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        spawn = client.post("/api/instances", json={"project": "alpha"})  # running
        instance_id = spawn.json()["instance_id"]
        forget = client.post(f"/api/instances/{instance_id}/forget")
        assert forget.status_code == 409  # Stop it first; forget never kills
        assert any(i["project"] == "alpha" for i in client.get("/api/instances").json())


def test_max_bridges_cap_returns_409(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    config.instance_defaults.max_bridges = 1
    runner = SessionRunner(config, claude_json=claude_json)
    with TestClient(create_app(config, runner=runner)) as client:
        first = client.post("/api/instances", json={"project": "alpha"})
        assert first.status_code == 201, first.text
        alpha_id = first.json()["instance_id"]
        second = client.post("/api/instances", json={"project": "beta"})  # 1 live >= cap
        assert second.status_code == 409, second.text
        assert "max_bridges" in second.json()["detail"]
        client.delete(f"/api/instances/{alpha_id}")
        client.delete(f"/api/instances/{alpha_id}")  # cleanup the fake process


def test_forget_unknown_instance_404(runner_config):
    with _client(runner_config) as client:
        assert client.post("/api/instances/ghost/forget").status_code == 404


def test_spawn_unknown_project_404(runner_config):
    with _client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": "ghost"})
        assert resp.status_code == 404


@pytest.mark.parametrize("evil", ["../etc", "a/b", ".."])
def test_spawn_path_traversal_rejected_404(runner_config, evil):
    with _client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": evil})
        assert resp.status_code == 404


def test_spawn_missing_body_422(runner_config):
    with _client(runner_config) as client:
        resp = client.post("/api/instances", json={})
        assert resp.status_code == 422


def test_spawn_non_string_resume_mode_422(runner_config):
    with _client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": "alpha", "resume_mode": 1})
        assert resp.status_code == 422


def test_spawn_invalid_resume_mode_422(runner_config):
    # An unknown resume_mode is rejected by _validate_spawn_options -> 422,
    # not silently ignored or 500.
    with _client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": "alpha", "resume_mode": "bogus"})
        assert resp.status_code == 422


def test_spawn_accepts_resume_mode(runner_config, monkeypatch):
    # The per-launch picker: an explicit resume_mode is recorded on the instance.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": "alpha", "resume_mode": "standard"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["resume_mode"] == "standard"
        client.delete("/api/instances/alpha")


def test_get_unknown_instance_404(runner_config):
    with _client(runner_config) as client:
        assert client.get("/api/instances/nope").status_code == 404


def test_trust_endpoint_flips_state(runner_config, tmp_path):
    # Use an untrusted claude.json so the trust route has visible effect.
    config, _ = runner_config
    untrusted = tmp_path / "untrusted.json"
    untrusted.write_text("{}")
    runner = SessionRunner(config, claude_json=untrusted)
    with TestClient(create_app(config, runner=runner)) as client:
        resp = client.post("/api/projects/alpha/trust")
        assert resp.status_code == 200
        assert resp.json()["trust_state"] == "trusted"


def test_trust_endpoint_read_failure_returns_500(runner_config, monkeypatch):
    # If ~/.claude.json exists but can't be read/written (e.g. permissions), the
    # trust route surfaces a 500 rather than silently dropping other settings.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)

    def boom(*args, **kwargs):
        raise PermissionError("simulated: cannot read claude.json")

    monkeypatch.setattr("clauster.runner.trust_directory", boom)
    with TestClient(create_app(config, runner=runner)) as client:
        resp = client.post("/api/projects/alpha/trust")
        assert resp.status_code == 500
        assert "could not update trust state" in resp.json()["detail"]


# -- external-session adoption endpoints (FE-4b, #330) -----------------------


def test_adoptable_endpoint_returns_sorted(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr(runner, "adoptable_external_projects", lambda: {"gamma", "alpha"})
    with TestClient(create_app(config, runner=runner)) as client:
        resp = client.get("/api/sessions/adoptable")
        assert resp.status_code == 200
        assert resp.json() == ["alpha", "gamma"]  # sorted for a stable UI order


def test_adopt_endpoint_success(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: _FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: True)
    with TestClient(create_app(config, runner=runner)) as client:
        resp = client.post("/api/projects/alpha/adopt")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "running"
        assert body["bridge_pid"] == 4242
        assert body["resume_mode"] == "standard"


def test_adopt_endpoint_unavailable_returns_409(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: _FakePtr())
    # Gate fails (pty/flag-form bridge, or stale pointer) -> 409, not a partial adopt.
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: False)
    with TestClient(create_app(config, runner=runner)) as client:
        resp = client.post("/api/projects/alpha/adopt")
        assert resp.status_code == 409


def test_adopt_endpoint_already_managed_returns_409(runner_config):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    runner._instances["alpha"] = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.RUNNING
    )
    with TestClient(create_app(config, runner=runner)) as client:
        resp = client.post("/api/projects/alpha/adopt")
        assert resp.status_code == 409


def test_adopt_endpoint_unknown_project_returns_404(runner_config):
    with _client(runner_config) as client:
        resp = client.post("/api/projects/does-not-exist/adopt")
        assert resp.status_code == 404
