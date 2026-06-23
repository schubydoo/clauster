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


def test_static_assets_carry_immutable_cache_control(write_config):
    # #353: vendored assets are cacheable forever (safe because URLs are version-busted).
    resp = _client(write_config).get("/static/alpine.min.js")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_missing_static_asset_has_no_immutable_cache(write_config):
    # Only a 200 file response gets the immutable header — a 404 must not (so a
    # mistyped/removed asset isn't cached-forever as missing).
    resp = _client(write_config).get("/static/does-not-exist.js")
    assert resp.status_code == 404
    assert "immutable" not in resp.headers.get("cache-control", "")


def test_not_modified_static_response_has_no_immutable_cache(write_config):
    # A conditional request that resolves to 304 must not carry the immutable header
    # (only 200 file responses do) — exercises the non-200 branch of get_response.
    client = _client(write_config)
    first = client.get("/static/alpine.min.js")
    assert first.status_code == 200 and "etag" in first.headers
    again = client.get("/static/alpine.min.js", headers={"if-none-match": first.headers["etag"]})
    assert again.status_code == 304
    assert "immutable" not in again.headers.get("cache-control", "")


def test_large_response_is_gzip_compressed(write_config):
    # #353: responses over the threshold are gzip-compressed for clients that accept it.
    resp = _client(write_config).get("/", headers={"accept-encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"


def test_asset_urls_are_version_busted(write_config):
    # #353: linked assets carry ?v=<app version> so an upgrade busts the immutable cache.
    from clauster import __version__

    page = _client(write_config).get("/").text
    assert f"tabler.min.css?v={__version__}" in page
    assert f"alpine.min.js?v={__version__}" in page
    assert f"favicon.svg?v={__version__}" in page


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


def test_no_xshow_element_carries_important_display_utility(write_config):
    # Class-level regression guard for the false-banner bug. Alpine's `x-show` hides an element
    # with an inline `display:none`, but Tabler's display utilities (`.d-flex`, `.d-none`,
    # `.d-grid`, …) are all `display:… !important`, which OVERRIDES that inline hide — so any
    # element carrying BOTH `x-show` and a `d-*` display class is pinned to the utility's display
    # regardless of state. Put the display utility on an inner wrapper instead. Scanning the whole
    # rendered dashboard keeps the entire class dead: this caught the live-tail "reconnecting" /
    # "disconnected" banners AND the pty-mode resume hint, all of which stayed visible wrongly.
    page = _client(write_config).get("/").text
    # Match a full opening tag, allowing quoted attribute values to contain '>' (e.g. an x-show
    # expression like `length > 6`), so the tag boundary isn't cut short mid-attribute.
    tag_re = re.compile(r"""<[a-zA-Z](?:"[^"]*"|'[^']*'|[^>"'])*>""")
    display_util = re.compile(r"\bd-(flex|inline-flex|block|inline-block|grid|none)\b")
    offenders = [
        tag.group(0)[:140]
        for tag in tag_re.finditer(page)
        if "x-show=" in tag.group(0) and display_util.search(tag.group(0))
    ]
    assert not offenders, (
        "element(s) carry both x-show and an !important `d-*` display utility, which overrides "
        "x-show's inline display:none and pins them visible regardless of state — move the "
        "display utility onto an inner wrapper:\n" + "\n".join(offenders)
    )


def test_dashboard_log_ws_and_refresh_robustness(write_config):
    # Audit fixes (frontend, no JS unit harness — guard the wiring by presence):
    #  FIX 3 — the bridge-log tail AUTO-RECONNECTS a dropped-but-live socket (mirroring the hosted
    #          live-view) instead of stranding on a manual-Reconnect "disconnected" banner. onclose
    #          gates the reconnect on the bridge still being live + the per-tail token, so a
    #          genuinely-stopped bridge stays on the disconnect banner and a retired socket can't
    #          reconnect; only a live bridge surfaces "reconnecting…" and retries.
    #  FIX 4 — the 4s refresh poll has an in-flight guard so a slow tick can't resolve after
    #          a newer one and clobber this.instances / this.hosted with stale data.
    page = _client(write_config).get("/").text
    # Liveness gate: reconnect only while the bridge is running/starting (whitespace-tolerant
    # pin so a reformat doesn't break it — CodeRabbit).
    assert re.search(
        r'\["running",\s*"starting"\]\.includes\(\s*self\.statusOf\(name\)\s*\)', page
    )
    assert re.search(
        r"s\.reconnecting\s*=\s*true", page
    )  # transient drop surfaces "reconnecting…"
    # The reconnect timer gates on the per-tail TOKEN + open (not object identity, which is
    # always-true through the Proxy) or a genuinely-dropped live tail would never re-open.
    assert re.search(r"s2\s*&&\s*s2\.token\s*===\s*token\s*&&\s*s2\.open", page)
    # Retry cap (CodeRabbit): consecutive failed reconnects are bounded by MAX_LOG_RECONNECTS so a
    # stale "running" snapshot can't loop forever; a streamed frame resets the counter.
    assert "const MAX_LOG_RECONNECTS" in page
    assert re.search(r"s\.attempts\s*>=\s*MAX_LOG_RECONNECTS", page)
    # A streamed frame resets the counter (onmessage) so a healthy tail reconnects without limit;
    # pin it so dropping the reset can't silently let a working tail cap out (@claude).
    assert re.search(r"s\.attempts\s*=\s*0\b", page)
    assert "Live tail dropped" in page  # FIX 3 reconnecting UI surface
    assert "Live tail disconnected" in page  # FIX 3 disconnect UI surface (bridge actually gone)
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


def test_dashboard_live_tail_banners_are_mutually_exclusive(write_config):
    # #498: the reconnecting/lost live-tail banners must never stack. Even though the state
    # machine sets one and clears the other on every transition, the info banner is gated on
    # `!lost` too so a missed clear (an Alpine reactivity edge, cf. #310/#315) can't render BOTH.
    page = _client(write_config).get("/").text
    assert re.search(r"logs\[i\.project\]\.reconnecting\s*&&\s*!logs\[i\.project\]\.lost", page)


def test_dashboard_disconnect_copy_is_liveness_aware(write_config):
    # #498: don't claim "the bridge may have stopped" while the snapshot still reports the
    # bridge running/starting — a live-tail WebSocket can drop while the bridge keeps running.
    # The alarming copy is gated on NOT alive; a live bridge gets the transient-drop wording.
    page = _client(write_config).get("/").text
    assert "the bridge may have stopped" in page  # only when not alive
    assert "the bridge is still running" in page  # transient tail drop, bridge alive
    # The alarming copy must be liveness-gated, not unconditional.
    assert re.search(r"!isRunning\(i\.project\)\s*&&\s*!isBusy\(i\.project\)", page)
    assert re.search(r"isRunning\(i\.project\)\s*\|\|\s*isBusy\(i\.project\)", page)


def test_dashboard_hosted_view_token_guard(write_config):
    # openHosted retires a prior socket and binds each socket's onmessage/onclose to its own
    # `view`, checked against the live hostedView[id]. That check must compare a per-view TOKEN,
    # not the object reference: assigning `view` into Alpine's reactive `this.hostedView` wraps
    # it in a Proxy, so an object-identity check (`hostedView[id] !== view`) is ALWAYS true and
    # drops every streamed frame — and silently disables the onclose reconnect/ended path. This
    # is the same Proxy footgun that bit openLogs; the token survives the Proxy.
    page = _client(write_config).get("/").text
    assert "const token = ++this._hostedSeq;" in page
    # Pin the liveness helper whitespace-tolerantly (CodeRabbit: an exact-string pin breaks on
    # harmless reformatting). liveView() must return the live PROXY (or null) — not a bool — so
    # the handlers mutate THROUGH Alpine's reactivity and the panel repaints immediately; a
    # raw-`view` mutation bypasses the Proxy set-traps and the "link dropped" banner would only
    # surface on the next reactive flush.
    assert re.search(
        r"return\s+w\s*&&\s*w\.open\s*&&\s*w\.token\s*===\s*token\s*\?\s*w\s*:\s*null", page
    )
    assert re.search(
        r"(?<![\w.])w\.connLost\s*=\s*true", page
    )  # dropped-link flag set THROUGH the proxy
    # The reconnect timer must also gate on the token, not the always-false `w === view`
    # (raw vs Proxy), or a genuinely-dropped live link would never reconnect.
    assert re.search(r"w2\s*&&\s*w2\.token\s*===\s*token\s*&&\s*w2\.open", page)
    # Negative guards: the buggy identity comparisons AND the reactivity-bypassing raw
    # mutations must be gone from the hosted path. Strip comment-only lines first — the token
    # comment legitimately *names* `this.hostedView[id] !== view` as the bug — then reject both
    # receiver forms (`this.`/`self.`) and both operators (`!==`/`===`) as executable code, so
    # the original regression can't slip back in under either spelling. Strip BOTH `/* ... */`
    # block comments and `//` comment-only lines first, so a future comment naming the buggy
    # pattern in either style can't mask a real regression (CodeRabbit).
    hosted_code = re.sub(r"/\*.*?\*/", "", page, flags=re.DOTALL)
    hosted_code = "\n".join(
        ln for ln in hosted_code.splitlines() if not ln.lstrip().startswith("//")
    )
    assert "this.hostedView[id] !== view" not in hosted_code
    assert "this.hostedView[id] === view" not in hosted_code
    assert "self.hostedView[id] !== view" not in hosted_code
    assert "self.hostedView[id] === view" not in hosted_code
    assert "if (w === view && w.open)" not in hosted_code
    assert (
        re.search(r"(?<![\w.])view\.connLost\s*=", hosted_code) is None
    )  # raw (non-reactive) write
    assert re.search(r"(?<![\w.])view\.ws\s*=\s*ws", hosted_code) is None  # ws now via the proxy


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


def test_dashboard_permission_effect_renders_inline(write_config):
    # #427 UX-01: the riskiest per-launch choice (permission mode) explained its 6
    # modes only in a hover title= — invisible on touch (phone-first product). The
    # selected mode's plain-language effect now renders inline beneath the <select>,
    # reusing the launch-mode m.sub pattern. Assert the helper ships + is bound, and
    # the select is wired to it for screen readers.
    page = _client(write_config).get("/").text
    assert "permissionEffect(mode) {" in page  # the helper is defined and shipped
    assert 'x-text="permissionEffect(lperm)"' in page  # bound to the selected mode
    assert 'aria-describedby="perm-effect-alpha"' in page  # select points at the hint
    assert 'id="perm-effect-alpha"' in page  # and the hint exists per project


def test_dashboard_explains_missing_connect_url(write_config):
    # #427 UX-02: a running desktop bridge whose session_url hasn't arrived (async
    # capture gap) — or never will (pty flag-form) — used to show 'Running' with the
    # 'Open in Claude' + QR affordances silently gone and unexplained. Now a disabled
    # placeholder fills the gap: a spinner 'Preparing connect link…' for the transient
    # case, 'No web link — use Logs' for pty. Assert the helpers ship + are bound.
    page = _client(write_config).get("/").text
    assert "connectUrlMissing(name) {" in page  # transient-or-permanent gap helper
    assert "connectUrlUnavailable(name) {" in page  # pty (no URL ever) split
    assert 'x-show="connectUrlUnavailable(i.project)"' in page  # pty placeholder gate
    assert 'x-show="connectUrlMissing(i.project) && !connectUrlUnavailable(i.project)"' in page
    assert "Preparing connect link" in page  # the transient spinner copy (visual chip)
    # The visual chips are aria-hidden; the SR announcement is a PERSISTENT aria-live
    # region (always mounted, content-toggled) — a live region shown in via x-show is
    # silently skipped by NVDA/VoiceOver (Greptile P2). Assert that wiring.
    assert "connectStatusText(name) {" in page  # the live-region text helper ships
    assert (
        '<span class="visually-hidden" aria-live="polite" x-text="connectStatusText(i.project)">'
        in page
    )
    # The announced strings mirror the visible chip labels word-for-word, so the SR and
    # sighted experiences match (pin them — they're otherwise unasserted and could drift).
    assert '"No web link — use Logs"' in page  # pty SR text == pty chip label
    assert '"Preparing connect link…"' in page  # transient SR text == spinner chip label


def test_dashboard_warns_restart_ends_live_sessions(write_config):
    # #427 UX-03: the config-save banner said 'restart Clauster to apply' with no
    # affordance and no warning that a restart reaps the cgroup and ends live pty
    # bridges + browser sessions. It now carries a 'How do I restart?' docs link and
    # a conditional 'N session(s) running — a restart will end them' line (mirroring
    # the CLAUDE.md editor's live-session caveat). Detached/external sessions are
    # separate processes, so restartImpactCount() intentionally excludes them.
    page = _client(write_config).get("/").text
    assert "restartImpactCount() {" in page  # the count helper is defined and shipped
    # Counts only "running"/"starting" sessions (not "stopping" — a bridge mid-Stop is
    # on its way out, so the "N running" copy would overstate it; Greptile P2).
    assert 'const liveStatuses = ["running", "starting"];' in page
    assert 'data-test="cfg-restart-warn"' in page  # the warning element
    assert 'x-show="restartImpactCount() > 0"' in page  # gated on live sessions
    assert "How do I restart?" in page  # the docs affordance


def test_dashboard_surfaces_crashed_instance_error_detail(write_config):
    # #313: a bridge that spawns then CRASHES has `error_detail` set (None on success), but the
    # card never rendered it — the failure reason was invisible (a silent dead card). The
    # `detailOf(name)` helper already returns the instance's error_detail; the card must both
    # GATE on it (x-show — error_detail is only set on ERROR/CRASHED, so presence == a failure
    # to surface) AND RENDER it (x-text, which auto-escapes — no XSS from the captured stderr
    # tail). This is distinct from `errorOf` (a transient ACTION error, already shown).
    page = _client(write_config).get("/").text
    # detailOf must be USED in the markup (it was defined-but-unused before), keyed per project.
    assert re.search(r"x-show=\"detailOf\(\s*'alpha'\s*\)\"", page)  # gated on the failure reason
    assert re.search(r"x-text=\"detailOf\(\s*'alpha'\s*\)\"", page)  # and actually displayed


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


def test_recent_zone_rows_decoupled_from_live_filter(write_config):
    # #421: the live-session filter chips are about LIVE/active sessions, so the Recent
    # group's ended rows must NOT be gated on activeFilter — otherwise recentCount()
    # (filter-unaware) counts a row the x-show hides ("Recent (1)" with the row invisible).
    # Active-zone rows stay filtered; only the Recent zone is decoupled.
    page = _client(write_config).get("/").text
    assert "Recent / resumable" in page, (
        "Anchor comment not found in rendered HTML — was the Recent zone comment changed?"
    )
    recent_start = page.index("Recent / resumable")
    active, recent = page[:recent_start], page[recent_start:]

    live_filter = re.compile(
        r"activeFilter === 'all' \|\| activeFilter === '(?:browser|detached|desktop)'"
    )
    # Active zone keeps filtering (detached macro row + desktop bridge row).
    assert live_filter.search(active)
    # Recent zone has its ended rows rendered (detached macro + ended-bridge row)…
    assert 'badge bg-purple-lt mode-badge me-1">detached' in recent  # detached_row(filtered=False)
    assert "resume(i.project)" in recent  # ended-bridge row still present
    # …but NONE of them carry the live-session-filter x-show.
    assert not live_filter.search(recent)


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
