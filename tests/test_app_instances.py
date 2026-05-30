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
