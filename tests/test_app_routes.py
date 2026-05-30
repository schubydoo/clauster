"""Route validation / error branches in app.py (auth off — no CSRF ceremony).

Covers the input-guard and not-found paths the feature tests don't hit:
empty/missing body fields, unknown-instance stop, unknown-project trust, and the
claude-md resolver / read-error branches.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import load_config

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _client(write_config, tmp_path) -> TestClient:
    return TestClient(create_app(load_config(
        write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n"))))


# ----- create / clone body validation ----------------------------------

def test_create_missing_name_422(write_config, tmp_path):
    assert _client(write_config, tmp_path).post("/api/projects", json={}).status_code == 422
    assert _client(write_config, tmp_path).post("/api/projects", json={"name": ""}).status_code == 422


def test_clone_missing_name_422(write_config, tmp_path):
    r = _client(write_config, tmp_path).post("/api/projects/clone", json={"url": "https://x/r.git"})
    assert r.status_code == 422


def test_clone_missing_url_422(write_config, tmp_path):
    r = _client(write_config, tmp_path).post("/api/projects/clone", json={"name": "x"})
    assert r.status_code == 422


# ----- instance stop / trust not-found ----------------------------------

def test_stop_unknown_instance_404(write_config, tmp_path):
    assert _client(write_config, tmp_path).delete("/api/instances/ghost").status_code == 404


def test_trust_unknown_project_404(write_config, tmp_path):
    # valid name, but not a discovered project -> UnknownProject -> 404
    assert _client(write_config, tmp_path).post("/api/projects/ghostproj/trust").status_code == 404


# ----- claude-md resolver + read-error branches -------------------------

def test_claude_md_unknown_project_404(write_config, tmp_path):
    assert _client(write_config, tmp_path).get("/api/projects/ghostproj/claude-md").status_code == 404


def test_claude_md_put_base_sha_not_string_422(write_config, tmp_path):
    r = _client(write_config, tmp_path).put(
        "/api/projects/alpha/claude-md", json={"content": "x", "base_sha256": 123})
    assert r.status_code == 422


def test_claude_md_get_non_utf8_is_422(write_config, projects_root, tmp_path):
    # A non-UTF8 CLAUDE.md in a real project -> ClaudeMdError -> 422.
    (projects_root / "gamma" / "CLAUDE.md").write_bytes(b"\xff\xfe not utf8")
    client = _client(write_config, tmp_path)
    assert client.get("/api/projects/gamma/claude-md").status_code == 422


def test_claude_md_invalid_name_404(write_config, tmp_path):
    # A dotted name fails the project-name regex -> resolver's first 404.
    assert _client(write_config, tmp_path).get("/api/projects/ghost.proj/claude-md").status_code == 404


def test_claude_md_put_path_escape_422(write_config, projects_root, tmp_path):
    # CLAUDE.md is a symlink out of the project -> ClaudeMdPathError (a ClaudeMdError) -> 422.
    os.symlink("/etc/passwd", projects_root / "gamma" / "CLAUDE.md")
    client = _client(write_config, tmp_path)
    assert client.put("/api/projects/gamma/claude-md", json={"content": "x"}).status_code == 422


# ----- spawn + clone route error branches -------------------------------

def test_spawn_untrusted_dir_409(write_config, tmp_path):
    # projects_root (a tmp dir) isn't trusted in ~/.claude.json -> NotTrusted -> 409.
    r = _client(write_config, tmp_path).post("/api/instances", json={"project": "alpha"})
    assert r.status_code == 409


def test_clone_route_invalid_name_422(write_config, tmp_path):
    r = _client(write_config, tmp_path).post(
        "/api/projects/clone", json={"name": "../evil", "url": "https://10.0.0.1/r.git"})
    assert r.status_code == 422


def test_clone_route_existing_target_409(write_config, tmp_path):
    # 'alpha' already exists under projects_root -> TargetExists (before URL checks).
    r = _client(write_config, tmp_path).post(
        "/api/projects/clone", json={"name": "alpha", "url": "https://10.0.0.1/r.git"})
    assert r.status_code == 409


# ----- per-project usage (cost badge) -----------------------------------

def test_project_usage_invalid_name_422(write_config, tmp_path):
    # Dotted name fails the project-name regex -> 422 (don't even scan transcripts).
    assert _client(write_config, tmp_path).get("/api/projects/bad.name/usage").status_code == 422


def test_project_usage_empty_project_zero(write_config, tmp_path):
    # A real project with no transcripts under ~/.claude -> well-formed zero rollup.
    r = _client(write_config, tmp_path).get("/api/projects/gamma/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["project"] == "gamma"
    assert body["transcripts"] == 0
    assert body["total_tokens"] == 0
    assert body["cost_usd"] == 0.0
    assert body["by_model"] == {}
    assert body["approximate"] is True


def test_project_usage_serializes_rollup(write_config, tmp_path, monkeypatch):
    # Aggregation itself is unit-tested; here we pin the route's serialization
    # (field names, $ rounding, per-model breakdown) against a known rollup.
    from clauster import usage as usage_mod

    pu = usage_mod.ProjectUsage(project="alpha", transcript_count=2)
    pu.by_model["claude-opus-4-8"] = usage_mod.TokenTotals(input=1_000_000, messages=3)
    monkeypatch.setattr(usage_mod, "aggregate_project_usage", lambda *a, **k: pu)

    body = _client(write_config, tmp_path).get("/api/projects/alpha/usage").json()
    assert body["transcripts"] == 2
    assert body["messages"] == 3
    assert body["total_tokens"] == 1_000_000
    assert body["cost_usd"] == 15.0  # 1 Mtok opus input @ $15/Mtok
    assert body["by_model"]["claude-opus-4-8"]["cost_usd"] == 15.0
    assert body["unpriced_models"] == []
