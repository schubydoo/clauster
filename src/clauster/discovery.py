"""Project discovery (feature 1) and workspace-trust resolution.

Pure/synchronous: the app layer is responsible for running these off the event
loop via ``asyncio.to_thread`` (spec §10).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Project, TrustState

# Path-traversal defense (spec §9): a directory is only usable as a project if
# its name is a safe single path component.
PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

CLAUDE_JSON = Path("~/.claude.json").expanduser()


def is_valid_project_name(name: str) -> bool:
    return PROJECT_NAME_RE.fullmatch(name) is not None


def _load_trusted_paths(claude_json: Path) -> set[Path]:
    """Return the set of paths with hasTrustDialogAccepted: true in ~/.claude.json.

    Trust inherits *down* a tree, so the caller walks ancestors against this set.
    """
    try:
        data = json.loads(claude_json.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return set()
    trusted: set[Path] = set()
    for raw_path, entry in projects.items():
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            trusted.add(Path(raw_path))
    return trusted


def trust_state_for(path: Path, trusted: set[Path]) -> TrustState:
    """Trusted if the path or any ancestor has accepted the trust dialog."""
    resolved_trusted = {p.resolve() for p in trusted}
    candidate = path.resolve()
    for ancestor in (candidate, *candidate.parents):
        if ancestor in resolved_trusted:
            return TrustState.TRUSTED
    return TrustState.UNTRUSTED


def discover_projects(projects_root: Path, claude_json: Path = CLAUDE_JSON) -> list[Project]:
    """Scan one level under projects_root; one Project per safe-named directory."""
    trusted = _load_trusted_paths(claude_json)
    projects: list[Project] = []
    for entry in sorted(projects_root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not is_valid_project_name(entry.name):
            continue
        projects.append(
            Project(
                name=entry.name,
                path=entry,
                is_git_repo=(entry / ".git").exists(),
                has_claude_md=(entry / "CLAUDE.md").is_file(),
                trust_state=trust_state_for(entry, trusted),
            )
        )
    return projects
