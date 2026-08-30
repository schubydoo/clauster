# Guard for the docs-lint glob list (#1316).
#
# `.markdownlint-cli2.yaml` deliberately uses POSITIVE globs instead of `**/*.md` and
# `gitignore: false` — the recursive walk (and gitignore:true's own ignore-file hunt)
# follows the `.claude` symlink cycle inside spawned agent worktrees and OOMs node.
# The cost of positive globs is that they fail OPEN: a new directory of tracked docs
# outside the listed trees would silently go unlinted while the gate stays green.
# This guard closes that: every git-tracked `*.md` must sit inside a globbed tree or
# be deliberately ignore-listed. Marked `repo_meta` so the always-on lint job runs it
# (see test_ci_change_filter.py for the bootstrap rationale).

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo_meta

REPO = Path(__file__).resolve().parents[1]
LINT_CONFIG = REPO / ".markdownlint-cli2.yaml"


def test_every_tracked_markdown_file_is_covered_by_the_lint_globs():
    config = yaml.safe_load(LINT_CONFIG.read_text(encoding="utf-8"))
    globs = config["globs"]
    ignores = set(config["ignores"])
    # The glob trees, derived from the config so this guard can't drift from it:
    # "docs/**/*.md" covers the "docs/" prefix; a bare "*.md" covers root-level files.
    tree_prefixes = tuple(g.split("/**/", 1)[0] + "/" for g in globs if "/**/" in g)
    assert tree_prefixes, "expected at least one tree glob like 'docs/**/*.md'"
    assert "*.md" in globs, "root-level markdown must stay covered"

    # Platform smoke jobs run the suite from a copied tree with no usable git context
    # (the alpine/musl leg failed here); the guard is only meaningful in a real checkout.
    if not shutil.which("git"):
        pytest.skip("git not available")
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        pytest.skip("not a git checkout (platform smoke container)")

    tracked = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert tracked, "instrument check: git ls-files must see the repo's markdown"

    # Directory-shaped ignore entries (".changeset/**") also count as deliberate coverage —
    # derived from the config like the globs, so dropping one there fails here too.
    ignore_prefixes = tuple(
        i[: -len("/**")] + "/" for i in ignores if i.endswith("/**") and not i.startswith("**/")
    )
    uncovered = [
        path
        for path in tracked
        if "/" in path  # root-level files are matched by "*.md"
        and not path.startswith(tree_prefixes)
        and not path.startswith(ignore_prefixes)
    ]
    assert not uncovered, (
        f"tracked markdown outside the lint globs (add its tree to "
        f".markdownlint-cli2.yaml globs, or ignore-list it deliberately): {uncovered}"
    )
