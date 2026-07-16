from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.pointers import sanitize_cwd
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


def test_forget_clears_bridge_pointer(runner_config, monkeypatch):
    # #867 L1: forgetting a stopped bridge deletes its bridge-pointer.json (+ .bak) so the
    # next spawn registers a fresh anchor instead of reattaching a possibly-poisoned one.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        spawn = client.post("/api/instances", json={"project": "alpha"})
        instance_id = spawn.json()["instance_id"]
        client.delete(f"/api/instances/{instance_id}")  # stop -> resumable

        runner = client.app.state.runner
        proj_path = (runner._config.projects_root / "alpha").resolve()
        pdir = runner._claude_projects_dir / sanitize_cwd(proj_path)
        pdir.mkdir(parents=True, exist_ok=True)
        pointer = pdir / "bridge-pointer.json"
        pointer.write_text(
            json.dumps(
                {
                    "sessionId": "session_x",
                    "environmentId": "env_x",
                    "source": "standalone",
                    "pid": 81750,  # long-dead PID -> not live
                    "procStart": "2590192",
                }
            )
        )

        forget = client.post(f"/api/instances/{instance_id}/forget")
        assert forget.status_code == 200, forget.text
        assert not pointer.exists()  # cleared
        assert pointer.with_name(pointer.name + ".bak").exists()  # backed up first


def test_forget_running_bridge_409(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        spawn = client.post("/api/instances", json={"project": "alpha"})  # running
        instance_id = spawn.json()["instance_id"]
        forget = client.post(f"/api/instances/{instance_id}/forget")
        assert forget.status_code == 409  # Stop it first; forget never kills
        assert any(i["project"] == "alpha" for i in client.get("/api/instances").json())


# ----- route identity: the client still sends the PROJECT NAME (#777) -----------
# The registry is keyed by instance_id, but the current dashboard sends `i.project`
# on Stop / Resume / Forget / QR / GET / bridge-log. These drive each route with the
# project-name identity the UI actually uses (NOT the instance_id echo) so a real
# deployment doesn't 404 on every standard-bridge action. Removing resolve_bridge_id
# from any route must fail here even though CI stays green on the instance_id path.


def test_stop_via_api_accepts_project_name(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        client.post("/api/instances", json={"project": "alpha"})
        stop = client.delete("/api/instances/alpha")  # the identity the UI sends
        assert stop.status_code == 200, stop.text
        assert stop.json()["status"] == "stopped"


def test_get_instance_via_api_accepts_project_name(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        spawn = client.post("/api/instances", json={"project": "alpha"})
        got = client.get("/api/instances/alpha")  # project name, not instance_id
        assert got.status_code == 200, got.text
        assert got.json()["instance_id"] == spawn.json()["instance_id"]
        client.delete("/api/instances/alpha")  # cleanup


def test_resume_via_api_accepts_project_name(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        client.post("/api/instances", json={"project": "alpha"})
        client.delete("/api/instances/alpha")  # stop -> a resumable STOPPED card
        resume = client.post("/api/instances/alpha/resume")  # project name
        assert resume.status_code == 200, resume.text
        assert resume.json()["status"] == "running"
        client.delete("/api/instances/alpha")  # cleanup


def test_forget_via_api_accepts_project_name(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        client.post("/api/instances", json={"project": "alpha"})
        client.delete("/api/instances/alpha")  # stop first (forget never kills)
        forget = client.post("/api/instances/alpha/forget")  # project name
        assert forget.status_code == 200, forget.text
        assert forget.json()["forgotten"] is True
        assert client.get("/api/instances").json() == []  # gone from the list


def test_qr_via_api_accepts_project_name(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        client.post("/api/instances", json={"project": "alpha"})
        qr = client.get("/api/instances/alpha/qr")  # project name
        assert qr.status_code == 200, qr.text
        assert qr.headers["content-type"] == "image/svg+xml"
        client.delete("/api/instances/alpha")  # cleanup


def test_stop_via_api_unknown_identity_404(runner_config):
    with _client(runner_config) as client:
        # Neither a known instance_id nor a managed project -> the same 404 as before.
        assert client.delete("/api/instances/ghost").status_code == 404


def test_resume_via_api_unknown_identity_404(runner_config):
    with _client(runner_config) as client:
        # resolve_bridge_id -> None for an unknown identity -> 404 (not a spawn attempt).
        assert client.post("/api/instances/ghost/resume").status_code == 404


def test_qr_via_api_unknown_identity_404(runner_config):
    with _client(runner_config) as client:
        assert client.get("/api/instances/ghost/qr").status_code == 404


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


def test_spawn_non_string_resume_session_id_422(runner_config):
    # Type gate at the API layer, like every sibling optional field (#303).
    with _client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": "alpha", "resume_session_id": 7})
        assert resp.status_code == 422


def test_spawn_malformed_resume_session_id_422(runner_config):
    # Format gate in the runner (InvalidSpawnOption -> 422), before any spawn side
    # effect — the value would otherwise reach a subprocess argv (#303).
    with _client(runner_config) as client:
        resp = client.post(
            "/api/instances",
            json={"project": "alpha", "resume_mode": "pty", "resume_session_id": "not-a-uuid"},
        )
        assert resp.status_code == 422


def test_spawn_resume_session_id_requires_pty_422(runner_config):
    # pty-only: a standard launch with a picked conversation is rejected, never
    # silently ignored (#303).
    with _client(runner_config) as client:
        resp = client.post(
            "/api/instances",
            json={
                "project": "alpha",
                "resume_mode": "standard",
                "resume_session_id": "12345678-1234-1234-1234-123456789abc",
            },
        )
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


# ----- spawn outcome surfaced through the API (#778) -----------------------------


def test_spawn_response_carries_outcome_keys(runner_config, monkeypatch):
    """A real launch answers 201 with created=True and the additive outcome keys (#778)."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": "alpha"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["created"] is True
        assert body["reason"] is None
        assert body["warnings"] == []  # standard spawn: no worktree advisory
        # The instance fields stay at the top level (pre-#778 clients read them there).
        assert body["status"] == "running" and body["project"] == "alpha"
        client.delete(f"/api/instances/{body['instance_id']}")


def test_second_standard_spawn_returns_200_reused(runner_config, monkeypatch):
    """The standard-singleton cap answers 200 + created=False + reason, not a second 201 (#778)."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _client(runner_config) as client:
        first = client.post("/api/instances", json={"project": "alpha"})
        assert first.status_code == 201, first.text
        second = client.post("/api/instances", json={"project": "alpha"})
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["created"] is False
        assert "capped at one per project" in body["reason"]
        assert body["instance_id"] == first.json()["instance_id"]  # the same bridge came back
        # Only one bridge actually runs.
        assert client.get("/healthz").json()["instances_running"] == 1
        client.delete(f"/api/instances/{body['instance_id']}")


def test_spawn_response_passes_runner_warnings_through(runner_config, monkeypatch):
    """warnings[] from the spawn outcome land on the response body verbatim (#778)."""
    from clauster.runner import SpawnOutcome

    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.RUNNING)

    async def _canned(name, **kwargs):
        return SpawnOutcome(instance=inst, created=True, warnings=["no worktree — beware"])

    monkeypatch.setattr(runner, "spawn_detailed", _canned)
    with TestClient(create_app(config, runner=runner)) as client:
        resp = client.post("/api/instances", json={"project": "alpha"})
        assert resp.status_code == 201
        assert resp.json()["warnings"] == ["no worktree — beware"]
