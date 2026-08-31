# Guards for the CI cost filters (Rel-5).
#
# Two mechanisms trim CI on a PR that cannot have changed how the app behaves, and both fail
# SILENTLY rather than loudly — hence these static guards.
#
# 1. `.github/actions/changed-code` classifies a PR diff as code vs docs/meta. `.github/**` is
#    docs/meta EXCEPT the workflows and composite actions that gate on the classifier themselves
#    (`selfgating`). Miss one of those and its edit merges having skipped the very jobs it
#    rewires — green PR, broken main.
#
# 2. `osv-scanner.yml` uses a `paths:` filter on `pull_request`. That is safe there and only
#    there because neither of its jobs backs a REQUIRED status check: a path-filtered required
#    check never reports and the PR hangs at "Expected — waiting for status" forever (#196). If
#    the filter drifts from the lockfiles the scanner reads, a dependency bump silently goes
#    unscanned on its own PR.
#
# THE BOOTSTRAP PROBLEM: everything above lives under `.github/**`, so a PR editing it takes the
# `code=false` path and skips the `tests` matrix — including these guards. That is why every
# module here is marked `repo_meta` and re-run in the always-on required `lint` job
# (.github/workflows/lint.yml). `test_every_repo_meta_guard_is_marked` keeps the marker from
# being forgotten on a future guard.

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo_meta

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"
WORKFLOWS = REPO / ".github" / "workflows"
CHANGED_CODE = REPO / ".github" / "actions" / "changed-code" / "action.yml"
OSV = WORKFLOWS / "osv-scanner.yml"
RULESET = REPO / ".github" / "rulesets" / "main.json"

# Lockfile/manifest basenames OSV-Scanner resolves packages from. `scan-scheduled` runs
# `osv-scanner scan source -r .` over the whole tree, so ANY tracked file with one of these
# names is in scope — which is what makes the PR `paths:` filter checkable rather than a
# hand-maintained list that quietly rots when a new ecosystem lands.
OSV_LOCKFILE_NAMES = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "requirements.txt",
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "Cargo.toml",
        "go.mod",
        "go.sum",
        "Gemfile.lock",
        "composer.lock",
        "gradle.lockfile",
        "pom.xml",
        "flake.lock",
        "flake.nix",
        "conan.lock",
        "renv.lock",
        "mix.lock",
        "pubspec.lock",
    }
)


def _shell_var(name):
    """Extract a single-quoted bash assignment (e.g. `noncode='...'`) from the action."""
    raw = CHANGED_CODE.read_text(encoding="utf-8")
    match = re.search(rf"^\s*{name}='([^']*)'\s*$", raw, re.MULTILINE)
    assert match, f"{name}= not found in {CHANGED_CODE.name} — did the classifier get rewritten?"
    return match.group(1)


def _classify(path):
    """Mirror the action's decision for one path: selfgating wins, then the noncode allowlist."""
    if re.search(_shell_var("selfgating"), path):
        return "code"
    return "noncode" if re.search(_shell_var("noncode"), path) else "code"


def _tracked_files():
    if not shutil.which("git"):
        pytest.skip("git not on PATH")
    proc = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        pytest.skip("not a git checkout")
    return [p for p in proc.stdout.split("\0") if p]


def _osv_pr_paths():
    doc = yaml.safe_load(OSV.read_text(encoding="utf-8"))
    # PyYAML parses the bare `on:` key as the boolean True.
    triggers = doc.get("on", doc.get(True, {}))
    return triggers["pull_request"]["paths"]


# --------------------------------------------------------------------------- classifier


def test_classifier_treats_github_automation_as_non_code():
    assert _classify(".github/workflows/docs.yml") == "noncode"
    assert _classify(".github/ISSUE_TEMPLATE/bug.yml") == "noncode"
    assert _classify(".greptile/config.json") == "noncode"
    # The pre-existing categories must survive the widening, including the CODEOWNERS and
    # FUNDING.yml entries that `^\.github/` now subsumes.
    assert _classify("README.md") == "noncode"
    assert _classify("docs/index.md") == "noncode"
    assert _classify("mkdocs.yml") == "noncode"
    assert _classify(".github/CODEOWNERS") == "noncode"
    assert _classify(".github/FUNDING.yml") == "noncode"
    # Root-level repo automation the matrix never validates (pre-commit runs no CI job).
    assert _classify(".pre-commit-config.yaml") == "noncode"
    assert _classify(".pre-commit-config.yml") == "noncode"


def test_classifier_still_classifies_real_code_as_code():
    for path in ("src/clauster/app.py", "tests/test_app.py", "uv.lock", "pyproject.toml"):
        assert _classify(path) == "code", f"{path} must classify as code"


def test_every_classifier_consumer_is_exempt_from_its_own_skip():
    # THE invariant: a workflow that gates jobs on `code` must never be able to skip those jobs
    # by editing itself, or the edit ships unvalidated. Adding a consumer without adding it to
    # `selfgating` fails here.
    # Structural, not a substring scan: a workflow only consumes the classifier if it actually
    # `uses:` the composite action. (lint.yml merely *mentions* it in a comment and always runs.)
    consumers = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        uses = []
        for job in (doc.get("jobs") or {}).values():
            uses.append(job.get("uses"))
            uses.extend(step.get("uses") for step in (job.get("steps") or []))
        if any(u and u.rstrip("/").endswith(".github/actions/changed-code") for u in uses):
            consumers.append(f".github/workflows/{path.name}")
    assert sorted(consumers) == [
        ".github/workflows/ci.yml",
        ".github/workflows/security.yml",
    ], f"classifier consumers changed — update `selfgating` in the action too: {consumers}"
    unguarded = [c for c in consumers if _classify(c) != "code"]
    assert unguarded == [], (
        f"these workflows gate on the changed-code classifier but are not in `selfgating`, "
        f"so editing one would skip the very jobs it rewires: {unguarded}"
    )


def test_selfgating_covers_the_shared_composite_actions():
    # The `setup` action installs uv/Python for the whole test matrix; a break there must not
    # hide behind the `^\.github/` docs-only rule.
    assert _classify(".github/actions/setup/action.yml") == "code"
    assert _classify(".github/actions/changed-code/action.yml") == "code"


@pytest.mark.skipif(os.name == "nt", reason="POSIX grep -E is the runtime the action uses")
def test_python_re_agrees_with_the_ere_the_action_actually_runs():
    # The tests above evaluate the action's patterns with Python's `re`; the action evaluates
    # them with `grep -Eq` (POSIX ERE). Prove the two agree for these patterns, so a future
    # edit that leans on a PCRE-only construct fails here instead of only in CI.
    if not shutil.which("grep"):
        pytest.skip("grep not on PATH")
    samples = [
        ".github/workflows/ci.yml",
        ".github/workflows/docs.yml",
        ".github/actions/setup/action.yml",
        ".github/CODEOWNERS",
        ".greptile/config.json",
        ".pre-commit-config.yaml",
        "src/clauster/app.py",
        "docs/index.md",
        "README.md",
    ]
    for var in ("selfgating", "noncode"):
        pattern = _shell_var(var)
        for sample in samples:
            grep = subprocess.run(["grep", "-Eq", pattern], input=sample, text=True, check=False)
            assert (grep.returncode == 0) is bool(re.search(pattern, sample)), (
                f"{var} disagrees between grep -E and Python re on {sample!r}"
            )


# --------------------------------------------------------------------------- osv paths filter


def test_osv_pr_paths_cover_every_lockfile_in_the_repo():
    # Derived, not hand-listed: adding a new ecosystem (Cargo.lock, go.mod, a docs
    # requirements.txt) fails here until the PR trigger is widened to match, instead of
    # silently never firing for it.
    tracked = _tracked_files()
    in_scope = sorted(f for f in tracked if os.path.basename(f) in OSV_LOCKFILE_NAMES)
    assert in_scope, "expected at least uv.lock to be tracked"
    paths = _osv_pr_paths()
    missing = [f for f in in_scope if f not in paths]
    assert missing == [], f"osv-scanner.yml PR paths filter misses scanned files: {missing}"


def test_osv_pr_paths_include_the_workflow_itself():
    # Self-validation: a change to the filter must still trigger its own scan.
    assert ".github/workflows/osv-scanner.yml" in _osv_pr_paths()


def test_osv_paths_filter_is_only_safe_because_it_gates_nothing_required():
    # A `paths:` filter on a workflow backing a REQUIRED context is the #196 breakage. Assert
    # no job here carries a required context name, so this filter can never become one.
    ruleset = yaml.safe_load(RULESET.read_text(encoding="utf-8"))
    required = {
        ctx["context"]
        for rule in ruleset["rules"]
        if rule["type"] == "required_status_checks"
        for ctx in rule["parameters"]["required_status_checks"]
    }
    assert required, "expected the ruleset to list required status checks"
    doc = yaml.safe_load(OSV.read_text(encoding="utf-8"))
    for key, job in doc["jobs"].items():
        name = job.get("name", key)
        # Two shapes, because `scan-pr` is a reusable-workflow call (`uses:`) and GitHub
        # reports those as "<key> / <inner job name>", never the bare key. An equality
        # check alone would let a required `scan-pr / osv-scan` slip through and reopen
        # the #196 trap this test exists to close.
        clash = [r for r in required if r == name or r.startswith(f"{key} / ")]
        assert not clash, (
            f"{clash} is a REQUIRED check but osv-scanner.yml is path-filtered — a PR that "
            f"skips it would hang at 'Expected — waiting for status' (#196)"
        )


# --------------------------------------------------------------------------- the bootstrap


def test_every_repo_meta_guard_is_marked():
    # A test asserting on a `.github/**` file is skipped by the very filter it guards unless it
    # is marked `repo_meta` (which also runs it in the always-on `lint` job). Catch a new guard
    # that forgets the marker — the whole point of this module.
    # rglob, not glob: a guard added under a subdirectory (tests/ci/test_x.py) would be
    # invisible to a top-level glob — exactly the miss this test exists to catch. e2e is
    # excluded because it is opt-in and never part of the default or lint run.
    unmarked = []
    for path in sorted(TESTS.rglob("test_*.py")):
        if "e2e" in path.relative_to(TESTS).parts:
            continue
        source = path.read_text(encoding="utf-8")
        # ⚠️ Substring heuristic with a thin margin: `.github/` deliberately keeps the
        # trailing slash so it does NOT match the `schubydoo.github.io/...` docs URL in
        # tests/test_app.py (`.github.` there, not `.github/`). Widening this to
        # `.github` would false-positive on that file.
        touches_meta = '".github"' in source or ".github/" in source
        marked = "pytest.mark.repo_meta" in source
        if touches_meta and not marked:
            unmarked.append(path.name)
    assert unmarked == [], (
        f"these tests assert on .github/** but are not marked `repo_meta`, so they are skipped "
        f"on exactly the PRs they guard — add `pytestmark = pytest.mark.repo_meta`: {unmarked}"
    )


def test_the_guard_host_cannot_skip_its_own_validation():
    # The other half of the bootstrap, and the subtler one. `lint.yml` HOSTS the
    # `repo_meta` step, and on a `.github/**`-only PR that step is the only thing
    # validating the change. It also matches the `^\.github/` non-code allowlist — so
    # unless it is ALSO self-gating, a PR whose diff is just `lint.yml` classifies
    # code=false, skips the matrix, and runs a `lint` job with the guard step deleted.
    # The entire bootstrap could then be removed by a green, CI-only PR, with every
    # guard against that executing nowhere.
    #
    # Derived from the workflow rather than hardcoded: whichever workflow hosts the
    # marker must be self-gating, so this keeps holding if the step ever moves.
    hosts = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in (doc.get("jobs") or {}).values():
            if any("-m repo_meta" in (s.get("run") or "") for s in (job.get("steps") or [])):
                hosts.append(f".github/workflows/{path.name}")
                break
    assert hosts, "no workflow runs `pytest -m repo_meta` — the repo_meta bootstrap is gone"
    for host in hosts:
        assert _classify(host) == "code", (
            f"{host} hosts the repo_meta guards but is not in `selfgating`, so a PR editing "
            f"only that file would skip the matrix AND delete the step that replaces it"
        )


def test_the_lint_job_actually_runs_the_repo_meta_marker():
    # The marker only helps if the always-on required `lint` job selects on it.
    #
    # Parsed, not substring-searched: a plain `"-m repo_meta" in lint` passes on a
    # lint.yml where the step has been commented out, or replaced by prose that
    # happens to mention the marker. This guard is the load-bearing half of the
    # bootstrap, so it has to be structural — and parsing also survives a rename of
    # the step.
    doc = yaml.safe_load((WORKFLOWS / "lint.yml").read_text(encoding="utf-8"))
    runs = [step.get("run", "") for step in doc["jobs"]["lint"]["steps"]]
    assert any("-m repo_meta" in run for run in runs), (
        "a step in lint.yml's `lint` job must run `pytest -m repo_meta` — it is the only "
        "thing validating a `.github/**`-only PR (see this module's header)"
    )


def test_the_lint_job_actually_runs_the_changeset_lint():
    # Same shape, same reason. A `.changeset/*.md`-only PR classifies non-code and skips the
    # tests matrix, so this step is the ONLY thing checking a fragment knope would silently
    # drop (#1320). Nothing else asserts it exists, so deleting it would merge green.
    # Parsed and matched on the script path, so a step rename doesn't break the guard.
    doc = yaml.safe_load((WORKFLOWS / "lint.yml").read_text(encoding="utf-8"))
    runs = [step.get("run", "") for step in doc["jobs"]["lint"]["steps"]]
    assert any("scripts/lint_changesets.py" in run for run in runs), (
        "a step in lint.yml's `lint` job must run `scripts/lint_changesets.py` — it is the "
        "only thing validating a changeset-only PR"
    )


def test_cflite_requirements_export_is_fresh():
    # `.clusterfuzzlite/requirements.txt` is GENERATED from uv.lock (see build.sh) and hash-
    # pins the fuzz build (Scorecard alert 149). The failure mode this guards is the silent
    # one: a Renovate bump moves uv.lock, nobody regenerates the export, and the fuzzers run
    # against dependency versions the project no longer declares — `pip3 check` cannot catch
    # that, because pyproject's ranges are unbounded minimums the stale pins still satisfy.
    # `--frozen` makes the export deterministic from uv.lock, so equality is checkable.
    # Compared without comment lines: uv writes its own invocation into the header, which
    # differs between `-o <file>` (committed) and stdout (here).
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not on PATH")
    result = subprocess.run(  # noqa: S603 — fixed argv, no untrusted input
        [uv, "export", "--frozen", "--no-emit-project", "--extra", "pty"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    strip = lambda text: [ln for ln in text.splitlines() if ln and not ln.startswith("#")]  # noqa: E731
    committed = (REPO / ".clusterfuzzlite" / "requirements.txt").read_text(encoding="utf-8")
    assert strip(committed) == strip(result.stdout), (
        "`.clusterfuzzlite/requirements.txt` is stale — regenerate: uv export --frozen "
        "--no-emit-project --extra pty -o .clusterfuzzlite/requirements.txt"
    )
