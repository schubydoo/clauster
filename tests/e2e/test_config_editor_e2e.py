"""Browser E2E for the in-app config editor (FE-3, #299).

Drives the real save path: open the editor from the header, change a Tier-A field,
save, and confirm the restart banner plus the value persisted to the on-disk config
(with a backup). The fail-closed allowlist + re-validate logic is unit-tested; this
asserts the wired UI round-trips to a real running server.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e


def test_config_editor_persists_tier_a_change(
    browser: AgentBrowser, config_server: Server
) -> None:
    """Editing a Tier-A field via the modal writes it to disk and keeps a backup."""
    cfg_path = Path(config_server.state_dir).parent / "clauster.yml"
    browser.goto(config_server.url)
    browser.expect_visible('[data-project="alpha"]')

    # Open the editor from the header gear.
    browser.click('[aria-label="Edit configuration"]')
    fx = '[id="cfg-usage.fx_rate"]'
    browser.expect_visible(fx)

    # Change the seeded fx_rate (1.0 -> 7) and save.
    browser.fill(fx, "7")
    browser.click('[data-test="cfg-save"]')
    browser.expect_visible('[data-test="cfg-saved"]')

    # Persisted to the real config file, with a timestamped backup of the prior content.
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["usage"]["fx_rate"] == 7
    assert list(cfg_path.parent.glob("clauster.yml.bak-*")), "expected a config backup"


def test_config_editor_enum_selects_reflect_saved_value(
    browser: AgentBrowser, enum_config_server: Server
) -> None:
    """Enum dropdowns show the persisted value, not the first option.

    Regression for the ``<select>`` x-model/x-for ordering bug: options render
    after x-model sets the value, so the browser fell back to option index 0 —
    the editor displayed ``standard``/``cost`` while the saved value was
    ``pty``/``off``, and Save stayed greyed when you re-picked the real value.
    """
    browser.goto(enum_config_server.url)
    browser.expect_visible('[data-project="alpha"]')

    browser.click('[aria-label="Edit configuration"]')
    launch_mode = '[id="cfg-claude.launch_mode"]'
    browser.expect_visible(launch_mode)

    # The selects must reflect the on-disk values (pty / off), NOT the first option.
    browser.expect_value(launch_mode, "pty")
    browser.expect_value('[id="cfg-usage.mode"]', "off")

    # And since the displayed value matches the model, there are no pending edits —
    # Save stays disabled until a real change (the symptom the bug masked).
    browser.expect_disabled('[data-test="cfg-save"]')


def _login(browser: AgentBrowser, url: str, password: str) -> None:
    """Authenticate against an auth-enabled server and land on the dashboard."""
    browser.goto(f"{url}/login")
    browser.fill("#password", password)
    browser.click('button[type="submit"]')
    browser.expect_url(f"{url}/")
    browser.expect_visible("#project-grid")


def test_advanced_panel_unlock_and_save(
    browser: AgentBrowser, advanced_config_server: Server, e2e_password: str
) -> None:
    """Step-up unlock reveals the Tier-B fields; a save persists to disk with a backup (#978)."""
    cfg_path = Path(advanced_config_server.state_dir).parent / "clauster.yml"
    _login(browser, advanced_config_server.url, e2e_password)

    browser.click('[aria-label="Edit configuration"]')
    # The Advanced panel renders (config-write on) but starts locked behind the password.
    browser.expect_visible('[data-test="adv-panel"]')
    browser.expect_visible('[data-test="adv-password"]')

    # Step up with the correct password -> the Tier-B fields load.
    browser.fill('[data-test="adv-password"]', e2e_password)
    browser.click('[data-test="adv-unlock"]')
    field = '[id="adv-clone.timeout_seconds"]'
    browser.expect_visible(field)

    # Change the seeded value (300 -> 137) and save through PUT /api/config/advanced.
    browser.fill(field, "137")
    browser.click('[data-test="adv-save"]')
    browser.expect_visible('[data-test="adv-saved"]')

    # Persisted to the real config file, with a timestamped backup of the prior content.
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["clone"]["timeout_seconds"] == 137
    assert list(cfg_path.parent.glob("clauster.yml.bak-*")), "expected a config backup"


def test_advanced_panel_list_and_map_editors(
    browser: AgentBrowser, advanced_config_server: Server, e2e_password: str
) -> None:
    """Slice 4: the Tier-B rows editor + event-checkbox map persist to disk (#978).

    Adds a clone scheme via the list rows editor and enables a lifecycle event via the
    webhooks.events checkbox map, saves once, and confirms both landed in clauster.yml.
    """
    cfg_path = Path(advanced_config_server.state_dir).parent / "clauster.yml"
    _login(browser, advanced_config_server.url, e2e_password)

    browser.click('[aria-label="Edit configuration"]')
    browser.fill('[data-test="adv-password"]', e2e_password)
    browser.click('[data-test="adv-unlock"]')

    # The Tier-B panel is a tall scrollable modal — its list/map editors + Save sit below a
    # sticky footer, so scroll each target into view before acting or the click lands on the
    # overlay (empirically verified: an un-scrolled Save click never reaches the button).
    # List rows editor: the seed is the default [https, ssh]; add a third scheme.
    add = '[data-test="adv-list-add-allowed_schemes"]'
    browser.expect_visible(add)
    browser.scroll_into_view(add)
    browser.click(add)
    row = '[data-test="adv-list-allowed_schemes"] .input-group:last-of-type input'
    browser.scroll_into_view(row)
    browser.fill(row, "git")

    # Checkbox map: enable permission-needed (a #432 event that defaults OFF).
    event = '[data-test="adv-event-permission-needed"]'
    browser.scroll_into_view(event)
    browser.check(event)

    save = '[data-test="adv-save"]'
    browser.scroll_into_view(save)
    browser.expect_enabled(save)
    browser.click(save)
    browser.expect_visible('[data-test="adv-saved"]')

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["clone"]["allowed_schemes"] == ["https", "ssh", "git"]
    # The map serializes minimally — only the toggled non-default key is written.
    assert data["webhooks"]["events"] == {"permission-needed": True}


def test_advanced_panel_rejects_wrong_password(
    browser: AgentBrowser, advanced_config_server: Server, e2e_password: str
) -> None:
    """A wrong step-up password shows an error and never reveals the Tier-B fields (#978)."""
    _login(browser, advanced_config_server.url, e2e_password)
    browser.click('[aria-label="Edit configuration"]')
    browser.expect_visible('[data-test="adv-password"]')

    browser.fill('[data-test="adv-password"]', "definitely-not-the-password")
    browser.click('[data-test="adv-unlock"]')
    browser.expect_text('[data-test="adv-reauth-error"]', "Incorrect password.")
    # Still locked: the save button (unlocked-only) is absent.
    browser.expect_hidden('[data-test="adv-save"]')


def test_advanced_panel_needs_auth_when_auth_disabled(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Config-write on but auth off: Advanced shows a needs-auth note, not an unlock form (#978).

    Step-up has no password to prove when auth is disabled, so the unlock form could never
    succeed — the panel points the operator at enabling authentication instead.
    """
    browser.goto(config_mgmt_server.url)
    browser.expect_visible('[data-project="alpha"]')
    browser.click('[aria-label="Edit configuration"]')
    browser.expect_visible('[data-test="adv-panel"]')
    browser.expect_visible('[data-test="adv-needs-auth"]')
    browser.expect_hidden('[data-test="adv-password"]')


def test_advanced_panel_absent_when_config_write_disabled(
    browser: AgentBrowser, config_server: Server
) -> None:
    """The Advanced panel is invisible when config-write is off (#978).

    Invisible-surface invariant at the UI layer: the panel's ``x-show`` is the
    config-write capability, mirroring the /api/config/advanced 404 gate. The Tier-A
    editor still opens normally.
    """
    browser.goto(config_server.url)
    browser.expect_visible('[data-project="alpha"]')
    browser.click('[aria-label="Edit configuration"]')
    browser.expect_visible('[id="cfg-usage.fx_rate"]')  # Tier-A editor open
    browser.expect_hidden('[data-test="adv-panel"]')  # but no Advanced surface
