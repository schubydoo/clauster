#!/usr/bin/env python3
"""Rebuild per-target fuzzer_stats JSON from Python coverage data.

ClusterFuzzLite's per-PR "code-change" mode runs only the fuzz targets a change
affects. cifuzz decides which targets are affected by reading
``coverage/latest/fuzzer_stats/<target>.json`` and expects the llvm-cov export
shape: ``data[0].files[]`` where each entry has a ``filename`` and a
``summary.regions.covered`` count. On the Atheris/Python path the oss-fuzz
base-runner writes a dummy ``{}`` per target, so cifuzz sees no coverage and
never prunes.

This script rebuilds those per-target files from the base-runner's own per-target
coverage data. During a coverage run the base-runner writes one translated
coverage data file per harness to ``$OUT`` as ``coverage_d_<target>``. For each of
those, this script lists the files the harness covered and how many lines, then
writes ``<target>.json`` in the shape cifuzz reads.

The recorded paths are not the paths cifuzz compares against, so this script rewrites
them. cifuzz keeps a covered file only if it starts with the image checkout
``/src/clauster``, then strips that prefix and compares the remainder to git-diff paths.
The base-runner records each covered module under the interpreter's
``site-packages/clauster/`` copy (``build.sh`` installs clauster non-editable), so this
script maps ``.../site-packages/clauster/redact.py`` to
``/src/clauster/src/clauster/redact.py`` and drops every non-clauster-module path. Without
that rewrite cifuzz filters out every entry and prunes nothing. See ``_image_repo_path``.

Reading uses coverage's ``CoverageData`` API, not ``coverage json``, so it needs
no source tree on disk. The source lives inside the build container, not on the
runner, and ``coverage json`` fails with "No source for code" without it.

Usage::

    python scripts/gen_fuzzer_stats.py --coverage-dir build-out --out-dir fuzzer_stats

Fail-closed by design. A target with an empty but present data file yields an empty
file list, which cifuzz reads as "no coverage" and so keeps the target. A target with
no data file at all is not written here and keeps its existing (dummy ``{}``) stats,
which cifuzz also reads as "no coverage". Either way pruning never drops a target this
script lacks data for.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from coverage import CoverageData

PREFIX = "coverage_d_"

# cifuzz filters covered files by ``filename.startswith(repo_path)`` and compares the
# remainder to git-diff paths (repo-relative), where ``repo_path`` is the image checkout
# ``/src/<repo name>`` (oss-fuzz infra/cifuzz/get_coverage.py + continuous_integration.py).
# So a covered file only counts if its path is under this prefix.
IMAGE_REPO = "/src/clauster"

# ``.clusterfuzzlite/build.sh`` installs clauster non-editable, so the base-runner records
# each covered module under the interpreter's ``site-packages/clauster/`` copy, not the
# repo checkout. Map that back to the checkout path so the remainder matches git diff:
# ``.../site-packages/clauster/redact.py`` -> ``/src/clauster/src/clauster/redact.py`` ->
# (cifuzz strips ``/src/clauster/``) -> ``src/clauster/redact.py``.
_SITE_PACKAGES_CLAUSTER = re.compile(r"/site-packages/clauster/(.+)$")


def _image_repo_path(filename: str) -> str | None:
    """Map a base-runner coverage path to the checkout path cifuzz compares against.

    Return None for anything that is not a clauster package module (third-party
    dependencies, the standard library, and the harness wrappers themselves). cifuzz
    would filter those out anyway, so dropping them keeps the file small and cannot
    make a change to a non-target file look like it affects a harness. Dropping the
    harness wrapper does mean a PR that edits only a harness, not its module, skips that
    target's per-PR fuzz. That is a safe false-negative: the harness smoke test and the
    weekly cron still exercise it, and the per-PR run is advisory.
    """
    match = _SITE_PACKAGES_CLAUSTER.search(filename)
    if match:
        return f"{IMAGE_REPO}/src/clauster/{match.group(1)}"
    return None


def target_stats(data_path: Path) -> dict:
    """Return the llvm-cov-style coverage dict for one target's data file."""
    data = CoverageData(basename=str(data_path))
    data.read()
    files = []
    for filename in sorted(data.measured_files()):
        mapped = _image_repo_path(filename)
        if mapped is None:
            continue
        covered = len(data.lines(filename) or [])
        if covered:
            files.append({"filename": mapped, "summary": {"regions": {"covered": covered}}})
    return {"data": [{"files": files}]}


def generate(coverage_dir: Path, out_dir: Path) -> int:
    """Write one ``<target>.json`` per ``coverage_d_<target>`` file; return the count.

    Read every data file before writing any output, so a failure on one target leaves
    nothing behind rather than a partial set of files in the ``out_dir`` working tree.
    """
    stats_by_target = {
        data_path.name[len(PREFIX) :]: target_stats(data_path)
        for data_path in sorted(coverage_dir.glob(f"{PREFIX}*"))
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    for target, stats in stats_by_target.items():
        (out_dir / f"{target}.json").write_text(json.dumps(stats), encoding="utf-8")
    return len(stats_by_target)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and generate the per-target fuzzer_stats files."""
    parser = argparse.ArgumentParser(description="Rebuild per-target fuzzer_stats JSON.")
    parser.add_argument(
        "--coverage-dir",
        type=Path,
        required=True,
        help="Directory holding the base-runner coverage_d_<target> files.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="fuzzer_stats directory to write <target>.json into.",
    )
    args = parser.parse_args(argv)
    if not args.coverage_dir.is_dir():
        print(f"coverage-dir not found: {args.coverage_dir}", file=sys.stderr)
        return 1
    written = generate(args.coverage_dir, args.out_dir)
    print(f"wrote {written} fuzzer_stats file(s) to {args.out_dir}")
    if written == 0:
        print("no coverage_d_* files found — nothing written", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
