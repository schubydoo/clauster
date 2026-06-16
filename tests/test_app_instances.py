from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.runner import SessionRunner


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


def test_spawn_and_stop_via_api(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": "alpha"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "running"
        assert body["environment_id"] == "env_01TESTENVAAAAAAAAAAAAAAAA"

        health = client.get("/healthz").json()
        assert health["instances_running"] == 1

        stop = client.delete("/api/instances/alpha")
        assert stop.status_code == 200
        assert stop.json()["status"] == "stopped"


def test_forget_stopped_bridge_via_api(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        client.post("/api/instances", json={"project": "alpha"})
        client.delete("/api/instances/alpha")  # stop -> a stopped, resumable card
        assert any(i["project"] == "alpha" for i in client.get("/api/instances").json())

        forget = client.post("/api/instances/alpha/forget")
        assert forget.status_code == 200, forget.text
        assert forget.json() == {"id": "alpha", "forgotten": True}
        assert client.get("/api/instances").json() == []  # gone from the list


def test_forget_running_bridge_409(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        client.post("/api/instances", json={"project": "alpha"})  # running
        forget = client.post("/api/instances/alpha/forget")
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
        second = client.post("/api/instances", json={"project": "beta"})  # 1 live >= cap
        assert second.status_code == 409, second.text
        assert "max_bridges" in second.json()["detail"]
        client.delete("/api/instances/alpha")
        client.delete("/api/instances/alpha")  # cleanup the fake process


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
