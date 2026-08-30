"""Route validation / error branches in app.py (auth off — no CSRF ceremony).

Covers the input-guard and not-found paths the feature tests don't hit:
empty/missing body fields, unknown-instance stop, unknown-project trust, and the
claude-md resolver / read-error branches.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import ClausterConfig, load_config
from clauster.runner import SessionRunner
from helpers import RecordingEmitter, assert_stays_empty, wait_for_calls

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


def test_create_project_provision_error_maps_400(write_config, tmp_path, monkeypatch):
    from clauster.provisioning import ProvisionError

    def boom(*a, **k):
        raise ProvisionError("disk on fire")

    monkeypatch.setattr("clauster.app.create_project", boom)
    with _client(write_config, tmp_path) as client:
        r = client.post("/api/projects", json={"name": "proj"})
    assert r.status_code == 400
    assert "disk on fire" in r.json()["detail"]


def test_create_project_git_unavailable_maps_503(write_config, tmp_path, monkeypatch):
    from clauster.provisioning import GitUnavailable

    def boom(*a, **k):
        raise GitUnavailable("git not found")

    monkeypatch.setattr("clauster.app.create_project", boom)
    with _client(write_config, tmp_path) as client:
        r = client.post("/api/projects", json={"name": "proj"})
    assert r.status_code == 503
    assert "git not found" in r.json()["detail"]


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


# ----- batch preflight (first-paint, one discovery scan) ----------------


def test_batch_preflight_shape_and_all_projects(write_config, tmp_path):
    # First-paint batch returns {name: {ok, checks}} for every discovered project from
    # ONE scan (replacing N per-project /preflight calls). Each entry carries the same
    # {ok, checks:[{name,status,detail}]} contract the per-project route returns.
    r = _client(write_config, tmp_path).get("/api/projects/preflight")
    assert r.status_code == 200
    body = r.json()
    assert {"alpha", "beta", "gamma"} <= set(body)  # every discovered project present
    entry = body["alpha"]
    assert isinstance(entry["ok"], bool)
    names = {c["name"] for c in entry["checks"]}
    assert {"trust", "git"} <= names
    for c in entry["checks"]:
        assert set(c) == {"name", "status", "detail"}
        assert c["status"] in {"ok", "warn", "fail"}


def test_batch_preflight_literal_path_beats_name_route(write_config, tmp_path):
    # The literal /api/projects/preflight route must win over /{name}/preflight: the
    # batch returns a dict keyed by project name, never a single-project {project,...}.
    body = _client(write_config, tmp_path).get("/api/projects/preflight").json()
    assert "project" not in body  # not the per-project shape
    assert isinstance(body.get("alpha"), dict)


# ----- #837: MCP-approval preflight surfaced through both preflight routes ----


def test_project_preflight_surfaces_unapproved_mcp_servers(write_config, tmp_path, projects_root):
    # A committed .mcp.json with servers nobody has approved/rejected yet must show
    # up as a WARN "mcp-approval" check on the per-project route — the read-only,
    # pre-launch signal #837 asks for (never a FAIL; never blocks the launch).
    (projects_root / "alpha" / ".mcp.json").write_text(
        '{"mcpServers": {"unapproved-one": {"command": "x"}}}', encoding="utf-8"
    )
    r = _client(write_config, tmp_path).get("/api/projects/alpha/preflight")
    assert r.status_code == 200
    body = r.json()
    checks = {c["name"]: c for c in body["checks"]}
    assert "mcp-approval" in checks
    assert checks["mcp-approval"]["status"] == "warn"
    assert "unapproved-one" in checks["mcp-approval"]["detail"]
    assert body["ok"] is True  # WARN-only: a preflight warning never flips ok False


def test_project_preflight_no_mcp_json_omits_the_check(write_config, tmp_path, projects_root):
    # No .mcp.json in the project at all -> no mcp-approval check appears (nothing
    # to warn about), on both routes.
    r = _client(write_config, tmp_path).get("/api/projects/alpha/preflight")
    assert "mcp-approval" not in {c["name"] for c in r.json()["checks"]}


def test_batch_preflight_surfaces_unapproved_mcp_servers(write_config, tmp_path, projects_root):
    # Same signal on the batch (first-paint) route used by the dashboard grid.
    (projects_root / "alpha" / ".mcp.json").write_text(
        '{"mcpServers": {"needs-approval": {"command": "x"}}}', encoding="utf-8"
    )
    body = _client(write_config, tmp_path).get("/api/projects/preflight").json()
    checks = {c["name"]: c for c in body["alpha"]["checks"]}
    assert "mcp-approval" in checks
    assert checks["mcp-approval"]["status"] == "warn"
    assert "needs-approval" in checks["mcp-approval"]["detail"]


# ----- projects sortmeta (batch sort keys for the Projects sort control, FE-2) ----


def test_projects_sortmeta_shape_and_all_projects(write_config, tmp_path, monkeypatch):
    # {name: {last_used, cost_usd}} for every discovered project, from the history
    # rollup. alpha gets a known rollup; the others have no history -> null fields.
    from datetime import datetime

    from clauster.db import stores as stores_mod

    when = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)

    def _sortmeta(self, names):
        # alpha has history; the others are omitted -> caller defaults to (None, None).
        return {"alpha": (when, 1.25)}

    monkeypatch.setattr(stores_mod.SessionHistoryStore, "sortmeta_for_all", _sortmeta)
    r = _client(write_config, tmp_path).get("/api/projects/sortmeta")
    assert r.status_code == 200
    body = r.json()
    assert {"alpha", "beta", "gamma"} <= set(body)  # every discovered project present
    assert body["alpha"] == {"last_used": when.isoformat(), "cost_usd": 1.25}
    assert body["beta"] == {"last_used": None, "cost_usd": None}  # no history -> nulls


def test_projects_sortmeta_literal_path_beats_name_route(write_config, tmp_path):
    # The literal /api/projects/sortmeta route must win over /{name}/...: returns a dict
    # keyed by project name (rollup or null fields), never a single-project shape.
    body = _client(write_config, tmp_path).get("/api/projects/sortmeta").json()
    assert "project" not in body
    assert isinstance(body.get("alpha"), dict)
    assert set(body["alpha"]) == {"last_used", "cost_usd"}


def test_projects_sortmeta_degrades_to_empty_on_error(write_config, tmp_path, monkeypatch):
    # Advisory endpoint: an infra read error (DB engine / IO) must degrade to {} (the
    # client falls back to name order), never 500 the dashboard.
    from clauster.db import stores as stores_mod

    def _boom(self, names):
        raise OSError("db gone")

    monkeypatch.setattr(stores_mod.SessionHistoryStore, "sortmeta_for_all", _boom)
    r = _client(write_config, tmp_path).get("/api/projects/sortmeta")
    assert r.status_code == 200
    assert r.json() == {}


def test_dashboard_renders_projects_sort_control(write_config, tmp_path):
    # The Projects zone exposes the opt-in sort dropdown (name / last-used / cost) and
    # the client defaults to 'name' (no reorder until the user chooses).
    html = _client(write_config, tmp_path).get("/").text
    assert 'aria-label="Sort projects"' in html
    assert '<option value="last-used">' in html
    assert '<option value="cost">' in html
    assert 'projectSort: "name"' in html


def _client_empty(tmp_path) -> TestClient:
    # A client whose projects_root is an empty directory, so discovery finds no
    # projects and the dashboard renders its {% else %} empty branch (#692).
    empty_root = tmp_path / "empty_root"
    empty_root.mkdir()
    cfg = tmp_path / "clauster_empty.yml"
    cfg.write_text(
        f"projects_root: {empty_root}\nclaude:\n  binary: {FAKE_CLAUDE}\n"
        f"state_dir: {tmp_path}/.se\n",
        encoding="utf-8",
    )
    return TestClient(create_app(load_config(cfg)))


def test_empty_dashboard_renders_orientation_card(tmp_path):
    # First-run orientation (#692): an empty projects_root renders the dismissible
    # orientation card. Assert on STRUCTURE/wiring (stable contract), not the
    # PROPOSED visible copy — the card wording awaits maintainer approval and may
    # change before merge, so verbatim-text assertions would be brittle.
    html = _client_empty(tmp_path).get("/").text
    # The card element and its labelled heading exist.
    assert 'id="orientation-card"' in html
    assert '<h2 class="empty-title" id="orientation-heading">' in html
    assert 'aria-labelledby="orientation-heading"' in html
    # Dismiss control: labeled (stable aria-label), keyboard-reachable, wired to
    # the persisted action — assert the contract, not the visible "Got it" text.
    assert 'aria-label="Dismiss the welcome card"' in html
    assert "dismissOrientation()" in html
    # Persistence is mirrored from the theme pattern: an Alpine flag read from
    # localStorage under a stable key.
    assert "orientationDismissed" in html
    assert "clauster-orientation-dismissed" in html


def test_populated_dashboard_omits_orientation_card(write_config, tmp_path):
    # A dashboard with discovered projects never renders the orientation card —
    # it lives strictly in the {% else %} empty branch. Assert on the stable
    # element id, not the (proposed) copy.
    html = _client(write_config, tmp_path).get("/").text
    assert 'id="orientation-card"' not in html
    assert 'id="orientation-heading"' not in html


# ----- single-row fragment (reactive insertion, no full reload) ---------


def test_card_renders_known_project(write_config, tmp_path):
    # The fragment route returns the same row markup the grid loop renders.
    r = _client(write_config, tmp_path).get("/api/projects/alpha/row")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert 'data-project="alpha"' in r.text
    assert "Run Claude here" in r.text  # it's a real row, not an empty stub


def test_card_reflects_project_shape(write_config, tmp_path):
    # Per-project Jinja conditionals render: the CLAUDE.md meta indicator only
    # appears where a CLAUDE.md is present; the Git meta indicator only appears for
    # a git repo. Fixtures: alpha=git, beta=CLAUDE.md, gamma=plain. The CLAUDE.md
    # badge is matched by its unique title/label (the ic-page icon is no longer
    # exclusive to it — the #431 transcript trigger reuses the same file-text glyph).
    client = _client(write_config, tmp_path)
    alpha = client.get("/api/projects/alpha/row").text
    beta = client.get("/api/projects/beta/row").text
    gamma = client.get("/api/projects/gamma/row").text
    assert 'title="A CLAUDE.md file is present"' in beta  # beta ships CLAUDE.md
    assert 'title="A CLAUDE.md file is present"' not in gamma  # gamma doesn't
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


def test_put_claude_md_untrusted_returns_403(write_config, tmp_path, monkeypatch):
    # An untrusted project dir refuses the write: write_claude_md raises
    # ClaudeMdNotTrusted, which the route surfaces as 403 (not a swallowed 200).
    from clauster.claude_md import ClaudeMdNotTrusted

    def boom(*a, **k):
        raise ClaudeMdNotTrusted("/p is not a trusted directory")

    monkeypatch.setattr("clauster.app.write_claude_md", boom)
    with _client(write_config, tmp_path) as client:
        r = client.put("/api/projects/alpha/claude-md", json={"content": "x"})
    assert r.status_code == 403
    assert "not a trusted directory" in r.json()["detail"]


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


# ----- bg-settled webhook (#432) ----------------------------------------


def _client_webhooks(write_config, tmp_path, event_line: str) -> TestClient:
    extra = f"webhooks:\n  enabled: true\n  urls: ['https://hook.test/h']\n  events:\n{event_line}"
    return TestClient(
        create_app(
            load_config(
                write_config(
                    f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}"
                )
            )
        )
    )


def test_bg_settled_webhook_fires_and_redacts_detail(write_config, tmp_path, monkeypatch):
    # The supervisor stop is stubbed (no real `claude`); we test only the emission wiring.
    def fake_stop(job_id, *, binary, **kw):
        return {
            "id": job_id,
            "settled": True,
            "removed": True,
            "detail": "removed session_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        }

    monkeypatch.setattr("clauster.supervisor.stop_background_job", fake_stop)

    with _client_webhooks(write_config, tmp_path, "    bg-settled: true\n") as client:
        rec = RecordingEmitter()
        client.app.state.runner._webhooks = rec
        resp = client.delete("/api/agents/abc12345")
        assert resp.status_code == 200, resp.text
        calls = wait_for_calls(rec)

    assert len(calls) == 1
    event, payload = calls[0]
    assert event == "bg-settled"
    assert payload["event_type"] == "bg-settled"
    assert payload["id"] == "abc12345"
    assert payload["settled"] is True and payload["removed"] is True
    # The raw session id in detail is masked before egress.
    assert "session_01ARZ3NDEKTSV4RRFFQ69G5FAV" not in (payload["detail"] or "")
    assert "<redacted>" in (payload["detail"] or "")


def test_bg_settled_webhook_silent_when_default_off(write_config, tmp_path, monkeypatch):
    # webhooks enabled but bg-settled NOT opted in -> the real emitter's gate drops it.
    def fake_stop(job_id, *, binary, **kw):
        return {"id": job_id, "settled": False, "removed": True, "detail": None}

    monkeypatch.setattr("clauster.supervisor.stop_background_job", fake_stop)

    extra = "webhooks:\n  enabled: true\n  urls: ['https://hook.test/h']\n"
    client = TestClient(
        create_app(
            load_config(
                write_config(
                    f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}"
                )
            )
        )
    )
    with client:
        emitter = client.app.state.runner._webhooks
        assert emitter.active and emitter.wants("bg-settled") is False
        aemit_calls: list = []
        orig = emitter.aemit

        async def _spy(event, payload):
            aemit_calls.append(event)
            await orig(event, payload)

        monkeypatch.setattr(emitter, "aemit", _spy)
        resp = client.delete("/api/agents/abc12345")
        assert resp.status_code == 200
        # Negative assertion: confirm no emit fires across a window, failing fast if one does.
        assert_stays_empty(aemit_calls)


async def test_hosted_permission_needed_closure_emits_webhook(runner_config):
    """create_app wires _on_hosted_permission_needed to fire the #432 webhook.

    The hosted-layer tests inject a mock callback, so the real app-side closure —
    which forwards the parked-prompt signal through runner.emit_event — is only
    exercised here. Call the wired closure on the running loop and assert the
    emitter records the redacted permission-needed payload.
    """
    config, claude_json = runner_config
    cfg = ClausterConfig(
        projects_root=config.projects_root,
        state_dir=config.state_dir,
        claude={"binary": config.claude.binary},
    )
    runner = SessionRunner(cfg, claude_json=claude_json)
    rec = RecordingEmitter()
    runner._webhooks = rec
    app = create_app(cfg, runner)

    callback = app.state.hosted._on_permission_needed
    assert callback is not None
    callback("0f1e2d3c", "can_use_tool")
    await asyncio.gather(*runner._notify_tasks)

    assert rec.calls == [
        (
            "permission-needed",
            {
                "event_type": "permission-needed",
                "process_id": "0f1e2d3c",
                "subtype": "can_use_tool",
            },
        )
    ]


class _RecordingNotifier:
    """Active stand-in notifier capturing anotify calls (for app-wiring tests)."""

    def __init__(self) -> None:
        self.active = True
        self.calls: list[tuple[str, str]] = []

    async def anotify(self, title: str, body: str) -> None:
        self.calls.append((title, body))


async def test_hosted_permission_needed_closure_fires_notification(runner_config):
    """The wired closure also fires a #541 notification when notify_on_permission is on."""
    config, claude_json = runner_config
    cfg = ClausterConfig(
        projects_root=config.projects_root,
        state_dir=config.state_dir,
        claude={"binary": config.claude.binary},
        notifications={"enabled": True, "urls": ["slack://x"], "notify_on_permission": True},
    )
    runner = SessionRunner(cfg, claude_json=claude_json)
    runner._webhooks = RecordingEmitter()
    notifier = _RecordingNotifier()
    runner._notifier = notifier
    app = create_app(cfg, runner)

    app.state.hosted._on_permission_needed("0f1e2d3c", "can_use_tool")
    await asyncio.gather(*runner._notify_tasks)

    assert len(notifier.calls) == 1
    title, body = notifier.calls[0]
    assert "permission" in title.lower()
    # The redacted subtype rides the body; the raw prompt body never does.
    assert "can_use_tool" in body


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


# ----- read-only transcript viewer (#431) ------------------------------

TURNS_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "transcripts" / "turns-session.jsonl"
)


def _plant_transcripts(monkeypatch, tmp_path, project_name="gamma", sessions=None):
    """Redirect the transcript dir to a tmp dir and plant session transcripts.

    Mirrors how Claude stores per-session JSONLs under
    ``<claude_projects_dir>/<sanitized-cwd>/<session>.jsonl``. The route calls
    ``usage.transcript_paths_for`` / ``usage.resolve_session_transcript`` WITHOUT a
    ``claude_projects_dir`` arg, so they use the import-time default
    (``~/.claude/projects`` — the live account dir we must never touch). We rebind
    those two readers to inject the tmp ``claude_dir`` instead, so the test stays
    fully off the real home. The route resolves ``config.projects_root / name``
    (projects_root == tmp_path). Returns the planted transcript dir.
    """
    from clauster import usage as usage_mod
    from clauster.pointers import sanitize_cwd

    claude_dir = tmp_path / "claude_projects"
    _real_paths_for = usage_mod.transcript_paths_for
    _real_resolve = usage_mod.resolve_session_transcript
    monkeypatch.setattr(
        usage_mod,
        "transcript_paths_for",
        lambda project_path, claude_projects_dir=claude_dir: _real_paths_for(
            project_path, claude_projects_dir
        ),
    )
    monkeypatch.setattr(
        usage_mod,
        "resolve_session_transcript",
        lambda project_path, session, claude_projects_dir=claude_dir: _real_resolve(
            project_path, session, claude_projects_dir
        ),
    )
    project_path = tmp_path / project_name  # projects_root is tmp_path
    tdir = claude_dir / sanitize_cwd(project_path)
    tdir.mkdir(parents=True, exist_ok=True)
    for session in sessions or {}:
        (tdir / f"{session}.jsonl").write_bytes(TURNS_FIXTURE.read_bytes())
    return tdir


def test_transcripts_list_invalid_name_422(write_config, tmp_path):
    r = _client(write_config, tmp_path).get("/api/projects/bad.name/transcripts")
    assert r.status_code == 422


def test_transcripts_list_empty_project(write_config, tmp_path, monkeypatch):
    _plant_transcripts(monkeypatch, tmp_path, sessions=[])
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts")
    assert r.status_code == 200
    body = r.json()
    assert body == {"project": "gamma", "sessions": []}


def test_transcripts_list_newest_first_with_counts(write_config, tmp_path, monkeypatch):
    tdir = _plant_transcripts(monkeypatch, tmp_path, sessions=["s-old", "s-new"])
    # Make s-new strictly newer so the newest-first ordering is deterministic.
    os.utime(tdir / "s-old.jsonl", (1_000_000, 1_000_000))
    os.utime(tdir / "s-new.jsonl", (2_000_000, 2_000_000))
    body = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts").json()
    sessions = body["sessions"]
    assert [s["session"] for s in sessions] == ["s-new", "s-old"]  # newest first
    assert all(s["turn_count"] == 4 for s in sessions)  # fixture has 4 renderable turns
    assert all(
        set(s)
        == {
            "session",
            "mtime",
            "turn_count",
            "live",
            "first_prompt",
            "first_ts",
            "last_ts",
            "is_subagent",
        }
        for s in sessions
    )
    assert all(s["live"] is False for s in sessions)  # no running session → none live


def test_transcripts_list_resume_picker_fields(write_config, tmp_path, monkeypatch):
    """The listing carries the resume picker's label fields (#303): first USER
    prompt + the conversation's first/last turn timestamps, from the same
    redaction-safe per-line scan that already computes turn_count."""
    tdir = _plant_transcripts(monkeypatch, tmp_path, sessions=["s-pick"])
    # A second transcript whose first user prompt exceeds the 120-char cap and
    # carries no timestamps — the truncation and ""-degradation paths.
    (tdir / "s-long.jsonl").write_text(
        json.dumps({"message": {"role": "user", "content": "x" * 500}}) + "\n",
        encoding="utf-8",
    )
    sessions = {
        s["session"]: s
        for s in _client(write_config, tmp_path)
        .get("/api/projects/gamma/transcripts")
        .json()["sessions"]
    }
    picked = sessions["s-pick"]
    assert picked["first_prompt"] == "Hi, can you read my config?"  # first USER turn
    assert picked["first_ts"] == "2026-06-25T10:00:00Z"
    assert picked["last_ts"] == "2026-06-25T10:00:15Z"
    long = sessions["s-long"]
    assert len(long["first_prompt"]) == 120  # server-side truncation
    assert long["first_ts"] == "" and long["last_ts"] == ""  # no timestamps → ""


def test_transcripts_list_flags_subagent_transcripts(write_config, tmp_path, monkeypatch):
    """A dispatched-subagent (sidechain) transcript is FLAGGED, not dropped (#1092).

    The listing feeds both the read-only viewer and the fork picker; the server keeps the
    entry and only the picker filters on it, so nothing is hidden from the viewer.
    """
    tdir = _plant_transcripts(monkeypatch, tmp_path, sessions=["s-real"])
    (tdir / "s-sub.jsonl").write_text(
        json.dumps(
            {"isSidechain": True, "message": {"role": "user", "content": "Review this change"}}
        )
        + "\n",
        encoding="utf-8",
    )
    sessions = {
        s["session"]: s
        for s in _client(write_config, tmp_path)
        .get("/api/projects/gamma/transcripts")
        .json()["sessions"]
    }
    assert sessions["s-sub"]["is_subagent"] is True
    assert sessions["s-real"]["is_subagent"] is False  # the fixture is a real conversation


def test_transcripts_list_badges_running_bridge_session_live(write_config, tmp_path, monkeypatch):
    """A transcript whose session id maps to a running bridge/agent is badged live (#614)."""
    from clauster.models import Attribution, WorkingSession

    _plant_transcripts(monkeypatch, tmp_path, sessions=["live-one", "dead-one"])
    client = _client(write_config, tmp_path)
    # A live agents --json session at the project's cwd whose local_uuid is one of the
    # planted transcript stems. The cwd must sanitize to the same dir Claude wrote into.
    client.app.state.runner._sessions = [
        WorkingSession(
            pid=4242,
            cwd=tmp_path / "gamma",  # projects_root / name -> same sanitized transcript dir
            kind="interactive",
            started_at=4242,
            local_uuid="live-one",
            attribution=Attribution.TRACKED,
        )
    ]
    sessions = client.get("/api/projects/gamma/transcripts").json()["sessions"]
    live = {s["session"]: s["live"] for s in sessions}
    assert live == {"live-one": True, "dead-one": False}
    # Live-first ordering: the running session sorts ahead of the dead one.
    assert sessions[0]["session"] == "live-one"


def test_transcripts_list_badges_hosted_session_live(write_config, tmp_path, monkeypatch):
    """A hosted (claustrum) session's captured uuid badges its transcript live (#614)."""
    from clauster.models import InstanceStatus, RemoteControlInstance

    _plant_transcripts(monkeypatch, tmp_path, sessions=["hosted-uuid", "other"])
    client = _client(write_config, tmp_path)
    inst = RemoteControlInstance(
        project="gamma",
        label="hosted:gamma",
        channel="hosted",
        claustrum_process_id="01HOSTED000000000000000A",
        claude_session_uuid="hosted-uuid",
        status=InstanceStatus.RUNNING,
    )
    client.app.state.hosted._instances[inst.claustrum_process_id] = inst
    live = {
        s["session"]: s["live"]
        for s in client.get("/api/projects/gamma/transcripts").json()["sessions"]
    }
    assert live == {"hosted-uuid": True, "other": False}


def test_transcripts_list_stopped_hosted_session_not_live(write_config, tmp_path, monkeypatch):
    """A STOPPED/CRASHED hosted instance must NOT badge its transcript live (#614).

    `_instances` is not pruned on session end, so the route must status-filter to
    RUNNING/STARTING — otherwise a stopped session keeps showing as live.
    """
    from clauster.models import InstanceStatus, RemoteControlInstance

    _plant_transcripts(monkeypatch, tmp_path, sessions=["hosted-uuid", "other"])
    client = _client(write_config, tmp_path)
    inst = RemoteControlInstance(
        project="gamma",
        label="hosted:gamma",
        channel="hosted",
        claustrum_process_id="01HOSTED000000000000000A",
        claude_session_uuid="hosted-uuid",
        status=InstanceStatus.STOPPED,
    )
    client.app.state.hosted._instances[inst.claustrum_process_id] = inst
    live = {
        s["session"]: s["live"]
        for s in client.get("/api/projects/gamma/transcripts").json()["sessions"]
    }
    assert live == {"hosted-uuid": False, "other": False}


def test_transcripts_list_no_live_for_other_project(write_config, tmp_path, monkeypatch):
    """A running session at a DIFFERENT cwd never badges a transcript here live (#614)."""
    from clauster.models import Attribution, WorkingSession

    _plant_transcripts(monkeypatch, tmp_path, sessions=["s1"])
    client = _client(write_config, tmp_path)
    # Same uuid, but a cwd that sanitizes to a different transcript dir -> not a match.
    client.app.state.runner._sessions = [
        WorkingSession(
            pid=7,
            cwd=tmp_path / "elsewhere",
            kind="interactive",
            started_at=7,
            local_uuid="s1",
            attribution=Attribution.EXTERNAL,
        )
    ]
    sessions = client.get("/api/projects/gamma/transcripts").json()["sessions"]
    assert sessions[0]["live"] is False


def test_transcripts_list_skips_session_vanished_mid_walk(write_config, tmp_path, monkeypatch):
    # A transcript listed but removed before its turn-read (racing session cleanup)
    # is skipped, not fatal: the list still returns the surviving sessions.
    from clauster import usage as usage_mod

    _plant_transcripts(monkeypatch, tmp_path, sessions=["keep", "vanish"])
    real_read = usage_mod.read_transcript_turns

    def _read(path):
        if path.stem == "vanish":
            raise FileNotFoundError("gone")
        return real_read(path)

    monkeypatch.setattr(usage_mod, "read_transcript_turns", _read)
    body = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts").json()
    assert [s["session"] for s in body["sessions"]] == ["keep"]


def test_transcripts_list_read_error_503_no_path_leak(write_config, tmp_path, monkeypatch):
    from clauster import usage as usage_mod

    def _boom(*a, **k):
        raise OSError("[Errno 13] Permission denied: '/home/secret/projects/gamma'")

    monkeypatch.setattr(usage_mod, "transcript_paths_for", _boom)
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts")
    assert r.status_code == 503
    assert r.json()["detail"] == "could not read transcripts"
    assert "/home/secret" not in r.text and "Errno" not in r.text


def test_transcript_session_invalid_name_422(write_config, tmp_path):
    r = _client(write_config, tmp_path).get("/api/projects/bad.name/transcripts/s1")
    assert r.status_code == 422


def test_transcript_session_returns_redacted_turns(write_config, tmp_path, monkeypatch):
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts/sess1")
    assert r.status_code == 200
    body = r.json()
    assert body["session"] == "sess1"
    assert body["total"] == 4
    # Newest-first: the last assistant turn leads the page.
    assert body["turns"][0]["role"] == "assistant"
    assert {"role", "content", "model", "timestamp"} == set(body["turns"][0])


def test_transcript_session_redacts_planted_secret(write_config, tmp_path, monkeypatch):
    # THE security gate at the ROUTE boundary: a planted session id / sk- key / AKIA
    # id in transcript text must never reach the HTTP response — sanitize_line applied.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts/sess1")
    assert r.status_code == 200
    assert "DEADBEEF012345" not in r.text
    assert "sk-ABCDEF0123456789ghij" not in r.text
    assert "AKIAIOSFODNN7EXAMPLE" not in r.text
    assert "<redacted>" in r.text


def test_transcript_session_pagination_cursor_advances(write_config, tmp_path, monkeypatch):
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    client = _client(write_config, tmp_path)
    # 4 turns, limit 2 -> first page of 2 with a next_cursor, second page exhausts it.
    first = client.get("/api/projects/gamma/transcripts/sess1?limit=2").json()
    assert len(first["turns"]) == 2
    assert first["next_cursor"] == 2
    second = client.get(
        f"/api/projects/gamma/transcripts/sess1?limit=2&cursor={first['next_cursor']}"
    ).json()
    assert len(second["turns"]) == 2
    assert second["next_cursor"] is None  # exhausted
    # The two pages are disjoint and cover all turns newest-first.
    assert first["turns"] + second["turns"] != first["turns"]  # advanced, not repeated


@pytest.mark.parametrize("session", ["..", "../escape", "a%2Fb", "..%2F..%2Fx"])
def test_transcript_session_traversal_router_rejects_404(
    write_config, tmp_path, monkeypatch, session
):
    # First line of defense: a traversal/slash session is unroutable (the literal
    # path has too many/normalized segments), so Starlette 404s before our handler.
    # The security outcome is the same — no escape, no 500. (%2F decodes to "/".)
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    r = _client(write_config, tmp_path).get(f"/api/projects/gamma/transcripts/{session}")
    assert r.status_code == 404


@pytest.mark.parametrize("session", ["a%5Cb", "foo..bar"])
def test_transcript_session_guard_rejects_unsafe_404(write_config, tmp_path, monkeypatch, session):
    # Defense-in-depth: a session that DOES reach the handler but is unsafe (a
    # backslash separator) or simply has no matching file -> resolve_session_transcript
    # fails closed and the route returns our defined 404, never a path escape or 500.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    r = _client(write_config, tmp_path).get(f"/api/projects/gamma/transcripts/{session}")
    assert r.status_code == 404
    assert r.json()["detail"] == "transcript not found"


def test_transcript_session_unknown_404(write_config, tmp_path, monkeypatch):
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts/ghost")
    assert r.status_code == 404


def test_transcript_session_read_error_503_no_path_leak(write_config, tmp_path, monkeypatch):
    from clauster import usage as usage_mod

    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])

    def _boom(path):
        raise OSError("[Errno 13] Permission denied: '/home/secret/sess1.jsonl'")

    monkeypatch.setattr(usage_mod, "read_transcript_turns", _boom)
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts/sess1")
    assert r.status_code == 503
    assert r.json()["detail"] == "could not read transcript"
    assert "/home/secret" not in r.text and "Errno" not in r.text


# ----- transcript sort toggle + in-message search (#612) ----------------


def test_transcript_session_default_order_is_newest_first(write_config, tmp_path, monkeypatch):
    # No order param == the historical newest-first default: the last assistant turn leads.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts/sess1")
    assert r.status_code == 200
    body = r.json()
    assert body["turns"][0]["role"] == "assistant"  # turn 4 (newest)
    assert body["turns"][-1]["role"] == "user"  # turn 1 (oldest)


def test_transcript_session_order_asc_is_oldest_first(write_config, tmp_path, monkeypatch):
    # order=asc flips the page to chronological: the first user turn leads.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts/sess1?order=asc")
    assert r.status_code == 200
    body = r.json()
    assert body["turns"][0]["role"] == "user"  # turn 1 (oldest)
    assert "read my config" in body["turns"][0]["content"]
    assert body["turns"][-1]["role"] == "assistant"  # turn 4 (newest)


def test_transcript_session_order_asc_desc_are_reverses(write_config, tmp_path, monkeypatch):
    # The two orders are exact reverses of each other across the whole transcript.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    client = _client(write_config, tmp_path)
    desc = client.get("/api/projects/gamma/transcripts/sess1?limit=500").json()["turns"]
    asc = client.get("/api/projects/gamma/transcripts/sess1?order=asc&limit=500").json()["turns"]
    assert list(reversed(asc)) == desc


def test_transcript_session_unknown_order_falls_back_to_desc(write_config, tmp_path, monkeypatch):
    # A typo'd / unexpected order value must never silently reverse — it stays newest-first.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    client = _client(write_config, tmp_path)
    default = client.get("/api/projects/gamma/transcripts/sess1").json()["turns"]
    bogus = client.get("/api/projects/gamma/transcripts/sess1?order=sideways").json()["turns"]
    assert bogus == default


def test_transcript_session_order_asc_pagination_terminates(write_config, tmp_path, monkeypatch):
    # asc pagination still walks cursor 0 → end, terminates (next_cursor None), no double-render.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    client = _client(write_config, tmp_path)
    first = client.get("/api/projects/gamma/transcripts/sess1?order=asc&limit=2").json()
    assert len(first["turns"]) == 2
    assert first["next_cursor"] == 2
    second = client.get(
        f"/api/projects/gamma/transcripts/sess1?order=asc&limit=2&cursor={first['next_cursor']}"
    ).json()
    assert second["next_cursor"] is None
    # Together the two pages cover every turn exactly once, oldest-first.
    full = client.get("/api/projects/gamma/transcripts/sess1?order=asc&limit=500").json()["turns"]
    assert first["turns"] + second["turns"] == full


def test_transcript_search_filters_to_matching_turns(write_config, tmp_path, monkeypatch):
    # q= returns only turns whose (redacted) content contains the term.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts/sess1?q=config")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1  # only the first user turn says "read my config"
    assert len(body["turns"]) == 1
    assert "config" in body["turns"][0]["content"]


def test_transcript_search_is_case_insensitive(write_config, tmp_path, monkeypatch):
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    client = _client(write_config, tmp_path)
    lower = client.get("/api/projects/gamma/transcripts/sess1?q=config").json()
    upper = client.get("/api/projects/gamma/transcripts/sess1?q=CONFIG").json()
    assert upper["total"] == lower["total"] == 1


def test_transcript_search_no_match_returns_empty(write_config, tmp_path, monkeypatch):
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    r = _client(write_config, tmp_path).get(
        "/api/projects/gamma/transcripts/sess1?q=zzz-nothing-matches"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["turns"] == []
    assert body["next_cursor"] is None


def test_transcript_search_matches_redacted_text_not_secret(write_config, tmp_path, monkeypatch):
    # THE security gate: searching for the planted secret's plaintext must NOT confirm it.
    # The filter runs over the redacted content, so the secret is already <redacted>.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    client = _client(write_config, tmp_path)
    # The raw secret no longer exists in any turn's content -> no match.
    leaked = client.get("/api/projects/gamma/transcripts/sess1?q=DEADBEEF012345").json()
    assert leaked["total"] == 0
    # But the masked token IS searchable, proving the filter sees the redacted text.
    masked = client.get("/api/projects/gamma/transcripts/sess1?q=redacted").json()
    assert masked["total"] >= 1
    assert "DEADBEEF012345" not in masked  # the secret never rides back in the response


def test_transcript_search_blank_query_is_no_filter(write_config, tmp_path, monkeypatch):
    # An empty / whitespace-only q means "no filter": the full ordered list pages.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    client = _client(write_config, tmp_path)
    unfiltered = client.get("/api/projects/gamma/transcripts/sess1").json()
    blank = client.get("/api/projects/gamma/transcripts/sess1?q=%20%20").json()
    assert blank["total"] == unfiltered["total"] == 4


def test_transcript_search_honors_order(write_config, tmp_path, monkeypatch):
    # Search + order compose: filter the whole transcript, then order the matches.
    # The substring "is" appears in two turns (assistant "here is the plan", user
    # "my token is …"), so the filtered set is non-trivially ordered.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    client = _client(write_config, tmp_path)
    asc = client.get("/api/projects/gamma/transcripts/sess1?q=is&order=asc").json()["turns"]
    desc = client.get("/api/projects/gamma/transcripts/sess1?q=is&order=desc").json()["turns"]
    assert len(asc) == 2  # two turns contain "is"
    assert list(reversed(asc)) == desc  # asc and desc are exact reverses of the filtered set


# ----- transcript live-tail (#614 Part 2) -------------------------------

# Two complete JSONL records (each newline-terminated) for building a tail fixture.
_TAIL_TURN_1 = b'{"message": {"role": "user", "content": "first live turn"}}\n'
_TAIL_TURN_2 = b'{"message": {"role": "assistant", "content": "second live turn"}}\n'


def _make_bridge_live(client, tmp_path, session, project_name="gamma"):
    """Plant a running bridge whose session uuid maps a transcript to live (#614)."""
    from clauster.models import Attribution, WorkingSession

    client.app.state.runner._sessions = [
        WorkingSession(
            pid=4242,
            cwd=tmp_path / project_name,  # sanitizes to the same dir Claude wrote into
            kind="interactive",
            started_at=4242,
            local_uuid=session,
            attribution=Attribution.TRACKED,
        )
    ]


def test_transcript_tail_invalid_name_422(write_config, tmp_path):
    r = _client(write_config, tmp_path).get("/api/projects/bad.name/transcripts/s1/tail")
    assert r.status_code == 422


@pytest.mark.parametrize("session", ["..", "../escape", "a%2Fb", "..%2F..%2Fx"])
def test_transcript_tail_unsafe_session_404(write_config, tmp_path, monkeypatch, session):
    # The path-traversal guard fails closed at the tail boundary too (never an escape).
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    r = _client(write_config, tmp_path).get(f"/api/projects/gamma/transcripts/{session}/tail")
    assert r.status_code == 404


def test_transcript_tail_unknown_session_404(write_config, tmp_path, monkeypatch):
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts/nope/tail")
    assert r.status_code == 404


def test_transcript_tail_from_zero_all_turns_oldest_first(write_config, tmp_path, monkeypatch):
    # offset=0 -> the whole transcript in FILE order (oldest-first append order),
    # with an offset == file size to poll from next, and no reset.
    tdir = _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    f = tdir / "sess1.jsonl"
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts/sess1/tail?offset=0")
    assert r.status_code == 200
    body = r.json()
    assert [t["role"] for t in body["turns"]] == ["user", "assistant", "user", "assistant"]
    assert body["offset"] == f.stat().st_size
    assert body["reset"] is False
    assert {"role", "content", "model", "timestamp"} == set(body["turns"][0])


def test_transcript_tail_appends_new_turns_after_offset(write_config, tmp_path, monkeypatch):
    # The core tail contract: read from the prior offset and get ONLY the turns
    # appended since, with the offset advanced past them.
    tdir = _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    f = tdir / "sess1.jsonl"
    client = _client(write_config, tmp_path)
    first = client.get("/api/projects/gamma/transcripts/sess1/tail?offset=0").json()
    # Append a brand-new turn to the live transcript, then poll from the prior offset.
    with f.open("ab") as fh:
        fh.write(_TAIL_TURN_1)
    nxt = client.get(f"/api/projects/gamma/transcripts/sess1/tail?offset={first['offset']}").json()
    assert [t["role"] for t in nxt["turns"]] == ["user"]
    assert nxt["turns"][0]["content"] == "first live turn"
    assert nxt["offset"] == f.stat().st_size
    assert nxt["reset"] is False


def test_transcript_tail_eof_no_new_data_is_empty(write_config, tmp_path, monkeypatch):
    # Polling at EOF (offset == size) returns no turns and the same offset — the
    # steady-state "nothing new yet" poll while the agent is idle.
    tdir = _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    size = (tdir / "sess1.jsonl").stat().st_size
    body = (
        _client(write_config, tmp_path)
        .get(f"/api/projects/gamma/transcripts/sess1/tail?offset={size}")
        .json()
    )
    assert body["turns"] == []
    assert body["offset"] == size
    assert body["reset"] is False


def test_transcript_tail_partial_trailing_line_not_consumed(write_config, tmp_path, monkeypatch):
    # A half-written final record (no trailing newline) is NOT parsed or consumed: the
    # offset stops at the last complete line so the partial reparses once it's finished.
    tdir = _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    f = tdir / "sess1.jsonl"
    client = _client(write_config, tmp_path)
    base = client.get("/api/projects/gamma/transcripts/sess1/tail?offset=0").json()["offset"]
    with f.open("ab") as fh:
        fh.write(b'{"message": {"role": "user", "content": "half')  # no newline yet
    mid = client.get(f"/api/projects/gamma/transcripts/sess1/tail?offset={base}").json()
    assert mid["turns"] == []  # the partial line is not surfaced as a turn
    assert mid["offset"] == base  # and the offset did not advance past it
    # Finish the record; the next poll picks it up exactly once.
    with f.open("ab") as fh:
        fh.write(b' done"}}\n')
    done = client.get(f"/api/projects/gamma/transcripts/sess1/tail?offset={mid['offset']}").json()
    assert [t["content"] for t in done["turns"]] == ["half done"]


def test_transcript_tail_rotated_file_resets_to_zero(write_config, tmp_path, monkeypatch):
    # A truncated/rotated transcript (now shorter than our offset) signals reset and
    # re-reads from byte 0 so the client replaces its buffer instead of appending.
    tdir = _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    f = tdir / "sess1.jsonl"
    client = _client(write_config, tmp_path)
    old_offset = client.get("/api/projects/gamma/transcripts/sess1/tail?offset=0").json()["offset"]
    # Replace the file with a single, shorter record (simulating rotation/truncation).
    f.write_bytes(_TAIL_TURN_1)
    body = client.get(f"/api/projects/gamma/transcripts/sess1/tail?offset={old_offset}").json()
    assert body["reset"] is True
    assert [t["content"] for t in body["turns"]] == ["first live turn"]
    assert body["offset"] == f.stat().st_size


def test_transcript_tail_redacts_planted_secret(write_config, tmp_path, monkeypatch):
    # THE security gate at the tail boundary: a planted session id / sk- key / AKIA id
    # in newly-appended transcript text must never reach the HTTP response.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts/sess1/tail?offset=0")
    assert r.status_code == 200
    assert "DEADBEEF012345" not in r.text
    assert "sk-ABCDEF0123456789ghij" not in r.text
    assert "AKIAIOSFODNN7EXAMPLE" not in r.text
    assert "<redacted>" in r.text


def test_transcript_tail_negative_offset_clamped_to_zero(write_config, tmp_path, monkeypatch):
    # A negative offset can't seek before the file; it clamps to 0 and is not a reset.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    body = (
        _client(write_config, tmp_path)
        .get("/api/projects/gamma/transcripts/sess1/tail?offset=-5")
        .json()
    )
    assert len(body["turns"]) == 4
    assert body["reset"] is False


def test_transcript_tail_live_true_for_running_session(write_config, tmp_path, monkeypatch):
    # The `live` flag tracks the same running-bridge mapping as the list route (#614).
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    client = _client(write_config, tmp_path)
    _make_bridge_live(client, tmp_path, "sess1")
    body = client.get("/api/projects/gamma/transcripts/sess1/tail?offset=0").json()
    assert body["live"] is True


def test_transcript_tail_live_false_when_not_running(write_config, tmp_path, monkeypatch):
    # No running session -> live is False, so the front-end stops polling after this drain.
    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])
    body = (
        _client(write_config, tmp_path)
        .get("/api/projects/gamma/transcripts/sess1/tail?offset=0")
        .json()
    )
    assert body["live"] is False


def test_transcript_tail_read_error_503_no_path_leak(write_config, tmp_path, monkeypatch):
    # An OSError reading the transcript degrades to a defined 503 with no on-disk path.
    from clauster import usage as usage_mod

    _plant_transcripts(monkeypatch, tmp_path, sessions=["sess1"])

    def _boom(*a, **k):
        raise OSError("[Errno 13] Permission denied: '/home/secret/projects/gamma'")

    monkeypatch.setattr(usage_mod, "read_transcript_turns_from_offset", _boom)
    r = _client(write_config, tmp_path).get("/api/projects/gamma/transcripts/sess1/tail?offset=0")
    assert r.status_code == 503
    assert r.json()["detail"] == "could not read transcript"
    assert "/home/secret" not in r.text and "Errno" not in r.text


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


# a live bridge (kept), two ghosts, and the cloud Default (never reaped).
# Dirs are RELATIVE and get anchored under the app's projects_root: since #1100 the
# reaper only attributes environments inside its own tree, so an absolute "/ghost/one"
# would be skipped as unattributable and these tests would pass vacuously.
_ENVS = [
    ("env_live", "bridge", "live/dir", "live"),
    ("env_ghost1", "bridge", "ghost/one", "ghost-one"),
    ("env_ghost2", "bridge", "ghost/two", "ghost-two"),
    ("env_cloud", "cloud", None, "Default"),
]


def _live_dir(projects_root) -> str:
    """The live bridge's cwd, anchored in projects_root (matches ``_ENVS``)."""
    return str(Path(projects_root) / "live/dir")


def _make_envs(projects_root):
    return [_env(i, t, str(Path(projects_root) / d) if d else None, n) for i, t, d, n in _ENVS]


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


def test_reaper_panel_uses_plain_copy(write_config, tmp_path):
    # UX-09: the maintenance panel is de-jargoned — plain "Clean up leftover environments" /
    # "Permanently delete" copy, not the old "Reap ghost environments" / "force-delete" jargon.
    page = _reaper_client(write_config, tmp_path).get("/").text
    assert "Clean up leftover environments" in page
    assert "Permanently delete" in page
    assert "Reap ghost environments" not in page
    assert "force-delete (irreversible)" not in page


def test_reaper_preview_lists_only_ghosts(write_config, tmp_path, projects_root, monkeypatch):
    _setup_reaper(monkeypatch, _make_envs(projects_root), {_live_dir(projects_root)}, [])
    body = _reaper_client(write_config, tmp_path).get("/api/environments/ghosts").json()
    assert body["enabled"] is True
    assert body["total"] == 4 and body["live_dirs"] == 1
    ids = {g["id"] for g in body["ghosts"]}
    assert ids == {"env_ghost1", "env_ghost2"}  # cloud + live excluded


def test_reaper_archive_acts_only_on_ghosts(write_config, tmp_path, projects_root, monkeypatch):
    sink = []
    _setup_reaper(monkeypatch, _make_envs(projects_root), {_live_dir(projects_root)}, sink)
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


def test_reaper_delete_requires_typed_confirm(write_config, tmp_path, projects_root, monkeypatch):
    sink = []
    _setup_reaper(monkeypatch, _make_envs(projects_root), {_live_dir(projects_root)}, sink)
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


def test_reaper_validation_errors(write_config, tmp_path, projects_root, monkeypatch):
    _setup_reaper(monkeypatch, _make_envs(projects_root), {_live_dir(projects_root)}, [])
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


def test_reaper_per_env_error_is_reported_not_fatal(
    write_config, tmp_path, projects_root, monkeypatch
):
    from clauster import environments as envmod

    monkeypatch.setattr(
        envmod,
        "load_credentials",
        lambda **k: envmod.Credentials(access_token="t", organization_uuid="o"),
    )
    monkeypatch.setattr(
        envmod, "live_bridge_directories", lambda *a, **k: {_live_dir(projects_root)}
    )

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def list_environments(self, **k):
            return _make_envs(projects_root)

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


def test_reaper_fails_closed_on_live_set_failure(
    write_config, tmp_path, projects_root, monkeypatch
):
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
            return _make_envs(projects_root)

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


def test_dashboard_injects_browser_notifications_off_by_default(write_config, tmp_path):
    html = _client(write_config, tmp_path).get("/").text
    assert "const BROWSER_NOTIFICATIONS_ENABLED = false;" in html


def test_dashboard_injects_browser_notifications_when_enabled(write_config, tmp_path):
    extra = "notifications:\n  browser_enabled: true\n  notify_on_ready: true\n"
    html = _client_with(write_config, tmp_path, extra).get("/").text
    assert "const BROWSER_NOTIFICATIONS_ENABLED = true;" in html
    # The per-event map carries the toggles the client honours on a transition.
    assert "ready: true" in html
    assert "crash: true" in html  # default-ON
    assert "stop: false" in html  # default-OFF


def test_notify_on_transitions_skips_first_observed_state(write_config, tmp_path):
    # #636 (P1): instances starts as {} on (re)load, so without a guard every bridge in the
    # first poll looks like a fresh transition and fires a spurious notification (e.g. a
    # still-crashed bridge with notify_on_crash ON re-notifies on every dashboard open). The
    # fix skips the first observed state — the empty-prev guard must run BEFORE the loop that
    # diffs the maps. No JS engine ships in CI, so we pin the fix in the rendered source.
    extra = "notifications:\n  browser_enabled: true\n"
    html = _client_with(write_config, tmp_path, extra).get("/").text
    body = html.split("notifyOnTransitions(prev, next) {", 1)[1].split("},", 1)[0]
    guard = "if (Object.keys(prev).length === 0) return;"
    assert guard in body, "first-load guard missing from notifyOnTransitions"
    # The guard must precede the per-bridge transition loop, else the storm still fires.
    assert body.index(guard) < body.index("for (const name of Object.keys(next))")


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


# ----- busy-flag reset in `finally` (401-redirect wedge, #401) ------
# Each async action handler sets a busy flag, then on a 401 does
# `window.location.assign(ROOT + "/login"); return;`. A plain post-try reset is skipped
# by that 401 `return`, so the control wedges (button stuck disabled / input stuck) when
# the login redirect is slow or blocked. The reset must live in a `finally` so it runs on
# every path including the 401 early-return — the same pattern as resumeAgent (#336).


def test_dashboard_busy_flags_reset_in_finally(write_config, tmp_path):
    html = _client_with(write_config, tmp_path, "claustrum:\n  enabled: true\n").get("/").text
    # Each flag's reset must appear ONLY inside a `finally` (count == finally-count == N),
    # so a 401 early-return can't strand the control. dispatchAgent + startHosted share
    # `f.busy`; stopHosted + killHosted share `hostedStopping[id]` — hence the 2s.
    assert html.count("f.busy = false;") == html.count("finally { f.busy = false; }") == 2
    assert (
        html.count("this.hostedStopping[id] = false;")
        == html.count("finally { this.hostedStopping[id] = false; }")
        == 2
    )
    assert (
        html.count("this.agentStopping[j.id] = false;")
        == html.count("finally { this.agentStopping[j.id] = false; }")
        == 1
    )
    assert (
        html.count("this.hostedResuming[id] = false;")
        == html.count("finally { this.hostedResuming[id] = false; }")
        == 1
    )
    assert html.count("v.sending = false;") == html.count("finally { v.sending = false; }") == 1


# ----- Forget buttons (drop a stopped session from Recent/resumable) ------


def test_dashboard_renders_bridge_forget_button(write_config, tmp_path):
    html = _client(write_config, tmp_path).get("/").text
    assert '@click="forget(i.rk)"' in html  # bridge Forget in Recent/resumable
    # Coerced so the busy-state binding isn't stuck-disabled on first paint; keyed
    # by rk (= the instance_id _forget spins on) so the spinner tracks THIS row (#779).
    assert ':disabled="!!forgetting[i.rk]"' in html


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


def _cache_sample(client, project: str, sample: dict) -> str:
    """Register an instance for ``project``, cache ``sample`` under its id, return that id.

    The cache is keyed by instance_id (#778), so a seeded sample must belong to a
    registered instance to be visible through either public reader.
    """
    from clauster.models import RemoteControlInstance

    inst = RemoteControlInstance(project=project, label=project)
    runner = client.app.state.runner
    runner._instances[inst.instance_id] = inst
    runner._metrics_cache[inst.instance_id] = sample
    return inst.instance_id


def test_metrics_running_bridge_returns_sample(write_config, tmp_path):
    # The endpoint now serves the runner's cached snapshot (#354) — O(1), no sampling.
    client = _client(write_config, tmp_path)
    _cache_sample(client, "alpha", {"cpu_percent": 3.0, "rss_bytes": 4096, "procs": 2})
    body = client.get("/api/projects/alpha/metrics").json()
    assert body == {
        "running": True,
        "cpu_percent": 3.0,
        "rss_bytes": 4096,
        "procs": 2,
        "bridges": 1,
    }


def test_metrics_batch_returns_all_cached(write_config, tmp_path):
    # The batch endpoint (#354) returns every cached bridge in one O(1) read, keyed by
    # instance_id (#1090) so a row can look up its OWN bridge.
    client = _client(write_config, tmp_path)
    alpha = _cache_sample(client, "alpha", {"cpu_percent": 1.0, "rss_bytes": 10})
    beta = _cache_sample(client, "beta", {"cpu_percent": 2.0, "rss_bytes": 20})
    body = client.get("/api/metrics").json()
    assert body[alpha] == {"running": True, "cpu_percent": 1.0, "rss_bytes": 10}
    assert body[beta]["cpu_percent"] == 2.0
    assert "alpha" not in body  # project names are no longer keys


def test_metrics_batch_keeps_bridges_of_one_project_apart(write_config, tmp_path):
    # #1090: two bridges of ONE project each report their own figure. Folded per project
    # the Server-Mode row wore the Interactive Session's CPU/RAM as its own.
    client = _client(write_config, tmp_path)
    disk = {"disk_read_bps": None, "disk_write_bps": None}
    one = {"cpu_percent": 1.0, "rss_bytes": 10, "procs": 1, **disk}
    two = {"cpu_percent": 8.0, "rss_bytes": 90, "procs": 3, **disk}
    server = _cache_sample(client, "alpha", dict(one))
    interactive = _cache_sample(client, "alpha", dict(two))
    body = client.get("/api/metrics").json()
    assert body[server] == {"running": True, **one}
    assert body[interactive] == {"running": True, **two}
    # The project total stays available on the per-project endpoint.
    assert client.get("/api/projects/alpha/metrics").json() == {
        "running": True,
        "cpu_percent": 9.0,
        "rss_bytes": 100,
        "procs": 4,
        "bridges": 2,
        **disk,
    }


def test_metrics_batch_empty_when_disabled(write_config, tmp_path):
    client = _client_with(write_config, tmp_path, "metrics:\n  enabled: false\n")
    client.app.state.runner._metrics_cache["alpha"] = {"cpu_percent": 1.0, "rss_bytes": 10}
    assert client.get("/api/metrics").json() == {}


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


# Sampling-guard behavior (sampler error, PID reuse, dead PID) now lives in the runner's
# metrics-cache refresh — see tests/test_metrics_cache.py. The endpoint just reads the cache.


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


def test_prometheus_exposes_per_bridge_cpu_rss(write_config, tmp_path):
    # /metrics reads the runner's metrics cache (#354) — no per-scrape sampling.
    client = _client_with(write_config, tmp_path, "observability:\n  prometheus_enabled: true\n")
    _cache_sample(client, "alpha", {"cpu_percent": 7.5, "rss_bytes": 2048})
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


def test_prometheus_no_cpu_rss_when_cache_empty(write_config, tmp_path):
    # With nothing in the metrics cache (e.g. metrics disabled → task never runs), the
    # scrape emits no per-bridge cpu/rss series.
    client = _client_with(write_config, tmp_path, "observability:\n  prometheus_enabled: true\n")
    assert "clauster_bridge_cpu_percent" not in client.get("/metrics").text


# The per-bridge sampling guards (error / PID reuse / dead PID / wholesale replace) are
# exercised at the runner-cache layer — see tests/test_metrics_cache.py.


def test_metrics_token_grants_scrape_without_session(runner_config):
    # With auth on and a metrics_token_hash set (#473), the matching raw Bearer token
    # reaches /metrics with no session; a wrong/absent token is rejected; the existing
    # gauges are unchanged. Only the hash is stored at rest (parity with the API token).
    from clauster.app import create_app
    from clauster.auth import hash_token
    from clauster.runner import SessionRunner

    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.password_required = True
    config.observability.prometheus_enabled = True
    config.observability.metrics_token_hash = hash_token("scrape-me")  # noqa: S106 — test token
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


def test_metrics_token_non_ascii_bearer_denied_not_500(runner_config):
    # Item-1 (#408): a non-ASCII (>U+00FF) Bearer once raised TypeError in
    # hmac.compare_digest(str, str) → a 500. Hashing the presented token before the
    # constant-time compare (verify_token, #473) makes it a clean denial
    # (401/redirect), never a server error, and never reveals the payload.
    from clauster.app import create_app
    from clauster.auth import hash_token
    from clauster.runner import SessionRunner

    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.password_required = True
    config.observability.prometheus_enabled = True
    config.observability.metrics_token_hash = hash_token(  # noqa: S106 — test token
        "scrape-me-please"
    )
    client = TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))

    # A bearer with a char above U+00FF (would crash str-vs-str compare_digest).
    # httpx refuses to ASCII-encode a non-ASCII header, so send raw UTF-8 bytes —
    # that is exactly what a hostile client would put on the wire.
    resp = client.get(
        "/metrics",
        headers={"authorization": "Bearer tökéŁ€".encode()},
        follow_redirects=False,
    )
    assert resp.status_code in {302, 303, 307, 401, 403}  # denied, NOT 500
    assert "clauster_build_info" not in resp.text  # payload withheld


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


# ----- session QR deep link (error branches; happy path is e2e) ---------


def test_qr_unknown_instance_404(write_config, tmp_path):
    # No managed instance by that id -> 404 (only the e2e happy path was covered).
    r = _client(write_config, tmp_path).get("/api/instances/ghost/qr")
    assert r.status_code == 404
    assert r.json()["detail"] == "no such instance: ghost"


def test_route_resolves_a_unique_id_prefix(write_config, tmp_path):
    # #1099: the dashboard and CLI both show truncated ids; the routes now accept them.
    from clauster.models import InstanceStatus, RemoteControlInstance

    client = _client(write_config, tmp_path)
    iid = "f2c456fd-1111-2222-3333-444444444444"
    client.app.state.runner._instances[iid] = RemoteControlInstance(
        instance_id=iid, project="alpha", label="alpha", status=InstanceStatus.RUNNING
    )
    r = client.get("/api/instances/f2c456fd")
    assert r.status_code == 200
    assert r.json()["instance_id"] == iid


def test_route_refuses_an_ambiguous_id_prefix_with_409(write_config, tmp_path):
    # Ambiguity is a DIFFERENT answer from "unknown", and the operator can only act on
    # it if told the candidates — a bare 404 would read as "that bridge is gone".
    # 409 regresses nothing: prefixes never resolved before, so this input used to 404.
    from clauster.models import InstanceStatus, RemoteControlInstance

    client = _client(write_config, tmp_path)
    a = "f2c456fd-aaaa-0000-0000-000000000000"
    b = "f2c456fd-bbbb-0000-0000-000000000000"
    for iid in (a, b):
        client.app.state.runner._instances[iid] = RemoteControlInstance(
            instance_id=iid, project="alpha", label="alpha", status=InstanceStatus.RUNNING
        )
    r = client.delete("/api/instances/f2c456fd")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert a in detail and b in detail, "the candidate ids must be named, not just refused"
    # And the bridges are untouched — refusing must never stop one of them.
    assert client.app.state.runner.get_instance(a) is not None
    assert client.app.state.runner.get_instance(b) is not None


def test_route_refuses_an_ambiguous_project_name_with_409(write_config, tmp_path):
    # #1150. A bare project name matching several instances refuses exactly like an
    # ambiguous prefix — but the hint differs: the candidate ids do NOT prefix the name,
    # so the operator can't "use more characters" and is told to pass an instance id.
    # (One start → stop → start cycle reaches this two-row state.)
    from clauster.models import InstanceStatus, RemoteControlInstance

    client = _client(write_config, tmp_path)
    a = "f2c456fd-aaaa-0000-0000-000000000000"
    b = "a1b2c3d4-bbbb-0000-0000-000000000000"
    for iid, status in ((a, InstanceStatus.RUNNING), (b, InstanceStatus.STOPPED)):
        client.app.state.runner._instances[iid] = RemoteControlInstance(
            instance_id=iid, project="alpha", label="alpha", status=status
        )
    r = client.delete("/api/instances/alpha")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert a in detail and b in detail, "the candidate ids must be named, not just refused"
    assert "use an instance id directly" in detail, "project-name hint, not the prefix one"
    assert "use more characters" not in detail
    # Both bridges untouched — refusing must never act on one.
    assert client.app.state.runner.get_instance(a) is not None
    assert client.app.state.runner.get_instance(b) is not None


def test_forget_refuses_an_ambiguous_id_prefix_with_409(write_config, tmp_path):
    # forget falls back to the raw identity (`resolve_bridge_id(...) or instance_id`) so a
    # purely-persisted record still reaches runner.forget's own lookup. Without an explicit
    # guard the ambiguous prefix rides that fallback through and comes back as a bare 404,
    # for an id that names several REAL bridges (#1099).
    from clauster.models import InstanceStatus, RemoteControlInstance

    client = _client(write_config, tmp_path)
    a = "f2c456fd-aaaa-0000-0000-000000000000"
    b = "f2c456fd-bbbb-0000-0000-000000000000"
    for iid in (a, b):
        client.app.state.runner._instances[iid] = RemoteControlInstance(
            instance_id=iid, project="alpha", label="alpha", status=InstanceStatus.STOPPED
        )
    r = client.post("/api/instances/f2c456fd/forget")
    assert r.status_code == 409
    assert a in r.json()["detail"] and b in r.json()["detail"]
    # Neither record was forgotten by the refusal.
    assert client.app.state.runner.get_instance(a) is not None
    assert client.app.state.runner.get_instance(b) is not None


def test_qr_no_session_url_409(write_config, tmp_path):
    # A registered instance that has neither a session_url (no starter_session_id)
    # nor a url yet -> nothing to encode -> 409, not a broken/empty QR.
    from clauster.models import RemoteControlInstance

    client = _client(write_config, tmp_path)
    inst = RemoteControlInstance(project="alpha", label="alpha")
    assert inst.session_url is None and inst.url is None  # precondition for the branch
    client.app.state.runner._instances["alpha"] = inst
    r = client.get("/api/instances/alpha/qr")
    assert r.status_code == 409
    assert r.json()["detail"] == "no session URL available yet"


def test_qr_renders_svg_when_url_present(write_config, tmp_path):
    # Sanity-pin the success path at the route layer too: a `url` (the secondary
    # deep link) is enough to encode even without a starter session.
    from clauster.models import RemoteControlInstance

    client = _client(write_config, tmp_path)
    inst = RemoteControlInstance(
        project="alpha", label="alpha", url="https://claude.ai/code?environment=env_X"
    )
    client.app.state.runner._instances["alpha"] = inst
    r = client.get("/api/instances/alpha/qr")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.content.startswith(b"<?xml") or b"<svg" in r.content


# ----- per-project usage read-error branch (degrade, never bare 500) ----


def test_project_usage_read_error_503(write_config, tmp_path, monkeypatch):
    # The rollup walks on-disk transcripts; an OSError mid-walk must surface as a
    # defined 503 ("could not read usage transcripts"), never an unhandled 500.
    from clauster import usage as usage_mod

    def _boom(*a, **k):
        # Carry an absolute-path-shaped message so the leak guard is exercised:
        # the path must be logged server-side, never echoed in the response.
        raise OSError("[Errno 13] Permission denied: '/home/secret/projects/alpha'")

    monkeypatch.setattr(usage_mod, "aggregate_project_usage", _boom)
    r = _client(write_config, tmp_path).get("/api/projects/alpha/usage")
    assert r.status_code == 503
    # Only the static prefix — no path, errno, or raw OSError text in the body.
    assert r.json()["detail"] == "could not read usage transcripts"
    assert "/home/secret" not in r.text and "Errno" not in r.text


# ----- resume vanished-mid-op (gone-race) -------------------------------


def test_resume_instance_vanishes_mid_op_404(write_config, tmp_path, monkeypatch):
    # Distinct from the synchronous unknown-id 404: here the route fetches a real
    # (non-hosted) instance, but the row vanishes before/while the runner resumes
    # it (concurrent stop/forget). runner.resume raises UnknownProject, which
    # _spawn_or_http maps to a defined 404 rather than letting it leak as a 500.
    # Patches resume_detailed: that is the seam the route drives since #1145.
    from clauster.models import RemoteControlInstance
    from clauster.runner import UnknownProject

    client = _client(write_config, tmp_path)
    client.app.state.runner._instances["alpha"] = RemoteControlInstance(
        project="alpha", label="alpha"
    )

    async def _gone(_name):
        raise UnknownProject("no managed instance to resume: 'alpha'")

    monkeypatch.setattr(client.app.state.runner, "resume_detailed", _gone)
    r = client.post("/api/instances/alpha/resume")
    assert r.status_code == 404
    assert "no managed instance to resume" in r.json()["detail"]


# ----- clone cancel (#573) ----------------------------------------------


def test_clone_cancel_unknown_job_404(write_config, tmp_path):
    # No job with this id was ever created -> the endpoint maps the miss to a 404.
    r = _client(write_config, tmp_path).post("/api/projects/clone/nope/cancel")
    assert r.status_code == 404
    assert "unknown or expired" in r.json()["detail"]


def test_clone_cancel_terminal_job_409(write_config, tmp_path):
    # A job that already finished is not cancellable: request_cancel returns False and
    # the endpoint maps that to a 409 naming the terminal status.
    client = _client(write_config, tmp_path)
    job = client.app.state.clone_jobs.create("doomed")
    client.app.state.clone_jobs.finish(job)  # -> "done" (terminal)
    r = client.post(f"/api/projects/clone/{job.id}/cancel")
    assert r.status_code == 409
    assert "already done" in r.json()["detail"]


def test_clone_cancel_running_job_202(write_config, tmp_path):
    # A still-running job is cancellable: the endpoint returns 202, flags the job, and
    # fires its registered terminate hook (the git subprocess's terminate).
    client = _client(write_config, tmp_path)
    job = client.app.state.clone_jobs.create("live")
    terminated: list[bool] = []
    job.register_terminate(lambda: terminated.append(True))
    r = client.post(f"/api/projects/clone/{job.id}/cancel")
    assert r.status_code == 202
    assert r.json() == {"job_id": job.id, "cancelling": True}
    assert job.cancel_requested is True
    assert terminated == [True]  # the terminate hook actually fired


# ----- clone cancel UI affordance (#659 items 1+2) ----------------------


def test_dashboard_renders_clone_cancel_button(write_config, tmp_path):
    # Item 1: a dedicated Cancel control next to Clone, shown only while a clone is
    # in its cancellable in-progress state (np.cloning) and wired to cancelClone().
    html = _client(write_config, tmp_path).get("/").text
    assert "cancelClone()" in html
    assert 'x-show="np.cloning"' in html  # only visible during an in-progress clone
    assert "Cancel clone" in html


def test_dashboard_clone_cancel_is_single_flight(write_config, tmp_path):
    # Item 1: cancelClone() guards on np.cloning so a second click (after the first
    # already cleared np.cloning + nulled _abortClone) no-ops rather than re-POSTing
    # cancel against a job already torn down.
    html = _client(write_config, tmp_path).get("/").text
    assert "if (!np.cloning) return;" in html  # the single-flight guard
    assert "this._abortClone = null;" in html  # second click finds no abort hook


def test_dashboard_clone_cancel_confirms_with_toast(write_config, tmp_path):
    # Item 2: cancelClone surfaces a confirmation toast via the existing toast()
    # mechanism so the operator sees the clone was actually cancelled.
    html = _client(write_config, tmp_path).get("/").text
    assert 'this.toast("Clone cancelled", "info")' in html


def test_dashboard_clone_cancel_bumps_op_to_neutralize_inflight(write_config, tmp_path):
    # cancelClone bumps the per-submit op id (like resetNewProject) so a Cancel that lands
    # while the clone POST is still in flight staleifies that attempt — the resolving POST
    # then can't install an orphan watch that would run the clone to completion invisibly.
    html = _client(write_config, tmp_path).get("/").text
    # Slice the method body: from its definition up to the confirmation toast that ends it.
    body = html.split("cancelClone() {", 1)[1].split('this.toast("Clone cancelled"', 1)[0]
    assert "this._nextNpOp();" in body


def test_resume_failure_fires_reconnect_failed_notification(write_config, tmp_path, monkeypatch):
    # #541: a bridge resume that fails (here SpawnError -> 409) fires the
    # reconnect-failed notification, then still re-raises the mapped HTTP error.
    from clauster.models import RemoteControlInstance
    from clauster.runner import SpawnError

    client = _client_with(
        write_config,
        tmp_path,
        "notifications:\n  enabled: true\n  urls:\n    - 'slack://x'\n"
        "  notify_on_reconnect_failed: true\n",
    )
    runner = client.app.state.runner
    runner._instances["alpha"] = RemoteControlInstance(project="alpha", label="alpha")
    notifier = _RecordingNotifier()
    runner._notifier = notifier

    async def _boom(_name):
        raise SpawnError("bridge would not come back up")

    monkeypatch.setattr(runner, "resume_detailed", _boom)
    r = client.post("/api/instances/alpha/resume")
    assert r.status_code == 409  # the mapped HTTP error is unchanged
    assert len(notifier.calls) == 1
    title, _body = notifier.calls[0]
    assert "reconnect failed" in title.lower()


def test_resume_failure_no_notification_when_toggle_off(write_config, tmp_path, monkeypatch):
    # The notification stays silent when notify_on_reconnect_failed is off (default).
    from clauster.models import RemoteControlInstance
    from clauster.runner import SpawnError

    client = _client_with(
        write_config,
        tmp_path,
        "notifications:\n  enabled: true\n  urls:\n    - 'slack://x'\n",
    )
    runner = client.app.state.runner
    runner._instances["alpha"] = RemoteControlInstance(project="alpha", label="alpha")
    notifier = _RecordingNotifier()
    runner._notifier = notifier

    async def _boom(_name):
        raise SpawnError("nope")

    monkeypatch.setattr(runner, "resume_detailed", _boom)
    assert client.post("/api/instances/alpha/resume").status_code == 409
    assert notifier.calls == []


def test_resume_non_spawn_failure_does_not_notify(write_config, tmp_path, monkeypatch):
    # #652: a resume that fails for a precondition reason — here the instance vanished
    # mid-op (UnknownProject -> 404) — is NOT a failed reconnect, so it must stay silent
    # even with notify_on_reconnect_failed ON. Only a SpawnError (409) fires the notice.
    from clauster.models import RemoteControlInstance
    from clauster.runner import UnknownProject

    client = _client_with(
        write_config,
        tmp_path,
        "notifications:\n  enabled: true\n  urls:\n    - 'slack://x'\n"
        "  notify_on_reconnect_failed: true\n",
    )
    runner = client.app.state.runner
    runner._instances["alpha"] = RemoteControlInstance(project="alpha", label="alpha")
    notifier = _RecordingNotifier()
    runner._notifier = notifier

    async def _gone(_name):
        raise UnknownProject("no managed instance to resume: 'alpha'")

    monkeypatch.setattr(runner, "resume_detailed", _gone)
    assert client.post("/api/instances/alpha/resume").status_code == 404
    assert notifier.calls == []


# ----- audited coverage gaps (2026-07 audit) ----------------------------


def test_create_project_missing_after_provision_500(write_config, tmp_path, monkeypatch):
    # app.py 1615->1618: provisioning "succeeded" but the project isn't discoverable
    # afterwards (dir vanished / never landed) — must surface as an explicit 500,
    # never a silent success with a phantom Project body.
    monkeypatch.setattr("clauster.app.create_project", lambda *a, **k: None)
    with _client(write_config, tmp_path) as client:
        r = client.post("/api/projects", json={"name": "phantom"})
    assert r.status_code == 500
    assert "missing after provisioning" in r.json()["detail"]


def test_stop_unknown_project_race_maps_404(runner_config, monkeypatch):
    # app.py 2377-2378: resolve_bridge_id found the instance but stop() raced a
    # concurrent forget/removal (UnknownProject) — the route reports 404, not a 500.
    from clauster.models import InstanceStatus, RemoteControlInstance
    from clauster.runner import UnknownProject

    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.RUNNING)
    runner._instances[inst.instance_id] = inst

    async def _gone(instance_id: str):
        raise UnknownProject("alpha vanished mid-stop")

    monkeypatch.setattr(runner, "stop", _gone)
    with TestClient(create_app(config, runner=runner)) as client:
        r = client.delete(f"/api/instances/{inst.instance_id}")
    assert r.status_code == 404
    assert "vanished" in r.json()["detail"]
