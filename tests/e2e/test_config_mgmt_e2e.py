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
import time
from collections.abc import Iterator
from contextlib import contextmanager
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


def _mcp_raw_mode(browser: AgentBrowser) -> None:
    """Switch the MCP editor to Raw JSON and wait for the textarea to replace the fields.

    The editor opens in Fields (TASK-272). The switch hides the form and shows the
    textarea on the next Alpine frame, so a fill sent before that frame has no target.
    """
    browser.click('[data-test="cm-mcp-mode-raw"]')
    browser.expect_visible('[data-test="cm-mcp-entry"]')


def _switch_to_user_scope(browser: AgentBrowser) -> None:
    """Click the User scope and wait for the layout it causes to settle.

    User scope hides the project selector (`x-show`), which sits above the surface tabs,
    so every tab moves up by the selector's height on the next Alpine frame. A click
    sent before that frame is aimed at the tab's OLD position, which is then inside the
    CLAUDE.md textarea: the click focuses the textarea and the surface never changes.
    Waiting for the selector to be hidden makes the next click hit the tab it names.
    """
    browser.click('[data-test="cm-scope-user"]')
    browser.expect_hidden('[data-test="cm-project"]')


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


def test_config_mgmt_confirm_clears_on_surface_switch(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Switching surface tabs clears the typed scope confirm-token (#958 P3)."""
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    # Type the confirm token on the default (CLAUDE.md) doc surface...
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.expect_value('[data-test="cm-confirm"]', "alpha")
    # ...then switch to another doc surface: the stale token must NOT linger (it would
    # otherwise read as pre-satisfied on the new surface).
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-view-permissions"]')
    browser.expect_value('[data-test="cm-confirm"]', "")


def test_config_mgmt_blank_json_saves_as_empty(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Clearing a JSON surface and saving treats the blank box as {} (#958 P3)."""
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-view-permissions"]')
    # Permissions defaults to the rows editor (#958 Part 5); drop to the raw-JSON
    # escape hatch to exercise the blank-box path.
    browser.click('[data-test="cm-permissions-mode-raw"]')
    browser.expect_visible('[data-test="cm-permissions-text"]')
    # Clear the JSON box entirely, then save. A blank box must be accepted as the
    # empty-object default rather than rejected with "must be valid JSON".
    browser.fill('[data-test="cm-permissions-text"]', "")
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)


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
    # Permissions defaults to the rows editor (#958 Part 5); drop to raw JSON to save
    # the block as literal JSON (the rows path is covered separately below).
    browser.click('[data-test="cm-permissions-mode-raw"]')
    browser.expect_visible('[data-test="cm-permissions-text"]')
    # alpha has no settings.json yet -> the permissions view is {} (fetch bound).
    browser.expect_value('[data-test="cm-permissions-text"]', "{}")

    browser.fill('[data-test="cm-permissions-text"]', '{"allow": ["Bash(ls:*)"]}')
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    stored = json.loads(
        (projects_root / "alpha" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert stored["permissions"]["allow"] == ["Bash(ls:*)"]


def test_config_mgmt_permissions_rows_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Adding an allow rule via the rows editor writes it into the project's settings.json."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-view-permissions"]')
    # Rows mode is the default. Add an allow rule, type it, and save.
    browser.click('[data-test="cm-perm-add-allow"]')
    browser.expect_visible('[data-test="cm-perm-rule-allow"]')  # let the x-for row hydrate
    browser.fill('[data-test="cm-perm-rule-allow"]', "Bash(ls:*)")
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    stored = json.loads(
        (projects_root / "alpha" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert stored["permissions"]["allow"] == ["Bash(ls:*)"]

    settings = projects_root / "alpha" / ".claude" / "settings.json"
    assert settings.exists(), "expected alpha/.claude/settings.json to be written"
    assert "Bash(ls:*)" in settings.read_text(encoding="utf-8")


#: The maintainer's exact autoMode block from the TASK-449.1 report (2026-11-04).
_MAINTAINER_AUTO_MODE = {
    "allow": [
        "$defaults",
        "git push --force-with-lease from a worktree under /home/claude/worktrees/claustrum/ "
        "to an unmerged feature branch that this session opened is allowed. Never to main "
        "or master, ...",
    ]
}


def test_config_mgmt_auto_mode_user_scope_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """TASK-449.1: the reported autoMode block saves from the Permissions tab at User scope."""
    # _start_server isolates HOME at <tmp>/home, beside state_dir; user-scope settings
    # land in <home>/.claude/settings.json, never the real account.
    home = Path(config_mgmt_server.state_dir).parent / "home"

    _open_modal(browser, config_mgmt_server)
    _switch_to_user_scope(browser)
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-view-permissions"]')
    browser.expect_visible('[data-test="cm-automode-text"]')
    browser.fill('[data-test="cm-automode-text"]', json.dumps(_MAINTAINER_AUTO_MODE))
    browser.fill('[data-test="cm-confirm"]', "USER SCOPE")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    stored = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert stored["autoMode"] == _MAINTAINER_AUTO_MODE


def test_config_mgmt_auto_mode_only_edit_keeps_permissions_as_loaded(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """An autoMode-only save resends `permissions` as loaded, explicit `false` included."""
    home = Path(config_mgmt_server.state_dir).parent / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    # Not the rows' canonical form: an empty list, then an explicit false switch.
    seeded = {"deny": [], "allow": ["Bash(ls:*)"], "blockReadsOutsideWorkingDirectories": False}
    settings.write_text(json.dumps({"permissions": seeded}), encoding="utf-8")

    _open_modal(browser, config_mgmt_server)
    _switch_to_user_scope(browser)
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-permissions-rows"]')  # the seed fits the rows
    browser.fill('[data-test="cm-automode-text"]', json.dumps(_MAINTAINER_AUTO_MODE))
    browser.fill('[data-test="cm-confirm"]', "USER SCOPE")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    stored = json.loads(settings.read_text(encoding="utf-8"))
    assert stored["permissions"] == seeded  # same keys, same order-free content, false kept
    assert list(stored["permissions"]) == list(seeded)  # and the loaded key order
    assert stored["autoMode"] == _MAINTAINER_AUTO_MODE


def test_config_mgmt_permissions_failed_load_clears_previous_rows(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A failed load never leaves the previous target's rules editable or saveable."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])
    project_settings = projects_root / "alpha" / ".claude" / "settings.json"
    project_settings.parent.mkdir(parents=True, exist_ok=True)
    project_settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}), encoding="utf-8"
    )
    user_settings = (
        Path(config_mgmt_server.state_dir).parent / "home" / ".claude" / "settings.json"
    )
    user_settings.parent.mkdir(parents=True, exist_ok=True)
    user_settings.write_text("{not json", encoding="utf-8")  # the User-scope read fails (422)

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-permissions"]')
    # Wait for the rule row to be VISIBLE, not just in the DOM: a count also sees rows
    # under a still-hidden view, so the scope switch below could beat the load.
    browser.expect_visible('[data-test="cm-perm-rule-allow"]')  # alpha's rule loaded
    _switch_to_user_scope(browser)
    browser.expect_visible('[data-test="cm-error"]')
    browser.expect_count('[data-test="cm-perm-rule-allow"]', 0)  # alpha's rule is gone
    browser.fill('[data-test="cm-confirm"]', "USER SCOPE")
    browser.expect_visible('[data-test="cm-save"][disabled]')  # nothing to save over


def _open_permissions_at(browser: AgentBrowser, server: Server, scope: str) -> None:
    """Open alpha's Permissions tab at Project or Local scope and wait for its load."""
    _open_modal(browser, server)
    browser.select('[data-test="cm-project"]', "alpha")
    if scope == "local":
        browser.click('[data-test="cm-scope-local"]')
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-permissions-rows"]')
    browser.expect_visible('[data-test="cm-automode-repo"]')


def test_config_mgmt_auto_mode_project_scope_none_stored_offers_user_scope(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """TASK-509: no box at Project scope, only a note and a switch to User scope."""
    _open_permissions_at(browser, config_mgmt_server, "project")
    browser.expect_visible('[data-test="cm-automode-none"]')
    assert "only from User scope" in browser.get_text('[data-test="cm-automode-none"]')
    browser.expect_hidden('[data-test="cm-automode-text"]')  # nothing to type into
    browser.expect_hidden('[data-test="cm-automode-stored"]')
    browser.click('[data-test="cm-automode-switch-user"]')
    browser.expect_hidden('[data-test="cm-project"]')
    browser.expect_visible('[data-test="cm-automode-text"]')  # the box lives at User scope
    browser.expect_attr('[data-test="cm-scope-user"]', "aria-pressed", "true")
    assert browser.eval_json(
        "document.activeElement === document.querySelector('[data-test=\"cm-scope-user\"]')"
    ), "focus should land on the pressed User scope button, not fall to the page"


def test_config_mgmt_auto_mode_without_user_scope_names_the_host_file(
    browser: AgentBrowser, config_mgmt_project_only_server: Server
) -> None:
    """TASK-509: with User-scope editing off, the note names the host file, no button."""
    _open_permissions_at(browser, config_mgmt_project_only_server, "project")
    browser.expect_visible('[data-test="cm-automode-host-hint"]')
    assert "~/.claude/settings.json" in browser.get_text('[data-test="cm-automode-host-hint"]')
    assert browser.get_count('[data-test="cm-automode-switch-user"]') == 0
    browser.expect_hidden('[data-test="cm-automode-text"]')


def test_config_mgmt_permissions_rows_new_keys_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """TASK-449.3: a switch and an additional directory save through the rows editor."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-perm-flags"]')
    browser.check('[data-test="cm-perm-flag-blockWritesOutsideWorkingDirectories"]')
    browser.click('[data-test="cm-perm-add-additionalDirectories"]')
    browser.expect_visible('[data-test="cm-perm-rule-additionalDirectories"]')
    browser.fill('[data-test="cm-perm-rule-additionalDirectories"]', "../docs/")
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    stored = json.loads(
        (projects_root / "alpha" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert stored["permissions"] == {
        "additionalDirectories": ["../docs/"],
        "blockWritesOutsideWorkingDirectories": True,
    }


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


# Fail the User-scope settings GET in the page, either as an HTTP error or as a network
# error, so the load fails while ~/.claude/settings.json does NOT exist. That is the case
# TASK-463 names: a missing file takes a null hash, so the server would accept the save.
_FAIL_USER_SETTINGS_GET = """(() => {
  const orig = window.fetch;
  window.fetch = (url, opts) => {
    const u = String(url);
    if (u.includes("/api/config-write/settings?") && u.includes("scope=user")) {
      return %s;
    }
    return orig(url, opts);
  };
  return "stubbed";
})()"""


@pytest.mark.parametrize(
    "failure",
    [
        'Promise.resolve(new Response("{}", { status: 500 }))',
        'Promise.reject(new TypeError("Failed to fetch"))',
    ],
    ids=["http-error", "network-error"],
)
def test_config_mgmt_settings_failed_load_clears_previous_env_rows(
    browser: AgentBrowser, config_mgmt_server: Server, failure: str
) -> None:
    """A failed Settings load never lets another target's env rows save (TASK-463)."""
    _seed_alpha_settings(config_mgmt_server, {"env": {"ALPHA_VAR": "alpha-only"}})
    user_settings = (
        Path(config_mgmt_server.state_dir).parent / "home" / ".claude" / "settings.json"
    )
    assert not user_settings.exists()

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-settings"]')
    browser.expect_value('[data-test="cm-settings-env-key"]', "ALPHA_VAR")  # alpha loaded
    assert browser.eval_json(_FAIL_USER_SETTINGS_GET % failure) == "stubbed"
    _switch_to_user_scope(browser)
    browser.expect_visible('[data-test="cm-error"]')
    # The row check is what fails without the fix in both cases. In the network-error case
    # the old text stayed too, so Save was already disabled (and the old hash would 409).
    browser.expect_count('[data-test="cm-settings-env-key"]', 0)  # alpha's row is gone
    browser.fill('[data-test="cm-confirm"]', "USER SCOPE")
    browser.expect_disabled('[data-test="cm-save"]')  # nothing to save into User scope
    assert not user_settings.exists()


# --- TASK-271: a typed row for every top-level key --------------------------------------


def _alpha_settings_path(server: Server) -> Path:
    cfg_path = Path(server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])
    return projects_root / "alpha" / ".claude" / "settings.json"


def _open_settings_rows(browser: AgentBrowser, server: Server) -> None:
    _open_modal(browser, server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-settings"]')
    browser.expect_visible('[data-test="cm-settings-rows"]')


def _row(key: str, control: str = "") -> str:
    """Selector for the settings row of ``key`` (or one of its controls)."""
    sel = f'[data-test="cm-settings-key-row"][data-key="{key}"]'
    return f'{sel} [data-test="{control}"]' if control else sel


def _checked(browser: AgentBrowser, selector: str) -> bool:
    return browser.eval_json(f"document.querySelector({json.dumps(selector)}).checked")


def _save_alpha(browser: AgentBrowser) -> None:
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)


def _add_settings_key(browser: AgentBrowser, key: str, value_type: str = "") -> None:
    """Add ``key``; ``value_type`` (string, boolean or number) overrides the preselection."""
    browser.fill('[data-test="cm-settings-add-key"]', key)
    if value_type:
        browser.select('[data-test="cm-settings-add-type"]', value_type)
    browser.click('[data-test="cm-settings-add-key-btn"]')


_MARKETPLACES = {"team": {"source": {"source": "github", "repo": "org/plugins"}}}


def test_config_mgmt_settings_typed_rows_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Each key loads as its typed row and saves with the JSON type it had (TASK-271)."""
    _seed_alpha_settings(
        config_mgmt_server,
        {
            "verbose": True,
            "cleanupPeriodDays": 30,
            "tui": "default",
            "language": "1",
            "extraKnownMarketplaces": _MARKETPLACES,
            "apiKeyHelper": "/opt/print-key --token s3cr3t",
            "env": {"A": "x"},
        },
    )
    _open_settings_rows(browser, config_mgmt_server)
    browser.expect_count('[data-test="cm-settings-key-row"]', 7)
    assert _checked(browser, _row("verbose", "cm-settings-bool")) is True
    browser.expect_value(_row("cleanupPeriodDays", "cm-settings-number"), "30")
    browser.expect_value(_row("tui", "cm-settings-string"), "default")
    browser.expect_value(_row("language", "cm-settings-string"), "1")
    browser.expect_visible(_row("extraKnownMarketplaces", "cm-settings-readonly"))
    # The helper is masked in the page; the real command never reaches the browser.
    browser.expect_value(_row("apiKeyHelper", "cm-settings-string"), "********")
    assert "s3cr3t" not in browser.eval_js("document.documentElement.outerHTML")
    browser.expect_visible(_row("env", "cm-settings-env-summary"))

    browser.click(_row("verbose", "cm-settings-bool"))
    browser.fill(_row("cleanupPeriodDays", "cm-settings-number"), "45")
    browser.fill(_row("tui", "cm-settings-string"), "fullscreen")
    _save_alpha(browser)

    stored = json.loads(_alpha_settings_path(config_mgmt_server).read_text(encoding="utf-8"))
    assert stored == {
        "verbose": False,
        "cleanupPeriodDays": 45,
        "tui": "fullscreen",
        "language": "1",  # untouched text that looks like a number stays text
        "extraKnownMarketplaces": _MARKETPLACES,  # the read-only row is written back as is
        "apiKeyHelper": "/opt/print-key --token s3cr3t",  # the untouched mask kept it
        "env": {"A": "x"},
    }
    assert type(stored["cleanupPeriodDays"]) is int


def test_config_mgmt_settings_add_and_remove_key_rows(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Added keys get the server's editor type, unknown keys are text, owned keys link."""
    _seed_alpha_settings(config_mgmt_server, {"model": "opus"})
    _open_settings_rows(browser, config_mgmt_server)
    browser.expect_count('[data-test="cm-settings-key-row"]', 1)
    # The Add-a-key warning is hidden on a fresh load (the rows above have rendered, so
    # Alpine has run). An x-show element must not carry a Tabler d-* display class,
    # because its !important display beats the display:none that x-show sets.
    assert not browser.is_visible('[data-test="cm-settings-add-error"]')
    browser.click(_row("model", "cm-settings-key-remove"))
    browser.expect_count('[data-test="cm-settings-key-row"]', 0)

    # A refused add shows the warning, and the next successful add hides it again.
    _add_settings_key(browser, "env")
    browser.expect_text('[data-test="cm-settings-add-error"]', "env table below")
    browser.expect_visible('[data-test="cm-settings-add-error"]')
    _add_settings_key(browser, "verbose")
    browser.expect_visible(_row("verbose", "cm-settings-bool"))
    browser.expect_hidden('[data-test="cm-settings-add-error"]')
    assert _checked(browser, _row("verbose", "cm-settings-bool")) is False
    browser.click(_row("verbose", "cm-settings-bool"))
    _add_settings_key(browser, "someFutureKey")
    browser.fill(_row("someFutureKey", "cm-settings-string"), "abc")
    _add_settings_key(browser, "theme")
    browser.expect_value(_row("theme", "cm-settings-string"), "")
    browser.expect_text(_row("theme", "cm-settings-value-warning"), "Empty. Type or pick one")
    browser.fill(_row("theme", "cm-settings-string"), "dark")
    browser.expect_hidden(_row("theme", "cm-settings-value-warning"))
    _add_settings_key(browser, "cleanupPeriodDays")
    # An empty number box blocks the save until it holds a number.
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.expect_visible('[data-test="cm-settings-number-error"]')
    browser.expect_disabled('[data-test="cm-save"]')
    # Raw JSON would lose that row, so the switch is refused and says why at the top.
    browser.click('[data-test="cm-settings-mode-raw"]')
    browser.expect_text('[data-test="cm-settings-raw-blocked"]', "every number box holds a number")
    browser.expect_visible('[data-test="cm-settings-rows"]')
    browser.fill(_row("cleanupPeriodDays", "cm-settings-number"), "7")
    browser.expect_hidden('[data-test="cm-settings-number-error"]')
    browser.expect_hidden('[data-test="cm-settings-raw-blocked"]')
    _save_alpha(browser)
    stored = json.loads(_alpha_settings_path(config_mgmt_server).read_text(encoding="utf-8"))
    assert stored == {
        "verbose": True,
        "someFutureKey": "abc",
        "theme": "dark",
        "cleanupPeriodDays": 7,
    }

    # A documented object key is not made a text row; the message sends it to Raw JSON.
    _add_settings_key(browser, "statusLine")
    browser.expect_text('[data-test="cm-settings-add-error"]', "statusLine holds an object")
    browser.expect_visible('[data-test="cm-settings-add-raw"]')
    browser.expect_count(_row("statusLine"), 0)

    # A key that another tab edits is not added; the message links to that tab.
    _add_settings_key(browser, "hooks")
    browser.expect_text('[data-test="cm-settings-add-error"]', "hooks is edited on the Hooks tab")
    browser.expect_count(_row("hooks"), 0)
    browser.click('[data-test="cm-settings-add-owner-link"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')


def _suggestions(browser: AgentBrowser, key: str) -> list[str]:
    """The values the text box of ``key`` suggests, read through its ``list`` attribute."""
    sel = json.dumps(_row(key, "cm-settings-string"))
    return browser.eval_json(
        f"(() => {{ const box = document.querySelector({sel});"
        " return box && box.list ? [...box.list.options].map(o => o.value) : null; })()"
    )


def test_config_mgmt_settings_suggestions_accept_any_value(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A former select is a text box with suggestions; an off-list value warns and saves."""
    _seed_alpha_settings(
        config_mgmt_server,
        {"tui": "default", "theme": "custom:solarized", "effortLevel": "ultra", "model": "opus"},
    )
    _open_settings_rows(browser, config_mgmt_server)
    # The documented values are suggestions on a text box, not options of a select.
    assert browser.get_count('[data-test="cm-settings-keys"] select') == 0
    assert _suggestions(browser, "tui") == ["default", "fullscreen"]
    assert _suggestions(browser, "model") is None  # a plain text key suggests nothing
    # A known value and a documented open form (a custom theme) show no warning; a stored
    # value outside the list loads as it is, with a warning on its row and at the top.
    browser.expect_value(_row("theme", "cm-settings-string"), "custom:solarized")
    assert not browser.is_visible(_row("theme", "cm-settings-value-warning"))
    assert not browser.is_visible(_row("tui", "cm-settings-value-warning"))
    browser.expect_value(_row("effortLevel", "cm-settings-string"), "ultra")
    browser.expect_text(_row("effortLevel", "cm-settings-value-warning"), "Not one of the known")
    browser.expect_text('[data-test="cm-settings-offlist"]', "effortLevel")

    browser.fill(_row("tui", "cm-settings-string"), "split")
    browser.expect_text(_row("tui", "cm-settings-value-warning"), "It saves as typed")
    browser.expect_text('[data-test="cm-settings-offlist"]', "tui, effortLevel")
    browser.fill(_row("theme", "cm-settings-string"), "custom:nord")
    _save_alpha(browser)  # no refusal: the value saves as typed
    stored = json.loads(_alpha_settings_path(config_mgmt_server).read_text(encoding="utf-8"))
    assert stored == {
        "tui": "split",
        "theme": "custom:nord",
        "effortLevel": "ultra",
        "model": "opus",
    }
    # After the reload the warning is still there, in view beside the saved banner.
    browser.expect_text('[data-test="cm-settings-offlist"]', "tui, effortLevel")
    assert not browser.is_visible(_row("theme", "cm-settings-value-warning"))
    browser.fill(_row("tui", "cm-settings-string"), "fullscreen")
    browser.fill(_row("effortLevel", "cm-settings-string"), "high")
    browser.expect_hidden('[data-test="cm-settings-offlist"]')


def test_config_mgmt_settings_add_key_type_picker(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Add a key lets the operator pick text, true/false or number; a known key preselects."""
    _open_settings_rows(browser, config_mgmt_server)
    picker = '[data-test="cm-settings-add-type"]'
    browser.expect_value(picker, "string")
    # A documented key preselects its type as soon as its name is typed.
    browser.fill('[data-test="cm-settings-add-key"]', "cleanupPeriodDays")
    browser.expect_value(picker, "number")
    browser.fill('[data-test="cm-settings-add-key"]', "verbose")
    browser.expect_value(picker, "boolean")
    # A name the server does not list undoes the preselection, but keeps an operator pick.
    browser.fill('[data-test="cm-settings-add-key"]', "verboseX")
    browser.expect_value(picker, "string")
    browser.select(picker, "number")
    browser.fill('[data-test="cm-settings-add-key"]', "verboseY")
    browser.expect_value(picker, "number")
    # Keys the server does not list, one per type the operator picks.
    _add_settings_key(browser, "futureFlag", "boolean")
    browser.click(_row("futureFlag", "cm-settings-bool"))
    browser.expect_value(picker, "string")  # the pick resets after each add
    _add_settings_key(browser, "futureCount", "number")
    browser.fill(_row("futureCount", "cm-settings-number"), "3")
    _add_settings_key(browser, "futureName")
    browser.fill(_row("futureName", "cm-settings-string"), "x")
    # The preselection is only a suggestion: a documented Boolean key added as text.
    _add_settings_key(browser, "verbose", "string")
    browser.fill(_row("verbose", "cm-settings-string"), "yes")
    _save_alpha(browser)
    stored = json.loads(_alpha_settings_path(config_mgmt_server).read_text(encoding="utf-8"))
    assert stored == {"futureFlag": True, "futureCount": 3, "futureName": "x", "verbose": "yes"}
    assert type(stored["futureCount"]) is int


def test_config_mgmt_settings_refused_key_shows_server_refusal(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A key Claude Code ignores at Project scope saves to the server's own refusal."""
    _open_settings_rows(browser, config_mgmt_server)
    options = browser.eval_json(
        "[...document.querySelectorAll('#cm-settings-key-options option')].map(o => o.value)"
    )
    assert "verbose" in options
    assert "skipAutoPermissionPrompt" not in options  # User scope only: not offered here
    _add_settings_key(browser, "skipAutoPermissionPrompt")  # typed: still a typed row
    browser.click(_row("skipAutoPermissionPrompt", "cm-settings-bool"))
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_text(
        '[data-test="cm-error"]',
        "Claude Code reads skipAutoPermissionPrompt only from user settings and ignores it "
        "in project settings (.claude/settings.json)",
    )
    assert not _alpha_settings_path(config_mgmt_server).exists()


def test_config_mgmt_settings_rows_raw_switch_keeps_every_value(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Rows <-> Raw never drops a value, an unedited switch is not dirty (TASK-271)."""
    seeded = {
        "constructor": "x",  # a key named like an Object member is an ordinary key
        "__proto__": "p",
        "verbose": True,
        "feedbackSurveyRate": 0.25,
        "attribution": False,
        "companyAnnouncements": ["a", "b"],
        "nothing": None,
        "statusLine": {"type": "command", "command": "echo hi"},
    }
    _seed_alpha_settings(config_mgmt_server, seeded)
    _open_settings_rows(browser, config_mgmt_server)
    browser.expect_count('[data-test="cm-settings-key-row"]', len(seeded))
    browser.expect_visible(_row("constructor", "cm-settings-string"))
    browser.expect_visible(_row("nothing", "cm-settings-readonly"))

    # An unedited round trip stays clean.
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-settings-mode-raw"]')
    browser.expect_visible('[data-test="cm-settings-text"]')
    browser.expect_disabled('[data-test="cm-save"]')
    browser.click('[data-test="cm-settings-mode-rows"]')
    browser.expect_count('[data-test="cm-settings-key-row"]', len(seeded))
    browser.expect_disabled('[data-test="cm-save"]')

    # A row edit shows in Raw JSON, and a Raw JSON edit shows in the rows.
    browser.click(_row("verbose", "cm-settings-bool"))
    browser.click(_row("statusLine", "cm-settings-edit-raw"))
    browser.expect_visible('[data-test="cm-settings-text"]')
    raw = json.loads(browser.get_value('[data-test="cm-settings-text"]'))
    assert raw == {**seeded, "verbose": False}
    browser.fill('[data-test="cm-settings-text"]', json.dumps({**raw, "feedbackSurveyRate": 0.5}))
    browser.click('[data-test="cm-settings-mode-rows"]')
    browser.expect_value(_row("feedbackSurveyRate", "cm-settings-number"), "0.5")
    assert _checked(browser, _row("verbose", "cm-settings-bool")) is False
    _save_alpha(browser)
    stored = json.loads(_alpha_settings_path(config_mgmt_server).read_text(encoding="utf-8"))
    assert stored == {**seeded, "verbose": False, "feedbackSurveyRate": 0.5}


def test_config_mgmt_settings_env_of_another_shape_is_a_read_only_row(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """An env that is not text-to-text is a read-only row, and the server refuses its save."""
    # The server masks each env value, so only an env that is not an object (masked whole)
    # reaches the page in another shape.
    _seed_alpha_settings(config_mgmt_server, {"env": ["A=1"], "verbose": True})
    before = _alpha_settings_path(config_mgmt_server).read_bytes()
    _open_settings_rows(browser, config_mgmt_server)
    browser.expect_visible(_row("env", "cm-settings-readonly"))
    browser.expect_hidden('[data-test="cm-settings-env-add"]')
    browser.click(_row("verbose", "cm-settings-bool"))
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_text('[data-test="cm-error"]', "'env' must be an object")
    assert _alpha_settings_path(config_mgmt_server).read_bytes() == before


def test_config_mgmt_settings_row_count_matches_merged_view(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Rows and the merged view agree on what exists, and the rows fit a 320 px phone."""
    _seed_alpha_settings(
        config_mgmt_server,
        {
            "verbose": True,
            "cleanupPeriodDays": 30,
            "tui": "fullscreen",
            "model": "opus",
            "env": {"A": "x"},
            "extraKnownMarketplaces": _MARKETPLACES,
            "companyAnnouncements": ["hello"],
        },
    )
    _open_settings_rows(browser, config_mgmt_server)
    browser.click('[data-test="cm-effective-toggle"]')
    browser.expect_visible('[data-test="cm-effective-table"]')
    browser.expect_count('[data-test="cm-effective-table"] tbody tr', 7)
    browser.expect_count('[data-test="cm-settings-key-row"]', 7)
    # The Settings view fits a 320 px phone, with no sideways scroll inside it. goto() sets
    # its own viewport, so resize after the page is open (the session closes after the
    # test, so no restore). The page header behind the modal is not measured here.
    browser.set_viewport(320, 740)
    widths = browser.eval_json(
        "(() => { const v = document.querySelector('[data-test=\"cm-view-settings\"]');"
        " return [window.innerWidth, Math.round(v.getBoundingClientRect().right),"
        " v.scrollWidth, v.clientWidth]; })()"
    )
    assert widths[0] == 320, widths
    assert widths[1] <= 320, widths
    assert widths[2] <= widths[3], widths


def test_config_mgmt_hooks_tab_loads(browser: AgentBrowser, config_mgmt_server: Server) -> None:
    """Switching to the Hooks tab loads its JSON editor for the scope."""
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    # Hooks defaults to the rows editor (#958 Part 5); drop to raw JSON to see the loaded {}.
    browser.click('[data-test="cm-hooks-mode-raw"]')
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


def test_config_mgmt_subagent_masks_a_frontmatter_secret_and_keeps_it_on_save(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """The editor shows a frontmatter secret as the mask, and a save keeps it (TASK-427)."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])
    agents = projects_root / "alpha" / ".claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    stored = "---\nname: masked-agent\ndescription: old\napi_token: fake-E2E\n---\nBody.\n"
    path = agents / "masked-agent.md"
    path.write_bytes(stored.encode("utf-8"))

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-subagents"]')
    browser.click('[data-test="cm-agent-edit-masked-agent"]')
    shown = '---\nname: masked-agent\ndescription: old\napi_token: "********"\n---\nBody.\n'
    # The driver reads the value back without its final newline.
    browser.expect_value('[data-test="cm-agent-content"]', shown.rstrip("\n"))

    browser.fill('[data-test="cm-agent-content"]', shown.replace("old", "new"))
    browser.fill('[data-test="cm-agent-confirm"]', "alpha")
    browser.click('[data-test="cm-agent-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    assert path.read_text(encoding="utf-8") == stored.replace("old", "new")


def test_config_mgmt_subagent_it_cannot_mask_shows_why_and_keeps_save_off(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A secret behind a YAML anchor is not shown, the note says why, and Save stays off."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])
    agents = projects_root / "alpha" / ".claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "anchor-agent.md").write_bytes(
        b"---\nname: anchor-agent\ndescription: d\napi_token: &t fake-ANCHOR\n"
        b"other_token: *t\n---\nBody.\n"
    )

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-subagents"]')
    browser.click('[data-test="cm-agent-edit-anchor-agent"]')
    browser.expect_visible('[data-test="cm-agent-converted"]')
    browser.expect_text('[data-test="cm-agent-converted"]', "cannot mask")
    browser.expect_value('[data-test="cm-agent-content"]', "")
    browser.fill('[data-test="cm-agent-confirm"]', "alpha")
    assert browser.eval_js("document.querySelector('[data-test=\"cm-agent-save\"]').disabled") in (
        "true",
        True,
    )


def test_config_mgmt_new_subagent_name_single_source(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """The Name box alone drives both the filename and the frontmatter name (#958 Part 5).

    The content carries NO `name:` line — the editor injects it from the Name box on save,
    so the backend's "frontmatter name must match the filename" check passes without the
    operator typing the name twice.
    """
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-subagents"]')
    browser.expect_visible('[data-test="cm-agent-new"]')
    browser.click('[data-test="cm-agent-new"]')
    browser.expect_visible('[data-test="cm-agent-editor"]')

    browser.fill('[data-test="cm-agent-name"]', "solo-agent")
    # No `name:` in the frontmatter — just description + body.
    browser.fill(
        '[data-test="cm-agent-content"]',
        "---\ndescription: single-source agent\n---\nDo it.\n",
    )
    browser.fill('[data-test="cm-agent-confirm"]', "alpha")
    browser.click('[data-test="cm-agent-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    saved = projects_root / "alpha" / ".claude" / "agents" / "solo-agent.md"
    assert saved.exists(), "expected alpha/.claude/agents/solo-agent.md to be written"
    text = saved.read_text(encoding="utf-8")
    assert "name: solo-agent" in text  # injected from the Name box
    assert "single-source agent" in text


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
    """Adding an MCP server through the Raw JSON editor writes it into the project .mcp.json.

    Raw JSON is the fallback for fields the form does not show (TASK-272). The entry
    carries an ``env`` value, so the backend routes it to the secret-safe direct writer
    (never the CLI) — proving the wired flow round-trips to disk
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
    _mcp_raw_mode(browser)
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


def test_config_mgmt_mcp_converted_entry_shows_note_and_saves_unedited(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A stored field Claude Code ignores is converted on load, named, and fixed by a save.

    The entry holds the legacy ``transport`` key and a remote ``env``. The list shows the
    conversion note, the editor repeats it, and Save is enabled with no edit (the converted
    definition differs from the file); saving writes the converted entry. A ``headers``
    value (shown masked) sends the save to the direct writer, so the fake ``claude`` is
    never needed and the masked value is restored from the stored one.
    """
    import json

    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])
    mcp_json = projects_root / "alpha" / ".mcp.json"
    # The server is module-scoped and later tests read this file and launch alpha, so
    # put it back exactly as it was, whatever happens here.
    prior = mcp_json.read_bytes() if mcp_json.exists() else None
    try:
        mcp_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "legacy": {
                            "transport": "sse",
                            "url": "https://x.example/sse",
                            "headers": {"X-Api": "kept-4498"},
                            "env": {"K": "v"},
                        },
                        "clean": {"command": "echo"},
                    }
                }
            ),
            encoding="utf-8",
        )

        _open_modal(browser, config_mgmt_server)
        browser.select('[data-test="cm-project"]', "alpha")
        browser.click('[data-test="cm-surface-mcp"]')
        browser.expect_visible('[data-test="cm-mcp-conversions"]')
        # The wrong-case guard for the empty-baseline trick: an unedited server that needed no
        # conversion stays unsavable even with the confirm typed, and shows no editor note.
        browser.click('[data-test="cm-mcp-edit-clean"]')
        browser.expect_visible('[data-test="cm-mcp-editor"]')
        browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
        browser.expect_disabled('[data-test="cm-mcp-save"]')
        browser.expect_hidden('[data-test="cm-mcp-entry-conversions"]')
        browser.click('[data-test="cm-mcp-cancel"]')
        browser.expect_visible('[data-test="cm-mcp-edit-legacy"]')  # a reload may run first
        browser.click('[data-test="cm-mcp-edit-legacy"]')
        browser.expect_visible('[data-test="cm-mcp-entry-conversions"]')
        browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
        browser.click('[data-test="cm-mcp-save"]')
        browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert data["mcpServers"]["legacy"] == {
            "type": "sse",
            "url": "https://x.example/sse",
            "headers": {"X-Api": "kept-4498"},
        }
    finally:
        if prior is None:
            mcp_json.unlink(missing_ok=True)
        else:
            mcp_json.write_bytes(prior)


# --- TASK-272: the MCP fields editor -------------------------------------------------------


@contextmanager
def _alpha_mcp_json(config_mgmt_server: Server, servers: dict | None) -> Iterator[Path]:
    """Seed alpha's ``.mcp.json`` with ``servers`` (or leave it absent), then restore it.

    The projects tree outlives one test, and later tests read this file and launch alpha,
    so it is put back exactly as it was, whatever happens in the test.
    """
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])
    mcp_json = projects_root / "alpha" / ".mcp.json"
    prior = mcp_json.read_bytes() if mcp_json.exists() else None
    try:
        if servers is None:
            mcp_json.unlink(missing_ok=True)
        else:
            mcp_json.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        yield mcp_json
    finally:
        if prior is None:
            mcp_json.unlink(missing_ok=True)
        else:
            mcp_json.write_bytes(prior)


def _open_mcp_new(browser: AgentBrowser, config_mgmt_server: Server, name: str) -> None:
    """Open alpha's MCP tab, start a new server in the fields editor and type its name."""
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-mcp"]')
    browser.expect_visible('[data-test="cm-mcp-new"]')  # wait for the list to finish loading
    browser.click('[data-test="cm-mcp-new"]')
    browser.expect_visible('[data-test="cm-mcp-fields"]')
    browser.fill('[data-test="cm-mcp-name"]', name)


def _save_mcp(browser: AgentBrowser) -> None:
    """Type the scope token, save, and wait for the saved notice."""
    browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
    browser.click('[data-test="cm-mcp-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)


_REMOTE_272 = {
    "type": "http",
    "url": "https://a.example/mcp",
    "headers": {"Authorization": "Bearer keep-272"},
}


def test_config_mgmt_mcp_fields_add_stdio(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A stdio server typed into the fields saves with no hand-written JSON (TASK-272).

    The env value sends the save to the secret-safe direct writer, so the fake ``claude``
    is never needed.
    """
    with _alpha_mcp_json(config_mgmt_server, None) as mcp_json:
        _open_mcp_new(browser, config_mgmt_server, "stdio-272")
        browser.fill('[data-test="cm-mcp-command"]', "echo")
        browser.click('[data-test="cm-mcp-arg-add"]')
        browser.expect_visible('[data-test="cm-mcp-arg"]')
        browser.fill('[data-test="cm-mcp-arg"]', "hi-272")
        browser.click('[data-test="cm-mcp-env-add"]')
        browser.expect_visible('[data-test="cm-mcp-env-key"]')
        browser.fill('[data-test="cm-mcp-env-key"]', "FOO")
        browser.fill('[data-test="cm-mcp-env-value"]', "bar-272")
        _save_mcp(browser)

        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert data["mcpServers"]["stdio-272"] == {
            "type": "stdio",
            "command": "echo",
            "args": ["hi-272"],
            "env": {"FOO": "bar-272"},
        }


def test_config_mgmt_mcp_fields_add_http_with_header(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Picking http swaps in the url and header fields, and the server saves (TASK-272)."""
    with _alpha_mcp_json(config_mgmt_server, None) as mcp_json:
        _open_mcp_new(browser, config_mgmt_server, "http-272")
        browser.select('[data-test="cm-mcp-type"]', "http")
        # The type switch replaces the stdio fields with the remote ones above the targets.
        browser.expect_visible('[data-test="cm-mcp-url"]')
        browser.expect_hidden('[data-test="cm-mcp-command"]')
        browser.fill('[data-test="cm-mcp-url"]', "https://h.example/mcp")
        browser.click('[data-test="cm-mcp-header-add"]')
        browser.expect_visible('[data-test="cm-mcp-header-key"]')
        browser.fill('[data-test="cm-mcp-header-key"]', "Authorization")
        browser.fill('[data-test="cm-mcp-header-value"]', "Bearer h-272")
        _save_mcp(browser)

        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert data["mcpServers"]["http-272"] == {
            "type": "http",
            "url": "https://h.example/mcp",
            "headers": {"Authorization": "Bearer h-272"},
        }


def test_config_mgmt_mcp_fields_edit_keeps_masked_header(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A masked header left unchanged keeps the stored value through a fields save.

    The stored header shows as the mask, never as its value. Only the timeout changes,
    so the destination is the same and the backend restores the header from disk.
    """
    with _alpha_mcp_json(config_mgmt_server, {"remote-272": _REMOTE_272}) as mcp_json:
        _open_modal(browser, config_mgmt_server)
        browser.select('[data-test="cm-project"]', "alpha")
        browser.click('[data-test="cm-surface-mcp"]')
        browser.click('[data-test="cm-mcp-edit-remote-272"]')
        browser.expect_visible('[data-test="cm-mcp-fields"]')
        browser.expect_value('[data-test="cm-mcp-url"]', "https://a.example/mcp")
        browser.expect_value('[data-test="cm-mcp-header-key"]', "Authorization")
        browser.expect_value('[data-test="cm-mcp-header-value"]', "********")
        browser.fill('[data-test="cm-mcp-timeout"]', "5000")
        browser.expect_hidden('[data-test="cm-mcp-retype"]')
        _save_mcp(browser)

        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert data["mcpServers"]["remote-272"] == {**_REMOTE_272, "timeout": 5000}


def test_config_mgmt_mcp_fields_url_change_asks_to_retype(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A new url with a masked header left in place explains the retype and blocks Save.

    The backend never sends a stored credential to a new destination. The editor names
    the masked value to retype and keeps Save off; once the header is retyped, the
    warning goes and the save writes the new url with the new header.
    """
    with _alpha_mcp_json(config_mgmt_server, {"remote-272": _REMOTE_272}) as mcp_json:
        _open_modal(browser, config_mgmt_server)
        browser.select('[data-test="cm-project"]', "alpha")
        browser.click('[data-test="cm-surface-mcp"]')
        browser.click('[data-test="cm-mcp-edit-remote-272"]')
        browser.expect_visible('[data-test="cm-mcp-fields"]')
        browser.expect_value('[data-test="cm-mcp-header-value"]', "********")
        browser.fill('[data-test="cm-mcp-url"]', "https://b.example/mcp")
        browser.expect_visible('[data-test="cm-mcp-retype"]')
        assert "headers.Authorization" in browser.get_text('[data-test="cm-mcp-retype"]')
        browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
        browser.expect_disabled('[data-test="cm-mcp-save"]')

        browser.fill('[data-test="cm-mcp-header-value"]', "Bearer new-272")
        browser.expect_hidden('[data-test="cm-mcp-retype"]')
        browser.click('[data-test="cm-mcp-save"]')
        browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert data["mcpServers"]["remote-272"] == {
            "type": "http",
            "url": "https://b.example/mcp",
            "headers": {"Authorization": "Bearer new-272"},
        }


def test_config_mgmt_mcp_fields_stdio_arg_change_asks_to_retype(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A new stdio argument with a masked env value left in place asks for the env value.

    The backend never hands a stored env value to a new command or argument list.
    """
    stored = {"command": "echo", "args": ["a"], "env": {"TOKEN": "keep-272"}}
    with _alpha_mcp_json(config_mgmt_server, {"cmd-272": stored}) as mcp_json:
        _open_modal(browser, config_mgmt_server)
        browser.select('[data-test="cm-project"]', "alpha")
        browser.click('[data-test="cm-surface-mcp"]')
        browser.click('[data-test="cm-mcp-edit-cmd-272"]')
        browser.expect_visible('[data-test="cm-mcp-fields"]')
        browser.expect_value('[data-test="cm-mcp-env-value"]', "********")
        browser.fill('[data-test="cm-mcp-arg"]', "b")
        browser.expect_visible('[data-test="cm-mcp-retype"]')
        text = browser.get_text('[data-test="cm-mcp-retype"]')
        assert "command or its arguments" in text and "env.TOKEN" in text
        browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
        browser.expect_disabled('[data-test="cm-mcp-save"]')

        browser.fill('[data-test="cm-mcp-env-value"]', "new-272")
        browser.expect_hidden('[data-test="cm-mcp-retype"]')
        browser.click('[data-test="cm-mcp-save"]')
        browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert data["mcpServers"]["cmd-272"] == {
            "command": "echo",
            "args": ["b"],
            "env": {"TOKEN": "new-272"},
        }


def test_config_mgmt_mcp_fields_renamed_masked_header_asks_for_value(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Renaming a header that still shows the mask asks for its value: nothing is stored there."""
    with _alpha_mcp_json(config_mgmt_server, {"remote-272": _REMOTE_272}):
        _open_modal(browser, config_mgmt_server)
        browser.select('[data-test="cm-project"]', "alpha")
        browser.click('[data-test="cm-surface-mcp"]')
        browser.click('[data-test="cm-mcp-edit-remote-272"]')
        browser.expect_visible('[data-test="cm-mcp-fields"]')
        browser.fill('[data-test="cm-mcp-header-key"]', "X-Renamed")
        browser.expect_visible('[data-test="cm-mcp-retype"]')
        assert "headers.X-Renamed" in browser.get_text('[data-test="cm-mcp-retype"]')
        browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
        browser.expect_disabled('[data-test="cm-mcp-save"]')


def _env_row(idx: int, field: str) -> str:
    """The selector of one env row's ``key`` or ``value`` input (0-based row index)."""
    return f'[data-test="cm-mcp-env-row-{idx}"] [data-test="cm-mcp-env-{field}"]'


def test_config_mgmt_mcp_raw_switch_refused_while_rows_would_lose_values(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Raw JSON is refused while an env row has no name or shares one, and no value is lost.

    The form writes one value per name and drops an unnamed row, so a switch would replace
    the rows with less. The refusal names the rows; once they are fixed, the switch and the
    switch back keep every value.
    """
    with _alpha_mcp_json(config_mgmt_server, None):
        _open_mcp_new(browser, config_mgmt_server, "rows-272")
        browser.fill('[data-test="cm-mcp-command"]', "echo")
        for count in (1, 2, 3):
            browser.click('[data-test="cm-mcp-env-add"]')
            browser.expect_count('[data-test="cm-mcp-env-key"]', count)
        rows = [("FOO", "a-272"), ("FOO", "b-272"), ("", "c-272")]
        for idx, (key, value) in enumerate(rows):
            if key:
                browser.fill(_env_row(idx, "key"), key)
            browser.fill(_env_row(idx, "value"), value)

        browser.click('[data-test="cm-mcp-mode-raw"]')
        browser.expect_visible('[data-test="cm-mcp-raw-blocked"]')
        text = browser.get_text('[data-test="cm-mcp-fields-warn"]')
        assert "environment variable 3" in text and "rows 1, 2" in text
        browser.expect_visible('[data-test="cm-mcp-fields"]')
        browser.expect_hidden('[data-test="cm-mcp-raw"]')
        for idx, (key, value) in enumerate(rows):
            browser.expect_value(_env_row(idx, "key"), key)
            browser.expect_value(_env_row(idx, "value"), value)

        # Any edit that changes the problems hides the refusal note for good, even while
        # another row (the unnamed one) still needs a fix.
        browser.fill(_env_row(1, "key"), "BAR")
        browser.expect_hidden('[data-test="cm-mcp-raw-blocked"]')
        browser.fill(_env_row(2, "key"), "BAZ")
        browser.expect_hidden('[data-test="cm-mcp-fields-warn"]')
        browser.click('[data-test="cm-mcp-mode-raw"]')
        browser.expect_visible('[data-test="cm-mcp-entry"]')
        entry = json.loads(browser.get_value('[data-test="cm-mcp-entry"]'))
        assert entry["env"] == {"FOO": "a-272", "BAR": "b-272", "BAZ": "c-272"}
        browser.click('[data-test="cm-mcp-mode-fields"]')
        browser.expect_count('[data-test="cm-mcp-env-key"]', 3)
        browser.expect_value(_env_row(2, "value"), "c-272")


def test_config_mgmt_mcp_multiline_arg_opens_in_raw_json_and_round_trips(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A line break in an argument keeps the server in Raw JSON, and a save keeps it exact.

    The fields are single-line inputs, which would drop the line break on the next save.
    """
    script = "print(1)\nprint(2)"
    stored = {"command": "python", "args": ["-c", script]}
    with _alpha_mcp_json(config_mgmt_server, {"py-272": stored}) as mcp_json:
        _open_modal(browser, config_mgmt_server)
        browser.select('[data-test="cm-project"]', "alpha")
        browser.click('[data-test="cm-surface-mcp"]')
        browser.click('[data-test="cm-mcp-edit-py-272"]')
        browser.expect_visible('[data-test="cm-mcp-raw"]')
        browser.expect_hidden('[data-test="cm-mcp-fields"]')
        assert "args[1]" in browser.get_text('[data-test="cm-mcp-fields-error"]')
        browser.click('[data-test="cm-mcp-mode-fields"]')
        browser.expect_visible('[data-test="cm-mcp-raw"]')
        browser.expect_hidden('[data-test="cm-mcp-fields"]')

        # Edit another field in Raw JSON and save: the script keeps its line break.
        browser.fill('[data-test="cm-mcp-entry"]', json.dumps({**stored, "timeout": 5000}))
        _save_mcp(browser)
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert data["mcpServers"]["py-272"] == {**stored, "timeout": 5000}
        assert data["mcpServers"]["py-272"]["args"][1] == script


def test_config_mgmt_mcp_padded_env_name_opens_in_raw_json(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """An env name with spaces around it opens in Raw JSON: the form would trim it."""
    with _alpha_mcp_json(config_mgmt_server, {"pad-272": {"command": "echo", "env": {" A": "1"}}}):
        _open_modal(browser, config_mgmt_server)
        browser.select('[data-test="cm-project"]', "alpha")
        browser.click('[data-test="cm-surface-mcp"]')
        browser.click('[data-test="cm-mcp-edit-pad-272"]')
        browser.expect_visible('[data-test="cm-mcp-raw"]')
        browser.expect_hidden('[data-test="cm-mcp-fields"]')
        assert 'env " A"' in browser.get_text('[data-test="cm-mcp-fields-error"]')


def test_config_mgmt_mcp_unshown_field_opens_in_raw_json(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A server with a field the form does not show opens in Raw JSON and says why."""
    servers = {"role-272": {"command": "echo", "role": "comms"}}
    with _alpha_mcp_json(config_mgmt_server, servers):
        _open_modal(browser, config_mgmt_server)
        browser.select('[data-test="cm-project"]', "alpha")
        browser.click('[data-test="cm-surface-mcp"]')
        browser.click('[data-test="cm-mcp-edit-role-272"]')
        browser.expect_visible('[data-test="cm-mcp-raw"]')
        browser.expect_hidden('[data-test="cm-mcp-fields"]')
        assert "role" in browser.get_text('[data-test="cm-mcp-fields-error"]')
        # Fields stays refused while the entry still has the field.
        browser.click('[data-test="cm-mcp-mode-fields"]')
        browser.expect_visible('[data-test="cm-mcp-raw"]')
        browser.expect_hidden('[data-test="cm-mcp-fields"]')


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


# Lets the skill PUT's headers through but holds its body until `__releaseBody()` runs: the
# window between `res.ok` and `res.json()` in which the operator can start a new skill.
_HOLD_SKILL_PUT_BODY = """(() => {
  window.__holdBody = new Promise(r => { window.__releaseBody = r; });
  const real = window.fetch;
  window.fetch = function (url, opts) {
    const p = real.apply(this, arguments);
    const u = String(url);
    const isPut = opts && opts.method === "PUT" && u.includes("/api/config-write/skills")
      && !u.includes("/overrides");
    if (!isPut) return p;
    return p.then(res => {
      const json = res.json.bind(res);
      res.json = () => window.__holdBody.then(json);
      return res;
    });
  };
  return true;
})()"""


def test_config_mgmt_new_skill_opened_during_a_save_stays_open(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A New skill editor opened while a save's body is read is not closed by its reload."""
    alpha = _projects_root(config_mgmt_server) / "alpha"
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-skills"]')
    browser.expect_visible('[data-test="cm-skill-new"]')
    browser.click('[data-test="cm-skill-new"]')
    browser.fill('[data-test="cm-skill-name"]', "first-skill")
    browser.fill('[data-test="cm-skill-content"]', "---\nname: first-skill\n---\nBody.\n")
    browser.fill('[data-test="cm-skill-confirm"]', "alpha")
    assert browser.eval_json(_HOLD_SKILL_PUT_BODY) is True
    browser.click('[data-test="cm-skill-save"]')
    saved = alpha / ".claude" / "skills" / "first-skill" / "SKILL.md"
    deadline = time.monotonic() + 10
    while not saved.exists():
        assert time.monotonic() < deadline, "the held save never reached the server"
        time.sleep(0.1)
    browser.expect_visible('[data-test="cm-skill-new"]')
    browser.click('[data-test="cm-skill-new"]')  # a second, new editor during the wait
    browser.fill('[data-test="cm-skill-name"]', "second-skill")
    browser.eval_json("(() => { window.__releaseBody(); return true; })()")
    time.sleep(1.0)  # let a reload, if one ran, finish
    browser.expect_visible('[data-test="cm-skill-editor"]')
    browser.expect_value('[data-test="cm-skill-name"]', "second-skill")


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


# --- skill visibility (TASK-249): the Skills tab's `skillOverrides` panel ---------------


def _projects_root(server: Server) -> Path:
    cfg_path = Path(server.state_dir).parent / "clauster.yml"
    return Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])


def _seed_skill(project_dir: Path, name: str) -> None:
    skill_md = project_dir / ".claude" / "skills" / name / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(f"---\ndescription: {name} skill\n---\nDo it.\n", encoding="utf-8")


def _open_skill_visibility(browser: AgentBrowser, server: Server) -> None:
    """Open the Skills tab for alpha at Project scope and wait for the visibility rows."""
    _open_modal(browser, server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-skills"]')
    browser.expect_visible('[data-test="cm-skill-ov-add-name"]')


def _save_skill_visibility(browser: AgentBrowser, token: str) -> None:
    browser.expect_visible('[data-test="cm-skill-ov-confirm"]')
    browser.fill('[data-test="cm-skill-ov-confirm"]', token)
    browser.click('[data-test="cm-skill-ov-save"]')


def _expect_focused(browser: AgentBrowser, element_id: str, timeout_s: float = 5.0) -> None:
    """Wait until the element with ``element_id`` holds keyboard focus (set on $nextTick)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if browser.eval_json("document.activeElement && document.activeElement.id") == element_id:
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for #{element_id} to have focus")


def _add_skill_override(browser: AgentBrowser, name: str, value: str) -> None:
    browser.fill('[data-test="cm-skill-ov-add-name"]', name)
    browser.select('[data-test="cm-skill-ov-add-value"]', value)
    browser.click('[data-test="cm-skill-ov-add"]')
    browser.expect_visible(f'[data-test="cm-skill-ov-row-{name}"]')


def test_config_mgmt_skill_visibility_project_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """List, set, add and remove skill overrides at Project scope; the map lands on disk."""
    alpha = _projects_root(config_mgmt_server) / "alpha"
    _seed_skill(alpha, "greeter")
    settings = alpha / ".claude" / "settings.json"
    settings.write_text(
        json.dumps({"model": "opus", "skillOverrides": {"orphan": "off"}}), encoding="utf-8"
    )

    _open_skill_visibility(browser, config_mgmt_server)
    # A listed folder with no override, and a stored key that matches no folder.
    browser.expect_value('[data-test="cm-skill-ov-greeter"]', "")
    browser.expect_hidden('[data-test="cm-skill-ov-unlisted-greeter"]')
    browser.expect_value('[data-test="cm-skill-ov-orphan"]', "off")
    browser.expect_visible('[data-test="cm-skill-ov-unlisted-orphan"]')
    browser.expect_hidden('[data-test="cm-skill-ov-confirm"]')  # nothing changed yet

    browser.select('[data-test="cm-skill-ov-greeter"]', "name-only")
    browser.select('[data-test="cm-skill-ov-orphan"]', "")  # No override: remove the key
    _add_skill_override(browser, "doctor", "off")  # a bundled skill: no folder
    # Add disables itself once the name clears, so focus must return to the name box.
    _expect_focused(browser, "cm-skill-ov-add-name")
    _save_skill_visibility(browser, "alpha")
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    # Save hid itself once nothing was dirty, so focus moves to the name box, not the body.
    _expect_focused(browser, "cm-skill-ov-add-name")

    on_disk = json.loads(settings.read_text(encoding="utf-8"))
    assert on_disk["skillOverrides"] == {"greeter": "name-only", "doctor": "off"}
    assert on_disk["model"] == "opus"
    # The reload drops the removed key's row and keeps the added one.
    browser.expect_hidden('[data-test="cm-skill-ov-row-orphan"]')
    browser.expect_value('[data-test="cm-skill-ov-doctor"]', "off")
    browser.expect_hidden('[data-test="cm-skill-ov-confirm"]')


def test_config_mgmt_skill_visibility_local_and_user_scope(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Local scope writes settings.local.json from the project's rows; User scope writes ~."""
    alpha = _projects_root(config_mgmt_server) / "alpha"
    home = Path(config_mgmt_server.state_dir).parent / "home"
    _seed_skill(alpha, "greeter")

    _open_skill_visibility(browser, config_mgmt_server)
    browser.click('[data-test="cm-scope-local"]')
    browser.expect_visible('[data-test="cm-skill-local-note"]')
    browser.expect_hidden('[data-test="cm-skill-new"]')  # no local skills folder
    browser.expect_value('[data-test="cm-skill-ov-greeter"]', "")
    browser.select('[data-test="cm-skill-ov-greeter"]', "off")
    _save_skill_visibility(browser, "alpha (local)")
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    local = json.loads((alpha / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert local["skillOverrides"] == {"greeter": "off"}
    assert not (alpha / ".claude" / "settings.json").exists()  # Project scope untouched

    _switch_to_user_scope(browser)
    browser.expect_hidden('[data-test="cm-skill-local-note"]')
    browser.expect_visible('[data-test="cm-skill-new"]')
    browser.expect_visible('[data-test="cm-skill-ov-empty"]')  # no user skills, no keys
    _add_skill_override(browser, "anthropic-skills:pdf", "user-invocable-only")
    _save_skill_visibility(browser, "USER SCOPE")
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    user = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert user["skillOverrides"] == {"anthropic-skills:pdf": "user-invocable-only"}


def test_config_mgmt_skill_visibility_local_lists_user_skill_folders(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Local rows name the user skill folders too: a local key acts on a user skill (TASK-494)."""
    alpha = _projects_root(config_mgmt_server) / "alpha"
    home = Path(config_mgmt_server.state_dir).parent / "home"
    _seed_skill(alpha, "greeter")
    _seed_skill(home, "helper")  # a user skill folder: ~/.claude/skills/helper
    _seed_skill(home, "greeter")  # the same name at both scopes is one row

    _open_skill_visibility(browser, config_mgmt_server)
    browser.expect_hidden('[data-test="cm-skill-ov-row-helper"]')  # Project: not listed
    browser.click('[data-test="cm-scope-local"]')
    browser.expect_visible('[data-test="cm-skill-local-note"]')
    browser.expect_visible('[data-test="cm-skill-ov-row-helper"]')
    browser.expect_value('[data-test="cm-skill-ov-helper"]', "")
    browser.expect_hidden('[data-test="cm-skill-ov-unlisted-helper"]')
    assert (
        browser.eval_json(
            "document.querySelectorAll('[data-test=\"cm-skill-ov-row-greeter\"]').length"
        )
        == 1
    )
    browser.select('[data-test="cm-skill-ov-helper"]', "off")
    _save_skill_visibility(browser, "alpha (local)")
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    local = json.loads((alpha / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert local["skillOverrides"] == {"helper": "off"}
    assert not (home / ".claude" / "settings.json").exists()  # User scope untouched


_FAIL_USER_SKILLS_GET = """(() => {
  const orig = window.fetch;
  window.fetch = (url, opts) => {
    if (String(url).includes("/api/config-write/skills?scope=user")) return %s;
    return orig(url, opts);
  };
  return "stubbed";
})()"""


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (
            'Promise.resolve(new Response("{}", { status: 500 }))',
            "User skills: Failed to load (500).",
        ),
        ('Promise.reject(new TypeError("Failed to fetch"))', "Failed to load user skills."),
    ],
    ids=["http-error", "network-error"],
)
def test_config_mgmt_skill_visibility_local_failed_user_list_keeps_project_rows(
    browser: AgentBrowser, config_mgmt_server: Server, failure: str, message: str
) -> None:
    """A failed user list keeps the project's Local rows and names itself in the banner."""
    alpha = _projects_root(config_mgmt_server) / "alpha"
    home = Path(config_mgmt_server.state_dir).parent / "home"
    _seed_skill(alpha, "greeter")
    _seed_skill(home, "helper")

    _open_skill_visibility(browser, config_mgmt_server)
    assert browser.eval_json(_FAIL_USER_SKILLS_GET % failure) == "stubbed"
    browser.click('[data-test="cm-scope-local"]')
    browser.expect_visible('[data-test="cm-skill-ov-row-greeter"]')
    browser.expect_text('[data-test="cm-error"]', message)
    browser.expect_count('[data-test="cm-skill-ov-row-helper"]', 0)


def test_config_mgmt_skill_visibility_local_without_user_scope_lists_project_only(
    browser: AgentBrowser, config_mgmt_project_only_server: Server
) -> None:
    """Without allow_user_scope the Local rows stay the project's, with no error banner."""
    server = config_mgmt_project_only_server
    alpha = _projects_root(server) / "alpha"
    home = Path(server.state_dir).parent / "home"
    _seed_skill(alpha, "greeter")
    _seed_skill(home, "helper")

    _open_skill_visibility(browser, server)
    browser.click('[data-test="cm-scope-local"]')
    browser.expect_visible('[data-test="cm-skill-local-note"]')
    browser.expect_visible('[data-test="cm-skill-ov-row-greeter"]')
    browser.expect_value('[data-test="cm-skill-ov-greeter"]', "")
    browser.expect_hidden('[data-test="cm-skill-ov-row-helper"]')
    browser.expect_hidden('[data-test="cm-error"]')


def test_config_mgmt_skill_visibility_refused_key_shows_backend_message(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A key the backend refuses shows its message, and nothing is written."""
    settings = _projects_root(config_mgmt_server) / "alpha" / ".claude" / "settings.json"

    _open_skill_visibility(browser, config_mgmt_server)
    _add_skill_override(browser, "a<b", "off")  # Claude Code cannot show `<` in a name
    _save_skill_visibility(browser, "alpha")
    browser.expect_text(
        '[data-test="cm-error"]', "skillOverrides has an invalid skill name: 'a<b'"
    )
    assert not settings.exists()

    # `__proto__` reaches the backend as an own key, so its rule refuses it by name too.
    browser.select('[data-test="cm-skill-ov-a<b"]', "")
    _add_skill_override(browser, "__proto__", "off")
    _save_skill_visibility(browser, "alpha")
    browser.expect_text(
        '[data-test="cm-error"]', "skillOverrides has an invalid skill name: '__proto__'"
    )
    assert not settings.exists()


def test_config_mgmt_skill_visibility_stale_hash_is_refused(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A settings file changed after the load is not overwritten; the conflict shows."""
    settings = _projects_root(config_mgmt_server) / "alpha" / ".claude" / "settings.json"
    _open_skill_visibility(browser, config_mgmt_server)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"skillOverrides": {"elsewhere": "on"}}), encoding="utf-8")

    _add_skill_override(browser, "doctor", "off")
    _save_skill_visibility(browser, "alpha")
    browser.expect_text('[data-test="cm-error"]', "Changed on disk")
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "skillOverrides": {"elsewhere": "on"}
    }


# Holds the response of the skill-visibility PUT in the page until `__release249()` runs.
# The request itself goes out at once, so the write lands on disk while the page still
# waits: exactly the window in which an operator can click Edit on a skill.
_HOLD_VISIBILITY_PUT = """(() => {
  window.__hold249 = new Promise(r => { window.__release249 = r; });
  const real = window.fetch;
  window.fetch = function (url, opts) {
    const p = real.apply(this, arguments);
    const isPut = opts && opts.method === "PUT" && String(url).includes("/skills/overrides");
    return isPut ? window.__hold249.then(() => p) : p;
  };
  return true;
})()"""


def test_config_mgmt_skill_visibility_save_lands_across_editor_navigation(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Edit clicked during a visibility save does not drop the saved hash and baseline."""
    alpha = _projects_root(config_mgmt_server) / "alpha"
    _seed_skill(alpha, "greeter")
    settings = alpha / ".claude" / "settings.json"

    _open_skill_visibility(browser, config_mgmt_server)
    browser.select('[data-test="cm-skill-ov-greeter"]', "off")
    assert browser.eval_json(_HOLD_VISIBILITY_PUT) is True
    _save_skill_visibility(browser, "alpha")

    # The write lands while the page still waits for the response.
    deadline = time.monotonic() + 10
    while not (settings.exists() and "greeter" in settings.read_text(encoding="utf-8")):
        assert time.monotonic() < deadline, "the held save never reached the server"
        time.sleep(0.1)
    browser.click('[data-test="cm-skill-edit-greeter"]')  # bumps the editor's gen
    browser.expect_visible('[data-test="cm-skill-editor"]')
    browser.eval_json("(() => { window.__release249(); return true; })()")
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    browser.click('[data-test="cm-skill-cancel"]')

    # Back in the list: the saved value is the baseline, so nothing reads as unsaved.
    browser.expect_value('[data-test="cm-skill-ov-greeter"]', "off")
    browser.expect_hidden('[data-test="cm-skill-ov-changed-greeter"]')
    browser.expect_hidden('[data-test="cm-skill-ov-confirm"]')

    # A second save uses the new hash, so it is not refused as changed on disk.
    browser.select('[data-test="cm-skill-ov-greeter"]', "name-only")
    _save_skill_visibility(browser, "alpha")
    # "Saved." may still show from the first save, so wait on the outcome instead: the
    # confirm hides only once the reload makes the new value the baseline.
    browser.expect_hidden('[data-test="cm-skill-ov-confirm"]', timeout_ms=_SAVE_TIMEOUT)
    browser.expect_hidden('[data-test="cm-error"]')
    on_disk = json.loads(settings.read_text(encoding="utf-8"))
    assert on_disk["skillOverrides"] == {"greeter": "name-only"}


def _start_held_visibility_save(browser: AgentBrowser, settings: Path) -> None:
    """Save greeter=off with the response held, and wait until the write is on disk."""
    browser.select('[data-test="cm-skill-ov-greeter"]', "off")
    assert browser.eval_json(_HOLD_VISIBILITY_PUT) is True
    _save_skill_visibility(browser, "alpha")
    deadline = time.monotonic() + 10
    while not (settings.exists() and "greeter" in settings.read_text(encoding="utf-8")):
        assert time.monotonic() < deadline, "the held save never reached the server"
        time.sleep(0.1)


_MODAL_SCROLL_TOP = (
    "document.querySelector('[data-test=\"configmgmt-modal\"] .modal-body').scrollTop"
)


def _release_held_save(browser: AgentBrowser) -> None:
    browser.eval_json("(() => { window.__release249(); return true; })()")


def test_config_mgmt_skill_visibility_save_does_not_take_focus_from_delete(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A save that lands while a Delete confirm is open leaves focus in that confirm."""
    alpha = _projects_root(config_mgmt_server) / "alpha"
    _seed_skill(alpha, "greeter")
    settings = alpha / ".claude" / "settings.json"

    _open_skill_visibility(browser, config_mgmt_server)
    # A short window, so the modal body scrolls: the banner and the confirm are not both
    # in view, and a wrong "Saved." reveal would move scrollTop.
    browser.set_viewport(1280, 560)
    _start_held_visibility_save(browser, settings)
    browser.scroll_into_view('[data-test="cm-skill-del-greeter"]')
    browser.click('[data-test="cm-skill-del-greeter"]')
    browser.expect_visible('[data-test="cm-skill-delete-confirm"]')
    _expect_focused(browser, "cm-skill-delete-input")
    time.sleep(0.5)  # let the confirm's own smooth scroll finish before measuring
    scroll_before = browser.eval_json(_MODAL_SCROLL_TOP)
    _release_held_save(browser)
    # The save landed: its reload made "off" the baseline.
    browser.expect_hidden('[data-test="cm-skill-ov-changed-greeter"]', timeout_ms=_SAVE_TIMEOUT)
    browser.expect_value('[data-test="cm-skill-ov-greeter"]', "off")
    time.sleep(0.5)  # past the frames in which a wrong focus move or scroll would happen
    _expect_focused(browser, "cm-skill-delete-input")
    # Nor does the "Saved." reveal scroll the operator up to the banner, away from the
    # confirm. The panel's reload can still shift the layout (scroll anchoring moves
    # scrollTop down a little), so the pin is the direction: never back toward the top.
    assert browser.eval_json(_MODAL_SCROLL_TOP) >= scroll_before


# Holds the response of the FIRST skills-list GET (`/api/config-write/skills?`, not the
# file or overrides routes) until `__releaseList249()` runs. Later list GETs pass.
_HOLD_FIRST_LIST_GET = """(() => {
  window.__holdList249 = new Promise(r => { window.__releaseList249 = r; });
  let armed = true;
  const real = window.fetch;
  window.fetch = function (url, opts) {
    const p = real.apply(this, arguments);
    const isGet = !opts || !opts.method || opts.method === "GET";
    if (armed && isGet && String(url).includes("/api/config-write/skills?")) {
      armed = false;
      return window.__holdList249.then(() => p);
    }
    return p;
  };
  return true;
})()"""


def test_config_mgmt_skill_list_reload_dropped_after_scope_switch(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A SKILL.md save's list reload that lands after a scope switch does not show."""
    alpha = _projects_root(config_mgmt_server) / "alpha"
    _open_skill_visibility(browser, config_mgmt_server)
    browser.click('[data-test="cm-skill-new"]')
    browser.expect_visible('[data-test="cm-skill-editor"]')
    browser.fill('[data-test="cm-skill-name"]', "fresh")
    browser.fill('[data-test="cm-skill-content"]', "---\ndescription: new\n---\nGo.\n")
    browser.fill('[data-test="cm-skill-confirm"]', "alpha")
    assert browser.eval_json(_HOLD_FIRST_LIST_GET) is True
    browser.click('[data-test="cm-skill-save"]')  # the write lands; its list reload is held
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    assert (alpha / ".claude" / "skills" / "fresh" / "SKILL.md").exists()

    browser.click('[data-test="cm-scope-local"]')  # no folder list at Local scope
    browser.expect_visible('[data-test="cm-skill-local-note"]')
    browser.expect_visible('[data-test="cm-skill-ov-row-fresh"]')  # named by project folders
    browser.eval_json("(() => { window.__releaseList249(); return true; })()")
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:  # the stale Project list would appear in this window
        assert not browser.is_visible('[data-test="cm-skills-table"]'), "stale list shown"
        time.sleep(0.1)


def test_config_mgmt_skill_visibility_save_dropped_after_surface_switch(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A save that lands after a switch to another tab shows nothing on that tab."""
    alpha = _projects_root(config_mgmt_server) / "alpha"
    _seed_skill(alpha, "greeter")
    settings = alpha / ".claude" / "settings.json"

    _open_skill_visibility(browser, config_mgmt_server)
    _start_held_visibility_save(browser, settings)
    browser.click('[data-test="cm-surface-claude-md"]')
    browser.expect_visible('[data-test="cm-view-claude-md"]')
    _release_held_save(browser)
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:  # a late "Saved." would appear within this window
        assert not browser.is_visible('[data-test="cm-saved"]'), "a dropped save showed Saved."
        time.sleep(0.1)

    # Back on Skills, a fresh load shows the value on disk as the baseline.
    browser.click('[data-test="cm-surface-skills"]')
    browser.expect_value('[data-test="cm-skill-ov-greeter"]', "off")
    browser.expect_hidden('[data-test="cm-skill-ov-changed-greeter"]')


def test_config_mgmt_skill_save_keeps_unsaved_visibility_choices(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A SKILL.md save reloads the folder list but keeps unsaved visibility choices."""
    _open_skill_visibility(browser, config_mgmt_server)
    _add_skill_override(browser, "doctor", "off")

    browser.click('[data-test="cm-skill-new"]')
    browser.expect_visible('[data-test="cm-skill-editor"]')
    browser.fill('[data-test="cm-skill-name"]', "fresh")
    browser.fill('[data-test="cm-skill-content"]', "---\ndescription: new\n---\nGo.\n")
    browser.fill('[data-test="cm-skill-confirm"]', "alpha")
    browser.click('[data-test="cm-skill-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    # The new folder gets a row, and the unsaved "doctor" choice is still there to save.
    browser.expect_value('[data-test="cm-skill-ov-fresh"]', "")
    browser.expect_value('[data-test="cm-skill-ov-doctor"]', "off")
    browser.expect_visible('[data-test="cm-skill-ov-confirm"]')


def test_config_mgmt_skill_visibility_fits_a_phone_width(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """At 320 px the visibility panel has no horizontal overflow, also with a long name.

    A stored value outside the known four shows as its own "Not a known value" choice.
    """
    alpha = _projects_root(config_mgmt_server) / "alpha"
    _seed_skill(alpha, "a-rather-long-skill-folder-name-that-must-wrap")
    (alpha / ".claude" / "settings.json").write_text(
        json.dumps({"skillOverrides": {"orphan": "off", "odd": "maybe"}}), encoding="utf-8"
    )
    _open_skill_visibility(browser, config_mgmt_server)
    browser.expect_value('[data-test="cm-skill-ov-odd"]', "?")
    browser.expect_text('[data-test="cm-skill-ov-odd"]', 'Not a known value: "maybe"')
    browser.set_viewport(320, 900)  # the session closes after the test, so no restore
    browser.select(
        '[data-test="cm-skill-ov-a-rather-long-skill-folder-name-that-must-wrap"]',
        "user-invocable-only",
    )
    browser.expect_visible('[data-test="cm-skill-ov-confirm"]')
    overflow = browser.eval_json(
        "(() => { const sels = ['[data-test=\"configmgmt-modal\"] .modal-body',"
        " '[data-test=\"cm-skill-ov\"]'];"
        " return sels.map(s => { const e = document.querySelector(s);"
        " return e ? e.scrollWidth - e.clientWidth : null; }); })()"
    )
    assert overflow == [0, 0], overflow

    # The stored value is sent unchanged: it saves, with a warning that names it (TASK-524).
    _save_skill_visibility(browser, "alpha")
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    browser.expect_text(
        '[data-test="cm-save-warnings"]', "skillOverrides['odd'] 'maybe' is not one of"
    )


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
    # Rows is the default; drop to raw JSON to save a literal hooks block (the rows path
    # is covered by test_config_mgmt_hooks_rows_round_trip below).
    browser.click('[data-test="cm-hooks-mode-raw"]')
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


def test_config_mgmt_hooks_rows_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Adding a hook via the rows editor writes the nested command group to settings.json."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    # Rows mode default: add a hook, pick the event, type the command, save.
    browser.click('[data-test="cm-hooks-add"]')
    browser.expect_visible('[data-test="cm-hook-command"]')  # let the x-for row hydrate
    browser.fill('[data-test="cm-hook-event"]', "SessionStart")
    browser.fill('[data-test="cm-hook-command"]', "echo rows-hook")
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    on_disk = json.loads(
        (projects_root / "alpha" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert on_disk["hooks"] == {
        "SessionStart": [{"hooks": [{"type": "command", "command": "echo rows-hook"}]}]
    }


def test_config_mgmt_hooks_unknown_event_saves_with_a_visible_warning(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """An event outside the known list saves as typed, with a warning (TASK-524)."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    browser.click('[data-test="cm-hooks-add"]')
    browser.expect_visible('[data-test="cm-hook-command"]')
    # A known event shows no warning; the positive control for the check below.
    browser.fill('[data-test="cm-hook-event"]', "Stop")
    browser.expect_hidden('[data-test="cm-hook-event-warning"]')
    browser.fill('[data-test="cm-hook-event"]', "FutureEvent")
    browser.expect_text('[data-test="cm-hook-event-warning"]', "Not one of the known events")
    browser.fill('[data-test="cm-hook-command"]', "echo future")
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    browser.expect_text(
        '[data-test="cm-save-warnings"]', "'FutureEvent' is not one of the known values"
    )

    on_disk = json.loads(
        (projects_root / "alpha" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert on_disk["hooks"] == {
        "FutureEvent": [{"hooks": [{"type": "command", "command": "echo future"}]}]
    }

    # A later save with only known events clears the warning, on screen and in the live
    # region.
    browser.expect_visible('[data-test="cm-hook-event"]')
    browser.fill('[data-test="cm-hook-event"]', "Stop")
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    browser.expect_hidden('[data-test="cm-save-warnings"]')
    assert browser.get_text('[data-test="cm-save-warnings-live"]').strip() == ""

    # An event named `__proto__` stays an own key of the saved block, never a prototype.
    browser.fill('[data-test="cm-hook-event"]', "__proto__")
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_text('[data-test="cm-save-warnings"]', "'__proto__' is not one of")
    on_disk = json.loads(
        (projects_root / "alpha" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert on_disk["hooks"] == {
        "__proto__": [{"hooks": [{"type": "command", "command": "echo future"}]}]
    }


def test_config_mgmt_hooks_rows_http_and_mcp_tool_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A row's type select swaps in the type's fields; http and mcp_tool rows save (TASK-449.4)."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    browser.click('[data-test="cm-hooks-add"]')
    browser.expect_visible('[data-test="cm-hook-command"]')
    browser.fill('[data-test="cm-hook-event"]', "PostToolUse")
    browser.select('[data-test="cm-hook-type"]', "http")
    browser.expect_visible('[data-test="cm-hook-url"]')
    browser.fill('[data-test="cm-hook-url"]', "https://hooks.example.com/in")
    browser.click('[data-test="cm-hooks-add"]')
    browser.expect_visible('[data-test="cm-hook-row-1"]')
    browser.fill('[data-test="cm-hook-row-1"] [data-test="cm-hook-event"]', "Stop")
    browser.select('[data-test="cm-hook-row-1"] [data-test="cm-hook-type"]', "mcp_tool")
    browser.expect_visible('[data-test="cm-hook-row-1"] [data-test="cm-hook-server"]')
    browser.fill('[data-test="cm-hook-row-1"] [data-test="cm-hook-server"]', "scanner")
    browser.fill('[data-test="cm-hook-row-1"] [data-test="cm-hook-tool"]', "scan")
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    on_disk = json.loads(
        (projects_root / "alpha" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert on_disk["hooks"] == {
        "PostToolUse": [{"hooks": [{"type": "http", "url": "https://hooks.example.com/in"}]}],
        "Stop": [{"hooks": [{"type": "mcp_tool", "server": "scanner", "tool": "scan"}]}],
    }


# TASK-513: the handler inputs each hook type shows, and nothing else. Every key must be
# an option of the type select (the test asserts the two sets are equal), so a new type
# cannot slip past without a row here.
_HOOK_TYPE_INPUTS = {
    "command": ["cm-hook-command"],
    "http": ["cm-hook-url"],
    "mcp_tool": ["cm-hook-server", "cm-hook-tool"],
    "prompt": ["cm-hook-prompt"],
    "agent": ["cm-hook-prompt"],
}
_ROW0 = '[data-test="cm-hook-row-0"]'
# The data-test names of row 0's handler inputs that the browser renders.
_VISIBLE_HOOK_INPUTS = (
    "(() => [...document.querySelectorAll('[data-test=\"cm-hook-row-0\"] input')]"
    ".filter((el) => el.checkVisibility()).map((el) => el.dataset.test)"
    ".filter((t) => !['cm-hook-event', 'cm-hook-matcher', 'cm-hook-timeout'].includes(t))"
    ".sort())()"
)
# For each row-0 select: [name, width it needs for its widest option, width it has]. A
# clone with width:auto and no floor sizes itself to the widest option, with the same
# padding and arrow. The event box is a text input since TASK-524: _HOOK_EVENT_FITS checks
# that its text fits it.
_HOOK_SELECT_WIDTHS = (
    "(() => [...document.querySelectorAll('[data-test=\"cm-hook-row-0\"] select')].map((el) => {"
    " const c = el.cloneNode(true); c.style.width = 'auto'; c.style.minWidth = '0';"
    " c.style.position = 'absolute';"
    " el.parentElement.appendChild(c); const need = c.offsetWidth; c.remove();"
    " return [el.dataset.test, need, el.offsetWidth]; }))()"
)


# [scrollWidth, clientWidth] of row 0's event box: the text fits when the first is not larger.
_HOOK_EVENT_FITS = (
    '(() => { const el = document.querySelector(\'[data-test="cm-hook-row-0"]'
    ' [data-test="cm-hook-event"]\'); return [el.scrollWidth, el.clientWidth]; })()'
)


def _open_hooks_rows_with_one_row(browser: AgentBrowser, server: Server) -> None:
    _open_modal(browser, server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    browser.click('[data-test="cm-hooks-add"]')
    browser.expect_visible(f'{_ROW0} [data-test="cm-hook-command"]')


def test_config_mgmt_hooks_row_shows_only_its_type_inputs(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Each hook type shows only its own handler inputs (TASK-513).

    The mcp_tool inputs sat in a ``.d-flex`` element that carried the ``x-show``, and
    Tabler's ``display: flex !important`` beat the inline ``display: none``, so the
    server and tool boxes showed on every row whatever its type.
    """
    _open_hooks_rows_with_one_row(browser, config_mgmt_server)
    options = browser.eval_json(
        '[...document.querySelectorAll(\'[data-test="cm-hook-row-0"] [data-test="cm-hook-type"]'
        " option')].map((o) => o.value)"
    )
    assert sorted(options) == sorted(_HOOK_TYPE_INPUTS), options
    for hook_type, expected in _HOOK_TYPE_INPUTS.items():
        browser.select(f'{_ROW0} [data-test="cm-hook-type"]', hook_type)
        # x-show applies on the next frame, so wait for the set to settle.
        deadline = time.monotonic() + 5.0
        visible = browser.eval_json(_VISIBLE_HOOK_INPUTS)
        while visible != sorted(expected) and time.monotonic() < deadline:
            time.sleep(0.1)
            visible = browser.eval_json(_VISIBLE_HOOK_INPUTS)
        assert visible == sorted(expected), (hook_type, visible)


def test_config_mgmt_hooks_rows_fit_the_modal(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """The event box and type select fit their longest value, with no sideways scroll (TASK-513).

    The table had ``min-width: 42rem`` (672 px in a 670 px modal body), and the width:100%
    selects were squeezed to a few characters: "UserPromptExpansion" showed "UserProm".
    """
    _open_hooks_rows_with_one_row(browser, config_mgmt_server)
    browser.fill(f'{_ROW0} [data-test="cm-hook-event"]', "UserPromptExpansion")
    browser.select(f'{_ROW0} [data-test="cm-hook-type"]', "mcp_tool")
    browser.expect_visible(f'{_ROW0} [data-test="cm-hook-server"]')
    widths = browser.eval_json(_HOOK_SELECT_WIDTHS)
    assert [w[0] for w in widths] == ["cm-hook-type"], widths
    for name, need, have in widths:
        assert need <= have, (name, need, have)
    event = browser.eval_json(_HOOK_EVENT_FITS)
    assert event[0] <= event[1], event
    # The matcher input takes the width the selects leave; keep it a usable size.
    matcher = browser.eval_json(
        f"document.querySelector('{_ROW0} [data-test=\"cm-hook-matcher\"]').offsetWidth"
    )
    assert matcher >= 120, matcher
    scroll = browser.eval_json(
        '(() => { const v = document.querySelector(\'[data-test="cm-hooks-rows"]'
        " .table-responsive'); return [v.scrollWidth, v.clientWidth]; })()"
    )
    assert scroll[0] <= scroll[1], scroll
    # On a phone the wrapper scrolls sideways, but no control is squeezed: the selects keep
    # their longest option and the matcher keeps its floor. goto() set the viewport, so
    # resize after the page is open (the session closes after the test).
    browser.set_viewport(375, 1000)
    # Prove the phone layout is the one measured: the page is 375 px and the wrapper scrolls.
    phone = browser.eval_json(
        '(() => { const v = document.querySelector(\'[data-test="cm-hooks-rows"]'
        " .table-responsive'); return [window.innerWidth, v.scrollWidth, v.clientWidth]; })()"
    )
    assert phone[0] == 375 and phone[1] > phone[2], phone
    for name, need, have in browser.eval_json(_HOOK_SELECT_WIDTHS):
        assert need <= have, ("phone", name, need, have)
    event = browser.eval_json(_HOOK_EVENT_FITS)
    assert event[0] <= event[1], ("phone", event)
    matcher = browser.eval_json(
        f"document.querySelector('{_ROW0} [data-test=\"cm-hook-matcher\"]').offsetWidth"
    )
    assert matcher >= 120, ("phone", matcher)


def test_config_mgmt_hooks_http_headers_fall_back_to_raw_masked(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """An http hook with headers opens in Raw with the header value masked, never in a row."""
    _seed_alpha_settings(
        config_mgmt_server,
        {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "http",
                                "url": "https://h.example/x",
                                "headers": {"Authorization": "Bearer sk-e2e-secret"},
                            }
                        ]
                    }
                ]
            }
        },
    )
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    browser.expect_visible('[data-test="cm-hooks-rows-error"]')
    browser.expect_visible('[data-test="cm-hooks-text"]')
    text = browser.get_value('[data-test="cm-hooks-text"]')
    assert "https://h.example/x" in text
    assert "sk-e2e-secret" not in text


def test_config_mgmt_hooks_failed_load_clears_previous_rows(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A failed load never leaves the previous target's hooks editable or saveable."""
    _seed_alpha_settings(
        config_mgmt_server,
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo alpha"}]}]}},
    )
    user_settings = (
        Path(config_mgmt_server.state_dir).parent / "home" / ".claude" / "settings.json"
    )
    user_settings.parent.mkdir(parents=True, exist_ok=True)
    user_settings.write_text("{not json", encoding="utf-8")  # the User-scope read fails (422)

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_value('[data-test="cm-hook-command"]', "echo alpha")  # alpha loaded
    browser.click('[data-test="cm-scope-user"]')
    browser.expect_visible('[data-test="cm-error"]')
    browser.expect_count('[data-test="cm-hook-command"]', 0)  # alpha's hook is gone
    browser.fill('[data-test="cm-confirm"]', "USER SCOPE")
    browser.expect_disabled('[data-test="cm-save"]')  # nothing to save over


def test_config_mgmt_hooks_loaded_row_shows_its_type_and_event(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A loaded row's Type select and Event box show the stored values, not the first option."""
    _seed_alpha_settings(
        config_mgmt_server,
        {"hooks": {"Stop": [{"hooks": [{"type": "http", "url": "https://h.example/x"}]}]}},
    )
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-hook-url"]')
    browser.expect_value('[data-test="cm-hook-type"]', "http")
    browser.expect_value('[data-test="cm-hook-event"]', "Stop")
    browser.expect_value('[data-test="cm-hook-url"]', "https://h.example/x")


def test_config_mgmt_hooks_type_switch_never_drops_a_hidden_hook(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Switching a filled command row to http with no URL blocks Save, not a silent delete."""
    _seed_alpha_settings(
        config_mgmt_server,
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}]}},
    )
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-hook-command"]')
    browser.select('[data-test="cm-hook-type"]', "http")
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.expect_visible('[data-test="cm-hooks-timeout-warn"]')
    browser.expect_disabled('[data-test="cm-save"]')


def _seed_alpha_settings(config_mgmt_server: Server, settings: dict) -> None:
    """Write alpha's .claude/settings.json directly (bypassing the write API) for a test."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])
    path = projects_root / "alpha" / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings), encoding="utf-8")


def test_config_mgmt_hooks_duplicate_matcher_falls_back_to_raw(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Duplicate (event, matcher) groups can't round-trip as rows → open in Raw with a hint."""
    _seed_alpha_settings(
        config_mgmt_server,
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo a"}]},
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo b"}]},
                ]
            }
        },
    )
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    # Non-representable → raw fallback (verbatim) with the explanatory error, never a
    # silent regroup that would reorder firing.
    browser.expect_visible('[data-test="cm-hooks-rows-error"]')
    browser.expect_visible('[data-test="cm-hooks-text"]')


def test_config_mgmt_hooks_plugin_owned_command_falls_back_to_raw(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A hook command referencing CLAUDE_PLUGIN_ROOT stays in Raw (visible), never a row."""
    _seed_alpha_settings(
        config_mgmt_server,
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/run.sh"}]}
                ]
            }
        },
    )
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    browser.expect_visible('[data-test="cm-hooks-rows-error"]')
    browser.expect_visible('[data-test="cm-hooks-text"]')


@pytest.mark.parametrize(
    "timeout",
    [
        "1.5",  # not a whole number
        "0",  # the CLI reads timeout as a number above 0, so rows need ≥ 1
    ],
)
def test_config_mgmt_hooks_invalid_timeout_blocks_save(
    browser: AgentBrowser, config_mgmt_server: Server, timeout: str
) -> None:
    """A timeout the rows can't save warns and disables Save rather than being dropped."""
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    browser.click('[data-test="cm-hooks-add"]')
    browser.expect_visible('[data-test="cm-hook-command"]')
    browser.fill('[data-test="cm-hook-command"]', "echo x")
    browser.fill('[data-test="cm-hook-timeout"]', timeout)
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.expect_visible('[data-test="cm-hooks-timeout-warn"]')
    browser.expect_disabled('[data-test="cm-save"]')


def test_config_mgmt_hooks_prototype_matcher_round_trip(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A matcher named __proto__ (a backend-valid opaque string) groups correctly via rows."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    browser.click('[data-test="cm-hooks-add"]')
    browser.expect_visible('[data-test="cm-hook-matcher"]')
    browser.fill('[data-test="cm-hook-event"]', "PreToolUse")
    browser.fill('[data-test="cm-hook-matcher"]', "__proto__")
    browser.fill('[data-test="cm-hook-command"]', "echo proto")
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    on_disk = json.loads(
        (projects_root / "alpha" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert on_disk["hooks"] == {
        "PreToolUse": [
            {"matcher": "__proto__", "hooks": [{"type": "command", "command": "echo proto"}]}
        ]
    }


def test_config_mgmt_hooks_negative_timeout_falls_back_to_raw(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A hook with a negative timeout (backend-lax) opens in Raw, not an unsavable row."""
    _seed_alpha_settings(
        config_mgmt_server,
        {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "echo x", "timeout": -1}]}]
            }
        },
    )
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-hooks"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')
    browser.expect_visible('[data-test="cm-hooks-rows-error"]')
    browser.expect_visible('[data-test="cm-hooks-text"]')


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
    browser.expect_text('[data-test="cm-marketplaces-table"]', "github · owner/repo")

    # Actions are disabled until the scope token is typed, then a Disable posts.
    browser.expect_visible('[data-test="cm-plugin-confirm"]')
    browser.fill('[data-test="cm-plugin-confirm"]', "alpha")
    browser.click('[data-test="cm-plugin-disable-hello@market"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)


def _plugin_calls(server: Server, verb: str) -> list[list[str]]:
    """Return the logged ``claude plugin`` argv lists whose first word is ``verb``."""
    log = server.state_dir.parent / "plugin-argv.jsonl"
    if not log.exists():
        return []
    rows = [json.loads(line)["argv"] for line in log.read_text(encoding="utf-8").splitlines()]
    return [argv for argv in rows if argv and argv[0] == verb]


def _wait_for_plugin_calls(
    server: Server, verb: str, count: int, timeout_s: float = 20.0
) -> list[list[str]]:
    """Poll the fake CLI's call log until ``verb`` ran ``count`` times; return those calls."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        calls = _plugin_calls(server, verb)
        if len(calls) >= count:
            return calls
        time.sleep(0.1)
    raise AssertionError(
        f"expected {count} `plugin {verb}` calls, got {_plugin_calls(server, verb)}"
    )


def test_config_mgmt_plugins_one_confirm_disables_two_plugins(
    browser: AgentBrowser, config_mgmt_plugins_server: Server
) -> None:
    """One typed confirm covers two Disables; Uninstall needs it typed again (TASK-522).

    The confirm stays typed after Enable, Disable and Update. Install, Uninstall and
    marketplace Add/Remove stay disabled until it is typed again, an Uninstall clears it,
    and a scope switch clears a kept confirm.
    """
    server = config_mgmt_plugins_server
    hello, shared = "hello@market", "shared@market"
    confirm = '[data-test="cm-plugin-confirm"]'
    # An always-mounted live region (TASK-508): it changes text, it never hides.
    spent = '[data-test="cm-plugin-confirm-spent"]'
    again = "Type the confirm again"
    _open_modal(browser, server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-plugins"]')
    browser.expect_visible(f'[data-test="cm-plugin-disable-{hello}"]')
    browser.fill(confirm, "alpha")
    assert browser.get_text(spent) == ""
    browser.click(f'[data-test="cm-plugin-disable-{hello}"]')
    browser.expect_text(spent, again, timeout_ms=_SAVE_TIMEOUT)
    assert browser.get_value(confirm) == "alpha"
    # The second Disable needs no retype.
    browser.expect_enabled(f'[data-test="cm-plugin-disable-{shared}"]', timeout_ms=_SAVE_TIMEOUT)
    browser.click(f'[data-test="cm-plugin-disable-{shared}"]')
    calls = _wait_for_plugin_calls(server, "disable", 2)
    assert [argv[1] for argv in calls] == [hello, shared]
    browser.expect_enabled(f'[data-test="cm-plugin-update-{hello}"]', timeout_ms=_SAVE_TIMEOUT)
    browser.expect_enabled('[data-test="cm-market-update-market"]')
    # The kept confirm does not arm the actions that need a fresh one.
    browser.expect_disabled(f'[data-test="cm-plugin-uninstall-{hello}"]')
    browser.expect_disabled('[data-test="cm-market-remove-market"]')
    browser.fill('[data-test="cm-marketplace-add-source"]', "owner/other")
    browser.expect_disabled('[data-test="cm-marketplace-add-go"]')
    browser.fill('[data-test="cm-plugin-install-id"]', "x@market")
    browser.fill('[data-test="cm-plugin-install-confirm"]', "x@market")
    browser.expect_disabled('[data-test="cm-plugin-install-go"]')
    # Typed again: Uninstall arms, and after it runs the confirm is empty.
    browser.fill(confirm, "alpha")
    assert browser.get_text(spent) == ""
    browser.expect_enabled(f'[data-test="cm-plugin-uninstall-{hello}"]')
    browser.click(f'[data-test="cm-plugin-uninstall-{hello}"]')
    _wait_for_plugin_calls(server, "uninstall", 1)
    browser.expect_value(confirm, "", timeout_ms=_SAVE_TIMEOUT)
    browser.expect_disabled(f'[data-test="cm-plugin-disable-{shared}"]')
    # A kept confirm clears when the scope changes.
    browser.fill(confirm, "alpha")
    browser.click(f'[data-test="cm-plugin-disable-{shared}"]')
    _wait_for_plugin_calls(server, "disable", 3)
    browser.expect_text(spent, again, timeout_ms=_SAVE_TIMEOUT)
    browser.click('[data-test="cm-scope-local"]')
    browser.expect_attr('[data-test="cm-scope-local"]', "aria-pressed", "true")
    browser.expect_value(confirm, "")
    assert browser.get_text(spent) == ""


def test_config_mgmt_plugins_marketplace_lists_what_it_offers(
    browser: AgentBrowser, config_mgmt_plugins_server: Server
) -> None:
    """A marketplace shows its plugins and installs one with no id retype (TASK-516).

    Installed plugins are marked, the CLI's built-in directory stays out, marketplace
    markup shows as text, and a marketplace that offers nothing says so. The install sends
    the listed id as the server's ``confirm_plugin`` and needs a freshly typed confirm.
    """
    server = config_mgmt_plugins_server
    _open_modal(browser, server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-plugins"]')
    toggle = '[data-test="cm-market-offered-toggle-market"]'
    browser.expect_visible(toggle)
    browser.expect_attr(toggle, "aria-expanded", "false")
    assert not browser.is_visible('[data-test="cm-market-offered-market"]')
    assert _plugin_calls(server, "list") == [["list", "--json"]]  # nothing read before a click
    # The accessible name starts with the visible words (WCAG 2.5.3).
    # innerText puts a line break before the visually-hidden part.
    assert " ".join(browser.get_text(toggle).split()) == "Show plugins from market"
    browser.click(toggle)
    browser.expect_attr(toggle, "aria-expanded", "true")
    offered = '[data-test="cm-market-offered-market"]'
    browser.expect_visible('[data-test="cm-offered-newbie@market"]')
    # The row's always-mounted live region says what the list holds.
    browser.expect_text('[data-test="cm-market-offered-live-market"]', "market offers 4 plugins.")
    # Installed for alpha: marked, no Install. Installed only in beta: offered again.
    browser.expect_visible('[data-test="cm-offered-installed-hello@market"]')
    browser.expect_visible('[data-test="cm-offered-installed-shared@market"]')
    assert browser.get_count('[data-test="cm-offered-install-hello@market"]') == 0
    browser.expect_visible('[data-test="cm-offered-install-other@market"]')
    assert (
        browser.get_count('[data-test="cm-offered-directory-only@anthropic-plugin-directory"]')
        == 0
    )
    # Marketplace text is text, never markup.
    browser.expect_text(offered, "New <img src=x onerror=window.__xss516=1> plugin")
    browser.expect_text(offered, "version 0.1.0")
    assert browser.get_count(f"{offered} img") == 0
    assert browser.eval_json("window.__xss516 === undefined") is True
    # Read from alpha's directory, with the --available flag.
    [read] = [argv for argv in _plugin_calls(server, "list") if "--available" in argv]
    assert read == ["list", "--available", "--json"]
    # A marketplace with no plugins says so instead of an empty area.
    browser.click('[data-test="cm-market-offered-toggle-empty"]')
    browser.expect_text(
        '[data-test="cm-market-offered-empty-empty"]', "This marketplace offers no plugins."
    )
    available_reads = 2  # one per open list
    _wait_for_plugin_calls(server, "list", 1 + available_reads)
    # A Disable cannot change what a marketplace offers: no list is read again.
    browser.fill('[data-test="cm-plugin-confirm"]', "alpha")
    browser.click('[data-test="cm-plugin-disable-hello@market"]')
    _wait_for_plugin_calls(server, "disable", 1)
    browser.expect_text('[data-test="cm-plugin-confirm-spent"]', "Type", timeout_ms=_SAVE_TIMEOUT)
    assert len(_plugin_calls(server, "list")) == 2 + available_reads  # + the plugin list
    # Install from the list: the scope confirm typed again, no id retype.
    install = '[data-test="cm-offered-install-newbie@market"]'
    browser.expect_disabled(install)
    browser.fill('[data-test="cm-plugin-confirm"]', "alpha")
    browser.expect_enabled(install)
    browser.click(install)
    [call] = _wait_for_plugin_calls(server, "install", 1)
    assert call == ["install", "newbie@market", "--scope", "project"]
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    # An install clears the confirm, and both open lists are read again.
    browser.expect_value('[data-test="cm-plugin-confirm"]', "", timeout_ms=_SAVE_TIMEOUT)
    _wait_for_plugin_calls(server, "list", 3 + 2 * available_reads)
    browser.expect_visible('[data-test="cm-offered-newbie@market"]', timeout_ms=_SAVE_TIMEOUT)
    browser.expect_disabled(install)
    # Hide closes the list.
    browser.click(toggle)
    browser.expect_attr(toggle, "aria-expanded", "false")
    browser.expect_hidden(offered)


def test_config_mgmt_plugins_rows_follow_the_install_scope(
    browser: AgentBrowser, config_mgmt_plugins_server: Server
) -> None:
    """Each row shows its install scope and offers only the actions that work there.

    TASK-523: at Project scope a User-scope plugin keeps Enable/Disable (the CLI writes the
    project's override) but has no Update/Uninstall, which the CLI refuses there; it links to
    User scope instead. At User scope the Project-scope plugin is read-only with a link back.
    TASK-517: an Update says what happened, here that the plugin was already up to date.
    """
    shared, hello = "shared@market", "hello@market"
    _open_modal(browser, config_mgmt_plugins_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-plugins"]')
    browser.expect_text(f'[data-test="cm-plugin-scope-{shared}"]', "User")
    browser.expect_text(f'[data-test="cm-plugin-scope-{hello}"]', "Project")
    # The Project-scope install has every action; the User-scope one has no failing action.
    assert browser.get_count(f'[data-test="cm-plugin-update-{hello}"]') == 1
    assert browser.get_count(f'[data-test="cm-plugin-uninstall-{hello}"]') == 1
    assert browser.get_count(f'[data-test="cm-plugin-update-{shared}"]') == 0
    assert browser.get_count(f'[data-test="cm-plugin-uninstall-{shared}"]') == 0
    assert not browser.is_visible(f'[data-test="cm-plugin-readonly-{hello}"]')
    browser.expect_text(f'[data-test="cm-plugin-readonly-{shared}"]', "Installed at User scope")
    # Disable on the User-scope plugin is offered at Project scope, and it succeeds.
    browser.fill('[data-test="cm-plugin-confirm"]', "alpha")
    browser.click(f'[data-test="cm-plugin-disable-{shared}"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    # The link switches the editor to User scope, where Update and Uninstall appear.
    browser.click(f'[data-test="cm-plugin-switch-scope-{shared}"]')
    browser.expect_attr('[data-test="cm-scope-user"]', "aria-pressed", "true")
    browser.expect_visible(f'[data-test="cm-plugin-update-{shared}"]')
    # The reload hid the clicked link; focus moves to the scope confirm, not <body>.
    _expect_focused(browser, "cm-plugin-confirm")
    assert browser.get_count(f'[data-test="cm-plugin-update-{hello}"]') == 0
    assert browser.get_count(f'[data-test="cm-plugin-disable-{hello}"]') == 0
    # User scope has no selected project, so the link names the install's project.
    browser.expect_text(
        f'[data-test="cm-plugin-switch-scope-{hello}"]', "Open alpha at Project scope"
    )
    # Update at the plugin's own scope reports the result, not a bare "Saved.".
    browser.fill('[data-test="cm-plugin-confirm"]', "USER SCOPE")
    browser.click(f'[data-test="cm-plugin-update-{shared}"]')
    browser.expect_text(
        '[data-test="cm-saved"]',
        "shared@market is already up to date (version 2.0.0).",
        timeout_ms=_SAVE_TIMEOUT,
    )

    # And back: the Project-scope plugin's link returns to Project scope in alpha.
    browser.click(f'[data-test="cm-plugin-switch-scope-{hello}"]')
    browser.expect_attr('[data-test="cm-scope-project"]', "aria-pressed", "true")
    browser.expect_visible(f'[data-test="cm-plugin-update-{hello}"]')

    # At Local scope the Project-scope plugin keeps Disable (Local outranks Project) and
    # loses Update/Uninstall; the User-scope plugin keeps Disable too.
    browser.click('[data-test="cm-scope-local"]')
    browser.expect_attr('[data-test="cm-scope-local"]', "aria-pressed", "true")
    browser.expect_text(f'[data-test="cm-plugin-readonly-{hello}"]', "Local override")
    browser.expect_visible(f'[data-test="cm-plugin-disable-{hello}"]')
    browser.expect_visible(f'[data-test="cm-plugin-disable-{shared}"]')
    assert browser.get_count(f'[data-test="cm-plugin-update-{hello}"]') == 0
    assert browser.get_count(f'[data-test="cm-plugin-uninstall-{hello}"]') == 0

    # Another project's install is read-only here, and its link selects that project.
    other = "other@market"
    browser.expect_text(f'[data-test="cm-plugin-readonly-{other}"]', "in beta")
    assert browser.get_count(f'[data-test="cm-plugin-disable-{other}"]') == 0
    assert browser.get_count(f'[data-test="cm-plugin-enable-{other}"]') == 0
    browser.expect_text(
        f'[data-test="cm-plugin-switch-scope-{other}"]', "Open beta at Project scope"
    )
    browser.click(f'[data-test="cm-plugin-switch-scope-{other}"]')
    browser.expect_attr('[data-test="cm-scope-project"]', "aria-pressed", "true")
    browser.expect_value('[data-test="cm-project"]', "beta")
    browser.expect_visible(f'[data-test="cm-plugin-update-{other}"]')


def test_config_mgmt_plugins_symlink_alias_is_the_same_project(
    browser: AgentBrowser, config_mgmt_plugins_alias_server: Server
) -> None:
    """A project selected through a symlink alias keeps its own plugin actions.

    Greptile, PR 1838: ``alpha-link`` resolves to ``alpha``, where the CLI records the
    install. A name compare ("alpha" vs "alpha-link") hid Disable at Local scope and sent
    the link to "another" project. The server's ``inProject`` decides it instead.
    """
    hello = "hello@market"
    _open_modal(browser, config_mgmt_plugins_alias_server)
    browser.select('[data-test="cm-project"]', "alpha-link")
    browser.click('[data-test="cm-surface-plugins"]')
    browser.expect_visible(f'[data-test="cm-plugin-update-{hello}"]')  # atScope, by path
    browser.click('[data-test="cm-scope-local"]')
    browser.expect_attr('[data-test="cm-scope-local"]', "aria-pressed", "true")
    browser.expect_text(f'[data-test="cm-plugin-readonly-{hello}"]', "Local override")
    browser.expect_visible(f'[data-test="cm-plugin-disable-{hello}"]')
    browser.expect_text(f'[data-test="cm-plugin-switch-scope-{hello}"]', "Switch to Project scope")
    browser.click(f'[data-test="cm-plugin-switch-scope-{hello}"]')
    browser.expect_attr('[data-test="cm-scope-project"]', "aria-pressed", "true")
    browser.expect_value('[data-test="cm-project"]', "alpha-link")  # the chosen project stays


def test_config_mgmt_marketplaces_remove_only_where_the_scope_declares(
    browser: AgentBrowser, config_mgmt_plugins_server: Server
) -> None:
    """Remove shows only on a marketplace the current scope declares (TASK-527).

    ``marketplace list`` is one pool for the host, and ``marketplace remove --scope`` refuses
    a name that scope does not declare. The fixture declares ``market`` at alpha's Project
    scope and ``empty`` at User scope. Every other row is read-only and links to the scope
    of this project that declares it. Update stays on every row: it takes no scope.
    """
    server = config_mgmt_plugins_server
    _open_modal(browser, server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-plugins"]')
    browser.expect_visible('[data-test="cm-market-remove-market"]')
    assert browser.get_count('[data-test="cm-market-remove-empty"]') == 0
    assert not browser.is_visible('[data-test="cm-market-readonly-market"]')
    browser.expect_text(
        '[data-test="cm-market-readonly-empty"]',
        "Declared at User scope, not at Project scope. Remove it there.",
    )
    browser.expect_visible('[data-test="cm-market-update-empty"]')
    # No scope of alpha (nor User) declares `loose`: no Remove, no link.
    browser.expect_text(
        '[data-test="cm-market-readonly-loose"]',
        "Not declared at Project scope, so Remove is not offered here. No other scope of this "
        "project that Clauster reads declares it.",
    )
    assert browser.get_count('[data-test="cm-market-remove-loose"]') == 0
    assert not browser.is_visible('[data-test="cm-market-switch-scope-loose"]')

    # The link opens User scope, where only `empty` can be removed.
    browser.expect_text('[data-test="cm-market-switch-scope-empty"]', "Switch to User scope")
    browser.click('[data-test="cm-market-switch-scope-empty"]')
    browser.expect_attr('[data-test="cm-scope-user"]', "aria-pressed", "true")
    browser.expect_visible('[data-test="cm-market-remove-empty"]')
    _expect_focused(browser, "cm-plugin-confirm")
    assert browser.get_count('[data-test="cm-market-remove-market"]') == 0
    browser.expect_text(
        '[data-test="cm-market-readonly-market"]',
        "Not declared at User scope, so Remove is not offered here. A project can declare it "
        "at Project or Local scope.",
    )
    # User scope reads no project's files, so it has no link for a project's marketplace.
    assert not browser.is_visible('[data-test="cm-market-switch-scope-market"]')
    browser.fill('[data-test="cm-plugin-confirm"]', "USER SCOPE")
    browser.click('[data-test="cm-market-remove-empty"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    removes = [a for a in _plugin_calls(server, "marketplace") if a[1:2] == ["remove"]]
    assert removes == [["marketplace", "remove", "empty", "--scope", "user"]]

    # Local scope of alpha declares neither: both rows link to the scope that does.
    browser.click('[data-test="cm-scope-local"]')
    browser.expect_attr('[data-test="cm-scope-local"]', "aria-pressed", "true")
    browser.expect_text(
        '[data-test="cm-market-readonly-market"]',
        "Declared at Project scope, not at Local scope.",
    )
    assert browser.get_count('[data-test="cm-market-remove-market"]') == 0
    assert browser.get_count('[data-test="cm-market-remove-empty"]') == 0
    browser.click('[data-test="cm-market-switch-scope-market"]')
    browser.expect_attr('[data-test="cm-scope-project"]', "aria-pressed", "true")
    browser.expect_visible('[data-test="cm-market-remove-market"]')


def test_config_mgmt_marketplaces_unreadable_declarations_offer_no_remove(
    browser: AgentBrowser, config_mgmt_plugins_server: Server
) -> None:
    """A scope whose settings file cannot be read offers no Remove, and says why (TASK-527).

    alpha's ``settings.local.json`` is not valid JSON, so the declared read for Local scope
    fails. Local scope then offers no Remove at all. Project scope still reads its own file
    and keeps the Remove of the marketplace it declares.
    """
    server = config_mgmt_plugins_server
    cfg = yaml.safe_load((server.state_dir.parent / "clauster.yml").read_text(encoding="utf-8"))
    local = Path(cfg["projects_root"]) / "alpha" / ".claude" / "settings.local.json"
    local.write_bytes(b"{not json\n")
    _open_modal(browser, server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-plugins"]')
    browser.expect_visible('[data-test="cm-market-remove-market"]')
    # At Project scope a row no readable scope declares names the scope it could not read.
    browser.expect_text(
        '[data-test="cm-market-readonly-loose"]',
        "No other scope that Clauster could read declares it. Clauster could not read Local "
        "scope.",
    )
    browser.click('[data-test="cm-scope-local"]')
    browser.expect_attr('[data-test="cm-scope-local"]', "aria-pressed", "true")
    browser.expect_text(
        '[data-test="cm-market-readonly-market"]',
        "Clauster could not read which marketplaces Local scope declares",
    )
    browser.expect_text(
        '[data-test="cm-market-readonly-empty"]',
        "Clauster could not read which marketplaces Local scope declares",
    )
    assert browser.get_count('[data-test="cm-market-remove-market"]') == 0
    assert browser.get_count('[data-test="cm-market-remove-empty"]') == 0


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
    _mcp_raw_mode(browser)
    browser.fill('[data-test="cm-mcp-entry"]', '{"command": "echo", "env": {"K": "v"}}')
    browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
    browser.click('[data-test="cm-mcp-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    # The approvals panel now lists the committed server — approve it and save.
    browser.expect_visible('[data-test="cm-mcp-approvals"]')
    browser.expect_visible('[data-test="cm-mcp-approve-gizmo"]')
    # TASK-493: the save row is hidden while nothing is dirty. Its x-show once sat on a
    # .d-flex element, whose display:flex !important beat x-show's display:none.
    browser.expect_hidden('[data-test="cm-mcp-approvals-confirm"]')
    browser.expect_hidden('[data-test="cm-mcp-approvals-save"]')
    browser.click('[data-test="cm-mcp-approve-gizmo"]')
    # The confirm input + Save appear only once the toggle makes the panel dirty.
    browser.expect_visible('[data-test="cm-mcp-approvals-confirm"]')
    browser.fill('[data-test="cm-mcp-approvals-confirm"]', "alpha")
    browser.expect_visible('[data-test="cm-mcp-approvals-save"]:not([disabled])')
    browser.click('[data-test="cm-mcp-approvals-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    # A saved panel is clean again, so the row hides.
    browser.expect_hidden('[data-test="cm-mcp-approvals-save"]')

    alpha_key = str(projects_root / "alpha")
    stored = json.loads(claude_json.read_text(encoding="utf-8"))
    assert "gizmo" in stored["projects"][alpha_key]["enabledMcpjsonServers"]


_ROOT = "Alpine.$data(document.querySelector('[x-data^=\"dashboard\"]'))"
_GATE = (
    "(()=>{const d=" + _ROOT + ";d.resumeMode['alpha']='pty';return d._mcpGateBlocks('alpha')})()"
)


def _arm_mcp_approval(browser: AgentBrowser, server: Server) -> None:
    """Commit a server, approve it in the approvals panel, and type the save token."""
    _open_modal(browser, server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-mcp"]')
    browser.expect_visible('[data-test="cm-mcp-new"]')
    browser.click('[data-test="cm-mcp-new"]')
    browser.expect_visible('[data-test="cm-mcp-editor"]')
    browser.fill('[data-test="cm-mcp-name"]', "gizmo")
    _mcp_raw_mode(browser)
    browser.fill('[data-test="cm-mcp-entry"]', '{"command": "echo", "env": {"K": "v"}}')
    browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
    browser.click('[data-test="cm-mcp-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    browser.expect_visible('[data-test="cm-mcp-approvals"]')
    browser.expect_visible('[data-test="cm-mcp-approve-gizmo"]')
    browser.click('[data-test="cm-mcp-approve-gizmo"]')
    browser.fill('[data-test="cm-mcp-approvals-confirm"]', "alpha")
    browser.expect_visible('[data-test="cm-mcp-approvals-save"]:not([disabled])')


def _stub_alpha_preflight(browser: AgentBrowser, first: str) -> None:
    """Answer alpha's /preflight: the first call with ``first`` (JS), every later one 500."""
    browser.eval_js(
        "(function(){window.__pf=0;if(!window.__realFetch)window.__realFetch=window.fetch;"
        "window.fetch=(u,o)=>{if(!String(u).endsWith('/api/projects/alpha/preflight'))"
        "return window.__realFetch(u,o);window.__pf++;"
        "return window.__pf===1?" + first + ":Promise.resolve(new Response('{}',{status:500}))};"
        "return 1})()"
    )


def test_mcp_gate_stays_closed_when_the_reload_after_a_save_fails(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """TASK-489: a save whose readiness reload fails keeps the pty launch soft-blocked."""
    _arm_mcp_approval(browser, config_mgmt_server)
    _stub_alpha_preflight(browser, "Promise.resolve(new Response('{}',{status:500}))")
    browser.click('[data-test="cm-mcp-approvals-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    _wait_for_failed_reload(browser, min_calls=1)  # the save's fetch is the first call
    assert browser.eval_json(_GATE) is True


def _wait_for_failed_reload(browser: AgentBrowser, *, min_calls: int) -> None:
    """Wait until the save's own readiness reload has failed and been applied.

    ``min_calls`` is the stub's call count that includes this save's fetch, so an older
    preflightError cannot end the wait before the save has fetched.
    """
    deadline = time.monotonic() + 10
    cond = f"(()=>window.__pf>={min_calls}&&!!{_ROOT}.preflightError['alpha'])()"
    while not browser.eval_json(cond):
        assert time.monotonic() < deadline, "the save's readiness reload never failed"
        time.sleep(0.2)


def test_mcp_gate_ignores_a_pre_write_answer_that_lands_during_the_save(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """An older readiness answer that lands after the write cannot clear the save's block.

    The order is fixed, not timed: the older request's "all passed" (pre-write) answer is
    held until the save has set its block and entered its MCP list reload, then released.
    The save's own readiness reload then fails. The older call must report that its answer
    was dropped, and the gate must still block.
    """
    _arm_mcp_approval(browser, config_mgmt_server)
    ok = json.dumps(
        json.dumps({"ok": True, "checks": [{"name": "trust", "status": "ok", "detail": "x"}]})
    )
    _stub_alpha_preflight(
        browser,
        "new Promise(r=>{window.__releaseStale=()=>r(new Response(" + ok + ",{status:200,"
        "headers:{'Content-Type':'application/json'}}))})",
    )
    browser.eval_js(
        "(function(){const d=" + _ROOT + ";const orig=d._loadMcp.bind(d);"
        "d._loadMcp=async(g)=>{window.__mcpEntered=true;window.__releaseStale();"
        "while(window.__staleApplied===undefined)await new Promise(r=>setTimeout(r,20));"
        "return orig(g)};"
        "d.loadPreflight('alpha').then(v=>{window.__staleApplied=v});return 1})()"
    )
    browser.click('[data-test="cm-mcp-approvals-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    _wait_for_failed_reload(browser, min_calls=2)  # call 1 is the held older request
    state = browser.eval_json(
        "(()=>({entered:window.__mcpEntered===true,stale:window.__staleApplied??null,"
        "calls:window.__pf}))()"
    )
    # The wrapper ran, the held answer was dropped (true would mean it landed), and the
    # save's own reload was a second call.
    assert state["entered"] is True and state["stale"] is False and state["calls"] >= 2, state
    assert browser.eval_json(_GATE) is True


def test_config_mgmt_mcp_settings_owned_approval_is_read_only(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A settings-file-owned approval renders read-only, not as a revertible toggle (#961).

    ``claude mcp add-json --scope local`` relocates an approval into
    ``settings.local.json``; the ~/.claude.json write path can't change it, so the
    panel must show that server's row read-only (a ``settings`` badge + disabled
    approve/reject/unset) rather than offer an action that silently reverts on reload.
    """
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-mcp"]')
    browser.expect_visible('[data-test="cm-mcp-new"]')
    # Commit a server so it appears in the approvals panel.
    browser.click('[data-test="cm-mcp-new"]')
    browser.expect_visible('[data-test="cm-mcp-editor"]')
    browser.fill('[data-test="cm-mcp-name"]', "gizmo")
    _mcp_raw_mode(browser)
    browser.fill('[data-test="cm-mcp-entry"]', '{"command": "echo", "env": {"K": "v"}}')
    browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
    browser.click('[data-test="cm-mcp-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    # Approve gizmo via settings.local.json (what `claude mcp add-json --scope local` does),
    # then re-enter the surface so the approvals reload folds the settings decision in.
    settings_local = projects_root / "alpha" / ".claude" / "settings.local.json"
    settings_local.parent.mkdir(parents=True, exist_ok=True)
    settings_local.write_text(json.dumps({"enabledMcpjsonServers": ["gizmo"]}), encoding="utf-8")
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-view-permissions"]')
    browser.click('[data-test="cm-surface-mcp"]')
    browser.expect_visible('[data-test="cm-mcp-approvals"]')

    # The row is now settings-owned: badge shown, and every approval control disabled.
    browser.expect_visible('[data-test="cm-mcp-locked-gizmo"]')
    browser.expect_disabled('[data-test="cm-mcp-approve-gizmo"]')
    browser.expect_disabled('[data-test="cm-mcp-reject-gizmo"]')
    browser.expect_disabled('[data-test="cm-mcp-unset-gizmo"]')


def test_config_mgmt_mcp_approvals_confirm_clears_on_surface_switch(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """The MCP-approvals confirm surface stays blank after a surface round-trip (#960).

    Smoke coverage for the nested-token path Greptile flagged: the approvals confirm
    is not pre-satisfied after navigating away and back. NOTE this asserts the
    *steady* end state only — the reload's `_loadMcpApprovals` also zeros the token
    synchronously on return, so it cannot discriminate the in-window race the handler
    fix closes (there is no JS unit harness to test the handler in isolation). It
    guards the observable invariant and documents the nested path, not the race.
    """
    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-mcp"]')
    browser.expect_visible('[data-test="cm-mcp-new"]')
    # Commit a server so the per-server approvals panel renders.
    browser.click('[data-test="cm-mcp-new"]')
    browser.expect_visible('[data-test="cm-mcp-editor"]')
    browser.fill('[data-test="cm-mcp-name"]', "gizmo")
    _mcp_raw_mode(browser)
    browser.fill('[data-test="cm-mcp-entry"]', '{"command": "echo", "env": {"K": "v"}}')
    browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
    browser.click('[data-test="cm-mcp-save"]')
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)

    # Arm the approvals save: approve makes the panel dirty, then type the token.
    browser.expect_visible('[data-test="cm-mcp-approvals"]')
    browser.click('[data-test="cm-mcp-approve-gizmo"]')
    browser.expect_visible('[data-test="cm-mcp-approvals-confirm"]')
    browser.fill('[data-test="cm-mcp-approvals-confirm"]', "alpha")
    browser.expect_value('[data-test="cm-mcp-approvals-confirm"]', "alpha")

    # Navigate away and back WITHOUT saving; the nested token must not linger.
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-view-permissions"]')
    browser.click('[data-test="cm-surface-mcp"]')
    browser.expect_visible('[data-test="cm-mcp-approvals"]')
    # Re-approve to reveal the confirm input again: it must be blank, not pre-satisfied.
    browser.click('[data-test="cm-mcp-approve-gizmo"]')
    browser.expect_visible('[data-test="cm-mcp-approvals-confirm"]')
    browser.expect_value('[data-test="cm-mcp-approvals-confirm"]', "")


def test_delete_confirm_scrolls_into_view_and_focuses(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Arming a delete brings its confirm box into view and focuses the input (#1031).

    The confirm renders at the panel foot while the row's Delete button can sit
    anywhere above it — without the reveal, the panel doesn't move and Delete
    looks like a no-op. The focus assertion is the discriminating check: it can
    only pass when configMgmtAskDeleteAgent actually ran revealFeedback.
    """
    _open_modal(browser, config_mgmt_server)
    browser.click('[data-test="cm-surface-subagents"]')
    browser.expect_visible('[data-test="cm-agent-new"]')
    browser.click('[data-test="cm-agent-new"]')
    browser.expect_visible('[data-test="cm-agent-editor"]')
    browser.fill('[data-test="cm-agent-name"]', "del-target")
    browser.fill(
        '[data-test="cm-agent-content"]',
        "---\ndescription: temp\n---\n\nBody.",
    )
    browser.fill('[data-test="cm-agent-confirm"]', "alpha")
    browser.click('[data-test="cm-agent-save"]')
    browser.expect_visible('[data-test="cm-agent-del-del-target"]')

    browser.click('[data-test="cm-agent-del-del-target"]')
    browser.expect_visible('[data-test="cm-agent-delete-confirm"]')
    # Discriminating assertion: the confirm INPUT holds focus only via the reveal.
    import time

    deadline = time.monotonic() + 5
    focused = False
    while time.monotonic() < deadline and not focused:
        focused = browser.eval_json(
            "(() => document.activeElement ==="
            " document.querySelector('[data-test=\"cm-agent-delete-input\"]'))()"
        )
        if not focused:
            time.sleep(0.2)
    assert focused, "delete-confirm input should receive focus when the confirm is armed"


# --- TASK-508: every config-modal write brings its result banner into view ------------
# The shared cm-error / cm-saved banners render at the top of the modal body and the
# write controls sit below them. Each test parks the body at its foot (where an operator
# who just reached Save or a panel-foot button is), writes, and asserts the banner landed
# inside the body's scrollport. On the old code the banner stayed above the fold.

_CM_BODY = '[data-test="configmgmt-modal"] .modal-body'


def _short_window(browser: AgentBrowser) -> None:
    """A window short enough that every surface's modal body scrolls."""
    browser.set_viewport(1280, 560)  # the session closes after the test, so no restore


def _park_modal_body_at_foot(browser: AgentBrowser) -> None:
    top = browser.eval_json(
        f"(() => {{ const b = document.querySelector({json.dumps(_CM_BODY)});"
        " b.scrollTop = b.scrollHeight; return b.scrollTop; })()"
    )
    assert top > 0, "the modal body does not scroll, so the test would prove nothing"


def _expect_in_modal_view(browser: AgentBrowser, selector: str) -> None:
    """Poll until ``selector`` is visible and wholly inside the modal body's scrollport."""
    browser.expect_visible(selector, timeout_ms=_SAVE_TIMEOUT)
    script = (
        f"(() => {{ const b = document.querySelector({json.dumps(_CM_BODY)})"
        ".getBoundingClientRect();"
        f" const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();"
        " return r.top >= b.top && r.bottom <= b.bottom; })()"
    )
    deadline = time.monotonic() + 5  # the reveal scrolls smoothly, so give it a beat
    while not browser.eval_json(script):
        assert time.monotonic() < deadline, f"{selector} stayed outside the visible modal body"
        time.sleep(0.25)


def _expect_error_announced(browser: AgentBrowser, text: str) -> None:
    """The persistent role=alert region carries the refusal for assistive tech."""
    browser.expect_text('[data-test="cm-error-live"]', text)
    assert browser.get_attr('[data-test="cm-error-live"]', "role") == "alert"


@pytest.mark.parametrize(
    ("scope", "filename", "confirm"),
    [("project", "settings.json", "alpha"), ("local", "settings.local.json", "alpha (local)")],
)
def test_config_mgmt_stored_auto_mode_is_read_only_and_removable(
    browser: AgentBrowser, config_mgmt_server: Server, scope: str, filename: str, confirm: str
) -> None:
    """TASK-509: a stored block shows read-only and masked; Remove + Save drops only it."""
    path = _projects_root(config_mgmt_server) / "alpha" / ".claude" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    seeded = {
        "model": "opus",
        "permissions": {"deny": [], "allow": ["Bash(ls:*)"]},
        "autoMode": {"allow": ["$defaults", "Mirror: https://ci:hunter2@git.example.com/acme"]},
    }
    path.write_bytes(json.dumps(seeded).encode("utf-8"))

    _open_permissions_at(browser, config_mgmt_server, scope)
    _short_window(browser)
    browser.expect_visible('[data-test="cm-automode-readonly"]')
    shown = browser.get_text('[data-test="cm-automode-readonly"]')
    assert "********" in shown and "$defaults" in shown
    assert "hunter2" not in shown  # the read-only view is the masked read
    assert "Ignored by Claude Code" in browser.get_text('[data-test="cm-automode-ignored"]')
    browser.expect_hidden('[data-test="cm-automode-text"]')
    browser.expect_hidden('[data-test="cm-automode-none"]')
    browser.expect_visible('[data-test="cm-save"][disabled]')  # nothing to save yet

    # Keep undoes a Remove, and the save stays off.
    browser.click('[data-test="cm-automode-remove"]')
    browser.expect_visible('[data-test="cm-automode-removing"]')
    # The pending removal is announced, and focus moves to Keep (Remove just hid).
    browser.expect_text('[data-test="cm-automode-live"]', "marked for removal")
    assert browser.eval_json(
        "document.activeElement === document.querySelector('[data-test=\"cm-automode-keep\"]')"
    )
    browser.click('[data-test="cm-automode-keep"]')
    browser.expect_visible('[data-test="cm-automode-remove"]')
    assert browser.get_text('[data-test="cm-automode-live"]') == ""
    browser.fill('[data-test="cm-confirm"]', confirm)
    browser.expect_visible('[data-test="cm-save"][disabled]')

    browser.click('[data-test="cm-automode-remove"]')
    browser.expect_visible('[data-test="cm-automode-removing"]')
    browser.expect_visible('[data-test="cm-save"]:not([disabled])')
    _park_modal_body_at_foot(browser)
    browser.click('[data-test="cm-save"]')
    _expect_in_modal_view(browser, '[data-test="cm-saved"]')  # the result is in view
    browser.expect_visible('[data-test="cm-automode-none"]')  # reloaded: no block now

    stored = json.loads(path.read_bytes())
    assert stored == {"model": "opus", "permissions": {"deny": [], "allow": ["Bash(ls:*)"]}}


def test_config_mgmt_refused_settings_save_reveals_error(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A 422 refusal on the Settings tab lands in view too."""
    _open_settings_rows(browser, config_mgmt_server)
    _short_window(browser)
    _add_settings_key(browser, "skipAutoPermissionPrompt")
    browser.click(_row("skipAutoPermissionPrompt", "cm-settings-bool"))
    browser.fill('[data-test="cm-confirm"]', "alpha")
    _park_modal_body_at_foot(browser)
    browser.click('[data-test="cm-save"]')
    _expect_in_modal_view(browser, '[data-test="cm-error"]')
    _expect_error_announced(browser, "only from user settings")


def test_config_mgmt_settings_save_reveals_saved(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A successful save brings "Saved." into view and announces it politely."""
    _open_settings_rows(browser, config_mgmt_server)
    _short_window(browser)
    _add_settings_key(browser, "verbose")
    browser.click(_row("verbose", "cm-settings-bool"))
    browser.fill('[data-test="cm-confirm"]', "alpha")
    _park_modal_body_at_foot(browser)
    browser.click('[data-test="cm-save"]')
    _expect_in_modal_view(browser, '[data-test="cm-saved"]')
    browser.expect_text('[data-test="cm-saved-live"]', "Saved.")
    assert browser.get_attr('[data-test="cm-saved-live"]', "role") == "status"
    # The error region is mounted (get_text also returns "" for a missing element) and empty.
    assert browser.get_count('[data-test="cm-error-live"]') == 1
    assert browser.get_text('[data-test="cm-error-live"]') == ""


# Records the `behavior` of every scrollIntoView call, then scrolls as asked.
_RECORD_SCROLL_BEHAVIOR = """(() => {
  window.__scrolls508 = [];
  const real = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = function (opts) {
    window.__scrolls508.push(opts && opts.behavior);
    return real.apply(this, arguments);
  };
  return true;
})()"""


@pytest.mark.parametrize("reduce", [False, True], ids=["motion", "reduced-motion"])
def test_config_mgmt_save_reveal_honors_reduced_motion(
    browser: AgentBrowser, config_mgmt_server: Server, reduce: bool
) -> None:
    """The reveal scrolls smoothly, or at once when the OS asks for reduced motion."""
    _open_settings_rows(browser, config_mgmt_server)
    _short_window(browser)
    if reduce:
        browser.set_reduced_motion()
    _add_settings_key(browser, "verbose")
    browser.click(_row("verbose", "cm-settings-bool"))
    browser.fill('[data-test="cm-confirm"]', "alpha")
    assert browser.eval_json(_RECORD_SCROLL_BEHAVIOR) is True
    _park_modal_body_at_foot(browser)
    browser.click('[data-test="cm-save"]')
    _expect_in_modal_view(browser, '[data-test="cm-saved"]')
    assert browser.eval_json("window.__scrolls508") == ["auto" if reduce else "smooth"]


def test_config_mgmt_client_side_refusal_reveals_error(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A save the page refuses itself (autoMode is not JSON) shows its reason in view."""
    _open_modal(browser, config_mgmt_server)
    _short_window(browser)
    _switch_to_user_scope(browser)
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-automode-text"]')
    browser.fill('[data-test="cm-automode-text"]', "{not json")
    browser.fill('[data-test="cm-confirm"]', "USER SCOPE")
    _park_modal_body_at_foot(browser)
    browser.click('[data-test="cm-save"]')
    _expect_in_modal_view(browser, '[data-test="cm-error"]')
    _expect_error_announced(browser, "autoMode must be valid JSON.")


def test_config_mgmt_stale_save_reveals_error(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A 409 (the file changed on disk after the load) lands in view."""
    _seed_alpha_settings(config_mgmt_server, {"model": "opus"})
    _open_settings_rows(browser, config_mgmt_server)
    _short_window(browser)
    _add_settings_key(browser, "verbose")
    browser.click(_row("verbose", "cm-settings-bool"))
    _alpha_settings_path(config_mgmt_server).write_text(
        json.dumps({"model": "sonnet"}), encoding="utf-8"
    )
    browser.fill('[data-test="cm-confirm"]', "alpha")
    _park_modal_body_at_foot(browser)
    browser.click('[data-test="cm-save"]')
    _expect_in_modal_view(browser, '[data-test="cm-error"]')
    _expect_error_announced(browser, "Changed on disk")


# Makes every config-write PUT reject the way fetch does when the network is down.
_FAIL_CONFIG_WRITE_PUT = """(() => {
  const real = window.fetch;
  window.fetch = function (url, opts) {
    if (opts && opts.method === "PUT" && String(url).includes("/api/config-write/")) {
      return Promise.reject(new TypeError("Failed to fetch"));
    }
    return real.apply(this, arguments);
  };
  return true;
})()"""


def test_config_mgmt_network_error_reveals_error(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A save whose request never reaches the server says so, in view."""
    _open_settings_rows(browser, config_mgmt_server)
    _short_window(browser)
    _add_settings_key(browser, "verbose")
    browser.click(_row("verbose", "cm-settings-bool"))
    browser.fill('[data-test="cm-confirm"]', "alpha")
    assert browser.eval_json(_FAIL_CONFIG_WRITE_PUT) is True
    _park_modal_body_at_foot(browser)
    browser.click('[data-test="cm-save"]')
    _expect_in_modal_view(browser, '[data-test="cm-error"]')
    _expect_error_announced(browser, "Save failed.")
    browser.expect_text('[data-test="cm-save"]', "Save")  # not stuck at "Saving…"


# Holds every config-write PUT, then rejects it like a dropped network on `__fail508()`.
_HOLD_THEN_FAIL_PUT = """(() => {
  window.__hold508f = new Promise(r => { window.__fail508 = r; });
  const real = window.fetch;
  window.fetch = function (url, opts) {
    if (opts && opts.method === "PUT" && String(url).includes("/api/config-write/")) {
      return window.__hold508f.then(() => Promise.reject(new TypeError("Failed to fetch")));
    }
    return real.apply(this, arguments);
  };
  return true;
})()"""


def test_config_mgmt_failed_write_after_scope_switch_stays_silent(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """A write that fails after the operator switched scope does not blame the new scope."""
    _open_settings_rows(browser, config_mgmt_server)
    _add_settings_key(browser, "verbose")
    browser.click(_row("verbose", "cm-settings-bool"))
    browser.fill('[data-test="cm-confirm"]', "alpha")
    assert browser.eval_json(_HOLD_THEN_FAIL_PUT) is True
    browser.click('[data-test="cm-save"]')
    browser.expect_text('[data-test="cm-save"]', "Saving")  # the PUT is in flight
    _switch_to_user_scope(browser)
    browser.expect_hidden(_row("verbose"))  # the User scope's rows replaced alpha's edit
    browser.eval_json("(() => { window.__fail508(); return true; })()")
    browser.expect_text('[data-test="cm-save"]', "Save")  # the failed write finished
    time.sleep(0.5)  # past the frames in which a stale error would land
    assert not browser.is_visible('[data-test="cm-error"]')
    assert browser.get_count('[data-test="cm-error-live"]') == 1
    assert browser.get_text('[data-test="cm-error-live"]') == ""


def test_config_mgmt_plugins_action_reveals_saved(
    browser: AgentBrowser, config_mgmt_plugins_server: Server
) -> None:
    """Adding a marketplace from the panel foot brings "Saved." into view."""
    _open_modal(browser, config_mgmt_plugins_server)
    _short_window(browser)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-plugins"]')
    browser.expect_text('[data-test="cm-marketplaces-table"]', "market")
    browser.fill('[data-test="cm-plugin-confirm"]', "alpha")
    _park_modal_body_at_foot(browser)
    browser.fill('[data-test="cm-marketplace-add-source"]', "owner/other")
    browser.click('[data-test="cm-marketplace-add-go"]')
    _expect_in_modal_view(browser, '[data-test="cm-saved"]')


# Holds every settings PUT until `__release508()` runs.
_HOLD_SETTINGS_PUT = """(() => {
  window.__hold508 = new Promise(r => { window.__release508 = r; });
  const real = window.fetch;
  window.fetch = function (url, opts) {
    const p = real.apply(this, arguments);
    const isPut = opts && opts.method === "PUT" && String(url).includes("/config-write/settings");
    return isPut ? window.__hold508.then(() => p) : p;
  };
  return true;
})()"""


def test_config_mgmt_save_reveal_leaves_a_control_the_operator_moved_to(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """Focus moved to another control during the save keeps its place and its view."""
    settings = _alpha_settings_path(config_mgmt_server)
    _open_settings_rows(browser, config_mgmt_server)
    _short_window(browser)
    _add_settings_key(browser, "verbose")
    browser.click(_row("verbose", "cm-settings-bool"))
    browser.fill('[data-test="cm-confirm"]', "alpha")
    assert browser.eval_json(_HOLD_SETTINGS_PUT) is True
    _park_modal_body_at_foot(browser)
    browser.click('[data-test="cm-save"]')
    deadline = time.monotonic() + 10
    while not settings.exists():
        assert time.monotonic() < deadline, "the held save never reached the server"
        time.sleep(0.1)
    # Meanwhile the operator moves on to the Add-a-key box at the foot of the panel.
    browser.eval_json(
        "(() => { document.getElementById('cm-settings-add-key')"
        ".focus({ preventScroll: true }); return true; })()"
    )
    scroll_before = browser.eval_json(_MODAL_SCROLL_TOP)
    browser.eval_json("(() => { window.__release508(); return true; })()")
    browser.expect_visible('[data-test="cm-saved"]', timeout_ms=_SAVE_TIMEOUT)
    time.sleep(0.5)  # past the frames in which a wrong scroll would happen
    _expect_focused(browser, "cm-settings-add-key")
    # Never scrolled back up to the banner (a reload can only nudge scrollTop down).
    assert browser.eval_json(_MODAL_SCROLL_TOP) >= scroll_before
    browser.expect_text('[data-test="cm-saved-live"]', "Saved.")  # still announced


def _list_surface_editor(browser: AgentBrowser, server: Server, surface: str) -> None:
    """Open alpha's ``surface`` (subagents, skills or mcp) and start a new entry."""
    key = {"subagents": "agent", "skills": "skill", "mcp": "mcp"}[surface]
    _open_modal(browser, server)
    _short_window(browser)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click(f'[data-test="cm-surface-{surface}"]')
    browser.expect_visible(f'[data-test="cm-{key}-new"]')  # the list finished loading
    browser.click(f'[data-test="cm-{key}-new"]')


@pytest.mark.parametrize("surface", ["subagents", "skills"])
def test_config_mgmt_list_editor_refusal_reveals_error(
    browser: AgentBrowser, config_mgmt_server: Server, surface: str
) -> None:
    """A refused subagent or skill save, from the editor's foot, lands in view."""
    key = {"subagents": "agent", "skills": "skill"}[surface]
    _list_surface_editor(browser, config_mgmt_server, surface)
    browser.expect_visible(f'[data-test="cm-{key}-editor"]')
    browser.fill(f'[data-test="cm-{key}-name"]', "Not A Valid Name")
    browser.fill(f'[data-test="cm-{key}-content"]', "---\ndescription: e2e\n---\nGo.\n")
    browser.fill(f'[data-test="cm-{key}-confirm"]', "alpha")
    _park_modal_body_at_foot(browser)
    browser.click(f'[data-test="cm-{key}-save"]')
    _expect_in_modal_view(browser, '[data-test="cm-error"]')
    assert browser.get_text('[data-test="cm-error-live"]') != ""


# Records every text the error live region takes, so a test can see it change.
_RECORD_ERROR_REGION = """(() => {
  window.__errTexts508 = [];
  const el = document.querySelector('[data-test="cm-error-live"]');
  new MutationObserver(() => window.__errTexts508.push(el.textContent))
    .observe(el, { childList: true, characterData: true, subtree: true });
  return true;
})()"""


def test_config_mgmt_mcp_refusal_twice_is_revealed_and_announced_twice(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """The same client-side refusal twice in a row changes the live region both times."""
    _list_surface_editor(browser, config_mgmt_server, "mcp")
    browser.expect_visible('[data-test="cm-mcp-fields"]')
    browser.fill('[data-test="cm-mcp-name"]', "gizmo")
    _mcp_raw_mode(browser)
    browser.fill('[data-test="cm-mcp-entry"]', "{not json")
    browser.fill('[data-test="cm-mcp-confirm"]', "alpha")
    assert browser.eval_json(_RECORD_ERROR_REGION) is True
    for _ in range(2):
        _park_modal_body_at_foot(browser)
        browser.click('[data-test="cm-mcp-save"]')
        _expect_in_modal_view(browser, '[data-test="cm-error"]')
    deadline = time.monotonic() + 5
    want = "Server entry must be valid JSON."
    while browser.eval_json("window.__errTexts508").count(want) < 2:
        assert time.monotonic() < deadline, browser.eval_json("window.__errTexts508")
        time.sleep(0.1)


def test_config_mgmt_json_surface_refusal_twice_is_announced_twice(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """The shared Save path already clears the region before a repeated refusal; pin it."""
    _open_modal(browser, config_mgmt_server)
    _short_window(browser)
    _switch_to_user_scope(browser)
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-automode-text"]')
    browser.fill('[data-test="cm-automode-text"]', "{not json")
    browser.fill('[data-test="cm-confirm"]', "USER SCOPE")
    assert browser.eval_json(_RECORD_ERROR_REGION) is True
    for _ in range(2):
        browser.click('[data-test="cm-save"]')
        browser.expect_visible('[data-test="cm-error"]')
        time.sleep(0.3)
    want = "autoMode must be valid JSON."
    deadline = time.monotonic() + 5
    while browser.eval_json("window.__errTexts508").count(want) < 2:
        assert time.monotonic() < deadline, browser.eval_json("window.__errTexts508")
        time.sleep(0.1)


def test_config_mgmt_claude_md_save_reveals_saved(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """The CLAUDE.md save, through the same footer Save, brings "Saved." into view."""
    _open_modal(browser, config_mgmt_server)
    _short_window(browser)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.fill('[data-test="cm-claudemd-text"]', "# managed\n" + "line\n" * 60)
    browser.fill('[data-test="cm-confirm"]', "alpha")
    _park_modal_body_at_foot(browser)
    browser.click('[data-test="cm-save"]')
    _expect_in_modal_view(browser, '[data-test="cm-saved"]')
