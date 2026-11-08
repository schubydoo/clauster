"""Permission-rules config-write surface (#689) over the #347 Foundation seam.

Sibling of the MCP-server surface (#688): it adds exactly a **pure structural
validator** for a ``permissions`` block and a **router** that runs the fail-closed
Foundation pipeline (gate → confirm → validate → contain → stale-hash → atomic
write) for a write. It owns no gate, no writer, and no redaction logic of its own —
it *consumes* :mod:`clauster.config_write`.

Permission rules are **data**, not code, but the gate is applied uniformly with the
code-executing children. The validator is **structural only**: it checks shape and
the recognized ``defaultMode`` vocabulary; it **never resolves, parses, or evaluates
a rule string** (a rule like ``Bash(rm:*)`` is stored verbatim as inert text), and it
never resolves an ``additionalDirectories`` path.

The recognized ``permissions`` keys follow the settings reference
(https://code.claude.com/docs/en/settings-reference, ``permissions`` entry, checked
2026-11-04 against Claude Code 2.1.318): the rule lists ``allow``/``deny``/``ask``,
the path list ``additionalDirectories``, the Booleans
``blockReadsOutsideWorkingDirectories``/``blockWritesOutsideWorkingDirectories``, the
``"disable"`` switches ``disableBypassPermissionsMode``/``disableAutoMode``, and
``defaultMode``.

**The auto mode classifier rules (``autoMode``, TASK-449.1).** ``autoMode`` is a
*top-level* ``settings.json`` key, a sibling of ``permissions``, not a member of it. It
is permission policy, so this surface manages it next to ``permissions`` (the generic
settings surface carves it out, see
:data:`~clauster.config_write_settings.OWNED_KEYS`). Its documented shape is the prose
rule lists ``environment``/``allow``/``soft_deny``/``hard_deny`` (each may hold the
literal ``"$defaults"`` entry, which splices in the built-in rules) plus the
``classifyAllShell`` Boolean (https://code.claude.com/docs/en/auto-mode-config).

Claude Code reads ``autoMode`` **only from user settings** (and managed settings or
``--settings``): it ignores the block in ``.claude/settings.json`` and
``.claude/settings.local.json`` so a repository cannot inject its own classifier
allow rules (same doc, "Where the classifier reads configuration"; verified with
``claude auto-mode config`` on 2.1.318, which listed a user-scope rule and dropped
the project and local ones). Writing a block there would save rules that never apply,
so a project or local write with a non-empty ``autoMode`` is refused (422) with a
message that points to User scope. An empty ``{}`` is accepted at every scope: it
removes the key, so a block that is already in a project file can be cleared. The
same docs say ``defaultMode: "auto"`` does not take effect from project or local
settings either, so it is refused there for the same reason.

``autoMode`` is redacted on read (:func:`_redact_auto_mode`). An entry is free prose, so a
credential-bearing URL or a ``${...}`` interpolation can sit in the middle of it, where
the structural :func:`~clauster.config_write.redact_secrets` (which matches a URL only at
the start of a value) would miss it. Each such entry is therefore replaced *whole* with
:data:`~clauster.config_write.REDACTION_SENTINEL`, using the line-oriented
:func:`~clauster.config_write.redact_secret_lines` as the detector. The box is free
text, so a user can move entries around a mask. A write therefore maps each resent
sentinel to the stored masked entries *in order* (:func:`_merge_auto_mode`), and it
refuses the write whole (422) when a list still holds masks but their count no longer
matches the count of masked entries stored there. A mask therefore never restores a
value from another list or from a clear entry. Two limits remain: masks all look the
same, so an edit that deletes one mask and duplicates another while keeping the count
is not detected; and a list with *no* masks left is written as sent, which deletes its
stored masked entries (an explicit choice, not an ambiguous one).

Three surfaces, one entry shape (mirroring Claude Code's own ``settings.json``):

* **project** scope ⇒ ``<project>/.claude/settings.json``, guarded by the stale-hash
  external-edit check + path containment.
* **local** scope ⇒ ``<project>/.claude/settings.local.json`` — you, this project
  only, never shared or committed. Same stale-hash guard + path containment as
  project scope; :func:`~clauster.config_write.ensure_gitignored` runs *before* the
  write (the write drops a secret-bearing ``.bak``), so neither that file nor its
  backup is ever accidentally committed (#766).
* **user** scope ⇒ ``~/.claude/settings.json`` (the settings file — *not*
  ``~/.claude.json``), gated additionally on ``allow_user_scope`` and likewise guarded
  by the stale-hash check (it is a real file, not a ``~/.claude.json`` subtree).

Only the ``permissions`` subtree (and ``autoMode`` when the caller sends one) is
written; every other sibling key in the file is preserved by the locked atomic replace
(never a whole-file clobber).

``bypassPermissions`` is **deliberately excluded** from the recognized
``defaultMode`` set: it stays behind the existing footgun gate
(``ProjectConfig.allow_bypass_permissions``) and can never be set through this
surface. A candidate requesting it as the *mode* is rejected (→ 422); the literal as
a rule string is harmless inert data.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from . import config_write as cw
from .claude_json import locked_replace_json_file
from .config import INHERIT_PERMISSION_MODE, PERMISSION_LABELS

#: The top-level key holding the permission rules in ``settings.json``.
PERMISSIONS_KEY = "permissions"

#: The top-level key holding the auto mode classifier rules, a sibling of
#: :data:`PERMISSIONS_KEY` (never nested inside it).
AUTO_MODE_KEY = "autoMode"

#: The list-valued rule buckets (each a list of opaque, never-parsed rule strings).
#: ``ask`` is the third canonical Claude Code decision bucket (prompt before use) —
#: same opaque-string shape as ``allow``/``deny``, validated identically and never
#: parsed or executed. ``additionalDirectories`` is a list of directory paths with the
#: same shape; a path is stored verbatim and never resolved.
_STRING_LIST_KEYS = frozenset({"allow", "deny", "ask", "additionalDirectories"})

#: Boolean ``permissions`` keys. ``false`` is documented as the same as unset.
_BOOL_KEYS = frozenset(
    {"blockReadsOutsideWorkingDirectories", "blockWritesOutsideWorkingDirectories"}
)

#: Keys whose only documented value is the string ``"disable"``.
_DISABLE_KEYS = frozenset({"disableBypassPermissionsMode", "disableAutoMode"})

#: The one value a :data:`_DISABLE_KEYS` member accepts.
DISABLE_VALUE = "disable"

#: Allowed top-level keys inside the ``permissions`` object.
_PERMISSION_KEYS = _STRING_LIST_KEYS | _BOOL_KEYS | _DISABLE_KEYS | {"defaultMode"}

#: The mode this surface refuses to set — it stays behind the footgun gate (#347/#685).
BYPASS_MODE = "bypassPermissions"


def is_bypass_mode(mode: str) -> bool:
    """Whether ``mode`` spells :data:`BYPASS_MODE` once normalized (NFKC, case, punctuation).

    Only letters and digits count, after NFKC folds fullwidth and compatibility forms, so
    ``-``, ``_``, spaces, a byte order mark and zero-width characters are all ignored.

    Claude Code 2.1.319 matches a mode exactly, so a variant such as ``BypassPermissions``
    is ignored today. Since TASK-524 a mode outside the known list saves with a warning, so
    the gate refuses every such variant too: a later Claude Code that matched modes loosely
    must never turn a warned value into bypass mode.
    """
    kept = "".join(c for c in unicodedata.normalize("NFKC", mode) if c.isalnum())
    return kept.casefold() == BYPASS_MODE.casefold()


#: The recognized permission-mode values (suggestions, TASK-524: a ``defaultMode`` or a
#: subagent ``permissionMode`` outside them saves with a warning, except
#: :data:`BYPASS_MODE`, which stays refused): the canonical permission-label vocabulary
#: minus :data:`BYPASS_MODE`, which is footgun-gated and never settable here. Derived
#: from :data:`~clauster.config.PERMISSION_LABELS` so the two never drift (#685).
#: ``inherit`` (#1231) is subtracted too: it is a Clauster *launch-time* sentinel meaning
#: "pass no ``--permission-mode`` flag", not a value claude understands — writing it into
#: a real ``settings.json`` would produce a file claude rejects. The subagent surface
#: shares this set for an agent's ``permissionMode``.
RECOGNIZED_MODES = frozenset(PERMISSION_LABELS) - {BYPASS_MODE, INHERIT_PERMISSION_MODE}

#: Settings-only ``defaultMode`` aliases: ``"manual"`` is documented as an alias for
#: ``"default"`` in ``permissions.defaultMode``. It is not a launch mode, so it is not
#: in :data:`~clauster.config.PERMISSION_LABELS`.
DEFAULT_MODE_ALIASES = frozenset({"manual"})

#: Every ``defaultMode`` value this surface writes.
SETTINGS_DEFAULT_MODES = RECOGNIZED_MODES | DEFAULT_MODE_ALIASES

#: ``defaultMode`` values Claude Code ignores in project and local settings (the docs
#: list ``auto`` and ``bypassPermissions``; the latter is never settable here at all).
USER_ONLY_MODES = frozenset({"auto"})

#: The prose rule lists inside ``autoMode`` (each a list of non-empty strings; the
#: literal ``"$defaults"`` entry is an ordinary member of the list).
_AUTO_MODE_LIST_KEYS = frozenset({"environment", "allow", "soft_deny", "hard_deny"})

#: The one Boolean inside ``autoMode``.
_AUTO_MODE_BOOL_KEYS = frozenset({"classifyAllShell"})

_AUTO_MODE_KEYS = _AUTO_MODE_LIST_KEYS | _AUTO_MODE_BOOL_KEYS


def _validate_string_list(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a list of non-empty strings (opaque data).

    A rule string or path is stored verbatim and **never parsed, resolved, or
    executed** — only its type (non-empty ``str``) is checked.
    """
    if not isinstance(value, list):
        raise cw.InvalidCandidateError(f"{label} must be a list of strings")
    for item in value:
        if not isinstance(item, str) or not item:
            raise cw.InvalidCandidateError(f"{label} must contain only non-empty strings")


def validate_permissions(candidate: Any) -> None:
    """Structural validator for the whole ``permissions`` object (the Foundation hook).

    ``candidate`` is the desired ``permissions`` object: a ``dict`` whose only
    known keys are the documented ones (see the module docstring). The string lists hold
    non-empty strings, the Booleans are ``bool``, and the ``"disable"`` switches and
    ``defaultMode`` are strings. A wrong type rejects the whole write (→ 422 via
    :func:`config_write.validate_candidate`), so a partial/garbled block never lands. A key
    outside the known keys, a switch other than ``"disable"`` and a ``defaultMode`` outside
    :data:`SETTINGS_DEFAULT_MODES` save as typed with a warning (TASK-524): the lists are
    copied by hand and lag behind each new Claude Code release.

    :data:`BYPASS_MODE` is **not** a recognized mode: a request to set it as
    ``defaultMode``, in any spelling :func:`is_bypass_mode` matches, is rejected here,
    keeping ``bypassPermissions`` behind the existing footgun gate. **Never parses or
    executes any rule string.**
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError("permissions must be an object")
    if AUTO_MODE_KEY in candidate:
        # The likeliest mistake (TASK-449.1): autoMode pasted inside permissions.
        raise cw.InvalidCandidateError(
            "autoMode is a top-level settings key, not part of permissions; "
            "put it in the auto mode rules section at User scope instead"
        )
    cw.warn_unknown_keys(candidate, _PERMISSION_KEYS, "permissions key")
    for key in sorted(set(candidate) - _PERMISSION_KEYS, key=str):
        # A misspelled key such as `defaultmode` saves with a warning; it must never carry
        # bypass mode past the gate, should a later Claude Code read it.
        if any(is_bypass_mode(s) for s in _string_leaves(candidate[key])):
            raise cw.InvalidCandidateError(
                f"permissions {key!r} cannot be 'bypassPermissions' (footgun-gated)"
            )

    for key in sorted(_STRING_LIST_KEYS & set(candidate)):
        _validate_string_list(candidate[key], f"permissions {key!r}")
    for key in sorted(_BOOL_KEYS & set(candidate)):
        if not isinstance(candidate[key], bool):
            raise cw.InvalidCandidateError(f"permissions {key!r} must be true or false")
    for key in sorted(_DISABLE_KEYS & set(candidate)):
        if not isinstance(candidate[key], str):
            raise cw.InvalidCandidateError(
                f"permissions {key!r} must be a string ({DISABLE_VALUE!r})"
            )
        if candidate[key] != DISABLE_VALUE:
            cw.warn_unknown_value(f"permissions {key!r}", candidate[key], (DISABLE_VALUE,))

    if "defaultMode" in candidate:
        mode = candidate["defaultMode"]
        if not isinstance(mode, str):
            raise cw.InvalidCandidateError("permissions 'defaultMode' must be a string")
        if is_bypass_mode(mode):
            # Fail closed: bypassPermissions stays behind the footgun gate, never here.
            raise cw.InvalidCandidateError(
                "permissions 'defaultMode' cannot be 'bypassPermissions' (footgun-gated)"
            )
        if mode == INHERIT_PERMISSION_MODE:
            # Clauster's own launch sentinel (#1231), never a Claude Code mode: refused, not
            # a stale-list miss.
            raise cw.InvalidCandidateError(
                "permissions 'defaultMode' cannot be 'inherit': that is Clauster's launch "
                "setting for 'pass no --permission-mode', not a Claude Code mode"
            )
        if mode not in SETTINGS_DEFAULT_MODES:
            cw.warn_unknown_value("permissions 'defaultMode'", mode, SETTINGS_DEFAULT_MODES)


def _string_leaves(value: Any) -> list[str]:
    """Return every string in ``value``, nested dicts and lists included (iterative)."""
    found: list[str] = []
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _mask_outside_list_entries(value: Any) -> bool:
    """Whether a mask sits in ``value`` anywhere but as a direct entry of a list ``value``.

    Iterative; the caller has already checked that ``value`` is walkable.
    """
    if isinstance(value, list):
        # A direct string entry is restored by _merge_auto_mode; only containers can hide one.
        stack = [v for v in value if isinstance(v, dict | list)]
    else:
        stack = [value]
    while stack:
        node = stack.pop()
        if node == cw.REDACTION_SENTINEL:
            return True
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def validate_auto_mode(candidate: Any) -> None:
    """Structural validator for a top-level ``autoMode`` object.

    ``candidate`` must be a ``dict``. Its documented keys are the prose rule lists
    ``environment``/``allow``/``soft_deny``/``hard_deny`` (non-empty strings, the
    ``"$defaults"`` entry included) and the ``classifyAllShell`` Boolean, and a wrong type in
    one rejects the whole write. Any other key saves as typed with a warning (TASK-524), but
    a mask in it that :func:`_merge_auto_mode` cannot restore is refused. Rules are prose
    for the classifier and are **never parsed or executed** here.
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError("autoMode must be an object")
    cw.warn_unknown_keys(candidate, _AUTO_MODE_KEYS, "autoMode key")
    for key in sorted(set(candidate) - _AUTO_MODE_KEYS, key=str):
        # The read view masks prose leaves anywhere in the block, but a resent mask is only
        # restored as a direct entry of a list (_merge_auto_mode). Anywhere else it would be
        # written as the literal mask over the stored value.
        if _mask_outside_list_entries(candidate[key]):
            raise cw.InvalidCandidateError(
                f"autoMode {key!r} keeps a masked value ({cw.REDACTION_SENTINEL}) where "
                "Clauster cannot restore it; type the real value"
            )
    for key in sorted(_AUTO_MODE_LIST_KEYS & set(candidate)):
        _validate_string_list(candidate[key], f"autoMode {key!r}")
    for key in sorted(_AUTO_MODE_BOOL_KEYS & set(candidate)):
        if not isinstance(candidate[key], bool):
            raise cw.InvalidCandidateError(f"autoMode {key!r} must be true or false")


def _reject_user_only(incoming: dict[str, Any], auto_mode: Any, label: str) -> None:
    """Refuse a value Claude Code ignores outside user settings, at a project/local scope.

    A non-empty ``autoMode`` and ``defaultMode: "auto"`` never take effect from a project
    or local settings file (see the module docstring), so saving one there would show a
    policy that does not apply. Fail closed with a message that names User scope.
    """
    if auto_mode:
        raise cw.InvalidCandidateError(
            f"Claude Code reads autoMode only from user settings and ignores it in {label}; "
            "switch to User scope, or edit ~/.claude/settings.json on the host, to save auto "
            "mode rules (an empty autoMode removes the block)"
        )
    mode = incoming.get("defaultMode")
    if mode in USER_ONLY_MODES:
        raise cw.InvalidCandidateError(
            f"permissions 'defaultMode' {mode!r} does not take effect from {label}; "
            "set it at User scope instead"
        )


def _is_masked_entry(entry: Any) -> bool:
    """Whether an ``autoMode`` list entry is secret-shaped anywhere in its text.

    Masks the whole entry when the line-oriented detector changes it (a credential URL
    or ``${...}`` anywhere, or a ``token: ...``-style line) or the structural one would.
    Over-masking only costs a resend; under-masking leaks.
    """
    if not isinstance(entry, str):
        return False
    return cw.redact_secret_lines(entry) != entry or cw.redact_secrets([entry]) != [entry]


def _mask_prose_leaves(node: Any) -> Any:
    """Return ``node`` with every string leaf that :func:`_is_masked_entry` flags masked."""
    if isinstance(node, dict):
        return {k: _mask_prose_leaves(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_prose_leaves(v) for v in node]
    return cw.REDACTION_SENTINEL if _is_masked_entry(node) else node


def _redact_auto_mode(value: Any) -> Any:
    """Return the display copy of a stored ``autoMode`` value (see the module docstring).

    Two passes over the whole value, whatever its shape (a hand-edited or cloned file can
    hold anything): first :func:`~clauster.config_write.redact_secrets`, which keeps its
    key-name hint, so a value under a secret-named key is masked; then every string leaf
    that :func:`_is_masked_entry` flags is masked whole. The second pass is what catches
    a secret in the middle of a prose entry.
    """
    return _mask_prose_leaves(cw.redact_secrets(value))


def _merge_auto_mode(incoming: dict[str, Any], stored: Any) -> dict[str, Any]:
    """Restore each resent sentinel in ``incoming`` from the stored masked entries.

    Per list, the n-th sentinel takes the n-th stored entry that :func:`_is_masked_entry`
    flags, so adding, removing or moving *unmasked* entries keeps every secret. When the
    number of sentinels differs from the number of stored masked entries, the mapping is
    ambiguous (a mask was deleted, duplicated, or has nothing stored behind it), so the
    write is refused whole rather than guessed. A list with no sentinel is written as
    sent. Must run under the file lock, on the stored value read there.
    """
    stored_obj = stored if isinstance(stored, dict) else {}
    # The masked entries are the ones the READ VIEW masked: a key outside the known lists
    # with a secret-shaped name masks every entry of its list (TASK-524), not only the
    # entries _is_masked_entry flags.
    view = _redact_auto_mode(stored_obj)
    out: dict[str, Any] = {}
    for key, member in incoming.items():
        if not isinstance(member, list) or cw.REDACTION_SENTINEL not in member:
            out[key] = member
            continue
        stored_list = stored_obj.get(key)
        shown = view.get(key) if isinstance(view, dict) else None
        masked = (
            [e for e, v in zip(stored_list, shown, strict=True) if v == cw.REDACTION_SENTINEL]
            if isinstance(stored_list, list)
            and isinstance(shown, list)
            and len(shown) == len(stored_list)
            else []
        )
        if member.count(cw.REDACTION_SENTINEL) != len(masked):
            raise cw.InvalidCandidateError(
                f"autoMode {key!r} has {member.count(cw.REDACTION_SENTINEL)} masked "
                f"entries but {len(masked)} are stored; keep each masked entry as loaded, "
                "or type the full entry again"
            )
        restored = iter(masked)
        out[key] = [next(restored) if e == cw.REDACTION_SENTINEL else e for e in member]
    return out


def _validate_write(incoming: Any, auto_mode: Any) -> None:
    """Run both structural validators (``auto_mode`` only when the caller sent one)."""
    cw.validate_candidate(incoming, validate_permissions)
    if auto_mode is not None:
        cw.validate_candidate(auto_mode, validate_auto_mode)


def _read_permissions(path: Path) -> tuple[dict[str, Any], Any, str]:
    """Return ``(permissions, auto_mode, content_hash)`` for a settings file at ``path``.

    The hash is over the *current file bytes* (empty digest when absent) — the caller
    echoes it back on write so the stale-hash guard can reject a stale write (409).
    Permission rules are not secret-shaped, so no redaction is applied to them; a
    missing file reads as an empty permissions block. ``auto_mode`` is the on-disk
    ``autoMode`` value passed through :func:`_redact_auto_mode`, or ``None`` when the
    file has no such key. A non-object value is returned as it is (after
    redaction), so the editor shows what is stored rather than hiding it.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = cw.load_settings_json_obj(raw)
    permissions = data.get(PERMISSIONS_KEY)
    permissions = permissions if isinstance(permissions, dict) else {}
    auto_mode = _redact_auto_mode(data[AUTO_MODE_KEY]) if AUTO_MODE_KEY in data else None
    return permissions, auto_mode, cw.hash_bytes(raw)


def _locked_write(
    path: Path, incoming: dict[str, Any], auto_mode: Any, expected_hash: str | None
) -> None:
    """Locked stale-hash-guarded write of ``permissions`` (and ``autoMode``) at ``path``.

    The caller must have already validated both values. ``permissions`` is replaced
    wholesale, except that an empty one is not added to a file that has no such key (the
    Permissions tab's autoMode Remove resends the empty block it read, and must change
    only ``autoMode``, TASK-509). ``auto_mode`` ``None`` leaves the stored ``autoMode`` as
    it is; an empty dict removes the key; any other dict replaces it after :func:`_merge_auto_mode`
    restores each resent sentinel from the stored value (or refuses the write). Both
    keys change in one locked replace, so they never race each other.
    """

    def _mutate(current_bytes: bytes) -> dict[str, Any]:
        """Check the expected hash, then swap in the new subtree(s)."""
        if expected_hash is None:
            if current_bytes:
                raise cw.StaleConfigWriteError(f"{path.name} already exists; a hash is required")
        elif cw.hash_bytes(current_bytes) != expected_hash:
            raise cw.StaleConfigWriteError("config file changed on disk since it was loaded")
        current = cw.load_settings_json_obj(current_bytes)
        if incoming or PERMISSIONS_KEY in current:
            current[PERMISSIONS_KEY] = incoming
        if auto_mode is not None:
            if auto_mode:
                current[AUTO_MODE_KEY] = _merge_auto_mode(auto_mode, current.get(AUTO_MODE_KEY))
            else:
                current.pop(AUTO_MODE_KEY, None)
        return current

    locked_replace_json_file(path, _mutate, render=cw.render_json)


def read_project_permissions(project_dir: Path) -> tuple[dict[str, Any], Any, str]:
    """Return ``(permissions, auto_mode, content_hash)`` for a project's ``settings.json``."""
    return _read_permissions(cw.project_settings_path(project_dir))


def write_project_permissions(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None, *, auto_mode: Any
) -> None:
    """Validate + write the project ``.claude/settings.json`` ``permissions`` block.

    Ensures the ``<project>/.claude`` parent exists (so a first write to a project that
    has no ``.claude`` dir yet does not fail the atomic writer's ``mkstemp`` in a
    missing directory), then runs this surface's share of the Foundation pipeline —
    structural validation, the stale-hash guard, and the locked atomic subtree write.
    The capability gate, type-the-name confirm, and path containment are the route's,
    upstream of here. The candidate is validated *before* the directory is created, so
    a bad shape (422) leaves the filesystem untouched. ``auto_mode`` follows
    :func:`_locked_write`; a non-empty one is refused at this scope.
    """
    _validate_write(incoming, auto_mode)
    _reject_user_only(incoming, auto_mode, "project settings (.claude/settings.json)")
    path = cw.project_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _locked_write(path, incoming, auto_mode, expected_hash)


def read_user_permissions(settings_json: Path) -> tuple[dict[str, Any], Any, str]:
    """Return ``(permissions, auto_mode, content_hash)`` for ``~/.claude/settings.json``."""
    return _read_permissions(settings_json)


def write_user_permissions(
    settings_json: Path, incoming: dict[str, Any], expected_hash: str | None, *, auto_mode: Any
) -> None:
    """Validate + write the user-scope ``~/.claude/settings.json`` ``permissions`` block.

    Ensures the ``~/.claude`` parent exists, then runs this surface's share of the
    Foundation pipeline — structural validation, the stale-hash guard, and the locked
    atomic subtree write; the capability gate, type-the-name confirm, and path
    containment are the route's, upstream of here. Unlike the #688 user-scope MCP
    writer (which edits a ``~/.claude.json``
    subtree), this writes a *separate real file* and so carries the same stale-hash
    guard as the project scope. The candidate is validated *before* the directory is
    created, so a bad shape (422) writes nothing. This is the one scope where Claude
    Code reads ``autoMode``, so ``auto_mode`` is written here as sent.
    """
    _validate_write(incoming, auto_mode)
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    _locked_write(settings_json, incoming, auto_mode, expected_hash)


def read_project_local_permissions(project_dir: Path) -> tuple[dict[str, Any], Any, str]:
    """Return ``(permissions, auto_mode, content_hash)`` for a ``settings.local.json``."""
    return _read_permissions(cw.project_local_settings_path(project_dir))


def write_project_local_permissions(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None, *, auto_mode: Any
) -> None:
    """Validate + write the local-scope ``.claude/settings.local.json`` ``permissions`` block.

    Third (local) scope, sibling of the project/user writers above: the same share of the
    Foundation pipeline (structural validation, stale-hash guard, locked atomic subtree
    write), targeting a *third* file that is you, this project only. The capability gate,
    type-the-name confirm, and path containment are the route's, upstream of here.
    :func:`~clauster.config_write.ensure_gitignored` runs *before* the
    write (fail-closed: the write creates a secret-bearing ``.bak``), so
    ``settings.local.json`` and that backup are never accidentally committed (#766) —
    idempotent, so an already-gitignored file is a no-op there. A non-empty
    ``auto_mode`` is refused at this scope, as at project scope.
    """
    _validate_write(incoming, auto_mode)
    _reject_user_only(incoming, auto_mode, "local settings (.claude/settings.local.json)")
    path = cw.project_local_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Ignore BEFORE writing (fail-closed order) — see `cw.ensure_gitignored` for why.
    cw.ensure_gitignored(project_dir, ".claude/settings.local.json", ignore_backup_sibling=True)
    _locked_write(path, incoming, auto_mode, expected_hash)
