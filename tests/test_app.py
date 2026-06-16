from __future__ import annotations

import re
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


def test_dashboard_active_zone_precedes_projects_in_dom(write_config):
    # The Active-sessions zone is pinned above Projects, but it must achieve that by DOM
    # source order — NOT a CSS `order` hack — so keyboard / screen-reader focus order matches
    # the visual order (WCAG 2.4.3). Regression guard: a redesign once used `order: -1`, which
    # inverted the tab sequence (Projects' ~25 controls came first). Assert the source order
    # and that no `order:-1` inversion is reintroduced.
    page = _client(write_config).get("/").text
    # Match the class TOKEN, not an exact class string, so adding another class to either
    # section can't break the order check (CodeRabbit hardening).
    active = re.search(r'class="[^"]*\bzone-active\b[^"]*"', page)
    projects = re.search(r'class="[^"]*\bzone-projects\b[^"]*"', page)
    assert active is not None and projects is not None
    assert active.start() < projects.start()
    # Whitespace-tolerant: `order : -1` would also reintroduce the inversion (CodeRabbit).
    assert re.search(r"\border\s*:\s*-1\b", page) is None


def test_dashboard_log_ws_and_refresh_robustness(write_config):
    # Audit fixes (frontend, no JS unit harness — guard the wiring by presence):
    #  FIX 3 — the bridge-log WebSocket sets onclose/onerror so a dropped socket flips a
    #          `lost` flag and the panel surfaces the disconnect instead of a frozen tail.
    #  FIX 4 — the 4s refresh poll has an in-flight guard so a slow tick can't resolve after
    #          a newer one and clobber this.instances / this.hosted with stale data.
    page = _client(write_config).get("/").text
    assert "ws.onclose = lost" in page and "ws.onerror = lost" in page  # FIX 3 handlers
    assert "Live tail disconnected" in page  # FIX 3 UI surface
    # openLogs is idempotent + state-bound: a reconnect retires the prior state and binds the
    # new socket's handlers to its own tail, token-checked against the live logs[name], so two
    # tails can't stream into one view and a retired socket can't flip the fresh panel. The
    # check compares a per-tail TOKEN, not the object reference: assigning `state` into Alpine's
    # reactive `this.logs` wraps it in a Proxy, so an object-identity check (`logs[name] !==
    # state`) is always true and drops every frame — the token survives the Proxy.
    assert "const token = ++this._logSeq;" in page
    # The retire check compares a per-tail TOKEN, not object identity. Pin it whitespace-
    # tolerantly (CodeRabbit: an exact-string pin breaks on harmless reformatting) and assert
    # liveState() RETURNS the live proxy (so handlers mutate through Alpine's reactivity and
    # each frame repaints immediately) rather than the raw `state`, whose mutations bypass the
    # Proxy set-traps and only surface on the next reactive flush.
    assert re.search(
        r"return\s+s\s*&&\s*s\.open\s*&&\s*s\.token\s*===\s*token\s*\?\s*s\s*:\s*null", page
    )
    assert re.search(
        r"const\s+s\s*=\s*liveState\(\)\s*;", page
    )  # onmessage uses the resolved proxy
    # Negative guard (CodeRabbit): reintroducing the object-identity comparison (always true
    # through the Proxy, which drops every frame) must FAIL this test, not survive on the
    # token substring above. The bug compared `logs[name]` to the raw `state` object.
    assert "logs[name] !== state" not in page
    assert "logs[name] === state" not in page
    # Single-flight guard that QUEUES a trailing refresh (so action-flow refreshes aren't
    # dropped), plus the reset + trailing run — assert all three so a regression fails.
    assert "if (this._refreshing) { this._refreshQueued = true; return; }" in page
    assert "this._refreshing = false;" in page
    assert "this._refreshQueued = false; this.refresh();" in page


def test_dashboard_footer_credits_vendored_assets(write_config):
    # The footer must credit the bundled MIT front-end assets (Tabler + Alpine.js)
    # and link the third-party notices. This regressed once when a card redesign
    # silently replaced the attribution with a tagline (#143); this guards it.
    # Assert the visible credit (link text + phrasing), not the raw hrefs: a
    # `"https://host" in page` check trips CodeQL's url-substring rule, and the
    # anchor text is footer-specific (stray Tabler/Alpine mentions elsewhere in
    # the page — CSS link, JS comments — can't satisfy these).
    page = _client(write_config).get("/").text
    assert "Built with" in page
    assert ">Tabler</a>" in page
    assert ">Alpine.js</a>" in page
    assert "MIT licensed" in page
    assert "THIRD_PARTY_NOTICES.md" in page


def test_dashboard_ux_polish_followups(write_config):
    # Follow-up UX polish from the multi-agent review: (P2-12) untrusted projects get an
    # explicit muted shield with an aria-label — not just the absence of the trusted one, so
    # screen readers can tell "untrusted" from "no data"; (P2-7) the bypass typed-confirm's
    # "Start with bypass" stays disabled until the typed name matches the project; (P2-6)
    # hosted status badges gain a dot helper so browser/desktop/detached present status the
    # same way (dot + capitalized) instead of a dotless lowercase pill.
    page = _client(write_config).get("/").text
    assert 'aria-label="Directory not yet trusted"' in page  # explicit untrusted signal
    # Assert the actual binding, not a loose token (CodeRabbit): the Start-with-bypass button
    # is gated on the typed name matching, and the hosted status-dot helper ships. Its USE in
    # the (claustrum-gated) hosted row is asserted in test_app_hosted's enabled render.
    assert ":disabled=\"(bypassTyped['alpha'] || '') !== 'alpha'\"" in page
    assert "hostedStatusDot(status) {" in page  # the helper is defined and shipped


def test_dashboard_has_readiness_panel(write_config):
    # Readiness is now a header pill (severity-aware: "blocking" vs "check(s)") that
    # opens a collapsible panel titled "Before you start a session". The pill logic
    # + the /api/doctor wiring must ship (Alpine x-show hides it until a check needs
    # attention).
    resp = _client(write_config).get("/")
    assert "Before you start a session" in resp.text  # the collapsible panel title
    assert "readinessBlockers()" in resp.text  # severity split: blocking vs warning
    assert "readinessWarnings()" in resp.text
    assert "blocking" in resp.text  # the solid-red pill copy
    assert "loadReadiness" in resp.text
    assert "/api/doctor" in resp.text


def test_dashboard_has_bg_agent_dispatch_and_stop_controls(write_config):
    # BG-4 (redesign): background agents are now launched as the "Fire-and-forget"
    # outcome in the per-project launch popover (_launchDetached -> POST /api/agents,
    # with an optional "also register on claude.ai"), and stopped via the per-row
    # Stop button (stopAgent -> DELETE /api/agents/{id}) in the unified Active list.
    page = _client(write_config).get("/").text
    assert "Fire-and-forget" in page  # the launch-mode label
    assert "_launchDetached" in page  # routes the fire-and-forget launch
    assert "also register on claude.ai" in page  # the claude.ai opt-in checkbox
    assert "stopAgent" in page and "agentStopping" in page  # per-row stop control
    assert "/api/agents" in page


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


def test_dashboard_resume_controls_render(write_config):
    # Redesign: "Start new session" is gone (launching is now "Run Claude here").
    # The Recent / resumable group still offers Resume for a terminated bridge
    # (resume(i.project) -> POST /resume). The hosted resume affordance
    # (resumeHosted) is claustrum-gated and asserted in test_app_hosted.py.
    resp = _client(write_config).get("/")
    assert resp.status_code == 200
    assert ">Resume</button>" in resp.text
    assert "resume(i.project)" in resp.text  # bridge resume affordance
    assert ">Start new session</button>" not in resp.text  # the start-new button is gone


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
    # Guards (now in the per-project row, rendered by the dashboard loop): the
    # trust-on-start confirm opens on confirmTrust; "Trust & start" stays disabled
    # until the "I trust…" checkbox is ticked; a trusted dir shows a green shield by
    # its name (no prompt needed).
    txt = _client(write_config).get("/").text
    assert "x-show=\"confirmTrust['alpha']\"" in txt  # the trust-on-start confirm
    assert ':disabled="!trustConfirmed[' in txt  # checkbox gates Trust & start
    assert "Trust &amp; start" in txt  # the confirm button copy
    assert "Directory trusted" in txt  # the trusted-shield tooltip


def test_dashboard_renders_resume_mode_picker(write_config):
    # Redesign: the Mode picker now lives in the launch popover's "Advanced"
    # disclosure (Desktop launch). Its JS wiring is platform-independent
    # (DEFAULT_RESUME_MODE seed + the resume_mode posted in the spawn body); the
    # <select> itself is gated on pty_supported (POSIX only).
    resp = _client(write_config).get("/")
    assert resp.status_code == 200
    assert "DEFAULT_RESUME_MODE" in resp.text
    assert "resume_mode:" in resp.text  # posted in the /api/instances body
    # A fresh start on a resumable bridge (Mode picker hidden) must keep the
    # bridge's recorded mode, not silently post the global default.
    assert "existing.resume_mode" in resp.text
    if sys.platform != "win32":
        assert "x-model=\"resumeMode['alpha']\"" in resp.text  # popover Advanced picker
        assert "pty (single-session, true-resume)" in resp.text
        assert "!== 'pty'" in resp.text  # Spawn selector gated off in pty mode
        assert 'id="resume-hint-alpha"' in resp.text  # hint element rendered
        assert 'aria-describedby="resume-hint-alpha"' in resp.text  # a11y wiring
