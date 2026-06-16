# Guard for the OSV-Scanner workflow (#320).
#
# The OSV reusable workflows must be SHA-pinned, NOT tag-pinned. GitHub allows a
# reusable-workflow ref to be a SHA, tag, or branch (SHA is the safest), so tag-pinning is
# avoidable here — and a tag-pin would (a) fail the required `zizmor` "workflow audit" check
# (unpinned-uses) and (b) re-introduce the Scorecard Pinned-Dependencies alerts that #319 just
# cleared. The OSV docs show tags, so this is an easy regression; this guard fails fast — before
# CI does. The repo's other pin guard (test_workflow_guards) only inspects STEP `uses:`, but a
# reusable workflow is called at the JOB level, which this test covers explicitly.

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "osv-scanner.yml"
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _doc():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _all_uses(doc):
    """Collect every `uses:` — job-level (reusable-workflow calls) AND step-level."""
    refs = []
    for job in doc.get("jobs", {}).values():
        if "uses" in job:  # a reusable-workflow call lives at the job level
            refs.append(job["uses"])
        for step in job.get("steps", []):
            if "uses" in step:
                refs.append(step["uses"])
    return refs


def _is_pinned(ref):
    return (
        ref.startswith(".") or _SHA.match(ref.split("@", 1)[1] if "@" in ref else "") is not None
    )


def test_osv_scanner_workflow_exists():
    assert WORKFLOW.is_file(), "osv-scanner.yml missing — a rename/delete must be deliberate"


def test_osv_uses_are_sha_pinned():
    refs = _all_uses(_doc())
    assert refs, "expected at least the OSV reusable-workflow call"
    unpinned = [r for r in refs if not _is_pinned(r)]
    assert unpinned == [], f"tag-pinned uses would fail zizmor + Scorecard: {unpinned}"


def test_osv_calls_the_reusable_workflow():
    assert any("google/osv-scanner-action" in r for r in _all_uses(_doc()))


def test_osv_permissions_are_minimal():
    # Explicit workflow-level default-deny (`permissions: {}`) so we never inherit the broad
    # default token (zizmor excessive-permissions), then grant least privilege PER calling job:
    # read-only except the SARIF upload; never contents: write.
    doc = _doc()
    assert doc.get("permissions") == {}, "set top-level `permissions: {}` (default-deny)"
    jobs = doc.get("jobs", {})
    assert jobs
    for name, job in jobs.items():
        perms = job.get("permissions", {})
        assert perms.get("contents") == "read", f"{name}: contents must be read"
        assert perms.get("security-events") == "write", f"{name}: needs security-events: write"
        assert perms.get("actions") == "read", f"{name}: actions must be read"
