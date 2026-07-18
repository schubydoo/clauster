"""Browser E2E for the first-run setup wizard (#978).

Drives the real loopback wizard a fresh ``clauster run`` (no config) serves: fill the form,
submit, and confirm an auth-enabled ``clauster.yml`` lands on disk. A successful submit then
re-execs the process onto the new config — the assertions run against the success panel +
the written file, not the re-exec'd app.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import SetupServer

pytestmark = pytest.mark.e2e

PASSWORD = "wizard-secret-123"


def test_setup_wizard_writes_auth_enabled_config(
    browser: AgentBrowser, setup_server: SetupServer
) -> None:
    """Filling the wizard writes an auth-enabled config and shows the success panel."""
    browser.goto(setup_server.url)
    browser.expect_visible('[data-test="setup-submit"]')

    browser.fill("#projects_root", str(setup_server.projects_root))
    browser.fill("#password", PASSWORD)
    browser.fill("#confirm", PASSWORD)
    browser.click('[data-test="setup-submit"]')

    browser.expect_visible('[data-test="setup-done"]')
    text = setup_server.write_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["auth"]["enabled"] is True
    assert data["auth"]["password_required"] is True
    assert data["projects_root"] == str(setup_server.projects_root)
    assert PASSWORD not in text  # the plaintext password is never written


def test_setup_wizard_shows_validation_errors(
    browser: AgentBrowser, setup_server: SetupServer
) -> None:
    """A short password surfaces an inline field error and writes nothing."""
    browser.goto(setup_server.url)
    browser.fill("#projects_root", str(setup_server.projects_root))
    browser.fill("#password", "short")
    browser.fill("#confirm", "short")
    browser.click('[data-test="setup-submit"]')

    browser.expect_text('[data-error="password"]', "at least")
    assert not setup_server.write_path.exists()
