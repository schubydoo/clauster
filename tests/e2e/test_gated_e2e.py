"""Browser E2E for the two opt-in safety gates: bypassPermissions + ghost-reaper UI.

These port the **bypassPermissions footgun** and **Ghost-environment reaper UI** rows
from ``tests/E2E_CHECKLIST.md``. Both are pure render/route gating — no ``claude``
bridge is ever spawned:

* **bypassPermissions** — the dangerous ``bypassPermissions`` permission option must
  render *only* for a project that opts in via
  ``projects.<name>.allow_bypass_permissions: true``, and selecting it must force a
  typed-name confirmation before a session can start. We drive the gate up to (and
  including) the wrong-name rejection, then stop — never reaching a real spawn.
* **ghost-reaper** — the dashboard reaper panel and its ``/api/environments/ghosts``
  endpoint must appear only when ``reaper.ui_enabled: true``; with the flag unset the
  panel is absent and the endpoint 404s. The ghost-list / archive / typed-DELETE flow
  needs live cloud-environment data and stays manual.

The OFF cases ride the shared, read-only ``open_server``; the ON cases use the
``reaper_server`` (read-only) and ``bypass_server`` (function-scoped — its typed
confirm trusts ``gamma``, a mutation that must not leak between tests).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Page, expect

if TYPE_CHECKING:
    from .conftest import Server

pytestmark = pytest.mark.e2e

# CSS for the footgun permission option, scoped per-card by a `[data-project=...]`
# prefix. `to_have_count` counts DOM presence (Jinja gates the <option> server-side),
# so it holds regardless of the picker's visibility.
_BYPASS_OPTION = "option[value='bypassPermissions']"

# A trust write + the dashboard's status poll need headroom on a slow CI host.
_GATE_TIMEOUT = 20_000

# The reaper button renders as "👻 Reap ghost environments". `get_by_role(name=...)`
# matches the accessible name as a case-insensitive *substring* (not exact), so we
# intentionally drop the leading emoji here — the substring still matches, and stays
# robust if the emoji ever changes.
_REAPER_BUTTON = "Reap ghost environments"


def test_bypass_option_only_renders_for_opted_in_project(
    page: Page, bypass_server: Server
) -> None:
    """The bypassPermissions option renders only for the project that opts in.

    ``gamma`` carries ``allow_bypass_permissions: true``; ``alpha``/``beta`` do not, so
    the footgun option is absent from their permission pickers — it cannot be forced
    from the client for a project without the ceiling.
    """
    page.goto(bypass_server.url)
    expect(page.locator(f'[data-project="gamma"] {_BYPASS_OPTION}')).to_have_count(1)
    expect(page.locator(f'[data-project="alpha"] {_BYPASS_OPTION}')).to_have_count(0)
    expect(page.locator(f'[data-project="beta"] {_BYPASS_OPTION}')).to_have_count(0)


def test_bypass_option_absent_when_no_project_opts_in(page: Page, open_server: str) -> None:
    """With no project opting in, the bypassPermissions option renders nowhere."""
    page.goto(open_server)
    expect(page.locator(_BYPASS_OPTION)).to_have_count(0)


def test_bypass_requires_typed_confirmation_before_spawn(
    page: Page, bypass_server: Server
) -> None:
    """Selecting bypassPermissions forces a typed-name confirm; a wrong name is rejected.

    The full gate, without ever spawning: pick the option, Start (untrusted dir → the
    trust prompt shows first), Trust & start (re-routes through ``start()`` so the
    bypass gate still applies → the typed confirm appears *without* spawning), then a
    wrong name surfaces an inline error and the gate stays up.
    """
    page.goto(bypass_server.url)
    card = page.locator('[data-project="gamma"]')
    expect(card).to_be_visible()

    # The spawn/permission pickers hide behind the "Options" toggle; expand it, then
    # choose the footgun permission and Start. The dir is untrusted, so the trust
    # prompt appears first (the order is trust → bypass).
    card.get_by_role("button", name="Options").click()
    card.locator(f"select:has({_BYPASS_OPTION})").select_option("bypassPermissions")
    card.get_by_role("button", name="Start bridge").click()
    trust_start = card.get_by_role("button", name="Trust & start")
    expect(trust_start).to_be_visible()

    # Trust & start re-enters start(): now trusted + bypass selected → the typed
    # confirm appears instead of a spawn.
    card.get_by_role("checkbox").check()
    trust_start.click()
    confirm = card.get_by_text("Type the project name to confirm")
    expect(confirm).to_be_visible(timeout=_GATE_TIMEOUT)

    # A wrong name is rejected inline; nothing spawns and the gate stays up.
    card.get_by_placeholder("gamma").fill("not-gamma")
    card.get_by_role("button", name="Start with bypass").click()
    expect(card.get_by_text("Type the project name exactly to confirm bypass.")).to_be_visible()
    expect(confirm).to_be_visible()


def test_reaper_panel_present_when_enabled(page: Page, reaper_server: str) -> None:
    """With ``reaper.ui_enabled: true``, the ghost-reaper panel renders above the grid."""
    page.goto(reaper_server)
    expect(page.get_by_role("button", name=_REAPER_BUTTON)).to_be_visible()
    # The gate is open: the endpoint no longer 404s (it fails later on absent cloud
    # credentials, which is out of scope for this gating row).
    assert page.request.get(reaper_server + "/api/environments/ghosts").status != 404


def test_reaper_panel_and_endpoint_absent_when_disabled(page: Page, open_server: str) -> None:
    """With the reaper flag unset, the panel is absent and the endpoint 404s."""
    page.goto(open_server)
    expect(page.get_by_role("button", name=_REAPER_BUTTON)).to_have_count(0)
    assert page.request.get(open_server + "/api/environments/ghosts").status == 404
