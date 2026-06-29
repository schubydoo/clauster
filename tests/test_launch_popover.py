"""Single-screen launch popover with one "More options" disclosure (#686).

The launch flow is reshaped so the advanced Mode selector (Server Mode /
Interactive Session) and Spawn sit under a single "More options" disclosure
(tucked away by default), the trust-on-start + bypassPermissions confirm gates
fold INSIDE the popover (no below-the-row alerts), and the default launch mode
stays the stable safe ``desktop`` choice — never auto-flipped to the
experimental hosted ``browser`` channel.
"""

from __future__ import annotations

import sys

from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import load_config

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


def test_run_button_yields_to_a_pending_gate(write_config) -> None:
    # While a confirm gate is pending the primary Run action hides so the gate's own
    # confirm button is the single next step; on a clean launch (no gate) launchRun()
    # resolves true and the popover closes.
    row = _client(write_config).get("/api/projects/alpha/row").text
    run = row.find('data-test="launch-run-go"')
    assert run != -1
    seg = row[run : run + 500]
    assert "!confirmTrust['alpha'] && !confirmBypass['alpha']" in seg
    assert ".then(done => { if (done) lopen = false })" in seg


def test_launch_run_keeps_popover_open_for_a_gate(write_config) -> None:
    # launchRun() signals the caller to keep the popover OPEN when start() opens a
    # trust/bypass gate (returns false), and the gate-resolving handlers dismiss the
    # popover via the launch-pop-close window event the row listens for.
    page = _client(write_config).get("/").text
    assert "if (this.confirmTrust[name] || this.confirmBypass[name]) return false;" in page
    assert "_closeLaunchPop(name) {" in page
    assert 'window.dispatchEvent(new CustomEvent("launch-pop-close"' in page
    assert "@launch-pop-close.window" in page


def test_gate_open_moves_focus_into_the_gate(write_config) -> None:
    # When a gate opens the Run button hides, so focus must move into the gate's first
    # control (WCAG 2.4.3 / 4.1.3) rather than orphaning on <body>. Each gate carries an
    # x-effect that focuses its x-ref'd first control when its confirm flag flips on.
    row = _client(write_config, _BYPASS_CEILING).get("/api/projects/alpha/row").text
    assert 'x-ref="trustCheck"' in row
    assert "$refs.trustCheck && $refs.trustCheck.focus()" in row
    assert 'x-ref="bypassInput"' in row
    assert "$refs.bypassInput && $refs.bypassInput.focus()" in row


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
    assert row.count("run tools without asking. Type the project name to confirm.") == 1
    assert 'x-show="errorOf' in row  # the transient-error alert is untouched
