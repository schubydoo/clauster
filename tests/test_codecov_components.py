"""Guard: every source module is assigned to a codecov.yml component.

Codecov's component matcher does NOT expand globs here (an `src/clauster/**` pattern
matches nothing — see the notes in ``codecov.yml``), so components list explicit file
paths. That means a newly-added module is silently *uncounted* in the per-component
coverage breakdown until someone adds it by hand — drift that has bitten repeatedly.

This test turns "remember to update codecov.yml" into an automatic red X: it fails when
the source tree and the listed component paths disagree (an orphaned module, a stale
path whose file is gone, or a path listed under two components). It reads files only —
no coverage impact, and it runs on every matrix cell.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _listed_paths() -> list[str]:
    """Every path listed under ``component_management.individual_components``."""
    cfg = yaml.safe_load((_ROOT / "codecov.yml").read_text(encoding="utf-8"))
    return [
        path
        for comp in cfg["component_management"]["individual_components"]
        for path in comp["paths"]
    ]


def _source_modules() -> set[str]:
    """Every ``src/clauster/**/*.py`` module, as a repo-relative POSIX path."""
    return {p.relative_to(_ROOT).as_posix() for p in (_ROOT / "src" / "clauster").rglob("*.py")}


def test_every_source_module_maps_to_a_codecov_component():
    listed = _listed_paths()
    source = _source_modules()

    orphans = sorted(source - set(listed))
    assert not orphans, (
        "These source modules are in no codecov.yml component (add each to the right "
        f"`individual_components` entry, or the per-component coverage undercounts): {orphans}"
    )

    stale = sorted(set(listed) - source)
    assert not stale, f"codecov.yml lists paths whose files no longer exist: {stale}"

    dupes = sorted({p for p in listed if listed.count(p) > 1})
    assert not dupes, f"codecov.yml lists these paths under more than one component: {dupes}"
