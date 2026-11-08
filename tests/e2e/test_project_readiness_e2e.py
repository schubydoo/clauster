"""Browser E2E for the per-project readiness pill when its checks do not run (TASK-489).

The project row's readiness pill is silent when every check passes. Before TASK-489 it
was silent too when the preflight request failed, so "launch-ready" and "the checks
never ran" looked the same. Now a failed, rejected, hung or empty answer shows a
"Checks not run" pill, and a click on it runs the checks again.

The stubs replace ``window.fetch`` for the preflight URLs only and then call the real
loaders, so the real state handling and the real template are under test.
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
_ROW = '[data-project="alpha"]'
_WARN = f'{_ROW} [data-test="preflight-pill"]'
_NOT_RUN = f'{_ROW} [data-test="preflight-unavailable"]'
_DETAIL = f'{_ROW} [data-test="preflight-detail"]'
_BATCH = "/api/projects/preflight"
_ALPHA = "/api/projects/alpha/preflight"
_SETTLE_S = 15

_ALL_OK = {
    "ok": True,
    "checks": [
        {"name": "trust", "status": "ok", "detail": "workspace trusted"},
        {"name": "git", "status": "ok", "detail": "git repository"},
    ],
}
_MCP_WARN = {
    "ok": True,
    "checks": [
        *_ALL_OK["checks"],
        {"name": "mcp-approval", "status": "warn", "detail": "1 MCP server need approval"},
    ],
}


def _state(browser: AgentBrowser) -> str:
    return browser.eval_json("(()=>" + _ROOT + ".preflightState('alpha'))()")


def _settle(browser: AgentBrowser, server: str) -> None:
    """Load the page and wait for the real first-paint batch to answer."""
    browser.goto(server)
    browser.expect_visible(_ROW)
    deadline = time.monotonic() + _SETTLE_S
    while time.monotonic() < deadline:
        state = _state(browser)
        if state != "checking":
            # The real batch must have answered, or a later "not run" proves nothing.
            assert state != "unavailable", "the real first-paint batch already failed"
            return
        time.sleep(0.25)
    raise AssertionError("the first-paint preflight batch never answered")


def _answer(status: int | None, body: dict | None) -> str:
    if status is None:
        return "Promise.reject(new TypeError('Failed to fetch'))"
    return (
        "Promise.resolve(new Response(" + json.dumps(json.dumps(body)) + ","
        "{status:" + str(status) + ",headers:{'Content-Type':'application/json'}}))"
    )


def _stub(
    browser: AgentBrowser, url: str, status: int | None, body: dict | None = None, run: str = ""
) -> None:
    """Answer ``url`` with ``status``/``body`` (None rejects), then run ``run`` on the root."""
    browser.eval_js(
        "(async function(){const d=" + _ROOT + ";"
        "if(!window.__stubs)window.__stubs={};"
        "if(!window.__realFetch){window.__realFetch=window.fetch;"
        "window.fetch=(u,o)=>{const k=Object.keys(window.__stubs).find(s=>String(u).endsWith(s));"
        "return k?window.__stubs[k]():window.__realFetch(u,o)};}"
        "window.__stubs["
        + json.dumps(url)
        + "]=()=>"
        + _answer(status, body)
        + ";"
        + run
        + "return 1})()"
    )


def test_failed_batch_shows_not_run_and_a_recheck_clears_it(
    browser: AgentBrowser, open_server: str
) -> None:
    """HTTP 500 on the batch -> "Checks not run"; a passing re-check makes the row silent."""
    _settle(browser, open_server)
    _stub(browser, _BATCH, 500, {"detail": "boom"}, "await d.loadAllPreflight();")

    browser.expect_visible(_NOT_RUN)
    browser.expect_text(_NOT_RUN, "Checks not run")
    browser.expect_hidden(_WARN)
    title = browser.get_attr(_NOT_RUN, "title") or ""
    assert "the server answered HTTP 500" in title and "not a pass" in title, title

    # The click re-runs this project's check; an all-ok answer is the silent pass.
    _stub(browser, _ALPHA, 200, _ALL_OK)
    browser.click(_NOT_RUN)
    browser.expect_hidden(_NOT_RUN)
    browser.expect_hidden(_WARN)
    assert _state(browser) == "passed"


def test_rejected_per_project_request_shows_not_run(
    browser: AgentBrowser, open_server: str
) -> None:
    """A per-project request that rejects is "not run", and a warning re-check shows it."""
    _settle(browser, open_server)
    _stub(browser, _ALPHA, None, None, "await d.loadPreflight('alpha');")

    browser.expect_visible(_NOT_RUN)
    browser.expect_hidden(_WARN)
    assert "the request failed or timed out" in (browser.get_attr(_NOT_RUN, "title") or "")

    _stub(browser, _ALPHA, 200, _MCP_WARN)
    browser.click(_NOT_RUN)
    browser.expect_visible(_WARN)
    browser.expect_hidden(_NOT_RUN)
    browser.click(_WARN)
    browser.expect_visible(_DETAIL)


def test_empty_check_list_is_not_a_pass(browser: AgentBrowser, open_server: str) -> None:
    """A 200 answer with no check rows names nothing that ran, so it is "not run"."""
    _settle(browser, open_server)
    _stub(browser, _ALPHA, 200, {"ok": True, "checks": []}, "await d.loadPreflight('alpha');")

    browser.expect_visible(_NOT_RUN)
    browser.expect_hidden(_WARN)
    assert _state(browser) == "unavailable"


def test_batch_without_this_project_is_not_a_pass(browser: AgentBrowser, open_server: str) -> None:
    """A batch answer that leaves the project out shows "not run" on that row."""
    _settle(browser, open_server)
    _stub(browser, _BATCH, 200, {}, "await d.loadAllPreflight();")

    browser.expect_visible(_NOT_RUN)
    assert "no result for this project" in (browser.get_attr(_NOT_RUN, "title") or "")


def test_hung_request_reaches_not_run_at_the_deadline(
    browser: AgentBrowser, open_server: str
) -> None:
    """A request that never answers is aborted at the deadline and shows "not run"."""
    _settle(browser, open_server)
    browser.eval_js(
        "(function(){const d=" + _ROOT + ";d.preflightTimeoutMs=500;"
        "if(!window.__realFetch)window.__realFetch=window.fetch;"
        "window.fetch=(u,o)=>String(u).endsWith('" + _ALPHA + "')"
        "?new Promise((res,rej)=>{const s=o&&o.signal;"
        "if(s)s.addEventListener('abort',()=>rej(new DOMException('aborted','AbortError')))})"
        ":window.__realFetch(u,o);"
        "d.loadPreflight('alpha');return 1})()"
    )
    browser.expect_visible(_NOT_RUN)
    assert _state(browser) == "unavailable"


def test_failed_batch_keeps_a_stale_mcp_warning_for_the_launch_gate(
    browser: AgentBrowser, open_server: str
) -> None:
    """The row says "not run", but an earlier MCP-approval warning still soft-blocks."""
    _settle(browser, open_server)
    _stub(browser, _ALPHA, 200, _MCP_WARN, "await d.loadPreflight('alpha');")
    browser.expect_visible(_WARN)
    browser.click(_WARN)
    browser.expect_visible(_DETAIL)
    gate = (
        "(()=>{const d="
        + _ROOT
        + ";d.resumeMode['alpha']='pty';return d._mcpGateBlocks('alpha')})()"
    )

    _stub(browser, _BATCH, None, None, "await d.loadAllPreflight();")
    browser.expect_visible(_NOT_RUN)
    browser.expect_hidden(_WARN)
    browser.expect_hidden(_DETAIL)
    assert browser.eval_json(gate) is True

    # A re-check that fails too keeps the stale warning, so the gate still blocks.
    _stub(browser, _ALPHA, 500, {"detail": "boom"})
    browser.click(_NOT_RUN)
    browser.expect_visible(_NOT_RUN)
    assert "HTTP 500" in (browser.get_attr(_NOT_RUN, "title") or "")
    assert browser.eval_json(gate) is True


def test_a_late_failed_batch_does_not_override_a_newer_pass(
    browser: AgentBrowser, open_server: str
) -> None:
    """Only the newest request's answer lands: an older batch that fails later is dropped."""
    _settle(browser, open_server)
    browser.eval_js(
        "(function(){if(!window.__realFetch)window.__realFetch=window.fetch;"
        "window.fetch=(u,o)=>String(u).endsWith('" + _BATCH + "')"
        "?new Promise((res,rej)=>setTimeout(()=>rej(new TypeError('Failed to fetch')),400))"
        ":String(u).endsWith('" + _ALPHA + "')?" + _answer(200, _ALL_OK) + ""
        ":window.__realFetch(u,o);"
        "const d=" + _ROOT + ";d.loadAllPreflight();d.loadPreflight('alpha');return 1})()"
    )
    time.sleep(1.0)  # past the batch's rejection
    assert _state(browser) == "passed"
    browser.expect_hidden(_NOT_RUN)


def test_passing_recheck_moves_focus_and_announces_the_result(
    browser: AgentBrowser, open_server: str
) -> None:
    """The pill hides on a pass, so focus moves to Run and a toast names the result."""
    _settle(browser, open_server)
    _stub(browser, _ALPHA, 500, {}, "await d.loadPreflight('alpha');")
    browser.expect_visible(_NOT_RUN)
    _stub(browser, _ALPHA, 200, _ALL_OK)
    browser.eval_js(
        "(async function(){const el=document.querySelector('" + _NOT_RUN + "');"
        "el.focus();await " + _ROOT + ".recheckPreflight('alpha');return 1})()"
    )
    browser.expect_hidden(_NOT_RUN)
    focused = browser.eval_json("(()=>document.activeElement.dataset.test||'')()")
    assert focused == "run-launch", focused
    browser.expect_text("body", "alpha: readiness checks passed")


# WCAG contrast of the pill's text against its background (1x1 canvas -> sRGB bytes).
_CONTRAST_JS = """(() => {
  const cv = document.createElement("canvas"); cv.width = cv.height = 1;
  const cx = cv.getContext("2d", { willReadFrequently: true });
  const rgb = (c) => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = "#000"; cx.fillStyle = c;
    cx.fillRect(0, 0, 1, 1); return [...cx.getImageData(0, 0, 1, 1).data]; };
  const lum = ([r, g, b]) => { const f = (v) => { v /= 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const pill = document.querySelector(
    '[data-project="alpha"] [data-test="preflight-unavailable"]');
  const s = getComputedStyle(pill);
  // A translucent tint composes over the card, so blend it before measuring.
  const card = getComputedStyle(pill.closest(".card") || document.body).backgroundColor;
  const [br, bg, bb, ba] = rgb(s.backgroundColor), [cr, cg, cb] = rgb(card), a = ba / 255;
  const back = [br * a + cr * (1 - a), bg * a + cg * (1 - a), bb * a + cb * (1 - a)];
  const [x, y] = [lum(rgb(s.color)), lum(back)].sort((p, q) => q - p);
  return { color: s.color, bg: s.backgroundColor, card, ratio: (x + 0.05) / (y + 0.05) };
})()"""


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_not_run_pill_text_meets_aa(browser: AgentBrowser, open_server: str, theme: str) -> None:
    """The "Checks not run" pill text is >= 4.5:1 against its background in both themes."""
    browser.goto(open_server)
    browser.eval_js(f"(()=>{{localStorage.setItem('clauster-theme','{theme}');return 1}})()")
    browser.reload()
    _settle(browser, open_server)
    _stub(browser, _ALPHA, 500, {}, "await d.loadPreflight('alpha');")
    browser.expect_visible(_NOT_RUN)
    got = browser.eval_json(_CONTRAST_JS)
    assert got["ratio"] >= 4.5, got
