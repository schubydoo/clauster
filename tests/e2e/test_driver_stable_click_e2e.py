"""Browser E2E: the driver waits for a moving target before it clicks (TASK-553).

agent-browser reads the target's centre and then dispatches the mouse event there. Its
native binary (0.43.0 and later) is fast enough to read the centre in the frame before a
deferred ``x-show`` moves the control, so the click lands on the container. These tests
move a real button from page script on every animation frame, so the result does not
depend on host speed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import _driver
import pytest

if TYPE_CHECKING:
    from _driver import AgentBrowser

pytestmark = pytest.mark.e2e

_PAGE = (
    "data:text/html,<!doctype html><body style='margin:0;height:1800px'>"
    "<button id='target' style='position:absolute;left:40px;top:40px;width:120px;height:40px'>"
    "Go</button></body>"
)

# Move the button once per frame, ``frames`` times (forever when negative), and record each
# mousedown with its target and whether the motion was still running.
_MOVE = """(function(frames){
  const b = document.getElementById('target');
  window.__downs = [];
  window.__moving = true;
  document.addEventListener('mousedown', (e) => {
    window.__downs.push({ onTarget: e.target === b, moving: window.__moving });
  }, true);
  let left = frames, top = 40;
  const step = () => {
    if (left === 0) { window.__moving = false; return; }
    left -= 1;
    top = top === 40 ? 340 : 40;
    b.style.top = top + 'px';
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  return 1;
})(%d)"""


def test_click_waits_until_the_target_stops_moving(browser: AgentBrowser) -> None:
    """The one click lands on the button, after the motion ended."""
    browser.goto(_PAGE)
    browser.eval_js(_MOVE % 45)
    browser.click("#target")
    assert browser.eval_json("window.__downs") == [{"onTarget": True, "moving": False}]


def test_fill_waits_until_the_field_is_visible(browser: AgentBrowser) -> None:
    """A field that a later frame reveals is filled, not refused as ``not_editable``."""
    browser.goto(_PAGE)
    browser.eval_js(
        "(function(){const i=document.createElement('input');i.id='late';"
        "i.style.display='none';document.body.appendChild(i);"
        "setTimeout(()=>{i.style.display='block'},400);return 1})()"
    )
    browser.fill("#late", "typed")
    browser.expect_value("#late", "typed")


def test_click_fails_closed_on_a_target_that_never_settles(
    browser: AgentBrowser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target that keeps moving fails the wait, and no click reaches the page."""
    monkeypatch.setattr(_driver, "_STABLE_TIMEOUT_MS", 800)
    browser.goto(_PAGE)
    browser.eval_js(_MOVE % -1)
    with pytest.raises(AssertionError, match="stable"):
        browser.click("#target")
    assert browser.eval_json("window.__downs") == []
