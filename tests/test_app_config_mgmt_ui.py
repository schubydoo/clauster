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
    assert "_settingsEnvSerialized(" in html
    assert 'const CONFIG_MASK = "********"' in html


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
    # Skills are user/project only — registered in the no-local list. Match the array
    # membership rather than exact spacing/ordering so a reformat can't break this.
    no_local = re.search(r"configMgmtNoLocalSurfaces:\s*\[([^\]]*)\]", html)
    assert no_local and '"skills"' in no_local.group(1)


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
