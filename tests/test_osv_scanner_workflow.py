# Guard for the OSV-Scanner workflow (#320).
#
# The OSV reusable workflows must be SHA-pinned, NOT tag-pinned. GitHub allows a
# reusable-workflow ref to be a SHA, tag, or branch (SHA is the safest), so tag-pinning is
# avoidable here — and a tag-pin would (a) fail the required `zizmor` "workflow audit" check
# (unpinned-uses) and (b) re-introduce the Scorecard Pinned-Dependencies alerts that #319 just
# cleared. The OSV docs show tags, so this is an easy regression; this guard fails fast — before
# CI does. A step-level `uses:` pin guard only inspects STEP `uses:`, but a reusable workflow
# is called at the JOB level, which this test covers explicitly.

import re
from pathlib import Path

import pytest
import yaml

# Static guard over a .github/** file: also run in the always-on `lint` job, because the
# `tests` matrix is skipped on a `.github/**`-only PR (.github/actions/changed-code).
pytestmark = pytest.mark.repo_meta

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "osv-scanner.yml"
_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)  # a valid SHA may be written uppercase


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


def test_osv_is_fork_safe_trigger():
    # The whole fork-safety posture rests on plain `pull_request` (read-only token, no secrets) —
    # never `pull_request_target`. Assert BOTH the positive (PR triggering present, so the diff
    # scan actually runs) and the negative (no privileged trigger), by key membership not substring
    # (`pull_request` is a prefix of `pull_request_target`). PyYAML parses the `on:` key as True.
    doc = _doc()
    on = doc.get(True, doc.get("on", {}))
    keys = set(on) if isinstance(on, dict) else set()
    assert "pull_request" in keys, "must trigger on pull_request (the fork-safe diff scan)"
    assert "pull_request_target" not in keys, "use pull_request, never pull_request_target"


def test_osv_pr_scan_uses_the_reusable_workflow():
    # The PR (diff) scan rides the reusable PR workflow. Match the actual call-shape, not a
    # bare substring (CodeRabbit). The scheduled scan uses the reusable FULL-scan workflow —
    # see test_scheduled_scan_uses_the_reusable_full_scan_workflow.
    assert any(
        r.startswith("google/osv-scanner-action/.github/workflows/osv-scanner-reusable-pr")
        for r in _all_uses(_doc())
    )


def test_scheduled_scan_uses_the_reusable_full_scan_workflow():
    # The scheduled/full scan rides the reusable FULL-scan workflow (SHA-pinned), reverted from
    # the pinned-binary workaround once upstream v2.5.0 SHA-pinned its internal download-artifact
    # so every transitive `uses:` resolves to a full SHA and the require-SHA policy no longer
    # blocks it (#326). Anchor to the `scan-scheduled` job itself — a sweep over all jobs would
    # still pass if the ref were ever attached to `scan-pr` and the scheduled scan repointed. The
    # trailing `.yml@` excludes the PR-diff reusable (`osv-scanner-reusable-pr.yml@`).
    full_scan = "google/osv-scanner-action/.github/workflows/osv-scanner-reusable.yml@"
    ref = _doc()["jobs"].get("scan-scheduled", {}).get("uses", "")
    assert ref.startswith(full_scan), "scan-scheduled must call the reusable full-scan workflow"
    # Re-assert OUR pin locally; the require-SHA property the revert depends on is upstream's
    # transitive pins, which no test in this repo can observe.
    assert _is_pinned(ref)


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
        # Exactly these three scopes — reject any extra grant (e.g. pull-requests: write) so the
        # guard truly enforces minimal permissions, not just the presence of the required keys.
        assert set(perms) == {"contents", "security-events", "actions"}, (
            f"{name}: permissions must be exactly contents/security-events/actions"
        )
        assert perms.get("contents") == "read", f"{name}: contents must be read"
        assert perms.get("security-events") == "write", f"{name}: needs security-events: write"
        assert perms.get("actions") == "read", f"{name}: actions must be read"
