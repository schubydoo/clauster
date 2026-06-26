"""A thin pytest driver over the ``agent-browser`` CLI for the E2E suite.

The browser E2E suite drives a REAL headless Chromium against a REAL ``clauster``
subprocess (see :mod:`conftest`). This module wraps Vercel's ``agent-browser`` CLI
— a stateful, session-based browser driver — behind a small Python class whose
methods map onto the Playwright calls the suite used before.

``agent-browser`` state queries (``is visible``, ``get ...``) are one-shot, unlike
Playwright's auto-retrying ``expect()``. The ``expect_*`` helpers here close that gap
by polling a predicate until it holds or a timeout elapses, so a test never races the
server's async render. Target elements by CSS selector or, for controls best named by
their accessible label, by ARIA role + name (``*_role`` helpers).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

# The CLI is installed globally (``agent-browser install`` in scripts/e2e.sh / CI).
_BIN = "agent-browser"

# Vendored, network-free axe-core (pinned). Registered as a page init script (via the
# ``AGENT_BROWSER_INIT_SCRIPTS`` env the driver injects into every subcommand) so
# ``window.axe`` exists before the first navigation — the a11y smoke tests then run
# ``axe.run`` in-page. Committed so the suite needs no network.
_AXE_SCRIPT = Path(__file__).resolve().parent / "vendor" / "axe.min.js"

# Auto-accept native dialogs (window.confirm/alert/prompt). The dashboard guards
# destructive actions with window.confirm (e.g. the Stop button, #577) and agent-browser
# has no dialog API, so without this a confirm()-ing click fails. Registered as a second
# init script (AGENT_BROWSER_INIT_SCRIPTS is comma-separated) so it runs before app code.
_DIALOG_SCRIPT = Path(__file__).resolve().parent / "auto_accept_dialogs.js"

# Poll cadence for the expect_* helpers; the per-call timeout is the caller's (the
# suite's _STATUS_TIMEOUT / _GATE_TIMEOUT constants), defaulting to 5s.
_DEFAULT_TIMEOUT_MS = 5_000
_POLL_INTERVAL_S = 0.2


class AgentBrowser:
    """Drive one ``agent-browser`` session for the lifetime of a single test."""

    def __init__(self) -> None:
        """Construct the driver; :meth:`goto` opens the first page."""

    # ----- process plumbing ----------------------------------------------

    def _run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        """Run one ``agent-browser`` subcommand and capture its output.

        Injects ``AGENT_BROWSER_INIT_SCRIPTS`` (comma-separated) so two page init
        scripts are registered before the first navigation (every subcommand inherits
        them, including ``open``): the vendored axe-core (``window.axe`` for the a11y
        smoke tests, no network fetch) and the dialog auto-accepter (so a
        ``window.confirm``-guarded destructive action can be driven). Harmless for the
        subcommands that use neither.
        """
        env = dict(os.environ)
        init_scripts = [s for s in (_AXE_SCRIPT, _DIALOG_SCRIPT) if s.exists()]
        if init_scripts:
            env["AGENT_BROWSER_INIT_SCRIPTS"] = ",".join(str(s) for s in init_scripts)
        return subprocess.run(
            [_BIN, *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=60,
            env=env,
        )

    # ----- navigation -----------------------------------------------------

    def goto(self, url: str) -> None:
        """Open ``url`` and best-effort wait for the network to settle.

        A tall viewport keeps the per-project launch popover (a long floated panel:
        mode radios → permissions → Advanced → the bottom "Run" button) fully on
        screen. A real CDP click hit-tests at the element's centre, so a control
        below the fold gets ``elementFromPoint() == null`` and the click silently
        lands nowhere — the popover's submit button is the one that bites.
        """
        self._run("set", "viewport", "1280", "2000", check=True)
        self._run("open", url, check=True)
        # Best-effort settle; the expect_* pollers absorb any residual async render.
        self._run("wait", "--load", "networkidle")

    def reload(self) -> None:
        """Reload the current page."""
        self._run("reload", check=True)
        self._run("wait", "--load", "networkidle")

    # ----- one-shot state queries (raw; prefer the expect_* helpers) ------

    def is_visible(self, selector: str) -> bool:
        """Whether ``selector`` matches a VISIBLE element (False if absent)."""
        # `is visible` prints "true"/"false" with exit 0 when the element exists, and
        # errors with a non-zero exit when it does not — both collapse to "not visible".
        return self._run("is", "visible", selector).stdout.strip() == "true"

    def is_enabled(self, selector: str) -> bool:
        """Whether ``selector`` matches an ENABLED element (False if absent)."""
        return self._run("is", "enabled", selector).stdout.strip() == "true"

    def get_text(self, selector: str) -> str:
        """Visible text of ``selector`` (empty string if absent)."""
        result = self._run("get", "text", selector)
        return result.stdout.strip() if result.returncode == 0 else ""

    def get_attr(self, selector: str, name: str) -> str | None:
        """Value of attribute ``name`` on ``selector`` (None if absent)."""
        result = self._run("get", "attr", selector, name)
        return result.stdout.strip() if result.returncode == 0 else None

    def get_value(self, selector: str) -> str:
        """Current value of an input ``selector`` (empty string if absent)."""
        result = self._run("get", "value", selector)
        return result.stdout.strip() if result.returncode == 0 else ""

    def get_count(self, selector: str) -> int:
        """Number of elements matching ``selector`` (0 if none / on error)."""
        # Gate on a clean exit and a strictly-numeric payload: parsing the first
        # integer out of arbitrary stdout would let digits in an error message
        # masquerade as a count and produce false assertions.
        result = self._run("get", "count", selector)
        out = result.stdout.strip()
        return int(out) if result.returncode == 0 and out.isdigit() else 0

    def get_url(self) -> str:
        """Current page URL."""
        return self._run("get", "url").stdout.strip()

    def _interactive_refs(self) -> list[dict[str, str]]:
        """The interactive-element refs ({role, name}) from a JSON snapshot."""
        result = self._run("snapshot", "-i", "--json")
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return []
        refs = (payload.get("data") or {}).get("refs") or {}
        return list(refs.values())

    def role_visible(self, role: str, name: str) -> bool:
        """Whether an interactive element of ``role`` with accessible ``name`` is present.

        Backed by the interactive snapshot (``snapshot -i``), which lists only
        visible, interactive nodes — the agent-browser analogue of Playwright's
        ``get_by_role(role, name=...)`` visibility check, for controls that have no
        stable id/class to target by CSS. Like Playwright's default, ``name`` is matched
        case-insensitively as a substring (pass a full label for an unambiguous match).
        """
        wanted = name.casefold()
        return any(
            r.get("role") == role and wanted in (r.get("name") or "").casefold()
            for r in self._interactive_refs()
        )

    # ----- interactions ---------------------------------------------------

    def click(self, selector: str) -> None:
        """Click the element matching ``selector``."""
        self._run("click", selector, check=True)

    def click_role(self, role: str, name: str) -> None:
        """Click an element found by ARIA ``role`` + accessible ``name``.

        Note: a role-found click does not reliably trigger native form submission —
        for a ``type=submit`` button prefer :meth:`click` with a CSS selector, which
        issues a real CDP mouse click.
        """
        self._run("find", "role", role, "click", name, check=True)

    def fill(self, selector: str, text: str) -> None:
        """Clear ``selector`` and type ``text`` into it."""
        self._run("fill", selector, text, check=True)

    def check(self, selector: str) -> None:
        """Check the checkbox matching ``selector``."""
        self._run("check", selector, check=True)

    def select(self, selector: str, value: str) -> None:
        """Select option ``value`` in the ``<select>`` matching ``selector``."""
        self._run("select", selector, value, check=True)

    def eval_js(self, script: str) -> str:
        """Evaluate ``script`` in the page and return its stdout."""
        return self._run("eval", script, check=True).stdout.strip()

    def eval_json(self, script: str):
        """Evaluate ``script`` and parse its result as JSON.

        ``agent-browser eval`` serializes the evaluated value as JSON on stdout (an
        object/array comes back as a parseable JSON document). Have the script *return
        the value* (an object or array — not ``JSON.stringify(...)``, which would
        double-encode into a JSON string) and this parses it in one step. Awaited
        promises resolve first, so an ``async`` IIFE (e.g. ``axe.run``) works directly.
        """
        out = self._run("eval", script, check=True).stdout.strip()
        return json.loads(out)

    # ----- polling assertions (replace Playwright's auto-retrying expect) -

    def _poll(self, predicate, timeout_ms: int, describe: str) -> None:
        """Poll ``predicate`` until truthy or ``timeout_ms`` elapses, else fail."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(_POLL_INTERVAL_S)
        raise AssertionError(f"timed out after {timeout_ms}ms waiting for: {describe}")

    def expect_visible(self, selector: str, timeout_ms: int = _DEFAULT_TIMEOUT_MS) -> None:
        """Assert ``selector`` becomes visible within ``timeout_ms``."""
        self._poll(lambda: self.is_visible(selector), timeout_ms, f"{selector} visible")

    def expect_hidden(self, selector: str, timeout_ms: int = _DEFAULT_TIMEOUT_MS) -> None:
        """Assert ``selector`` becomes hidden/absent within ``timeout_ms``."""
        self._poll(lambda: not self.is_visible(selector), timeout_ms, f"{selector} hidden")

    def expect_enabled(self, selector: str, timeout_ms: int = _DEFAULT_TIMEOUT_MS) -> None:
        """Assert ``selector`` becomes enabled within ``timeout_ms``."""
        self._poll(lambda: self.is_enabled(selector), timeout_ms, f"{selector} enabled")

    def expect_disabled(self, selector: str, timeout_ms: int = _DEFAULT_TIMEOUT_MS) -> None:
        """Assert ``selector`` becomes disabled within ``timeout_ms``."""
        self._poll(
            lambda: self.is_visible(selector) and not self.is_enabled(selector),
            timeout_ms,
            f"{selector} disabled",
        )

    def expect_text(
        self, selector: str, substring: str, timeout_ms: int = _DEFAULT_TIMEOUT_MS
    ) -> None:
        """Assert ``selector``'s text contains ``substring`` within ``timeout_ms``."""
        self._poll(
            lambda: substring in self.get_text(selector),
            timeout_ms,
            f"{selector} to contain {substring!r}",
        )

    def expect_attr(
        self, selector: str, name: str, value: str, timeout_ms: int = _DEFAULT_TIMEOUT_MS
    ) -> None:
        """Assert attribute ``name`` of ``selector`` equals ``value`` within ``timeout_ms``."""
        self._poll(
            lambda: self.get_attr(selector, name) == value,
            timeout_ms,
            f"{selector}[{name}] == {value!r}",
        )

    def expect_count(
        self, selector: str, count: int, timeout_ms: int = _DEFAULT_TIMEOUT_MS
    ) -> None:
        """Assert exactly ``count`` elements match ``selector`` within ``timeout_ms``."""
        self._poll(
            lambda: self.get_count(selector) == count,
            timeout_ms,
            f"{selector} count == {count}",
        )

    def expect_url(
        self, expected: str | re.Pattern[str], timeout_ms: int = _DEFAULT_TIMEOUT_MS
    ) -> None:
        """Assert the URL equals ``expected`` (str) or matches it (compiled regex)."""

        def matches() -> bool:
            url = self.get_url()
            return (
                bool(expected.search(url)) if isinstance(expected, re.Pattern) else url == expected
            )

        self._poll(matches, timeout_ms, f"url == {expected!r}")

    def expect_role_visible(
        self, role: str, name: str, timeout_ms: int = _DEFAULT_TIMEOUT_MS
    ) -> None:
        """Assert a ``role``/``name`` interactive element appears within ``timeout_ms``."""
        self._poll(lambda: self.role_visible(role, name), timeout_ms, f"{role} {name!r} visible")

    def expect_role_hidden(
        self, role: str, name: str, timeout_ms: int = _DEFAULT_TIMEOUT_MS
    ) -> None:
        """Assert no ``role``/``name`` interactive element is present within ``timeout_ms``."""
        self._poll(
            lambda: not self.role_visible(role, name), timeout_ms, f"{role} {name!r} hidden"
        )

    # ----- diagnostics ----------------------------------------------------

    def screenshot(self, path: str) -> bool:
        """Best-effort screenshot of the current page to ``path``; return success.

        Used by the failure hook to capture what the headless browser last showed
        (invaluable when a CI run fails with no display). Deliberately never raises:
        it runs during teardown of an already-failing test, so a capture error must
        not mask the real failure — the caller logs the miss instead.
        """
        try:
            return self._run("screenshot", path).returncode == 0
        except (subprocess.SubprocessError, OSError):
            return False

    # ----- teardown -------------------------------------------------------

    def close(self) -> None:
        """Close every browser session.

        Unconditional and idempotent (``close --all`` is a harmless no-op when no
        session is open): a test that takes the ``browser`` fixture but never reaches
        :meth:`goto` (or fails before it) still resets the shared daemon, so a prior
        test's cookies/storage can't leak into the next. Fails loud on a non-zero exit
        (``close --all`` exits 0 even when nothing is open) so a broken teardown can't
        silently leak session state across tests.
        """
        self._run("close", "--all", check=True)
