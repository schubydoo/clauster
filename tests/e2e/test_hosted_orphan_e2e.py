"""Browser E2E: an orphan Direct Session row stops saying "still running" (TASK-556).

The server restores one hosted row whose daemon no longer knows it, while a stand-in
agent with a matching start identity still runs, so the row is an orphan. The test ends
that agent and watches the dashboard. Before the fix nothing judged the row again, and
it kept the orphan badge and the "still running" text until the operator acted. The
dashboard's 4-second hosted poll now judges it again and the row becomes a lost row.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from _helpers import STATUS_TIMEOUT

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import OrphanHosted

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform != "linux", reason="the stand-in agent's ticks need /proc"),
]

_ORPHAN = '[data-test="hosted-orphan"]'
_REASON = '[data-test="hosted-ended-reason"]'


def test_orphan_row_turns_lost_when_its_agent_exits(
    browser: AgentBrowser, orphan_hosted_server: OrphanHosted
) -> None:
    """The badge and the "still running" text go once the agent is gone, without a click."""
    server = orphan_hosted_server
    browser.goto(server.url)
    browser.expect_visible('[data-test="recent-toggle"]', timeout_ms=STATUS_TIMEOUT)
    browser.click('[data-test="recent-toggle"]')
    browser.expect_visible(_ORPHAN, timeout_ms=STATUS_TIMEOUT)
    browser.click('[data-test="hosted-view-toggle"]')
    browser.expect_text(_REASON, "still running", timeout_ms=STATUS_TIMEOUT)

    server.agent.kill()
    server.agent.wait(timeout=5)

    browser.expect_hidden(_ORPHAN, timeout_ms=STATUS_TIMEOUT)
    browser.expect_text(_REASON, "the agent exited", timeout_ms=STATUS_TIMEOUT)
    reason = browser.get_text(_REASON)
    assert "still running" not in reason
    assert "Use Resume above" in reason  # the row keeps its project and conversation id
