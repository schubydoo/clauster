# Guard for the jactionlint workflow, which replaced actionlint.yml and the zizmor job of
# security.yml.
#
# The `jactionlint` job is a REQUIRED status check, so it must report on every pull request
# (no `paths:` filter, `synchronize` in the types). It uploads SARIF to code scanning with the
# same safety design the zizmor job had: the job token stays `contents: read`, the linter runs
# with a read-only App token, and a SEPARATE App token with `security-events: write` goes only
# to the upload step, so the linter binary never holds a write scope. Both mints and the
# upload are same-repo only, because a fork PR has no App secrets and cannot write code
# scanning results.

import json
from pathlib import Path

import pytest
import yaml

# Static guard over a .github/** file: also run in the always-on `lint` job, because the
# `tests` matrix is skipped on a `.github/**`-only PR (.github/actions/changed-code).
pytestmark = pytest.mark.repo_meta

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "jactionlint.yml"
CONFIG = REPO / ".github" / "jactionlint.yaml"
RULESET = REPO / ".github" / "rulesets" / "main.json"

SAME_REPO = "github.event.pull_request.head.repo.full_name == github.repository"
NOT_PR = "github.event_name != 'pull_request'"


def _doc():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job():
    return _doc()["jobs"]["jactionlint"]


def _step(name):
    for step in _job()["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"jactionlint.yml: expected a `{name}` step")


def _cond(step):
    return " ".join(str(step.get("if", "")).split())


def test_job_name_is_the_required_check_name():
    # The ruleset requires the context `jactionlint`, which is the job's `name:`.
    assert _job().get("name") == "jactionlint"


def test_runs_on_every_pull_request_without_a_paths_filter():
    # A required check behind a `paths:` filter never reports on a PR that skips it, and
    # GitHub waits for it forever ("Expected — waiting for status").
    doc = _doc()
    on = doc.get(True, doc.get("on"))
    pull_request = on["pull_request"] or {}
    assert "paths" not in pull_request and "paths-ignore" not in pull_request
    assert {"opened", "synchronize", "reopened", "ready_for_review"} <= set(pull_request["types"])
    assert "pull_request_target" not in on, "use pull_request, never pull_request_target"
    assert on["push"] == {"branches": ["main"]}


def test_job_token_is_read_only_and_checkout_does_not_persist_it():
    doc = _doc()
    assert doc.get("permissions") == {"contents": "read"}
    assert _job().get("permissions") == {"contents": "read"}
    checkout = _job()["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["persist-credentials"] is False


def test_linter_gets_the_read_token_and_upload_gets_the_write_token():
    audit = _step("Mint GitHub App token")
    assert audit["with"].get("permission-contents") == "read"
    assert not [
        k for k, v in audit["with"].items() if k.startswith("permission-") and v == "write"
    ]
    upload_mint = _step("Mint SARIF upload token")
    writes = [
        k for k, v in upload_mint["with"].items() if k.startswith("permission-") and v == "write"
    ]
    assert writes == ["permission-security-events"], f"least-privilege upload scope: {writes}"
    for name in ("Write SARIF", "Run jactionlint"):
        token = _step(name)["env"]["GH_TOKEN"]
        assert "steps.app-token.outputs.token" in token, f"{name}: use the read-only App token"
        assert "sarif-token" not in token, f"{name}: the linter must never hold the write token"
    assert _step("Upload SARIF")["env"]["GH_TOKEN"] == "${{ steps.sarif-token.outputs.token }}"


def test_both_tokens_are_minted_before_the_lint_runs():
    # The gate exits nonzero on findings. A mint after it (implicit success()) would be skipped,
    # leaving the !cancelled() upload with no token.
    names = [s.get("name") for s in _job()["steps"]]
    first_lint = min(names.index("Write SARIF"), names.index("Run jactionlint"))
    assert names.index("Mint GitHub App token") < first_lint
    assert names.index("Mint SARIF upload token") < first_lint


def test_mints_and_upload_are_same_repo_only():
    for name in (
        "Mint GitHub App token",
        "Mint SARIF upload token",
        "Write SARIF",
        "Upload SARIF",
    ):
        cond = _cond(_step(name))
        assert SAME_REPO in cond, f"{name}: must carry the same-repo guard for fork safety"
        assert NOT_PR in cond, f"{name}: must let push and schedule through the guard"


def test_upload_runs_after_findings_and_only_after_a_good_sarif():
    cond = _cond(_step("Upload SARIF"))
    assert cond.startswith("${{ !cancelled()"), "a findings run is exactly the one to upload"
    assert "steps.sarif.outcome == 'success'" in cond
    assert _step("Write SARIF").get("id") == "sarif"


def test_upload_posts_to_the_public_endpoint_with_its_own_category():
    run = _step("Upload SARIF")["run"]
    assert "code-scanning/sarifs" in run
    # The trailing slash is load-bearing: GitHub parses the id as `category/run-id`.
    assert '.runs[].automationDetails.id = "jactionlint/"' in run
    assert "tool_name=jactionlint" in run
    uses = [s.get("uses", "") for s in _job()["steps"]]
    assert not any("codeql-action/upload-sarif" in u for u in uses)


def test_both_runs_are_strict_online_and_require_shellcheck():
    gate = " ".join(_step("Run jactionlint")["run"].split())
    sarif = " ".join(_step("Write SARIF")["run"].split())
    assert "--format github" in gate and "--strict-exit" in gate
    assert "--format sarif" in sarif
    for run in (gate, sarif):
        # strict: a skipped lookup (rate limit, bad token) fails the run instead of passing.
        assert "--online=strict --online-token-env GH_TOKEN" in run
        assert "--shellcheck shellcheck" in run


def test_pre_commit_pin_matches_the_workflow_pin():
    version = _step("Install jactionlint")["env"]["JACTIONLINT_VERSION"]
    hooks = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    entries = [
        hook["entry"]
        for repo in hooks["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "jactionlint"
    ]
    assert entries == [f"uvx --from jactionlint=={version} jactionlint --strict-exit --no-online"]


def test_config_required_checks_match_the_ruleset():
    # unreachable-required-check only guards the names listed in the config, so the list must
    # be the ruleset's list.
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    listed = [entry["name"] for entry in config["required-checks"]]
    ruleset = json.loads(RULESET.read_text(encoding="utf-8"))
    required = [
        ctx["context"]
        for rule in ruleset["rules"]
        if rule["type"] == "required_status_checks"
        for ctx in rule["parameters"]["required_status_checks"]
    ]
    assert sorted(listed) == sorted(required)
    assert "jactionlint" in required
    for entry in config["required-checks"]:
        assert entry.get("branches") == ["main"], entry


def test_actionlint_and_zizmor_jobs_are_gone():
    workflows = REPO / ".github" / "workflows"
    assert not (workflows / "actionlint.yml").exists()
    assert not (REPO / ".github" / "actionlint.yaml").exists()
    security = yaml.safe_load((workflows / "security.yml").read_text(encoding="utf-8"))
    assert "zizmor" not in security["jobs"]
    needs = security["jobs"]["security-required-checks-passed"]["needs"]
    assert "zizmor" not in needs
