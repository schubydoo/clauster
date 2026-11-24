"""Browser E2E: the driver waits for a settled target before it acts (TASK-553).

agent-browser reads the target's centre and then dispatches the mouse event there. Its
native binary (0.43.0 and later) is fast enough to read the centre in the frame before a
deferred ``x-show`` moves the control, so the click lands on the container. These tests
move or reveal a real control from page script, so the result does not depend on host
speed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import _driver
import pytest

if TYPE_CHECKING:
    from _driver import AgentBrowser

pytestmark = pytest.mark.e2e

_PAGE = (
    "data:text/html,<!doctype html><body style='margin:0;height:1800px'>"
    "<button id='target' style='position:absolute;left:40px;top:40px;width:120px;height:40px'>"
    "Go</button>"
    "<input type='checkbox' id='tick' style='position:absolute;left:240px;top:40px'>"
    "</body>"
)


_MOVE_JS = """(function(id, frames, period){
  const b = document.getElementById(id);
  window.__downs = [];
  window.__moving = true;
  document.addEventListener('mousedown', (e) => {
    window.__downs.push({ onTarget: e.target === b, moving: window.__moving });
  }, true);
  let left = frames, count = 0, top = 40;
  const step = () => {
    if (left === 0) { window.__moving = false; return; }
    left -= 1;
    count += 1;
    if (count % period === 0) { top = top === 40 ? 340 : 40; b.style.top = top + 'px'; }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  return 1;
})"""


def _move(element_id: str, *, frames: int, period: int = 1) -> str:
    """Build a script that moves the element every ``period`` frames and logs each mousedown.

    It runs for ``frames`` animation frames, or forever when ``frames`` is negative.
    """
    return f"{_MOVE_JS}({json.dumps(element_id)}, {frames}, {period})"


def test_click_waits_until_the_target_stops_moving(browser: AgentBrowser) -> None:
    """The one click lands on the button, after the motion ended."""
    browser.goto(_PAGE)
    browser.eval_js(_move("target", frames=45))
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


@pytest.mark.parametrize("period", [1, 2], ids=["every-frame", "every-second-frame"])
def test_click_fails_closed_on_a_target_that_never_settles(
    browser: AgentBrowser, monkeypatch: pytest.MonkeyPatch, period: int
) -> None:
    """A target that keeps moving fails the wait, and no click reaches the page."""
    monkeypatch.setattr(_driver, "_STABLE_TIMEOUT_MS", 800)
    browser.goto(_PAGE)
    browser.eval_js(_move("target", frames=-1, period=period))
    with pytest.raises(AssertionError, match="stable"):
        browser.click("#target")
    assert browser.eval_json("window.__downs") == []


def test_check_fails_closed_on_a_target_that_never_settles(
    browser: AgentBrowser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``check`` has the same guard: a moving checkbox stays unticked."""
    monkeypatch.setattr(_driver, "_STABLE_TIMEOUT_MS", 800)
    browser.goto(_PAGE)
    browser.eval_js(_move("tick", frames=-1))
    with pytest.raises(AssertionError, match="stable"):
        browser.check("#tick")
    assert browser.eval_json("document.getElementById('tick').checked") is False


def test_check_waits_for_a_hidden_target_to_get_a_box(browser: AgentBrowser) -> None:
    """A checkbox with no box yet is not stable, so the check runs once it is revealed."""
    browser.goto(_PAGE)
    browser.eval_js(
        "(function(){const t=document.getElementById('tick');t.style.display='none';"
        "window.__shown=false;t.addEventListener('click',()=>{window.__early=!window.__shown});"
        "setTimeout(()=>{window.__shown=true;t.style.display='block'},400);return 1})()"
    )
    browser.check("#tick")
    state = browser.eval_json(
        "({checked:document.getElementById('tick').checked,early:!!window.__early})"
    )
    assert state == {"checked": True, "early": False}


def test_click_fails_closed_when_a_visible_page_produces_no_frames(
    browser: AgentBrowser, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A starved frame is not proof of a settled box, so no click is dispatched."""
    monkeypatch.setattr(_driver, "_STABLE_TIMEOUT_MS", 1_500)
    browser.goto(_PAGE)
    browser.eval_js(_move("target", frames=0))
    browser.eval_js("(function(){window.requestAnimationFrame=()=>0;return 1})()")
    with pytest.raises(AssertionError, match="stable"):
        browser.click("#target")
    assert browser.eval_json("window.__downs") == []
