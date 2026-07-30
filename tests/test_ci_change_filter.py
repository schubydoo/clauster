# Guards for the CI cost filters (Rel-5).
#
# Two mechanisms trim CI on a PR that cannot have changed how the app behaves, and each has a
# failure mode that is silent rather than loud — hence these static guards.
#
# 1. `.github/actions/changed-code` classifies a PR diff as code vs docs/meta. `.github/**` is
#    docs/meta EXCEPT the workflows and composite actions that themselves gate on the classifier
#    (`selfgating`). Miss one of those and its edit merges having skipped the very jobs it
#    rewires — green PR, broken main.
#
# 2. `osv-scanner.yml` uses a `paths:` filter on `pull_request`. That is only safe because
#    neither of its jobs is a REQUIRED status check: a path-filtered required check never
#    reports, and the PR hangs at "Expected — waiting for status" forever (#196). If the filter
#    ever drifts from the lockfiles the scanner reads, a dependency bump silently goes unscanned
#    on its own PR.

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
CHANGED_CODE = REPO / ".github" / "actions" / "changed-code" / "action.yml"
OSV = WORKFLOWS / "osv-scanner.yml"

# The lockfiles + manifests OSV resolves vulnerabilities from. Kept here (not derived) so that
# ADDING an ecosystem to the repo fails this test until the workflow filter is widened too.
OSV_SCANNED_FILES = (
    "uv.lock",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "tests/e2e/package.json",
    "tests/e2e/package-lock.json",
    "flake.lock",
    "flake.nix",
)


def _shell_var(name: str) -> str:
    """Extract a single-quoted bash assignment (e.g. `noncode='...'`) from the action."""
    raw = CHANGED_CODE.read_text(encoding="utf-8")
    match = re.search(rf"^\s*{name}='([^']*)'\s*$", raw, re.MULTILINE)
    assert match, f"{name}= not found in {CHANGED_CODE.name} — did the classifier get rewritten?"
    return match.group(1)


def _consumers_of_the_classifier():
    """Every workflow with a job that gates on the changed-code classifier's `code` output."""
    consumers = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if "actions/changed-code" in text:
            consumers.append(f".github/workflows/{path.name}")
    return consumers


def test_classifier_treats_github_automation_as_non_code():
    noncode = _shell_var("noncode")
    assert re.search(noncode, ".github/workflows/docs.yml")
    assert re.search(noncode, ".github/ISSUE_TEMPLATE/bug.yml")
    assert re.search(noncode, ".greptile/config.json")
    # The pre-existing categories must survive the widening.
    assert re.search(noncode, "README.md")
    assert re.search(noncode, "docs/index.md")
    assert re.search(noncode, "mkdocs.yml")


def test_classifier_still_calls_real_code_code():
    noncode = _shell_var("noncode")
    for path in ("src/clauster/app.py", "tests/test_app.py", "uv.lock", "pyproject.toml"):
        assert not re.search(noncode, path), f"{path} must classify as code"


def test_every_classifier_consumer_is_exempt_from_its_own_skip():
    # THE invariant: a workflow that gates jobs on `code` must never be able to skip those jobs
    # by editing itself, or the edit ships unvalidated. Adding a new consumer without adding it
    # to `selfgating` fails here.
    selfgating = _shell_var("selfgating")
    consumers = _consumers_of_the_classifier()
    assert consumers, "expected ci.yml/security.yml to consume the classifier"
    unguarded = [c for c in consumers if not re.search(selfgating, c)]
    assert unguarded == [], (
        f"these workflows gate on the changed-code classifier but are not in `selfgating`, "
        f"so editing one would skip the jobs it rewires: {unguarded}"
    )


def test_selfgating_covers_the_shared_composite_actions():
    # The `setup` action installs uv/Python for the test matrix; a break there must not hide
    # behind the `^\\.github/` docs-only rule.
    selfgating = _shell_var("selfgating")
    assert re.search(selfgating, ".github/actions/setup/action.yml")
    assert re.search(selfgating, ".github/actions/changed-code/action.yml")
    # ...but unrelated automation stays skippable, which is the whole point.
    assert not re.search(selfgating, ".github/workflows/docs.yml")


def test_osv_pr_paths_cover_every_scanned_lockfile():
    doc = yaml.safe_load(OSV.read_text(encoding="utf-8"))
    # PyYAML parses the bare `on:` key as the boolean True.
    triggers = doc.get("on", doc.get(True, {}))
    paths = triggers["pull_request"]["paths"]
    missing = [f for f in OSV_SCANNED_FILES if f not in paths]
    assert missing == [], f"osv-scanner.yml PR paths filter misses scanned files: {missing}"
    # Self-validation: a change to the workflow must still trigger its own scan.
    assert ".github/workflows/osv-scanner.yml" in paths


def test_osv_paths_filter_is_only_safe_because_it_gates_nothing_required():
    # A `paths:` filter on a workflow backing a REQUIRED context is the #196 breakage. Assert the
    # required-context names never appear as job names here, so this filter can't become one.
    ruleset = yaml.safe_load((REPO / ".github" / "rulesets" / "main.json").read_text("utf-8"))
    required = {
        ctx["context"]
        for rule in ruleset["rules"]
        if rule["type"] == "required_status_checks"
        for ctx in rule["parameters"]["required_status_checks"]
    }
    doc = yaml.safe_load(OSV.read_text(encoding="utf-8"))
    for key, job in doc["jobs"].items():
        name = job.get("name", key)
        assert name not in required, (
            f"{name} is a REQUIRED check but osv-scanner.yml is path-filtered — a PR that "
            f"skips it would hang at 'Expected — waiting for status' (#196)"
        )
