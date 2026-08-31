# Guard for the `$/` self-repository migration (code-scanning alerts 139-148).
#
# actionlint's local-action call checks — the referenced action exists, every `with:`
# key is a declared input, every `required:`-without-default input is supplied — are
# gated on the `./` uses-prefix internally. Migrating to `$/` (immune to runtime
# filesystem substitution; treated as pinned by GitHub) makes actionlint see an
# unrecognised remote action and silently skip ALL of that, so a typo'd input would
# lint clean and fail only at run time. This module re-asserts the same validation
# over every local `uses:` form, `$/` and `./` alike, so the migration moves the
# coverage here instead of losing it. Remove alongside `.github/actionlint.yaml`
# once rhysd/actionlint issue 711 ships native `$/` support (clauster issue 1361).

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo_meta

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"

_LOCAL_PREFIXES = ("$/", "./")


def _local_action_calls():
    """Yield (workflow name, job id, uses target, with-mapping) for local action steps."""
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                uses = step.get("uses") or ""
                if uses.startswith(_LOCAL_PREFIXES):
                    yield wf.name, job_id, uses, (step.get("with") or {})


def _action_calls_param():
    calls = list(_local_action_calls())
    assert calls, "expected at least one local action call (ci.yml uses actions/setup)"
    return [pytest.param(*c, id=f"{c[0]}:{c[1]}:{c[2]}") for c in calls]


@pytest.mark.parametrize(("workflow", "job", "uses", "supplied"), _action_calls_param())
def test_local_action_call_matches_its_declared_inputs(workflow, job, uses, supplied):
    """Every local `uses:` resolves, and its `with:` matches the action's inputs.

    Exactly what actionlint checks for `./` refs and skips for `$/` ones: the action
    directory must exist with an action.yml, every supplied `with:` key must be a
    declared input, and every `required: true` input without a `default:` must be
    supplied. A failure here is what a typo'd or missing input looks like BEFORE it
    becomes a run-time failure inside a green-linting workflow.
    """
    rel = uses.removeprefix("$/").removeprefix("./")
    action_yml = REPO / rel / "action.yml"
    assert action_yml.is_file(), f"{workflow}:{job}: {uses} -> {action_yml} does not exist"

    inputs = yaml.safe_load(action_yml.read_text(encoding="utf-8")).get("inputs") or {}
    unknown = sorted(set(supplied) - set(inputs))
    assert unknown == [], f"{workflow}:{job}: {uses} passes undeclared input(s) {unknown}"

    missing = sorted(
        name
        for name, spec in inputs.items()
        if (spec or {}).get("required") and "default" not in (spec or {}) and name not in supplied
    )
    assert missing == [], f"{workflow}:{job}: {uses} omits required input(s) {missing}"


def test_job_level_reusable_workflow_refs_resolve():
    """A job-level local `uses:` (reusable workflow) must point at an existing file.

    knope-release.yml deliberately keeps the `./` form for its job-level call — that
    grammar is exercised only at release time, so it is not migrated to `$/` — and
    this pins that the target exists either way.
    """
    seen = 0
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        for job_id, job in (doc.get("jobs") or {}).items():
            uses = job.get("uses") or ""
            if uses.startswith(_LOCAL_PREFIXES):
                seen += 1
                rel = uses.removeprefix("$/").removeprefix("./")
                assert (REPO / rel).is_file(), f"{wf.name}:{job_id}: {uses} does not resolve"
    assert seen, "expected at least one job-level local reusable-workflow call (knope-release)"
