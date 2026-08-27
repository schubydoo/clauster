"""Browser E2E: an ``x-show`` flipped hide→show→hide inside ONE animation frame ends HIDDEN.

Alpine core (the vendored 3.15.12) applies every non-first ``x-show`` toggle on the next
animation frame through a hide cascade that loses the final hide when two hides race one
show inside a single frame; the directive's ``value === oldValue`` guard then never
re-evaluates, so the element stays visible with data that says hidden. ``adopt()`` produces
exactly that sequence (optimistic hide → the trailing ``refresh()`` re-shows because the
server hasn't re-attributed yet → the next poll hides), and a CPU-starved headless Chrome
sees it for any pair of updates — the ``test_adopt_external_standard_bridge_becomes_managed``
flake that exhausted its reruns. ``_dashboard_script.html`` installs a last-value-wins
replacement for the cascade; this pins it.

The race case drives the three flips from page script with only microtasks between them,
so no frame can land between the steps however fast or slow the host is — it is
deterministic, not load-dependent. The control case leaves a real frame between the flips
and passes with or without the fix: it proves the probe can see a hide at all.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e

_ROOT = "Alpine.$data(document.querySelector('[x-data^=\"dashboard\"]'))"
_EXT = "{alpha:[{pid:1,cwd:'/x',kind:'interactive',state:'running',started_at:0,local_uuid:'u'}]}"
_BTN = '[data-project="alpha"] [data-test="adopt-btn"]'
_IND = '[data-project="alpha"] [data-test="external-indicator"]'


def _display(browser: AgentBrowser) -> dict[str, str]:
    """Computed ``display`` of the indicator + Manage button, plus the data they render."""
    raw = browser.eval_js(
        "(function(){const d=" + _ROOT + ";return JSON.stringify({"
        "adopt:d.adoptableProjects.join(','),ext:Object.keys(d.externalSessions).join(','),"
        "btn:getComputedStyle(document.querySelector('" + _BTN + "')).display,"
        "ind:getComputedStyle(document.querySelector('" + _IND + "')).display})})()"
    )
    state = json.loads(raw)
    # `eval` prints the JS string JSON-encoded, so a string result is double-encoded.
    return json.loads(state) if isinstance(state, str) else state


@pytest.mark.parametrize(
    "gap",
    [
        pytest.param("await new Promise(r=>setTimeout(r,120))", id="control-frame-between"),
        pytest.param("for(let i=0;i<8;i++)await Promise.resolve()", id="race-same-frame"),
    ],
)
def test_xshow_hide_show_hide_in_one_frame_ends_hidden(
    browser: AgentBrowser, bridge_server: Server, gap: str
) -> None:
    """After hide→show→hide the DOM agrees with the data: both elements are hidden."""
    browser.goto(bridge_server.url)
    browser.expect_visible('[data-project="alpha"]')
    # Start SHOWN, so every later toggle takes the deferred (non-first) cascade path.
    browser.eval_js(
        "(function(){const d=" + _ROOT + ";d.externalSessions=" + _EXT + ";"
        "d.adoptableProjects=['alpha'];return 1})()"
    )
    browser.expect_visible(_BTN)

    browser.eval_js(
        "(async function(){const d=" + _ROOT + ";const ext=" + _EXT + ";"
        "const tick=async()=>{" + gap + "};"
        "d.externalSessions={};d.adoptableProjects=[];await tick();"
        "d.externalSessions=ext;d.adoptableProjects=['alpha'];await tick();"
        "d.externalSessions={};d.adoptableProjects=[];await tick();return 'ok'})()"
    )
    # Give the deferred toggle (next animation frame) ample time to land.
    time.sleep(1.5)
    state = _display(browser)
    assert state["adopt"] == "" and state["ext"] == "", state
    assert state["btn"] == "none", state
    assert state["ind"] == "none", state
