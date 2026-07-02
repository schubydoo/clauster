"""Path-contained atomic file/dir writer primitive (#766, over the #347 Foundation).

The Foundation (:mod:`clauster.config_write`) is a JSON-*subtree* writer: every
existing surface (MCP servers, permission rules, hooks) reads/writes a named key
inside a JSON settings file. The surfaces still to come — skills (a *directory*:
``SKILL.md`` + supporting files), subagents, and ``CLAUDE.md`` (a single *file*, no
JSON structure at all) — need a different shape: an atomic create / replace / delete
of a contained file **or** directory tree. This module is that shared primitive,
built once here and consumed by those later surfaces (#691 skills, subagents,
``CLAUDE.md``); it ships **no concrete writer endpoint of its own**, mirroring how
:mod:`clauster.config_write` shipped no endpoint for its child surfaces.

Every one of the Foundation's invariants carries over, adapted to files/dirs instead
of JSON subtrees:

* **Strict path containment** — :func:`resolve_contained_path` resolves a
  caller-supplied relative path against an allowed root and rejects anything that
  would land outside it: an absolute path (the classic ``Path(root) / "/etc/passwd"``
  footgun, which silently discards ``root`` in vanilla ``pathlib`` join semantics), an
  explicit ``..``/``.``/empty path component, or a component that is a symlink
  resolving outside the root. Raises :class:`PathEscapeError` **before any I/O**.
* **flock** — :func:`write_file`, :func:`replace_tree`, and :func:`delete_path` each
  hold an advisory ``flock`` (a sidecar ``<target>.lock``, never the target itself, so
  ``os.replace`` swapping the inode never orphans a held lock) across their whole
  operation, the same technique :mod:`clauster.claude_json` uses for
  ``~/.claude.json``. POSIX-only; degrades to a best-effort no-op elsewhere (the
  atomic replace still prevents a torn file/tree).
* **Temp-file / temp-dir + atomic rename** — a file create/replace is a single
  ``mkstemp`` + ``os.replace`` (always atomic on POSIX, same filesystem). A directory
  create (target absent) is a single rename of a freshly built sibling temp dir — also
  fully atomic. A directory *replace* (target already exists) needs two renames back
  to back (swap the existing tree aside, then swap the new one in) — see
  :func:`replace_tree` for the documented, narrow non-atomicity window this implies.
* **Redaction** — :func:`read_file` runs its content through
  :func:`~clauster.config_write.redact_secret_lines` (the line-oriented twin of the
  Foundation's structural :func:`~clauster.config_write.redact_secrets`, since free
  text has no dict/list structure to recurse) before returning it, so a secret-shaped
  line in a skill script or subagent frontmatter is never assembled into a response.

This module does **not** validate or interpret file *content* — a skill's
``SKILL.md`` schema, a subagent's frontmatter shape, that is each consumer's own
structural validator, run the same validate-never-execute way the JSON children run
theirs. This primitive only guarantees the write/delete mechanics are safe.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

try:
    import fcntl  # POSIX only; Windows has no flock equivalent we rely on here.
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]

from .config_write import redact_secret_lines


def _is_posix() -> bool:
    """Return whether POSIX file-mode semantics apply (False on Windows).

    A seam over ``os.name == "posix"`` so the non-POSIX mode-preservation branch in
    :func:`write_file` is testable on a POSIX host without monkeypatching ``os.name``
    itself (which would break ``tempfile``) — the identical technique
    :func:`clauster.claude_json._is_posix` uses.
    """
    return os.name == "posix"


class FileWriteError(Exception):
    """Base for file/dir-writer failures."""


class PathEscapeError(FileWriteError):
    """A caller-supplied relative path resolved outside its allowed root (→ 400)."""


def resolve_contained_path(root: Path, relative: str | os.PathLike[str]) -> Path:
    """Resolve ``relative`` under ``root``, or fail closed with :class:`PathEscapeError`.

    Validates *before* any I/O:

    1. ``relative`` must not be an absolute path. ``Path(root) / "/etc/passwd"``
       silently discards ``root`` in vanilla ``pathlib`` join semantics (the joined
       path becomes the absolute operand) — rejecting this outright closes that
       footgun rather than relying on the containment check below to catch it.
    2. Every path component must be non-empty and neither ``.`` nor ``..`` — defense
       in depth on top of the resolution check (a lexical ``..`` is rejected here
       even before we touch the filesystem).
    3. The resolved absolute path (symlinks in any *existing* path component
       followed, per ``Path.resolve()``) must equal ``root`` or have ``root`` among
       its parents — this is what catches a symlinked intermediate directory that
       escapes ``root`` even without a literal ``..`` in ``relative``.

    Returns the resolved absolute path. Never creates anything.
    """
    root_resolved = root.resolve()
    rel = Path(relative)
    if rel.is_absolute():
        raise PathEscapeError(f"path must be relative, got absolute: {relative!r}")
    if not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
        raise PathEscapeError(f"invalid relative path: {relative!r}")
    candidate = (root_resolved / rel).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise PathEscapeError(f"path escapes root {root_resolved}: {relative!r}")
    return candidate


@contextlib.contextmanager
def _locked(target: Path):
    """Hold an exclusive advisory lock for an operation on ``target``.

    Uses a sidecar ``<target>.lock`` (never ``target`` itself — an atomic replace
    swaps the inode, which would orphan a lock held on the old one), the identical
    technique :func:`clauster.claude_json._locked` uses. POSIX-only; where ``fcntl``
    is unavailable (Windows) or the lock file can't be opened, this degrades to a
    best-effort no-op rather than blocking the write — the atomic replace still
    prevents a torn file/tree.
    """
    if fcntl is None:
        yield
        return
    lock_path = target.parent / f"{target.name}.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # implicitly releases the flock


def write_file(
    root: Path,
    relative: str,
    content: str | bytes,
    *,
    mode: int = 0o600,
) -> Path:
    """Atomically create or replace the single file ``root/relative``.

    Path-contained (:func:`resolve_contained_path` runs first — a bad path never
    touches disk), flock-guarded, and a single ``mkstemp`` + ``os.replace`` (always
    atomic on POSIX, same filesystem — the target's parent directory is created if
    missing, and the temp file is written there so the replace stays on one
    filesystem). ``mode`` is applied to a **newly created** file only; replacing an
    existing file preserves its current permission bits (mirrors
    :mod:`clauster.claude_json`'s existing-mode-preservation behavior), so replacing a
    file someone hardened to something other than the default never silently
    re-permissions it.
    """
    target = resolve_contained_path(root, relative)
    data = content.encode("utf-8") if isinstance(content, str) else content
    target.parent.mkdir(parents=True, exist_ok=True)
    with _locked(target):
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                if _is_posix():
                    try:
                        existing_mode = stat.S_IMODE(target.stat().st_mode)
                    except FileNotFoundError:
                        existing_mode = mode
                    os.fchmod(fh.fileno(), existing_mode)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return target


def read_file(root: Path, relative: str) -> str:
    """Return the redacted UTF-8 text content of ``root/relative``.

    Path-contained (same guard as :func:`write_file`). Runs the content through
    :func:`~clauster.config_write.redact_secret_lines` before returning it — a
    secret-shaped line is masked from the read path, never assembled into a response
    unredacted. Raises ``FileNotFoundError`` for a missing file (propagated, never
    silently treated as empty — a missing file and an empty file are different facts
    for a caller deciding whether to show "create" or "edit").
    """
    target = resolve_contained_path(root, relative)
    text = target.read_text(encoding="utf-8")
    return redact_secret_lines(text)


def replace_tree(root: Path, relative: str, build: Callable[[Path], None]) -> Path:
    """Atomically create or replace the directory tree ``root/relative``.

    ``build(staging_dir)`` populates a fresh, empty staging directory with the
    desired final content (the caller writes whatever files/subdirs it needs into
    ``staging_dir`` — this primitive does not interpret directory *content*, only
    guarantees the promote/replace mechanics). ``build`` raising aborts the whole
    operation before anything touches the real path; the half-built staging directory
    is cleaned up and never promoted.

    Path-contained (:func:`resolve_contained_path` on ``relative``) and flock-guarded
    (serializes concurrent replaces of the *same* target).

    Atomicity:

    * **Create** (target does not yet exist): a single ``os.replace`` of the staging
      dir into the target path — always atomic on POSIX, same filesystem.
    * **Replace** (target already exists): true single-operation atomicity isn't
      achievable with plain POSIX renames when the destination is a non-empty
      directory, so this does the standard two-rename swap — move the existing tree
      aside to a sibling ``.trash-<uuid>`` (atomic rename, frees the target path),
      then ``os.replace`` the staging dir into the now-vacant target path (also
      atomic). Back to back with no I/O in between, so the window where the target
      path does not exist is vanishingly small — but it is not zero: a reader
      resolving the path in that exact instant sees ``FileNotFoundError``, never a
      half-written tree (the property that matters — no observer ever sees a torn
      write). The displaced old tree is then removed; if that cleanup itself fails,
      the live path is already correct and only the ``.trash-<uuid>`` sibling lingers
      for manual cleanup — a disk-space leak, never a correctness issue.

    If the promote rename itself fails (ENOSPC, permission), the displaced original is
    restored to the target path AND the un-promoted staging directory is removed, so a
    failure never orphans a ``.staging-<uuid>`` dir in the parent (the same cleanup is
    applied to the single-rename create path).
    """
    target = resolve_contained_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        build(staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    with _locked(target):
        if target.exists():
            trash = target.parent / f".{target.name}.trash-{uuid.uuid4().hex}"
            os.replace(target, trash)
            try:
                os.replace(staging, target)
            except BaseException:
                # Best-effort restore of the displaced tree so a failed promote never
                # leaves the target path missing, AND remove the un-promoted staging dir
                # so a promote failure (ENOSPC, permission) never orphans it in the parent.
                with contextlib.suppress(OSError):
                    os.replace(trash, target)
                shutil.rmtree(staging, ignore_errors=True)
                raise
            else:
                shutil.rmtree(trash, ignore_errors=True)
        else:
            try:
                os.replace(staging, target)
            except BaseException:
                # Same orphan guard on the create path: a failed single rename must not
                # leave the staging dir behind.
                shutil.rmtree(staging, ignore_errors=True)
                raise
    return target


def delete_path(root: Path, relative: str) -> bool:
    """Delete the file or directory tree at ``root/relative``; return whether it existed.

    Path-contained and flock-guarded like the writers above. Idempotent: a missing
    target returns ``False`` rather than raising (deleting something already gone is
    not an error). A directory is displaced via an atomic rename to a sibling
    ``.trash-<uuid>`` first (so the target path either fully exists or is fully gone —
    never a partially-``rmtree``'d tree visible at the live path) and then removed; a
    plain file is removed directly (a single ``unlink`` has no partial-delete state to
    guard against).
    """
    target = resolve_contained_path(root, relative)
    with _locked(target):
        if not target.exists() and not target.is_symlink():
            return False
        if target.is_dir() and not target.is_symlink():
            trash = target.parent / f".{target.name}.trash-{uuid.uuid4().hex}"
            os.replace(target, trash)
            shutil.rmtree(trash, ignore_errors=True)
        else:
            target.unlink()
        return True
