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

Two surfaces, one entry shape (mirroring Claude Code's own ``settings.json``):

* **project** scope ⇒ ``<project>/.claude/settings.json``, guarded by the stale-hash
  external-edit check + path containment.
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

import json
from pathlib import Path
from typing import Any

from . import config_write as cw
from .claude_json import locked_replace_json_file
from .config import PERMISSION_LABELS

#: The top-level key holding the permission rules in ``settings.json``.
PERMISSIONS_KEY = "permissions"

#: The list-valued rule buckets (each a list of opaque, never-parsed rule strings).
_RULE_LIST_KEYS = frozenset({"allow", "deny"})

#: Allowed top-level keys inside the ``permissions`` object.
_PERMISSION_KEYS = frozenset({"allow", "deny", "defaultMode"})

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
    recognized keys are ``allow``/``deny`` (lists of non-empty rule strings) and
    ``defaultMode`` (one of :data:`RECOGNIZED_MODES`). Unknown keys, wrong types, or an
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


def _load_json_obj(raw: bytes) -> dict[str, Any]:
    """Parse ``raw`` bytes as a JSON object, returning ``{}`` for empty/whitespace.

    A non-object or malformed JSON is a structural error (→ caller maps to 422): we
    will not overwrite a file we could not parse.
    """
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise cw.InvalidCandidateError(f"existing settings is not valid UTF-8: {exc}") from exc
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise cw.InvalidCandidateError(f"existing settings is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise cw.InvalidCandidateError("existing settings is not a JSON object")
    return data


def _render_json(data: dict[str, Any]) -> str:
    """Render ``data`` as pretty JSON with a trailing newline (matches CLI style)."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _read_permissions(path: Path) -> tuple[dict[str, Any], str]:
    """Return ``(permissions, content_hash)`` for a settings file at ``path``.

    The hash is over the *current file bytes* (empty digest when absent) — the caller
    echoes it back on write so :func:`config_write.guard_unchanged` can reject a stale
    write (409). Permission rules are not secret-shaped, so no redaction is applied; a
    missing file reads as an empty permissions block.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = _load_json_obj(raw)
    permissions = data.get(PERMISSIONS_KEY)
    permissions = permissions if isinstance(permissions, dict) else {}
    return permissions, cw.hash_bytes(raw)


def _write_permissions(path: Path, incoming: dict[str, Any], expected_hash: str | None) -> None:
    """Write the ``permissions`` subtree of ``path`` under the lock, fail-closed.

    Pipeline (each step aborts before the write): stale-hash external-edit guard over
    the current bytes read under the lock (→ 409) → replace **only** the ``permissions``
    subtree, preserving every sibling key → atomic replace of the rendered file. The
    caller must have already run the capability gate, the type-the-name confirm,
    (project scope) path containment, **and the structural validation (→ 422)** — this
    helper trusts an already-validated ``incoming`` (the public ``write_*`` entry points
    validate before ``mkdir`` so a bad shape never creates a directory; mirrors the
    single-validation shape of :func:`config_write_mcp.write_project_servers`).

    The whole read-merge-write runs inside the shared
    :func:`~clauster.claude_json.locked_replace_json_file` transaction (``flock`` +
    one-time ``.bak`` + unique-``mkstemp`` + mode-preserving + atomic-``os.replace``),
    so the stale-hash check and the replace are one critical section (no TOCTOU window).

    No rule string is ever parsed or executed; only the block's shape is checked.
    """

    def _mutate(current_bytes: bytes) -> dict[str, Any]:
        # Stale-hash guard against the bytes read under the lock (raises → 409). An
        # absent hash is only the legitimate first-write path: if the file already has
        # content, refuse the unguarded overwrite (a client that drops `hash` must not
        # be able to bypass the external-edit check on an existing file).
        if expected_hash is None:
            if current_bytes:
                raise cw.StaleConfigWriteError("settings.json already exists; a hash is required")
        elif cw.hash_bytes(current_bytes) != expected_hash:
            raise cw.StaleConfigWriteError("config file changed on disk since it was loaded")
        current = _load_json_obj(current_bytes)
        current[PERMISSIONS_KEY] = incoming
        return current

    locked_replace_json_file(path, _mutate, render=_render_json)


def project_settings_path(project_dir: Path) -> Path:
    """Return the project-scope settings file path (``<project>/.claude/settings.json``)."""
    return project_dir / ".claude" / "settings.json"


def read_project_permissions(project_dir: Path) -> tuple[dict[str, Any], str]:
    """Return ``(permissions, content_hash)`` for a project's ``.claude/settings.json``."""
    return _read_permissions(project_settings_path(project_dir))


def write_project_permissions(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the project ``.claude/settings.json`` ``permissions`` block.

    Ensures the ``<project>/.claude`` parent exists (so a first write to a project that
    has no ``.claude`` dir yet does not fail the atomic writer's ``mkstemp`` in a
    missing directory), then runs the fail-closed :func:`_write_permissions` pipeline.
    The candidate is validated *before* the directory is created, so a bad shape (422)
    leaves the filesystem untouched.
    """
    cw.validate_candidate(incoming, validate_permissions)
    path = project_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_permissions(path, incoming, expected_hash)


def read_user_permissions(settings_json: Path) -> tuple[dict[str, Any], str]:
    """Return ``(permissions, content_hash)`` for the user-scope ``~/.claude/settings.json``."""
    return _read_permissions(settings_json)


def write_user_permissions(
    settings_json: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the user-scope ``~/.claude/settings.json`` ``permissions`` block.

    Ensures the ``~/.claude`` parent exists, then runs the fail-closed
    :func:`_write_permissions` pipeline. Unlike the #688 user-scope MCP writer (which
    edits a ``~/.claude.json`` subtree), this writes a *separate real file* and so
    carries the same stale-hash guard as the project scope. The candidate is validated
    *before* the directory is created, so a bad shape (422) writes nothing.
    """
    cw.validate_candidate(incoming, validate_permissions)
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    _write_permissions(settings_json, incoming, expected_hash)
