"""Route tests for the in-app config editor — GET/PUT /api/config (FE-3, #299)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import load_config

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _client_and_path(write_config, tmp_path) -> tuple[TestClient, Path]:
    path = write_config(
        f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\nusage:\n  fx_rate: 1.0\n"
    )
    return TestClient(create_app(load_config(path))), path


def test_get_config_returns_tier_a_only(write_config, tmp_path):
    client, _ = _client_and_path(write_config, tmp_path)
    res = client.get("/api/config")
    assert res.status_code == 200
    body = res.json()
    assert body["fields"]["usage.fx_rate"] == 1.0
    assert isinstance(body["hash"], str) and body["hash"]
    # No auth/secret/bind field is ever surfaced.
    assert not any(k.startswith(("auth.", "host", "port")) for k in body["fields"])


def test_put_config_applies_tier_a_edit(write_config, tmp_path):
    client, path = _client_and_path(write_config, tmp_path)
    h = client.get("/api/config").json()["hash"]
    res = client.put("/api/config", json={"edits": {"usage.fx_rate": 4.5}, "hash": h})
    assert res.status_code == 200
    assert res.json()["restart_required"] is True
    assert load_config(path).usage.fx_rate == 4.5


def test_put_config_rejects_disallowed_field_400(write_config, tmp_path):
    client, path = _client_and_path(write_config, tmp_path)
    h = client.get("/api/config").json()["hash"]
    res = client.put("/api/config", json={"edits": {"auth.enabled": False}, "hash": h})
    assert res.status_code == 400
    assert load_config(path).auth.enabled is False  # unchanged default


def test_put_config_stale_hash_409(write_config, tmp_path):
    client, _ = _client_and_path(write_config, tmp_path)
    res = client.put("/api/config", json={"edits": {"usage.fx_rate": 2.0}, "hash": "stale"})
    assert res.status_code == 409


def test_put_config_invalid_value_422(write_config, tmp_path):
    client, _ = _client_and_path(write_config, tmp_path)
    h = client.get("/api/config").json()["hash"]
    res = client.put("/api/config", json={"edits": {"instance_defaults.capacity": 0}, "hash": h})
    assert res.status_code == 422


def test_put_config_requires_edits_and_hash_422(write_config, tmp_path):
    client, _ = _client_and_path(write_config, tmp_path)
    h = client.get("/api/config").json()["hash"]
    assert client.put("/api/config", json={"hash": h}).status_code == 422
    assert client.put("/api/config", json={"edits": {"usage.fx_rate": 2.0}}).status_code == 422
