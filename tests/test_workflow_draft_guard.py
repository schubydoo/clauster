# Guard for the draft-PR skip: the HEAVY / SARIF-uploading jobs must not run while a PR is
# a draft, while a small allowlist of CHEAP jobs deliberately stays live on drafts.
#
# Two halves, and BOTH are needed — half a guard is worse than none:
#   * the trigger must include `ready_for_review`, or a PR flipped draft → ready never
#     re-dispatches and the work is skipped forever rather than deferred;
#   * every job NOT on the RUNS_ON_DRAFTS allowlist must carry
#     `github.event.pull_request.draft != true`, or one unguarded heavy job keeps burning
#     the runner (and, for the analysers, the shared GITHUB_TOKEN SARIF-upload pool) on
#     every draft push.
#
# Why an allowlist rather than "skip everything": skipping a CHEAP required check
# (conventional PR title, lint) buys no runner/API time and leaves that required context
# vacuously green on a draft — real coverage lost for nothing. And gitleaks is a security
# scan that uploads no SARIF and mints its own App token, so running it on every draft
# costs nothing the skip was protecting while catching a leaked credential early. So those
# stay live; only the expensive / SARIF jobs defer to the ready flip.
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

# The exact job-level expression a draft-skipping job must contain, as an AND conjunct.
DRAFT_GUARD = "github.event.pull_request.draft != true"

# Cheap PR jobs that deliberately RUN on drafts instead of skipping — the exception, kept
# minimal. Two admissible reasons only, both checked by eye when adding an entry:
#   * a REQUIRED status context whose check would otherwise be vacuously green on a draft
#     (conventional PR title, lint) — skipping it saves no runner/API time and drops real
#     coverage; or
#   * a security scan that is cheap AND uploads no SARIF, so a per-draft run costs nothing
#     from the shared GITHUB_TOKEN pool the skip protects (gitleaks).
# Every job NOT listed here must still carry the draft guard.
RUNS_ON_DRAFTS = frozenset(
    {
        ("pr-title.yml", "validate"),
        ("lint.yml", "lint"),
        ("actionlint.yml", "actionlint"),
        ("changeset-check.yml", "changeset"),
        ("repo-config-drift.yml", "drift"),
        ("security.yml", "gitleaks"),
    }
)

# A job whose `if:` already excludes pull_request entirely cannot run on a draft, so a
# draft term there would be dead code (e.g. security.yml's trivy-image).
#
# ⚠️ This must be matched as a LEADING CONJUNCT, never as a substring. security.yml's
# zizmor job reads `(github.event_name != 'pull_request' || github.event.action !=
# 'synchronize') && <guard>` — there the string appears as a DISJUNCT, where it excludes
# nothing and the job really does run on PR opened/reopened/ready_for_review. A substring
# test skipped that job, so deleting its draft guard left this suite green.
PR_EXCLUDED = "github.event_name != 'pull_request'"

# The four workflows backing a required status context. They are evaluated per head SHA,
# so dropping `synchronize` would leave a fixup push's head with no check run and hang
# the PR at "Expected — waiting for status". osv-scanner, repo-config-drift, and
# cflite_pr trim their types deliberately and are NOT in this set.
REQUIRED_CTX_WORKFLOWS = frozenset({"ci.yml", "lint.yml", "security.yml", "pr-title.yml"})
DEFAULT_PR_TYPES = frozenset({"opened", "reopened", "synchronize"})


def _is_merge_time_only(types):
    """True for a `types: [closed]` trigger — a draft can never reach it.

    Derived from the trigger rather than a filename allowlist, so a workflow that later
    gains a non-close type stops being exempt instead of silently keeping a stale pass.
    """
    return bool(types) and set(types) <= {"closed"}


def _is_pr_excluded(condition):
    """True when an `if:` excludes `pull_request` events at the TOP level of an AND."""
    return condition == PR_EXCLUDED or condition.startswith(f"{PR_EXCLUDED} &&")


def _has_draft_guard(condition):
    """True when DRAFT_GUARD is present as a top-level AND conjunct, never as a disjunct.

    The mirror of `_is_pr_excluded`'s leading-conjunct rule, and the reason a plain `in`
    test is not enough: spelled as a disjunct — `<other> || <guard>` — the guard excludes
    nothing and the job still runs on every draft, yet the substring is present. So a job
    that reads `needs.x == 'true' || github.event.pull_request.draft != true` must FAIL
    this, exactly as the exemption side rejects the same shape.
    """
    if DRAFT_GUARD not in condition:
        return False
    return f"|| {DRAFT_GUARD}" not in condition and f"{DRAFT_GUARD} ||" not in condition


def _triggers(doc):
    """Return a workflow's `on:` mapping (YAML 1.1 parses the bare key `on` as True)."""
    on = doc.get("on", doc.get(True))
    return on if isinstance(on, dict) else {}


def _pr_workflows():
    """Yield (filename, pull_request-trigger, jobs) for workflows this guard covers."""
    files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    for wf in files:
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        triggers = _triggers(doc)
        if "pull_request" not in triggers:
            continue
        pull_request = triggers["pull_request"] or {}
        if _is_merge_time_only(pull_request.get("types")):
            continue
        yield wf.name, pull_request, doc.get("jobs") or {}


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
    """Every PR workflow re-dispatches on the draft → ready flip.

    Required for a draft-SKIPPING workflow (or its deferred work never runs); still
    required for a RUNS_ON_DRAFTS one whose types drop `synchronize` (repo-config-drift),
    so a ready flip carrying no push still re-runs it. Setting any `types:` replaces the
    implicit opened/synchronize/reopened defaults, so the list must be explicit AND must
    still carry whatever the workflow relied on.
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
    if workflow in REQUIRED_CTX_WORKFLOWS:
        missing = DEFAULT_PR_TYPES - set(types)
        assert not missing, (
            f"{workflow} backs a REQUIRED status context but dropped {sorted(missing)} "
            f"from its types. Required checks are evaluated per head SHA, so without "
            f"`synchronize` a fixup push leaves the new head with no check run and the "
            f"PR hangs at 'Expected — waiting for status'."
        )


@pytest.mark.parametrize(("workflow", "job_id", "job"), _job_params())
def test_every_pull_request_job_has_the_correct_draft_behavior(workflow, job_id, job):
    """Heavy / SARIF jobs skip drafts (AND conjunct); the RUNS_ON_DRAFTS allowlist stays live."""
    condition = " ".join(str(job.get("if", "")).split())
    if _is_pr_excluded(condition):
        pytest.skip(f"{workflow}:{job_id} never runs on pull_request events")
    if (workflow, job_id) in RUNS_ON_DRAFTS:
        assert not _has_draft_guard(condition), (
            f"{workflow}:{job_id} is on the RUNS_ON_DRAFTS allowlist (a cheap required "
            f"context or a no-SARIF secret scan) but its `if:` still carries the draft "
            f"guard — it should stay LIVE on drafts. Drop the guard, or remove the entry."
        )
        return
    assert _has_draft_guard(condition), (
        f"{workflow}:{job_id} runs on draft PRs — its `if:` is {condition!r}. Add "
        f"`{DRAFT_GUARD}` as an AND conjunct so drafts cost no CI, and keep `!= true` so "
        f"push/schedule (null `draft`) still run. If it is a cheap required check or a "
        f"no-SARIF secret scan that SHOULD run on drafts, add it to RUNS_ON_DRAFTS instead."
    )
