"""Hooks config-write surface (#690) over the #347 Foundation seam.

Sibling of the permission-rules surface (#689) and the MCP-server surface (#688): it
adds exactly a **pure structural validator** for a ``hooks`` block and a **router**
that runs the fail-closed Foundation pipeline (gate → confirm → validate → contain →
stale-hash → atomic write) for a write. It owns no gate, no writer, and no redaction
logic of its own — it *consumes* :mod:`clauster.config_write`.

This is the **code-executing** config-write surface, and the most security-critical
of the children: a hook is a shell command Claude runs on a lifecycle event, so
writing a hook from the browser is the one config-write path that can lead to host
RCE. The off-by-default fail-closed gate and the **validate-never-execute** invariant
are the only protections, and they are absolute here:

* The validator checks **structure only** — event keys, matcher type, entry shape,
  ``type`` (only ``"command"`` accepted), a non-empty ``command`` string, and an
  optional integer ``timeout``. It **never resolves, spawns, runs, shell-parses, or
  "dry-runs" a command string.** A command is inert data on write; it only ever runs
  later inside a real ``claude`` process. Executing it here — even to "check" it —
  *is* the RCE the gate exists to prevent.
* A bad shape, an unknown event key, or a non-``command`` hook type rejects the
  *whole* write (→ 422 via :func:`config_write.validate_candidate`), so a
  partial/garbled block never lands.

Two surfaces, one entry shape (mirroring Claude Code's own ``settings.json``):

* **project** scope ⇒ ``<project>/.claude/settings.json``, guarded by the stale-hash
  external-edit check + path containment.
* **user** scope ⇒ ``~/.claude/settings.json`` (the settings file — *not*
  ``~/.claude.json``), gated additionally on ``allow_user_scope`` and likewise guarded
  by the stale-hash check (it is a real file, not a ``~/.claude.json`` subtree).

Only the ``hooks`` subtree is written; every sibling key in the file is preserved
verbatim by the locked atomic replace (never a whole-file clobber).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config_write as cw
from .claude_json import locked_replace_json_file

#: The top-level key holding the hooks block in ``settings.json``.
HOOKS_KEY = "hooks"

#: The only hook ``type`` this surface accepts. Claude Code also defines ``prompt``,
#: ``agent``, ``http``, and ``mcp_tool`` types, but this surface accepts **only**
#: ``command`` — the one whose payload is a shell command we store as inert data and
#: never run. Refusing the others keeps the validator's contract narrow and explicit.
COMMAND_TYPE = "command"

#: The recognized hook **event** keys. Keyed by the Claude Code lifecycle event the
#: hook fires on. An unknown event key rejects the whole write (→ 422) so a typo'd or
#: speculative event name can never land an inert-but-misfiled command.
RECOGNIZED_EVENTS = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "UserPromptSubmit",
        "Notification",
        "Stop",
        "SubagentStop",
        "PreCompact",
        "SessionStart",
        "SessionEnd",
    }
)

#: Allowed keys inside a single hook entry (the ``{type, command, timeout?}`` object).
_HOOK_ENTRY_KEYS = frozenset({"type", "command", "timeout"})

#: Allowed keys inside a matcher group (the ``{matcher?, hooks: [...]}`` object).
_MATCHER_GROUP_KEYS = frozenset({"matcher", "hooks"})


def _validate_hook_entry(entry: Any, where: str) -> None:
    """Reject ``entry`` unless it is a structurally valid ``command`` hook object.

    A command hook is ``{"type": "command", "command": <non-empty str>, "timeout"?:
    <int>}``. The ``command`` is stored verbatim and **never parsed, resolved, or
    executed** — only its type (non-empty ``str``) is checked. ``type`` must be the
    literal ``"command"``; any other type (``prompt``/``agent``/``http``/``mcp_tool``)
    is rejected here. Unknown keys reject the whole write.
    """
    if not isinstance(entry, dict):
        raise cw.InvalidCandidateError(f"{where} must be an object")
    unknown = set(entry) - _HOOK_ENTRY_KEYS
    if unknown:
        raise cw.InvalidCandidateError(f"{where} has unknown keys: {sorted(unknown)}")

    hook_type = entry.get("type")
    if hook_type != COMMAND_TYPE:
        # Fail closed: only command hooks are managed by this surface. A non-command
        # type (or a missing one) is rejected — never silently coerced.
        raise cw.InvalidCandidateError(
            f"{where} 'type' must be {COMMAND_TYPE!r} (got {hook_type!r})"
        )

    command = entry.get("command")
    if not isinstance(command, str) or not command:
        # The command is OPAQUE data: validated for shape only, never run or resolved.
        raise cw.InvalidCandidateError(f"{where} 'command' must be a non-empty string")

    if "timeout" in entry:
        timeout = entry["timeout"]
        # bool is an int subclass; a JSON ``true`` is not a valid timeout.
        if not isinstance(timeout, int) or isinstance(timeout, bool):
            raise cw.InvalidCandidateError(f"{where} 'timeout' must be an integer")


def _validate_matcher_group(group: Any, where: str) -> None:
    """Reject ``group`` unless it is a valid ``{matcher?, hooks: [...]}`` object.

    ``matcher`` is optional and, when present, must be a string (it is a pattern, never
    evaluated here). ``hooks`` is a required non-empty list of command hook entries.
    Unknown keys reject the whole write.
    """
    if not isinstance(group, dict):
        raise cw.InvalidCandidateError(f"{where} must be an object")
    unknown = set(group) - _MATCHER_GROUP_KEYS
    if unknown:
        raise cw.InvalidCandidateError(f"{where} has unknown keys: {sorted(unknown)}")

    if "matcher" in group and not isinstance(group["matcher"], str):
        # The matcher is an opaque pattern string; its content is never evaluated here.
        raise cw.InvalidCandidateError(f"{where} 'matcher' must be a string")

    hooks = group.get("hooks")
    if not isinstance(hooks, list) or not hooks:
        raise cw.InvalidCandidateError(f"{where} 'hooks' must be a non-empty list")
    for i, entry in enumerate(hooks):
        _validate_hook_entry(entry, f"{where} hooks[{i}]")


def validate_hooks(candidate: Any) -> None:
    """Structural validator for the whole ``hooks`` object (the Foundation hook).

    ``candidate`` is the desired ``hooks`` object: a ``dict`` keyed by a recognized
    lifecycle **event** (see :data:`RECOGNIZED_EVENTS`), each mapping to a list of
    matcher groups ``{matcher?: str, hooks: [{type: "command", command: str,
    timeout?: int}]}``. Unknown event keys, wrong types, a non-``command`` hook type,
    an empty command, or a non-integer timeout reject the whole write (→ 422 via
    :func:`config_write.validate_candidate`), so a partial/garbled block never lands.

    **STRUCTURE ONLY.** No ``command`` string is ever resolved, spawned, shell-parsed,
    or run — storing it is fine; executing it (even to "check" it) would be the RCE
    this gate exists to prevent. The command is inert data on write; it only ever runs
    later inside a real ``claude`` process.
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError("hooks must be an object")
    unknown = set(candidate) - RECOGNIZED_EVENTS
    if unknown:
        raise cw.InvalidCandidateError(
            f"hooks has unknown event(s): {sorted(unknown)} "
            f"(want one of {sorted(RECOGNIZED_EVENTS)})"
        )

    for event, groups in candidate.items():
        if not isinstance(groups, list):
            raise cw.InvalidCandidateError(f"hooks {event!r} must be a list of matcher groups")
        for i, group in enumerate(groups):
            _validate_matcher_group(group, f"hooks {event!r}[{i}]")


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


def _read_hooks(path: Path) -> tuple[dict[str, Any], str]:
    """Return ``(hooks, content_hash)`` for a settings file at ``path``.

    The hash is over the *current file bytes* (empty digest when absent) — the caller
    echoes it back on write so the stale-hash guard can reject a stale write (409). A
    command string is opaque shell text, not a secret-shaped value, so no redaction is
    applied; a missing file reads as an empty hooks block.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = _load_json_obj(raw)
    hooks = data.get(HOOKS_KEY)
    hooks = hooks if isinstance(hooks, dict) else {}
    return hooks, cw.hash_bytes(raw)


def _write_hooks(path: Path, incoming: dict[str, Any], expected_hash: str | None) -> None:
    """Write the ``hooks`` subtree of ``path`` under the lock, fail-closed.

    Pipeline (each step aborts before the write): stale-hash external-edit guard over
    the current bytes read under the lock (→ 409) → replace **only** the ``hooks``
    subtree, preserving every sibling key → atomic replace of the rendered file. The
    caller must have already run the capability gate, the type-the-name confirm,
    (project scope) path containment, **and the structural validation (→ 422)** — this
    helper trusts an already-validated ``incoming`` (the public ``write_*`` entry points
    validate before ``mkdir`` so a bad shape never creates a directory; mirrors the
    single-validation shape of :func:`config_write_permissions.write_project_permissions`).

    The whole read-merge-write runs inside the shared
    :func:`~clauster.claude_json.locked_replace_json_file` transaction (``flock`` +
    one-time ``.bak`` + unique-``mkstemp`` + mode-preserving + atomic-``os.replace``),
    so the stale-hash check and the replace are one critical section (no TOCTOU window).

    No command string is ever parsed, resolved, spawned, or executed; only the block's
    shape is checked.
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
        current[HOOKS_KEY] = incoming
        return current

    locked_replace_json_file(path, _mutate, render=_render_json)


def project_settings_path(project_dir: Path) -> Path:
    """Return the project-scope settings file path (``<project>/.claude/settings.json``)."""
    return project_dir / ".claude" / "settings.json"


def read_project_hooks(project_dir: Path) -> tuple[dict[str, Any], str]:
    """Return ``(hooks, content_hash)`` for a project's ``.claude/settings.json``."""
    return _read_hooks(project_settings_path(project_dir))


def write_project_hooks(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the project ``.claude/settings.json`` ``hooks`` block.

    Ensures the ``<project>/.claude`` parent exists (so a first write to a project that
    has no ``.claude`` dir yet does not fail the atomic writer's ``mkstemp`` in a
    missing directory), then runs the fail-closed :func:`_write_hooks` pipeline. The
    candidate is validated *before* the directory is created, so a bad shape (422)
    leaves the filesystem untouched. **No command is ever executed** — only validated
    for shape and stored as inert text.
    """
    cw.validate_candidate(incoming, validate_hooks)
    path = project_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_hooks(path, incoming, expected_hash)


def read_user_hooks(settings_json: Path) -> tuple[dict[str, Any], str]:
    """Return ``(hooks, content_hash)`` for the user-scope ``~/.claude/settings.json``."""
    return _read_hooks(settings_json)


def write_user_hooks(
    settings_json: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the user-scope ``~/.claude/settings.json`` ``hooks`` block.

    Ensures the ``~/.claude`` parent exists, then runs the fail-closed
    :func:`_write_hooks` pipeline. Like the #689 permission writer (and unlike the #688
    user-scope MCP writer, which edits a ``~/.claude.json`` subtree), this writes a
    *separate real file* and so carries the same stale-hash guard as the project scope.
    The candidate is validated *before* the directory is created, so a bad shape (422)
    writes nothing. **No command is ever executed** — only validated for shape.
    """
    cw.validate_candidate(incoming, validate_hooks)
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    _write_hooks(settings_json, incoming, expected_hash)
