"""``scripts/gen_fuzzer_stats.py`` rebuilds per-target fuzzer_stats JSON.

The weekly coverage cron feeds it the base-runner's ``coverage_d_<target>`` data
files and it writes one ``<target>.json`` per harness in the llvm-cov shape
cifuzz reads for code-change pruning. These tests run it as a subprocess (matching
``test_bump_packaging.py``) against synthetic coverage data, asserting the shape,
the path rewrite cifuzz needs, per-target distinctness, and the fail-closed paths.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# coverage is a dev dependency (via pytest-cov). The musl smoke job runs `uv sync --no-dev`
# (atheris has no musl wheel), so skip this whole module there rather than fail collection.
CoverageData = pytest.importorskip("coverage").CoverageData

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = "scripts/gen_fuzzer_stats.py"

# The base-runner records a covered clauster module under the interpreter's non-editable
# install, with the report tree's /pythoncovmergedfiles/medio/medio prefix in front.
SITE = "/pythoncovmergedfiles/medio/medio/usr/local/lib/python3.11/site-packages/clauster"
# A third-party dependency the harness happens to execute; cifuzz filters it out.
THIRD_PARTY = "/pythoncovmergedfiles/medio/medio/usr/local/lib/python3.11/site-packages/anyio/x.py"


def _write_cov(path: Path, lines: dict[str, set[int]]) -> None:
    """Write a coverage.py data file at ``path`` recording ``lines`` per file."""
    data = CoverageData(basename=str(path))
    data.add_lines(lines)
    data.write()


def _run(coverage_dir: Path, out_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the generator over ``coverage_dir`` into ``out_dir``."""
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / SCRIPT),
            "--coverage-dir",
            str(coverage_dir),
            "--out-dir",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _covered(out: Path, target: str) -> set[str]:
    """Return the covered filenames written for ``target``."""
    stats = json.loads((out / f"{target}.json").read_text())
    return {f["filename"] for f in stats["data"][0]["files"]}


def test_rewrites_site_packages_path_to_checkout_path(tmp_path: Path) -> None:
    """A covered clauster module maps to the /src/clauster path cifuzz filters on."""
    cov = tmp_path / "build-out"
    cov.mkdir()
    _write_cov(
        cov / "coverage_d_redact_fuzzer",
        {
            f"{SITE}/redact.py": {1, 2, 3},
            # A third-party dep in the same data file must be dropped, not emitted.
            THIRD_PARTY: {9},
        },
    )
    out = tmp_path / "fuzzer_stats"

    result = _run(cov, out)
    assert result.returncode == 0, result.stderr

    stats = json.loads((out / "redact_fuzzer.json").read_text())
    files = stats["data"][0]["files"]
    assert len(files) == 1
    entry = files[0]
    # cifuzz keeps files under /src/clauster and strips it to compare with git diff.
    assert entry["filename"] == "/src/clauster/src/clauster/redact.py"
    assert entry["summary"]["regions"]["covered"] == 3


def test_drops_non_clauster_paths(tmp_path: Path) -> None:
    """Harness wrappers and third-party files are dropped (cifuzz would filter them)."""
    cov = tmp_path / "build-out"
    cov.mkdir()
    _write_cov(
        cov / "coverage_d_redact_fuzzer",
        {
            # The harness wrapper itself lives under the repo, not the package.
            "/pythoncovmergedfiles/medio/medio/src/clauster/fuzz/redact_fuzzer.py": {1, 2},
            THIRD_PARTY: {3},
        },
    )
    out = tmp_path / "fuzzer_stats"

    assert _run(cov, out).returncode == 0
    assert _covered(out, "redact_fuzzer") == set()


def test_targets_stay_distinct(tmp_path: Path) -> None:
    """Two harnesses with different coverage get different covered-file sets."""
    cov = tmp_path / "build-out"
    cov.mkdir()
    _write_cov(cov / "coverage_d_redact_fuzzer", {f"{SITE}/redact.py": {1, 2}})
    _write_cov(
        cov / "coverage_d_validate_clone_url_fuzzer",
        {f"{SITE}/provisioning.py": {7, 8, 9}},
    )
    out = tmp_path / "fuzzer_stats"

    assert _run(cov, out).returncode == 0

    redact = _covered(out, "redact_fuzzer")
    validate = _covered(out, "validate_clone_url_fuzzer")
    assert redact != validate
    assert redact == {"/src/clauster/src/clauster/redact.py"}
    assert validate == {"/src/clauster/src/clauster/provisioning.py"}


def test_empty_data_file_yields_empty_list_kept_by_cifuzz(tmp_path: Path) -> None:
    """A present-but-empty data file writes {"data":[{"files":[]}]} (cifuzz keeps it)."""
    cov = tmp_path / "build-out"
    cov.mkdir()
    # A present data file whose only file has zero covered lines (harness ran, covered nothing).
    empty = CoverageData(basename=str(cov / "coverage_d_empty_fuzzer"))
    empty.add_lines({f"{SITE}/redact.py": set()})
    empty.write()
    out = tmp_path / "fuzzer_stats"

    assert _run(cov, out).returncode == 0
    # This exact shape is what cifuzz reads as "no coverage" and so keeps the target.
    assert json.loads((out / "empty_fuzzer.json").read_text()) == {"data": [{"files": []}]}


def test_corrupt_data_file_fails_loud(tmp_path: Path) -> None:
    """A corrupt data file exits nonzero and writes nothing, never partial stats."""
    cov = tmp_path / "build-out"
    cov.mkdir()
    (cov / "coverage_d_corrupt_fuzzer").write_bytes(b"not a coverage database")
    out = tmp_path / "fuzzer_stats"

    result = _run(cov, out)
    assert result.returncode != 0
    assert not (out / "corrupt_fuzzer.json").exists()


def test_one_corrupt_file_writes_no_partial_stats(tmp_path: Path) -> None:
    """A corrupt file among valid ones writes none of them, not a partial set."""
    cov = tmp_path / "build-out"
    cov.mkdir()
    good = CoverageData(basename=str(cov / "coverage_d_redact_fuzzer"))
    good.add_lines({f"{SITE}/redact.py": {1, 2}})
    good.write()
    # The corrupt file must sort AFTER the good one (the script iterates sorted()), so a
    # non-atomic write-in-loop would already have written redact_fuzzer.json before failing.
    # That is what makes this test guard the atomic refactor rather than pass vacuously.
    (cov / "coverage_d_zzz_corrupt_fuzzer").write_bytes(b"not a coverage database")
    out = tmp_path / "fuzzer_stats"

    result = _run(cov, out)
    assert result.returncode != 0
    # The valid target's file is not written either: the whole rebuild is all-or-nothing.
    # Path.glob on a missing dir yields nothing, so this holds whether or not out exists.
    assert list(out.glob("*.json")) == []


def test_no_coverage_files_fails_closed(tmp_path: Path) -> None:
    """An empty coverage dir writes nothing and exits nonzero, not silently."""
    cov = tmp_path / "build-out"
    cov.mkdir()
    out = tmp_path / "fuzzer_stats"

    result = _run(cov, out)
    assert result.returncode == 1
    assert not list(out.glob("*.json"))


def test_missing_coverage_dir_fails_closed(tmp_path: Path) -> None:
    """A missing coverage dir exits nonzero rather than crashing."""
    result = _run(tmp_path / "does-not-exist", tmp_path / "fuzzer_stats")
    assert result.returncode == 1
