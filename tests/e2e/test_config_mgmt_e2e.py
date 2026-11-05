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


def test_config_mgmt_auto_mode_project_scope_explains_refusal(
    browser: AgentBrowser, config_mgmt_server: Server
) -> None:
    """At project scope the tab warns first, and a save is refused with a User-scope hint."""
    cfg_path = Path(config_mgmt_server.state_dir).parent / "clauster.yml"
    projects_root = Path(yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["projects_root"])

    _open_modal(browser, config_mgmt_server)
    browser.select('[data-test="cm-project"]', "alpha")
    browser.click('[data-test="cm-surface-permissions"]')
    browser.expect_visible('[data-test="cm-automode-scope-warning"]')
    browser.fill('[data-test="cm-automode-text"]', json.dumps(_MAINTAINER_AUTO_MODE))
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.click('[data-test="cm-save"]')
    browser.expect_visible('[data-test="cm-error"]', timeout_ms=_SAVE_TIMEOUT)
    assert "User scope" in browser.get_text('[data-test="cm-error"]')
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


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


def _add_settings_key(browser: AgentBrowser, key: str) -> None:
    browser.fill('[data-test="cm-settings-add-key"]', key)
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
    browser.expect_value(_row("tui", "cm-settings-enum"), "default")
    browser.expect_value(_row("language", "cm-settings-string"), "1")
    browser.expect_visible(_row("extraKnownMarketplaces", "cm-settings-readonly"))
    # The helper is masked in the page; the real command never reaches the browser.
    browser.expect_value(_row("apiKeyHelper", "cm-settings-string"), "********")
    assert "s3cr3t" not in browser.eval_js("document.documentElement.outerHTML")
    browser.expect_visible(_row("env", "cm-settings-env-summary"))

    browser.click(_row("verbose", "cm-settings-bool"))
    browser.fill(_row("cleanupPeriodDays", "cm-settings-number"), "45")
    browser.select(_row("tui", "cm-settings-enum"), "fullscreen")
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
    browser.click(_row("model", "cm-settings-key-remove"))
    browser.expect_count('[data-test="cm-settings-key-row"]', 0)

    _add_settings_key(browser, "verbose")
    browser.expect_visible(_row("verbose", "cm-settings-bool"))
    assert _checked(browser, _row("verbose", "cm-settings-bool")) is False
    browser.click(_row("verbose", "cm-settings-bool"))
    _add_settings_key(browser, "someFutureKey")
    browser.fill(_row("someFutureKey", "cm-settings-string"), "abc")
    _add_settings_key(browser, "theme")
    browser.expect_value(_row("theme", "cm-settings-enum"), "auto")
    browser.select(_row("theme", "cm-settings-enum"), "dark")
    _add_settings_key(browser, "cleanupPeriodDays")
    # An empty number box blocks the save until it holds a number.
    browser.fill('[data-test="cm-confirm"]', "alpha")
    browser.expect_visible('[data-test="cm-settings-number-error"]')
    browser.expect_disabled('[data-test="cm-save"]')
    browser.fill(_row("cleanupPeriodDays", "cm-settings-number"), "7")
    browser.expect_hidden('[data-test="cm-settings-number-error"]')
    _save_alpha(browser)
    stored = json.loads(_alpha_settings_path(config_mgmt_server).read_text(encoding="utf-8"))
    assert stored == {
        "verbose": True,
        "someFutureKey": "abc",
        "theme": "dark",
        "cleanupPeriodDays": 7,
    }

    # A key that another tab edits is not added; the message links to that tab.
    _add_settings_key(browser, "hooks")
    browser.expect_text('[data-test="cm-settings-add-error"]', "hooks is edited on the Hooks tab")
    browser.expect_count(_row("hooks"), 0)
    browser.click('[data-test="cm-settings-add-owner-link"]')
    browser.expect_visible('[data-test="cm-view-hooks"]')


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

    A stored value that Claude Code does not read shows as its own "Not valid" choice.
    """
    alpha = _projects_root(config_mgmt_server) / "alpha"
    _seed_skill(alpha, "a-rather-long-skill-folder-name-that-must-wrap")
    (alpha / ".claude" / "settings.json").write_text(
        json.dumps({"skillOverrides": {"orphan": "off", "odd": "maybe"}}), encoding="utf-8"
    )
    _open_skill_visibility(browser, config_mgmt_server)
    browser.expect_value('[data-test="cm-skill-ov-odd"]', "?")
    browser.expect_text('[data-test="cm-skill-ov-odd"]', 'Not valid: "maybe"')
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

    # The "Not valid" value is sent unchanged, so the backend refuses the save by its key.
    _save_skill_visibility(browser, "alpha")
    browser.expect_text('[data-test="cm-error"]', "skillOverrides['odd'] must be one of")


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
    browser.select('[data-test="cm-hook-event"]', "SessionStart")
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
    browser.select('[data-test="cm-hook-event"]', "PostToolUse")
    browser.select('[data-test="cm-hook-type"]', "http")
    browser.expect_visible('[data-test="cm-hook-url"]')
    browser.fill('[data-test="cm-hook-url"]', "https://hooks.example.com/in")
    browser.click('[data-test="cm-hooks-add"]')
    browser.expect_visible('[data-test="cm-hook-row-1"]')
    browser.select('[data-test="cm-hook-row-1"] [data-test="cm-hook-event"]', "Stop")
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
    """A loaded row's Type and Event selects show the stored values, not the first option."""
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
    browser.select('[data-test="cm-hook-event"]', "PreToolUse")
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
