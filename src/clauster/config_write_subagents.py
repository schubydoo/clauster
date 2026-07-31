"""Subagents config-write surface (#767) over the #347 Foundation + #766 file writer.

A subagent is a single Markdown file with a YAML frontmatter block followed by a
freeform body (the system prompt), at exactly two scopes — **unlike** the JSON-subtree
surfaces (hooks/permissions/settings) and CLAUDE.md, there is no genuine ``local``
scope here: Claude Code itself only ever reads subagents from ``~/.claude/agents/``
(user) and ``<project>/.claude/agents/`` (project); it has no local-scope directory
analogous to ``settings.local.json`` / ``CLAUDE.local.md``:

* **user** — ``~/.claude/agents/<name>.md``, available across every project.
* **project** — ``<project>/.claude/agents/<name>.md``, specific to one project.

This module owns exactly what the sibling children own: a **pure structural
validator** for the frontmatter shape, and the read/write/delete/list functions that
run the fail-closed Foundation pipeline (gate → confirm → validate → contain →
stale-hash → atomic write) via the #766 file/dir-writer primitive
(:mod:`clauster.config_file_writer`) rather than the JSON-subtree writer — a subagent
is a whole file, not a key inside ``settings.json``.

**Validate-never-execute, extended to a capability grant.** The frontmatter's
``tools``/``disallowedTools`` fields are a capability grant (what the subagent may
do), so they get the same structural-shape-only check as everything else here: never
resolved against a real tool registry, just checked for the right shape, and the
whole surface stays behind the confirm gate. A ``hooks`` block inside a subagent's
frontmatter is exactly as RCE-sensitive as a project/user ``hooks`` block, so it is
validated by reusing :func:`clauster.config_write_hooks.validate_hooks` wholesale —
including that validator's plugin-owned-command rejection — rather than duplicating
the logic. ``permissionMode`` reuses the same recognized-mode vocabulary as
:mod:`clauster.config_write_permissions` (``bypassPermissions`` stays footgun-gated,
never settable here).

**Plugin / built-in agents are read-only.** Claude Code ships three built-in
subagents (``general-purpose``, ``Explore``, ``Plan``) that are compiled in, not
files — there is nothing on disk to edit or delete, so a write/delete targeting one
of :data:`BUILTIN_AGENT_NAMES` is refused (:class:`ReadOnlyAgentError`) before any
I/O. Plugin-provided subagents live in the *plugin's own* ``agents/`` directory
(never inside the two directories this surface manages), but an operator or install
script could still place a symlink or a copied file into ``.claude/agents/`` that
shadows one — that is treated as read-only too, using the same two structural signals
:mod:`clauster.config_write_hooks` uses for plugin-owned hook commands: a symlink (a
real project/user agent is always a plain file this surface itself wrote) or the
literal ``${CLAUDE_PLUGIN_ROOT}`` interpolation appearing anywhere in the raw bytes
(that interpolation only resolves inside a plugin's own bundle). Either signal is
sufficient, and both are content/metadata sniffs — nothing is ever resolved, spawned,
or executed to make this determination.

**Symlink ordering (a read-only-contract correctness fix).** The read/write/delete
paths classify a **symlink** on the *un-resolved* ``<agents>/<name>.md`` path — via
:func:`_unresolved_target` + :meth:`~pathlib.Path.is_symlink` — **before**
:func:`_resolve` runs. Were the containment resolve to run first, it would *follow*
the symlink and reject its out-of-tree target as a
:class:`~clauster.config_write.PathEscapeError` (400), so a plugin symlink would
wrongly surface as a path-escape instead of the read-only status a plugin agent is
promised: GET returns a content-less read-only ``200`` without following the link, and
write/delete raise :class:`ReadOnlyAgentError` (403) without reading the target. A
genuinely escaping **non-symlink** input still fails closed as a path-escape via
:func:`_resolve`. **Listing is the exception and does read past the boundary:**
:func:`_list_agents` calls ``is_file()`` and ``read_bytes()`` on the direntry — both
*follow* symlinks — before :func:`_is_read_only_file` classifies it, so a planted
symlink's out-of-tree target is read and its frontmatter ``description`` reaches the
listing (the entry is still marked plugin-owned and non-editable). The
never-followed/never-read guarantee therefore holds for GET/PUT/DELETE only.

**Filename safety.** The subagent ``name`` maps directly to ``<name>.md``; the name
must match :data:`_NAME_RE` (Claude Code's own "lowercase letters, digits, and
hyphens" shape — no path separators, no ``..``, no leading hyphen), and every
read/write/delete additionally resolves through
:func:`clauster.config_file_writer.resolve_contained_path` so a filename that somehow
passed the regex still cannot escape the agents directory.

**Redaction.** The freeform *body* (the system prompt) is never redacted — like
CLAUDE.md, it is operator-authored content, not credential storage, and masking it
would corrupt the operator's own prose. The *frontmatter*, when surfaced as a parsed
convenience field (a subagent's ``mcpServers``/``env``-shaped values could carry
secrets), is run through the structural :func:`clauster.config_write.redact_secrets`
before being returned — this is a read-only display field, never the value round
tripped back on write, so masking it costs nothing on the write side. The raw
``content`` field returned for editing is the unredacted file bytes (the round trip
CLAUDE.md, hooks, and permissions all use), so a save always re-submits the real text.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from . import config_file_writer as fw
from . import config_write as cw
from .config_write_hooks import validate_hooks
from .config_write_permissions import BYPASS_MODE, RECOGNIZED_MODES

#: The directory name holding subagent files under ``.claude/`` at either scope.
AGENTS_DIRNAME = "agents"

#: Size cap for a single subagent file (frontmatter + body), matching the CLAUDE.md cap.
MAX_BYTES = 64 * 1024

#: Claude Code's own subagent-name shape: lowercase letters, digits, and hyphens; must
#: start with a letter (no leading hyphen/digit) and never contain a path separator,
#: ``.``, or ``..`` — the regex itself is the containment defense-in-depth, on top of
#: :func:`clauster.config_file_writer.resolve_contained_path`.
_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

#: Claude Code's built-in subagents — compiled into the CLI, never files on disk.
#: Matched case-insensitively against a candidate name (``Explore``/``Plan`` are
#: capitalized in Claude Code's own vocabulary; ``general-purpose`` is lowercase).
BUILTIN_AGENT_NAMES = frozenset({"general-purpose", "explore", "plan"})

#: Marker identifying subagent content as **plugin-owned** — mirrors
#: ``clauster.config_write_hooks._PLUGIN_ROOT_MARKER``: this interpolation only
#: resolves inside a plugin's own bundle, so its presence anywhere in a subagent
#: file's raw bytes marks it as copied from (or intending to shadow) a plugin agent.
_PLUGIN_ROOT_MARKER = "CLAUDE_PLUGIN_ROOT"

#: Frontmatter keys required on every subagent (Claude Code's own requirement).
_REQUIRED_FRONTMATTER_KEYS = frozenset({"name", "description"})

#: Frontmatter keys whose value is a non-empty string OR a non-empty list of
#: non-empty strings (Claude Code accepts either a comma-separated string or an
#: array for these — this repo's own shipped subagents use the string form).
_STRING_OR_LIST_KEYS = frozenset({"tools", "disallowedTools"})

# A frontmatter block: ``---`` on its own line, the YAML body, then a closing ``---``
# on its own line (optionally followed by the rest of the file). DOTALL so `.` spans
# newlines inside the captured YAML; non-greedy so the FIRST closing `---` ends the
# block (a body that itself contains a `---` line is not swallowed into the header).
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)


class AgentNotFoundError(cw.ConfigWriteError):
    """No subagent file exists at the resolved name (→ 404; raised by the read path only).

    Delete does not use it: an absent ordinary name no-ops to ``False``.
    """


class ReadOnlyAgentError(cw.ConfigWriteError):
    """The named agent is a Claude Code built-in or plugin-provided (→ 403)."""


def is_valid_agent_name(name: object) -> bool:
    """Whether ``name`` is a safe, Claude-Code-shaped subagent identifier."""
    return isinstance(name, str) and _NAME_RE.fullmatch(name) is not None


def is_builtin_agent(name: str) -> bool:
    """Whether ``name`` collides with a Claude Code built-in (case-insensitive)."""
    return name.lower() in BUILTIN_AGENT_NAMES


def user_agents_dir(claude_json: Path) -> Path:
    """Return ``~/.claude/agents`` given the resolved ``~/.claude.json`` path."""
    return claude_json.parent / ".claude" / AGENTS_DIRNAME


def project_agents_dir(project_dir: Path) -> Path:
    """Return ``<project_dir>/.claude/agents``."""
    return project_dir / ".claude" / AGENTS_DIRNAME


def _resolve(root: Path, name: str) -> Path:
    """Resolve ``name`` to ``root/<name>.md``, contained, or fail closed.

    Rejects a malformed name *before* touching the filesystem (mirrors
    ``claude_md._resolve``'s rewrap of :class:`~clauster.config_file_writer.PathEscapeError`
    as :class:`~clauster.config_write.PathEscapeError`, so every failure from this
    surface is a single :class:`~clauster.config_write.ConfigWriteError` family the app
    layer's one error mapper already handles).
    """
    if not is_valid_agent_name(name):
        raise cw.PathEscapeError(f"invalid subagent name: {name!r}")
    try:
        return fw.resolve_contained_path(root, f"{name}.md")
    except fw.PathEscapeError as exc:
        raise cw.PathEscapeError(str(exc)) from exc


def _unresolved_target(root: Path, name: str) -> Path:
    """Return the **un-resolved** ``root/<name>.md`` after validating ``name``.

    Unlike :func:`_resolve`, this deliberately does **not** call
    :func:`~clauster.config_file_writer.resolve_contained_path` — it never follows
    symlinks. The returned path (``<name>.md``, a single validated component, joined
    onto ``root``) is always *inside* ``root``; only its symlink *target*, if any,
    may point elsewhere. Callers check :meth:`~pathlib.Path.is_symlink` on it to
    classify a plugin-provided **symlink** as read-only **before** :func:`_resolve`
    would follow and reject the escaping target as a
    :class:`~clauster.config_write.PathEscapeError` — the ordering fix for the
    read-only-contract break (a plugin symlink must be a 403/read-only-200, never a
    400 path-escape). An invalid name still fails closed here, exactly as
    :func:`_resolve` does.
    """
    if not is_valid_agent_name(name):
        raise cw.PathEscapeError(f"invalid subagent name: {name!r}")
    return root / f"{name}.md"


def _plugin_symlink_doc(name: str) -> dict[str, Any]:
    """Return the read-only detail doc for a plugin-provided **symlink** agent.

    A symlinked ``<agents>/<name>.md`` points at a plugin file *outside* the agents
    dir, so this read path **never reads it** (following it would be an arbitrary-file
    read past the containment boundary). Surfaced instead as a content-less
    read-only doc, the same shape the built-in synthetic doc uses: shown, marked
    plugin-owned, never editable.
    """
    return {
        "name": name,
        "source": "plugin",
        "editable": False,
        "exists": True,
        "content": "",
        "frontmatter": {},
        "hash": None,
    }


def _is_read_only_file(path: Path, raw: bytes) -> bool:
    """Whether an on-disk subagent file is plugin-provided, never editable here.

    See the module docstring for the two structural signals (symlink, or the
    ``${CLAUDE_PLUGIN_ROOT}`` marker anywhere in the raw bytes). Pure content/metadata
    sniff — nothing here is ever resolved, parsed as YAML, or executed.
    """
    if path.is_symlink():
        return True
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return _PLUGIN_ROOT_MARKER in text


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into ``(frontmatter, body)``, or reject a malformed block.

    ``frontmatter`` is loaded with :func:`yaml.safe_load` (never the unsafe loader)
    and must be a mapping; a missing/malformed frontmatter block, invalid YAML, or a
    non-mapping frontmatter all raise :class:`~clauster.config_write.InvalidCandidateError`.
    The body is returned verbatim (never parsed or executed — it is the subagent's
    freeform system prompt).
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise cw.InvalidCandidateError(
            "subagent content must start with a YAML frontmatter block ('---' ... '---')"
        )
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise cw.InvalidCandidateError(f"frontmatter is not valid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise cw.InvalidCandidateError("frontmatter must be a YAML mapping (an object)")
    return data, text[match.end() :]


def _validate_string_or_string_list(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a non-empty string or a non-empty list of them.

    Structural only: a comma-separated ``tools`` string or list entry is never split,
    resolved against a real tool registry, or otherwise interpreted here — that grant
    is enforced later, by Claude Code itself, when the subagent actually runs.
    """
    if isinstance(value, str):
        if not value.strip():
            raise cw.InvalidCandidateError(f"{label} must be a non-empty string")
        return
    if isinstance(value, list):
        if not value or not all(isinstance(v, str) and v.strip() for v in value):
            raise cw.InvalidCandidateError(
                f"{label} must be a non-empty list of non-empty strings"
            )
        return
    raise cw.InvalidCandidateError(f"{label} must be a string or a list of strings")


def validate_frontmatter(candidate: Any, *, expected_name: str | None = None) -> None:
    """Structural validator for a subagent's parsed frontmatter mapping.

    Type-checks the recognized keys only (Claude Code's documented subagent
    frontmatter fields) — never resolves a tool name, spawns/parses a ``hooks``
    command, or connects to an ``mcpServers`` entry. A missing required key
    (``name``/``description``) rejects the whole write (→ 422). An UNRECOGNIZED key
    is passed through untouched rather than rejected: Claude Code tolerates
    forward-compatible frontmatter, so a hardcoded allowlist produced false "unknown
    key" errors on valid subagents (#958/DF-3). The security-relevant keys it DOES
    know (``hooks`` / ``mcpServers`` / ``permissionMode``) are still fully validated
    below when present, and no key is ever executed — this only stops rejecting keys
    the surface has no opinion about.

    When ``expected_name`` is given (the write path always supplies it — the target
    filename, ``<name>.md``), the frontmatter's own ``name`` must match it exactly:
    a subagent's identity is its filename, and a mismatch would silently rename what
    the operator thinks they are editing.
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError("frontmatter must be an object")
    missing = _REQUIRED_FRONTMATTER_KEYS - set(candidate)
    if missing:
        raise cw.InvalidCandidateError(f"frontmatter is missing required keys: {sorted(missing)}")

    name = candidate["name"]
    if not is_valid_agent_name(name):
        raise cw.InvalidCandidateError(
            "frontmatter 'name' must be lowercase letters, digits, and hyphens "
            "(no leading hyphen, no path separators)"
        )
    if expected_name is not None and name != expected_name:
        raise cw.InvalidCandidateError(
            f"frontmatter 'name' ({name!r}) must match the target agent name ({expected_name!r})"
        )

    description = candidate["description"]
    if not isinstance(description, str) or not description.strip():
        raise cw.InvalidCandidateError("frontmatter 'description' must be a non-empty string")

    for key in _STRING_OR_LIST_KEYS:
        if key in candidate:
            _validate_string_or_string_list(candidate[key], f"frontmatter {key!r}")

    if "skills" in candidate:
        skills = candidate["skills"]
        if not isinstance(skills, list) or not all(
            isinstance(v, str) and v.strip() for v in skills
        ):
            raise cw.InvalidCandidateError(
                "frontmatter 'skills' must be a list of non-empty strings"
            )

    if "model" in candidate:
        model = candidate["model"]
        if not isinstance(model, str) or not model.strip():
            raise cw.InvalidCandidateError("frontmatter 'model' must be a non-empty string")

    if "permissionMode" in candidate:
        mode = candidate["permissionMode"]
        if not isinstance(mode, str):
            raise cw.InvalidCandidateError("frontmatter 'permissionMode' must be a string")
        if mode == BYPASS_MODE:
            # Fail closed: bypassPermissions stays behind the existing footgun gate,
            # never settable through a subagent's frontmatter either.
            raise cw.InvalidCandidateError(
                "frontmatter 'permissionMode' cannot be 'bypassPermissions' (footgun-gated)"
            )
        if mode not in RECOGNIZED_MODES:
            raise cw.InvalidCandidateError(
                f"unknown frontmatter permissionMode {mode!r} "
                f"(want one of {sorted(RECOGNIZED_MODES)})"
            )

    if "mcpServers" in candidate:
        servers = candidate["mcpServers"]
        if not isinstance(servers, dict) or not all(isinstance(v, dict) for v in servers.values()):
            raise cw.InvalidCandidateError(
                "frontmatter 'mcpServers' must be an object mapping server name to object"
            )

    if "hooks" in candidate:
        # Reuse the hooks structural validator wholesale (including its
        # plugin-owned-command rejection) rather than duplicating the RCE-sensitive
        # shape checks: a subagent's own hooks are exactly as dangerous as a
        # project/user hooks block. STRUCTURE ONLY — no command is ever executed.
        try:
            validate_hooks(candidate["hooks"])
        except cw.InvalidCandidateError as exc:
            raise cw.InvalidCandidateError(f"frontmatter 'hooks': {exc}") from exc

    if "maxTurns" in candidate:
        turns = candidate["maxTurns"]
        if not isinstance(turns, int) or isinstance(turns, bool) or turns <= 0:
            raise cw.InvalidCandidateError("frontmatter 'maxTurns' must be a positive integer")

    if "initialPrompt" in candidate and not isinstance(candidate["initialPrompt"], str):
        raise cw.InvalidCandidateError("frontmatter 'initialPrompt' must be a string")

    if "memory" in candidate and not isinstance(candidate["memory"], str | dict):
        raise cw.InvalidCandidateError("frontmatter 'memory' must be a string or an object")

    if "effort" in candidate:
        effort = candidate["effort"]
        if not isinstance(effort, str) or not effort.strip():
            raise cw.InvalidCandidateError("frontmatter 'effort' must be a non-empty string")

    if "background" in candidate and not isinstance(candidate["background"], bool):
        raise cw.InvalidCandidateError("frontmatter 'background' must be a boolean")

    if "isolation" in candidate:
        isolation = candidate["isolation"]
        if not isinstance(isolation, str) or not isolation.strip():
            raise cw.InvalidCandidateError("frontmatter 'isolation' must be a non-empty string")

    if "color" in candidate:
        color = candidate["color"]
        if not isinstance(color, str) or not color.strip():
            raise cw.InvalidCandidateError("frontmatter 'color' must be a non-empty string")

    if "env" in candidate:
        # Dropping the unknown-key allowlist makes ``env`` reachable for the first time
        # (it was never in the old recognized-key set). Claude Code loads ``env`` as a
        # name→value environment map, so a non-mapping payload (``env: 42``) or a
        # non-scalar value would land on disk here and only fail later when Claude Code
        # tries to load the subagent — validate the shape at write time instead. STRUCTURE
        # ONLY: names/values are never resolved or exported here.
        env = candidate["env"]
        if not isinstance(env, dict):
            raise cw.InvalidCandidateError(
                "frontmatter 'env' must be an object mapping variable names to scalar values"
            )
        for var_name, var_value in env.items():
            if not isinstance(var_name, str) or not var_name.strip():
                raise cw.InvalidCandidateError("frontmatter 'env' keys must be non-empty strings")
            if not isinstance(var_value, str | int | float | bool):
                raise cw.InvalidCandidateError(
                    f"frontmatter 'env' value for {var_name!r} must be a scalar "
                    "(string, number, or boolean)"
                )


def validate_agent_content(candidate: Any, *, expected_name: str | None = None) -> None:
    """Structural validator for a whole subagent file (the Foundation hook).

    ``candidate`` must be a ``str`` under :data:`MAX_BYTES` (UTF-8 encoded) that
    parses as a frontmatter block followed by a body (see :func:`parse_frontmatter`),
    whose frontmatter passes :func:`validate_frontmatter`. The body itself is never
    inspected beyond the size cap — it is the subagent's freeform system prompt,
    opaque content exactly like CLAUDE.md.
    """
    if not isinstance(candidate, str):
        raise cw.InvalidCandidateError("content must be a string")
    size = len(candidate.encode("utf-8"))
    if size > MAX_BYTES:
        raise cw.InvalidCandidateError(f"content is {size} bytes, over the {MAX_BYTES} byte cap")
    frontmatter, _body = parse_frontmatter(candidate)
    validate_frontmatter(frontmatter, expected_name=expected_name)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def _builtin_entries() -> list[dict[str, Any]]:
    """Return the always-present, never-editable built-in agent summary entries."""
    return [
        {"name": n, "source": "built-in", "editable": False, "description": None}
        for n in sorted(BUILTIN_AGENT_NAMES)
    ]


def _list_agents(root: Path, source: str) -> list[dict[str, Any]]:
    """List the real on-disk subagents directly under ``root`` (a ``.claude/agents`` dir).

    A missing directory lists as empty (a project/user with no subagents yet is not
    an error). Each entry is read once to detect the plugin-read-only signal and
    (best-effort) to surface its ``description`` for a summary view; a file that
    fails to decode as UTF-8 or parse as frontmatter still appears (its ``content``
    is only unavailable/rejected from the single-agent read, not from this listing),
    with ``description: None``. A filename that doesn't parse as a valid agent name
    is skipped outright — it was never written by this surface and can't be resolved
    by name through :func:`_resolve` either, so it is neither editable nor listable
    as a named resource.

    Unlike the read/write/delete paths, this one does **not** classify a symlink first:
    ``is_file()`` and ``read_bytes()`` both *follow* symlinks, so a planted symlink's
    out-of-tree target IS read here and its frontmatter ``description`` reaches the
    listing. The entry is still marked plugin-owned and non-editable, but the surface's
    "the target is never followed/read" guarantee does not hold on this path.
    """
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.suffix != ".md" or not entry.is_file():
            continue
        name = entry.stem
        if not is_valid_agent_name(name):
            continue
        try:
            raw = entry.read_bytes()
        except OSError:
            continue
        read_only = _is_read_only_file(entry, raw)
        description: str | None = None
        try:
            frontmatter, _body = parse_frontmatter(raw.decode("utf-8"))
            value = frontmatter.get("description")
            if isinstance(value, str):
                description = value
        except (UnicodeDecodeError, cw.InvalidCandidateError):
            description = None
        out.append(
            {
                "name": name,
                "source": "plugin" if read_only else source,
                "editable": not read_only,
                "description": description,
            }
        )
    return out


def list_user_agents(claude_json: Path) -> list[dict[str, Any]]:
    """Return every subagent visible at user scope: real files, then built-ins."""
    return _list_agents(user_agents_dir(claude_json), "user") + _builtin_entries()


def list_project_agents(project_dir: Path) -> list[dict[str, Any]]:
    """Return every subagent visible at project scope: real files, then built-ins."""
    return _list_agents(project_agents_dir(project_dir), "project") + _builtin_entries()


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def _read_agent(root: Path, name: str, source: str) -> dict[str, Any]:
    """Return the full detail doc for one subagent, or raise a typed read error.

    A built-in name reads as a synthetic, non-existent, non-editable doc (200-shaped
    — it is "shown but not editable", never a 404: Claude Code really does offer that
    agent, there is just no file backing it here). A missing real file raises
    :class:`AgentNotFoundError` (→ 404). ``frontmatter`` is a **derived, redacted**
    display field (:func:`~clauster.config_write.redact_secrets`); ``content`` is the
    raw, unredacted file text — the value a subsequent write must re-submit.
    """
    if is_builtin_agent(name):
        return {
            "name": name,
            "source": "built-in",
            "editable": False,
            "exists": False,
            "content": "",
            "frontmatter": {},
            "hash": None,
        }
    # Classify a plugin SYMLINK before the containment resolve (see _unresolved_target),
    # and never read its target.
    candidate = _unresolved_target(root, name)
    if candidate.is_symlink():
        return _plugin_symlink_doc(name)
    target = _resolve(root, name)
    try:
        raw = target.read_bytes()
    except FileNotFoundError as exc:
        raise AgentNotFoundError(f"no subagent named {name!r}") from exc
    read_only = _is_read_only_file(target, raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise cw.InvalidCandidateError(f"{name}.md is not valid UTF-8") from exc
    try:
        parsed, _body = parse_frontmatter(text)
        frontmatter = cw.redact_secrets(parsed)
    except cw.InvalidCandidateError:
        # An unparsable frontmatter block still surfaces via `content` (so the
        # operator can see and fix it); the derived display field just degrades.
        frontmatter = {}
    return {
        "name": name,
        "source": "plugin" if read_only else source,
        "editable": not read_only,
        "exists": True,
        "content": text,
        "frontmatter": frontmatter,
        "hash": cw.hash_bytes(raw),
    }


def read_user_agent(claude_json: Path, name: str) -> dict[str, Any]:
    """Return the detail doc for one user-scope subagent."""
    return _read_agent(user_agents_dir(claude_json), name, "user")


def read_project_agent(project_dir: Path, name: str) -> dict[str, Any]:
    """Return the detail doc for one project-scope subagent."""
    return _read_agent(project_agents_dir(project_dir), name, "project")


# ---------------------------------------------------------------------------
# Write (add/edit)
# ---------------------------------------------------------------------------


def _write_agent(root: Path, name: str, content: str, expected_hash: str | None) -> None:
    """Validate + read-only-guard + stale-hash-guard + atomically write one subagent.

    Gate order (mirrors the Foundation): built-in-name refusal → plugin-symlink
    refusal (before the containment resolve) → path containment → existing-file
    plugin-read-only refusal → structural validation (422, nothing written) →
    stale-hash guard (409) → atomic write via
    :func:`clauster.config_file_writer.write_file`. A name colliding with a built-in,
    a plugin symlink, or an existing plugin-owned (marker) file is refused with a
    :class:`ReadOnlyAgentError` (403) *before* any content is validated or written —
    a plugin symlink is caught before :func:`_resolve` would mis-report it as a
    path-escape 400, and its target is never followed.

    The stale-hash guard is **not** atomic with the write: ``current`` is read here, and
    :func:`clauster.config_file_writer.write_file` is called without its ``verify=``
    callback, so the comparison happens outside that function's per-target lock. Two
    concurrent writes — or an external edit landing between the read and the replace —
    can both pass the 409 guard and lost-update. (:mod:`clauster.claude_md`'s scoped
    write passes ``verify=`` and does keep the guard in one critical section.)
    """
    if is_builtin_agent(name):
        raise ReadOnlyAgentError(f"{name!r} is a Claude Code built-in agent (read-only)")
    candidate = _unresolved_target(root, name)
    if candidate.is_symlink():
        raise ReadOnlyAgentError(f"{name!r} is a plugin-provided subagent (read-only)")
    target = _resolve(root, name)
    try:
        current = target.read_bytes()
        found = True
    except FileNotFoundError:
        current = b""
        found = False
    if found and _is_read_only_file(target, current):
        raise ReadOnlyAgentError(f"{name!r} is a plugin-provided subagent (read-only)")

    cw.validate_candidate(content, lambda c: validate_agent_content(c, expected_name=name))

    if expected_hash is None:
        if found:
            raise cw.StaleConfigWriteError(f"{name}.md already exists; a hash is required")
    elif cw.hash_bytes(current) != expected_hash:
        raise cw.StaleConfigWriteError(f"{name}.md changed on disk since it was loaded")

    fw.write_file(root, f"{name}.md", content)


def write_user_agent(
    claude_json: Path, name: str, content: str, expected_hash: str | None
) -> None:
    """Validate + write a user-scope subagent (``~/.claude/agents/<name>.md``)."""
    _write_agent(user_agents_dir(claude_json), name, content, expected_hash)


def write_project_agent(
    project_dir: Path, name: str, content: str, expected_hash: str | None
) -> None:
    """Validate + write a project-scope subagent (``<project>/.claude/agents/<name>.md``)."""
    _write_agent(project_agents_dir(project_dir), name, content, expected_hash)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def _delete_agent(root: Path, name: str) -> bool:
    """Delete one subagent file; return whether it existed. Refuses read-only names.

    A built-in name is refused (nothing to delete, but fail closed rather than a
    silent no-op 404, so the caller can distinguish "never existed" from "not
    yours to remove"). A plugin symlink is refused *before* the containment resolve
    (so it is a clean 403, never a path-escape 400) and its target is never read; a
    plugin-owned (marker) file is likewise refused. A genuinely absent, ordinary
    name returns ``False`` (idempotent, matches
    :func:`clauster.config_file_writer.delete_path`).
    """
    if is_builtin_agent(name):
        raise ReadOnlyAgentError(f"{name!r} is a Claude Code built-in agent (read-only)")
    candidate = _unresolved_target(root, name)
    if candidate.is_symlink():
        raise ReadOnlyAgentError(f"{name!r} is a plugin-provided subagent (read-only)")
    target = _resolve(root, name)
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return False
    if _is_read_only_file(target, raw):
        raise ReadOnlyAgentError(f"{name!r} is a plugin-provided subagent (read-only)")
    return fw.delete_path(root, f"{name}.md")


def delete_user_agent(claude_json: Path, name: str) -> bool:
    """Delete a user-scope subagent; return whether it existed."""
    return _delete_agent(user_agents_dir(claude_json), name)


def delete_project_agent(project_dir: Path, name: str) -> bool:
    """Delete a project-scope subagent; return whether it existed."""
    return _delete_agent(project_agents_dir(project_dir), name)
