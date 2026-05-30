"""CLAUDE.md viewer/editor (spec §5: "CLAUDE.md viewer + editor").

Reads/writes a project's *root* CLAUDE.md. Editing only writes a file — it never
executes anything (the bridge runs CLAUDE.md only when the dir is trusted and a
session spawns), so no trust gate is needed here. But because the target lives
inside an operator-named project dir, the path is locked to exactly
``<project>/CLAUDE.md`` and rejected if it resolves outside that dir (symlink or
traversal). Writes are atomic (temp + ``os.replace``, the same idiom as
``trust.py``) and appended to an audit log. A 64 KB cap matches the spec.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .models import ClaudeMdDoc

FILENAME = "CLAUDE.md"
MAX_BYTES = 64 * 1024  # 64 KB cap (spec §5)
_AUDIT_FILE = "claude_md_audit.log"


class ClaudeMdError(RuntimeError):
    """Base for read/write failures the app maps to HTTP 4xx."""


class ClaudeMdTooLarge(ClaudeMdError):
    pass


class ClaudeMdConflict(ClaudeMdError):
    """On-disk content changed since the editor loaded it (lost-update guard)."""


class ClaudeMdPathError(ClaudeMdError):
    """The resolved CLAUDE.md path escapes the project directory."""


def _target(project_path: Path) -> Path:
    """The locked target path, guaranteed to sit directly inside the project dir.

    Resolving catches a symlinked CLAUDE.md that points out of the tree: the real
    path's parent must be the (resolved) project dir and its name must be CLAUDE.md.
    A not-yet-existing file resolves to ``<project>/CLAUDE.md`` and passes.
    """
    base = project_path.resolve()
    target = base / FILENAME
    resolved = target.resolve()
    if resolved.parent != base or resolved.name != FILENAME:
        raise ClaudeMdPathError(f"{FILENAME} resolves outside the project directory")
    return target


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_claude_md(project_path: Path) -> ClaudeMdDoc:
    target = _target(project_path)
    try:
        content = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ClaudeMdDoc(exists=False, content="", sha256=None, size=0)
    except UnicodeDecodeError as exc:
        raise ClaudeMdError(f"{FILENAME} is not valid UTF-8") from exc
    return ClaudeMdDoc(
        exists=True,
        content=content,
        sha256=_sha256(content),
        size=len(content.encode("utf-8")),
    )


def write_claude_md(
    project_path: Path,
    content: str,
    *,
    base_sha256: str | None = None,
    state_dir: Path | None = None,
    user: str = "?",
) -> ClaudeMdDoc:
    """Atomically write CLAUDE.md, enforcing the size cap and (optional) lost-update guard.

    ``base_sha256`` is the hash the editor loaded. When provided, the current
    on-disk hash must match (None == absent) or the write is refused as a conflict;
    omit it to create a brand-new file or to force last-write-wins.
    """
    target = _target(project_path)
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ClaudeMdTooLarge(
            f"{FILENAME} is {len(encoded)} bytes, over the {MAX_BYTES} byte cap"
        )

    current = read_claude_md(project_path)
    if base_sha256 is not None and current.sha256 != base_sha256:
        raise ClaudeMdConflict(f"{FILENAME} changed on disk since it was loaded")

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)

    new_sha = _sha256(content)
    if state_dir is not None:
        _append_audit(
            state_dir,
            project=project_path.name,
            user=user,
            action="create" if not current.exists else "update",
            size=len(encoded),
            sha256=new_sha,
        )
    return ClaudeMdDoc(exists=True, content=content, sha256=new_sha, size=len(encoded))


def _append_audit(
    state_dir: Path, *, project: str, user: str, action: str, size: int, sha256: str
) -> None:
    """Append one JSON line recording the edit. Best-effort: a failure here must
    not undo a write that already succeeded, but it is never silently dropped on a
    healthy disk (the state_dir is the same local volume as the rest of the app)."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user": user,
        "project": project,
        "action": action,
        "file": FILENAME,
        "size": size,
        "sha256": sha256,
    }
    state_dir = state_dir.expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / _AUDIT_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
