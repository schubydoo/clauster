"""Canonical permission-label map is the single source for every label surface (#685)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.config import (
    BYPASS_DESKTOP_HINT,
    PERMISSION_LABELS,
    PERMISSION_MODES,
    load_config,
)
from clauster.config_editor import field_specs

# A per-project ceiling that surfaces the gated bypassPermissions option in the picker.
_BYPASS_CEILING = "projects:\n  alpha:\n    allow_bypass_permissions: true\n"


def _client(write_config, extra: str = "") -> TestClient:
    return TestClient(create_app(load_config(write_config(extra))))


def test_canonical_map_covers_every_mode_with_all_three_forms() -> None:
    # The map is THE source of truth: one entry per wire token, each carrying the
    # short/long/effect trio every surface needs.
    assert set(PERMISSION_LABELS) == set(PERMISSION_MODES)
    for mode, labels in PERMISSION_LABELS.items():
        assert set(labels) == {"short", "long", "effect"}, mode
        assert all(labels[form].strip() for form in ("short", "long", "effect")), mode


def test_desktop_only_hint_is_not_baked_into_any_label() -> None:
    # The "(Desktop only)" caveat lives OUT of the canonical label as a contextual hint,
    # so it never appears in the stored short/long/effect strings.
    assert BYPASS_DESKTOP_HINT == "(Desktop only)"
    for labels in PERMISSION_LABELS.values():
        for form in ("short", "long", "effect"):
            assert BYPASS_DESKTOP_HINT not in labels[form]


def test_config_editor_choice_labels_derive_from_canonical_long_form() -> None:
    # The config editor reads the canonical map (no second hand-maintained copy):
    # every permission choice label is exactly the canonical "long" form.
    perm = field_specs()["instance_defaults.permission_mode"]
    choice_labels = perm["choice_labels"]
    assert choice_labels == {m: PERMISSION_LABELS[m]["long"] for m in PERMISSION_MODES}


def test_js_const_injects_the_canonical_map(write_config) -> None:
    # The dashboard exposes the canonical map to Alpine/JS as a single const, and the
    # helpers read from it rather than an inline duplicate.
    page = _client(write_config).get("/").text
    assert "const PERMISSION_LABELS = " in page
    # permLabel/permissionEffect now read the map — not a literal object of their own.
    assert "(PERMISSION_LABELS[mode] || {}).short || mode" in page
    assert '(PERMISSION_LABELS[mode] || {}).effect || ""' in page
    # Every mode key and its ASCII label strings ship in the injected JSON. (Non-ASCII
    # glyphs like the ⚠ in the bypass label are \\u-escaped by tojson, so only assert
    # verbatim on the ASCII-clean strings — the const-presence check above covers the rest.)
    for mode, labels in PERMISSION_LABELS.items():
        assert f'"{mode}"' in page
        for form in ("short", "long", "effect"):
            if labels[form].isascii():
                assert labels[form] in page


def test_launch_select_options_render_the_canonical_long_form(write_config) -> None:
    # The picker <option> text is the canonical "long" form for every non-gated mode.
    page = _client(write_config).get("/").text
    for mode, labels in PERMISSION_LABELS.items():
        if mode == "bypassPermissions":
            continue  # gated; only rendered when the ceiling allows it (asserted below)
        assert f'<option value="{mode}">{labels["long"]}</option>' in page


def test_bypass_option_appends_the_desktop_hint_outside_the_label(write_config) -> None:
    # With the ceiling on, the gated option renders the canonical long label PLUS the
    # contextual hint appended in the template (never baked into the label string).
    page = _client(write_config, _BYPASS_CEILING).get("/").text
    long = PERMISSION_LABELS["bypassPermissions"]["long"]
    assert f">{long} {BYPASS_DESKTOP_HINT}</option>" in page
    # The option is still Desktop-gated via the :disabled binding.
    assert '<option value="bypassPermissions" :disabled="lmode !== \'desktop\'"' in page


def test_no_bypass_option_without_ceiling(write_config) -> None:
    # Fail-closed: no ceiling -> the gated option is absent from the picker entirely.
    page = _client(write_config).get("/").text
    assert '<option value="bypassPermissions"' not in page


def test_perm_tooltip_ends_cleanly(write_config) -> None:
    # The effect tooltip joins modes with " · " and must end cleanly. When bypass is
    # filtered out (the common no-ceiling case) the trailing item is dontAsk, whose
    # effect already ends in a period — so the tooltip must end with that effect string
    # verbatim: no dangling " · " separator AND no doubled-up trailing period.
    import re

    row = _client(write_config).get("/api/projects/alpha/row").text
    m = re.search(r'title="What the agent may do without asking[^"]*"', row)
    assert m is not None
    tooltip = m.group(0)
    assert ' · "' not in tooltip  # no dangling separator before the closing quote
    last_effect = PERMISSION_LABELS["dontAsk"]["effect"]
    assert last_effect.endswith(".")  # the effect supplies its own period
    assert tooltip.endswith(f'{last_effect}"')  # ends with the effect itself — no extra "."
    assert ".." not in tooltip  # no doubled period anywhere


def test_project_row_endpoint_carries_the_map(write_config) -> None:
    # The standalone row render (reactive insert) must inject the map too, so its
    # picker renders identically to the dashboard grid loop.
    row = _client(write_config, _BYPASS_CEILING).get("/api/projects/alpha/row").text
    assert f'<option value="default">{PERMISSION_LABELS["default"]["long"]}</option>' in row
    assert f"{PERMISSION_LABELS['bypassPermissions']['long']} {BYPASS_DESKTOP_HINT}" in row
