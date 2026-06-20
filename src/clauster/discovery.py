"""Project discovery (feature 1) and workspace-trust resolution.

Pure/synchronous: the app layer is responsible for running these off the event
loop via ``asyncio.to_thread`` (spec §10).
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .models import Project, TrustState

# Path-traversal defense (spec §9): a directory is only usable as a project if
# its name is a safe single path component.
PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

CLAUDE_JSON = Path("~/.claude.json").expanduser()

# Discovery is hit on every /api/projects (4s poll) and many runner paths; an
# uncached scan is iterdir + 3 stat probes/dir + a full ~/.claude.json parse, and
# the first-paint preflight fan-out multiplies it. The cache below collapses the
# repeated scans within a short window while staying correct: it invalidates on a
# short TTL *and* on a change to either the projects_root directory mtime (a
# project added/removed) or the ~/.claude.json mtime (a trust change), so a bridge
# or trust state appearing/disappearing is never served stale past the TTL.
DISCOVERY_CACHE_TTL_SECONDS = 2.0


def is_valid_project_name(name: str) -> bool:
    """Whether ``name`` is a safe single path component (path-traversal defense)."""
    return PROJECT_NAME_RE.fullmatch(name) is not None


def _load_trusted_paths(claude_json: Path) -> set[Path]:
    """Return the set of paths with hasTrustDialogAccepted: true in ~/.claude.json.

    Trust inherits *down* a tree, so the caller walks ancestors against this set.
    """
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError, RecursionError):
        # A non-UTF-8 file (UnicodeDecodeError, a ValueError) or deeply-nested JSON
        # (RecursionError, which CPython's recursive scanner raises before it can
        # raise JSONDecodeError) degrades to the same "nothing trusted" result as any
        # other malformed claude.json — the contract is to never raise.
        return set()
    # A valid-JSON-but-non-dict top level (e.g. `[]`, `"x"`, `5`) parses fine but
    # has no `.get` — degrade it to "nothing trusted" like any other malformed file.
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return set()
    trusted: set[Path] = set()
    for raw_path, entry in projects.items():
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            trusted.add(Path(raw_path))
    return trusted


def _trust_state_against_resolved(candidate: Path, resolved_trusted: set[Path]) -> TrustState:
    """Trust state for an already-``resolve()``-d path against pre-resolved trusted paths.

    The trusted set is resolved once by the caller so a discovery scan does not
    re-``resolve()`` the whole set per project (``O(N×M)`` syscalls otherwise).
    """
    for ancestor in (candidate, *candidate.parents):
        if ancestor in resolved_trusted:
            return TrustState.TRUSTED
    return TrustState.UNTRUSTED


def trust_state_for(path: Path, trusted: set[Path]) -> TrustState:
    """Trusted if the path or any ancestor has accepted the trust dialog."""
    return _trust_state_against_resolved(path.resolve(), {p.resolve() for p in trusted})


def discover_projects(projects_root: Path, claude_json: Path = CLAUDE_JSON) -> list[Project]:
    """Scan one level under projects_root; one Project per safe-named directory."""
    # Resolve the trusted set once for the whole scan, not once per project.
    resolved_trusted = {p.resolve() for p in _load_trusted_paths(claude_json)}
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
                has_claude_dir=(entry / ".claude").is_dir(),
                trust_state=_trust_state_against_resolved(entry.resolve(), resolved_trusted),
            )
        )
    return projects


def _path_mtime(path: Path) -> float:
    """Best-effort mtime for cache invalidation; missing/unreadable → ``-1.0``.

    A vanished or unreadable path is treated as a distinct ``-1.0`` "stamp" so the
    cache invalidates when it (re)appears rather than serving a stale scan.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


class _DiscoveryCache:
    """Process-wide TTL + mtime cache for :func:`discover_projects`.

    Invalidates a cached project list when the TTL lapses **or** when either the
    ``projects_root`` directory mtime (project added/removed) or the
    ``claude_json`` mtime (trust change) moves. The short TTL backstops in-project
    changes a directory mtime can miss (e.g. a ``.git`` dir appearing). Thread-safe:
    discovery is run from ``asyncio.to_thread`` worker threads.
    """

    def __init__(self, ttl_seconds: float = DISCOVERY_CACHE_TTL_SECONDS) -> None:
        """Create an empty cache with the given freshness window (seconds)."""
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        # key -> (expires_at, root_mtime, claude_json_mtime, projects)
        self._entries: dict[tuple[str, str], tuple[float, float, float, list[Project]]] = {}

    def get(self, projects_root: Path, claude_json: Path) -> list[Project]:
        """Return cached projects when fresh, else rescan, cache, and return.

        Hands back a fresh list of shallow ``Project`` copies on every call, so a
        caller that mutates a returned project (e.g. the app layer stamping
        ``allow_bypass_permissions``) never writes into the cached objects — the
        cache stays a pure snapshot of the filesystem scan.
        """
        key = (str(projects_root), str(claude_json))
        root_mtime = _path_mtime(projects_root)
        json_mtime = _path_mtime(claude_json)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if (
                entry is not None
                and now < entry[0]
                and entry[1] == root_mtime
                and entry[2] == json_mtime
            ):
                return [p.model_copy() for p in entry[3]]
        # Scan outside the lock — the fs work must not serialize concurrent callers
        # for *different* roots, and a duplicate scan on a race is harmless.
        projects = discover_projects(projects_root, claude_json)
        with self._lock:
            self._entries[key] = (now + self._ttl, root_mtime, json_mtime, projects)
        return [p.model_copy() for p in projects]

    def clear(self) -> None:
        """Drop all cached entries (used by tests and after a mutating trust write)."""
        with self._lock:
            self._entries.clear()


_DISCOVERY_CACHE = _DiscoveryCache()


def discover_projects_cached(
    projects_root: Path, claude_json: Path = CLAUDE_JSON
) -> list[Project]:
    """Return :func:`discover_projects` through a short TTL + mtime-invalidated cache.

    Use on hot read paths (the ``/api/projects`` poll, the runner's ``_discovered``
    helper, first-paint preflight) where a sub-TTL-stale project list is acceptable.
    Callers that must see a mutation immediately should call :func:`discover_projects`
    directly or :func:`invalidate_discovery_cache` first.
    """
    return _DISCOVERY_CACHE.get(projects_root, claude_json)


def invalidate_discovery_cache() -> None:
    """Drop the discovery cache so the next read rescans (after a trust write etc.)."""
    _DISCOVERY_CACHE.clear()
