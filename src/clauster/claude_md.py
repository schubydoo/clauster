"""CLAUDE.md viewer/editor (spec §5: "CLAUDE.md viewer + editor").

Reads/writes a project's *root* CLAUDE.md. Editing only writes a file — it never
executes anything (the bridge runs CLAUDE.md only when the dir is trusted and a
session spawns), so no trust gate is needed here. But because the target lives
inside an operator-named project dir, the path is locked to exactly
``<project>/CLAUDE.md`` and rejected if it resolves outside that dir (symlink or
traversal). Writes are atomic (temp + ``os.replace``, the same idiom as
``trust.py``) and appended to an audit log. A 64 KB cap matches the spec.

This module also carries the **config-write** CLAUDE.md surface (#768, over the
#347/#687 Foundation and the #766 file/dir-writer primitive) — folded in here
rather than duplicated into a new module, since both surfaces read/write the same
family of files. It is a *separate* surface from the trust-gated editor above: it
sits behind the ``config_write.enabled`` capability gate + type-the-name confirm
(see :mod:`clauster.config_write`), and covers all three of Claude Code's memory
scopes:

* **user** — ``~/.claude/CLAUDE.md``
* **project** — ``<project>/CLAUDE.md`` *or* ``<project>/.claude/CLAUDE.md``
  (whichever already exists; a fresh project defaults to the root location, the
  same one the legacy trust-gated editor above uses)
* **local** — ``<project>/CLAUDE.local.md``, gitignored on create via
  :func:`~clauster.config_write.ensure_gitignored` (#766)

**Threat model — content tier (resolved 2026-06-29):** CLAUDE.md is
prompt-injection *content*, not executable configuration — Claude Code never
executes it, only reads it into a prompt. It therefore gets the same off-by-default
gate + type-the-name confirm as every other config-write surface, but
**deliberately no secret redaction**: it is user-authored memory, not credential
storage, and redacting it would be security theater that also corrupts the
operator's own prose. The read path returns raw content, never
:func:`~clauster.config_write.redact_secret_lines`.

Ops are **edit** (write new content) and **blank** (write empty content) — there
is no delete; :func:`~clauster.config_write.ensure_gitignored` is the create-time
side effect for the local scope only. The size cap (:data:`MAX_BYTES`) and
UTF-8 requirement are shared with the legacy editor.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import atomicio
from . import config_file_writer as fw
from . import config_write as cw
from .models import ClaudeMdDoc
from .trust import is_trusted

FILENAME = "CLAUDE.md"
#: The local-scope filename (project root, never inside ``.claude/``) — mirrors
#: Claude Code's own ``CLAUDE.local.md`` convention.
LOCAL_FILENAME = "CLAUDE.local.md"
MAX_BYTES = 64 * 1024  # 64 KB cap (spec §5); shared by the config-write surface below
_AUDIT_FILE = "claude_md_audit.log"
_log = logging.getLogger("clauster.claude_md")

# Serializes the config-write surface's read-hash→write critical section. Unlike the
# JSON-subtree writers (whose stale-hash check runs *inside* the same lock as the
# atomic replace, via `locked_replace_json_file`'s mutate callback),
# `config_file_writer.write_file` takes already-computed content, not a mutate hook —
# so the hash check here happens as a separate step before the call. A per-process
# lock closes that window between two concurrent PUTs (each dispatched to a worker
# thread via `asyncio.to_thread`, same reasoning as `config_writer._write_lock`).
_write_lock = threading.Lock()


class ClaudeMdError(RuntimeError):
    """Base for read/write failures the app maps to HTTP 4xx."""


class ClaudeMdTooLarge(ClaudeMdError):
    """The submitted CLAUDE.md exceeds the configured size limit."""


class ClaudeMdConflict(ClaudeMdError):
    """On-disk content changed since the editor loaded it (lost-update guard)."""


class ClaudeMdPathError(ClaudeMdError):
    """The resolved CLAUDE.md path escapes the project directory."""


class ClaudeMdNotTrusted(ClaudeMdError):
    """The project directory is not trusted — writing CLAUDE.md is refused."""


def _target(project_path: Path) -> Path:
    """Resolve the locked target path, guaranteed to sit directly inside the project dir.

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
    """Read the project's CLAUDE.md, returning an empty doc if it does not exist."""
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
    claude_json: Path | None = None,
) -> ClaudeMdDoc:
    """Atomically write CLAUDE.md, enforcing the size cap and (optional) lost-update guard.

    ``base_sha256`` is the hash the editor loaded. When provided, the current
    on-disk hash must match (None == absent) or the write is refused as a conflict;
    omit it to create a brand-new file or to force last-write-wins.

    When ``claude_json`` is given, the write is refused unless the project dir is
    trusted there — this confines writes to trusted dirs (a symlinked project that
    resolves outside projects_root won't be trusted), matching the spawn trust gate.
    """
    target = _target(project_path)
    # Validate the path shape first, then authorize: a symlinked project dir that
    # resolves outside projects_root won't be trusted (mirrors the spawn gate).
    if claude_json is not None and not is_trusted(project_path, claude_json):
        raise ClaudeMdNotTrusted(f"{project_path} is not a trusted directory")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ClaudeMdTooLarge(
            f"{FILENAME} is {len(encoded)} bytes, over the {MAX_BYTES} byte cap"
        )

    current = read_claude_md(project_path)
    if base_sha256 is not None and current.sha256 != base_sha256:
        raise ClaudeMdConflict(f"{FILENAME} changed on disk since it was loaded")

    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        # newline="\n" keeps CLAUDE.md byte-identical across OSes (the default would
        # translate to CRLF on Windows, #914); the replace retries a transient Windows
        # sharing violation (the `claude` CLI may hold the file open).
        tmp.write_text(content, encoding="utf-8", newline="\n")
        atomicio.replace_with_retry(tmp, target)
    except OSError as exc:
        # Atomic write failed (disk full, read-only, cross-device) — clean up the
        # partial temp file and surface a 4xx instead of leaking an orphan + raw 500.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ClaudeMdError(f"could not write {FILENAME}: {exc}") from exc

    new_sha = _sha256(content)
    if state_dir is not None:
        # Audit is best-effort: the content write already committed via os.replace,
        # so a failed audit append must NOT be reported as a failed save — but it is
        # logged loudly so the gap is never silent.
        try:
            _append_audit(
                state_dir,
                project=project_path.name,
                user=user,
                action="create" if not current.exists else "update",
                size=len(encoded),
                sha256=new_sha,
            )
        except OSError as exc:
            _log.error(
                "%s write for %s succeeded but audit append failed: %s",
                FILENAME,
                project_path.name,
                exc,
            )
    return ClaudeMdDoc(exists=True, content=content, sha256=new_sha, size=len(encoded))


def _append_audit(
    state_dir: Path, *, project: str, user: str, action: str, size: int, sha256: str
) -> None:
    """Append one JSON line recording the edit.

    Best-effort: a failure here must not undo a write that already succeeded, but
    it is never silently dropped on a healthy disk (the state_dir is the same local
    volume as the rest of the app).
    """
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "user": user,
        "project": project,
        "action": action,
        "file": FILENAME,
        "size": size,
        "sha256": sha256,
    }
    state_dir = state_dir.expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / _AUDIT_FILE, "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# config-write CLAUDE.md surface (#768) — user/project/local scope, gated by
# clauster.config_write, built on the config_file_writer primitive (#766).
# ---------------------------------------------------------------------------


def validate_content(candidate: Any) -> None:
    """Structural validator for CLAUDE.md content (the Foundation validate hook).

    ``candidate`` must be a ``str`` no larger than :data:`MAX_BYTES` when UTF-8
    encoded. This is the *only* check — CLAUDE.md is free-form prose, never
    parsed or executed, so there is no shape beyond "small enough text".
    """
    if not isinstance(candidate, str):
        raise cw.InvalidCandidateError("content must be a string")
    size = len(candidate.encode("utf-8"))
    if size > MAX_BYTES:
        raise cw.InvalidCandidateError(f"content is {size} bytes, over the {MAX_BYTES} byte cap")


def _resolve(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root`` via the shared containment primitive.

    Rewraps :class:`~clauster.config_file_writer.PathEscapeError` as
    :class:`~clauster.config_write.PathEscapeError` so every failure from this
    surface is a :class:`~clauster.config_write.ConfigWriteError`, matching the
    other config-write children and letting the app layer's single error mapper
    handle it uniformly.
    """
    try:
        return fw.resolve_contained_path(root, relative)
    except fw.PathEscapeError as exc:
        raise cw.PathEscapeError(str(exc)) from exc


def _project_relative(project_dir: Path) -> str:
    """Return the project-scope CLAUDE.md path, preferring whichever already exists.

    Claude Code accepts a project's CLAUDE.md at the project root *or* under
    ``.claude/``. When neither exists yet (a fresh project), default to the root
    location — the same one the legacy trust-gated editor above uses — so a first
    "create" lands where an operator would expect to find it.
    """
    if not (project_dir / FILENAME).exists() and (project_dir / ".claude" / FILENAME).exists():
        return f".claude/{FILENAME}"
    return FILENAME


def _read_scoped(root: Path, relative: str) -> tuple[str, str, bool]:
    """Return ``(content, hash, exists)`` for a CLAUDE.md-family file at ``root/relative``.

    Content-tier read: returns raw text, **never** redacted (this surface's
    resolved threat model — CLAUDE.md is prose, not credential storage). A missing
    file reads as empty content (hash of empty bytes, ``exists=False``) rather than
    raising, so a not-yet-created scope shows as an empty, ready-to-fill editor
    (create-if-missing), not a 404.
    """
    target = _resolve(root, relative)
    try:
        data = target.read_bytes()
        exists = True
    except FileNotFoundError:
        data = b""
        exists = False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise cw.InvalidCandidateError(f"{relative} is not valid UTF-8") from exc
    return text, cw.hash_bytes(data), exists


def _write_scoped(root: Path, relative: str, content: str, expected_hash: str | None) -> None:
    """Validate + stale-hash-guard + atomically write a CLAUDE.md-family file.

    Mirrors the gate order the other config-write children use: structural
    validation first (bad shape/oversize → 422, nothing written), then the
    external-edit guard (``expected_hash`` mismatch, or an existing file with no
    hash supplied, → 409), then the atomic write via
    :func:`~clauster.config_file_writer.write_file`. ``expected_hash=None`` is the
    legitimate first-write ("create") path only when the file is genuinely absent.

    Existence is tracked *separately from content* (``found``): an existing but
    **empty** file — exactly what the "blank" op writes — must still require a hash,
    so a subsequent ``expected_hash=None`` PUT is a 409, not a silent overwrite. An
    empty-bytes hash is a real hash a caller can echo back; only a genuinely missing
    file has no prior state to guard.
    """
    cw.validate_candidate(content, validate_content)
    target = _resolve(root, relative)
    with _write_lock:
        try:
            current = target.read_bytes()
            found = True
        except FileNotFoundError:
            current = b""
            found = False
        if expected_hash is None:
            if found:
                raise cw.StaleConfigWriteError(f"{relative} already exists; a hash is required")
        elif cw.hash_bytes(current) != expected_hash:
            raise cw.StaleConfigWriteError(f"{relative} changed on disk since it was loaded")
        fw.write_file(root, relative, content)


def read_project_claude_md(project_dir: Path) -> tuple[str, str, bool]:
    """Return ``(content, hash, exists)`` for the project-scope CLAUDE.md."""
    return _read_scoped(project_dir, _project_relative(project_dir))


def write_project_claude_md(project_dir: Path, content: str, expected_hash: str | None) -> None:
    """Validate + write the project-scope CLAUDE.md (root or ``.claude/``, whichever exists)."""
    _write_scoped(project_dir, _project_relative(project_dir), content, expected_hash)


def read_user_claude_md(claude_json: Path) -> tuple[str, str, bool]:
    """Return ``(content, hash, exists)`` for the user-scope ``~/.claude/CLAUDE.md``.

    ``claude_json`` is the resolved ``~/.claude.json`` path (its parent is the
    user's home dir), the same handle the other user-scope surfaces use to derive
    ``~/.claude`` without hardcoding ``Path.home()`` (testable via HOME isolation).
    """
    return _read_scoped(claude_json.parent / ".claude", FILENAME)


def write_user_claude_md(claude_json: Path, content: str, expected_hash: str | None) -> None:
    """Validate + write the user-scope ``~/.claude/CLAUDE.md``."""
    _write_scoped(claude_json.parent / ".claude", FILENAME, content, expected_hash)


def read_project_local_claude_md(project_dir: Path) -> tuple[str, str, bool]:
    """Return ``(content, hash, exists)`` for the local-scope ``CLAUDE.local.md``."""
    return _read_scoped(project_dir, LOCAL_FILENAME)


def write_project_local_claude_md(
    project_dir: Path, content: str, expected_hash: str | None
) -> None:
    """Validate + write the local-scope ``CLAUDE.local.md``; gitignore it on success.

    A successful write always runs
    :func:`~clauster.config_write.ensure_gitignored` (idempotent — a no-op once the
    entry exists) so a newly created ``CLAUDE.local.md`` is never accidentally
    committed (#766), mirroring the local-scope JSON writers.
    """
    _write_scoped(project_dir, LOCAL_FILENAME, content, expected_hash)
    cw.ensure_gitignored(project_dir, LOCAL_FILENAME)
