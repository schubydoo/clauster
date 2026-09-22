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

from . import atomicio, procutil
from .models import BridgePointer

_log = logging.getLogger("clauster.pointers")

CLAUDE_PROJECTS_DIR = Path("~/.claude/projects").expanduser()

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")

# Where `claude remote-control --spawn worktree` places each session's git worktree,
# relative to the project root. Lives here — beside ``sanitize_cwd`` — because two
# unrelated consumers must agree on it: ``inspector`` attributes a session to a bridge by
# containment in this subtree, and ``usage`` finds worktree transcripts by the sanitized
# form of it. A second copy would drift and silently resurrect #1020.
WORKTREE_SUBDIR = Path(".claude") / "worktrees"


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
    except (ValueError, OSError, RecursionError) as exc:
        # ValueError covers three distinct failures, only one of which is a decode
        # error: UnicodeDecodeError for a non-UTF-8 file, JSONDecodeError for bad
        # syntax, and a *bare* ValueError from the base-10 integer-string-conversion
        # limit (CVE-2020-10735), on by default for a >4300-digit int literal on every
        # supported interpreter (>=3.11). RecursionError is neither a ValueError nor an
        # OSError: the recursive scanner overflows on deeply-nested JSON and raises
        # RecursionError on every supported interpreter (the message changed from "maximum
        # recursion depth exceeded" on <=3.13 to "Stack overflow" on 3.14+, but not the
        # type), so without this arm it escapes and propagates out of the
        # documented malformed -> None contract. `usage` catches the same
        # (JSONDecodeError, ValueError, RecursionError) trio at all four of its
        # json.loads sites (PR 1372); OSError is this seam's own, because the read
        # happens inside the same try.
        #
        # The exception is in the message because these are not one failure: an OSError
        # is an unreadable file (permissions, IO), not malformed content, and it is the
        # arm actually worth diagnosing. They all degrade the same way, so the
        # distinction can only ever live in the log. Formatted with `str`, never `repr`:
        # `UnicodeDecodeError.args[1]` is the whole decoded buffer, so `%r` would put the
        # entire file on one log line (measured: 420 KB for a 200 KB pointer), while `str`
        # gives the bounded "codec can't decode byte 0xff in position N" form.
        _log.debug(
            "ignoring unreadable or malformed bridge pointer %s: %s: %s",
            path,
            type(exc).__name__,
            exc,
        )
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
    """Whether the pointer's pid is alive with a matching start time and bridge cmdline."""
    return procutil.is_live_bridge(pointer.pid, pointer.proc_start)


def clear_pointer(
    project_path: Path,
    *,
    claude_projects_dir: Path = CLAUDE_PROJECTS_DIR,
    backup: bool = True,
    keep_environment: bool = True,
) -> bool:
    """Back up a project's ``bridge-pointer.json`` and drop its session anchor.

    The ``claude remote-control`` CLI decides whether to reattach an existing environment
    purely from this file (clauster passes no reuse flag). An anchor session archived or
    deleted out from under a *preserved* env poisons every warm reattach (#671), so the
    next spawn must not re-adopt it and must log a fresh ``Created initial session``.

    The environment id is KEPT (#1580). Since claude 2.1.280 a SIGINT'd ``same-dir``
    bridge skips archive+deregister, and the server then refuses a fresh registration
    for the same folder with a 409 ("already served by a terminal ``claude
    remote-control``"). Deleting the whole file therefore wedged every restart. The
    pointer is rewritten to the environment fields only, with an empty ``sessionId`` and
    no session lists. The CLI then re-registers WITH ``reuseEnvironmentId`` and creates a
    fresh initial session. Verified live on 2.1.280 only, including an environment that
    the ghost reaper had archived: the reuse revives it. An older CLI is assumed to read
    the empty ``sessionId`` as no anchor, as it already writes one itself. An environment
    the server has dropped is not fatal either, because the CLI takes the new id it gets
    back.

    ``keep_environment=False`` deletes the whole file instead. The stale-pointer GC uses
    it: a rewrite resets the mtime its TTL reads, so a kept pointer would never age out.

    Returns ``True`` when the pointer was rewritten or removed, ``False`` when none
    existed. Refuses with :class:`PointerStillLive` when the pointer refers to a
    currently-live bridge — the reattach anchor must never be yanked from under a running
    process (Stop it first). A malformed pointer (no derivable liveness, no trustworthy
    environment id) is removed so a corrupt file can't wedge the next start. The backup
    (``bridge-pointer.json.bak``) is best-effort; the rewrite or delete is not.
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
    if not keep_environment or pointer is None or not pointer.environment_id:
        path.unlink()
        return True
    kept = {
        "sessionId": "",
        "environmentId": pointer.environment_id,
        "source": pointer.source,
        "pid": pointer.pid,
        "procStart": pointer.proc_start,
    }
    # own_dir=False: this directory (which also holds the transcripts) belongs to the CLI,
    # so the write must not tighten its mode the way clauster's own state dirs are.
    atomicio.atomic_write_text(path, json.dumps(kept), own_dir=False)
    return True
