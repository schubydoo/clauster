from __future__ import annotations

import re
import sys

import pytest
from fastapi.testclient import TestClient

from clauster import app as app_mod
from clauster.app import create_app
from clauster.config import load_config


def _client(write_config) -> TestClient:
    config = load_config(write_config())
    return TestClient(create_app(config))


def test_healthz(write_config):
    # write_config defaults the binary to the fake stub, so the #838 login probe
    # never invokes the real `claude auth status` (this host may run a live account).
    client = _client(write_config)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "instances_running" in body
    # #838: auth is off in this fixture, so the authenticated branch (and its
    # login-status fields) is exercised here. The first read is the neutral cold-start
    # value; wait for the single background probe to land, then re-read for the real
    # result (the fake stub reports logged-in via claude.ai by default).
    assert body["claude_login_ok"] is True  # cold-start neutral (quiet)
    client.app.state.login_status_cache.wait_for_pending_refresh()
    warm = client.get("/healthz").json()
    assert warm["claude_login_ok"] is True
    assert warm["claude_login_method"] == "claude.ai"
    assert "claude_login_expires_at" in warm


def test_dashboard_ships_claude_login_indicator(write_config):
    # #838: the header pill markup + its poll wiring ship, gated on a response with
    # the login field ("known") and only shown while logged out/expired.
    page = _client(write_config).get("/").text
    assert 'x-show="claudeLogin.known && !claudeLogin.ok"' in page
    assert "claude not logged in" in page
    # The badge polls the dedicated cache-only endpoint on its OWN path (loadLoginStatus)
    # + own interval — NOT the core refresh() Promise.all, and NOT /healthz (which runs
    # `claude --version`). So the badge poll never spawns a subprocess and never delays
    # the primary instances/sessions/agents poll.
    assert "async loadLoginStatus()" in page
    assert 'fetch(ROOT + "/api/login-status")' in page
    assert "setInterval(() => this.loadLoginStatus(), 4000)" in page
    assert page.count('fetch(ROOT + "/api/login-status")') == 1
    # The badge must NOT hit /healthz at all. The only /healthz fetch left in the script
    # is the #663 restart-reload poll (cache-busted "?_="); the bare `fetch(ROOT +
    # "/healthz")` must appear ZERO times now that the badge moved off it.
    assert page.count('fetch(ROOT + "/healthz")') == 0
    assert 'fetch(ROOT + "/healthz?_="' in page  # #663 restart poll untouched


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


def test_dashboard_non_credential_inputs_opt_out_of_autofill(write_config):
    # #1036: EVERY non-password dashboard input/textarea opts out of password-manager autofill,
    # and no credential field does — audited per field, not just page-wide.
    from conftest import audit_autofill

    html = _client(write_config).get("/").text
    missing, pw_optout = audit_autofill(html)
    assert not missing, f"non-credential inputs missing the opt-out: {missing}"
    assert not pw_optout, f"password inputs wrongly opted out of autofill: {pw_optout}"
    # ...and the full per-vendor set is present (not just data-lpignore).
    for attr in ("data-1p-ignore", "data-bwignore", 'data-form-type="other"'):
        assert attr in html


def test_optout_inputs_declare_an_explicit_type(write_config):
    # #1094: the opt-out attributes are NOT enough on their own — a manager that classifies
    # fields by heuristic can skip the `data-lpignore` check on an input with no `type`.
    # Observed live: the launch popover's First prompt and custom session name were the only
    # two opt-out inputs in the templates without an explicit type, and were the only two
    # LastPass still filled. Everything with `type="text"` was left alone.
    #
    # A filled *prompt* box is worse than a filled config row: whatever lands there is sent
    # to Claude as the session's opening instruction, so a silently injected credential ends
    # up in a transcript.
    from html.parser import HTMLParser

    class _Untyped(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.bad: list[dict] = []

        def handle_starttag(self, tag: str, attrs: list) -> None:
            if tag != "input":
                return
            d = {k: v for k, v in attrs}
            # `textarea` has no `type`, so this applies to `input` only. `type=""` counts
            # as missing: HTML invalid-value-defaults it to Text exactly like an absent
            # attribute, so a heuristic manager sees the same undeclared field.
            if "data-lpignore" in d and not d.get("type"):
                self.bad.append(d)

    p = _Untyped()
    p.feed(_client(write_config).get("/").text)
    assert not p.bad, f"autofill-opt-out inputs missing an explicit type: {p.bad}"


def test_live_terminal_button_and_xterm_gated_on_pty_screen_flag(write_config, monkeypatch):
    # #534 S5 / #904: the per-bridge "Live terminal" control + the xterm.js assets render ONLY
    # when the (default-off) claude.pty_screen_enabled tap is on AND the optional `pty` extra
    # (pyte) is present. The button is further gated client-side on i.resume_mode === 'pty'.
    monkeypatch.setattr("clauster.deps.probe", lambda entry: True)  # pretend pyte installed
    on = TestClient(create_app(load_config(write_config("claude:\n  pty_screen_enabled: true\n"))))
    body = on.get("/").text
    assert "/static/vendor/xterm/js/xterm.js" in body
    assert "togglePtyScreen(i.rk)" in body  # the button @click (Jinja-gated, not the JS method)
    assert "i.resume_mode === 'pty'" in body
    assert "#ic-terminal" in body

    off = TestClient(create_app(load_config(write_config())))  # default: flag off
    body_off = off.get("/").text
    assert "/static/vendor/xterm/js/xterm.js" not in body_off
    assert "togglePtyScreen(i.rk)" not in body_off


def test_live_terminal_greyed_when_pty_extra_missing(write_config, monkeypatch):
    # #904: tap ON but the optional `pty` extra (pyte) is ABSENT — the control must not vanish
    # silently. It renders greyed + disabled with a static install hint, and neither the
    # functional @click button nor the xterm.js assets (which would be dead weight) are shipped.
    monkeypatch.setattr("clauster.deps.probe", lambda entry: False)  # pyte not importable
    client = TestClient(
        create_app(load_config(write_config("claude:\n  pty_screen_enabled: true\n")))
    )
    body = client.get("/").text
    # Greyed control ships: disabled button + the static hint prose (no Alpine reactivity).
    assert "requires the <code>pty</code> extra" in body
    assert "pip install &#39;clauster[pty]&#39;" in body  # environment-correct hint, autoescaped
    # The functional live-view surface stays out: no toggle @click button, no xterm.js.
    assert "togglePtyScreen(i.rk)" not in body
    assert "/static/vendor/xterm/js/xterm.js" not in body


def test_live_terminal_hint_names_deps_command_when_frozen(write_config, monkeypatch):
    # #904 slice 2b: the frozen binary now bundles pip and installs the pty extra via the managed
    # `clauster deps install pty` command, so the greyed hint reads as a real "run <command>"
    # (same framing as the pip form off-binary), not the old prose docs pointer.
    monkeypatch.setattr("clauster.deps.probe", lambda entry: False)
    monkeypatch.setattr("clauster.deps.is_frozen", lambda: True)
    client = TestClient(
        create_app(load_config(write_config("claude:\n  pty_screen_enabled: true\n")))
    )
    body = client.get("/").text
    assert "requires the <code>pty</code> extra" in body  # still names the extra
    assert "run <code>clauster deps install pty</code>" in body  # runnable managed command
    assert "not bundled in the standalone binary" not in body  # the old prose pointer is gone
    assert "pip install" not in body  # the dead-end pip form is still never shown on the binary


def test_live_terminal_client_side_fit_wiring(write_config):
    # #641: the fixed-geometry (120x40) live terminal scales to the panel client-side with a
    # CSS transform — no wire-protocol/resize change. Assert the fit helper, its open-time +
    # resize-listener + first-frame re-fit hooks, and the cleanup that detaches the listener
    # all ship in the rendered source. (No JS engine in CI; this is a source-level contract.)
    body = (
        TestClient(create_app(load_config(write_config("claude:\n  pty_screen_enabled: true\n"))))
        .get("/")
        .text
    )
    assert "function _fitPtyScreen(reg)" in body
    # the intrinsic grid size MUST be measured off .xterm-screen (xterm sets the fixed px
    # width/height there) — .xterm is a plain block that stretches to the host, so measuring it
    # would yield scale~=1 and the grid would clip silently under overflow:hidden.
    assert 'reg.host.querySelector(".xterm-screen")' in body
    assert "const naturalW = screen.offsetWidth;" in body
    # scales by a transform (shrink-only) and never magnifies past 1
    assert "Math.min(1, avail / naturalW)" in body
    assert 'inner.style.transform = scale < 1 ? "scale(" + scale + ")" : "";' in body
    # fits on open, on every viewport resize (debounced), and once after the first rendered frame
    assert 'window.addEventListener("resize", reg.onResize)' in body
    assert "reg._fitTimer = setTimeout(reg.fit, 100);" in body  # resize is debounced
    assert "_fitPtyScreen(reg);  // first fit once the terminal element is laid out" in body
    assert "if (!reg._fittedOnce) { reg._fittedOnce = true; _fitPtyScreen(reg); }" in body
    # close detaches the resize listener so it can't fire against a disposed terminal
    assert 'window.removeEventListener("resize", reg.onResize)' in body
    # the geometry constants are UNCHANGED — the wire stays fixed 120x40 (locked decision)
    assert "const PTY_COLS = 120;" in body
    assert "const PTY_ROWS = 40;" in body
    # the host clips overflow (the transform shrinks the grid; no scrollbars)
    assert ".pty-screen-host {" in body
    assert "overflow: hidden" in body
    # #673: .xterm is pinned to naturalW so it doesn't stretch past the canvas as a block element
    # on wide viewports (which would leave a dark background strip to the right of the grid).
    assert 'inner.style.width = naturalW + "px";' in body
    # #673: host is border-box (Tabler reset), so style.height = total box including padding;
    # both padTop and padBottom are added so the content area inside equals the scaled grid height.
    assert "const padTop = parseFloat(cs.paddingTop) || 0;" in body
    assert "const padBottom = parseFloat(cs.paddingBottom) || 0;" in body
    assert (
        'reg.host.style.height = Math.ceil(naturalH * scale) + padTop + padBottom + "px";'
    ) in body


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


def test_tabler_sprites_for_structural_swap_present(write_config):
    # Icon pass (DES-03, #694): the structural-emoji swap (⚠/carets/⏸/←) relies on these
    # Tabler symbols existing in the sheet for the `<use>` refs to render. (The ↻ Re-check
    # reuses the pre-existing ic-restart, which is the same Tabler refresh glyph.)
    page = _client(write_config).get("/").text
    for sym in (
        'id="ic-alert"',
        'id="ic-caret-down"',
        'id="ic-caret-up"',
        'id="ic-caret-right"',
        'id="ic-pause"',
        'id="ic-arrow-left"',
    ):
        assert sym in page, f"missing Tabler sprite {sym}"


def test_static_assets_carry_immutable_cache_control(write_config):
    # #353: vendored assets are cacheable forever (safe because URLs are version-busted).
    resp = _client(write_config).get("/static/alpine.csp.min.js")
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
    first = client.get("/static/alpine.csp.min.js")
    assert first.status_code == 200 and "etag" in first.headers
    etag = first.headers["etag"]
    again = client.get("/static/alpine.csp.min.js", headers={"if-none-match": etag})
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
    assert f"alpine.csp.min.js?v={__version__}" in page
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
    # #533: the CSP build resolves bare identifiers against component state only, so the
    # directive reads the scoped `projectNames` (surfaced from the PROJECT_NAMES const).
    m = re.search(r'x-show="(projectNames\.length > 6[^"]*)"', page)
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


def test_project_row_order_style_uses_object_not_clobbering_string():
    # The bug #655 MISSED: the non-name-sort CSS `order` must be applied via an Alpine OBJECT
    # :style binding ({ order: ... }) — NOT a STRING ('order:' + n). Alpine writes a string :style
    # as the whole style attribute, clobbering the `display:none` that x-show sets on capped rows,
    # so the 6-row cap silently uncaps on any non-name sort. The object form MERGES, so the
    # display:none survives. Source-level guard only (no JS harness catches the DOM clobber — the
    # fix itself was verified in a real browser).
    from pathlib import Path

    from clauster import app as _app

    tpl = (Path(_app.__file__).parent / "templates" / "_project_row.html").read_text(
        encoding="utf-8"
    )
    assert "{ order: projectOrderRank(" in tpl, "row order :style must use the Alpine object form"
    assert "'order:' +" not in tpl, "row order :style must not be the clobbering string form"


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
    # The cap moved from an inline style="" to the .project-name class (#533, nonce-gated
    # style-src), so assert the class is applied AND the clamp value survives in the CSS.
    page = _client(write_config).get("/").text
    assert "max-width: clamp(10rem, 40vw, 28rem)" in page
    assert re.search(r'class="[^"]*\bproject-name\b[^"]*"', page), (
        "the project name must carry the .project-name class that holds the width cap"
    )


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
    assert re.search(r"openLogs\(i\.rk,\s*true\)", page)  # the Reconnect button
    assert re.search(r"openLogs\(name,\s*manual\s*=\s*false\)", page)  # default = auto-reconnect
    assert re.search(r"attempts:\s*\(prev\s*&&\s*!manual\)\s*\?\s*prev\.attempts\s*:\s*0", page)


def test_dashboard_live_tail_banners_are_mutually_exclusive(write_config):
    # #498: the reconnecting/lost live-tail banners must never stack. Even though the state
    # machine sets one and clears the other on every transition, the info banner is gated on
    # `!lost` too so a missed clear (an Alpine reactivity edge, cf. #310/#315) can't render BOTH.
    page = _client(write_config).get("/").text
    assert re.search(r"logs\[i\.rk\]\.reconnecting\s*&&\s*!logs\[i\.rk\]\.lost", page)


def test_dashboard_disconnect_copy_is_liveness_aware(write_config):
    # #498: don't claim "the bridge may have stopped" while the snapshot still reports the
    # bridge running/starting — a live-tail WebSocket can drop while the bridge keeps running.
    # The alarming copy is gated on NOT alive; a live bridge gets the transient-drop wording.
    page = _client(write_config).get("/").text
    assert "the bridge may have stopped" in page  # only when not alive
    assert "the bridge is still running" in page  # transient tail drop, bridge alive
    # The alarming copy must be liveness-gated, not unconditional.
    assert re.search(r"!isRunning\(i\.rk\)\s*&&\s*!isBusy\(i\.rk\)", page)
    assert re.search(r"isRunning\(i\.rk\)\s*\|\|\s*isBusy\(i\.rk\)", page)


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
    # #427 UX-02 + #665: a running bridge whose session_url hasn't arrived yet (async capture
    # gap) used to show 'Running' with the 'Open in Claude' + QR affordances silently gone and
    # unexplained. A disabled spinner placeholder 'Preparing connect link…' fills the gap. This
    # now applies to BOTH modes: pty DOES get a connect URL (the keeper scrapes it from the
    # reassembled pty screen, #665), so the old permanent 'No web link — use Logs' pty split is
    # gone — never claim a pty bridge has no link.
    page = _client(write_config).get("/").text
    assert "connectUrlMissing(name) {" in page  # the transient-gap helper
    assert "connectUrlUnavailable(name) {" not in page  # the pty 'no URL ever' split is removed
    assert "No web link" not in page  # the false 'permanent no link' copy is gone for good
    assert 'x-show="connectUrlMissing(i.rk)"' in page  # single placeholder gate, both modes
    assert "Preparing connect link" in page  # the transient spinner copy (visual chip)
    # The visual chip is aria-hidden; the SR announcement is a PERSISTENT aria-live region
    # (always mounted, content-toggled) — a live region shown in via x-show is silently skipped
    # by NVDA/VoiceOver (Greptile P2). Assert that wiring.
    assert "connectStatusText(name) {" in page  # the live-region text helper ships
    assert (
        '<span class="visually-hidden" aria-live="polite" x-text="connectStatusText(i.rk)">'
        in page
    )
    # The announced string mirrors the visible chip label word-for-word, so the SR and sighted
    # experiences match (pin it — it's otherwise unasserted and could drift).
    assert '"Preparing connect link…"' in page  # transient SR text == spinner chip label


def test_dashboard_restart_note_says_sessions_survive(write_config):
    # #663: the config-save note previously WARNED 'N session(s) running — a restart will
    # end them', contradicting the actual shutdown (runner.shutdown() leaves bridges
    # running; hosted.aclose() detaches, not stops) — the in-app re-exec PRESERVES live
    # sessions and reattaches them. The note now reassures ('they keep running and
    # reconnect') instead of warning. Counts only "running"/"starting" (not "stopping" —
    # a bridge mid-Stop is on its way out, so it would overstate the "N running" copy).
    page = _client(write_config).get("/").text
    assert "restartImpactCount() {" in page  # the count helper is defined and shipped
    assert 'const liveStatuses = ["running", "starting"];' in page
    assert 'data-test="cfg-restart-note"' in page  # renamed: it's a note, not a warning
    assert 'x-show="restartImpactCount() > 0"' in page  # gated on live sessions
    # The reassuring copy (with singular/plural verb agreement) — and NOT the old
    # false "will end them" warning.
    assert "restartImpactCount() === 1 ? 'reconnects' : 'reconnect'" in page
    assert "after the restart" in page
    assert "will end" not in page
    assert "How do I restart?" in page  # the docs affordance
    # #579: the link must point at a LIVE docs target (it previously rotted to a
    # nonexistent README #running anchor). Pin the stable operations#restart URL so a
    # silent rot fails the suite rather than shipping another dead help link.
    assert "https://schubydoo.github.io/clauster/operations/#restart" in page


def test_restart_handler_polls_healthz_then_reloads(write_config):
    # #663: the in-app re-exec rebinds the SAME port, so the WS reconnects but the page
    # never reloads itself — the old handler left the button stuck on "Restarting…"
    # forever. The handler now polls /healthz after the 202, then reloads; it is bounded
    # so a restart that never returns re-enables the button instead of spinning.
    page = _client(write_config).get("/").text
    assert "async restartClauster()" in page  # the handler is defined and shipped
    assert "async awaitRestartThenReload()" in page  # the poll-then-reload helper ships
    assert 'ROOT + "/healthz?_="' in page  # polls the auth-exempt health endpoint (cache-busted)
    assert "window.location.reload()" in page  # and reloads once the new image binds
    # The confirmation reassures (sessions survive) rather than warning they end.
    assert '(n === 1 ? "reconnects" : "reconnect") + " automatically."' in page


def test_dashboard_renders_in_app_restart_action(write_config):
    # #483: the config editor exposes a "Restart Clauster" action that POSTs the
    # auth-gated restart endpoint, gated behind the same #427 restart-impact confirm.
    page = _client(write_config).get("/").text
    assert 'data-test="cfg-restart"' in page  # the button is rendered in the modal footer
    assert "async restartClauster()" in page  # the handler is defined and shipped
    assert 'fetch(ROOT + "/api/restart", { method: "POST" })' in page  # POSTs the endpoint
    # Reuses the #427 impact confirmation rather than a new typed-confirm.
    assert "this.restartImpactCount()" in page


def test_restart_action_polls_health_then_reloads_and_handles_failure(write_config):
    # #663 (supersedes #483's "honest catch"): the in-app re-exec rebinds the SAME port,
    # so the page never reloads itself — the handler now polls /healthz after the 202, then
    # reloads. A DEFINITE reject (e.g. 503 no live server) re-enables the button and returns
    # (nothing restarted — retriable). The catch (an ambiguous mid-restart drop OR a
    # pre-flight failure) falls through to the SAME bounded poll, which resolves both
    # (/healthz answers at once if the server is still up) and re-enables on timeout so the
    # button never strands on "Restarting…".
    page = _client(write_config).get("/").text
    assert "if (res.ok || res.status === 202)" not in page  # no dead 2xx condition
    assert "if (!res.ok) {" in page  # the definite-reject branch
    # The definite-reject path re-enables + bails before the poll (server still up).
    assert re.search(r"if \(!res\.ok\) \{.*?c\.restarting = false;.*?return;", page, re.DOTALL)
    # Both the 2xx path and the catch fall through to the one bounded poll-then-reload.
    assert "await this.awaitRestartThenReload();" in page
    # The poll is BOUNDED (a deadline) and re-enables on timeout instead of spinning forever.
    assert "Date.now() + 60000" in page
    assert re.search(
        r"async awaitRestartThenReload\(\) \{.*?this\.configEditor\.restarting = false;",
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
    assert "resume(i.rk)" in resp.text  # bridge resume affordance
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
    assert "resume(i.rk)" in recent  # ended-bridge row still present
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
    # Redesign: the Mode picker now lives in the launch popover's "More options"
    # disclosure (Desktop launch, #686). Its JS wiring is platform-independent
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
        assert "x-model=\"resumeMode['alpha']\"" in resp.text  # popover More-options picker
        assert "Interactive Session (single-session, true-resume)" in resp.text
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


def test_dashboard_live_session_row_has_transcript_button(write_config):
    # #866: the live desktop/bridge session row carries its own read-only Transcript
    # trigger (distinct data-test from the project-row one), wired to openTranscripts
    # with the row's project, and hidden for ANY worktree-spawn bridge (standard or
    # interactive) whose transcript lives under a different cwd (not in this viewer).
    page = _client(write_config).get("/").text
    assert 'data-test="transcript-trigger-session"' in page
    assert "openTranscripts(i.project)" in page  # dynamic, per live session
    assert "i.spawn_mode !== 'worktree'" in page  # worktree-spawn gated (mode-agnostic)


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


def test_dashboard_transcript_live_is_single_list(write_config):
    # #614 Part 2 follow-up (0.12.9): a LIVE session is ONE unified, tailing transcript —
    # not a separate "live tail" box alongside a static list. Regression guards for the
    # duplication / divergence / ordering / label / fragile-key bug set.
    page = _client(write_config).get("/").text
    # Single list: there is exactly ONE turn x-for, over transcripts.turns. The old
    # separate tail loop (x-for over tailTurns, keyed 'tail-'+i) must be gone.
    assert page.count('data-test="transcript-turn"') == 1  # one rendered turn card block
    assert "transcripts.tailTurns" not in page  # the second list is gone
    assert ":key=\"'tail-' + i\"" not in page  # ...and so is its fragile index key
    # The live header is just a badge strip (no turns of its own); turns render in the
    # single list below. It shows only while live.
    assert 'data-test="transcript-tail"' in page
    assert 'x-show="transcripts.live"' in page
    assert 'data-test="transcript-tail-live"' in page
    # New live turns flow through tailRaw (the oldest-first master) into the one list via
    # _rebuildLiveTurns, which honors the active sort + search.
    assert "transcripts.tailRaw" in page
    assert "_rebuildLiveTurns()" in page
    assert "_liveMatch(" in page  # search filters the live list client-side
    # Sort toggle flips in place while live (no re-fetch); both label + aria-label
    # describe the same STATE (the old aria-label described the opposite ACTION).
    assert "if (t.live) this._rebuildLiveTurns();" in page  # flip re-derives, no re-fetch
    assert "transcripts.order === 'asc' ? 'Sorted oldest first' : 'Sorted newest first'" in page
    assert "transcripts.order === 'asc' ? 'Oldest first' : 'Newest first'" in page
    # Stable keys, not the bare index (#720): the single list keys on the per-turn _key
    # when present (live), else a timestamp+role+index composite. The old `:key="i"` is
    # gone. The fallback lives in keyOf() (#533: the CSP-build parser has no `??`, and
    # `||` would wrongly discard a legitimate _key of 0).
    assert ':key="keyOf(t, i)"' in page
    assert "t._key !== undefined && t._key !== null" in page  # falsy-0 _key preserved
    assert '(t.timestamp || "") + ":" + t.role + ":" + i' in page
    assert ':key="i"' not in page
    # Live path bumps t.gen before starting the tail (it no longer calls the paged loader
    # that used to do it) so an in-flight non-live fetch from a prior session is gen-voided
    # and can't write its stale turns over the live view.
    assert "t.gen = (t.gen || 0) + 1;\n          t.turns = []" in page
    # Idle polls (no new turns, no reset) must NOT trigger the O(n) rebuild; the rebuild is
    # gated on the master actually changing.
    assert "if (changed) this._rebuildLiveTurns();" in page
    # closeTranscripts clears tailOffset + tailTruncated alongside the other live-state
    # fields (matching backToTranscriptList) so the two teardown paths don't diverge.
    assert (
        "t.live = false; t.tailRaw = []; t.tailOffset = 0; t.tailKey = 0; "
        't.tailError = ""; t.tailTruncated = false;  // clear live state on close'
    ) in page


def test_dashboard_live_tail_cap_bounds_tailraw(write_config):
    # issue 735: tailRaw was append-only with no bound; a long-running session could grow
    # the array and DOM unbounded. The poll loop now front-splices tailRaw to MAX_TAIL_TURNS
    # after each push, and sets tailTruncated=true so the header can show a visible notice
    # ("showing last N turns") rather than silently dropping history.
    page = _client(write_config).get("/").text
    # The cap constant must be defined in the script.
    assert "const MAX_TAIL_TURNS = 1000;" in page
    # tailTruncated field is declared on the transcripts state object.
    assert "tailTruncated: false" in page
    # After each push the poll loop slices tailRaw to the cap when it grows over the limit.
    assert "t.tailRaw.length > MAX_TAIL_TURNS" in page
    assert "t.tailRaw = t.tailRaw.slice(t.tailRaw.length - MAX_TAIL_TURNS);" in page
    assert "t.tailTruncated = true;" in page
    # On file-rotation (d.reset) the master is replaced from scratch — tailTruncated resets.
    assert "t.tailTruncated = false; changed = true;" in page
    # A visible indicator (not a silent drop): the cap span uses x-show on tailTruncated.
    assert 'data-test="transcript-tail-cap"' in page
    assert 'x-show="transcripts.tailTruncated"' in page
    # Directives reference the SCOPED mirror (CSP-build-safe, issue 533), which is
    # initialized from the JS constant.
    assert "maxTailTurns: MAX_TAIL_TURNS" in page
    assert "'— showing last ' + transcripts.maxTailTurns + ' turns'" in page
    # live→ended on a CAPPED tail hands off to the paged backend path — a capped list
    # can't pose as the complete static transcript (and hiding the live card hides the
    # truncation banner), so the ended view reloads from the start instead.
    assert (
        "if (t.tailTruncated) { t.tailTruncated = false; this.loadTranscriptTurnsFromStart(); }"
        in page
    )


def test_clone_cancel_has_confirm_before_cancel_dialog(write_config):
    # #659 item 3: the dedicated "Cancel clone" button arms an inline confirm rather than
    # aborting outright — cancel discards a partial download, so a stray click shouldn't
    # destroy it. The button calls promptCancelClone(); the confirm shows a destructive
    # "Yes, cancel clone" + a non-destructive "Keep cloning", mirroring the trust/bypass
    # confirms. The button hides while the confirm is open so the inline Yes/No is the
    # only live control.
    page = _client(write_config).get("/").text
    assert "promptCancelClone()" in page  # the button arms the confirm, not a direct cancel
    assert 'x-show="np.confirmCancel"' in page  # the inline confirm dialog
    assert ">Yes, cancel clone<" in page
    assert ">Keep cloning<" in page
    assert 'x-show="np.cloning && !np.confirmCancel"' in page  # button hides under the confirm


def test_clone_reattach_badge_present_for_cross_tab(write_config):
    # #659 item 4: a clone started in another tab reattaches here and shows a badge marking
    # it as started elsewhere. The badge is gated on np.cloning && np.reattached so it
    # auto-hides the moment the clone settles.
    page = _client(write_config).get("/").text
    assert 'x-show="np.cloning && np.reattached"' in page
    assert "started in another tab" in page
    assert 'aria-live="polite"' in page  # the reactive badge is announced to assistive tech


def test_clone_reattach_is_detach_only(write_config):
    # #659 item 4: a tab that reattached to a clone started ELSEWHERE must not server-cancel
    # it when its panel closes/resets — it didn't start the job. reattachActiveClones installs
    # the watch with detachOnly=true; a deliberate "Cancel clone" still tears it down via the
    # direct cancel POST in cancelClone (gated on _cloneDetachOnly).
    page = _client(write_config).get("/").text
    assert "/* detachOnly */ true" in page  # reattach watch is detach-only
    assert "this._cloneDetachOnly && this._cloneJobId" in page  # confirmed Cancel still POSTs


def test_dashboard_actions_deref_displayed_instance_id(write_config):
    """Name-keyed actions pin the DISPLAYED row's instance_id at click time (#778).

    Re-resolving a project name server-side can drift to a bridge registered after
    the client's last poll (Greptile P1s on #797) — so Stop/Resume/Forget/QR and the
    log/pty websockets all deref ``bridgeIdOf(name)`` (instance_id of the rendered
    row). There is NO name fallback: the only id-less row is start()'s optimistic
    placeholder — an identity not yet minted — so actions on it refuse with a toast
    (``_requireBridgeId``) instead of letting the server re-resolve the name onto a
    possibly-hidden other bridge.
    """
    page = _client(write_config).get("/").text
    assert "bridgeIdOf(key) {" in page  # the deref helper ships (key-aware, #779)
    assert ".instance_id || null; }" in page  # no name fallback — null means refuse
    # Every action entry point routes through the refuse-while-unminted guard:
    # resume / stop / forget / log-tail ws / pty-screen ws.
    by_name = page.count("this._requireBridgeId(name)")
    by_key = page.count("this._requireBridgeId(key)")
    assert by_name + by_key >= 5
    assert "still starting — try again in a moment" in page  # the refusal is explained
    # No raw project-name identity remains on the instance API/ws call sites.
    assert 'fetch(ROOT + "/api/instances/" + encodeURIComponent(name)' not in page
    assert '"/ws/pty-screen/" + encodeURIComponent(name)' not in page
    assert '"/ws/bridge-log/" + encodeURIComponent(name)' not in page


def test_dashboard_multi_session_client_plumbing(write_config):
    """The client splits pty sessions out of the project-keyed map (#779).

    N interactive sessions per project render as id-keyed rows (rk = instance_id)
    beside the single standard bridge; a pending pty launch shows an explicit stub;
    spawn advisories (warnings[]) surface as toasts.
    """
    page = _client(write_config).get("/").text
    # The fold: pty rows go to the flat id-keyed collection, never the project map.
    assert "ptySessions" in page
    assert 'if (i.resume_mode === "pty") pty.push(i); else next[i.project] = i;' in page
    assert "_stamp(i) { i.rk = i.instance_id || i.project; return i; }" in page
    # Rows key by rk in both zones (the project name is not unique any more).
    assert page.count(':key="i.rk"') == 2  # Active + Recent bridge loops
    # Pending interactive-launch stub (no row exists until the POST returns).
    assert 'data-test="pty-pending-row"' in page
    assert "Starting interactive session…" in page
    # Spawn advisories from the outcome keys (#778) surface as toasts.
    assert 'for (const w of body.warnings || []) this.toast(w, "warning");' in page
    assert "body.created === false && body.reason" in page
    # Session-shape chips: interactive + worktree markers on the row head.
    assert ">interactive</span>" in page
    assert ">worktree</span>" in page
    # Project-level rollups see the split-out pty collection too (Greptile P2s on #800):
    # the restart-impact count includes live interactive sessions, and _absorbRow drops
    # a stale project-keyed placeholder from the id index.
    assert "const pty = this.ptySessions.filter((s) => liveStatuses.includes(s.status));" in page
    assert "delete this._byId[body.project];" in page


@pytest.mark.skipif(sys.platform == "win32", reason="the pty launch controls are POSIX-only")
def test_launch_popover_pty_worktree_controls(write_config):
    """The Spawn picker applies to interactive sessions; the collision hint warns (#779).

    pty honors same-dir/worktree (worktree = `claude --worktree`), so the picker is no
    longer hidden in pty mode; only the standard-only `session` option is disabled
    (and coerced away). The no-worktree collision hint warns without blocking. The
    whole block is `pty_supported`-gated markup, so it never renders on Windows.
    """
    page = _client(write_config).get("/").text
    # The old pty gate hid the whole Spawn column — it must be gone.
    assert '''x-show="(resumeMode['alpha'] || defaultResumeMode) !== 'pty'"''' not in page
    # `session` stays standard-only: disabled in pty mode, coerced back to same-dir.
    assert "coercePtySpawn(" in page
    assert '''=== 'pty'"''' in page  # the :disabled gate renders
    # The warn-don't-block collision hint (git projects, where worktree is offered).
    assert 'data-test="pty-collision-hint"' in page
    assert "choose\n                              Spawn: worktree to isolate this one." in page


# --- Interactive Session mode-picker gate: _pty_supported (#914) ---


def test_pty_supported_true_on_posix(monkeypatch):
    monkeypatch.setattr(app_mod.sys, "platform", "linux")
    assert app_mod._pty_supported() is True


def test_pty_supported_on_windows_reflects_conpty_keeper(monkeypatch):
    # On Windows the picker offers Interactive Session only when the ConPTY keeper (pywinpty)
    # is present; without it a `launch_mode: pty` request would fall back to Server Mode.
    monkeypatch.setattr(app_mod.sys, "platform", "win32")
    monkeypatch.setattr(app_mod, "_conpty_keeper_available", lambda: True)
    assert app_mod._pty_supported() is True
    monkeypatch.setattr(app_mod, "_conpty_keeper_available", lambda: False)
    assert app_mod._pty_supported() is False
