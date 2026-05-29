from __future__ import annotations

import json
from pathlib import Path

from clauster.discovery import (
    discover_projects,
    is_valid_project_name,
    trust_state_for,
)
from clauster.models import TrustState


def test_is_valid_project_name():
    assert is_valid_project_name("dockerize2")
    assert is_valid_project_name("my-project_1")
    assert not is_valid_project_name("bad name!")
    assert not is_valid_project_name("../escape")
    assert not is_valid_project_name("")
    assert not is_valid_project_name("x" * 65)


def test_discovers_directories_and_badges(projects_root, tmp_path):
    # No trust file -> everything untrusted
    projects = discover_projects(projects_root, claude_json=tmp_path / "nope.json")
    names = [p.name for p in projects]
    assert names == ["alpha", "beta", "gamma"]  # sorted, dotdir + bad-name skipped

    by_name = {p.name: p for p in projects}
    assert by_name["alpha"].is_git_repo is True
    assert by_name["alpha"].has_claude_md is False
    assert by_name["beta"].has_claude_md is True
    assert by_name["gamma"].is_git_repo is False


def test_trust_inherits_down_tree(tmp_path):
    claude_json = tmp_path / ".claude.json"
    root = tmp_path / "projects"
    child = root / "svc"
    child.mkdir(parents=True)
    claude_json.write_text(
        json.dumps({"projects": {str(root): {"hasTrustDialogAccepted": True}}})
    )
    trusted = {root}
    assert trust_state_for(child, trusted) is TrustState.TRUSTED


def test_untrusted_when_no_ancestor(tmp_path):
    target = tmp_path / "projects" / "svc"
    target.mkdir(parents=True)
    assert trust_state_for(target, set()) is TrustState.UNTRUSTED


def test_discover_sets_trust_from_claude_json(projects_root, tmp_path):
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps({"projects": {str(projects_root): {"hasTrustDialogAccepted": True}}})
    )
    projects = discover_projects(projects_root, claude_json=claude_json)
    assert all(p.trust_state is TrustState.TRUSTED for p in projects)
