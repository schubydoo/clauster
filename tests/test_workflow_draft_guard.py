# Guard for the draft-PR skip (CI + security suites must not run while a PR is a draft).
#
# Two halves, and BOTH are needed — half a guard is worse than none:
#   * the trigger must include `ready_for_review`, or a PR flipped draft → ready never
#     re-dispatches and the work is skipped forever rather than deferred;
#   * every job must carry `github.event.pull_request.draft != true`, or one unguarded
#     job keeps burning the runner (and, for the analysers, the shared GITHUB_TOKEN
#     SARIF-upload pool) on every draft push.
#
# `!= true` rather than `== false` is deliberate: on push/schedule/workflow_dispatch the
# `draft` field is null, and `null != true` fail-safes to RUNNING. A `== false` spelling
# would silently disable those triggers.

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo_meta

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"

# The exact job-level expression every PR-facing job must contain.
DRAFT_GUARD = "github.event.pull_request.draft != true"

# A job whose `if:` already excludes pull_request entirely cannot run on a draft, so a
# draft term there would be dead code (e.g. security.yml's trivy-image).
PR_EXCLUDED = "github.event_name != 'pull_request'"

# `types: [closed]` fires only when a PR closes or merges. GitHub cannot merge a draft,
# and these workflows already gate on `merged == true`, so a draft guard is dead code.
MERGE_TIME_ONLY = frozenset({"knope-release.yml", "tap-sync-trigger.yml"})

# ClusterFuzzLite's per-PR workflow is owned by a separate in-flight change; exempted so
# the two don't conflict. Removing this entry once it carries the guard is a no-op —
# the exemption permits the guard, it does not forbid it.
EXEMPT_WORKFLOWS = frozenset({"cflite_pr.yml"})


def _triggers(doc):
    """Return a workflow's `on:` mapping (YAML 1.1 parses the bare key `on` as True)."""
    on = doc.get("on", doc.get(True))
    return on if isinstance(on, dict) else {}


def _pr_workflows():
    """Yield (filename, pull_request-trigger, jobs) for workflows this guard covers."""
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        if wf.name in MERGE_TIME_ONLY or wf.name in EXEMPT_WORKFLOWS:
            continue
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        triggers = _triggers(doc)
        if "pull_request" not in triggers:
            continue
        yield wf.name, triggers["pull_request"] or {}, doc.get("jobs") or {}


def _workflow_params():
    entries = list(_pr_workflows())
    assert entries, "expected at least one pull_request-triggered workflow (ci.yml)"
    return [pytest.param(*e, id=e[0]) for e in entries]


def _job_params():
    params = []
    for name, _pr, jobs in _pr_workflows():
        for job_id, job in jobs.items():
            params.append(pytest.param(name, job_id, job or {}, id=f"{name}:{job_id}"))
    assert params, "expected at least one job across the pull_request-triggered workflows"
    return params


@pytest.mark.parametrize(("workflow", "pull_request", "jobs"), _workflow_params())
def test_pull_request_trigger_lists_ready_for_review(workflow, pull_request, jobs):
    """A draft-skipping workflow re-dispatches on the draft → ready flip.

    Setting any `types:` replaces the implicit opened/synchronize/reopened defaults, so
    the list must be explicit AND must still carry whatever the workflow relied on.
    """
    assert jobs, f"{workflow} declares no jobs"
    types = pull_request.get("types")
    assert types is not None, (
        f"{workflow} relies on the default pull_request types, so `ready_for_review` "
        f"never fires and a draft PR would skip its jobs permanently. Add an explicit "
        f"`types:` list containing opened, synchronize, reopened, ready_for_review."
    )
    assert "ready_for_review" in types, (
        f"{workflow} sets `types: {types}` without `ready_for_review` — a PR flipped "
        f"draft → ready would never re-dispatch it."
    )


@pytest.mark.parametrize(("workflow", "job_id", "job"), _job_params())
def test_every_pull_request_job_skips_drafts(workflow, job_id, job):
    """Every job reachable on a `pull_request` event carries the draft guard."""
    condition = " ".join(str(job.get("if", "")).split())
    if PR_EXCLUDED in condition:
        pytest.skip(f"{workflow}:{job_id} never runs on pull_request events")
    assert DRAFT_GUARD in condition, (
        f"{workflow}:{job_id} runs on draft PRs — its `if:` is {condition!r}. Add "
        f"`{DRAFT_GUARD}` so drafts cost no CI, and keep `!= true` so push/schedule "
        f"(null `draft`) still run."
    )
