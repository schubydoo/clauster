# Guard for the OSV-Scanner workflow (#320, #1469).
#
# Every `uses:` must be SHA-pinned, NOT tag-pinned: a tag-pin would (a) fail the required
# `zizmor` "workflow audit" check (unpinned-uses) and (b) re-introduce the Scorecard
# Pinned-Dependencies alerts that #319 cleared. The upstream OSV docs show tags, so this is an
# easy regression; this guard fails fast — before CI does.
#
# The SARIF upload must go through the PUBLIC `code-scanning/sarifs` endpoint with a minted
# clauster-ci App token, never `github/codeql-action/upload-sarif` on the shared GITHUB_TOKEN
# pool (#1469). The reusable `google/osv-scanner-action` workflows own that shared-pool upload
# internally, so this also guards that none is called any more.

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


def _steps(doc, job_name):
    return doc.get("jobs", {}).get(job_name, {}).get("steps", [])


def _step_by_name(doc, job_name, needle):
    for step in _steps(doc, job_name):
        if needle.lower() in str(step.get("name", "")).lower():
            return step
    return None


def _is_pinned(ref):
    return (
        ref.startswith(".") or _SHA.match(ref.split("@", 1)[1] if "@" in ref else "") is not None
    )


def test_osv_scanner_workflow_exists():
    assert WORKFLOW.is_file(), "osv-scanner.yml missing — a rename/delete must be deliberate"


def test_osv_uses_are_sha_pinned():
    refs = _all_uses(_doc())
    assert refs, "expected at least the checkout + app-token step `uses:`"
    unpinned = [r for r in refs if not _is_pinned(r)]
    assert unpinned == [], f"tag-pinned uses would fail zizmor + Scorecard: {unpinned}"


def test_osv_is_fork_safe_trigger():
    # The whole fork-safety posture rests on plain `pull_request` (read-only token, no secrets) —
    # never `pull_request_target`. Assert BOTH the positive (PR triggering present, so the scan
    # actually runs) and the negative (no privileged trigger), by key membership not substring
    # (`pull_request` is a prefix of `pull_request_target`). PyYAML parses the `on:` key as True.
    doc = _doc()
    on = doc.get(True, doc.get("on", {}))
    keys = set(on) if isinstance(on, dict) else set()
    assert "pull_request" in keys, "must trigger on pull_request (the fork-safe scan)"
    assert "pull_request_target" not in keys, "use pull_request, never pull_request_target"


def test_osv_does_not_call_the_reusable_action():
    # #1469: the reusable `google/osv-scanner-action` workflows own a `codeql-action/upload-sarif`
    # step that posts on the shared GITHUB_TOKEN pool. Replacing them with the pinned binary +
    # public-endpoint upload is the whole point; a revert to the reusable call would silently put
    # the upload back on the shared pool, so guard it out explicitly.
    reusable = [r for r in _all_uses(_doc()) if r.startswith("google/osv-scanner-action")]
    assert reusable == [], f"reusable OSV workflow re-uploads on GITHUB_TOKEN (#1469): {reusable}"


def test_osv_installs_the_pinned_checksum_verified_binary():
    # The scan runs the pinned OSV-Scanner BINARY (not the reusable workflow) so the upload can be
    # split out. The binary is pinned by version AND verified by sha256 — a fail-loud check, never
    # a silent skip if the checksum drifts on a Renovate bump.
    install = _step_by_name(_doc(), "scan", "Install OSV-Scanner")
    assert install is not None, "expected an `Install OSV-Scanner` step"
    env = install.get("env", {})
    assert env.get("OSV_VERSION"), "pin the release via OSV_VERSION"
    assert re.fullmatch(r"[0-9a-f]{64}", str(env.get("OSV_SHA256", ""))), "pin OSV_SHA256 (sha256)"
    assert "sha256sum -c" in install.get("run", ""), "verify the download with `sha256sum -c`"


def test_osv_uploads_through_the_public_endpoint_not_codeql_action():
    # #1469: post to the PUBLIC `code-scanning/sarifs` endpoint with the minted App token, off the
    # shared GITHUB_TOKEN pool. `codeql-action/upload-sarif` cannot take an App token
    # (codeql-action#2719), so its absence here is load-bearing, not incidental.
    doc = _doc()
    assert not any("codeql-action/upload-sarif" in r for r in _all_uses(doc)), (
        "upload-sarif rides GITHUB_TOKEN and rejects the App token (#1469)"
    )
    upload = _step_by_name(doc, "scan", "Upload SARIF")
    assert upload is not None, "expected an `Upload SARIF` step"
    run = upload.get("run", "")
    assert "code-scanning/sarifs" in run, "POST to the public code-scanning/sarifs endpoint"
    assert upload.get("env", {}).get("GH_TOKEN") == "${{ steps.sarif-token.outputs.token }}", (
        "the upload must authenticate with the minted App token, not GITHUB_TOKEN"
    )


def test_osv_upload_category_has_a_trailing_slash():
    # GitHub parses `automationDetails.id` as `category/run-id`: an id with NO trailing slash
    # records an EMPTY category and starts a fresh alert series. The slash is load-bearing.
    upload = _step_by_name(_doc(), "scan", "Upload SARIF")
    assert upload is not None
    assert 'automationDetails.id = "osv-scanner/"' in upload.get("run", ""), (
        "set the category WITH a trailing slash so re-uploads supersede the series"
    )


def test_osv_mints_an_upload_only_app_token():
    # The upload's `security-events: write` comes from a minted clauster-ci App token, granted the
    # LEAST privilege the upload needs — not from the job token, which stays read-only.
    mint = _step_by_name(_doc(), "scan", "Mint SARIF upload token")
    assert mint is not None, "expected a `Mint SARIF upload token` step"
    assert mint.get("uses", "").startswith("actions/create-github-app-token@"), (
        "mint via actions/create-github-app-token"
    )
    with_ = mint.get("with", {})
    assert with_.get("client-id") == "${{ secrets.CLAUSTER_CI_APP_CLIENT_ID }}"
    assert with_.get("private-key") == "${{ secrets.CLAUSTER_CI_APP_PRIVATE_KEY }}"
    assert with_.get("permission-security-events") == "write", "least-privilege upload scope"
    # No write scope beyond what the SARIF upload needs (issue 1469 requirement 4).
    extra_writes = [k for k, v in with_.items() if k.startswith("permission-") and v == "write"]
    assert extra_writes == ["permission-security-events"], f"no extra write grants: {extra_writes}"


def test_osv_mint_and_upload_are_gated_same_repo():
    # A fork PR lacks the App secrets and cannot write code-scanning results, so BOTH the token
    # mint and the SARIF upload must be gated same-repo — the `head.repo.full_name == repository`
    # conjunct (OR event != pull_request for push/schedule/dispatch). Without it a fork PR would
    # try to mint / upload and fail, or worse expose the flow to fork code. Guard both steps.
    doc = _doc()
    same_repo = "github.event.pull_request.head.repo.full_name == github.repository"
    not_pr = "github.event_name != 'pull_request'"
    for step_name in ("Mint SARIF upload token", "Upload SARIF"):
        step = _step_by_name(doc, "scan", step_name)
        assert step is not None, f"expected a `{step_name}` step"
        cond = step.get("if", "")
        assert same_repo in cond, f"{step_name}: must carry the same-repo guard for fork safety"
        assert not_pr in cond, f"{step_name}: must let push/schedule/dispatch through the guard"


def test_osv_permissions_are_minimal():
    # Explicit workflow-level default-deny (`permissions: {}`) so we never inherit the broad
    # default token (zizmor excessive-permissions). The job token now needs only `contents: read`
    # (checkout + the lockfile reads + the release download); the SARIF upload rides the minted
    # App token, so the job token never carries `security-events: write`.
    doc = _doc()
    assert doc.get("permissions") == {}, "set top-level `permissions: {}` (default-deny)"
    jobs = doc.get("jobs", {})
    assert jobs
    for name, job in jobs.items():
        perms = job.get("permissions", {})
        assert set(perms) == {"contents"}, f"{name}: the job token needs only contents: read"
        assert perms.get("contents") == "read", f"{name}: contents must be read"
