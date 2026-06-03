from __future__ import annotations

from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import load_config


def _client(write_config) -> TestClient:
    config = load_config(write_config())
    return TestClient(create_app(config))


def test_healthz(write_config):
    client = _client(write_config)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "instances_running" in body


def test_api_projects(write_config):
    client = _client(write_config)
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert names == ["alpha", "beta", "gamma"]


def test_dashboard_renders(write_config):
    client = _client(write_config)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Clauster" in resp.text
    assert "alpha" in resp.text


def test_dashboard_pty_bridge_is_resumable(write_config):
    # Regression (true-resume reachability): a stopped pty bridge has no
    # environment_id (the flag form leaves no env ghost), so isResumable() must
    # also accept resume_mode === "pty". Otherwise the Restart button — the only
    # path to POST /resume -> spawn(resume=True) -> `claude --continue` — never
    # renders, and pty true-resume is unreachable from the UI (only "Start bridge"
    # shows, which is a fresh session with no --continue).
    resp = _client(write_config).get("/")
    assert resp.status_code == 200
    assert 'i.resume_mode === "pty"' in resp.text
