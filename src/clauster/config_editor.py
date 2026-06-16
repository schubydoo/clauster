"""Safe-allowlist config editing for the in-app editor (FE-3, #299).

Only an explicit **Tier-A allowlist** of operational fields is editable from the
web UI; everything security / secret / bind / structural / supply-chain is
excluded and never round-trips to the browser. Edits are re-validated by
constructing :class:`~clauster.config.ClausterConfig` — which trips the existing
auth fail-closed validator — before any write. Design: ``scratch/fe3-config-editor-spike.md``.

This module is **pure** (allowlist + validation); the disk write (backup +
atomic replace + ruamel round-trip) lives in :mod:`clauster.config_writer`.
"""

from __future__ import annotations

import copy
import hashlib
import types
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import ValidationError

from .config import ClausterConfig

# Tier-A allowlist: dotted paths editable from the web UI. Operational only — no
# auth/secret/bind/structural/clone/supply-chain field appears here (those stay
# file/CLI-managed). Keep in sync with scratch/fe3-config-editor-spike.md.
EDITABLE_FIELDS: tuple[str, ...] = (
    "claude.min_version",
    "claude.agents_json_poll_interval_seconds",
    "claude.startup_grace_seconds",
    "claude.auto_enable_remote_control",
    "claude.resume_recap",
    "claude.resume_recap_max_chars",
    "claude.resume_mode",
    "instance_defaults.spawn_mode",
    "instance_defaults.permission_mode",
    "instance_defaults.session_name_prefix",
    "instance_defaults.capacity",
    "instance_defaults.max_bridges",
    "logs.bridge_log_max_size_mb",
    "logs.keep_rotated",
    "logs.redact_session_url",
    "logs.strip_ansi_in_stream",
    "reaper.ui_enabled",
    "usage.mode",
    "usage.currency",
    "usage.currency_symbol",
    "usage.fx_rate",
    "usage.token_total_includes_cache",
    "usage.show_cost",
    "metrics.enabled",
    "metrics.normalize_cpu",
    "metrics.show_disk",
    "metrics.sample_interval_seconds",
    "metrics.poll_seconds",
    "observability.prometheus_enabled",
    "notifications.enabled",
    "notifications.notify_on_crash",
)
_EDITABLE = frozenset(EDITABLE_FIELDS)


class ConfigEditError(Exception):
    """Base for config-edit failures (mapped to a 4xx at the route layer)."""


class DisallowedFieldError(ConfigEditError):
    """A requested key is not in the Tier-A allowlist (fail-closed: reject, never drop)."""


class StaleConfigError(ConfigEditError):
    """The on-disk file changed since the editor loaded it (external-edit guard)."""


class ConfigValidationError(ConfigEditError):
    """The merged config failed re-validation (e.g. it would open the dashboard)."""


def file_hash(path: str | Path) -> str:
    """Return a hex SHA-256 of the config file's bytes (the external-edit token)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _get_by_path(obj: Any, dotted: str) -> Any:
    """Resolve a dotted attribute path against a (pydantic) object."""
    cur = obj
    for part in dotted.split("."):
        cur = getattr(cur, part)
    return cur


def _set_by_path(mapping: dict[str, Any], dotted: str, value: Any) -> None:
    """Set a dotted key into a nested mapping, creating intermediate mappings."""
    parts = dotted.split(".")
    cur = mapping
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def editable_values(config: ClausterConfig) -> dict[str, Any]:
    """Extract the current Tier-A field values, keyed by dotted path.

    Only allowlisted fields are read, so no secret/auth/bind value is ever
    surfaced — redaction is structural, not a post-filter.
    """
    return {path: _get_by_path(config, path) for path in EDITABLE_FIELDS}


def validate_edits(raw: dict[str, Any], edits: dict[str, Any]) -> dict[str, Any]:
    """Merge allowlisted edits onto ``raw`` and re-validate; return the candidate mapping.

    Rejects any non-Tier-A key (:class:`DisallowedFieldError`) before merging, then
    constructs :class:`ClausterConfig` so every field + model validator runs (incl.
    the auth fail-closed check). Raises :class:`ConfigValidationError` on failure —
    nothing is written here.
    """
    disallowed = [k for k in edits if k not in _EDITABLE]
    if disallowed:
        raise DisallowedFieldError(", ".join(sorted(disallowed)))
    candidate = copy.deepcopy(raw)
    for path, value in edits.items():
        _set_by_path(candidate, path, value)
    try:
        ClausterConfig.model_validate(candidate)
    except ValidationError as exc:
        raise ConfigValidationError(str(exc)) from exc
    return candidate


def _resolve_field_info(path: str) -> Any:
    """Resolve the pydantic ``FieldInfo`` for a dotted editable path."""
    model: Any = ClausterConfig
    parts = path.split(".")
    for part in parts[:-1]:
        model = model.model_fields[part].annotation
    return model.model_fields[parts[-1]]


def _classify(annotation: Any) -> tuple[str, list[str] | None]:
    """Map a field annotation to a UI control type + enum choices (if any)."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return _classify(non_none[0])
    if origin is Literal:
        return "enum", [str(a) for a in get_args(annotation)]
    if annotation is bool:  # bool before int (bool is an int subclass)
        return "bool", None
    if annotation is int:
        return "int", None
    if annotation is float:
        return "float", None
    return "str", None


def field_specs() -> dict[str, dict[str, Any]]:
    """Return per-field UI metadata (``type``/``choices``/``description``) for the editor."""
    specs: dict[str, dict[str, Any]] = {}
    for path in EDITABLE_FIELDS:
        info = _resolve_field_info(path)
        kind, choices = _classify(info.annotation)
        specs[path] = {"type": kind, "choices": choices, "description": info.description or ""}
    return specs
