"""bridge-pointer.json discovery + rediscovery (spec §7, observability source #3).

On startup Clauster walks the Anthropic-written pointer files to re-detect
bridges that are already running (e.g. survivors of a clauster restart). The
pointer's ``pid`` is the bridge **parent** (verified: a live pointer's pid is
the ``claude remote-control`` process), so it drives liveness + stop.

Rediscovery is **read-only**: a pointer yields env_id, parent pid, proc_start,
and — via its directory name — the cwd. It does NOT carry ``intentional_stop``
or ``label`` (no ``state.json`` in v0.1); liveness is authoritative instead.

The pointer directory name is Claude's sanitized cwd: every non-alphanumeric
character becomes ``-`` (verified: ``/srv/projects/my_project`` →
``-srv-projects-my-project``). That mapping is lossy, so we resolve
**forward** (project path → expected dir), never by reversing the dir name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import procutil
from .models import BridgePointer

CLAUDE_PROJECTS_DIR = Path("~/.claude/projects").expanduser()

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")


def sanitize_cwd(path: Path) -> str:
    """Claude's pointer-dir name for a cwd: non-alphanumerics → ``-``."""
    return _SANITIZE_RE.sub("-", str(path))


def pointer_path_for(project_path: Path, claude_projects_dir: Path = CLAUDE_PROJECTS_DIR) -> Path:
    """Resolve the expected bridge-pointer.json path for a project (forward map)."""
    return claude_projects_dir / sanitize_cwd(project_path) / "bridge-pointer.json"


def load_pointer(path: Path) -> BridgePointer | None:
    """Parse a bridge-pointer.json, or None if missing/malformed."""
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    try:
        return BridgePointer.model_validate(data)
    except ValueError:
        return None


def pointer_for_project(
    project_path: Path, claude_projects_dir: Path = CLAUDE_PROJECTS_DIR
) -> BridgePointer | None:
    """Load the bridge pointer for a project, or None if missing/malformed."""
    return load_pointer(pointer_path_for(project_path, claude_projects_dir))


def is_live(pointer: BridgePointer) -> bool:
    """Whether the pointer refers to a currently-running, trusted bridge."""
    return procutil.is_live_bridge(pointer.pid, pointer.proc_start)
