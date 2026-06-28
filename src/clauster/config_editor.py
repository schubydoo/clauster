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

import annotated_types as at
from pydantic import ValidationError
from yaml import YAMLError

from .config import ClausterConfig, load_config

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
    "claude.launch_mode",
    "claude.pty_screen_enabled",
    "instance_defaults.spawn_mode",
    "instance_defaults.permission_mode",
    "instance_defaults.verbose",
    "instance_defaults.session_name_prefix",
    "instance_defaults.capacity",
    "instance_defaults.max_bridges",
    "claustrum.enabled",
    "claustrum.socket_path",
    "claustrum.spawn_timeout_seconds",
    "claustrum.keep_children",
    "claustrum.request_timeout_seconds",
    "logs.bridge_log_max_size_mb",
    "logs.keep_rotated",
    "logs.redact_session_url",
    "logs.strip_ansi_in_stream",
    "logs.retention_max_age_days",
    "logs.retention_max_files",
    "logs.retention_max_total_mb",
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
    "notifications.browser_enabled",
    "notifications.notify_on_crash",
    "notifications.notify_on_ready",
    "notifications.notify_on_stop",
    "notifications.notify_on_permission",
    "notifications.notify_on_session_end",
    "notifications.notify_on_reconnect_failed",
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


def editable_values_on_disk(path: str | Path) -> dict[str, Any] | None:
    """Re-read the Tier-A field values from the on-disk config, or ``None`` if unreadable.

    The editor edits the FILE, but a save deliberately does not live-reload the
    running config (a hot-swap is unsafe — the runner and other readers hold the
    startup object). Serving the in-memory config would therefore show STALE values
    after any save until a restart, making a successful save look reverted. Reading
    the file keeps the returned fields consistent with the content ``hash`` (both
    from disk) and reflects what the next restart will load. Returns ``None`` on a
    missing / unparseable / schema-invalid file (e.g. a concurrent external edit) so
    the caller can fall back to the in-memory config instead of failing the request.
    """
    try:
        return editable_values(load_config(path))
    except (OSError, ValueError, ValidationError, YAMLError):
        return None


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


# Human section names (raw key -> heading), in display order.
SECTION_LABELS: dict[str, str] = {
    "claude": "Claude",
    "instance_defaults": "Instance defaults",
    "claustrum": "Direct Session (live-view)",
    "logs": "Logs",
    "reaper": "Reaper",
    "usage": "Usage",
    "metrics": "Metrics",
    "observability": "Observability",
    "notifications": "Notifications",
}

# Human field labels (the raw key is still shown as subtext for cross-reference).
FIELD_LABELS: dict[str, str] = {
    "claude.min_version": "Minimum Claude version",
    "claude.agents_json_poll_interval_seconds": "Liveness poll interval",
    "claude.startup_grace_seconds": "Startup grace period",
    "claude.auto_enable_remote_control": "Auto-acknowledge remote control",
    "claude.resume_recap": "Recap prior transcript on restart",
    "claude.resume_recap_max_chars": "Recap size limit",
    "claude.launch_mode": "Launch mode for new bridges",
    "claude.pty_screen_enabled": "Live Interactive Session terminal view",
    "instance_defaults.spawn_mode": "Where new sessions run",
    "instance_defaults.permission_mode": "Default permission mode",
    "instance_defaults.verbose": "Verbose bridge logging",
    "instance_defaults.session_name_prefix": "Session name prefix",
    "instance_defaults.capacity": "Sessions per Server Mode bridge",
    "instance_defaults.max_bridges": "Max concurrent bridges",
    "claustrum.enabled": "Enable Direct Session live-view channel",
    "claustrum.socket_path": "Daemon socket path",
    "claustrum.spawn_timeout_seconds": "Daemon spawn timeout",
    "claustrum.keep_children": "Keep sessions on daemon restart",
    "claustrum.request_timeout_seconds": "Daemon request timeout",
    "logs.bridge_log_max_size_mb": "Per-bridge log rotation size",
    "logs.keep_rotated": "Rotated logs to keep",
    "logs.redact_session_url": "Redact session URL in logs",
    "logs.strip_ansi_in_stream": "Strip ANSI colours in stream",
    "logs.retention_max_age_days": "Bridge-log retention (max age)",
    "logs.retention_max_files": "Bridge-log retention (max sets)",
    "logs.retention_max_total_mb": "Bridge-log retention (max total size)",
    "reaper.ui_enabled": "Show ghost-environment reaper",
    "usage.mode": "Usage badge mode",
    "usage.currency": "Currency code",
    "usage.currency_symbol": "Currency symbol",
    "usage.fx_rate": "Currency conversion rate",
    "usage.token_total_includes_cache": "Count cache tokens in totals",
    "usage.show_cost": "Show cost (deprecated)",
    "metrics.enabled": "Enable metrics line",
    "metrics.normalize_cpu": "Normalize CPU to host cores",
    "metrics.show_disk": "Show disk read/write rate",
    "metrics.sample_interval_seconds": "Metrics sampling window",
    "metrics.poll_seconds": "Metrics refresh interval",
    "observability.prometheus_enabled": "Enable /metrics endpoint",
    "notifications.enabled": "Enable outbound notifications",
    "notifications.browser_enabled": "Enable browser notifications",
    "notifications.notify_on_crash": "Notify on unexpected crash",
    "notifications.notify_on_ready": "Notify when a bridge is ready",
    "notifications.notify_on_stop": "Notify when a bridge is stopped",
    "notifications.notify_on_permission": "Notify when permission is needed",
    "notifications.notify_on_session_end": "Notify when a session ends",
    "notifications.notify_on_reconnect_failed": "Notify when reconnect fails",
}

# Unit affix shown beside numeric controls.
FIELD_UNITS: dict[str, str] = {
    "claude.agents_json_poll_interval_seconds": "seconds",
    "claude.startup_grace_seconds": "seconds",
    "claude.resume_recap_max_chars": "characters",
    "claustrum.spawn_timeout_seconds": "seconds",
    "claustrum.request_timeout_seconds": "seconds",
    "logs.bridge_log_max_size_mb": "MB",
    "logs.retention_max_age_days": "days",
    "logs.retention_max_total_mb": "MB",
    "metrics.sample_interval_seconds": "seconds",
    "metrics.poll_seconds": "seconds",
}

# Placeholder copy for optional fields that read as blank when unset.
FIELD_PLACEHOLDERS: dict[str, str] = {
    "instance_defaults.session_name_prefix": "Unset — uses a generated name",
    "instance_defaults.max_bridges": "Unset — no limit",
    "claustrum.socket_path": "Unset — defaults to <state_dir>/claustrum/daemon.sock",
    "usage.currency_symbol": "Unset — defaults to $",
}

# Child field -> master switch: the child is disabled in the UI when the master is off.
FIELD_DEPENDS: dict[str, str] = {
    "claude.resume_recap_max_chars": "claude.resume_recap",
    "claustrum.socket_path": "claustrum.enabled",
    "claustrum.spawn_timeout_seconds": "claustrum.enabled",
    "claustrum.keep_children": "claustrum.enabled",
    "claustrum.request_timeout_seconds": "claustrum.enabled",
    "metrics.normalize_cpu": "metrics.enabled",
    "metrics.show_disk": "metrics.enabled",
    "metrics.sample_interval_seconds": "metrics.enabled",
    "metrics.poll_seconds": "metrics.enabled",
}

# Child field -> masters where the child is editable if ANY master is on. The per-event
# notify_on_* toggles drive BOTH the outbound (Apprise) and browser channels, so they must
# stay editable when EITHER channel is on — gating them on the outbound switch alone greyed
# them out for a browser-only user (browser_enabled=true, enabled=false) even though the
# browser channel reads them.
_NOTIFY_CHANNELS = ("notifications.enabled", "notifications.browser_enabled")
FIELD_DEPENDS_ANY: dict[str, tuple[str, ...]] = {
    "notifications.notify_on_crash": _NOTIFY_CHANNELS,
    "notifications.notify_on_ready": _NOTIFY_CHANNELS,
    "notifications.notify_on_stop": _NOTIFY_CHANNELS,
    "notifications.notify_on_permission": _NOTIFY_CHANNELS,
    "notifications.notify_on_session_end": _NOTIFY_CHANNELS,
    "notifications.notify_on_reconnect_failed": _NOTIFY_CHANNELS,
}

# Child -> (master field, the master VALUE that enables the child). Unlike FIELD_DEPENDS
# (boolean master: the child is disabled when the master is falsy), the child here is disabled
# unless the master EQUALS this value — for genuinely enum-gated fields.
#
# Currently EMPTY. The only entry (recap value-gated on launch_mode == "standard", added in
# #548) was removed in #586: launch mode is chosen PER-SPAWN, so locking the recap toggle to
# the config *default* launch mode was wrong (and rendered stale-disabled on first load). Recap
# is now always editable with an informational note instead (see FIELD_DESCRIPTIONS). The
# mechanism is kept for any future field that is genuinely gated on a specific master value.
FIELD_DEPENDS_VALUE: dict[str, tuple[str, str]] = {}

# Fields kept only for back-compat — the API marks them deprecated (and the UI points at the
# replacement) instead of surfacing the raw Pydantic deprecation docstring.
DEPRECATED_FIELDS: frozenset[str] = frozenset({"usage.show_cost"})

# Plain-text UI description overrides, keyed by dotted path. Used where the model's own
# description is raw markdown unsuitable for the panel (e.g. a deprecation note).
FIELD_DESCRIPTIONS: dict[str, str] = {
    "claude.resume_recap": (
        "Applies to Server Mode (standard) bridges only — Interactive Session (pty) bridges "
        "resume natively (via --continue), so recap does nothing for a pty launch. Opt-in: "
        "installs a SessionStart hook in the runtime user's ~/.claude/settings.json that recaps "
        "the most recent prior transcript for the cwd into a restarted Server Mode bridge (edits "
        "the user's Claude settings and injects prior turns)."
    ),
    "usage.show_cost": (
        "Deprecated. Use “Usage badge mode” → Off to hide the badge; usage.mode now takes "
        "precedence and show_cost only applies when mode is unset."
    ),
    "instance_defaults.verbose": (
        "Applies to Server Mode (standard) `claude remote-control` bridges only (every spawn "
        "mode — same-dir/worktree/session); the Interactive Session (pty) bridge runs under a "
        "PTY keeper and is never passed --verbose. Adds --verbose to the spawned "
        "`claude remote-control` process so "
        "it logs detailed connection/session events (useful for diagnosing intermittent "
        "bridge disconnects). Takes effect on the next bridge start."
    ),
    "claude.pty_screen_enabled": (
        "Applies to Interactive Session (pty / true-resume) bridges only — Server Mode "
        "(standard) bridges have no PTY to render. "
        "Publishes a redacted, read-only render of the bridge's live terminal for the dashboard's "
        "live-terminal view. Needs the optional pyte dependency (pip install 'clauster[pty]'); "
        "without it the feature stays dormant. The render is best-effort secret-redacted, so "
        "treat the live view as auth-gated, not secret-proof. Takes effect on the next start."
    ),
}

# Human labels for enum option VALUES (the saved value is unchanged). Lets the editor show the
# same friendly wording as the "Run Claude here" launch dropdown instead of bare enum tokens.
# Keyed by field path -> {value: label}.
FIELD_CHOICE_LABELS: dict[str, dict[str, str]] = {
    "instance_defaults.permission_mode": {
        "default": "Ask each time (default)",
        "plan": "Plan only (read-only)",
        "acceptEdits": "Auto-accept edits",
        "auto": "Auto-approve safe",
        "dontAsk": "Never prompt — deny unknowns",
        "bypassPermissions": "Skip all checks ⚠",
    },
    "claude.launch_mode": {
        "standard": "Server Mode (multi-session bridge)",
        "pty": "Interactive Session (single-session, true-resume)",
    },
    "instance_defaults.spawn_mode": {
        "same-dir": "Same directory",
        "worktree": "Git worktree",
        "session": "Fresh session",
    },
    "usage.mode": {
        "cost": "Cost ($)",
        "tokens": "Tokens",
        "off": "Off",
    },
}


def _humanize(key: str) -> str:
    """Fallback label for a raw field key (``foo_bar`` -> ``Foo bar``)."""
    return key.replace("_", " ").capitalize()


def _constraints(info: Any) -> dict[str, Any]:
    """Extract numeric min/max bounds from a field's annotated-type metadata."""
    out: dict[str, Any] = {}
    for meta in info.metadata:
        if isinstance(meta, at.Ge):
            out["min"] = meta.ge
        elif isinstance(meta, at.Gt):
            out["min"] = meta.gt
        elif isinstance(meta, at.Le):
            out["max"] = meta.le
        elif isinstance(meta, at.Lt):
            out["max"] = meta.lt
    return out


def field_specs() -> dict[str, dict[str, Any]]:
    """Return rich per-field UI metadata for the editor (label, help, control, bounds)."""
    specs: dict[str, dict[str, Any]] = {}
    for path in EDITABLE_FIELDS:
        section, key = path.split(".", 1) if "." in path else ("", path)
        info = _resolve_field_info(path)
        kind, choices = _classify(info.annotation)
        default = info.default
        dep_value = FIELD_DEPENDS_VALUE.get(path)
        spec: dict[str, Any] = {
            "key": key,
            "section": section,
            "section_label": SECTION_LABELS.get(section, _humanize(section)),
            "label": FIELD_LABELS.get(path, _humanize(key)),
            "type": kind,
            "choices": choices,
            "choice_labels": FIELD_CHOICE_LABELS.get(path),
            "description": FIELD_DESCRIPTIONS.get(path, info.description or ""),
            "unit": FIELD_UNITS.get(path),
            "placeholder": FIELD_PLACEHOLDERS.get(path),
            "depends_on": FIELD_DEPENDS.get(path) or (dep_value[0] if dep_value else None),
            "depends_on_value": dep_value[1] if dep_value else None,
            "depends_on_any": list(FIELD_DEPENDS_ANY.get(path, ())) or None,
            "deprecated": path in DEPRECATED_FIELDS,
            "default": default if isinstance(default, (str, int, float, bool)) else None,
        }
        if kind in ("int", "float"):
            spec.update(_constraints(info))
            spec["step"] = 1 if kind == "int" else "any"
        specs[path] = spec
    return specs
