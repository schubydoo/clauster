"""Browser E2E for the config-management modal (#773, slice A).

Drives the real save path: open the modal from the header, edit a project-scope
CLAUDE.md, type the scope name to confirm, save, and assert the file landed on
disk. Also checks the type-the-name gate (Save stays disabled until the confirm
text matches) and that the settings tab loads. The gate + markup are unit-tested;
this proves the wired Alpine flow round-trips to a real running server.
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


def _open_modal(browser: AgentBrowser, server: Server) -> None:
    browser.goto(server.url)
    browser.expect_visible('[data-project="alpha"]')
    browser.click('[data-test="configmgmt-trigger"]')
    browser.expect_visible('[data-test="configmgmt-modal"]')


def test_config_mgmt_claude_md_project_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Saving a project CLAUDE.md through the modal writes it to the project on disk."""
    # _start_server co-locates clauster.yml beside state_dir; read projects_root from it
    # rather than guessing the mutable tree's path (mirrors the config-editor E2E).
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)

    # Project scope is the default; pin project alpha so the confirm token is "alpha".
    browser.select('[data-test="cm-project"]', "alpha")
    browser.fill('[data-test="cm-claudemd-text"]', "# managed via clauster\n")
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]')

    saved = projects_root / "alpha" / "CLAUDE.md"
    assert saved.exists(), "expected alpha/CLAUDE.md to be written"
    assert "# managed via clauster" in saved.read_text(encoding="utf-8")


def test_config_mgmt_save_disabled_until_confirm_matches(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """The type-the-name gate keeps Save disabled until the scope token is retyped."""
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.fill('[data-test="cm-claudemd-text"]', "pending\n")
    # A dirty edit but no confirm -> Save stays disabled.
    browser.expect_visible('[data-test="cm-save"][disabled]')
    browser.fill('[data-test="cm-confirm"]', "alpha")
    # Correct token -> Save enables.
    browser.expect_visible('[data-test="cm-save"]:not([disabled])')


def test_config_mgmt_settings_tab_loads(browser: AgentBrowser, config_mgmt_server: Server) -> None:
    """Switching to the Settings tab loads the JSON editor for the scope."""
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-settings"]')
    browser.expect_visible('[data-test="cm-view-settings"]')
    # Close the render<->backend loop: alpha has no settings.json, so the redacted
    # misc view is {} — proving the fetch returned and Alpine bound it into the editor.
    browser.expect_value('[data-test="cm-settings-text"]', "{}")
    # The merged (effective) provenance view fetches + renders on demand.
    browser.click('[data-test="cm-effective-toggle"]')
    browser.expect_visible('[data-test="cm-effective-table"]')
