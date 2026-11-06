"""Config-management dashboard UI (#773, slice A) — gate + markup contract.

The navbar trigger and its modal are the first UI over the gated
``/api/config-write/*`` surfaces. They render **only** when
``config_write.enabled`` (the invisible-surface invariant the routes enforce with
a 404), and the *User* scope option renders only when ``allow_user_scope`` is also
on. Slice A wires the CLAUDE.md and settings surfaces; later slices append tabs.

These are server-render contract tests: the modal lives inside an Alpine
``<template x-if>``, whose children Jinja still emits as inert markup, so the
``data-test`` hooks are present in the returned HTML regardless of client state.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import load_config
from clauster.config_write_skills import SKILL_OVERRIDE_VALUES

_OFF = ""
_ON = "config_write:\n  enabled: true\n"
_ON_USER = "config_write:\n  enabled: true\n  allow_user_scope: true\n"


def _html(write_config, extra: str) -> str:
    client = TestClient(create_app(load_config(write_config(extra))))
    resp = client.get("/")
    assert resp.status_code == 200
    return resp.text


def _row_html(write_config, extra: str) -> str:
    """The per-row FRAGMENT render (GET /api/projects/{name}/row), not the full page."""
    client = TestClient(create_app(load_config(write_config(extra))))
    resp = client.get("/api/projects/alpha/row")
    assert resp.status_code == 200
    return resp.text


# ---- gate: trigger + modal only when config-write is enabled ----------------


def test_trigger_absent_when_config_write_disabled(write_config):
    assert 'data-test="configmgmt-trigger"' not in _html(write_config, _OFF)


def test_trigger_present_when_config_write_enabled(write_config):
    assert 'data-test="configmgmt-trigger"' in _html(write_config, _ON)


def test_modal_absent_when_config_write_disabled(write_config):
    assert 'data-test="configmgmt-modal"' not in _html(write_config, _OFF)


def test_modal_and_slice_a_surfaces_present_when_enabled(write_config):
    html = _html(write_config, _ON)
    assert 'data-test="configmgmt-modal"' in html
    # Both slice-A surfaces are wired.
    assert 'data-test="cm-view-claude-md"' in html
    assert 'data-test="cm-view-settings"' in html
    # Scope toggle (project/local), project picker, confirm, and save.
    assert 'data-test="cm-scope-project"' in html
    assert 'data-test="cm-scope-local"' in html
    assert 'data-test="cm-project"' in html
    assert 'data-test="cm-confirm"' in html
    assert 'data-test="cm-save"' in html


def test_permissions_and_hooks_surfaces_present_when_enabled(write_config):
    # Slice B adds the permissions + hooks surface tabs and their JSON editors.
    html = _html(write_config, _ON)
    assert 'data-test="cm-view-permissions"' in html
    assert 'data-test="cm-permissions-text"' in html
    assert 'data-test="cm-view-hooks"' in html
    assert 'data-test="cm-hooks-text"' in html


def test_permissions_rows_editor_present_when_enabled(write_config):
    # #958 Part 5: the permissions surface gains a friendly allow/deny/ask rows editor
    # + a defaultMode select, layered over the raw-JSON escape hatch. Assert the mode
    # toggle, the rows scaffold, the mode select, and the raw textarea are all wired.
    html = _html(write_config, _ON)
    assert 'data-test="cm-permissions-mode-rows"' in html
    assert 'data-test="cm-permissions-mode-raw"' in html
    assert 'data-test="cm-permissions-rows"' in html
    assert 'data-test="cm-perm-defaultmode"' in html
    assert 'data-test="cm-permissions-rows-error"' in html
    # The per-bucket row/add/remove data-tests are Alpine-bound (x-for), so their
    # binding expressions appear in the markup even before the rows hydrate.
    assert "'cm-perm-rule-' + bucket.key" in html
    assert "'cm-perm-add-' + bucket.key" in html
    # The raw JSON textarea stays as the escape hatch for unrepresentable shapes.
    assert 'data-test="cm-permissions-text"' in html
    # The rows editor + its projection/vocabulary helpers are wired in the Alpine script.
    assert "configMgmtPermissionsMode(" in html
    assert "_permissionsSerialized(" in html
    assert "configMgmtPermissionModeOptions(" in html
    assert "configMgmtAddPermRow(" in html


def test_permissions_new_keys_and_auto_mode_box_present(write_config):
    # TASK-449.3: the switches; TASK-449.1: the autoMode box and its scope note.
    html = _html(write_config, _ON_USER)
    assert 'data-test="cm-perm-flags"' in html
    assert "'cm-perm-flag-' + flag.key" in html
    assert 'data-test="cm-automode-text"' in html
    assert 'data-test="cm-automode-scope-warning"' in html
    assert "Switch to User scope to save auto mode rules." in html
    # The additionalDirectories bucket and the four switches are in the Alpine state.
    assert 'key: "additionalDirectories"' in html
    for key in (
        "blockReadsOutsideWorkingDirectories",
        "blockWritesOutsideWorkingDirectories",
        "disableBypassPermissionsMode",
        "disableAutoMode",
    ):
        assert f'key: "{key}"' in html
    assert "body.autoMode = am" in html
    # The scope note is described to screen readers only where it shows.
    assert (
        ":aria-describedby=\"configMgmt.scope !== 'user' ? "
        "'cm-automode-scope-warning cm-automode-hint' : 'cm-automode-hint'\""
    ) in html
    assert 'aria-describedby="cm-perm-flags-hint"' in html
    # ...and each reference has its target, so none dangles.
    for target in ("cm-perm-flags-hint", "cm-automode-hint", "cm-automode-scope-warning"):
        assert f'id="{target}"' in html
    # A failed load clears the text/orig/hash too, so an emptied row set cannot save over
    # the file with the previous target's hash (the catch path keeps them otherwise).
    assert 'p.text = ""; p.orig = ""; p.hash = null;' in html


def test_auto_mode_note_without_user_scope_points_at_the_host_file(write_config):
    # With allow_user_scope off there is no User button, so the note must not offer one.
    html = _html(write_config, _ON)
    assert "Switch to User scope to save auto mode rules." not in html
    assert "editing is off in this Clauster configuration" in html


def test_hooks_rows_editor_present_when_enabled(write_config):
    # #958 Part 5: the hooks surface gains a flat rows editor (event/matcher/command/
    # timeout) over the nested shape, layered on the raw-JSON escape hatch.
    html = _html(write_config, _ON)
    assert 'data-test="cm-hooks-mode-rows"' in html
    assert 'data-test="cm-hooks-mode-raw"' in html
    assert 'data-test="cm-hooks-rows"' in html
    assert 'data-test="cm-hooks-table"' in html
    assert 'data-test="cm-hook-event"' in html
    assert 'data-test="cm-hook-matcher"' in html
    assert 'data-test="cm-hook-command"' in html
    assert 'data-test="cm-hook-timeout"' in html
    assert 'data-test="cm-hook-remove"' in html
    assert 'data-test="cm-hooks-add"' in html
    assert 'data-test="cm-hooks-rows-error"' in html
    # The raw JSON textarea stays as the escape hatch.
    assert 'data-test="cm-hooks-text"' in html
    # The rows editor + its projection/vocabulary helpers are wired in the Alpine script.
    assert "configMgmtHooksMode(" in html
    assert "_hooksSerialized(" in html
    assert "configMgmtAddHookRow(" in html
    assert "configMgmtHookEvents(" in html
    # The event vocabulary is server-injected from the backend validator (no drift).
    assert "const HOOK_EVENTS =" in html


def test_hook_events_match_backend_recognized_events(write_config):
    # The injected HOOK_EVENTS const must equal the backend's RECOGNIZED_EVENTS (sorted)
    # so the rows editor's event <select> never offers an event the write surface rejects.
    import json
    import re

    from clauster.config_write_hooks import RECOGNIZED_EVENTS

    html = _html(write_config, _ON)
    m = re.search(r"const HOOK_EVENTS = (\[[^\]]*\]);", html)
    assert m, "HOOK_EVENTS const not found in the rendered dashboard"
    assert json.loads(m.group(1)) == sorted(RECOGNIZED_EVENTS)


def test_settings_env_rows_editor_present_when_enabled(write_config):
    # #765: the settings surface gains a friendly env key/value rows editor layered
    # over the raw-JSON escape hatch. Assert the mode toggle, the row editor, and the
    # raw textarea (still present as the escape hatch) are all wired.
    html = _html(write_config, _ON)
    assert 'data-test="cm-settings-mode-rows"' in html
    assert 'data-test="cm-settings-mode-raw"' in html
    assert 'data-test="cm-settings-rows"' in html
    assert 'data-test="cm-settings-env-table"' in html
    assert 'data-test="cm-settings-env-key"' in html
    assert 'data-test="cm-settings-env-value"' in html
    assert 'data-test="cm-settings-env-add"' in html
    assert 'data-test="cm-settings-env-remove"' in html
    # The raw JSON textarea stays as the escape hatch for model / misc keys.
    assert 'data-test="cm-settings-text"' in html
    # The friendly editor + its projection helpers are wired in the Alpine script.
    assert "configMgmtSettingsMode(" in html
    assert "_settingsRowsSerialized(" in html
    assert 'const CONFIG_MASK = "********"' in html


def test_settings_typed_rows_are_wired(write_config):
    # TASK-271: every top-level key is a typed row, and the editor types and owned-key
    # tabs come from the read payload, not from a list kept in the script.
    html = _html(write_config, _ON)
    for hook in (
        "cm-settings-keys",
        "cm-settings-key-row",
        "cm-settings-bool",
        "cm-settings-number",
        "cm-settings-enum",
        "cm-settings-string",
        "cm-settings-readonly",
        "cm-settings-edit-raw",
        "cm-settings-owned",
        "cm-settings-owner-link",
        "cm-settings-add-key",
        "cm-settings-add-key-btn",
        "cm-settings-key-remove",
        "cm-settings-number-error",
    ):
        assert f'data-test="{hook}"' in html, hook
    assert "s.keyTypes = isMap(d.key_types)" in html
    assert "s.ownedKeys = isMap(d.owned_keys)" in html
    # No copy of the server's value sets in the page: a select value comes from the payload.
    assert '"fullscreen"' not in html


def test_settings_owned_key_tabs_are_surface_labels(write_config):
    # The owner link matches the server's tab name against a surface label; a tab name
    # with no surface would hide the link, so every carved key must name a real tab.
    from clauster.config_write_settings import OWNED_KEY_TABS

    html = _html(write_config, _ON)
    for tab in set(OWNED_KEY_TABS.values()):
        assert f'{{ key: "{tab.lower()}", label: "{tab}" }}' in html, tab


def test_settings_desktop_only_note_is_wired(write_config):
    # TASK-490: the note names the Desktop-only keys the server lists for the loaded file.
    html = _html(write_config, _ON)
    assert 'data-test="cm-settings-desktop-only"' in html
    assert 'x-show="configMgmt.settings.desktopOnly.length"' in html
    assert "configMgmt.settings.desktopOnly.join(', ')" in html
    assert "Only Claude Desktop reads them. The Claude Code CLI ignores them" in html
    assert "c.settings.desktopOnly = Array.isArray(d.desktop_only)" in html


def test_subagents_surface_present_when_enabled(write_config):
    # Slice C adds the subagents list surface (list + per-agent editor + delete).
    html = _html(write_config, _ON)
    # The tab itself is data-driven (its data-test is Alpine-bound), so assert the
    # surface is registered in the JS surfaces array rather than a literal tab hook.
    assert 'key: "subagents"' in html
    assert 'data-test="cm-view-subagents"' in html
    assert 'data-test="cm-agent-new"' in html
    assert 'data-test="cm-agent-editor"' in html
    # The Local scope tab is conditionally hidden for user/project-only surfaces.
    assert "configMgmtSurfaceHasLocal()" in html
    # #958 Part 5: the Name box is the single source — auto-synced into the frontmatter
    # on save (no double entry), with a hint and a seeded new-agent template.
    assert 'data-test="cm-agent-name-hint"' in html
    assert "_syncAgentName(" in html
    assert "const AGENT_TEMPLATE =" in html


def test_subagent_editor_shows_the_mcp_conversion_note(write_config):
    # TASK-449.5: a file loaded with the old `mcpServers` mapping arrives rewritten as the
    # list form plus a `converted` note. The editor must show that note before the save,
    # and count the load as dirty so the converted text can be saved without an edit.
    html = _html(write_config, _ON)
    assert 'data-test="cm-agent-converted"' in html
    assert 'x-show="configMgmt.subagents.converted"' in html
    assert 'x-text="configMgmt.subagents.converted"' in html
    assert 's.converted = typeof d.converted === "string" ? d.converted : "";' in html
    assert "(s.content !== s.orig || !!s.converted)" in html
    # The note is read with the textarea, and meets AA contrast on its tint.
    assert 'aria-describedby="cm-agent-name-hint cm-agent-converted"' in html
    assert "alert alert-info text-info-emphasis" in html
    # The note and the extra dirty state are cleared on every path that leaves or replaces
    # the editor: new agent, cancel, list reload, surface switch, and a successful save.
    assert 's.content = AGENT_TEMPLATE; s.orig = ""; s.hash = null; s.confirm = "";\n' in html
    assert '        s.converted = "";\n      },\n      // #958 Part 5' in html
    assert 'c.subagents.confirm = ""; c.subagents.converted = ""; c.error = "";' in html
    assert 'c.subagents.deletingName = ""; c.subagents.converted = "";' in html
    assert html.count('c.subagents.converted = "";') == 3
    assert 'c.saved = true; s.editing = false; s.confirm = ""; s.converted = "";' in html


def test_subagent_editor_keeps_save_off_for_a_doc_it_cannot_show(write_config):
    # TASK-427: a doc whose frontmatter secret cannot be masked in place arrives with no
    # content and `editable: false`. Save stays off and the textarea is read-only; the flag
    # is reset on every path that opens, creates, cancels or reloads.
    html = _html(write_config, _ON)
    open_agent = _between(html, "async configMgmtOpenAgent(", "configMgmtCancelAgent()")
    assert "s.readOnly = d.editable === false;" in open_agent
    can_save = _between(html, "configMgmtCanSaveAgent() {", "async configMgmtSaveAgent()")
    assert "if (s.busy || s.readOnly || this.configMgmtNeedsProject()) return false;" in can_save
    assert ':readonly="configMgmt.subagents.readOnly"' in html
    assert html.count("readOnly = false;") == 3


def test_mcp_surface_present_when_enabled(write_config):
    # Slice D adds the MCP list surface (servers CRUD + project approvals panel).
    html = _html(write_config, _ON)
    # The tab is data-driven (Alpine-bound data-test), so assert the surface is
    # registered in the JS surfaces array rather than a literal tab hook.
    assert 'key: "mcp"' in html
    assert 'data-test="cm-view-mcp"' in html
    assert 'data-test="cm-mcp-new"' in html
    assert 'data-test="cm-mcp-editor"' in html
    assert 'data-test="cm-mcp-entry"' in html
    # The project-scope approvals sub-panel + its reset control.
    assert 'data-test="cm-mcp-approvals"' in html
    assert 'data-test="cm-mcp-approvals-save"' in html
    assert 'data-test="cm-mcp-reset-go"' in html
    # TASK-449.8: the convert-on-load notes, in the list and in the editor.
    assert 'data-test="cm-mcp-conversions"' in html
    assert 'data-test="cm-mcp-entry-conversions"' in html
    # The template's own call (the bare name also matches the method definition).
    assert (
        "!configMgmt.mcp.isNew && configMgmtMcpConversionsFor(configMgmt.mcp.name).length" in html
    )


def test_mcp_editor_has_fields_and_raw_json_modes(write_config):
    # TASK-272: a type select shows that transport's fields; Raw JSON stays the fallback.
    html = _html(write_config, _ON)
    for hook in (
        "cm-mcp-mode-fields",
        "cm-mcp-mode-raw",
        "cm-mcp-fields-error",
        "cm-mcp-raw-blocked",
        "cm-mcp-fields",
        "cm-mcp-type",
        "cm-mcp-stdio",
        "cm-mcp-command",
        "cm-mcp-arg-add",
        "cm-mcp-env-add",
        "cm-mcp-remote",
        "cm-mcp-url",
        "cm-mcp-header-add",
        "cm-mcp-headers-helper",
        "cm-mcp-oauth",
        "cm-mcp-oauth-client-id",
        "cm-mcp-oauth-port",
        "cm-mcp-oauth-metadata-url",
        "cm-mcp-oauth-scopes",
        "cm-mcp-timeout",
        "cm-mcp-always-load",
        "cm-mcp-raw",
        "cm-mcp-entry",
        "cm-mcp-fields-warn",
        "cm-mcp-retype",
    ):
        assert f'data-test="{hook}"' in html, hook
    # Every visible control in the form has a label bound to it.
    for control in ("cm-mcp-type", "cm-mcp-command", "cm-mcp-url", "cm-mcp-timeout"):
        assert f'for="{control}"' in html, control


def test_mcp_type_select_lists_every_type_the_backend_reads(write_config):
    # The <select> options and the JS transport map are server-injected from the validator,
    # so a type the backend gains or drops changes the form with it.
    from clauster.config_write_mcp import TRANSPORT_ALIASES

    html = _html(write_config, _ON)
    select = html[html.index('data-test="cm-mcp-type"') :]
    select = select[: select.index("</select>")]
    assert re.findall(r'<option value="([^"]+)">', select) == list(TRANSPORT_ALIASES)
    assert '"streamable-http": "http"' in html
    assert "const MCP_TRANSPORTS = new Map(Object.entries(" in html


def test_mcp_save_is_blocked_by_the_fields_and_retype_warnings(write_config):
    # TASK-272: the editor explains the backend's masked-value refusal before the save and
    # keeps Save off until the masked values are retyped; it never sends a bypass.
    html = _html(write_config, _ON)
    assert (
        "if (this.configMgmtMcpFieldsWarning() || this.configMgmtMcpRetypeWarning()) return false;"
        in html
    )
    assert 'x-show="configMgmtMcpRetypeWarning()"' in html
    # The Raw JSON switch refuses the same lossy rows (Greptile on PR 1815).
    assert 'if (problems.length) { m.rawBlocked = problems.join(" "); return; }' in html
    # A fields save builds the entry from the form; a raw save parses the textarea.
    assert 'if (m.mode === "fields") entry = this._mcpFieldsEntry();' in html


def test_mcp_delete_confirm_suppresses_approvals_confirms(write_config):
    # #1043: the delete confirm and the approvals save/reset confirms both bind the
    # scope token, so two identical "Type <scope>" inputs could stack with no signal
    # which action fires. Only one may show at a time — both approvals confirms are
    # gated off while a delete is armed (configMgmt.mcp.deletingName).
    html = _html(write_config, _ON)
    assert "configMgmtMcpApprovalsDirty() && !configMgmt.mcp.deletingName" in html
    assert '<details x-show="!configMgmt.mcp.deletingName"' in html


def test_mcp_approval_link_gated_on_config_write_on_both_render_paths(write_config):
    # #837: the "Resolve in Server approvals" link inside the per-project readiness
    # detail jumps into the Server-approvals panel — which 404s when config-write is
    # OFF. So the actionable link must render ONLY when config-write is enabled, on
    # BOTH the full-page dashboard render AND the api_project_row fragment (the two
    # paths that emit _project_row.html). The <template x-for> row markup is emitted
    # inert by Jinja regardless of runtime state, so a plain data-test presence check
    # is exact here — the ONLY gate on the button is {% if config_write_enabled %}.
    link = 'data-test="mcp-approval-link"'
    # Full-page render.
    assert link not in _html(write_config, _OFF)
    assert link in _html(write_config, _ON)
    # Fragment render (must be threaded the same flag, else it reads undefined→falsy
    # and the link would WRONGLY vanish on dynamically-inserted rows when it IS on).
    assert link not in _row_html(write_config, _OFF)
    assert link in _row_html(write_config, _ON)


def test_skills_surface_present_when_enabled(write_config):
    # Slice E adds the skills list surface (list + per-skill SKILL.md editor + delete).
    html = _html(write_config, _ON)
    # The tab is data-driven (Alpine-bound data-test), so assert the surface is
    # registered in the JS surfaces array rather than a literal tab hook.
    assert 'key: "skills"' in html
    assert 'data-test="cm-view-skills"' in html
    assert 'data-test="cm-skill-new"' in html
    assert 'data-test="cm-skill-editor"' in html
    assert 'data-test="cm-skill-content"' in html
    # Skills has a Local scope for its visibility map (TASK-249), so it is NOT in the
    # no-local list; subagents still are. Match the array membership rather than exact
    # spacing/ordering so a reformat can't break this.
    no_local = re.search(r"configMgmtNoLocalSurfaces:\s*\[([^\]]*)\]", html)
    assert no_local and '"skills"' not in no_local.group(1)
    assert '"subagents"' in no_local.group(1)
    # At Local scope the folder list and its New button hide behind a note.
    assert 'data-test="cm-skill-local-note"' in html


def test_skill_visibility_panel_present_when_enabled(write_config):
    # TASK-249: the Skills tab reads and writes `skillOverrides` through its own panel.
    html = _html(write_config, _ON)
    for hook in (
        "cm-skill-ov",
        "cm-skill-ov-unavailable",
        "cm-skill-ov-table",
        "cm-skill-ov-add-name",
        "cm-skill-ov-add-value",
        "cm-skill-ov-add",
        "cm-skill-ov-confirm",
        "cm-skill-ov-save",
    ):
        assert f'data-test="{hook}"' in html, hook
    assert '"/api/config-write/skills/overrides' in html
    # Each of the four values is an option in BOTH selects: the per-row select, plus
    # "No override", which removes the key, and the add form's select. Cut each select out
    # so one cannot satisfy the pin for the other.
    row_select = _between(html, ":data-test=\"'cm-skill-ov-' + row.name\"", "</select>")
    add_select = _between(html, 'data-test="cm-skill-ov-add-value"', "</select>")
    for value in sorted(SKILL_OVERRIDE_VALUES):
        option = f'<option value="{value}">{value}</option>'
        assert option in row_select, value
        assert option in add_select, value
    assert '<option value="">No override</option>' in row_select
    assert "No override" not in add_select  # adding a name with no value means nothing
    # The save sends a name as typed: no client-side name rule, and an own-key map so
    # `__proto__` reaches the backend rule. A trim or a name regex here would be a second
    # copy of the backend check.
    assert "Object.fromEntries(this.configMgmt.skills.ov.rows" in html
    assert "ov.addName.trim()" not in html


def test_skill_visibility_values_match_the_backend(write_config):
    # The panel tells a known stored value from a "Not valid" one with this list. If the
    # backend gains a value, the panel must too, or it shows a valid value as not valid.
    html = _html(write_config, _ON)
    js = re.search(r"configMgmtSkillOvValues:\s*\[([^\]]*)\]", html)
    assert js
    assert set(re.findall(r'"([^"]*)"', js.group(1))) == SKILL_OVERRIDE_VALUES


def test_plugins_surface_present_when_enabled(write_config):
    # Slice F adds the plugins + marketplaces surface (two CLI-driven action panels).
    html = _html(write_config, _ON)
    assert 'key: "plugins"' in html
    assert 'data-test="cm-view-plugins"' in html
    # Shared scope-token confirm arming the actions.
    assert 'data-test="cm-plugin-confirm"' in html
    # Plugins panel: table + install form (with the strong retype-id confirm).
    assert 'data-test="cm-plugins-table"' in html
    assert 'data-test="cm-plugin-install-id"' in html
    assert 'data-test="cm-plugin-install-confirm"' in html
    assert 'data-test="cm-plugin-install-go"' in html
    # Marketplaces panel: table + add form.
    assert 'data-test="cm-marketplaces-table"' in html
    assert 'data-test="cm-marketplace-add-source"' in html
    assert 'data-test="cm-marketplace-add-go"' in html
    # A marketplace row shows its location, not only the source type that
    # `marketplace list --json` carries in `source` (TASK-449.7).
    assert 'x-text="configMgmtMarketSource(m)"' in html
    assert "configMgmtMarketSource(m) {" in html
    assert 'x-text="m.source"' not in html
    # Plugins support all three scopes — NOT in the no-local list.
    no_local = re.search(r"configMgmtNoLocalSurfaces:\s*\[([^\]]*)\]", html)
    assert no_local and '"plugins"' not in no_local.group(1)


# ---- User scope option is gated on allow_user_scope -------------------------


def test_user_scope_button_absent_without_allow_user_scope(write_config):
    assert 'data-test="cm-scope-user"' not in _html(write_config, _ON)


def test_user_scope_button_present_with_allow_user_scope(write_config):
    assert 'data-test="cm-scope-user"' in _html(write_config, _ON_USER)


# ---- a11y: active scope/surface state is programmatic, not color-only ---------


def test_scope_and_surface_toggles_expose_aria_pressed(write_config):
    # The active selection must be conveyed with :aria-pressed, not just the
    # btn-primary fill — mirroring the filter chips + sort toggle convention so a
    # screen reader announces which scope/surface is current.
    html = _html(write_config, _ON_USER)
    assert html.count(":aria-pressed") >= 5  # 3 scope buttons + >=2 surface tabs
    assert "aria-pressed=\"configMgmt.scope === 'project'\"" in html
    assert 'aria-pressed="configMgmt.surface === s.key"' in html


# ---- #848: non-interactive auth callout in the login-shepherd card ------------

_LS_ON = "login_shepherd:\n  enabled: true\n"
_LS_ON_CW_ON = "login_shepherd:\n  enabled: true\nconfig_write:\n  enabled: true\n"
_LS_ON_CW_USER = (
    "login_shepherd:\n  enabled: true\nconfig_write:\n  enabled: true\n  allow_user_scope: true\n"
)


def test_noninteractive_auth_links_to_user_scope_when_user_scope_editable(write_config):
    # The deep link renders only when config-write AND allow_user_scope are on, so
    # openAuthConfig() can pin User scope — where account-wide auth belongs — instead
    # of inheriting a stale project/local scope.
    html = _html(write_config, _LS_ON_CW_USER)
    assert 'data-test="noninteractive-auth"' in html
    assert 'data-test="noninteractive-auth-open"' in html
    assert 'data-test="noninteractive-auth-userscope-hint"' not in html
    assert 'data-test="noninteractive-auth-hint"' not in html


def test_noninteractive_auth_hints_user_scope_when_allow_user_scope_off(write_config):
    # config-write on but allow_user_scope off: no deep link (it would land on a
    # project scope the account never reads) — a hint to enable User scope instead.
    html = _html(write_config, _LS_ON_CW_ON)
    assert 'data-test="noninteractive-auth"' in html
    assert 'data-test="noninteractive-auth-userscope-hint"' in html
    assert 'data-test="noninteractive-auth-open"' not in html


def test_noninteractive_auth_hints_when_config_write_off(write_config):
    # login-shepherd on but config-write off: fail closed — show the "enable
    # config_write" hint instead of a dead link into a 404 surface.
    html = _html(write_config, _LS_ON)
    assert 'data-test="noninteractive-auth"' in html
    assert 'data-test="noninteractive-auth-open"' not in html
    assert 'data-test="noninteractive-auth-hint"' in html


def test_noninteractive_auth_absent_when_login_shepherd_off(write_config):
    # The callout lives inside the login-shepherd card, so config-write alone
    # (shepherd off) must not surface it.
    html = _html(write_config, _ON)
    assert 'data-test="noninteractive-auth"' not in html


def _between(html: str, start: str, end: str) -> str:
    """Return the text of ``html`` from ``start`` up to the next ``end``."""
    head = html.index(start)
    return html[head : html.index(end, head)]


def test_skill_editor_shows_the_conversion_note(write_config):
    # TASK-449.6: a SKILL.md loaded with fields Claude Code ignores arrives with them removed
    # plus a `converted` note. The editor must show that note before the save, and count the
    # load as dirty so the converted text can be saved without an edit. Each script pin is
    # cut to the skill function it belongs to: the subagent editor has the same lines.
    html = _html(write_config, _ON)
    assert 'data-test="cm-skill-converted"' in html
    assert 'x-show="configMgmt.skills.converted"' in html
    assert 'x-text="configMgmt.skills.converted"' in html
    # TASK-427: the hint (masked lines) is described to a screen reader too.
    assert 'aria-describedby="cm-skill-converted cm-skill-hint"' in html
    open_skill = _between(html, "async configMgmtOpenSkill(", "configMgmtCancelSkill()")
    assert 's.converted = typeof d.converted === "string" ? d.converted : "";' in open_skill
    dirty = _between(html, "configMgmtSkillDirty() {", "configMgmtCanSaveSkill()")
    assert "(s.content !== s.orig || !!s.converted)" in dirty
    # Cleared on every path that leaves or replaces the editor: new skill, cancel, list
    # reload, surface switch, and a successful save.
    new_skill = _between(html, "configMgmtNewSkill() {", "async configMgmtOpenSkill(")
    assert 's.converted = "";' in new_skill
    cancel = _between(html, "configMgmtCancelSkill() {", "configMgmtSkillDirty()")
    assert 's.converted = "";' in cancel
    reload = _between(html, "async _loadSkills(keepOvEdits) {", "_skillOvRow(")
    assert 'c.skills.converted = "";' in reload
    switch = _between(
        html,
        'c.skills.editing = false; c.skills.deletingName = ""; c.skills.confirm',
        "this.configMgmtReload();",
    )
    assert 'c.skills.converted = "";' in switch
    save = _between(html, "async configMgmtSaveSkill() {", "configMgmtAskDeleteSkill(")
    assert 'c.saved = true; s.editing = false; s.confirm = ""; s.converted = "";' in save


def test_write_results_are_announced_once_by_persistent_live_regions(write_config):
    # TASK-508: the visible banners toggle with x-show, which NVDA and VoiceOver skip, so
    # they are visual only and two always-mounted regions carry the announcement. One of
    # each, or a screen reader hears a result twice (or not at all).
    html = _html(write_config, _ON)
    assert html.count('data-test="cm-error-live"') == 1
    assert html.count('data-test="cm-saved-live"') == 1
    assert (
        '<div class="visually-hidden" role="alert" data-test="cm-error-live"\n'
        '                 x-text="configMgmt.error"></div>'
    ) in html
    assert (
        '<div class="visually-hidden" role="status" data-test="cm-saved-live"\n'
        "                 x-text=\"configMgmt.saved ? 'Saved.' : ''\"></div>"
    ) in html
    for banner in ("cm-error", "cm-saved"):
        tag = html[html.index(f'data-test="{banner}"') :].split(">")[0]
        assert 'aria-hidden="true"' in tag, banner
        assert "role=" not in tag, banner  # the region below announces it, not the banner


# Every configMgmt write, with the text that ends its body. Each must reveal its result
# (TASK-508); dropping one call leaves that panel's result above the fold again.
_WRITES = [
    ("async configMgmtSave() {", "async _saveClaudeMd("),
    ("async configMgmtSaveAgent() {", "configMgmtAskDeleteAgent("),
    ("async configMgmtDeleteAgent() {", "// --- MCP servers"),
    ("async configMgmtSaveMcp() {", "configMgmtAskDeleteMcp("),
    ("async configMgmtDeleteMcp() {", "// --- MCP project approvals"),
    ("async configMgmtSaveMcpApprovals() {", "configMgmtCanResetMcp()"),
    ("async configMgmtResetMcpChoices() {", "// --- skills"),
    ("async configMgmtSaveSkill() {", "configMgmtAskDeleteSkill("),
    ("async configMgmtDeleteSkill() {", "// --- plugins + marketplaces"),
    ("async _pluginActionPost(url, body) {", "configMgmtPluginAction("),
]


def test_every_config_mgmt_write_reveals_its_result(write_config):
    html = _html(write_config, _ON)
    for start, end in _WRITES:
        body = _between(html, start, end)
        assert "const gen = c.gen, origin = document.activeElement;" in body or (
            "const origin = document.activeElement;" in body
        ), start
        # Only when the result still belongs to the panel on screen.
        assert (
            "if (c.gen === gen) this._configMgmtRevealResult(origin);" in body
            or "if (c.gen === myGen) this._configMgmtRevealResult(origin);" in body
        ), start
        # A request that never reached the server reports itself rather than rejecting.
        assert "this._configMgmtWriteFailed(e, " in body, start
    # The two refusals the MCP editor makes before sending reveal too.
    mcp = _between(html, "async configMgmtSaveMcp() {", "configMgmtAskDeleteMcp(")
    assert mcp.count("return this._configMgmtRefuse(") == 2
    # The reveal honors the OS reduced-motion preference.
    assert 'behavior: reduce ? "auto" : "smooth"' in html
