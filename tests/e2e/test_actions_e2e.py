"""Browser E2E for action-error surfacing and the live log tail (the TS-1 slice).

A follow-on slice to the earlier browser tests. These two port checklist rows that
need a real (fake-``claude``) bridge:

* **Action errors surface** — a *failed* action in the spawn flow must show an
  INLINE error on the project card (the ``errorOf`` ``.alert-danger`` block), not
  just a transient toast. The failure is induced for real: trust-on-start (the
  first step of a desktop spawn) writes ``HOME/.claude.json``, which the fixture
  obstructs (pre-creates it as a directory), so the trust POST returns 500 and the
  dashboard renders it in the persistent inline error block. (A fake-``claude``
  *crash* is surfaced instead as an error-*status* instance, not via ``errorOf`` —
  see the test docstring.)
* **Live log tail** — a running bridge streams its ``--debug-file`` over the WS;
  the panel populates, ANSI is stripped, and the session id / token on the
  bridge's deep-link line are redacted (clauster's ``sanitize_line``). The fake is
  told (``FAKE_CLAUDE_LOG_EXTRA=1``) to emit one ANSI-coloured line carrying a
  ``claude.ai/code/session_…`` link and a bearer token so the redaction is checked
  against a real streamed frame.

See ``tests/E2E_CHECKLIST.md`` for the full manual list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _helpers import STATUS_TIMEOUT, open_desktop_launch, trust_and_start

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e

# The fake's PTY-session id and the bearer token it logs only with FAKE_CLAUDE_LOG_EXTRA
# set (see tests/fixtures/fake_claude/claude) — these MUST NOT survive into the streamed
# view (redacted by sanitize_line over the WS).
_RAW_SESSION_ID = "session_01TESTPTYSESSIONAAAAA"
_RAW_TOKEN = "sk-FAKEFAKEFAKEFAKEFAKE"


def test_failed_action_surfaces_inline_error(
    browser: AgentBrowser, trust_fail_bridge_server: Server
) -> None:
    """A failed spawn-flow action shows the persistent inline ``.alert-danger`` error.

    Trust-on-start (the first step of a desktop spawn) is made to fail for real (the
    fixture obstructs ``HOME/.claude.json``), so the ``errorOf`` block — the persistent
    inline error, distinct from the transient toast — renders on the card with the
    server's detail, and the bridge never reaches Running.
    """
    browser.goto(trust_fail_bridge_server.url)
    browser.expect_visible('[data-project="gamma"]')

    open_desktop_launch(browser, "gamma")
    trust_and_start(browser, "gamma")

    # The inline error block (errorOf -> the persistent action-error alert, distinct from
    # the in-popover trust/bypass alerts and the spawn-failure detail, which all also carry
    # `.alert-danger.mb-0`) appears and carries a non-empty message. Target its stable
    # `data-test="inline-error"` hook — a bare `.alert-danger.mb-0` matches three blocks in
    # the row and `is visible` resolves the first (the hidden bypass-confirm), so it would
    # spuriously time out.
    error_block = '[data-project="gamma"] [data-test="inline-error"]'
    browser.expect_visible(error_block, timeout_ms=STATUS_TIMEOUT)
    assert browser.get_text(error_block).strip(), "inline error block rendered empty"

    # ...and it is genuinely persistent, independent of the toast. The failed action also
    # fires an error toast which, since #577, PERSISTS until dismissed (error toasts no
    # longer auto-dismiss; non-error toasts still do). Prove the inline block is its own
    # thing: dismiss the (persistent) error toast via its close button, confirm it clears
    # only on that explicit dismiss, and confirm the inline block is STILL shown.
    toast = ".toast-stack .alert-danger"
    browser.expect_visible(toast, timeout_ms=STATUS_TIMEOUT)  # error toast appears...
    # ...and clears only on explicit dismiss (no 4.5s auto-hide). Scope the close click to
    # the error toast's own button so a stray success toast can't be the one dismissed.
    browser.click(f"{toast} .btn-close")
    browser.expect_hidden(toast)
    browser.expect_visible(error_block)
    assert browser.get_text(error_block).strip(), "inline error block did not persist"
    # The failed action did not spawn a running bridge.
    assert "Running" not in browser.get_text("section.zone-active")


def test_live_log_tail_populates_and_redacts(
    browser: AgentBrowser, log_extra_bridge_server: Server
) -> None:
    """The live log panel streams lines, strips ANSI, and redacts the session id / token."""
    browser.goto(log_extra_bridge_server.url)
    browser.expect_visible('[data-project="gamma"]')

    # Trust-on-start a real (fake) bridge; it reaches RUNNING and surfaces in the
    # Active zone with the Logs toggle.
    open_desktop_launch(browser, "gamma")
    trust_and_start(browser, "gamma")
    browser.expect_text("section.zone-active", "Running", timeout_ms=STATUS_TIMEOUT)

    logs_btn = 'section.zone-active [data-test="bridge-logs-toggle"]'
    log_view = "section.zone-active pre.log-view"
    browser.click(logs_btn)
    browser.expect_visible(log_view)

    # The tail populates with the bridge's marker lines, then specifically with the
    # ANSI + deep-link line the fake emitted (its readable marker survives redaction).
    browser.expect_text(log_view, "[bridge:work]", timeout_ms=STATUS_TIMEOUT)
    browser.expect_text(log_view, "[bridge:link]", timeout_ms=STATUS_TIMEOUT)
    browser.expect_text(log_view, "Continue at")

    streamed = browser.get_text(log_view)
    # ANSI stripped: no raw escape byte survives into the streamed view.
    assert "\x1b" not in streamed, "ANSI escape not stripped from the streamed log"
    # Session id + token redacted (clauster sanitizes env_/session_ ids + bearer tokens
    # over the WS regardless of the on-disk redact setting).
    assert _RAW_SESSION_ID not in streamed, "session id leaked into the streamed log"
    assert _RAW_TOKEN not in streamed, "bearer token leaked into the streamed log"
    assert "redacted" in streamed.lower(), "expected a <redacted> marker in the streamed log"


def test_background_launch_requires_prompt_unless_cloud(
    browser: AgentBrowser, config_server: Server
) -> None:
    """Background launch blocks on a blank prompt unless cloud-registered (#1033).

    An unregistered background agent has no composer, so launched blank it would
    idle at "send a prompt to start" forever — the Run button stays disabled with
    an explanatory hint until a prompt is typed or the claude.ai opt-in is checked.
    """
    browser.goto(config_server.url)
    browser.expect_visible('[data-project="alpha"]')
    browser.click('[data-project="alpha"] [data-test="run-launch"]')
    browser.expect_visible('[data-project="alpha"] .launch-pop')
    browser.check('[data-project="alpha"] input[name="lm-alpha"][value="detached"]')

    run = '[data-project="alpha"] [data-test="launch-run-go"]'
    hint = '[data-project="alpha"] [data-test="launch-bg-prompt-required"]'
    cloud = '[data-project="alpha"] [data-test="launch-cloud-optin"]'

    # Blank prompt, not registered: blocked, with the explanatory hint.
    browser.expect_visible(hint)
    browser.expect_disabled(run)

    # Registering on claude.ai legitimizes a blank prompt (conversational there).
    browser.check(cloud)
    browser.expect_hidden(hint)
    browser.expect_enabled(run)

    # Un-register again: blocked again — then a typed prompt clears the block.
    browser.click(cloud)
    browser.expect_disabled(run)
    browser.fill('[id="lprompt-alpha"]', "count to three")
    browser.expect_hidden(hint)
    browser.expect_enabled(run)

    # #1114: the popover's text inputs must be UNMOUNTED, not merely hidden, while it is
    # shut. This row template renders once per project, so an x-show popover leaves a copy
    # of both fields in the DOM for every card — which is the one structural difference
    # left between these fields and the never-autofilled `np-*` singletons.
    # Deliberately page-global, not scoped to [data-project="alpha"]: the hypothesis is about
    # how many of these fields exist across the WHOLE document, which is the difference from
    # the singleton np-* controls. It assumes no other popover is open.
    fields = "input[id^='lprompt-'], input[id^='name-']"
    assert browser.get_count(fields) >= 1, (
        "control: the fields must exist while the popover is open"
    )
    browser.click('[data-project="alpha"] [data-test="run-launch"]')  # close it
    browser.expect_hidden('[data-project="alpha"] .launch-pop')
    # `get_count` returns 0 on a driver failure too, so a bare `== 0` can pass vacuously if the
    # browser died between the two calls. Prove the driver is still answering first.
    assert browser.get_count('[data-test="run-launch"]') >= 1, "control: the driver is still alive"
    assert browser.get_count(fields) == 0, (
        "closed popovers must leave no launch text inputs mounted"
    )
