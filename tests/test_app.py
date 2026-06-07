from __future__ import annotations

import sys

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


def test_dashboard_footer_credits_vendored_assets(write_config):
    # The footer must credit the bundled MIT front-end assets (Tabler + Alpine.js)
    # and link the third-party notices. This regressed once when a card redesign
    # silently replaced the attribution with a tagline (#143); this guards it.
    page = _client(write_config).get("/").text
    assert "https://tabler.io" in page
    assert "https://alpinejs.dev" in page
    assert "THIRD_PARTY_NOTICES.md" in page


def test_dashboard_has_readiness_panel(write_config):
    # The preflight panel + its /api/doctor wiring are present in the page (Alpine
    # x-show hides it until a check needs attention, but the markup/JS must ship).
    resp = _client(write_config).get("/")
    assert "Before you start a bridge" in resp.text
    assert "loadReadiness" in resp.text
    assert "/api/doctor" in resp.text


def test_dashboard_has_interrupted_status_logic(write_config):
    # The "interrupted vs stopped" distinction (a bridge that ended without a Stop)
    # is derived client-side from intentional_stop; the helper + state map + the card
    # note must ship in the page.
    resp = _client(write_config).get("/")
    assert "displayStatus" in resp.text
    assert "interrupted:" in resp.text  # present in the STATUS_* maps
    assert "statusNote" in resp.text  # the recoverable-state nudge


def test_dashboard_pty_bridge_is_resumable(write_config):
    # Regression (true-resume reachability): a stopped pty bridge has no
    # environment_id (the flag form leaves no env ghost), so isResumable() must
    # also accept resume_mode === "pty". Otherwise the Resume button — the only
    # path to POST /resume -> spawn(resume=True) -> `claude --continue` — never
    # renders, and pty true-resume is unreachable from the UI (only "Start bridge"
    # shows, which is a fresh session with no --continue).
    resp = _client(write_config).get("/")
    assert resp.status_code == 200
    assert 'i.resume_mode === "pty"' in resp.text


def test_dashboard_resume_and_start_new_controls_render(write_config):
    # PR2: a resumable bridge offers a primary "Resume" plus a distinct
    # "Start new session" that goes through a warning before a fresh spawn.
    resp = _client(write_config).get("/")
    assert resp.status_code == 200
    assert ">Resume</button>" in resp.text
    assert "startNew(" in resp.text and ">Start new session</button>" in resp.text
    assert "confirmNewStart(" in resp.text  # the warned confirm path


def test_trust_on_start_replaces_trust_button(write_config):
    # Trust-on-Start: there is NO standalone "Trust directory" button. Instead Start
    # gates on trust — an untrusted dir gets a confirm dialog (with a safety checkbox)
    # that trusts then spawns, like Claude Code's "Do you trust the files in this
    # folder?". Trusted dirs skip the prompt.
    txt = _client(write_config).get("/").text
    assert "Trust directory" not in txt  # the standalone button is gone
    assert "I trust the files in this directory" in txt  # the safety checkbox
    assert "confirmTrustStart(" in txt  # the "Trust & start" handler
    assert 'this.trustState[name] !== "trusted"' in txt  # the start() trust gate


def test_trust_on_start_guards(write_config):
    # Guards: "Trust & start" is disabled until the checkbox is ticked; the Start
    # button is greyed while a trust/bypass confirm is open (so a re-click can't reset
    # the checkbox); a trusted dir shows a green shield by its name (no prompt needed).
    txt = _client(write_config).get("/").text
    assert ':disabled="!trustConfirmed[' in txt  # checkbox gates Trust & start
    assert ':disabled="confirmTrust[' in txt  # Start greyed while deciding
    assert "Directory trusted" in txt  # the trusted-shield tooltip


def test_dashboard_renders_resume_mode_picker(write_config):
    # PR3: the per-launch Mode picker is wired (DEFAULT_RESUME_MODE seed + the
    # resume_mode posted in the spawn body are platform-independent); the <select>
    # itself is gated on pty_supported (POSIX only).
    resp = _client(write_config).get("/")
    assert resp.status_code == 200
    assert "DEFAULT_RESUME_MODE" in resp.text
    assert "resume_mode:" in resp.text  # posted in the /api/instances body
    # Start-new-session on a resumable card (Mode picker hidden) must keep the
    # bridge's recorded mode, not silently post the global default.
    assert "existing.resume_mode" in resp.text
    if sys.platform != "win32":
        assert "resumeMode['" in resp.text  # the picker <select> x-model (POSIX)
        assert "pty (true-resume)" in resp.text
