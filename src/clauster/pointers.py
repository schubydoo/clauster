"""bridge-pointer.json discovery + rediscovery (spec §7, observability source #3).

On startup Clauster walks the Anthropic-written pointer files to re-detect
bridges that are already running (e.g. survivors of a clauster restart). The
pointer's ``pid`` is the bridge **parent** (verified: a live pointer's pid is
the ``claude remote-control`` process), so it drives liveness + stop.

Rediscovery is **read-only**: a pointer yields env_id, parent pid, proc_start,
and — via its directory name — the cwd. It does NOT carry ``intentional_stop``
or ``label`` — the runner merges those from ``state.json`` (see
:mod:`clauster.state`); the pointer itself yields only live-derived facts.

The pointer directory name is Claude's sanitized cwd: every non-alphanumeric
character becomes ``-`` (verified: ``/srv/projects/my_project`` →
``-srv-projects-my-project``). That mapping is lossy, so we resolve
**forward** (project path → expected dir), never by reversing the dir name.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import procutil
from .models import BridgePointer

_log = logging.getLogger("clauster.pointers")

CLAUDE_PROJECTS_DIR = Path("~/.claude/projects").expanduser()

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")


class PointerStillLive(RuntimeError):
    """Raised when :func:`clear_pointer` is asked to drop a still-live bridge's pointer."""


def sanitize_cwd(path: Path) -> str:
    """Claude's pointer-dir name for a cwd: non-alphanumerics → ``-``."""
    return _SANITIZE_RE.sub("-", str(path))


def pointer_path_for(project_path: Path, claude_projects_dir: Path = CLAUDE_PROJECTS_DIR) -> Path:
    """Resolve the expected bridge-pointer.json path for a project (forward map)."""
    return claude_projects_dir / sanitize_cwd(project_path) / "bridge-pointer.json"


def load_pointer(path: Path) -> BridgePointer | None:
    """Parse a bridge-pointer.json, or None if missing/malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        # UnicodeDecodeError (a ValueError, not an OSError) for a non-UTF-8 file
        # must still honor the malformed -> None contract.
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


def clear_pointer(
    project_path: Path,
    *,
    claude_projects_dir: Path = CLAUDE_PROJECTS_DIR,
    backup: bool = True,
) -> bool:
    """Back up and delete a project's ``bridge-pointer.json`` so the next bridge starts cold.

    The ``claude remote-control`` CLI decides whether to reattach an existing environment
    purely from this file's presence (clauster passes no reuse flag), so removing it forces
    a clean ``Created initial session`` on the next spawn. That is the cure for #671: an
    anchor session archived/deleted out from under a *preserved* env poisons every warm
    reattach, and only a fresh env (cleared pointer) recovers.

    Returns ``True`` when a pointer file was removed, ``False`` when none existed. Refuses
    with :class:`PointerStillLive` when the pointer refers to a currently-live bridge — the
    reattach anchor must never be yanked from under a running process (Stop it first). A
    malformed pointer (no derivable liveness) is removed so a corrupt file can't wedge the
    next start. The backup (``bridge-pointer.json.bak``) is best-effort; the delete is not.
    """
    path = pointer_path_for(project_path, claude_projects_dir)
    if not path.exists():
        return False
    pointer = load_pointer(path)
    if pointer is not None and is_live(pointer):
        raise PointerStillLive(
            f"bridge-pointer for {project_path} refers to a live bridge (pid {pointer.pid})"
        )
    if backup:
        try:
            path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
        except OSError as exc:  # backup is best-effort — never let it block the delete
            _log.warning("could not back up %s before clearing: %s", path, exc)
    path.unlink()
    return True
