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
    # The PR (diff) scan still rides the reusable PR workflow. Match the actual call-shape, not a
    # bare substring (CodeRabbit). The scheduled scan deliberately does NOT use the reusable
    # full-scan workflow — see test_scheduled_scan_pins_and_verifies_the_osv_binary.
    assert any(
        r.startswith("google/osv-scanner-action/.github/workflows/osv-scanner-reusable-pr")
        for r in _all_uses(_doc())
    )


def test_scheduled_scan_pins_and_verifies_the_osv_binary():
    # The scheduled scan runs the OSV-Scanner BINARY itself (the reusable full-scan workflow pulls
    # a tag-pinned download-artifact the repo's require-SHA policy rejects). Lock the safety
    # properties: a pinned version, a sha256 gate, the v2 invocation, and Renovate tracking.
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "OSV_VERSION:" in raw and "OSV_SHA256:" in raw
    assert "sha256sum -c" in raw  # the binary is checksum-verified before it runs
    assert "scan source" in raw  # the OSV-Scanner v2 invocation
    assert "renovate: datasource=github-releases depName=google/osv-scanner" in raw


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
