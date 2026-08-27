"""Shared helpers for the browser E2E suite (launch popover, trust gate, argv reads).

Extracted from the near-identical private copies that had grown across six test
modules (#763): one definition, one fix. Imported as ``from _helpers import …`` —
the e2e dir rides pytest's rootdir sys.path insertion, same as ``_driver``. Plain
functions (not fixtures), so they live beside ``conftest.py`` rather than in it.

Selector policy (#763): prefer the templates' stable ``data-test`` hooks over
Tabler utility-class chains — a CSS/markup refactor must not read as a spurious
E2E failure.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from _driver import AgentBrowser

# A bridge spawn waits on the fake claude writing its readiness markers, then the
# dashboard's 4s poll reconciles state — give status transitions generous headroom.
STATUS_TIMEOUT = 20_000

# Stopped bridges live in the collapsed "Recent / resumable" group behind this toggle
# (its text carries the count).
RECENT_TOGGLE = 'section.zone-active [data-test="recent-toggle"]'

# Session-row lifecycle controls (Active zone Stop / Recent zone Resume).
STOP_BUTTON = 'section.zone-active [data-test="stop-session"]'
RESUME_BUTTON = 'section.zone-active [data-test="resume-session"]'


def read_launch_argv(state_dir: Path, project: str) -> list[str]:
    """Return the argv of the LATEST fake ``claude`` spawned for ``project``.

    The fake writes its argv to ``<debug-file>.argv.json`` beside each bridge log
    (``state_dir/logs/<project>-<ms>.log``); the ms timestamps are fixed-width so a
    lexicographic sort yields launch order and ``[-1]`` is the newest spawn.
    """
    argv_files = sorted((state_dir / "logs").glob(f"{project}-*.log.argv.json"))
    assert argv_files, f"no launch argv recorded for {project} under {state_dir / 'logs'}"
    return json.loads(argv_files[-1].read_text())


def open_desktop_launch(browser: AgentBrowser, project: str) -> None:
    """Open the project's launch popover and select the desktop (bridge) mode."""
    browser.click(f'[data-project="{project}"] [data-test="run-launch"]')
    browser.expect_visible(f'[data-project="{project}"] .launch-pop')
    browser.check(f'[data-project="{project}"] input[name="lm-{project}"][value="desktop"]')


def click_run(browser: AgentBrowser, project: str, *, expect_close: bool = True) -> None:
    """Click the popover's primary Run button and, on a clean launch, wait for it to close.

    ``launchRunAndClose`` closes the popover only when the spawn POST has resolved, which
    for a slow bridge is well AFTER the dashboard's poll already shows the new row — so a
    caller that gates on the row and then clicks "Run Claude here" again finds the popover
    still open and toggles it CLOSED. That window is invisible with a ~150 ms-per-command
    CLI and reliably hit with a ~1 ms one (agent-browser ≥ 0.27.2). Waiting for the close
    here makes the contract explicit instead of timing-dependent. ``expect_close=False``
    is for a launch that opens a gate INSIDE the popover (trust / bypass / MCP), which by
    design keeps it open.
    """
    browser.click(f'[data-project="{project}"] [data-test="launch-run-go"]')
    if expect_close:
        browser.expect_hidden(f'[data-project="{project}"] .launch-pop', timeout_ms=STATUS_TIMEOUT)


def trust_and_start(browser: AgentBrowser, project: str, *, run_first: bool = True) -> None:
    """Click Run (unless the caller already did), then satisfy the trust gate.

    The Trust & start button starts disabled; ticking the "I trust the files"
    checkbox enables it. ``run_first=False`` lets a caller that drove the Run
    click itself (e.g. after picking non-default launch options) reuse just the
    gate half.
    """
    if run_first:
        click_run(browser, project, expect_close=False)  # the trust gate keeps it open
    trust = f'[data-project="{project}"] [data-test="trust-confirm"]'
    trust_start = f"{trust} button.btn-warning"
    browser.expect_visible(trust_start)
    browser.expect_disabled(trust_start)
    browser.check(f'{trust} input[type="checkbox"]')
    browser.expect_enabled(trust_start)
    browser.click(trust_start)
