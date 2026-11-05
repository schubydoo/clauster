"""Browser E2E for the header System-readiness pill in every state (TASK-291).

The pill used to render only when a check needed attention, so "every check passed" and
"the checks never ran" looked the same: no pill. These tests pin each state:

* **Real run** — the live ``/api/doctor`` answer drives the pill to a settled state (never
  ``checking`` or ``unavailable``), and "Show all checks" lists exactly the rows the API
  returned.
* **All passed** — a stubbed all-``ok`` answer shows a calm "N checks passed" pill, and the
  panel lists every row without a toggle.
* **Did not run** — a stubbed HTTP 500 after a pass, and a stubbed empty list, both show
  "Checks not run" with no rows: a failed re-check never leaves a stale pass on screen.

The stubs wrap ``window.fetch`` for ``/api/doctor`` only and then call the real
``loadReadiness()``, so the code under test is the dashboard's own load path.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _driver import AgentBrowser

pytestmark = pytest.mark.e2e

_ROOT = "Alpine.$data(document.querySelector('[x-data^=\"dashboard\"]'))"
_PILL = '[data-test="readiness-pill"]'
_PANEL = '[data-test="readiness-panel"]'
_ROW = '[data-test="readiness-row"]'
_TOGGLE = '[data-test="readiness-toggle-all"]'
_SUMMARY = '[data-test="readiness-summary"]'
_UNAVAILABLE = '[data-test="readiness-unavailable"]'
# The real doctor run spawns `claude --version` and `git`; give a slow runner headroom.
_DOCTOR_TIMEOUT_MS = 20_000
_SETTLED = ("passed", "warning", "blocking")

_ALL_OK = {
    "ok": True,
    "checks": [
        {"name": "config", "status": "ok", "detail": "loaded clauster.yml"},
        {"name": "claude", "status": "ok", "detail": "2.1.300 (>= 2.1.0)"},
        {"name": "binary:claustrum", "status": "ok", "detail": "claustrum v1.9.0 available"},
    ],
}


def _settle(browser: AgentBrowser) -> str:
    """Wait for the first real doctor answer and return the pill's state."""
    deadline = time.monotonic() + _DOCTOR_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        state = browser.get_attr(_PILL, "data-state") or ""
        if state in _SETTLED:
            return state
        time.sleep(0.25)
    raise AssertionError(f"readiness pill never settled; last state {state!r}")


def _stub_doctor(browser: AgentBrowser, status: int | None, body: dict | None = None) -> None:
    """Answer ``/api/doctor`` with ``status``/``body`` and re-run the real loader.

    ``status=None`` makes the request reject, as a network failure or a timeout does.
    """
    answer = (
        "Promise.reject(new TypeError('Failed to fetch'))"
        if status is None
        else "Promise.resolve(new Response(" + json.dumps(json.dumps(body)) + ","
        "{status:" + str(status) + ",headers:{'Content-Type':'application/json'}}))"
    )
    browser.eval_js(
        "(async function(){const d=" + _ROOT + ";"
        "if(!window.__realFetch)window.__realFetch=window.fetch;"
        "window.fetch=(u,o)=>String(u).endsWith('/api/doctor')?" + answer + ""
        ":window.__realFetch(u,o);"
        "await d.loadReadiness();return 1})()"
    )


def _live_text(browser: AgentBrowser) -> str:
    """The text of the header's visually-hidden readiness live region."""
    return browser.eval_json(
        "(()=>document.querySelector('[data-test=\"readiness-live\"]').textContent)()"
    )


# WCAG contrast of the pill's text against its background, at rest and on keyboard focus.
# Colours go through a 1x1 canvas so oklch/oklab computed values become sRGB bytes.
_CONTRAST_JS = """(async () => {
  const cv = document.createElement("canvas"); cv.width = cv.height = 1;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  const rgb = (c) => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = "#000"; cx.fillStyle = c;
    cx.fillRect(0, 0, 1, 1); return [...cx.getImageData(0, 0, 1, 1).data]; };
  const lum = ([r, g, b]) => { const f = (v) => { v /= 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
    return (x + 0.05) / (y + 0.05); };
  const pill = document.querySelector('[data-test="readiness-pill"]');
  const pageBg = getComputedStyle(document.body).backgroundColor;
  const measure = () => { const s = getComputedStyle(pill);
    const bg = s.backgroundColor === "rgba(0, 0, 0, 0)" ? pageBg : s.backgroundColor;
    return { color: s.color, bg, ratio: ratio(rgb(s.color), rgb(bg)) }; };
  const rest = measure();
  pill.focus();
  await new Promise((r) => setTimeout(r, 300));
  return { rest, focus: { ...measure(), focusVisible: pill.matches(":focus-visible") } };
})()"""


def _open_panel(browser: AgentBrowser) -> None:
    if not browser.is_visible(_PANEL):
        browser.click(_PILL)
    browser.expect_visible(_PANEL)


def test_real_doctor_answer_settles_the_pill_and_lists_every_row(
    browser: AgentBrowser, open_server: str
) -> None:
    """The live answer always shows a pill, and "all checks" lists the API's rows."""
    browser.goto(open_server)
    state = _settle(browser)
    api = browser.eval_json(
        "(async function(){const r=await fetch('/api/doctor');return await r.json()})()"
    )
    assert api["checks"], "the real doctor run returned no rows"

    _open_panel(browser)
    if state != "passed":
        # Issues first; the toggle then reveals the passing rows as well.
        browser.expect_visible(_TOGGLE)
        browser.click(_TOGGLE)
    browser.expect_count(_ROW, len(api["checks"]))
    passed = sum(1 for c in api["checks"] if c["status"] == "ok")
    browser.expect_text(_SUMMARY, f"{passed} of {len(api['checks'])}")


def test_all_passed_shows_a_calm_count_and_every_row(
    browser: AgentBrowser, open_server: str
) -> None:
    """Every check ok -> "3 checks passed", and the panel lists all three, no toggle."""
    browser.goto(open_server)
    _settle(browser)
    _stub_doctor(browser, 200, _ALL_OK)

    browser.expect_attr(_PILL, "data-state", "passed")
    browser.expect_text(_PILL, "3 checks passed")
    # Calm register: the passed pill is the neutral ghost button, not a state colour.
    assert "btn-ghost-secondary" in (browser.get_attr(_PILL, "class") or "")

    _open_panel(browser)
    browser.expect_count(_ROW, 3)
    browser.expect_count(f'{_ROW}[data-status="ok"]', 3)
    browser.expect_text(_PANEL, "claustrum")  # names WHICH checks ran (the F-1013 case)
    browser.expect_text(_SUMMARY, "3 of 3 checks passed")
    browser.expect_hidden(_TOGGLE)  # no issues -> nothing to filter
    browser.expect_hidden(_UNAVAILABLE)
    assert _live_text(browser) == "System readiness: 3 of 3 checks passed"


def test_failed_recheck_after_a_pass_shows_not_run_never_a_stale_pass(
    browser: AgentBrowser, open_server: str
) -> None:
    """A pass, then an HTTP 500 re-check -> "Checks not run" with no rows left over."""
    browser.goto(open_server)
    _settle(browser)
    _stub_doctor(browser, 200, _ALL_OK)
    browser.expect_attr(_PILL, "data-state", "passed")
    _open_panel(browser)
    browser.expect_count(_ROW, 3)

    _stub_doctor(browser, 500, {"detail": "boom"})
    browser.expect_attr(_PILL, "data-state", "unavailable")
    browser.expect_text(_PILL, "Checks not run")
    assert "passed" not in browser.get_text(_PILL)
    browser.expect_count(_ROW, 0)
    browser.expect_visible(_UNAVAILABLE)
    browser.expect_text(_SUMMARY, "Checks not run: the server answered HTTP 500")
    # The header live region (outside the panel) carries the failure, not the old pass.
    assert _live_text(browser) == (
        "System readiness: Checks not run: the server answered HTTP 500"
    )


def test_rejected_request_shows_not_run(browser: AgentBrowser, open_server: str) -> None:
    """A request that rejects (network failure, timeout) is "Checks not run" too."""
    browser.goto(open_server)
    _settle(browser)
    _stub_doctor(browser, None)

    browser.expect_attr(_PILL, "data-state", "unavailable")
    _open_panel(browser)
    browser.expect_visible(_UNAVAILABLE)
    browser.expect_text(_SUMMARY, "Checks not run: the request failed or timed out")
    browser.expect_count(_ROW, 0)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_not_run_pill_text_meets_aa_at_rest_and_on_keyboard_focus(
    browser: AgentBrowser, open_server: str, theme: str
) -> None:
    """The outline-warning pill text is >= 4.5:1 at rest and when focus fills it."""
    browser.goto(open_server)
    browser.eval_js(f"(()=>{{localStorage.setItem('clauster-theme','{theme}');return 1}})()")
    browser.reload()
    _settle(browser)
    _stub_doctor(browser, 500, {})
    browser.expect_attr(_PILL, "data-state", "unavailable")

    got = browser.eval_json(_CONTRAST_JS)
    assert got["focus"]["focusVisible"], got  # the filled focus state was exercised
    assert got["rest"]["ratio"] >= 4.5, got
    assert got["focus"]["ratio"] >= 4.5, got


def test_warning_stays_visible_and_toggle_reveals_the_passing_rows(
    browser: AgentBrowser, open_server: str
) -> None:
    """One warning -> "1 warning" on the pill; the panel shows it, then all rows on toggle."""
    warn = {"name": "git", "status": "warn", "detail": "git not on PATH"}
    browser.goto(open_server)
    _settle(browser)
    _stub_doctor(browser, 200, {"ok": True, "checks": [*_ALL_OK["checks"], warn]})

    browser.expect_attr(_PILL, "data-state", "warning")
    browser.expect_text(_PILL, "1 warning")
    _open_panel(browser)
    browser.expect_count(_ROW, 1)
    browser.expect_count(f'{_ROW}[data-status="warn"]', 1)
    browser.expect_text(_SUMMARY, "3 of 4 checks passed · 1 warning")
    browser.click(_TOGGLE)
    browser.expect_count(_ROW, 4)


def test_empty_check_list_is_not_a_pass(browser: AgentBrowser, open_server: str) -> None:
    """``ok: true`` with zero rows names no check that ran, so it is "Checks not run"."""
    browser.goto(open_server)
    _settle(browser)
    _stub_doctor(browser, 200, {"ok": True, "checks": []})

    browser.expect_attr(_PILL, "data-state", "unavailable")
    browser.expect_text(_PILL, "Checks not run")
    _open_panel(browser)
    browser.expect_visible(_UNAVAILABLE)
    browser.expect_count(_ROW, 0)
