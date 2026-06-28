"""Unit tests for the safe-allowlist config-edit core (FE-3, #299)."""

from __future__ import annotations

import pytest

from clauster.config import load_config
from clauster.config_editor import (
    EDITABLE_FIELDS,
    ConfigValidationError,
    DisallowedFieldError,
    disk_state,
    editable_values,
    field_specs,
    file_hash,
    validate_edits,
)


def _raw(write_config, extra: str = "") -> dict:
    """Load a config file and return its validated raw mapping for editing."""
    path = write_config(extra)
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_editable_values_extracts_only_tier_a(write_config) -> None:
    config = load_config(write_config("usage:\n  fx_rate: 2.5\n"))
    vals = editable_values(config)
    # Exactly the allowlist — equality (not subset) catches a silently-dropped field.
    assert set(vals) == set(EDITABLE_FIELDS)
    assert vals["usage.fx_rate"] == 2.5
    # No secret/auth/bind field is ever surfaced.
    assert not any(k.startswith(("auth.", "host", "port")) for k in vals)
    assert "auth.password_hash" not in vals


def test_validate_edits_accepts_tier_a_change(write_config) -> None:
    raw = _raw(write_config)
    candidate = validate_edits(raw, {"usage.fx_rate": 1.5, "metrics.show_disk": False})
    assert candidate["usage"]["fx_rate"] == 1.5
    assert candidate["metrics"]["show_disk"] is False


def test_validate_edits_rejects_disallowed_field(write_config) -> None:
    raw = _raw(write_config)
    # An auth field is forbidden via the editor — rejected before any merge.
    with pytest.raises(DisallowedFieldError):
        validate_edits(raw, {"auth.enabled": False})
    with pytest.raises(DisallowedFieldError):
        validate_edits(raw, {"host": "0.0.0.0"})


def test_validate_edits_trips_fail_closed_validator(write_config) -> None:
    # A *value* that's individually out of range still fails re-validation, never silently.
    raw = _raw(write_config)
    with pytest.raises(ConfigValidationError):
        validate_edits(raw, {"instance_defaults.capacity": -1})


def test_field_specs_classifies_control_types() -> None:
    specs = field_specs()
    assert set(specs) == set(EDITABLE_FIELDS)
    assert specs["metrics.show_disk"]["type"] == "bool"
    assert specs["instance_defaults.capacity"]["type"] == "int"
    assert specs["usage.fx_rate"]["type"] == "float"
    # A Literal field surfaces its choices for a dropdown.
    rm = specs["instance_defaults.permission_mode"]
    assert rm["type"] == "enum" and rm["choices"]
    # An Optional[str] resolves to a plain string control.
    assert specs["instance_defaults.session_name_prefix"]["type"] == "str"


def test_field_specs_carries_rich_ui_metadata() -> None:
    specs = field_specs()
    poll = specs["claude.agents_json_poll_interval_seconds"]
    # Human label + raw key + section heading for grouping.
    assert poll["label"] == "Liveness poll interval"
    assert poll["key"] == "agents_json_poll_interval_seconds"
    assert poll["section_label"] == "Claude"
    # Numeric bound + unit + default surfaced for the control.
    assert poll["unit"] == "seconds" and poll["min"] == 1 and poll["default"] == 30
    # Master/child dependency for disabling, and a placeholder for an optional field.
    assert specs["metrics.normalize_cpu"]["depends_on"] == "metrics.enabled"
    assert specs["instance_defaults.max_bridges"]["placeholder"]


def test_field_specs_recap_limit_depends_on_recap_toggle() -> None:
    # The recap size limit only applies when the recap toggle is on, so the editor greys it
    # out when `claude.resume_recap` is off (same FIELD_DEPENDS mechanism as the metrics block).
    specs = field_specs()
    assert specs["claude.resume_recap_max_chars"]["depends_on"] == "claude.resume_recap"


def test_field_specs_permission_mode_carries_friendly_choice_labels() -> None:
    # The config dropdown shows the SAME friendly wording as the "Run Claude here" launch
    # dropdown (the saved value is still the raw enum token), instead of bare tokens.
    specs = field_specs()
    perm = specs["instance_defaults.permission_mode"]
    labels = perm["choice_labels"]
    assert labels is not None
    # Every enum value has a human label, keyed by the raw value that gets saved.
    assert set(labels) == set(perm["choices"])
    assert labels["default"] == "Ask each time (default)"
    assert labels["dontAsk"] == "Never prompt — deny unknowns"
    # A non-enum field has no label map (None), so the template falls back to the raw value.
    assert specs["instance_defaults.session_name_prefix"]["choice_labels"] is None


def test_field_specs_more_enum_dropdowns_carry_friendly_labels() -> None:
    # Polish-2: resume_mode / spawn_mode / usage.mode now show friendly labels in the config panel
    # too (the saved value is unchanged), matching the permission_mode treatment.
    specs = field_specs()
    rm = specs["claude.launch_mode"]["choice_labels"]
    sm = specs["instance_defaults.spawn_mode"]["choice_labels"]
    um = specs["usage.mode"]["choice_labels"]
    assert rm is not None and set(rm) == set(specs["claude.launch_mode"]["choices"])
    assert sm is not None and sm["worktree"] == "Git worktree"
    assert um is not None and um["off"] == "Off"


def test_pty_screen_enabled_is_editable_bool_with_safety_note() -> None:
    # #534 S5: the live pty-screen tap is a Tier-A editable bool carrying a friendly label and
    # the "best-effort redaction → auth-gate, not secret-proof" safety note in its description.
    assert "claude.pty_screen_enabled" in EDITABLE_FIELDS
    spec = field_specs()["claude.pty_screen_enabled"]
    assert spec["type"] == "bool"
    assert spec["label"] == "Live Interactive Session terminal view"
    assert "redact" in spec["description"].lower()


def test_verbose_toggle_is_editable_bool_with_restart_note() -> None:
    # The standard-bridge --verbose toggle is a Tier-A editable bool carrying a friendly label
    # and the standard-only / restart-required note in its description.
    assert "instance_defaults.verbose" in EDITABLE_FIELDS
    spec = field_specs()["instance_defaults.verbose"]
    assert spec["type"] == "bool"
    assert spec["label"] == "Verbose bridge logging"
    assert "standard" in spec["description"].lower()
    assert "next bridge start" in spec["description"].lower()


def test_validate_edits_accepts_verbose_toggle(write_config) -> None:
    raw = _raw(write_config)
    candidate = validate_edits(raw, {"instance_defaults.verbose": True})
    assert candidate["instance_defaults"]["verbose"] is True


def test_editable_values_surfaces_verbose_default_false(write_config) -> None:
    vals = editable_values(load_config(write_config()))
    assert vals["instance_defaults.verbose"] is False


def test_classify_and_constraints_cover_edge_annotations() -> None:
    import types as _types

    import annotated_types as at

    from clauster.config_editor import _classify, _constraints

    # A union with >1 non-None member falls through to the scalar-string fallback.
    assert _classify(int | str | None) == ("str", None)
    # Lt maps to max; Ge to min; an unrecognized metadata item is simply skipped (loop tail).
    meta = _types.SimpleNamespace(metadata=[at.Lt(lt=5), at.Ge(ge=1), object()])
    assert _constraints(meta) == {"max": 5, "min": 1}


def test_file_hash_changes_with_content(write_config) -> None:
    path = write_config("usage:\n  fx_rate: 1.0\n")
    h1 = file_hash(path)
    path.write_text(path.read_text(encoding="utf-8") + "# touched\n", encoding="utf-8")
    assert file_hash(path) != h1


def test_field_specs_exposes_claustrum_block_with_depends() -> None:
    # #539: the claustrum hosted-channel block is editable in-app — the master `enabled`
    # toggle plus operational fields that grey out when it is off (same FIELD_DEPENDS
    # mechanism as the metrics block). None are secrets, so all are Tier-A safe.
    specs = field_specs()
    claustrum = [p for p in EDITABLE_FIELDS if p.startswith("claustrum.")]
    # `claustrum.binary` is intentionally NOT editable — binary/executable paths stay out of
    # the editor (same boundary as `claude.binary`), so the editor can never repoint an exe.
    assert claustrum == [
        "claustrum.enabled",
        "claustrum.socket_path",
        "claustrum.spawn_timeout_seconds",
        "claustrum.keep_children",
        "claustrum.request_timeout_seconds",
    ]
    assert "claustrum.binary" not in EDITABLE_FIELDS
    assert specs["claustrum.enabled"]["type"] == "bool"
    assert specs["claustrum.enabled"]["section_label"] == "Direct Session (live-view)"
    assert specs["claustrum.enabled"]["depends_on"] is None  # the master switch itself
    # The 4 operational fields all depend on the master toggle (greyed when the channel is off).
    for path in claustrum[1:]:
        assert specs[path]["depends_on"] == "claustrum.enabled"
    assert specs["claustrum.spawn_timeout_seconds"]["unit"] == "seconds"
    assert specs["claustrum.socket_path"]["placeholder"]


def test_field_specs_exposes_notifications_block_with_depends() -> None:
    # #541: the notifications block splits into two channel master switches (outbound
    # `enabled` + `browser_enabled`) plus the per-event `notify_on_*` toggles. Each toggle
    # drives BOTH channels, so it stays editable when EITHER channel is on (depends_on_any).
    specs = field_specs()
    notif = [p for p in EDITABLE_FIELDS if p.startswith("notifications.")]
    assert notif == [
        "notifications.enabled",
        "notifications.browser_enabled",
        "notifications.notify_on_crash",
        "notifications.notify_on_ready",
        "notifications.notify_on_stop",
        "notifications.notify_on_permission",
        "notifications.notify_on_session_end",
        "notifications.notify_on_reconnect_failed",
    ]
    # Both master switches are un-gated; neither depends on the other.
    assert specs["notifications.enabled"]["depends_on"] is None
    assert specs["notifications.browser_enabled"]["depends_on"] is None
    assert specs["notifications.enabled"]["section_label"] == "Notifications"
    # Every per-event toggle is editable when EITHER channel is on — so a browser-only user
    # (browser_enabled=true, enabled=false) can still edit them. No single-master depends_on.
    for path in notif:
        if path.startswith("notifications.notify_on_"):
            assert specs[path]["depends_on"] is None
            assert specs[path]["depends_on_any"] == [
                "notifications.enabled",
                "notifications.browser_enabled",
            ]
    # The new fields are plain booleans.
    assert specs["notifications.notify_on_session_end"]["type"] == "bool"


def test_notify_event_toggles_editable_for_browser_only_user() -> None:
    # #636 (P2): a browser-only user (browser_enabled=true, enabled=false) must still be able
    # to edit the per-event toggles — the browser channel reads them. depends_on_any lists
    # BOTH masters so the JS configFieldDisabled greys the toggle out only when NEITHER is on.
    specs = field_specs()
    spec = specs["notifications.notify_on_crash"]
    masters = spec["depends_on_any"]
    assert masters == ["notifications.enabled", "notifications.browser_enabled"]

    # Mirror the JS gate (configFieldDisabled): disabled only when NO master is on.
    def disabled(edits: dict[str, bool]) -> bool:
        return not any(edits.get(m) for m in masters)

    assert disabled({"notifications.enabled": False, "notifications.browser_enabled": False})
    assert not disabled({"notifications.enabled": False, "notifications.browser_enabled": True})
    assert not disabled({"notifications.enabled": True, "notifications.browser_enabled": False})
    assert not disabled({"notifications.enabled": True, "notifications.browser_enabled": True})


def test_validate_edits_accepts_notification_event_toggles(write_config) -> None:
    # #541: the new per-event toggles + the browser-channel switch round-trip through
    # the Tier-A allowlist and re-validation.
    raw = _raw(write_config)
    candidate = validate_edits(
        raw,
        {
            "notifications.enabled": True,
            "notifications.browser_enabled": True,
            "notifications.notify_on_ready": True,
            "notifications.notify_on_reconnect_failed": True,
        },
    )
    assert candidate["notifications"]["browser_enabled"] is True
    assert candidate["notifications"]["notify_on_ready"] is True
    assert candidate["notifications"]["notify_on_reconnect_failed"] is True


def test_validate_edits_accepts_enabling_claustrum(write_config) -> None:
    # The reported papercut (#539): claustrum.enabled now flips from the editor (Tier-A write),
    # and an out-of-range operational value still fails closed.
    raw = _raw(write_config)
    candidate = validate_edits(
        raw, {"claustrum.enabled": True, "claustrum.spawn_timeout_seconds": 15}
    )
    assert candidate["claustrum"]["enabled"] is True
    assert candidate["claustrum"]["spawn_timeout_seconds"] == 15
    with pytest.raises(ConfigValidationError):
        validate_edits(raw, {"claustrum.spawn_timeout_seconds": -5})  # gt=0


def test_depends_maps_are_disjoint() -> None:
    # #548: FIELD_DEPENDS (boolean master) and FIELD_DEPENDS_VALUE (value-gated master) must stay
    # disjoint. A path in both would emit a contradictory spec (a boolean master AND a required
    # value); the frontend would then apply value-equality against a boolean and disable the field
    # forever with no error. Pin the invariant so a future addition to both fails loudly here.
    # (FIELD_DEPENDS_VALUE is currently empty post-#586, so this holds vacuously — the guard
    # stays to catch a future collision if value-gated fields return.)
    from clauster.config_editor import FIELD_DEPENDS, FIELD_DEPENDS_VALUE

    assert set(FIELD_DEPENDS) & set(FIELD_DEPENDS_VALUE) == set()


def test_field_specs_recap_is_always_editable() -> None:
    # #586: launch mode is chosen PER-SPAWN, so the recap toggle must NOT be locked to the
    # config's default launch mode (the #548 launch_mode value-gate was removed). Recap is now
    # always editable, with an informational "standard bridges only" note instead of a lock.
    specs = field_specs()
    recap = specs["claude.resume_recap"]
    assert recap["depends_on"] is None
    assert recap["depends_on_value"] is None
    assert "standard" in recap["description"].lower()
    # Boolean-gated fields still carry no value (None) — the frontend uses the falsy check.
    assert specs["metrics.normalize_cpu"]["depends_on_value"] is None


def test_field_specs_marks_deprecated_show_cost() -> None:
    # #548: the deprecated alias is flagged in the API and gets a plain-text UI description
    # instead of leaking the raw Pydantic "**Deprecated**" markdown docstring.
    specs = field_specs()
    show_cost = specs["usage.show_cost"]
    assert show_cost["deprecated"] is True
    assert "**" not in show_cost["description"]  # no raw markdown surfaced
    assert "usage.mode" in show_cost["description"]
    # A normal field is not flagged deprecated.
    assert specs["usage.mode"]["deprecated"] is False


def test_field_specs_hides_absent_deprecated_key() -> None:
    # #656: once the deprecated key is removed from disk, its row must drop. With a `present`
    # set that omits usage.show_cost, the deprecated field is flagged hidden (still deprecated).
    specs = field_specs(present={"usage.mode"})
    assert specs["usage.show_cost"]["hidden"] is True
    assert specs["usage.show_cost"]["deprecated"] is True  # still classified deprecated
    # A non-deprecated absent field is never hidden — hiding is for deprecated keys only.
    assert specs["usage.fx_rate"]["hidden"] is False


def test_field_specs_keeps_present_deprecated_key_visible() -> None:
    # #656: while the deprecated key IS on disk, keep showing the flagged row.
    specs = field_specs(present={"usage.show_cost"})
    assert specs["usage.show_cost"]["hidden"] is False
    assert specs["usage.show_cost"]["deprecated"] is True


def test_field_specs_none_present_hides_nothing() -> None:
    # #656: an unreadable file (present=None) fails open — never drop a field we can't prove gone.
    specs = field_specs()
    assert specs["usage.show_cost"]["hidden"] is False


def test_disk_state_reports_literal_keys(write_config) -> None:
    # #656: disk_state reads the RAW YAML in one pass, so a key only filled by a schema
    # default (not written) is absent, while a literally-written key is present. The hash
    # is computed from the same bytes.
    path = write_config("usage:\n  show_cost: false\n")
    content_hash, present = disk_state(path)
    assert content_hash is not None
    assert present is not None
    assert "usage.show_cost" in present
    assert "usage.fx_rate" not in present  # schema default, not on disk


def test_disk_state_unreadable_returns_none(tmp_path) -> None:
    # #656: a missing file yields (None, None) so the caller drops the hash and falls back to
    # showing every field (fail-open on display).
    assert disk_state(tmp_path / "no-such.yml") == (None, None)


def test_disk_state_non_mapping_keeps_hash_drops_keys(tmp_path) -> None:
    # #656: a file that parses to a non-dict (e.g. a bare scalar) still hashes (bytes are
    # readable) but yields None keys, so the editor shows every field rather than hiding on a
    # malformed file.
    bad = tmp_path / "scalar.yml"
    bad.write_text("just a string\n", encoding="utf-8")
    content_hash, present = disk_state(bad)
    assert content_hash is not None
    assert present is None


def test_field_specs_exposes_log_retention_fields() -> None:
    # #548: the actively-pruning retention knobs were invisible in the editor — now Tier-A.
    specs = field_specs()
    for path in (
        "logs.retention_max_age_days",
        "logs.retention_max_files",
        "logs.retention_max_total_mb",
    ):
        assert path in EDITABLE_FIELDS
        assert specs[path]["type"] == "int"
    assert specs["logs.retention_max_age_days"]["unit"] == "days"
    assert specs["logs.retention_max_total_mb"]["unit"] == "MB"


def test_validate_edits_accepts_log_retention(write_config) -> None:
    # All three retention knobs are operational (no secret) — each round-trips explicitly, and a
    # negative value fails closed on each (ge=0).
    raw = _raw(write_config)
    candidate = validate_edits(
        raw,
        {
            "logs.retention_max_age_days": 7,
            "logs.retention_max_files": 20,
            "logs.retention_max_total_mb": 500,
        },
    )
    assert candidate["logs"]["retention_max_age_days"] == 7
    assert candidate["logs"]["retention_max_files"] == 20
    assert candidate["logs"]["retention_max_total_mb"] == 500
    for field in (
        "logs.retention_max_age_days",
        "logs.retention_max_files",
        "logs.retention_max_total_mb",
    ):
        with pytest.raises(ConfigValidationError):
            validate_edits(raw, {field: -1})  # ge=0 fails closed
