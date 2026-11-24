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
import os
import re
from collections.abc import Mapping
from pathlib import Path

from . import atomicio, procutil
from .models import BridgePointer

_log = logging.getLogger("clauster.pointers")

CLAUDE_PROJECTS_DIR = Path("~/.claude/projects").expanduser()

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")

# The limits claude 2.1.330 puts on the pointer's identity fields. One bad value fails its
# schema, and the CLI then deletes the whole pointer, so a value outside them is never copied.
_POINTER_ID_RE = re.compile(r"[a-zA-Z0-9_-]{1,256}")
_POINTER_DIR_MAX = 4096

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


def _identity_fields(raw: object) -> dict[str, str]:
    """Return the valid ``dir`` and machine/account/org ids of a parsed pointer."""
    if not isinstance(raw, dict):
        return {}
    kept: dict[str, str] = {}
    folder = raw.get("dir")
    # UTF-8 bytes are never fewer than the UTF-16 units the CLI counts, so this cannot pass
    # a value the CLI's own limit refuses.
    if (
        isinstance(folder, str)
        and folder
        and len(folder.encode("utf-8", "surrogatepass")) <= _POINTER_DIR_MAX
    ):
        kept["dir"] = folder
    for key in ("machineId", "accountUuid", "organizationUuid"):
        value = raw.get(key)
        if isinstance(value, str) and _POINTER_ID_RE.fullmatch(value):
            kept[key] = value
    return kept


def reusable_identity(
    project_path: Path, *, claude_projects_dir: Path = CLAUDE_PROJECTS_DIR
) -> dict[str, str]:
    """Return the identity fields of this folder's pointer, for the pointer that replaces it.

    Force restart reads this before it deletes the old pointer. The directory name is a lossy
    map of the cwd, so the file can belong to another folder. Its identity is returned only
    when its ``dir`` is exactly ``project_path``; otherwise the result is empty.
    """
    path = pointer_path_for(project_path, claude_projects_dir)
    try:
        identity = _identity_fields(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError, RecursionError):
        return {}
    return identity if identity.get("dir") == str(project_path) else {}


def clear_pointer(
    project_path: Path,
    *,
    claude_projects_dir: Path = CLAUDE_PROJECTS_DIR,
    backup: bool = True,
    keep_mtime: bool,
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
    pointer is rewritten with an empty ``sessionId`` and no session lists. The CLI then
    re-registers WITH ``reuseEnvironmentId`` and creates a fresh initial session. Verified
    live on 2.1.280, including an environment that the ghost reaper had archived: the reuse
    revives it. The 2.1.276 and 2.1.278 binaries carry the same reuse-on-registration path
    and write ``sessionId: ""`` pointers themselves, so they read the empty anchor too; older
    versions were not inspected. An environment the server has dropped is not fatal either,
    because the CLI takes the new id it gets back.

    The rewrite keeps ``environmentId``, ``source``, ``pid``, ``procStart`` and, when the
    original has valid values, ``dir``, ``machineId``, ``accountUuid`` and
    ``organizationUuid`` (TASK-561). Read from the claude 2.1.330 bundle: the reader gives a
    standalone pointer a 30-day TTL only when those four match this folder, machine and
    account and ``procStart`` is set (and a server gate, on by default, allows it). Otherwise
    the TTL is 4 hours. The CLI also refuses to reuse a pointer whose ``dir`` or
    ``machineId`` differs, and copies the identity forward on a reuse, so dropping it would
    lose it for good. The 4-hour effect on a real Resume was not reproduced.

    A pointer that is already in that stripped shape is left untouched, with no write and
    no backup. ``keep_mtime`` puts the old mtime back after a rewrite. The stale-pointer GC
    sets it, because the CLI's TTL reads the mtime and a cleanup is not a sign of use.

    Returns ``True`` when the pointer was rewritten or removed, ``False`` when none
    existed or it was already stripped. Refuses with :class:`PointerStillLive` when the
    pointer refers to a currently-live bridge — the reattach anchor must never be yanked
    from under a running process (Stop it first). A malformed pointer (no derivable
    liveness, no trustworthy environment id) is removed so a corrupt file can't wedge the
    next start. The backup (``bridge-pointer.json.bak``) is best-effort; the rewrite or
    delete is not.
    """
    path = pointer_path_for(project_path, claude_projects_dir)
    if not path.exists():
        return False
    pointer = load_pointer(path)
    if pointer is not None and is_live(pointer):
        raise PointerStillLive(
            f"bridge-pointer for {project_path} refers to a live bridge (pid {pointer.pid})"
        )
    kept = None
    if pointer is not None and pointer.environment_id:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError, RecursionError):
            raw = None  # unreadable now -> fall through and rewrite it
        kept = {
            "sessionId": "",
            "environmentId": pointer.environment_id,
            "source": pointer.source,
            "pid": pointer.pid,
            "procStart": pointer.proc_start,
            **_identity_fields(raw),
        }
        if raw == kept:
            return False  # already stripped: nothing to clear
    mtime_ns = path.stat().st_mtime_ns if keep_mtime and kept is not None else None
    if backup:
        try:
            path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
        except OSError as exc:  # backup is best-effort — never let it block the delete
            _log.warning("could not back up %s before clearing: %s", path, exc)
    if kept is None:
        path.unlink()
        return True
    # own_dir=False: this directory (which also holds the transcripts) belongs to the CLI,
    # so the write must not tighten its mode the way clauster's own state dirs are.
    atomicio.atomic_write_text(path, json.dumps(kept), own_dir=False)
    if mtime_ns is not None:
        try:
            os.utime(path, ns=(mtime_ns, mtime_ns))
        except OSError as exc:  # the rewrite landed; only the age signal is lost
            _log.warning("could not restore the mtime of %s: %s", path, exc)
    return True


def remove_pointer(
    project_path: Path,
    *,
    claude_projects_dir: Path = CLAUDE_PROJECTS_DIR,
) -> bool:
    """Back up and delete a project's ``bridge-pointer.json``, environment id included.

    Only the Force restart path calls this, right after it archived the environment the
    pointer names. :func:`clear_pointer` keeps the environment id on purpose (#1580), but a
    kept id would let the next plain start reuse, and so revive, the archived environment and
    its old session (seen live on claude 2.1.327). A bridge started with
    ``--no-create-session-in-dir`` neither reads nor writes the pointer, so nothing else
    removes it; :func:`write_reuse_pointer` then writes one for the new bridge. Refuses
    with :class:`PointerStillLive` when the pointer names a live bridge.
    Returns ``True`` when a file was deleted, ``False`` when there was none.
    """
    path = pointer_path_for(project_path, claude_projects_dir)
    if not path.exists():
        return False
    pointer = load_pointer(path)
    if pointer is not None and is_live(pointer):
        raise PointerStillLive(
            f"bridge-pointer for {project_path} refers to a live bridge (pid {pointer.pid})"
        )
    try:
        path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
    except OSError as exc:  # backup is best-effort — never let it block the delete
        _log.warning("could not back up %s before removing: %s", path, exc)
    path.unlink()
    return True


def write_reuse_pointer(
    project_path: Path,
    *,
    environment_id: str,
    pid: int,
    proc_start: str,
    identity: Mapping[str, str],
    claude_projects_dir: Path = CLAUDE_PROJECTS_DIR,
) -> bool:
    """Write the stripped pointer for a bridge that runs without one (Force restart).

    A bridge started with ``--no-create-session-in-dir`` neither reads nor writes
    ``bridge-pointer.json`` (claude 2.1.327). With no pointer, a clauster restart cannot
    reattach it as RUNNING (the connect facts come from a live pointer), and a later plain
    start cannot reuse its environment, so the server refuses the new registration as
    "already served" when the CLI kept the environment. This writes the same shape
    :func:`clear_pointer` leaves, with an empty ``sessionId``. ``proc_start`` is the bridge's
    boot-relative start ticks, the value the CLI writes. ``identity`` comes from
    :func:`reusable_identity` and is empty when the old pointer had none for this folder.

    Never overwrites: an existing pointer, live or not, is left alone and ``False`` is
    returned. ``True`` means the pointer was written.
    """
    path = pointer_path_for(project_path, claude_projects_dir)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "sessionId": "",
        "environmentId": environment_id,
        "source": "standalone",
        "pid": pid,
        "procStart": proc_start,
        **identity,
    }
    # own_dir=False: the directory belongs to the CLI (it also holds the transcripts).
    atomicio.atomic_write_text(path, json.dumps(body), own_dir=False)
    return True
