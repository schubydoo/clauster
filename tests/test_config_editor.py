"""Unit tests for the safe-allowlist config-edit core (FE-3, #299)."""

from __future__ import annotations

import pytest

from clauster.config import load_config
from clauster.config_editor import (
    EDITABLE_FIELDS,
    ConfigValidationError,
    DisallowedFieldError,
    editable_values,
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
    # Every returned key is in the allowlist; a known Tier-A value round-trips.
    assert set(vals) <= set(EDITABLE_FIELDS)
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


def test_file_hash_changes_with_content(write_config) -> None:
    path = write_config("usage:\n  fx_rate: 1.0\n")
    h1 = file_hash(path)
    path.write_text(path.read_text(encoding="utf-8") + "# touched\n", encoding="utf-8")
    assert file_hash(path) != h1
