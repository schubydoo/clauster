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
