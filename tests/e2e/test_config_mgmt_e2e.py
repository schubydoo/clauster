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

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from _driver import AgentBrowser

    from .conftest import Server

pytestmark = pytest.mark.e2e


# A save round-trips through the server (some surfaces via a fake-claude subprocess);
# on a loaded 2-core CI runner the 5s driver default is too tight — give every
# cm-saved wait the same generous headroom as the bridge status transitions.
_SAVE_TIMEOUT = 20_000


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
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

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
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    settings = projects_root / "alpha" / ".claude" / "settings.json"
    assert settings.exists(), "expected alpha/.claude/settings.json to be written"
    assert "Bash(ls:*)" in settings.read_text(encoding="utf-8")


def test_config_mgmt_settings_env_rows_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """The friendly env rows editor (#765) writes an env var into settings.json.

    Drives the rows layer end to end: default to Rows mode, add a key/value row, and
    save — proving the projection back into settings.text reaches the real save path.
    """
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-settings"]')
    browser.expect_visible('[data-test="cm-view-settings"]')
    # Rows is the default mode; alpha has no env yet -> the empty-state hint shows.
    browser.expect_visible('[data-test="cm-settings-rows"]')
    browser.expect_visible('[data-test="cm-settings-env-empty"]')

    browser.click('[data-test="cm-settings-env-add"]')
    browser.expect_visible('[data-test="cm-settings-env-key"]')  # let the x-for row hydrate
    browser.fill('[data-test="cm-settings-env-key"]', "MY_VAR")
    browser.fill('[data-test="cm-settings-env-value"]', "hello")
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    settings = projects_root / "alpha" / ".claude" / "settings.json"
    assert settings.exists(), "expected alpha/.claude/settings.json to be written"
    body = settings.read_text(encoding="utf-8")
    assert "MY_VAR" in body and "hello" in body


def test_config_mgmt_settings_env_duplicate_key_blocks_save(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Two env rows sharing a key warn and disable Save until resolved (#765).

    Guards against a silent collapse: the last row would win on serialize, dropping the
    earlier value — so Save must stay disabled while a duplicate exists.
    """
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-settings"]')
    browser.expect_visible('[data-test="cm-view-settings"]')

    browser.click('[data-test="cm-settings-env-add"]')
    browser.expect_visible('[data-test="cm-settings-env-key"]')
    browser.click('[data-test="cm-settings-env-add"]')
    browser.expect_count('[data-test="cm-settings-env-key"]', 2)
    # Give both rows the same key (each <tr> carries an indexed data-test hook, so no
    # positional nth-of-type selector that a wrapping-markup change could mis-target).
    key_in = '[data-test="cm-settings-env-key"]'
    browser.fill(f'[data-test="cm-settings-env-row-0"] {key_in}', "DUP")
    browser.fill(f'[data-test="cm-settings-env-row-1"] {key_in}', "DUP")
    browser.fill('[data-test="cm-confirm"]', "alpha")

    browser.expect_visible('[data-test="cm-settings-env-dupe"]')
    browser.expect_disabled('[data-test="cm-save"]')


def test_config_mgmt_settings_empty_env_not_dirty(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """An on-disk empty ``env: {}`` must not read as an unsaved change in rows mode (#765).

    Regression guard: the rows editor drops an empty ``env`` on serialize, so the dirty
    check must normalize both sides — otherwise the surface loads pre-flagged dirty and a
    stray save would silently strip the key.
    """
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"env": {}}', encoding="utf-8")

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-settings"]')
    browser.expect_visible('[data-test="cm-view-settings"]')
    browser.expect_visible('[data-test="cm-settings-rows"]')
    # Type the confirm token so ONLY the dirty check can gate Save; with no edits it
    # must stay disabled (the empty env is not a phantom change).
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.expect_disabled('[data-test="cm-save"]')


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
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

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
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

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
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

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
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
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
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    assert "description: second" in skill_md.read_text(encoding="utf-8")

    # Delete it: inline confirm, then verify the directory is gone.
    browser.expect_visible('[data-test="cm-skill-del-editme"]')
    browser.click('[data-test="cm-skill-del-editme"]')
    browser.expect_visible('[data-test="cm-skill-delete-confirm"]')
    browser.fill('[data-test="cm-skill-delete-input"]', "alpha")
    browser.click('[data-test="cm-skill-delete-go"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    assert not skill_md.parent.exists(), "expected the deleted skill directory to be gone"


def test_config_mgmt_hooks_save_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Saving the Hooks JSON writes the project ``.claude/settings.json`` hooks block.

    Every sibling surface (CLAUDE.md, settings, permissions, MCP, skills, subagents)
    had a save round-trip; hooks only had a loads test (#763 audit gap). The command
    is inert data on write — validate-never-execute — so the assertion is purely that
    the block lands on disk verbatim.
    """
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    browser.expect_value('[data-test="cm-hooks-text"]', "{}")

    hooks = {"SessionStart": [{"hooks": [{"type": "command", "command": "echo e2e-hook"}]}]}
    browser.fill('[data-test="cm-hooks-text"]', json.dumps(hooks))
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    saved = projects_root / "alpha" / ".claude" / "settings.json"
    assert saved.exists(), "expected alpha/.claude/settings.json to be written"
    on_disk = json.loads(saved.read_text(encoding="utf-8"))
    assert on_disk.get("hooks") == hooks


def test_config_mgmt_subagent_delete_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Deleting a subagent via the list's typed-confirm removes its .md from disk.

    The sibling skills surface covered create+delete; subagents only covered create
    (#763 audit gap) — this drives the DELETE endpoint through the UI's own gate.
    """
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])
    agent_md = projects_root / "alpha" / ".claude" / "agents" / "deleteme.md"

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-subagents"]')
    browser.expect_visible('[data-test="cm-agent-new"]')

    # Create the subagent to act on (same flow the create round-trip pins).
    browser.click('[data-test="cm-agent-new"]')
    browser.expect_visible('[data-test="cm-agent-editor"]')
    browser.fill('[data-test="cm-agent-name"]', "deleteme")
    browser.fill(
        '[data-test="cm-agent-content"]',
        "---\nname: deleteme\ndescription: doomed e2e agent\n---\nGone soon.\n",
    )
    browser.fill('[data-test="cm-agent-confirm"]', "alpha")
    browser.click('[data-test="cm-agent-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    assert agent_md.exists(), "expected alpha/.claude/agents/deleteme.md to be written"

    # Delete it from the list: per-name Delete → typed confirm → gone from disk.
    browser.expect_visible('[data-test="cm-agent-del-deleteme"]')
    browser.click('[data-test="cm-agent-del-deleteme"]')
    browser.expect_visible('[data-test="cm-agent-delete-confirm"]')
    browser.fill('[data-test="cm-agent-delete-input"]', "alpha")
    browser.click('[data-test="cm-agent-delete-go"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    assert not agent_md.exists(), "expected the deleted subagent .md to be gone"


def test_config_mgmt_plugins_tab_lists_and_acts(
    browser: AgentBrowser, config_mgmt_plugins_server: Server
) -> None:
    """The plugins surface lists the seeded plugin + marketplace and an action posts.

    The plugin/marketplace lists come from the fake ``claude plugin`` (seeded by the
    fixture). Typing the scope confirm arms the action buttons; clicking Disable
    round-trips through the wired POST → reload and surfaces the saved banner.
    """
    _open_modal(browser, config_mgmt_plugins_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-plugins"]')
    browser.expect_visible('[data-test="cm-view-plugins"]')
    # Both CLI-driven lists rendered.
    browser.expect_text('[data-test="cm-plugins-table"]', "hello@market")
    browser.expect_text('[data-test="cm-marketplaces-table"]', "market")

    # Actions are disabled until the scope token is typed, then a Disable posts.
    browser.expect_visible('[data-test="cm-plugin-confirm"]')
    browser.fill('[data-test="cm-plugin-confirm"]', "alpha")
    browser.click('[data-test="cm-plugin-disable-hello@market"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)


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
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    # The approvals panel now lists the committed server — approve it and save.
    browser.expect_visible('[data-test="cm-mcp-approvals"]')
    browser.expect_visible('[data-test="cm-mcp-approve-gizmo"]')
    browser.click('[data-test="cm-mcp-approve-gizmo"]')
    # The confirm input + Save appear only once the toggle makes the panel dirty.
    browser.expect_visible('[data-test="cm-mcp-approvals-confirm"]')
    browser.fill('[data-test="cm-mcp-approvals-confirm"]', "alpha")
    browser.expect_visible('[data-test="cm-mcp-approvals-save"]:not([disabled])')
    browser.click('[data-test="cm-mcp-approvals-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    alpha_key = str(projects_root / "alpha")
    stored = json.loads(claude_json.read_text(encoding="utf-8"))
    assert "gizmo" in stored["projects"][alpha_key]["enabledMcpjsonServers"]
