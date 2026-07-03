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
