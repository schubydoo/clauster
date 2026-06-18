"""Route validation / error branches in app.py (auth off — no CSRF ceremony).

Covers the input-guard and not-found paths the feature tests don't hit:
empty/missing body fields, unknown-instance stop, unknown-project trust, and the
claude-md resolver / read-error branches.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
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


# ----- system readiness (dashboard preflight panel) ---------------------


def test_doctor_endpoint_shape_and_ok(write_config, tmp_path):
    # Surfaces run_doctor as JSON for the preflight panel. We assert the *contract*
    # (shape + core checks present + valid statuses), NOT the aggregate `ok`: on
    # Windows the shebang fake-claude isn't directly executable, so its `claude`
    # check FAILs and flips `ok` to False. run_doctor's own pass/fail logic is
    # covered in test_ops.py; here the portable, meaningful guarantee is the shape.
    r = _client(write_config, tmp_path).get("/api/doctor")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["ok"], bool)
    names = {c["name"] for c in body["checks"]}
    assert {"config", "claude", "claude-login", "projects_root", "auth"} <= names
    for c in body["checks"]:
        assert set(c) == {"name", "status", "detail"}
        assert c["status"] in {"ok", "warn", "fail"}


# ----- per-project preflight (spawn-readiness for one project) ----------


def test_project_preflight_invalid_name_422(write_config, tmp_path):
    r = _client(write_config, tmp_path).get("/api/projects/bad.name/preflight")
    assert r.status_code == 422


def test_project_preflight_unknown_project_404(write_config, tmp_path):
    r = _client(write_config, tmp_path).get("/api/projects/ghostproj/preflight")
    assert r.status_code == 404


def test_project_preflight_shape_and_checks(write_config, tmp_path):
    # Contract: the per-project checklist carries the project name, an `ok` bool, and
    # trust + git checks in the same {name, status, detail} shape the doctor panel uses.
    r = _client(write_config, tmp_path).get("/api/projects/alpha/preflight")
    assert r.status_code == 200
    body = r.json()
    assert body["project"] == "alpha"
    assert isinstance(body["ok"], bool)
    names = {c["name"] for c in body["checks"]}
    assert {"trust", "git"} <= names
    for c in body["checks"]:
        assert set(c) == {"name", "status", "detail"}
        assert c["status"] in {"ok", "warn", "fail"}


# ----- single-row fragment (reactive insertion, no full reload) ---------


def test_card_renders_known_project(write_config, tmp_path):
    # The fragment route returns the same row markup the grid loop renders.
    r = _client(write_config, tmp_path).get("/api/projects/alpha/row")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert 'data-project="alpha"' in r.text
    assert "Run Claude here" in r.text  # it's a real row, not an empty stub


def test_card_reflects_project_shape(write_config, tmp_path):
    # Per-project Jinja conditionals render: the CLAUDE.md meta indicator (the
    # ic-page icon) only appears where a CLAUDE.md is present; the Git meta
    # indicator only appears for a git repo. Fixtures: alpha=git, beta=CLAUDE.md,
    # gamma=plain.
    client = _client(write_config, tmp_path)
    alpha = client.get("/api/projects/alpha/row").text
    beta = client.get("/api/projects/beta/row").text
    gamma = client.get("/api/projects/gamma/row").text
    assert '<use href="#ic-page"' in beta  # beta ships CLAUDE.md
    assert '<use href="#ic-page"' not in gamma  # gamma doesn't
    assert '<use href="#ic-git"' in alpha  # alpha is a git repo
    assert '<use href="#ic-git"' not in gamma  # gamma isn't


def test_card_unknown_project_404(write_config, tmp_path):
    assert _client(write_config, tmp_path).get("/api/projects/ghostproj/row").status_code == 404


def test_card_invalid_name_422(write_config, tmp_path):
    assert _client(write_config, tmp_path).get("/api/projects/ghost.proj/row").status_code == 422


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


# ----- not-found handling (friendly page vs JSON) -----------------------


def test_unknown_page_returns_friendly_html_404(write_config, tmp_path):
    # A browser (Accept: text/html) hitting a stale/mistyped non-API URL gets a
    # styled page with a way back, not a bare {"detail": "Not Found"} body.
    r = _client(write_config, tmp_path).get("/totally/bogus", headers={"accept": "text/html"})
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "does not exist" in r.text
    assert "Back to the dashboard" in r.text


def test_unknown_api_route_stays_json_404(write_config, tmp_path):
    # An /api path keeps the machine-readable error even for an HTML client — including
    # the bare prefix with no trailing slash (/api, /ws), not just /api/...
    c = _client(write_config, tmp_path)
    for path in ("/api/nope", "/api", "/ws"):
        r = c.get(path, headers={"accept": "text/html"})
        assert r.status_code == 404, path
        assert r.headers["content-type"].startswith("application/json"), path
        assert r.json()["detail"], path


def test_unknown_page_json_client_gets_json_404(write_config, tmp_path):
    # A non-HTML client gets JSON, never the page.
    r = _client(write_config, tmp_path).get(
        "/totally/bogus", headers={"accept": "application/json"}
    )
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_unknown_project_404_wording_is_consistent(write_config, tmp_path):
    # row / claude-md / preflight all phrase the missing-project 404 identically.
    c = _client(write_config, tmp_path)
    details = {
        c.get("/api/projects/ghostproj/row").json()["detail"],
        c.get("/api/projects/ghostproj/claude-md").json()["detail"],
        c.get("/api/projects/ghostproj/preflight").json()["detail"],
    }
    assert details == {"project 'ghostproj' not found"}


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
    assert body["token_breakdown"] == {
        "input": 1_000_000,
        "output": 0,
        "cache_creation": 0,
        "cache_read": 0,
    }
    assert body["cost_usd"] == 15.0  # 1 Mtok opus input @ $15/Mtok
    assert body["by_model"]["claude-opus-4-8"]["cost_usd"] == 15.0
    assert body["unpriced_models"] == []


# ----- usage badge config (dashboard injection) ------------------------


def _client_with(write_config, tmp_path, extra: str) -> TestClient:
    return TestClient(
        create_app(
            load_config(
                write_config(
                    f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}"
                )
            )
        )
    )


def test_dashboard_injects_usage_mode_cost_by_default(write_config, tmp_path):
    html = _client(write_config, tmp_path).get("/").text
    assert 'const USAGE_MODE = "cost";' in html


def test_dashboard_injects_usage_mode_tokens_when_set(write_config, tmp_path):
    html = _client_with(write_config, tmp_path, "usage:\n  mode: tokens\n").get("/").text
    assert 'const USAGE_MODE = "tokens";' in html


def test_dashboard_injects_usage_mode_off_via_legacy_show_cost(write_config, tmp_path):
    # The deprecated show_cost alias still flips the badge off through to the frontend.
    html = _client_with(write_config, tmp_path, "usage:\n  show_cost: false\n").get("/").text
    assert 'const USAGE_MODE = "off";' in html


def test_dashboard_injects_currency_usd_by_default(write_config, tmp_path):
    html = _client(write_config, tmp_path).get("/").text
    assert 'const CURRENCY = "USD";' in html
    assert 'const CURRENCY_SYMBOL = "$";' in html
    assert "const FX_RATE = 1.0;" in html
    assert "const TOKENS_INCLUDE_CACHE = true;" in html


def test_dashboard_injects_currency_custom_when_set(write_config, tmp_path):
    extra = "usage:\n  currency: EUR\n  currency_symbol: '€'\n  fx_rate: 0.92\n"
    html = _client_with(write_config, tmp_path, extra).get("/").text
    assert 'const CURRENCY = "EUR";' in html
    assert 'const CURRENCY_SYMBOL = "\\u20ac";' in html  # tojson ASCII-escapes € as €
    assert "const FX_RATE = 0.92;" in html


def test_dashboard_injects_tokens_exclude_cache_when_set(write_config, tmp_path):
    html = (
        _client_with(write_config, tmp_path, "usage:\n  token_total_includes_cache: false\n")
        .get("/")
        .text
    )
    assert "const TOKENS_INCLUDE_CACHE = false;" in html


# ----- busy-state :disabled coercion (Alpine 3.15.x missing-key bug) ------
# The Stop/Kill/Resume buttons live in `x-for` clones and bind `:disabled` to a busy
# map (agentStopping/hostedStopping/hostedResuming) that has no key for a row until a
# request is in flight. Alpine 3.15.x renders a bare `:disabled="map[key]"` as DISABLED
# when that key is absent (an explicit false renders enabled), so the button is stuck
# un-clickable on first paint. `!!` coercion yields a concrete false for a missing key.


def test_dashboard_coerces_detached_stop_disabled_binding(write_config, tmp_path):
    html = _client(write_config, tmp_path).get("/").text
    assert ':disabled="!!agentStopping[j.id]"' in html
    assert ':disabled="agentStopping[j.id]"' not in html  # no bare (buggy) binding


def test_dashboard_coerces_hosted_busy_disabled_bindings(write_config, tmp_path):
    html = _client_with(write_config, tmp_path, "claustrum:\n  enabled: true\n").get("/").text
    assert ':disabled="!!hostedStopping[h.claustrum_process_id]"' in html
    assert ':disabled="!!hostedResuming[h.claustrum_process_id]"' in html
    assert ':disabled="hostedStopping[h.claustrum_process_id]"' not in html
    assert ':disabled="hostedResuming[h.claustrum_process_id]"' not in html


# ----- Forget buttons (drop a stopped session from Recent/resumable) ------


def test_dashboard_renders_bridge_forget_button(write_config, tmp_path):
    html = _client(write_config, tmp_path).get("/").text
    assert '@click="forget(i.project)"' in html  # bridge Forget in Recent/resumable
    # Coerced so the busy-state binding isn't stuck-disabled on first paint.
    assert ':disabled="!!forgetting[i.project]"' in html


def test_dashboard_renders_hosted_forget_button(write_config, tmp_path):
    html = _client_with(write_config, tmp_path, "claustrum:\n  enabled: true\n").get("/").text
    assert '@click="forgetHosted(h)"' in html
    assert ':disabled="!!forgetting[h.claustrum_process_id]"' in html


# ----- /api/projects/{name}/metrics (live per-bridge resource sample) ----


def test_metrics_invalid_name_returns_422(write_config, tmp_path):
    r = _client(write_config, tmp_path).get("/api/projects/bad%20name/metrics")
    assert r.status_code == 422


def test_metrics_no_running_bridge_returns_false(write_config, tmp_path):
    r = _client(write_config, tmp_path).get("/api/projects/nope/metrics")
    assert r.status_code == 200
    assert r.json() == {"running": False}


def test_metrics_running_bridge_returns_sample(write_config, tmp_path):
    from clauster.models import InstanceStatus, RemoteControlInstance

    client = _client(write_config, tmp_path)
    inst = RemoteControlInstance(project="alpha", label="alpha")
    inst.status = InstanceStatus.RUNNING
    inst.bridge_pid = os.getpid()  # a live pid → a real sample
    client.app.state.runner._instances["alpha"] = inst
    body = client.get("/api/projects/alpha/metrics").json()
    assert body["running"] is True
    assert "cpu_percent" in body and "rss_bytes" in body and body["procs"] >= 1


def test_metrics_disabled_returns_false_even_when_running(write_config, tmp_path):
    from clauster.models import InstanceStatus, RemoteControlInstance

    client = _client_with(write_config, tmp_path, "metrics:\n  enabled: false\n")
    inst = RemoteControlInstance(project="alpha", label="alpha")
    inst.status = InstanceStatus.RUNNING
    inst.bridge_pid = os.getpid()
    client.app.state.runner._instances["alpha"] = inst
    # The gate fires before sampling, so a live running bridge still reports false.
    assert client.get("/api/projects/alpha/metrics").json() == {"running": False}


def test_dashboard_injects_metrics_flags(write_config, tmp_path):
    html = _client(write_config, tmp_path).get("/").text
    assert "const METRICS_ENABLED = true;" in html
    assert "const METRICS_SHOW_DISK = true;" in html
    assert "const METRICS_POLL_MS = 4000;" in html


def _running_metrics_client(write_config, tmp_path):
    from clauster.models import InstanceStatus, RemoteControlInstance

    client = _client(write_config, tmp_path)
    inst = RemoteControlInstance(project="alpha", label="alpha")
    inst.status = InstanceStatus.RUNNING
    inst.bridge_pid = os.getpid()
    client.app.state.runner._instances["alpha"] = inst
    return client, inst


def test_metrics_sampler_failure_returns_false(write_config, tmp_path, monkeypatch):
    # Fail closed: a sampling exception must yield {running: false}, never a 500.
    from clauster import metrics as metrics_mod

    client, _ = _running_metrics_client(write_config, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(metrics_mod, "sample_tree", boom)
    r = client.get("/api/projects/alpha/metrics")
    assert r.status_code == 200
    assert r.json() == {"running": False}


def test_metrics_pid_reuse_guard_returns_false(write_config, tmp_path):
    # A recorded start time that no longer matches the live PID = reuse → not-running.
    client, inst = _running_metrics_client(write_config, tmp_path)
    inst.bridge_proc_start = 1.0  # nowhere near os.getpid()'s real create time
    assert client.get("/api/projects/alpha/metrics").json() == {"running": False}


def test_metrics_gone_pid_returns_false(write_config, tmp_path):
    # No proc_start recorded (guard skipped) + a dead PID → sample is None → false.
    client, inst = _running_metrics_client(write_config, tmp_path)
    inst.bridge_pid = 2_147_483_646
    assert client.get("/api/projects/alpha/metrics").json() == {"running": False}


# ----- Prometheus /metrics exposition (gated, default off) --------------


def test_prometheus_disabled_returns_404(write_config, tmp_path):
    # Default off → the endpoint does not exist (404), even behind the auth guard.
    r = _client(write_config, tmp_path).get("/metrics")
    assert r.status_code == 404


def test_prometheus_enabled_returns_exposition(write_config, tmp_path):
    from clauster import __version__
    from clauster.models import InstanceStatus

    client = _client_with(write_config, tmp_path, "observability:\n  prometheus_enabled: true\n")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    body = r.text
    # build_info carries the running version; every InstanceStatus appears (0 here);
    # the discovery fixture exposes three valid projects (alpha/beta/gamma).
    assert f'clauster_build_info{{version="{__version__}"}} 1' in body
    assert "# TYPE clauster_bridges gauge" in body
    # Iterate the enum (source of truth) so a new InstanceStatus can't silently go
    # unexposed.
    for status in InstanceStatus:
        assert f'clauster_bridges{{status="{status.value}"}} 0' in body
    assert "clauster_projects 3" in body


def test_prometheus_counts_reflect_seeded_instances(write_config, tmp_path):
    from clauster.models import InstanceStatus, RemoteControlInstance

    client = _client_with(write_config, tmp_path, "observability:\n  prometheus_enabled: true\n")
    runner = client.app.state.runner
    running = RemoteControlInstance(project="alpha", label="alpha")
    running.status = InstanceStatus.RUNNING
    stopped = RemoteControlInstance(project="beta", label="beta")
    stopped.status = InstanceStatus.STOPPED
    runner._instances["alpha"] = running
    runner._instances["beta"] = stopped
    body = client.get("/metrics").text
    assert 'clauster_bridges{status="running"} 1' in body
    assert 'clauster_bridges{status="stopped"} 1' in body
    assert 'clauster_bridges{status="starting"} 0' in body


def test_prometheus_exposes_crash_counter(write_config, tmp_path):
    client = _client_with(write_config, tmp_path, "observability:\n  prometheus_enabled: true\n")
    client.app.state.runner._crash_counts["alpha"] = 2
    body = client.get("/metrics").text
    assert "# TYPE clauster_bridge_crashes_total counter" in body
    assert 'clauster_bridge_crashes_total{project="alpha"} 2' in body


def test_prometheus_exposes_per_bridge_cpu_rss(write_config, tmp_path, monkeypatch):
    from clauster.models import InstanceStatus, RemoteControlInstance

    client = _client_with(write_config, tmp_path, "observability:\n  prometheus_enabled: true\n")
    inst = RemoteControlInstance(project="alpha", label="alpha")
    inst.status = InstanceStatus.RUNNING
    inst.bridge_pid = 4242
    client.app.state.runner._instances["alpha"] = inst
    # Stub the tree sampler the app calls so the test needs no real process.
    monkeypatch.setattr(
        "clauster.app.metrics.sample_tree",
        lambda *a, **k: {"cpu_percent": 7.5, "rss_bytes": 2048},
    )
    body = client.get("/metrics").text
    assert 'clauster_bridge_cpu_percent{project="alpha"} 7.5' in body
    assert 'clauster_bridge_rss_bytes{project="alpha"} 2048' in body


def test_prometheus_exposes_hosted_gauge_and_omits_claustrum_when_disabled(write_config, tmp_path):
    client = _client_with(write_config, tmp_path, "observability:\n  prometheus_enabled: true\n")
    body = client.get("/metrics").text
    assert "clauster_hosted_sessions 0" in body
    assert "clauster_claustrum_up" not in body  # claustrum disabled by default → omitted


def test_prometheus_claustrum_up_zero_when_enabled_but_no_daemon(write_config, tmp_path):
    # With claustrum enabled but no live daemon (no lifespan), the gauge reports 0.
    client = _client_with(
        write_config,
        tmp_path,
        "observability:\n  prometheus_enabled: true\nclaustrum:\n  enabled: true\n",
    )
    assert "clauster_claustrum_up 0" in client.get("/metrics").text


def _running_bridge(client, *, pid=4242):
    from clauster.models import InstanceStatus, RemoteControlInstance

    inst = RemoteControlInstance(project="alpha", label="alpha")
    inst.status = InstanceStatus.RUNNING
    inst.bridge_pid = pid
    client.app.state.runner._instances["alpha"] = inst


def test_prometheus_skips_cpu_rss_when_metrics_disabled(write_config, tmp_path):
    # metrics.enabled false → no per-bridge sampling, so no cpu/rss series (gauges off).
    client = _client_with(
        write_config,
        tmp_path,
        "observability:\n  prometheus_enabled: true\nmetrics:\n  enabled: false\n",
    )
    _running_bridge(client)
    body = client.get("/metrics").text
    assert "clauster_bridge_cpu_percent" not in body


def test_prometheus_drops_bridge_on_sampling_error(write_config, tmp_path, monkeypatch):
    # A sampling error for a bridge drops it from the scrape, never 500s the endpoint.
    client = _client_with(write_config, tmp_path, "observability:\n  prometheus_enabled: true\n")
    _running_bridge(client)
    monkeypatch.setattr(
        "clauster.app.metrics.sample_tree",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "clauster_bridge_cpu_percent" not in r.text


def test_prometheus_drops_bridge_on_empty_sample(write_config, tmp_path, monkeypatch):
    # sample_tree returns None (pid already gone) → that bridge is omitted.
    client = _client_with(write_config, tmp_path, "observability:\n  prometheus_enabled: true\n")
    _running_bridge(client)
    monkeypatch.setattr("clauster.app.metrics.sample_tree", lambda *a, **k: None)
    body = client.get("/metrics").text
    assert "clauster_bridge_cpu_percent" not in body


@pytest.mark.parametrize("cur_create_time", [None, 999.0])
def test_prometheus_drops_bridge_on_pid_reuse(
    write_config, tmp_path, monkeypatch, cur_create_time
):
    # If the live PID's create-time no longer matches (or is gone), don't attribute its
    # cpu/rss to this bridge — guard against PID reuse, mirroring api_project_metrics.
    client = _client_with(write_config, tmp_path, "observability:\n  prometheus_enabled: true\n")
    _running_bridge(client)
    client.app.state.runner._instances["alpha"].bridge_proc_start = 100.0
    monkeypatch.setattr("clauster.app.procutil.proc_create_time", lambda *a, **k: cur_create_time)
    monkeypatch.setattr(
        "clauster.app.metrics.sample_tree", lambda *a, **k: {"cpu_percent": 9.0, "rss_bytes": 1}
    )
    assert "clauster_bridge_cpu_percent" not in client.get("/metrics").text


def test_metrics_token_grants_scrape_without_session(runner_config):
    # With auth on and a metrics_token set, a valid Bearer token reaches /metrics with no
    # session; a wrong/absent token is rejected; the existing gauges are unchanged.
    from clauster.app import create_app
    from clauster.runner import SessionRunner

    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.password_required = True
    config.observability.prometheus_enabled = True
    config.observability.metrics_token = "scrape-me"  # noqa: S105 — test token
    client = TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))

    ok = client.get("/metrics", headers={"authorization": "Bearer scrape-me"})
    assert ok.status_code == 200 and "clauster_build_info" in ok.text

    wrong = client.get(
        "/metrics", headers={"authorization": "Bearer nope"}, follow_redirects=False
    )
    assert wrong.status_code in {302, 303, 307, 401, 403}  # denied, not a 500
    assert "clauster_build_info" not in wrong.text  # payload withheld

    none = client.get("/metrics", follow_redirects=False)
    assert none.status_code in {302, 303, 307, 401, 403}
    assert "clauster_build_info" not in none.text


def test_metrics_token_unset_keeps_endpoint_behind_guard(runner_config):
    from clauster.app import create_app
    from clauster.runner import SessionRunner

    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.password_required = True
    config.observability.prometheus_enabled = True
    client = TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))
    # No token configured → a Bearer is meaningless; the guard still requires a session.
    r = client.get(
        "/metrics", headers={"authorization": "Bearer anything"}, follow_redirects=False
    )
    assert r.status_code in {302, 303, 307, 401, 403}
    assert "clauster_build_info" not in r.text


# ----- /api/widget (homepage-dashboard summary) -------------------------


def test_widget_shape_empty(write_config, tmp_path):
    # Stable, flat JSON: every InstanceStatus key present (0 when none), correct
    # projects_total (conftest seeds alpha/beta/gamma; bad-name + dotdir skipped),
    # version present, and content-type JSON. No bridges seeded → all zero.
    from clauster.models import InstanceStatus

    with _client(write_config, tmp_path) as client:
        r = client.get("/api/widget")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert set(body) == {"projects_total", "bridges", "running_total", "version"}
    # Every enum status is a key, all zero with no bridges seeded.
    assert set(body["bridges"]) == {s.value for s in InstanceStatus}
    assert all(v == 0 for v in body["bridges"].values())
    assert body["running_total"] == 0
    assert body["projects_total"] == 3
    assert body["version"]


def test_widget_counts_reflect_seeded_instances(write_config, tmp_path):
    # by-status counts reflect the seeded mix; running_total == bridges["running"].
    from clauster.models import InstanceStatus, RemoteControlInstance

    with _client(write_config, tmp_path) as client:
        runner = client.app.state.runner
        for name, status in (
            ("alpha", InstanceStatus.RUNNING),
            ("beta", InstanceStatus.RUNNING),
            ("gamma", InstanceStatus.STOPPED),
        ):
            inst = RemoteControlInstance(project=name, label=name)
            inst.status = status
            runner._instances[name] = inst
        body = client.get("/api/widget").json()
    assert body["bridges"]["running"] == 2
    assert body["bridges"]["stopped"] == 1
    assert body["bridges"]["crashed"] == 0
    assert body["running_total"] == body["bridges"]["running"] == 2
    # Confirm the at-rest fields still hold under the seeded-instance scenario.
    assert body["projects_total"] == 3
    assert body["version"]
