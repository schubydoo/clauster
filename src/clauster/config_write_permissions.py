"""Permission-rules config-write surface (#689) over the #347 Foundation seam.

Sibling of the MCP-server surface (#688): it adds exactly a **pure structural
validator** for a ``permissions`` block and a **router** that runs the fail-closed
Foundation pipeline (gate → confirm → validate → contain → stale-hash → atomic
write) for a write. It owns no gate, no writer, and no redaction logic of its own —
it *consumes* :mod:`clauster.config_write`.

Permission rules are **data**, not code, but the gate is applied uniformly with the
code-executing children. The validator is **structural only**: it checks shape and
the recognized ``defaultMode`` vocabulary; it **never resolves, parses, or evaluates
a rule string** (a rule like ``Bash(rm:*)`` is stored verbatim as inert text).

Three surfaces, one entry shape (mirroring Claude Code's own ``settings.json``):

* **project** scope ⇒ ``<project>/.claude/settings.json``, guarded by the stale-hash
  external-edit check + path containment.
* **local** scope ⇒ ``<project>/.claude/settings.local.json`` — you, this project
  only, never shared or committed. Same stale-hash guard + path containment as
  project scope; a successful write also runs
  :func:`~clauster.config_write.ensure_gitignored` so a newly created file is never
  accidentally committed (#766).
* **user** scope ⇒ ``~/.claude/settings.json`` (the settings file — *not*
  ``~/.claude.json``), gated additionally on ``allow_user_scope`` and likewise guarded
  by the stale-hash check (it is a real file, not a ``~/.claude.json`` subtree).

Only the ``permissions`` subtree is written; every sibling key in the file is
preserved verbatim by the locked atomic replace (never a whole-file clobber).

``bypassPermissions`` is **deliberately excluded** from the recognized
``defaultMode`` set: it stays behind the existing footgun gate
(``ProjectConfig.allow_bypass_permissions``) and can never be set through this
surface. A candidate requesting it as the *mode* is rejected (→ 422); the literal as
a rule string is harmless inert data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config_write as cw
from .config import PERMISSION_LABELS

#: The top-level key holding the permission rules in ``settings.json``.
PERMISSIONS_KEY = "permissions"

#: The list-valued rule buckets (each a list of opaque, never-parsed rule strings).
#: ``ask`` is the third canonical Claude Code decision bucket (prompt before use) —
#: same opaque-string shape as ``allow``/``deny``, validated identically and never
#: parsed or executed.
_RULE_LIST_KEYS = frozenset({"allow", "deny", "ask"})

#: Allowed top-level keys inside the ``permissions`` object.
_PERMISSION_KEYS = frozenset({"allow", "deny", "ask", "defaultMode"})

#: The mode this surface refuses to set — it stays behind the footgun gate (#347/#685).
BYPASS_MODE = "bypassPermissions"

#: The recognized ``defaultMode`` values: the canonical permission-label vocabulary
#: minus :data:`BYPASS_MODE`, which is footgun-gated and never settable here. Derived
#: from :data:`~clauster.config.PERMISSION_LABELS` so the two never drift (#685).
RECOGNIZED_MODES = frozenset(PERMISSION_LABELS) - {BYPASS_MODE}


def _validate_rule_list(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a list of non-empty rule strings (opaque data).

    A rule string is stored verbatim and **never parsed or executed** — only its
    type (non-empty ``str``) is checked.
    """
    if not isinstance(value, list):
        raise cw.InvalidCandidateError(f"{label} must be a list of rule strings")
    for rule in value:
        if not isinstance(rule, str) or not rule:
            raise cw.InvalidCandidateError(f"{label} must contain only non-empty strings")


def validate_permissions(candidate: Any) -> None:
    """Structural validator for the whole ``permissions`` object (the Foundation hook).

    ``candidate`` is the desired ``permissions`` object: a ``dict`` whose only
    recognized keys are ``allow``/``deny``/``ask`` (lists of non-empty rule strings)
    and ``defaultMode`` (one of :data:`RECOGNIZED_MODES`). Unknown keys, wrong types, or an
    unrecognized mode reject the whole write (→ 422 via
    :func:`config_write.validate_candidate`), so a partial/garbled block never lands.

    :data:`BYPASS_MODE` is **not** a recognized mode: a request to set it as
    ``defaultMode`` is rejected here, keeping ``bypassPermissions`` behind the existing
    footgun gate. **Never parses or executes any rule string.**
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError("permissions must be an object")
    unknown = set(candidate) - _PERMISSION_KEYS
    if unknown:
        raise cw.InvalidCandidateError(f"permissions has unknown keys: {sorted(unknown)}")

    for key in _RULE_LIST_KEYS:
        if key in candidate:
            _validate_rule_list(candidate[key], f"permissions {key!r}")

    if "defaultMode" in candidate:
        mode = candidate["defaultMode"]
        if not isinstance(mode, str):
            raise cw.InvalidCandidateError("permissions 'defaultMode' must be a string")
        if mode == BYPASS_MODE:
            # Fail closed: bypassPermissions stays behind the footgun gate, never here.
            raise cw.InvalidCandidateError(
                "permissions 'defaultMode' cannot be 'bypassPermissions' (footgun-gated)"
            )
        if mode not in RECOGNIZED_MODES:
            raise cw.InvalidCandidateError(
                f"unknown permission defaultMode {mode!r} (want one of {sorted(RECOGNIZED_MODES)})"
            )


def _read_permissions(path: Path) -> tuple[dict[str, Any], str]:
    """Return ``(permissions, content_hash)`` for a settings file at ``path``.

    The hash is over the *current file bytes* (empty digest when absent) — the caller
    echoes it back on write so the stale-hash guard can reject a stale write (409).
    Permission rules are not secret-shaped, so no redaction is applied; a missing file
    reads as an empty permissions block.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = cw.load_settings_json_obj(raw)
    permissions = data.get(PERMISSIONS_KEY)
    permissions = permissions if isinstance(permissions, dict) else {}
    return permissions, cw.hash_bytes(raw)


def read_project_permissions(project_dir: Path) -> tuple[dict[str, Any], str]:
    """Return ``(permissions, content_hash)`` for a project's ``.claude/settings.json``."""
    return _read_permissions(cw.project_settings_path(project_dir))


def write_project_permissions(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the project ``.claude/settings.json`` ``permissions`` block.

    Ensures the ``<project>/.claude`` parent exists (so a first write to a project that
    has no ``.claude`` dir yet does not fail the atomic writer's ``mkstemp`` in a
    missing directory), then runs the fail-closed Foundation pipeline. The candidate is
    validated *before* the directory is created, so a bad shape (422) leaves the
    filesystem untouched.
    """
    cw.validate_candidate(incoming, validate_permissions)
    path = cw.project_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    cw.write_settings_subtree(path, PERMISSIONS_KEY, incoming, expected_hash)


def read_user_permissions(settings_json: Path) -> tuple[dict[str, Any], str]:
    """Return ``(permissions, content_hash)`` for the user-scope ``~/.claude/settings.json``."""
    return _read_permissions(settings_json)


def write_user_permissions(
    settings_json: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the user-scope ``~/.claude/settings.json`` ``permissions`` block.

    Ensures the ``~/.claude`` parent exists, then runs the fail-closed Foundation
    pipeline. Unlike the #688 user-scope MCP writer (which edits a ``~/.claude.json``
    subtree), this writes a *separate real file* and so carries the same stale-hash
    guard as the project scope. The candidate is validated *before* the directory is
    created, so a bad shape (422) writes nothing.
    """
    cw.validate_candidate(incoming, validate_permissions)
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    cw.write_settings_subtree(settings_json, PERMISSIONS_KEY, incoming, expected_hash)


def read_project_local_permissions(project_dir: Path) -> tuple[dict[str, Any], str]:
    """Return ``(permissions, content_hash)`` for a project's ``settings.local.json``."""
    return _read_permissions(cw.project_local_settings_path(project_dir))


def write_project_local_permissions(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the local-scope ``.claude/settings.local.json`` ``permissions`` block.

    Third (local) scope, sibling of the project/user writers above: same fail-closed
    Foundation pipeline and stale-hash guard, targeting a *third* file that is you,
    this project only. A successful write additionally runs
    :func:`~clauster.config_write.ensure_gitignored` so a newly created
    ``settings.local.json`` is never accidentally committed (#766) — idempotent, so a
    write to an already-gitignored file is a no-op there.
    """
    cw.validate_candidate(incoming, validate_permissions)
    path = cw.project_local_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    cw.write_settings_subtree(path, PERMISSIONS_KEY, incoming, expected_hash)
    cw.ensure_gitignored(project_dir, ".claude/settings.local.json", ignore_backup_sibling=True)
