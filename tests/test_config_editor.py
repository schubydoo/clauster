"""Unit tests for the safe-allowlist config-edit core (FE-3, #299)."""

from __future__ import annotations

import types
from typing import Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from clauster.config import ClausterConfig, load_config
from clauster.config_editor import (
    EDITABLE_FIELDS,
    EXCLUDED_FIELDS,
    TIER_B_FIELDS,
    ConfigValidationError,
    DisallowedFieldError,
    disk_state,
    editable_values,
    field_specs,
    file_hash,
    validate_edits,
)


def _nested_model(annotation: object) -> type[BaseModel] | None:
    """Return the nested BaseModel an annotation wraps (incl. Optional), else None.

    Mirrors ``config._nested_model``: a bare BaseModel subclass and an
    ``Optional[BaseModel]`` recurse; dict/list containers do NOT (their args are a
    key/value type, not a section model) — so the same leaves the env-var walk treats
    as unmappable scalars are the leaves this enumerates.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) in (Union, types.UnionType):
        for arg in get_args(annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


def _config_leaf_paths(
    model: type[BaseModel] = ClausterConfig, prefix: tuple[str, ...] = ()
) -> list[str]:
    """Enumerate every dotted leaf-field path in ClausterConfig (nested models recurse).

    Same recursion as ``config._env_leaf_map`` — nested models recurse, dict/list leaves
    stay leaves — so the editor's coverage decision is taken against exactly the
    addressable leaves.
    """
    out: list[str] = []
    for name, field in model.model_fields.items():
        path = (*prefix, name)
        nested = _nested_model(field.annotation)
        if nested is not None:
            out.extend(_config_leaf_paths(nested, path))
        else:
            out.append(".".join(path))
    return out


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


def test_login_shepherd_allow_setup_token_is_excluded(write_config) -> None:
    # #846 second gate: same rationale as login_shepherd.enabled and
    # config_write.allow_user_scope — never web-editable, so a browser session can't
    # grant itself the higher-risk setup-token mode.
    assert "login_shepherd.allow_setup_token" in EXCLUDED_FIELDS
    assert "login_shepherd.allow_setup_token" not in EDITABLE_FIELDS
    raw = _raw(write_config)
    with pytest.raises(DisallowedFieldError):
        validate_edits(raw, {"login_shepherd.allow_setup_token": True})


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


def test_field_dep_status_reports_optional_dep_availability(tmp_path, monkeypatch) -> None:
    # #1016 Part 2: runtime-consistent availability of the dep behind each dep-gated switch.
    from clauster import config_editor, deps

    monkeypatch.setattr("clauster.deps.shutil.which", lambda name: "/usr/local/bin/claustrum")
    monkeypatch.setattr(deps, "probe", lambda entry: entry.key == "apprise")  # pyte absent
    cfg = ClausterConfig(projects_root=tmp_path, state_dir=tmp_path / ".s")
    st = config_editor.field_dep_status(cfg)
    assert set(st) == {"claustrum.enabled", "notifications.enabled", "claude.pty_screen_enabled"}
    assert st["claustrum.enabled"]["available"] is True
    assert st["notifications.enabled"]["available"] is True
    assert st["claude.pty_screen_enabled"]["available"] is False
    assert "clauster deps install claustrum" in st["claustrum.enabled"]["hint"]


def test_field_specs_attaches_dep_status_only_with_config(tmp_path, monkeypatch) -> None:
    # #1016 Part 2: dep_status rides on the gated switches when config is given, not otherwise.
    from clauster import deps

    monkeypatch.setattr("clauster.deps.shutil.which", lambda name: None)  # claustrum missing
    monkeypatch.setattr(deps, "probe", lambda entry: False)  # extras missing
    cfg = ClausterConfig(projects_root=tmp_path, state_dir=tmp_path / ".s")
    specs = field_specs(config=cfg)
    assert specs["claustrum.enabled"]["dep_status"]["available"] is False
    assert "clauster deps install claustrum" in specs["claustrum.enabled"]["dep_status"]["hint"]
    assert "dep_status" not in specs["log_format"]  # a field with no optional dep
    # Without config, no dep_status is computed at all (the metadata-only call path).
    assert "dep_status" not in field_specs()["claustrum.enabled"]


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
    # list[...] -> a rows editor; dict[...] -> a fixed-key map editor (Slice 4).
    assert _classify(list[str]) == ("list", None)
    assert _classify(dict[str, bool]) == ("map", None)
    # _list_item_kind resolves the ELEMENT type, unwrapping a single-member Optional so a
    # `list[str] | None` field reports "str" (not the outer "list"); a bare list -> "str".
    from clauster.config_editor import _list_item_kind

    assert _list_item_kind(list[str]) == "str"
    assert _list_item_kind(list[str] | None) == "str"
    assert _list_item_kind(list) == "str"
    # A multi-member union is NOT unwrapped (len != 1), so it falls through to the first arg.
    assert _list_item_kind(str | int) == "str"
    # Lt maps to max AND exclusive_max; Ge to min alone (it IS inclusive); an unrecognized
    # metadata item is simply skipped (loop tail).
    meta = _types.SimpleNamespace(metadata=[at.Lt(lt=5), at.Ge(ge=1), object()])
    assert _constraints(meta) == {"max": 5, "exclusive_max": 5, "min": 1}


def test_tier_b_list_and_map_specs() -> None:
    # Slice 4: the three non-secret list/map fields are Tier-B with rich specs the rows/checkbox
    # editors consume. The secret url lists + auth trust lists stay OUT of Tier-B.
    from clauster.config_editor import TIER_B_FIELDS

    specs = field_specs(fields=TIER_B_FIELDS)
    assert specs["clone.allowed_schemes"]["type"] == "list"
    assert specs["clone.allowed_schemes"]["item_type"] == "str"
    assert specs["clone.allowed_private_cidrs"]["type"] == "list"
    events = specs["webhooks.events"]
    assert events["type"] == "map"
    assert [mk["key"] for mk in events["map_keys"]] == [
        "spawn",
        "ready",
        "stop",
        "crash",
        "bg-settled",
        "permission-needed",
        "clone-done",
    ]
    assert {mk["key"]: mk["default"] for mk in events["map_keys"]}["crash"] is True
    # The secret / trust lists are never Tier-B (unsafe mask round-trip / trust surface).
    for excluded in ("webhooks.urls", "notifications.urls", "auth.allowed_origins"):
        assert excluded not in TIER_B_FIELDS


def test_map_field_without_registry_entry_omits_map_keys(monkeypatch) -> None:
    # Defensive fallback: a `map`-typed field NOT registered in FIELD_MAP_KEYS gets no
    # `map_keys` (there's no safe checkbox rendering without a known key set) rather than a
    # crash. With the registry emptied, webhooks.events still classifies as a map, just bare.
    from clauster.config_editor import TIER_B_FIELDS

    monkeypatch.setattr("clauster.config_editor.FIELD_MAP_KEYS", {})
    specs = field_specs(fields=TIER_B_FIELDS)
    assert specs["webhooks.events"]["type"] == "map"
    assert "map_keys" not in specs["webhooks.events"]


def test_webhook_event_order_covers_every_known_event() -> None:
    # The editor's ordered taxonomy must stay in lock-step with config.py's known-event set:
    # a NEW webhook event added there with no matching order entry should trip THIS test, not
    # silently drop from (or scramble) the checkbox editor.
    from clauster.config import _WEBHOOK_KNOWN_EVENTS
    from clauster.config_editor import _WEBHOOK_EVENT_ORDER

    assert set(_WEBHOOK_EVENT_ORDER) == _WEBHOOK_KNOWN_EVENTS
    assert len(_WEBHOOK_EVENT_ORDER) == len(_WEBHOOK_KNOWN_EVENTS)  # no dupes


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


def test_log_format_is_editable_enum_with_general_section() -> None:
    # #660 PR1: log_format is the one gap-safe add — a cosmetic operational enum with no
    # security/bind/secret implication. Being top-level (no section prefix) it groups under the
    # synthetic "General" heading (SECTION_LABELS[""]), and carries friendly choice labels.
    assert "log_format" in EDITABLE_FIELDS
    spec = field_specs()["log_format"]
    assert spec["type"] == "enum"
    assert spec["choices"] == ["text", "json"]
    assert spec["section"] == ""
    assert spec["section_label"] == "General"
    assert spec["label"] == "Application log format"
    assert spec["choice_labels"] == {
        "text": "Human text (single line)",
        "json": "Structured JSON",
    }
    # The restart note is surfaced (logging is configured once at startup).
    assert "restart" in spec["description"].lower()


def test_validate_edits_accepts_log_format(write_config) -> None:
    # #660 PR1: log_format round-trips through the Tier-A allowlist + re-validation, and an
    # off-Literal value fails closed (the Literal["text","json"] gate trips on re-validate).
    raw = _raw(write_config)
    candidate = validate_edits(raw, {"log_format": "json"})
    assert candidate["log_format"] == "json"
    with pytest.raises(ConfigValidationError):
        validate_edits(raw, {"log_format": "yaml"})  # not a Literal member


def test_every_config_leaf_is_classified_editable_or_excluded() -> None:
    # #660 coverage guard: EVERY leaf field in ClausterConfig must be a deliberate editor
    # decision — either Tier-A editable OR in the intentionally-excluded registry. A new
    # config.py field with no decision lands in neither and FAILS here, forcing the dev to
    # classify it (add to EDITABLE_FIELDS, or to EXCLUDED_FIELDS with a reason). This is the
    # durable record that keeps the editor's surface and the config schema from drifting apart.
    leaves = set(_config_leaf_paths())
    editable = set(EDITABLE_FIELDS)
    tier_b = set(TIER_B_FIELDS)
    excluded = set(EXCLUDED_FIELDS)
    unclassified = leaves - editable - tier_b - excluded
    assert not unclassified, (
        f"Config leaf field(s) {sorted(unclassified)} are in none of EDITABLE_FIELDS, "
        "TIER_B_FIELDS, or EXCLUDED_FIELDS in config_editor.py. Classify each: add it to "
        "EDITABLE_FIELDS (Tier-A operational scalar), TIER_B_FIELDS (Advanced — behind the "
        "config_write capability + step-up re-auth; only GAP-SENSITIVE clone/webhook-class "
        "scalars, never config_write.*/login_shepherd.* or a secret/bind/auth/binary/"
        "structural field), OR EXCLUDED_FIELDS with a one-line reason. When unsure, EXCLUDE "
        "it (fail closed)."
    )


def test_editable_and_excluded_are_disjoint() -> None:
    # #660: a field cannot be in two classifications — a path in more than one is a
    # contradiction (the guard above would count it as covered while the editor surfaces it,
    # masking a stale entry). Pin pairwise disjointness so a copy/paste slip fails loudly.
    editable, tier_b, excluded = set(EDITABLE_FIELDS), set(TIER_B_FIELDS), set(EXCLUDED_FIELDS)
    assert not (editable & excluded), (
        f"field(s) in BOTH EDITABLE_FIELDS and EXCLUDED_FIELDS: {sorted(editable & excluded)}"
    )
    assert not (editable & tier_b), (
        f"field(s) in BOTH EDITABLE_FIELDS and TIER_B_FIELDS: {sorted(editable & tier_b)}"
    )
    assert not (tier_b & excluded), (
        f"field(s) in BOTH TIER_B_FIELDS and EXCLUDED_FIELDS: {sorted(tier_b & excluded)}"
    )


def test_editable_and_excluded_reference_only_real_leaves() -> None:
    # #660: neither set may carry a stale entry (a renamed/removed config field). Every dotted
    # path in EDITABLE_FIELDS and EXCLUDED_FIELDS must resolve to a real ClausterConfig leaf, so
    # a config.py rename that orphans an entry fails here instead of silently mis-classifying.
    leaves = set(_config_leaf_paths())
    stale_editable = set(EDITABLE_FIELDS) - leaves
    stale_tier_b = set(TIER_B_FIELDS) - leaves
    stale_excluded = set(EXCLUDED_FIELDS) - leaves
    assert not stale_editable, (
        f"EDITABLE_FIELDS names non-existent leaf(s): {sorted(stale_editable)}"
    )
    assert not stale_tier_b, f"TIER_B_FIELDS names non-existent leaf(s): {sorted(stale_tier_b)}"
    assert not stale_excluded, (
        f"EXCLUDED_FIELDS names non-existent leaf(s): {sorted(stale_excluded)}"
    )


def test_validation_error_message_is_operator_friendly(write_config) -> None:
    """A bad value yields a per-field message, not the raw pydantic dump (#1034).

    The dashboard banner renders this string verbatim, so it must name the field
    and the reason — and must NOT leak the internal model name, pydantic's type
    internals, or the errors.pydantic.dev URL.
    """
    raw = _raw(write_config)
    with pytest.raises(ConfigValidationError) as ei:
        validate_edits(
            raw,
            {"clone.allowed_private_cidrs": ["999.999.0.0/33"]},
            allowed=frozenset({"clone.allowed_private_cidrs"}),
        )
    msg = str(ei.value)
    assert "clone.allowed_private_cidrs" in msg
    assert "999.999.0.0/33" in msg
    for leaked in ("ClausterConfig", "pydantic.dev", "input_type", "[type="):
        assert leaked not in msg


def test_exclusive_bounds_are_emitted_distinctly() -> None:
    # `<input type=number>`'s min/max are INCLUSIVE by definition, so a `gt=0` field
    # advertised min="0", the browser called 0 valid, and the save came back 422 — the
    # control accepting exactly the value the server rejects. The distinct key is what lets
    # the client tell "at least 0" from "more than 0".
    import types as _types

    import annotated_types as at

    from clauster.config_editor import _constraints

    gt = _types.SimpleNamespace(metadata=[at.Gt(gt=0)])
    assert _constraints(gt) == {"min": 0, "exclusive_min": 0}
    # Inclusive bounds must NOT gain the key, or every control would reject its own endpoint.
    ge = _types.SimpleNamespace(metadata=[at.Ge(ge=0)])
    assert _constraints(ge) == {"min": 0}
    le = _types.SimpleNamespace(metadata=[at.Le(le=2.0)])
    assert _constraints(le) == {"max": 2.0}


def test_exclusive_bound_fields_carry_the_key_in_their_spec() -> None:
    # End-to-end through the real model, not a hand-built namespace: the field the finding
    # named must actually reach the dashboard with the key, and an inclusive neighbour must
    # not. `startup_grace_seconds` is gt=0; `capacity` is ge=1.
    from clauster.config_editor import field_specs

    specs = field_specs()
    assert specs["claude.startup_grace_seconds"]["exclusive_min"] == 0
    assert "exclusive_min" not in specs["instance_defaults.capacity"]
    # min stays alongside it — dropping it would leave the browser with no range at all.
    assert specs["claude.startup_grace_seconds"]["min"] == 0


def test_the_exclusive_endpoint_really_is_rejected_by_the_model() -> None:
    # The differential that makes the rest meaningful: 0 must genuinely fail validation for
    # a gt=0 field, or the control was right to offer it and there is nothing to fix.
    import pytest as _pytest
    from pydantic import ValidationError

    from clauster.config import ClaudeConfig

    with _pytest.raises(ValidationError):
        ClaudeConfig(startup_grace_seconds=0)
    assert ClaudeConfig(startup_grace_seconds=0.5).startup_grace_seconds == 0.5


def test_both_save_paths_gate_on_the_exclusive_bound() -> None:
    # There is no JS test harness for the dashboard, so this pins the wiring the only way
    # available: BOTH save paths must consult the guard. The Tier-A editor and the Tier-B
    # advanced panel are separate functions hitting separate endpoints, and fixing one and
    # forgetting the other is exactly how this class of bug survives a review.
    from pathlib import Path as _Path

    import clauster

    script = (_Path(clauster.__file__).parent / "templates" / "_dashboard_script.html").read_text()
    assert script.count("_exclusiveBoundError(") == 3  # 1 definition + 2 call sites
    assert "this._exclusiveBoundError(c.specs, edits)" in script  # Tier-A editor
    assert "this._exclusiveBoundError(a.specs, edits)" in script  # Tier-B advanced panel
