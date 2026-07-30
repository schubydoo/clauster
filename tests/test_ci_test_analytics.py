"""Guard: every per-OS test-results upload cell stays in the CI matrix.

``ci.yml`` uploads JUnit results to Codecov Test Analytics once per OS, from one exact
``(os, python)`` gate cell tagged with that OS as a flag. Unlike the *coverage* upload —
which fires from every matrix cell, so its OS flag populates regardless of the subset —
the test-results upload keys on a single cell per OS. So if a matrix variant (the internal-PR
subset, or the full grid) ever drops that cell (e.g. swapping the Windows leg from 3.14 to
3.12), the OS's Test-Analytics flag silently empties with no CI failure. This test turns that
drift into a red X. It reads config only — no coverage impact, and it runs on every matrix cell.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

# Static guard over a .github/** file: also run in the always-on `lint` job, because the
# `tests` matrix is skipped on a `.github/**`-only PR (.github/actions/changed-code).
pytestmark = pytest.mark.repo_meta

_ROOT = Path(__file__).resolve().parents[1]
_CI = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
# `runner.os` (used in the upload step's `if`) → the `matrix.os` value in the matrix.
_OS_BY_RUNNER = {"Linux": "ubuntu-latest", "macOS": "macos-latest", "Windows": "windows-latest"}


def _matrix_cell_sets() -> list[set[tuple[str, str]]]:
    """Every ``{"include":[...]}`` matrix blob in ci.yml (full + subset), as (os, python) sets.

    Matches the JSON object structure, not the bash ``subset='...'`` assignment, so it stays
    robust to how the matrix is written (quote style, heredoc, helper command, variable name);
    only a wholesale move away from a JSON literal would need this guard updated.
    """
    blobs = re.findall(r'\{"include":\s*\[.*?\]\}', _CI, re.DOTALL)
    return [{(c["os"], c["python"]) for c in json.loads(b)["include"]} for b in blobs]


def _test_results_upload_cells() -> set[tuple[str, str]]:
    """The ``(os, python)`` cells the per-OS test-results upload step keys on (from its `if`)."""
    cfg = yaml.safe_load(_CI)
    step = next(
        s
        for s in cfg["jobs"]["tests"]["steps"]
        if s.get("name") == "Upload test results to Codecov"
    )
    pairs = re.findall(r"runner\.os == '(\w+)' && matrix\.python == '([\d.]+)'", step["if"])
    return {(_OS_BY_RUNNER[runner], py) for runner, py in pairs}


def test_test_results_upload_cells_are_in_every_matrix_variant():
    upload = _test_results_upload_cells()
    variants = _matrix_cell_sets()

    assert upload, "no per-OS test-results upload cells parsed from ci.yml (expression drift?)"
    assert variants, "no `{'include': [...]}` matrix blobs found in ci.yml (structure changed?)"

    # Cells present in EVERY matrix variant (full + subset): the upload fires on internal-PR
    # subset runs AND push/nightly/fork full runs, so its cells must be in both.
    common = set.intersection(*variants)
    missing = sorted(upload - common)
    assert not missing, (
        "These per-OS test-results upload cells are not in every CI matrix variant, so their "
        "Codecov Test-Analytics flag would silently empty on some runs — add them back to the "
        f"subset or repoint the upload step's `if`: {missing}"
    )

    # Exactly one upload cell per OS — the whole point (no Python-version double-counting).
    oses = sorted(os_ for os_, _ in upload)
    assert oses == ["macos-latest", "ubuntu-latest", "windows-latest"], (
        f"expected exactly one test-results upload cell per OS, got: {sorted(upload)}"
    )
