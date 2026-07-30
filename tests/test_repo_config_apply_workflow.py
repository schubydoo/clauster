# Guard for the repo-config APPLY workflow (#307) — the write half of the advisory
# drift check. This workflow holds a privileged token and mutates repo labels/settings,
# so its safety rests entirely on in-workflow invariants; these tests fail CI if any is
# silently dropped (a parse-and-assert guard over the workflow's security posture).

import json
import re
from pathlib import Path

import pytest
import yaml

# Static guard over a .github/** file: also run in the always-on `lint` job, because the
# `tests` matrix is skipped on a `.github/**`-only PR (.github/actions/changed-code).
pytestmark = pytest.mark.repo_meta

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "repo-config-apply.yml"
LABELS = ROOT / ".github" / "repo-config" / "labels.json"
_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def _doc():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on(doc):
    # PyYAML parses the bare ``on:`` key as the boolean True.
    return doc.get(True, doc.get("on"))


def test_apply_workflow_exists():
    assert WORKFLOW.is_file()


def test_apply_is_workflow_dispatch_only():
    # The load-bearing safety property: a privileged WRITE workflow must never be
    # reachable from pull_request / push / schedule — only a maintainer's manual dispatch.
    on = _on(_doc())
    keys = set(on) if isinstance(on, dict) else {on}
    assert keys == {"workflow_dispatch"}, f"write workflow must be dispatch-only, got {keys}"


def test_apply_dry_run_defaults_true_and_prune_false():
    inputs = _on(_doc())["workflow_dispatch"]["inputs"]
    # Defaults must be safe: plan-only, and never delete labels unless explicitly opted in.
    assert inputs["dry_run"]["default"] is True, "dry_run must default to true"
    assert inputs["prune"]["default"] is False, "prune (label deletion) must default to false"


def test_apply_permissions_are_minimal():
    doc = _doc()
    # Default-deny at the workflow level; the job needs only contents:read for checkout —
    # all repo-config writes go through the REPO_CONFIG_TOKEN secret, not GITHUB_TOKEN.
    assert doc.get("permissions") == {}, "set top-level `permissions: {}` (default-deny)"
    assert doc["jobs"]["apply"]["permissions"] == {"contents": "read"}


def test_apply_writes_through_the_provisioned_secret():
    # Writes must use the scoped secret token (GITHUB_TOKEN can't PATCH repo settings),
    # and the apply path must fail closed when the secret is absent.
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets.REPO_CONFIG_TOKEN" in raw
    assert "HAS_TOKEN" in raw and "cannot apply" in raw  # the fail-closed guard


def test_apply_lists_all_labels_not_just_the_default_page():
    # `gh label list` defaults to 30; the reconcile must page past that or it would
    # re-create existing labels (then abort on the duplicate) on a label-heavy repo.
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"gh label list[^\n]*--limit\s+\d{3,}", raw)


def test_apply_actions_are_sha_pinned():
    doc = _doc()
    refs = [
        s["uses"]
        for job in doc.get("jobs", {}).values()
        for s in job.get("steps", [])
        if "uses" in s
    ]
    assert refs
    unpinned = [r for r in refs if not _SHA.match(r.split("@", 1)[1] if "@" in r else "")]
    assert unpinned == [], f"unpinned uses would fail zizmor: {unpinned}"


def test_tracked_label_is_in_the_baseline():
    # The board's `tracked` label must be in the baseline, or the drift check flags it and a
    # prune apply would delete it (breaking the GitHub Project board lifecycle).
    names = {entry["name"] for entry in json.loads(LABELS.read_text(encoding="utf-8"))}
    assert "tracked" in names
