"""Browser E2E for the config-management modal (#773, slices A + B + C + D + E).

Drives the real save path: open the modal from the header, edit a project-scope
surface, type the scope name to confirm, save, and assert the file landed on disk.
Covers the CLAUDE.md round trip, the type-the-name gate, the settings tab, the
slice-B permissions round trip + hooks tab, the slice-C subagents list surface
(read-only built-ins + a new-agent round trip to disk), and the slice-D MCP list
surface (tab load + a new-server round trip to .mcp.json). The gate + markup are
unit-tested; this proves the wired Alpine flow round-trips to a real running server.
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


def test_config_mgmt_permissions_project_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Saving project permissions writes the rules into the project's settings.json."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-view-permissions"]')
    # alpha has no settings.json yet -> the permissions view is {} (fetch bound).
    browser.expect_value('[data-test="cm-permissions-text"]', "{}")

    browser.fill('[data-test="cm-permissions-text"]', '{"allow": ["Bash(ls:*)"]}')
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]')

    settings = projects_root / "alpha" / ".claude" / "settings.json"
    assert settings.exists(), "expected alpha/.claude/settings.json to be written"
    assert "Bash(ls:*)" in settings.read_text(encoding="utf-8")


def test_config_mgmt_hooks_tab_loads(browser: AgentBrowser, config_mgmt_server: Server) -> None:
    """Switching to the Hooks tab loads its JSON editor for the scope."""
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    browser.expect_value('[data-test="cm-hooks-text"]', "{}")


def test_config_mgmt_subagents_list_shows_readonly_builtins(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """The subagents list renders Claude Code's built-ins as read-only entries."""
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-subagents"]')
    browser.expect_visible('[data-test="cm-view-subagents"]')
    browser.expect_visible('[data-test="cm-agents-table"]')
    # Built-ins are always present and never editable.
    browser.expect_text('[data-test="cm-agents-table"]', "general-purpose")
    browser.expect_text('[data-test="cm-agents-table"]', "read-only")


def test_config_mgmt_new_subagent_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Creating a subagent through the editor writes its .md into the project."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-subagents"]')
    browser.expect_visible('[data-test="cm-agent-new"]')  # wait for the list to finish loading
    browser.click('[data-test="cm-agent-new"]')
    browser.expect_visible('[data-test="cm-agent-editor"]')

    browser.fill('[data-test="cm-agent-name"]', "my-agent")
    browser.fill(
        '[data-test="cm-agent-content"]',
        "---\nname: my-agent\ndescription: an e2e agent\n---\nDo the thing.\n",
    )
    browser.fill('[data-test="cm-agent-confirm"]', "alpha")
    browser.click('[data-test="cm-agent-save"]')
    browser.expect_visible('[data-test="cm-saved"]')

    saved = projects_root / "alpha" / ".claude" / "agents" / "my-agent.md"
    assert saved.exists(), "expected alpha/.claude/agents/my-agent.md to be written"
    assert "an e2e agent" in saved.read_text(encoding="utf-8")


def test_config_mgmt_mcp_tab_loads(browser: AgentBrowser, config_mgmt_server: Server) -> None:
    """The MCP surface renders its list controls once the servers load."""
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-mcp"]')
    browser.expect_visible('[data-test="cm-view-mcp"]')
    browser.expect_visible('[data-test="cm-mcp-new"]')


def test_config_mgmt_new_mcp_server_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Adding an MCP server through the editor writes it into the project .mcp.json.

    The entry carries an ``env`` value, so the backend routes it to the secret-safe
    direct writer (never the CLI) — proving the wired flow round-trips to disk
    without depending on the fake ``claude`` implementing ``mcp add-json``.
    """
    import json

    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-mcp"]')
    browser.expect_visible('[data-test="cm-mcp-new"]')  # wait for the list to finish loading
    browser.click('[data-test="cm-mcp-new"]')
    browser.expect_visible('[data-test="cm-mcp-editor"]')

    browser.fill('[data-test="cm-mcp-name"]', "my-server")
    browser.fill(
        '[data-test="cm-mcp-entry"]',
        '{"command": "echo", "args": ["hi"], "env": {"FOO": "bar"}}',
    )
    browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
    browser.click('[data-test="cm-mcp-save"]')
    browser.expect_visible('[data-test="cm-saved"]')

    saved = projects_root / "alpha" / ".mcp.json"
    assert saved.exists(), "expected alpha/.mcp.json to be written"
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["mcpServers"]["my-server"]["command"] == "echo"
    assert data["mcpServers"]["my-server"]["env"]["FOO"] == "bar"


def test_config_mgmt_skills_tab_loads(browser: AgentBrowser, config_mgmt_server: Server) -> None:
    """The skills surface renders its list controls once the skills load."""
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-skills"]')
    browser.expect_visible('[data-test="cm-view-skills"]')
    browser.expect_visible('[data-test="cm-skill-new"]')


def test_config_mgmt_new_skill_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Creating a skill through the editor writes its SKILL.md into the project."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-skills"]')
    browser.expect_visible('[data-test="cm-skill-new"]')  # wait for the list to finish loading
    browser.click('[data-test="cm-skill-new"]')
    browser.expect_visible('[data-test="cm-skill-editor"]')

    browser.fill('[data-test="cm-skill-name"]', "my-skill")
    browser.fill(
        '[data-test="cm-skill-content"]',
        "---\nname: my-skill\ndescription: an e2e skill\n---\nDo the skill thing.\n",
    )
    browser.fill('[data-test="cm-skill-confirm"]', "alpha")
    browser.click('[data-test="cm-skill-save"]')
    browser.expect_visible('[data-test="cm-saved"]')

    saved = projects_root / "alpha" / ".claude" / "skills" / "my-skill" / "SKILL.md"
    assert saved.exists(), "expected alpha/.claude/skills/my-skill/SKILL.md to be written"
    assert "an e2e skill" in saved.read_text(encoding="utf-8")


def test_config_mgmt_skill_edit_and_delete_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Editing then deleting a skill through the UI round-trips to disk."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])
    skill_md = projects_root / "alpha" / ".claude" / "skills" / "editme" / "SKILL.md"

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-skills"]')
    browser.expect_visible('[data-test="cm-skill-new"]')

    # Create a skill to act on.
    browser.click('[data-test="cm-skill-new"]')
    browser.expect_visible('[data-test="cm-skill-editor"]')
    browser.fill('[data-test="cm-skill-name"]', "editme")
    browser.fill(
        '[data-test="cm-skill-content"]',
        "---\nname: editme\ndescription: first\n---\nOne.\n",
    )
    browser.fill('[data-test="cm-skill-confirm"]', "alpha")
    browser.click('[data-test="cm-skill-save"]')
    browser.expect_visible('[data-test="cm-saved"]')
    assert "description: first" in skill_md.read_text(encoding="utf-8")

    # Edit it from the list: open, change the body, save.
    browser.expect_visible('[data-test="cm-skill-edit-editme"]')
    browser.click('[data-test="cm-skill-edit-editme"]')
    browser.expect_visible('[data-test="cm-skill-editor"]')
    browser.fill(
        '[data-test="cm-skill-content"]',
        "---\nname: editme\ndescription: second\n---\nTwo.\n",
    )
    browser.fill('[data-test="cm-skill-confirm"]', "alpha")
    browser.click('[data-test="cm-skill-save"]')
    browser.expect_visible('[data-test="cm-saved"]')
    assert "description: second" in skill_md.read_text(encoding="utf-8")

    # Delete it: inline confirm, then verify the directory is gone.
    browser.expect_visible('[data-test="cm-skill-del-editme"]')
    browser.click('[data-test="cm-skill-del-editme"]')
    browser.expect_visible('[data-test="cm-skill-delete-confirm"]')
    browser.fill('[data-test="cm-skill-delete-input"]', "alpha")
    browser.click('[data-test="cm-skill-delete-go"]')
    browser.expect_visible('[data-test="cm-saved"]')
    assert not skill_md.parent.exists(), "expected the deleted skill directory to be gone"


def test_config_mgmt_mcp_approvals_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Approving a committed server through the panel writes enabledMcpjsonServers.

    Adds a server to .mcp.json, then approves it in the per-server approvals panel
    and saves — the choice must land in ``~/.claude.json`` under the project's
    ``enabledMcpjsonServers`` list.
    """
    import json

    state_dir = Path(config_mgmt_server.state_dir)
    cfg_path = state_dir.parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])
    claude_json = state_dir.parent / "home" / ".claude.json"

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-mcp"]')
    browser.expect_visible('[data-test="cm-mcp-new"]')
    browser.click('[data-test="cm-mcp-new"]')
    browser.expect_visible('[data-test="cm-mcp-editor"]')
    browser.fill('[data-test="cm-mcp-name"]', "gizmo")
    browser.fill('[data-test="cm-mcp-entry"]', '{"command": "echo", "env": {"K": "v"}}')
    browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
    browser.click('[data-test="cm-mcp-save"]')
    browser.expect_visible('[data-test="cm-saved"]')

    # The approvals panel now lists the committed server — approve it and save.
    browser.expect_visible('[data-test="cm-mcp-approvals"]')
    browser.expect_visible('[data-test="cm-mcp-approve-gizmo"]')
    browser.click('[data-test="cm-mcp-approve-gizmo"]')
    # The confirm input + Save appear only once the toggle makes the panel dirty.
    browser.expect_visible('[data-test="cm-mcp-approvals-confirm"]')
    browser.fill('[data-test="cm-mcp-approvals-confirm"]', "alpha")
    browser.expect_visible('[data-test="cm-mcp-approvals-save"]:not([disabled])')
    browser.click('[data-test="cm-mcp-approvals-save"]')
    browser.expect_visible('[data-test="cm-saved"]')

    alpha_key = str(projects_root / "alpha")
    stored = json.loads(claude_json.read_text(encoding="utf-8"))
    assert "gizmo" in stored["projects"][alpha_key]["enabledMcpjsonServers"]
