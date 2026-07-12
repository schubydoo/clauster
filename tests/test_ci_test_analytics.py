"""Guard: every per-OS test-results upload cell stays in the CI subset matrix.

``ci.yml`` uploads JUnit results to Codecov Test Analytics once per OS, from one exact
``(os, python)`` gate cell tagged with that OS as a flag. Unlike the *coverage* upload —
which fires from every matrix cell, so its OS flag populates regardless of the subset —
the test-results upload keys on a single cell per OS. So if the internal-PR subset ever
drops that cell (e.g. swapping the Windows leg from 3.14 to 3.12), the OS's Test-Analytics
flag silently empties with no CI failure. This test turns that drift into a red X. It reads
config only — no coverage impact, and it runs on every matrix cell.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_CI = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
# `runner.os` (used in the upload step's `if`) → the `matrix.os` value in the subset.
_OS_BY_RUNNER = {"Linux": "ubuntu-latest", "macOS": "macos-latest", "Windows": "windows-latest"}


def _subset_cells() -> set[tuple[str, str]]:
    """The ``(os, python)`` cells in the internal-PR subset matrix (parsed from ci.yml)."""
    m = re.search(r"subset='(\{.*?\})'", _CI, re.DOTALL)
    assert m, "could not find the `subset='{...}'` matrix in ci.yml"
    return {(c["os"], c["python"]) for c in json.loads(m.group(1))["include"]}


def _test_results_upload_cells() -> set[tuple[str, str]]:
    """The ``(os, python)`` cells the per-OS test-results upload step keys on."""
    cfg = yaml.safe_load(_CI)
    step = next(
        s
        for s in cfg["jobs"]["tests"]["steps"]
        if s.get("name") == "Upload test results to Codecov"
    )
    pairs = re.findall(r"runner\.os == '(\w+)' && matrix\.python == '([\d.]+)'", step["if"])
    return {(_OS_BY_RUNNER[runner], py) for runner, py in pairs}


def test_test_results_upload_cells_are_in_the_ci_subset():
    upload = _test_results_upload_cells()
    subset = _subset_cells()

    assert upload, "no per-OS test-results upload cells parsed from ci.yml (expression drift?)"

    missing = sorted(upload - subset)
    assert not missing, (
        "These per-OS test-results upload cells are not in the CI subset matrix, so their "
        "Codecov Test-Analytics flag would silently empty on internal PRs — add them back to "
        f"the subset or repoint the upload step's `if`: {missing}"
    )

    # Exactly one upload cell per OS — the whole point (no Python-version double-counting).
    oses = sorted(os_ for os_, _ in upload)
    assert oses == ["macos-latest", "ubuntu-latest", "windows-latest"], (
        f"expected exactly one test-results upload cell per OS, got: {sorted(upload)}"
    )
