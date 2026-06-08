# Fail-closed CI guard for the Claude GitHub Actions workflows.
#
# claude.yml (interactive @claude) and claude-review.yml (on-demand inline review)
# run in the BASE repo context WITH secrets on comment/issue events — events the
# repo's "require approval for external contributors" policy does NOT cover. Their
# safety therefore rests entirely on in-workflow invariants: an author-association
# allow-list, SHA-pinned actions, no credential persistence, advisory contents:read,
# and a COMMENT-only (never blocking) review. These tests parse the workflows and
# fail CI if any of those invariants is silently dropped — so a careless edit can't
# quietly open the door. Each invariant ships with a "detector catches a violation"
# test so the guard can never pass vacuously.

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
CLAUDE_WORKFLOWS = ["claude.yml", "claude-review.yml"]
TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_ACTION = "anthropics/claude-code-action@"


def _raw(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _doc(name):
    return yaml.safe_load(_raw(name))


def _steps(doc):
    for job in doc.get("jobs", {}).values():
        yield from job.get("steps", [])


def _job_ifs(doc):
    return [job["if"] for job in doc.get("jobs", {}).values() if "if" in job]


def _jobs_without_if(doc):
    """Return job names with no `if:` — they run unconditionally, bypassing the gate."""
    return [name for name, job in doc.get("jobs", {}).items() if "if" not in job]


# ----- detectors (return the offending items; empty/False == clean) ----------


def _unpinned_uses(doc):
    """Return `uses:` refs that are not pinned to a 40-hex commit SHA (local ./ refs ok)."""
    bad = []
    for step in _steps(doc):
        ref = step.get("uses")
        if not ref or ref.startswith("."):
            continue
        sha = ref.split("@", 1)[1] if "@" in ref else ""
        if not _SHA.match(sha):
            bad.append(ref)
    return bad


def _gate_ok(job_if):
    """True when an `if:` requires the trusted association allow-list and admits no others."""
    if "CONTRIBUTOR" in job_if:  # the untrusted "has a merged PR" role — must never be allowed
        return False
    if not all(role in job_if for role in TRUSTED):
        return False
    # Every distinct event branch must carry its own association check: a new trigger
    # added without a gate (more event branches than association checks) fails closed.
    return job_if.count("author_association") >= max(1, job_if.count("github.event_name =="))


def _checkouts_without_persist_false(doc):
    """Return actions/checkout steps that don't set persist-credentials: false."""
    bad = []
    for step in _steps(doc):
        if step.get("uses", "").startswith("actions/checkout@"):
            if step.get("with", {}).get("persist-credentials") is not False:
                bad.append(step)
    return bad


def _review_prompt(doc):
    for step in _steps(doc):
        if step.get("uses", "").startswith(_ACTION):
            return step.get("with", {}).get("prompt", "")
    return ""


def _comment_only_ok(prompt):
    """True when the review prompt pins COMMENT and forbids REQUEST_CHANGES."""
    norm = " ".join(prompt.split())
    if '"COMMENT"' not in norm:
        return False
    # REQUEST_CHANGES may appear ONLY inside the explicit `never "REQUEST_CHANGES"` ban.
    return "REQUEST_CHANGES" not in norm.replace('never "REQUEST_CHANGES"', "")


# ----- the guard: real workflows must satisfy every invariant ----------------


@pytest.mark.parametrize("name", CLAUDE_WORKFLOWS)
def test_workflow_file_exists(name):
    assert (WORKFLOWS / name).is_file(), f"{name} missing — a rename/delete must be deliberate"


@pytest.mark.parametrize("name", CLAUDE_WORKFLOWS)
def test_actions_are_sha_pinned(name):
    assert _unpinned_uses(_doc(name)) == []


@pytest.mark.parametrize("name", CLAUDE_WORKFLOWS)
def test_every_job_has_author_association_gate(name):
    doc = _doc(name)
    # An if-less job runs unconditionally — it never reaches _gate_ok, so check first
    # that EVERY job carries an `if:` before validating the gates themselves.
    missing = _jobs_without_if(doc)
    assert not missing, f"{name}: ungated jobs {missing} — every job must have an `if:` gate"
    job_ifs = _job_ifs(doc)
    assert job_ifs, f"{name}: no gated job — a claude workflow must gate its job `if:`"
    assert all(_gate_ok(j) for j in job_ifs)


@pytest.mark.parametrize("name", CLAUDE_WORKFLOWS)
def test_checkouts_do_not_persist_credentials(name):
    assert _checkouts_without_persist_false(_doc(name)) == []


@pytest.mark.parametrize("name", CLAUDE_WORKFLOWS)
def test_contents_permission_is_advisory_read(name):
    # Advisory posture: read + comment, never push. Bumping to write (e.g. "@claude fix
    # it") is a security-relevant change that must also touch this guard.
    assert _doc(name).get("permissions", {}).get("contents") == "read"


def test_review_is_comment_only_never_blocking():
    assert _comment_only_ok(_review_prompt(_doc("claude-review.yml")))


# ----- detector tests: prove the guard catches a real violation --------------


def test_detector_flags_a_tag_pinned_action():
    doc = {"jobs": {"j": {"steps": [{"uses": "actions/checkout@v4"}]}}}
    assert _unpinned_uses(doc) == ["actions/checkout@v4"]


def test_detector_passes_a_sha_pinned_action():
    sha = "0" * 40
    doc = {"jobs": {"j": {"steps": [{"uses": f"actions/checkout@{sha}"}]}}}
    assert _unpinned_uses(doc) == []


def test_detector_flags_job_without_if():
    # A job with no `if:` runs unconditionally (ungated) and would never reach _gate_ok —
    # the fail-open hole. _jobs_without_if must surface it.
    doc = {"jobs": {"gated": {"if": "x"}, "ungated": {"steps": []}}}
    assert _jobs_without_if(doc) == ["ungated"]


def test_detector_rejects_missing_association_gate():
    assert _gate_ok("github.event_name == 'issue_comment' && contains(body, '@claude')") is False


def test_detector_rejects_contributor_in_allow_list():
    gate = 'contains(fromJSON(\'["OWNER","MEMBER","COLLABORATOR","CONTRIBUTOR"]\'), x)'
    assert _gate_ok(gate) is False


def test_detector_rejects_ungated_added_event():
    # Two event branches but only one association check — the second trigger is ungated.
    gate = (
        "(github.event_name == 'issue_comment' && "
        'contains(fromJSON(\'["OWNER","MEMBER","COLLABORATOR"]\'), a)) || '
        "(github.event_name == 'issues')"
    )
    assert _gate_ok(gate) is False


def test_detector_accepts_real_gate_shape():
    gate = (
        "github.event.issue.pull_request && contains(body, '@claude review') && "
        'contains(fromJSON(\'["OWNER","MEMBER","COLLABORATOR"]\'), '
        "github.event.comment.author_association)"
    )
    assert _gate_ok(gate) is True


def test_detector_flags_checkout_persisting_credentials():
    doc = {"jobs": {"j": {"steps": [{"uses": "actions/checkout@" + "0" * 40}]}}}
    assert len(_checkouts_without_persist_false(doc)) == 1


def test_detector_flags_request_changes_review():
    assert _comment_only_ok('submit using event type "REQUEST_CHANGES"') is False


def test_detector_accepts_comment_only_review():
    assert _comment_only_ok('event type "COMMENT" (never "REQUEST_CHANGES")') is True
