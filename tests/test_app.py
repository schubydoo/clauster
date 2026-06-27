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


def test_live_terminal_button_and_xterm_gated_on_pty_screen_flag(write_config):
    # #534 S5: the per-bridge "Live terminal" control + the xterm.js assets render ONLY when the
    # (default-off) claude.pty_screen_enabled tap is on. The button itself is further gated
    # client-side on i.resume_mode === 'pty' (pty bridges have no PTY-less analog).
    on = TestClient(create_app(load_config(write_config("claude:\n  pty_screen_enabled: true\n"))))
    body = on.get("/").text
    assert "/static/vendor/xterm/js/xterm.js" in body
    assert (
        "togglePtyScreen(i.project)" in body
    )  # the button @click (Jinja-gated, not the JS method)
    assert "i.resume_mode === 'pty'" in body
    assert "#ic-terminal" in body

    off = TestClient(create_app(load_config(write_config())))  # default: flag off
    body_off = off.get("/").text
    assert "/static/vendor/xterm/js/xterm.js" not in body_off
    assert "togglePtyScreen(i.project)" not in body_off


def test_transcript_viewer_has_sort_toggle_and_search(write_config):
    # #612: the read-only transcript modal gains a sort-direction toggle and an
    # in-message search box. Assert both controls + their wiring ship in the markup,
    # and that turn content is still bound via x-text (never x-html — it's untrusted,
    # server-redacted output and binding it as HTML would be stored XSS).
    page = _client(write_config).get("/").text
    assert 'data-test="transcript-order-toggle"' in page
    assert "toggleTranscriptOrder()" in page
    assert 'data-test="transcript-search"' in page
    assert "onTranscriptSearchInput()" in page
    assert 'data-test="transcript-search-clear"' in page
    assert 'data-test="transcript-no-matches"' in page
    assert 'data-test="transcript-query-short"' in page
    # Untrusted turn content stays x-text; there must be no x-html anywhere near it.
    assert 'class="transcript-content small" x-text="t.content"' in page


def test_clone_progressbar_exposes_aria_value_attributes(write_config):
    # #607 (a11y): the New-project clone bar is a real ARIA progressbar. It carries
    # role + min/max always, binds aria-valuenow during the determinate phase, and
    # surfaces aria-valuetext (the phase label) during the indeterminate phase — so
    # assistive tech announces progress instead of a silent role="progressbar".
    page = _client(write_config).get("/").text
    assert 'role="progressbar"' in page
    assert 'aria-valuemin="0"' in page
    assert 'aria-valuemax="100"' in page
    # value + text are Alpine-bound, so they ship as `:attr` bindings in the source.
    assert ":aria-valuenow=" in page
    assert ":aria-valuetext=" in page


def test_tabler_sprites_for_hosted_chrome_present(write_config):
    # Icon pass (DES-04): the hosted-transcript chrome swaps structural emoji (🔧/✓/✕) for
    # Tabler sprites — those symbols must exist in the sprite sheet for the `<use>` refs to render.
    page = _client(write_config).get("/").text
    for sym in ('id="ic-tool"', 'id="ic-check"', 'id="ic-x"'):
        assert sym in page, f"missing Tabler sprite {sym}"


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


def test_hosted_result_frame_does_not_double_render_assistant_reply(write_config):
    # #591: with --include-partial-messages a single assistant turn emits the streamed
    # `assistant` frame AND a trailing `result` frame whose `result` field repeats the
    # same text. The live-view used to render both — the assistant message (white,
    # pre-wrap) and the result echo (green, run-on) — so every reply printed twice. The
    # result branch of _hostedFrameToItems must collapse a SUCCESSFUL turn to a
    # turn-boundary marker (no text echo) and surface text only on the error path, where
    # `result` carries content no assistant frame emits ("Not logged in · Please run
    # /login"). Pure-JS mapping, no JS harness in CI → guard the source text.
    page = _client(write_config).get("/").text
    branch = re.search(r'if \(t === "result"\) \{(.*?)if \(t === "assistant"', page, re.DOTALL)
    assert branch is not None, "result branch of _hostedFrameToItems not found"
    body = branch.group(1)
    # The success path must NOT re-emit the assistant text as its own renderable item.
    assert 'kind: "result"' not in body, (
        "result frame still emits a `result` item — that re-renders the assistant reply "
        "a second time (#591); a successful turn must collapse to a marker instead"
    )
    # Text survives only on the error path, gated on is_error — NOT subtype, which is
    # "success" even on auth failure (hosted-protocol empirical, ARM 1).
    assert "frame.is_error" in body, "result text echo must be gated on is_error"
    assert 'kind: "marker"' in body, "a successful result must collapse to a marker"
    # The error path must emit a kind:"error" item — a renderer that exists. Without this
    # assertion, flipping the error branch to a marker (or any kind with no template) would
    # silently swallow auth-failure text and the test would still pass.
    assert 'kind: "error"' in body, (
        "the error path must emit a kind:error item so failure text stays visible"
    )
    # …and the now-unreachable green `text-success` result renderer is gone from markup.
    assert "it.kind === 'result'" not in page, (
        "the green `text-success` result renderer is unreachable now — remove it (#591)"
    )


def test_projects_show_more_toggle_appears_in_all_sorts(write_config):
    # The Projects-zone "Show all / Show fewer" toggle must appear in EVERY sort, not only A–Z.
    # In a non-name sort the 6-row cap applies by the chosen sort rank; the toggle reveals the
    # rest. Regression: the x-show used to carry `projectSort === 'name'`, so the toggle vanished
    # whenever you sorted by last-used or cost.
    page = _client(write_config).get("/").text
    m = re.search(r'x-show="(PROJECT_NAMES\.length > 6[^"]*)"', page)
    assert m is not None, "Projects show-more toggle button (x-show) not found"
    assert "projectSort" not in m.group(1), (
        "the show-more toggle is gated on the name sort; it must appear in all sorts"
    )


def test_project_visible_caps_via_stable_rank_dep_not_idx_branch(write_config):
    # #585(b): switching the sort back to A–Z used to leave the list STUCK uncapped. Root cause was
    # a branch-varying reactive dep in projectVisible(): the name branch read the literal `idx`,
    # the non-name branch read `projectRanks`. Returning to A–Z re-evaluated rows in the `idx`
    # branch, dropping `projectRanks` from Alpine's tracked deps, so the cap never re-applied. The
    # fix makes the dep set STABLE — projectVisible always caps via projectOrderRank (which reads
    # projectRanks) in EVERY sort. No JS harness, so pin the mechanism in the rendered script.
    page = _client(write_config).get("/").text
    body = re.search(r"projectVisible\(name, idx\) \{(.*?)\n      \},", page, re.S)
    assert body is not None, "projectVisible() not found in rendered script"
    inner = body.group(1)
    # The cap must go through projectOrderRank (the projectRanks read) — the stable dep.
    assert "projectOrderRank(name, idx)" in inner, (
        "projectVisible must cap via projectOrderRank so projectRanks is tracked in every sort"
    )
    # The branch-varying special-case (`this.projectSort !== "name"`) that caused the
    # stuck-uncapped bug must be gone — a single rank-based path, not a per-sort branch.
    assert 'this.projectSort !== "name"' not in inner, (
        "projectVisible must not branch on projectSort; that reintroduces #585(b)"
    )
    # Now that projectRanks is always populated, a freshly fragment-inserted project (insertCard;
    # idx=0 from /api/projects/{name}/row) is absent from the rank map and would otherwise be
    # capped out (rank 9999). The "a just-created project is always shown" contract must hold:
    # a row whose name is not in a populated ranks map is never capped.
    assert "Object.keys(ranks).length && !(name in ranks)" in inner, (
        "projectVisible must keep a fragment-inserted (unranked) project visible"
    )


def test_recompute_project_ranks_populates_name_sort(write_config):
    # #585(b): recomputeProjectRanks() must POPULATE projectRanks for the name sort (ranked by the
    # stable A–Z / PROJECT_NAMES position) instead of clearing it to {}. A populated map in every
    # sort is what keeps projectVisible's reactive dep set stable so the cap re-applies on the
    # return-to-A–Z path. Guard against reverting to the `projectSort === "name" -> {} ` clear.
    page = _client(write_config).get("/").text
    fn = re.search(r"recomputeProjectRanks\(\) \{(.*?)\n      \},", page, re.S)
    assert fn is not None, "recomputeProjectRanks() not found in rendered script"
    inner = fn.group(1)
    assert 'this.projectSort === "name") { this.projectRanks = {}; return; }' not in inner, (
        "name sort must populate projectRanks (A–Z ranks), not clear it to {}"
    )
    assert "PROJECT_NAMES.forEach((n, i) => { ranks[n] = i; });" in inner, (
        "name sort must rank by the stable A–Z PROJECT_NAMES position"
    )


def test_load_sort_meta_swaps_ranks_without_clearing_first(write_config):
    # #585(a): the flash on sort change came from loadSortMeta() clearing projectRanks = {} BEFORE
    # the async fetch, which uncapped the whole list for the duration of the round-trip. The fix
    # swaps (recomputeProjectRanks reassigns in one shot) instead of clear-then-fetch, so the cap
    # stays applied throughout. Pin that the up-front clear is gone from loadSortMeta().
    page = _client(write_config).get("/").text
    fn = re.search(r"async loadSortMeta\(\) \{(.*?)\n      \},", page, re.S)
    assert fn is not None, "loadSortMeta() not found in rendered script"
    inner = fn.group(1)
    # The fetch must still happen; the destructive pre-fetch clear must not.
    assert "/api/projects/sortmeta" in inner, "loadSortMeta must still fetch the sort keys"
    pre_fetch = inner.split("fetch(", 1)[0]
    assert "this.projectRanks = {}" not in pre_fetch, (
        "loadSortMeta must not clear projectRanks before fetch (the #585(a) flash)"
    )


def test_project_name_uses_responsive_width_cap(write_config):
    # DES-07: the project name truncates at a viewport-relative width (clamp 10rem→28rem), not a
    # fixed 16rem cap, so long names adapt to the screen. Guards against reverting to a fixed cap.
    page = _client(write_config).get("/").text
    assert "max-width:clamp(10rem, 40vw, 28rem)" in page


def test_session_url_scheme_guard_present(write_config):
    # FE-03: sessionUrlOf() must filter the session URL to http(s) before it's bound to :href in
    # the Active list, so a non-http value can never reach the sink. No JS unit harness, so pin the
    # guard by presence in the rendered inline script (mirrors test_dashboard_log_ws_and_refresh).
    page = _client(write_config).get("/").text
    assert "sessionUrlOf" in page
    assert "^https?:" in page, "the sessionUrlOf http(s)-scheme guard is missing"


def test_dashboard_friendly_labels_and_label_associations(write_config):
    # Polish-2: no raw permission/filter enum tokens shown inline, and form controls are
    # label-associated (a11y). No JS harness — guard the wiring by presence in the rendered page.
    page = _client(write_config).get("/").text
    assert 'x-text="permLabel(lperm)"' in page  # Run button shows a friendly permission label
    assert 'x-text="filterLabel(f)"' in page  # Active-zone filter chips use friendly names
    assert "permLabel(" in page  # the helper is wired (also used by the hosted row when enabled)
    assert 'for="lprompt-' in page  # the "First prompt" input is label-associated
    assert 'aria-labelledby="np-type-label"' in page  # New-project "Type" radio group is labelled


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


def test_dashboard_reconnect_button_resets_retry_cap(write_config):
    # #584: once auto-reconnect hits MAX_LOG_RECONNECTS the tail gives up ("lost"). The
    # operator's Reconnect button must NOT carry that capped `attempts` forward, or the very
    # first onclose re-trips the cap and the button is a dead no-op. The button passes
    # `manual=true`, and openLogs resets attempts to 0 for a manual retry (carries it only on
    # the auto-reconnect re-entry, so the cap still bounds an unattended retry burst).
    page = _client(write_config).get("/").text
    assert re.search(r"openLogs\(i\.project,\s*true\)", page)  # the Reconnect button
    assert re.search(r"openLogs\(name,\s*manual\s*=\s*false\)", page)  # default = auto-reconnect
    assert re.search(r"attempts:\s*\(prev\s*&&\s*!manual\)\s*\?\s*prev\.attempts\s*:\s*0", page)


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
    # #579: the link must point at a LIVE docs target (it previously rotted to a
    # nonexistent README #running anchor). Pin the stable operations#restart URL so a
    # silent rot fails the suite rather than shipping another dead help link.
    assert "https://schubydoo.github.io/clauster/operations/#restart" in page


def test_dashboard_renders_in_app_restart_action(write_config):
    # #483: the config editor exposes a "Restart Clauster" action that POSTs the
    # auth-gated restart endpoint, gated behind the same #427 restart-impact confirm.
    page = _client(write_config).get("/").text
    assert 'data-test="cfg-restart"' in page  # the button is rendered in the modal footer
    assert "async restartClauster()" in page  # the handler is defined and shipped
    assert 'fetch(ROOT + "/api/restart", { method: "POST" })' in page  # POSTs the endpoint
    # Reuses the #427 impact confirmation rather than a new typed-confirm.
    assert "this.restartImpactCount()" in page


def test_restart_action_success_check_and_honest_catch(write_config):
    # #483 review (Greptile P2 x2): the success branch must not carry the dead
    # `|| res.status === 202` (res.ok already covers every 2xx), and the catch must
    # not over-promise a reconnect — fetch throws the same TypeError for an expected
    # mid-restart drop AND a pre-flight failure where nothing restarted, so the message
    # must cover both and the button must re-enable so a still-running server is retriable.
    page = _client(write_config).get("/").text
    assert "if (res.ok || res.status === 202)" not in page  # dead condition is gone
    assert "if (res.ok) {" in page  # simplified to the 2xx check
    # The catch surfaces an honest, non-over-promising message (not the bare success copy)
    # and re-enables the button rather than stranding it disabled forever.
    assert "the restart may have failed; check the service." in page
    assert re.search(
        r"catch \(e\) \{.*?the restart may have failed.*?c\.restarting = false;",
        page,
        re.DOTALL,
    )


def test_desktop_stop_confirms_and_error_toasts_stick(write_config):
    # #577: two error-UX consistency papercuts.
    # A) the desktop-bridge Stop was the only destructive action with no window.confirm
    #    (siblings forget/stopAgent/stopHosted/killHosted all confirm) — add one.
    # B) every toast auto-dismissed after 4.5s, so an "error" toast (sometimes a
    #    failure's only record) vanished — errors now persist until dismissed.
    page = _client(write_config).get("/").text
    # A: stop() confirms before the optimistic DELETE.
    assert 'window.confirm("Stop the session in ' in page
    # B: only non-error toasts get the 4.5s auto-dismiss timer.
    assert 'if (type !== "error") setTimeout(' in page


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


def test_readiness_vocab_is_unified_and_scoped(write_config):
    # UX-07: both pre-launch warning systems share ONE user-facing term ("readiness
    # checks"), distinguished by scope wording — the header pill is System-wide
    # ("affects every launch"), the per-project pill is "for this project". The jargon
    # word "preflight" is gone from visible copy; internal preflight* names/routes/
    # test hooks (data-test="preflight-pill", preflightWarnCount, /api/.../preflight)
    # intentionally stay, so this asserts the rendered COPY, not the identifiers.
    page = _client(write_config).get("/").text
    # header (global) readiness pill now carries the System-wide scope cue
    assert "System readiness" in page
    assert "affects every launch on this host" in page
    # per-project pill: scoped tooltip (pluralized to match the pill, no literal "(s)")
    # + the per-project "Readiness checks" detail heading
    assert "=== 1 ? ' readiness check' : ' readiness checks'" in page
    assert "for this project before launch" in page
    assert "Readiness checks for" in page
    # the detail group is programmatically labelled by its heading (a11y association)
    assert 'aria-labelledby="readiness-detail-head-alpha"' in page
    assert 'id="readiness-detail-head-alpha"' in page
    # the pill's visible token is pluralized check/checks — the bare "preflight" text node is gone
    assert "=== 1 ? 'check' : 'checks'" in page


def test_dashboard_has_bg_agent_dispatch_and_stop_controls(write_config):
    # BG-4 (redesign): background agents are now launched as the "Background"
    # outcome in the per-project launch popover (_launchDetached -> POST /api/agents,
    # with an optional "also register on claude.ai"), and stopped via the per-row
    # Stop button (stopAgent -> DELETE /api/agents/{id}) in the unified Active list.
    page = _client(write_config).get("/").text
    assert "Background" in page  # the launch-mode label
    assert "_launchDetached" in page  # routes the background launch
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
    assert (
        'badge bg-purple-lt mode-badge me-1">background' in recent
    )  # detached_row(filtered=False)
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


def test_dashboard_transcript_viewer_trigger_and_modal_render(write_config):
    # #431 frontend: every project row carries a "View transcript" trigger wired to
    # openTranscripts, and the read-only viewer modal ships with its session-list +
    # turn-page chrome and the Alpine fetch methods that hit the transcripts API.
    page = _client(write_config).get("/").text
    # Per-project trigger (rendered once per row — alpha/beta/gamma).
    assert 'data-test="transcript-trigger"' in page
    assert "openTranscripts('alpha')" in page
    assert "</svg>Transcript" in page  # the trigger's visible label
    # The viewer modal + its session/turn affordances.
    assert 'data-test="transcript-modal"' in page
    assert 'data-test="transcript-session"' in page  # session-list rows
    assert 'data-test="transcript-turn"' in page  # rendered turn cards
    assert 'data-test="transcript-load-more"' in page  # pagination control
    assert 'data-test="transcript-empty"' in page  # empty state
    assert 'data-test="transcript-error"' in page  # error state
    # "Load more" is hidden when next_cursor is exhausted (null).
    assert "transcripts.nextCursor !== null" in page
    # Alpine fetch wiring: the methods + the transcripts API path ship in the page.
    assert "async openTranscripts(name)" in page
    assert "async openTranscriptSession(session)" in page
    assert "async loadMoreTranscriptTurns()" in page
    assert '"/transcripts"' in page  # session-list endpoint suffix
    assert '"/transcripts/"' in page  # per-session endpoint suffix
    # 401 → bounce to login (copied from the usage/config fetchers).
    assert page.count('window.location.assign(ROOT + "/login")') >= 1


def test_dashboard_transcript_content_uses_x_text_not_x_html(write_config):
    # CRITICAL safety (#431): turn content is server-redacted but still untrusted
    # agent/user output — rendering it as HTML would be stored XSS. The turn body MUST
    # use x-text, and the dashboard must carry no x-html anywhere near the turn render.
    page = _client(write_config).get("/").text
    assert 'x-text="t.content"' in page  # turn body bound via x-text
    # No x-html DIRECTIVE anywhere on the page — the attribute form is the XSS sink
    # (a literal "x-html" inside prose/comments isn't, so match the binding, not the word).
    assert "x-html=" not in page and "x-html =" not in page
    # role / model / timestamp are likewise x-text-bound, never interpolated as HTML.
    # (The role badge renders "you" for user turns, so match the x-text binding by prefix.)
    assert 'x-text="t.role' in page
    assert 'x-text="t.model"' in page
