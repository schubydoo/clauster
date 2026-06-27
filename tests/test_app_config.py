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
    # Redaction regression net: no auth / secret / bind / clone / structural key is ever
    # surfaced (the GET only returns the Tier-A allowlist — this guards against a future
    # allowlist edit accidentally widening it).
    forbidden_prefixes = (
        "auth.",
        "reverse_proxy.",
        "clone.",
        "host",
        "port",
        "state_dir",
        "projects_root",
    )
    secret_keys = ("password_hash", "shared_secret", "urls", "binary")
    keys = list(body["fields"])
    assert not any(k.startswith(forbidden_prefixes) for k in keys)
    assert not any(any(s in k for s in secret_keys) for k in keys)


def test_get_config_reflects_saved_value_without_restart(write_config, tmp_path):
    """Reopening the editor after a save shows the saved value, not the startup config.

    A PUT writes the file but does not live-reload the runtime config, so serving
    app.state.config would show the stale, pre-save value until a restart — making a
    successful save look reverted. The GET must reflect what is now on disk.
    """
    client, path = _client_and_path(write_config, tmp_path)
    h = client.get("/api/config").json()["hash"]
    saved = client.put("/api/config", json={"edits": {"usage.fx_rate": 9.0}, "hash": h})
    assert saved.status_code == 200
    # The runtime config is deliberately NOT reloaded (restart_required) ...
    assert saved.json()["restart_required"] is True
    # ... yet a fresh GET reflects the saved value (read from disk), not the stale 1.0.
    assert client.get("/api/config").json()["fields"]["usage.fx_rate"] == 9.0


def test_get_config_falls_back_to_memory_when_disk_unreadable(write_config, tmp_path):
    """A corrupt on-disk config falls back to the in-memory values, not a 500."""
    client, path = _client_and_path(write_config, tmp_path)
    path.write_text("usage: {fx_rate: 1.0\n")  # unterminated flow map -> YAML parse error
    res = client.get("/api/config")
    assert res.status_code == 200
    assert res.json()["fields"]["usage.fx_rate"] == 1.0  # in-memory fallback


def test_get_config_survives_deleted_config_file(write_config, tmp_path):
    """A config file DELETED after startup still opens the editor (no 500).

    The field read degrades via ``editable_values_on_disk`` (catches ``OSError``),
    but ``file_hash`` reads the same path — without its own guard a missing file
    would raise ``FileNotFoundError`` and 500 the editor, the opposite of the
    documented "editor still opens" behaviour. The corrupt-YAML test above keeps the
    file readable, so it does not exercise this branch.
    """
    client, path = _client_and_path(write_config, tmp_path)
    path.unlink()
    res = client.get("/api/config")
    assert res.status_code == 200
    body = res.json()
    assert body["fields"]["usage.fx_rate"] == 1.0  # in-memory fallback
    assert body["hash"] is None  # no file to hash -> save is rejected until it returns


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


def test_put_config_enables_claustrum(write_config, tmp_path):
    # #539: the claustrum hosted channel is now flippable from the editor (it was yml-only).
    client, path = _client_and_path(write_config, tmp_path)
    body = client.get("/api/config").json()
    assert body["fields"]["claustrum.enabled"] is False  # surfaced + off by default
    res = client.put(
        "/api/config", json={"edits": {"claustrum.enabled": True}, "hash": body["hash"]}
    )
    assert res.status_code == 200
    assert (
        res.json()["restart_required"] is True
    )  # every save is restart-required (no live reload)
    assert load_config(path).claustrum.enabled is True


def test_put_config_requires_edits_and_hash_422(write_config, tmp_path):
    client, _ = _client_and_path(write_config, tmp_path)
    h = client.get("/api/config").json()["hash"]
    assert client.put("/api/config", json={"hash": h}).status_code == 422
    assert client.put("/api/config", json={"edits": {"usage.fx_rate": 2.0}}).status_code == 422


def test_config_routes_handle_missing_source_path(tmp_path, projects_root):
    # A config built directly (not via load_config) has no on-disk source path.
    from clauster.config import ClausterConfig

    config = ClausterConfig(
        projects_root=projects_root, state_dir=tmp_path / "s", claude={"binary": str(FAKE_CLAUDE)}
    )
    client = TestClient(create_app(config))
    # GET reports a null hash (nothing on disk to fingerprint) but still serves the values.
    body = client.get("/api/config").json()
    assert body["hash"] is None and body["fields"]
    # PUT has nowhere to write -> 409, not a 500.
    res = client.put("/api/config", json={"edits": {"usage.fx_rate": 2.0}, "hash": "x"})
    assert res.status_code == 409
