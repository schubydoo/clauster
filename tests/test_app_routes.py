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
    return TestClient(
        create_app(
            load_config(
                write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n")
            )
        )
    )


# ----- create / clone body validation ----------------------------------


def test_create_missing_name_422(write_config, tmp_path):
    assert _client(write_config, tmp_path).post("/api/projects", json={}).status_code == 422
    assert (
        _client(write_config, tmp_path).post("/api/projects", json={"name": ""}).status_code == 422
    )


def test_clone_missing_name_422(write_config, tmp_path):
    r = _client(write_config, tmp_path).post(
        "/api/projects/clone", json={"url": "https://x/r.git"}
    )
    assert r.status_code == 422


def test_clone_missing_url_422(write_config, tmp_path):
    r = _client(write_config, tmp_path).post("/api/projects/clone", json={"name": "x"})
    assert r.status_code == 422


# ----- instance stop / trust not-found ----------------------------------


def test_stop_unknown_instance_404(write_config, tmp_path):
    assert _client(write_config, tmp_path).delete("/api/instances/ghost").status_code == 404


def test_resume_unknown_instance_404(write_config, tmp_path):
    assert _client(write_config, tmp_path).post("/api/instances/ghost/resume").status_code == 404


def test_trust_unknown_project_404(write_config, tmp_path):
    # valid name, but not a discovered project -> UnknownProject -> 404
    assert _client(write_config, tmp_path).post("/api/projects/ghostproj/trust").status_code == 404


# ----- claude-md resolver + read-error branches -------------------------


def test_claude_md_unknown_project_404(write_config, tmp_path):
    assert (
        _client(write_config, tmp_path).get("/api/projects/ghostproj/claude-md").status_code == 404
    )


def test_claude_md_put_base_sha_not_string_422(write_config, tmp_path):
    r = _client(write_config, tmp_path).put(
        "/api/projects/alpha/claude-md", json={"content": "x", "base_sha256": 123}
    )
    assert r.status_code == 422


def test_claude_md_get_non_utf8_is_422(write_config, projects_root, tmp_path):
    # A non-UTF8 CLAUDE.md in a real project -> ClaudeMdError -> 422.
    (projects_root / "gamma" / "CLAUDE.md").write_bytes(b"\xff\xfe not utf8")
    client = _client(write_config, tmp_path)
    assert client.get("/api/projects/gamma/claude-md").status_code == 422


def test_claude_md_invalid_name_404(write_config, tmp_path):
    # A dotted name fails the project-name regex -> resolver's first 404.
    assert (
        _client(write_config, tmp_path).get("/api/projects/ghost.proj/claude-md").status_code
        == 404
    )


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
        "/api/projects/clone", json={"name": "../evil", "url": "https://10.0.0.1/r.git"}
    )
    assert r.status_code == 422


def test_clone_route_existing_target_409(write_config, tmp_path):
    # 'alpha' already exists under projects_root -> TargetExists (before URL checks).
    r = _client(write_config, tmp_path).post(
        "/api/projects/clone", json={"name": "alpha", "url": "https://10.0.0.1/r.git"}
    )
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


# ----- ghost-environment reaper (opt-in dashboard surface) --------------


def _reaper_client(write_config, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            load_config(
                write_config(
                    f"claude:\n  binary: {FAKE_CLAUDE}\n"
                    f"state_dir: {tmp_path}/.s\nreaper:\n  ui_enabled: true\n"
                )
            )
        )
    )


def _env(id, type, directory=None, name=""):
    from clauster import environments as envmod

    return envmod.Environment(
        id=id,
        name=name,
        config=envmod.EnvironmentConfig(type=type, directory=directory),
    )


def _setup_reaper(monkeypatch, envs, live, sink):
    """Patch the environments module so nothing hits the network; record API calls."""
    from clauster import environments as envmod

    monkeypatch.setattr(
        envmod,
        "load_credentials",
        lambda **k: envmod.Credentials(access_token="tkn", organization_uuid="org"),
    )
    monkeypatch.setattr(envmod, "live_bridge_directories", lambda *a, **k: set(live))

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def list_environments(self, **k):
            return list(envs)

        def archive_environment(self, env_id):
            sink.append(("archive", env_id))

        def delete_environment(self, env_id, *, force=False):
            sink.append(("delete", env_id, force))

    monkeypatch.setattr(envmod, "EnvironmentsClient", FakeClient)


# a live bridge (kept), two ghosts, and the cloud Default (never reaped)
_ENVS = [
    ("env_live", "bridge", "/live/dir", "live"),
    ("env_ghost1", "bridge", "/ghost/one", "ghost-one"),
    ("env_ghost2", "bridge", "/ghost/two", "ghost-two"),
    ("env_cloud", "cloud", None, "Default"),
]


def _make_envs():
    return [_env(*e) for e in _ENVS]


def test_reaper_disabled_by_default_404(write_config, tmp_path):
    c = _client(write_config, tmp_path)  # no reaper config -> ui_enabled false
    assert c.get("/api/environments/ghosts").status_code == 404
    assert (
        c.post(
            "/api/environments/reap",
            json={"action": "archive", "ids": ["x"], "confirm": "archive"},
        ).status_code
        == 404
    )


def test_reaper_preview_lists_only_ghosts(write_config, tmp_path, monkeypatch):
    _setup_reaper(monkeypatch, _make_envs(), {"/live/dir"}, [])
    body = _reaper_client(write_config, tmp_path).get("/api/environments/ghosts").json()
    assert body["enabled"] is True
    assert body["total"] == 4 and body["live_dirs"] == 1
    ids = {g["id"] for g in body["ghosts"]}
    assert ids == {"env_ghost1", "env_ghost2"}  # cloud + live excluded


def test_reaper_archive_acts_only_on_ghosts(write_config, tmp_path, monkeypatch):
    sink = []
    _setup_reaper(monkeypatch, _make_envs(), {"/live/dir"}, sink)
    # Client asks to archive a ghost AND the live + cloud env (stale/hostile input).
    r = _reaper_client(write_config, tmp_path).post(
        "/api/environments/reap",
        json={
            "action": "archive",
            "confirm": "archive",
            "ids": ["env_ghost1", "env_live", "env_cloud"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["reaped"] == ["env_ghost1"]
    assert set(body["skipped"]) == {"env_live", "env_cloud"}  # never acted on
    # The API client only ever saw the ghost — live/cloud were never archived.
    assert sink == [("archive", "env_ghost1")]


def test_reaper_delete_requires_typed_confirm(write_config, tmp_path, monkeypatch):
    sink = []
    _setup_reaper(monkeypatch, _make_envs(), {"/live/dir"}, sink)
    c = _reaper_client(write_config, tmp_path)
    # Wrong confirm token for delete -> 400, nothing touched.
    bad = c.post(
        "/api/environments/reap",
        json={"action": "delete", "ids": ["env_ghost1"], "confirm": "archive"},
    )
    assert bad.status_code == 400
    assert sink == []
    # Correct token -> force-delete proceeds.
    ok = c.post(
        "/api/environments/reap",
        json={"action": "delete", "ids": ["env_ghost1"], "confirm": "DELETE"},
    )
    assert ok.status_code == 200
    assert ok.json()["reaped"] == ["env_ghost1"]
    assert sink == [("delete", "env_ghost1", True)]  # force=True


def test_reaper_validation_errors(write_config, tmp_path, monkeypatch):
    _setup_reaper(monkeypatch, _make_envs(), {"/live/dir"}, [])
    c = _reaper_client(write_config, tmp_path)
    assert (
        c.post("/api/environments/reap", json={"action": "nope", "ids": ["x"]}).status_code == 422
    )
    assert (
        c.post("/api/environments/reap", json={"action": "archive", "ids": []}).status_code == 422
    )
    assert (
        c.post("/api/environments/reap", json={"action": "archive", "ids": [1, 2]}).status_code
        == 422
    )


def test_reaper_credentials_error_503(write_config, tmp_path, monkeypatch):
    from clauster import environments as envmod

    def _raise(**k):
        raise envmod.CredentialsError("no token")

    monkeypatch.setattr(envmod, "load_credentials", _raise)
    r = _reaper_client(write_config, tmp_path).get("/api/environments/ghosts")
    assert r.status_code == 503
    assert "credentials" in r.json()["detail"]


def test_reaper_list_api_error_502(write_config, tmp_path, monkeypatch):
    from clauster import environments as envmod

    monkeypatch.setattr(
        envmod,
        "load_credentials",
        lambda **k: envmod.Credentials(access_token="t", organization_uuid="o"),
    )
    monkeypatch.setattr(envmod, "live_bridge_directories", lambda *a, **k: set())

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def list_environments(self, **k):
            raise envmod.EnvironmentsAPIError(500, "upstream boom")

    monkeypatch.setattr(envmod, "EnvironmentsClient", FakeClient)
    r = _reaper_client(write_config, tmp_path).get("/api/environments/ghosts")
    assert r.status_code == 502
    assert "environments API error" in r.json()["detail"]


def test_reaper_per_env_error_is_reported_not_fatal(write_config, tmp_path, monkeypatch):
    from clauster import environments as envmod

    monkeypatch.setattr(
        envmod,
        "load_credentials",
        lambda **k: envmod.Credentials(access_token="t", organization_uuid="o"),
    )
    monkeypatch.setattr(envmod, "live_bridge_directories", lambda *a, **k: {"/live/dir"})

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def list_environments(self, **k):
            return _make_envs()

        def archive_environment(self, env_id):
            if env_id == "env_ghost1":
                raise envmod.EnvironmentsAPIError(409, "work queued")
            # env_ghost2 archives fine

    monkeypatch.setattr(envmod, "EnvironmentsClient", FakeClient)
    body = (
        _reaper_client(write_config, tmp_path)
        .post(
            "/api/environments/reap",
            json={
                "action": "archive",
                "confirm": "archive",
                "ids": ["env_ghost1", "env_ghost2"],
            },
        )
        .json()
    )
    assert body["reaped"] == ["env_ghost2"]  # the healthy one still went through
    assert "env_ghost1" in body["errors"]  # the failure is surfaced, not swallowed
    assert "409" in body["errors"]["env_ghost1"]


def test_reaper_fails_closed_on_live_set_failure(write_config, tmp_path, monkeypatch):
    from clauster import environments as envmod

    monkeypatch.setattr(
        envmod,
        "load_credentials",
        lambda **k: envmod.Credentials(access_token="t", organization_uuid="o"),
    )

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def list_environments(self, **k):
            return _make_envs()

    monkeypatch.setattr(envmod, "EnvironmentsClient", FakeClient)

    def _boom(*a, **k):
        raise RuntimeError("agents --json failed")

    monkeypatch.setattr(envmod, "live_bridge_directories", _boom)
    r = _reaper_client(write_config, tmp_path).get("/api/environments/ghosts")
    assert r.status_code == 503
    assert "could not determine live bridges" in r.json()["detail"]


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
