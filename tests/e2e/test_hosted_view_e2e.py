"""Browser E2E: the hosted View panel's ended banner follows the LATEST snapshot row (#1467).

The banner used to be computed once, when the session ended, and stored on the view. An
open panel then kept that sentence for as long as it stayed open, so after a poll moved
the row somewhere Resume is not offered it still said "Use Resume above" beside a row
with no Resume button. The banner is now built from the row on every render.

The server runs with claustrum enabled and no daemon, so ``/ws/hosted/<id>`` closes every
socket as unknown and the panel reaches its ended state through the real ``onclose`` path.
The snapshot rows are real ``RemoteControlInstance`` dumps (the same shape
``/api/hosted`` serves), assigned to ``hosted`` with the poll stubbed out, which is what
``refresh()`` does with the response. Stream frames go through ``_renderHostedEvent``,
the handler the socket's ``onmessage`` calls.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import pytest

from clauster.models import RemoteControlInstance

if TYPE_CHECKING:
    from _driver import AgentBrowser

pytestmark = pytest.mark.e2e

_ROOT = "Alpine.$data(document.querySelector('[x-data^=\"dashboard\"]'))"
_PID = "01JE2EHOSTEDVIEW0000000000"
_REASON = '[data-test="hosted-ended-reason"]'
_RESUME_HINT = "Use Resume above"
# The same row can move between the Active and Recent zones as its status changes, and
# the banner re-renders inside whichever row is shown. Give the re-render room on a
# CPU-starved runner.
_SETTLE_TIMEOUT_MS = 10_000


def _row(**overrides: object) -> dict:
    """Return one hosted snapshot row, serialized the way ``/api/hosted`` serves it."""
    fields: dict[str, object] = {
        "project": "alpha",
        "label": "alpha",
        "channel": "hosted",
        "claustrum_process_id": _PID,
        "status": "crashed",
        "claude_session_uuid": str(uuid.uuid4()),
    }
    fields.update(overrides)
    return RemoteControlInstance(**fields).model_dump(mode="json")


def _set_rows(browser: AgentBrowser, *rows: dict) -> None:
    """Replace the dashboard's hosted snapshot, as one ``refresh()`` poll would."""
    browser.eval_js("(function(){" + _ROOT + ".hosted=" + json.dumps(list(rows)) + ";return 1})()")


def _frame(browser: AgentBrowser, event: dict) -> None:
    """Deliver one hosted stream event through the socket's ``onmessage`` handler."""
    browser.eval_js(
        "(function(){"
        + _ROOT
        + "._renderHostedEvent('"
        + _PID
        + "',"
        + json.dumps(event)
        + ");return 1})()"
    )


def _resume_buttons(browser: AgentBrowser) -> int:
    """Count the rendered Resume buttons (the row's ``x-if`` removes the node when off)."""
    return browser.eval_json(
        "(function(){return Array.from(document.querySelectorAll('button'))"
        ".filter(b=>b.offsetParent!==null&&b.textContent.trim()==='Resume').length})()"
    )


def test_hosted_ended_banner_rereads_the_latest_snapshot(
    browser: AgentBrowser, hosted_server: str
) -> None:
    """The banner names Resume exactly when the row renders it, across every poll."""
    browser.goto(hosted_server)
    browser.expect_visible('[data-project="alpha"]')
    # The 4s poll would replace `hosted` with the server's (empty) list; stub it so only
    # the rows this test assigns reach the page.
    browser.eval_js("(function(){" + _ROOT + ".refresh=async()=>{};return 1})()")

    # 1. A crashed row that keeps both halves of the Resume gate. It lands in Recent.
    _set_rows(browser, _row())
    browser.expect_visible('[data-test="recent-toggle"]')
    browser.click('[data-test="recent-toggle"]')
    browser.click('[data-test="hosted-view-toggle"]')
    # The server closes the unknown socket; `onclose` reads the crashed row and ends the view.
    browser.expect_text(_REASON, "The session crashed.", timeout_ms=_SETTLE_TIMEOUT_MS)
    assert _RESUME_HINT in browser.get_text(_REASON)
    assert _resume_buttons(browser) == 1

    # 2. The next poll reports the uuid as unusable (a restart that re-reads an off-shape
    # value, for example). The Resume button goes; the banner must stop pointing at it.
    _set_rows(browser, _row(claude_session_uuid=None))
    browser.expect_text(_REASON, "no usable conversation id", timeout_ms=_SETTLE_TIMEOUT_MS)
    assert _RESUME_HINT not in browser.get_text(_REASON)
    assert _resume_buttons(browser) == 0

    # 3. The same row reads resumable again: the hint comes back. Neither direction latches.
    _set_rows(browser, _row(error_detail="exit 1"))
    browser.expect_text(_REASON, _RESUME_HINT, timeout_ms=_SETTLE_TIMEOUT_MS)
    assert "exit 1" in browser.get_text(_REASON)
    assert _resume_buttons(browser) == 1

    # 4. A row the snapshot still reports as running (a terminal frame that beat the poll,
    # or a reconnect give-up on a stale row). The row moves to Active and renders no
    # Resume, so the banner must not name it, even with a usable uuid and a project.
    _set_rows(browser, _row(status="running"))
    browser.expect_text(_REASON, "This session is no longer live.", timeout_ms=_SETTLE_TIMEOUT_MS)
    assert _RESUME_HINT not in browser.get_text(_REASON)
    assert _resume_buttons(browser) == 0
    # ...and a row that has not ended is not told it "cannot be resumed" either, even when
    # it has lost its uuid. The error_detail marks the re-render so the check is not vacuous.
    _set_rows(browser, _row(status="starting", claude_session_uuid=None, error_detail="warming"))
    browser.expect_text(_REASON, "warming", timeout_ms=_SETTLE_TIMEOUT_MS)
    assert "cannot be resumed" not in browser.get_text(_REASON)
    _set_rows(browser, _row(status="running"))

    # 5. Two terminal frames with different text while the row still reads running: the
    # banner leads with the LATEST frame, since the snapshot has no phrasing to offer yet.
    _frame(browser, {"type": "exit", "exit_code": 3})
    browser.expect_text(_REASON, "Session ended (exit 3).", timeout_ms=_SETTLE_TIMEOUT_MS)
    _frame(browser, {"type": "lost", "reason": "daemon went away"})
    browser.expect_text(_REASON, "Stream lost — daemon went away", timeout_ms=_SETTLE_TIMEOUT_MS)

    # 6. The poll catches up: the snapshot's own phrasing and the Resume hint replace the
    # frame text, because the snapshot explains the end better than the frame does.
    _set_rows(browser, _row(status="stopped"))
    browser.expect_text(_REASON, "Session stopped.", timeout_ms=_SETTLE_TIMEOUT_MS)
    assert _RESUME_HINT in browser.get_text(_REASON)
    assert _resume_buttons(browser) == 1
