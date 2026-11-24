"""Single-screen launch popover with one "More options" disclosure (#686).

The launch flow is reshaped so the advanced Mode selector (Server Mode /
Interactive Session) and Spawn sit under a single "More options" disclosure
(tucked away by default), the trust-on-start + bypassPermissions confirm gates
fold INSIDE the popover (no below-the-row alerts), and the default launch mode
stays the stable safe ``desktop`` choice — never auto-flipped to the
experimental hosted ``browser`` channel.
"""

from __future__ import annotations

import json
import sys

import pytest
from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import CEILING_GATED_MODES, PERMISSION_LABELS, load_config

# A per-project ceiling that surfaces the gated bypassPermissions option/gate.
_BYPASS_CEILING = "projects:\n  alpha:\n    allow_bypass_permissions: true\n"


def _client(write_config, extra: str = "") -> TestClient:
    return TestClient(create_app(load_config(write_config(extra))))


def _between(text: str, start_marker: str, needle: str, end_marker: str) -> bool:
    """Return True when ``needle`` falls between ``start_marker`` and ``end_marker``."""
    start = text.find(start_marker)
    end = text.find(end_marker, start)
    at = text.find(needle, start)
    return start != -1 and end != -1 and start < at < end


def test_default_launch_mode_is_the_stable_safe_default(write_config) -> None:
    # The pre-selected launch mode is always the safe Desktop/bridge launch — it is
    # NOT auto-flipped to the experimental hosted ("browser") channel even when
    # claustrum is enabled (#686). The old ternary flip is gone.
    page = _client(write_config).get("/").text
    assert 'const DEFAULT_LAUNCH_MODE = "desktop";' in page
    assert 'CLAUSTRUM_ENABLED ? "browser"' not in page


def test_browser_mode_stays_available_and_experimental_badged(write_config) -> None:
    # Browser is still offered (when claustrum is on) and carries the experimental
    # badge — it is appended, not made the default pick, so Desktop leads the list.
    page = _client(write_config).get("/").text
    assert 'badge: "experimental"' in page
    # Appended (push), never displacing the safe default to the front (unshift).
    assert 'modes.unshift({ id: "browser"' not in page
    assert 'modes.push({ id: "browser"' in page


def test_more_options_disclosure_holds_mode_and_spawn(write_config) -> None:
    # One "More options" disclosure (#686), tucked away by default (x-show="mopen"),
    # holding BOTH the advanced Mode selector and Spawn for the Desktop launch.
    row = _client(write_config).get("/api/projects/alpha/row").text
    assert 'data-test="launch-more-options"' in row
    assert 'id="more-opts-alpha"' in row
    assert '@click="mopen = !mopen"' in row
    assert ">More options</span>" in row  # default (collapsed) label
    assert "mopen ? 'Fewer options' : 'More options'" in row  # toggles when expanded
    # The disclosure body is gated on mopen — Mode is tucked away, not shown by default.
    assert _between(row, 'id="more-opts-alpha"', 'x-show="mopen"', 'data-test="trust-confirm"')
    # Spawn lives inside the disclosure.
    assert _between(row, 'id="more-opts-alpha"', "spawnMode['alpha']", 'data-test="trust-confirm"')
    if sys.platform != "win32":
        # The Mode selector (Server Mode / Interactive Session) is tucked inside the
        # SAME disclosure rather than its own separate "Advanced" block.
        assert "Advanced" not in row  # the old separate disclosure label is gone
        assert _between(
            row, 'id="more-opts-alpha"', "resumeMode['alpha']", 'data-test="trust-confirm"'
        )
        assert "Server Mode (multi-session bridge)" in row
        assert "Interactive Session (single-session, true-resume)" in row


def test_trust_and_bypass_gates_fold_inside_the_popover(write_config) -> None:
    # The trust-on-start + bypass typed-confirm gates render INSIDE the launch popover
    # (#686), not as separate below-the-row alerts. Structurally: both confirm blocks
    # fall between the popover open and its closing markers, and there is exactly one
    # of each (the old below-the-row copies are removed).
    row = _client(write_config, _BYPASS_CEILING).get("/api/projects/alpha/row").text
    pop = 'class="card launch-pop"'
    # Semantic end marker (a rendered sentinel element), not template whitespace — so a
    # reformat / nesting change can't silently turn these structural assertions into
    # false-positives (the helper would otherwise not find a whitespace-coupled close).
    pop_close = 'data-test="launch-pop-end"'
    assert _between(row, pop, 'data-test="trust-confirm"', pop_close)
    assert _between(row, pop, 'data-test="bypass-confirm"', pop_close)
    assert row.count('data-test="trust-confirm"') == 1
    assert row.count('data-test="bypass-confirm"') == 1
    # The gates keep their security wiring: the trust checkbox gates "Trust & start",
    # and the bypass button stays disabled until the typed name matches exactly.
    assert ':disabled="!trustConfirmed[' in row
    assert ":disabled=\"(bypassTyped['alpha'] || '') !== 'alpha'\"" in row


def test_inline_action_error_has_a_stable_hook_outside_the_popover(write_config) -> None:
    # The persistent action-error block (errorOf) renders OUTSIDE the launch popover
    # (after the popover's end sentinel) and carries a stable `data-test="inline-error"`
    # hook. This disambiguates it from the two sibling `.alert-danger.mb-0` blocks now in
    # the row (the in-popover bypass-confirm and the spawn-failure detail) so a browser
    # E2E targeting a trust-on-start failure can select THIS block, not the first match.
    row = _client(write_config).get("/api/projects/alpha/row").text
    pop_close = 'data-test="launch-pop-end"'
    assert 'data-test="inline-error"' in row
    assert row.count('data-test="inline-error"') == 1
    # It lives below the popover, not inside it: the hook appears AFTER the end sentinel.
    # Guard the ordering check below: str.find returns -1 for a missing needle, so
    # without this the `> row.find(pop_close)` assertion passes vacuously if the
    # launch-pop-end sentinel is ever removed/renamed.
    assert pop_close in row
    assert row.find('data-test="inline-error"') > row.find(pop_close)
    # And it is the errorOf() alert (transient action error), x-text-escaped.
    inline = row.find('data-test="inline-error"')
    assert "errorOf('alpha')" in row[inline : inline + 200]


def test_run_button_yields_to_a_pending_gate(write_config) -> None:
    # While a confirm gate is pending the primary Run action hides so the gate's own
    # confirm button is the single next step; on a clean launch (no gate) launchRun()
    # resolves true and the popover closes.
    row = _client(write_config).get("/api/projects/alpha/row").text
    run = row.find('data-test="launch-run-go"')
    assert run != -1
    seg = row[run : run + 500]
    assert "!confirmTrust['alpha'] && !confirmBypass['alpha']" in seg
    # CSP build (#533): .then(arrow fn) replaced with launchRunAndClose — a dashboard()
    # method (launchRun is a sibling there) that closes via the launch-pop-close event.
    assert "launchRunAndClose('alpha', lmode, lperm, lcloud, lprompt)" in seg


def test_launch_run_keeps_popover_open_for_a_gate(write_config) -> None:
    # launchRun() signals the caller to keep the popover OPEN when start() opens a
    # trust/bypass gate (returns false), and the gate-resolving handlers dismiss the
    # popover via the launch-pop-close window event the row listens for.
    page = _client(write_config).get("/").text
    assert "if (this.confirmTrust[name] || this.confirmBypass[name]) return false;" in page
    assert "_closeLaunchPop(name) {" in page
    assert 'window.dispatchEvent(new CustomEvent("launch-pop-close"' in page
    assert "@launch-pop-close.window" in page


def test_confirm_trust_start_closes_popover_optimistically(write_config) -> None:
    # #748: confirmTrustStart() must dispatch start() WITHOUT awaiting the spawn
    # round-trip, then close the popover the same turn (unless start() re-opened a
    # gate) — otherwise the popover lingers open through the whole spawn. _spawn()
    # surfaces any failure on its own (inline error + toast), so the un-awaited start
    # still reports errors. Trust is already awaited+confirmed above the dispatch, so
    # closing early can't let an untrusted start through.
    page = _client(write_config).get("/").text
    handler = page[page.find("async confirmTrustStart(name) {") :]
    handler = handler[: handler.find("cancelTrust(name) {")]
    # trust() stays awaited (we branch on whether it succeeded).
    assert "await this.trust(name);" in handler
    # start() is dispatched, NOT awaited — that was the lingering-popover bug.
    assert "this.start(name);" in handler
    assert "await this.start(name);" not in handler
    # ...then the optimistic close, gated on no pending trust/bypass gate.
    gated_close = (
        "if (!this.confirmTrust[name] && !this.confirmBypass[name]) this._closeLaunchPop(name);"
    )
    assert gated_close in handler


def test_gate_open_moves_focus_into_the_gate(write_config) -> None:
    # When a gate opens the Run button hides, so focus must move into the gate's first
    # control (WCAG 2.4.3 / 4.1.3) rather than orphaning on <body>. Each gate carries an
    # x-effect that focuses its x-ref'd first control when its confirm flag flips on.
    row = _client(write_config, _BYPASS_CEILING).get("/api/projects/alpha/row").text
    assert 'x-ref="trustCheck"' in row
    # CSP build (#533): arrow fn replaced with focusTrustCheck / focusBypassInput methods
    assert "focusTrustCheck(" in row
    assert 'x-ref="bypassInput"' in row
    assert "focusBypassInput(" in row


def test_bypass_typed_value_is_cleared_when_the_gate_opens(write_config) -> None:
    # The bypass gate demands a fresh, deliberate re-type each time it opens — start()
    # clears bypassTyped right before raising confirmBypass, mirroring the trust gate's
    # per-open trustConfirmed reset. Without this a dismissed-then-reopened gate would
    # render "Start with bypass" already enabled from a stale typed value.
    page = _client(write_config).get("/").text
    assert 'this.bypassTyped[name] = "";' in page
    assert "this.confirmBypass[name] = true;" in page
    # The reset precedes raising the gate (deliberate re-type each open).
    assert page.index('this.bypassTyped[name] = "";') < page.index(
        "this.confirmBypass[name] = true;"
    )


def test_no_below_the_row_trust_or_bypass_alert(write_config) -> None:
    # Regression guard: the standalone below-the-row trust/bypass alert blocks are
    # gone — the only confirm gates live inside the popover (asserted above). The
    # error-of alert (a transient action error) is a distinct surface and stays.
    row = _client(write_config, _BYPASS_CEILING).get("/api/projects/alpha/row").text
    # The legacy below-the-row trust alert wrapped its body in an inner <div>; the
    # in-popover version is a single labelled group. Assert the in-popover markers are
    # the only trust/bypass confirm surfaces.
    assert row.count("I trust the files in this directory") == 1
    assert row.count("Type the project name to confirm.</p>") == 1
    assert 'x-show="errorOf' in row  # the transient-error alert is untouched


def _default_mode(mode: str, extra: str = "") -> str:
    return f"instance_defaults:\n  permission_mode: {mode}\n{extra}"


@pytest.mark.parametrize("mode", ["inherit", "bypassPermissions"])
@pytest.mark.parametrize("path", ["/", "/api/projects/alpha/row"])
def test_gated_default_without_ceiling_starts_at_ask_and_says_why(
    write_config, mode: str, path: str
) -> None:
    # TASK-480: the gated default has no <option> on a project without the ceiling, so
    # the select showed blank and Run sent a mode the server refuses (403). The row now
    # starts at "default" and says why, in a hint the select is described by. The grid
    # and the fragment route render the same row.
    page = _client(write_config, _default_mode(mode)).get(path).text
    row = page[page.index('data-project="alpha"') :]
    row = row[: row.index('data-test="launch-pop-end"')]
    assert "x-data=\"projectRow('alpha', 'default')\"" in row
    assert 'aria-describedby="perm-effect-alpha perm-gated-alpha"' in row
    note = row[row.index('data-test="perm-default-gated"') :]
    note = " ".join(note[: note.index("</p>")].split())
    label = PERMISSION_LABELS[mode]["short"]
    assert (
        f"The configured default, {label}, needs <code>allow_bypass_permissions</code> for"
        " this project, so it starts at Ask each time." in note
    )


@pytest.mark.parametrize(
    ("mode", "extra"),
    [("inherit", _BYPASS_CEILING), ("bypassPermissions", _BYPASS_CEILING), ("plan", "")],
)
def test_allowed_default_starts_at_the_configured_mode(write_config, mode, extra) -> None:
    # With the ceiling on (or a mode it does not gate) the configured default is offered,
    # so the row starts there and shows no note.
    row = _client(write_config, _default_mode(mode, extra)).get("/api/projects/alpha/row").text
    assert f"x-data=\"projectRow('alpha', '{mode}')\"" in row
    assert 'data-test="perm-default-gated"' not in row
    assert 'aria-describedby="perm-effect-alpha"' in row


def test_inherit_takes_the_same_typed_confirm_as_bypass(write_config) -> None:
    # TASK-480: start() opens the typed confirm for every ceiling-gated mode, and the
    # page carries the same set as the backend gate.
    page = _client(write_config, _BYPASS_CEILING).get("/").text
    assert f"const CEILING_GATED_MODES = {json.dumps(sorted(CEILING_GATED_MODES))};" in page
    start = page[page.index("async start(name) {") :]
    start = start[: start.index("await this._spawn(name);")]
    assert "if (CEILING_GATED_MODES.includes(this.permMode[name])) {" in start
    assert "this.confirmBypass[name] = true;" in start
    # The gate names the mode it confirms: inherit gets its own text and button label.
    row = page[page.index('data-project="alpha"') :]
    gate = row[row.index('data-test="bypass-confirm"') : row.index('data-test="launch-pop-end"')]
    assert "x-show=\"permMode['alpha'] === 'inherit'\"" in gate
    assert "x-show=\"permMode['alpha'] !== 'inherit'\"" in gate
    assert "If that is bypassPermissions, they run tools without asking." in gate
    assert "'Start with no forced mode' : 'Start with bypass'" in gate
    assert ":disabled=\"(bypassTyped['alpha'] || '') !== 'alpha'\"" in gate
    # The gate starts permMode, so the picker is locked while either gate is open.
    select = row[row.index('<select id="perm-alpha"') :]
    select = select[: select.index(">")]
    assert ":disabled=\"confirmTrust['alpha'] || confirmBypass['alpha']\"" in select
    radio = row[row.index(":name=\"'lm-alpha'\"") :]
    radio = radio[: radio.index(">")]
    assert ":disabled=\"confirmTrust['alpha'] || confirmBypass['alpha']\"" in radio


def test_leaving_desktop_never_snaps_back_to_bypass(write_config) -> None:
    # A bypassPermissions start mode is not a valid target when leaving Desktop.
    page = _client(write_config).get("/").text
    clamp = page[page.index("clampPermForMode() {") :]
    clamp = clamp[: clamp.index("\n      },")]
    assert 'this.lperm = startPerm === "bypassPermissions" ? "default" : startPerm;' in clamp


def test_typed_confirm_mismatch_names_the_gated_mode(write_config) -> None:
    # TASK-480: the inherit gate reuses the bypass typed confirm, so its mismatch error must
    # name No forced mode, as the gate's heading and button do (review nit on PR 1953).
    page = _client(write_config).get("/").text
    confirm = page[page.index("confirmBypassStart(name) {") :]
    confirm = confirm[: confirm.index("\n      },")]
    assert '(this.permMode[name] === "inherit" ? "No forced mode." : "bypass.")' in confirm
    assert '"Type the project name exactly to confirm bypass."' not in confirm
