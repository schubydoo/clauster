"""Config-deprecation registry + ``clauster config reconcile`` decision logic (#569).

Clauster's config schema is additive-only with back-compat aliases for renamed
keys, so a deprecated key keeps working but warns at every load and lingers in the
operator's ``clauster.yml`` with no clean removal path. This module is the single
source of truth for *which* keys are deprecated and *how* an old value maps to the
replacement, so the load-time alias validators in :mod:`clauster.config` and the
``reconcile`` CLI never drift apart.

The decision logic is split from I/O: :func:`scan_config_file` reads the file and
:func:`build_plan` turns scan findings into a removal/edit plan via an injectable
``decide`` callback, so the interactive prompt is testable without a TTY. The
rewrite itself reuses the existing atomic backup + ruamel round-trip writer
(:func:`clauster.config_writer.write_edits`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import yaml

# --- value transforms (THE single source of truth, shared with config.py) ------------
#
# A transform maps an old (deprecated-key) value to the value the replacement key should
# carry, or returns ``None`` to mean "no replacement value — just drop the deprecated key
# and let the replacement keep its default". config.py's per-model alias validators import
# these so the mapping is defined ONCE here, not copied into each validator.


def resume_mode_to_launch_mode(value: str | None) -> str | None:
    """Map a legacy ``claude.resume_mode`` value to ``claude.launch_mode`` (identity).

    The rename (#540) was cosmetic — the accepted values (``standard`` / ``pty``) are
    unchanged, so the old value carries over verbatim.
    """
    return value


def show_cost_to_mode(value: bool | None) -> str | None:
    """Map a legacy ``usage.show_cost`` value to ``usage.mode``.

    ``show_cost: false`` hid the badge, which is now ``mode: "off"``. ``show_cost: true``
    was the historical default (badge shown), which maps to no explicit replacement —
    returning ``None`` drops the key and lets ``usage.mode`` keep its default.
    """
    return "off" if value is False else None


@dataclass(frozen=True)
class Deprecation:
    """One deprecated config key, its replacement, and the old→new value transform.

    ``transform`` maps the deprecated key's value to the replacement key's value, or
    returns ``None`` to signal "drop the deprecated key with no replacement value"
    (the replacement keeps its default). ``choices`` lists the values an operator may
    pick for the replacement when reconciling interactively.
    """

    deprecated_key: str
    replacement_key: str
    transform: Callable[[Any], Any]
    explain: str
    choices: tuple[str, ...] = ()


# THE deprecation registry. Add an entry here when a key is renamed with a back-compat
# alias; both this CLI and the load-time validators read from this one list.
DEPRECATIONS: tuple[Deprecation, ...] = (
    Deprecation(
        deprecated_key="claude.resume_mode",
        replacement_key="claude.launch_mode",
        transform=resume_mode_to_launch_mode,
        explain=(
            "`claude.resume_mode` was renamed to `claude.launch_mode` (#540); the old "
            "name read like a resume on/off toggle. The accepted values are unchanged."
        ),
        choices=("standard", "pty"),
    ),
    Deprecation(
        deprecated_key="usage.show_cost",
        replacement_key="usage.mode",
        transform=show_cost_to_mode,
        explain=(
            "`usage.show_cost` was superseded by `usage.mode` (#548). `show_cost: false` "
            "hid the badge, which is now `mode: off`; `show_cost: true` was the default "
            "(badge shown) and simply drops with no replacement value."
        ),
        choices=("cost", "tokens", "off"),
    ),
)


@dataclass(frozen=True)
class Finding:
    """A deprecated key found present in the config file, plus its proposed mapping.

    ``proposed_value`` is the transform's output; ``has_replacement`` is ``False`` when
    no value should be written to the replacement key — either because the transform
    returned ``None`` (drop-only) or because the replacement key is ALREADY present in
    the config (it already wins at load time, so reconcile only removes the dead alias).
    """

    deprecation: Deprecation
    old_value: Any
    proposed_value: Any
    has_replacement: bool
    # ``True`` when the replacement key is already set explicitly in the config — the
    # deprecated key is then a pure removal (the existing replacement value is kept).
    replacement_present: bool = False


@dataclass
class Plan:
    """The reconcile rewrite plan: keys to remove and replacement edits to apply."""

    removals: list[str] = field(default_factory=list)
    edits: dict[str, Any] = field(default_factory=dict)
    # Findings the operator declined to act on (kept for the summary).
    skipped: list[Finding] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Whether the plan would change nothing (no removals and no edits)."""
        return not self.removals and not self.edits


@dataclass(frozen=True)
class Decision:
    """An operator's choice for one finding: skip it, or apply a chosen value."""

    apply: bool
    value: Any = None
    # ``True`` when ``value`` is meaningful; ``False`` means "remove the key with no
    # replacement value" (used for a drop-only deprecation the operator accepts).
    has_value: bool = False


def _dotted_get(data: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    """Resolve a dotted key against a nested mapping; return ``(present, value)``."""
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def scan_raw(raw: dict[str, Any]) -> list[Finding]:
    """Scan an already-parsed raw config mapping for present deprecated keys."""
    findings: list[Finding] = []
    for dep in DEPRECATIONS:
        present, old_value = _dotted_get(raw, dep.deprecated_key)
        if not present:
            continue
        repl_present, _ = _dotted_get(raw, dep.replacement_key)
        proposed = dep.transform(old_value)
        # Write a replacement value only when the transform yields one AND the
        # replacement key isn't already set (an existing value already wins at load).
        has_replacement = proposed is not None and not repl_present
        findings.append(
            Finding(
                deprecation=dep,
                old_value=old_value,
                proposed_value=proposed,
                has_replacement=has_replacement,
                replacement_present=repl_present,
            )
        )
    return findings


def scan_config_file(path: str) -> list[Finding]:
    """Read ``path`` and return the deprecated keys it contains (empty when clean)."""
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        return []
    return scan_raw(raw)


def build_plan(findings: list[Finding], decide: Callable[[Finding], Decision]) -> Plan:
    """Turn ``findings`` into a :class:`Plan` by asking ``decide`` about each one.

    ``decide`` is injected so the interactive prompt (or ``--yes`` auto-accept) can be
    swapped for a deterministic callback in tests. A declined finding leaves the
    deprecated key in place; an accepted one is always removed, and the replacement
    edit is added only when the decision carries a value.
    """
    plan = Plan()
    for finding in findings:
        decision = decide(finding)
        if not decision.apply:
            plan.skipped.append(finding)
            continue
        plan.removals.append(finding.deprecation.deprecated_key)
        if decision.has_value:
            plan.edits[finding.deprecation.replacement_key] = decision.value
    return plan


def apply_plan(path: str, plan: Plan) -> str:
    """Atomically rewrite ``path`` per ``plan`` via the existing config writer.

    Reuses :func:`clauster.config_writer.write_edits` (backup + ruamel round-trip +
    atomic replace + post-write re-validation) so reconcile shares the one write path.
    Imported lazily to keep this module's import graph light (``config.py`` imports the
    transforms above at load time). Returns the new config-file hash.
    """
    from .config_writer import write_edits

    return write_edits(path, plan.edits, removals=plan.removals)
