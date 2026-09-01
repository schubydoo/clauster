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
:func:`_resolve`. **Listing follows the same ordering:** :func:`_list_agents` tests
``is_symlink()`` on the direntry **before ``is_file()``**, not merely before
``read_bytes()`` — both of those follow symlinks, so an ``is_file()`` test alone would
still *stat* the target. It emits a plugin-owned, non-editable entry with
``description: None`` without anything touching the link's target, so the
never-followed/never-read guarantee holds on **all four** paths: LIST, GET, PUT and
DELETE. Ordering it this way also keeps LIST and GET agreeing about existence: a
dangling or directory symlink fails ``is_file()``, and classifying first stops it
vanishing from the listing while :func:`_read_agent` still reports it present.

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
from collections.abc import Callable
from pathlib import Path
from typing import Any

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

# THE SAME OBJECT config_write_skills uses — an alias, not a copy, so the two parsers
# on this write tier cannot drift apart again (#1352). The pattern, its mechanics and
# its tolerance decisions are documented at the definition — change it in config_write.
_FRONTMATTER_RE = cw.FRONTMATTER_RE


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
    dir, so it is **never read** (following it would be an arbitrary-file read past the
    containment boundary). Surfaced instead as a content-less read-only doc, the same
    shape the built-in synthetic doc uses: shown, marked plugin-owned, never editable.
    :func:`_list_agents` withholds the target's ``description`` for the same reason, so
    this holds on the listing path too — not just this one.
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

    The fence and the YAML load are both shared with
    :func:`clauster.config_write_skills.parse_frontmatter` (see
    :data:`~clauster.config_write.FRONTMATTER_RE` and
    :func:`~clauster.config_write.load_frontmatter_yaml`) — the two guard the same
    code-executing tier and must not drift. One deliberate difference remains: an empty
    header (YAML ``None``) is an empty mapping here and a rejection there, so the two
    still differ on what they ACCEPT even though they now split identically.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise cw.InvalidCandidateError(
            "subagent content must start with a YAML frontmatter block ('---' ... '---')"
        )
    # `line_offset=1`: the header slice starts after the opening `---` fence, so a raw mark
    # names the line one above the fault in the FILE the operator is looking at.
    data = cw.load_frontmatter_yaml(match.group(1), what="frontmatter", line_offset=1)
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


def _validate_non_empty_string(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a string with non-whitespace content."""
    if not isinstance(value, str) or not value.strip():
        raise cw.InvalidCandidateError(f"{label} must be a non-empty string")


def _validate_string(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a string; an empty one is allowed."""
    if not isinstance(value, str):
        raise cw.InvalidCandidateError(f"{label} must be a string")


def _validate_bool(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a real boolean."""
    if not isinstance(value, bool):
        raise cw.InvalidCandidateError(f"{label} must be a boolean")


def _validate_string_list(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a list of non-empty strings (an empty list passes)."""
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise cw.InvalidCandidateError(f"{label} must be a list of non-empty strings")


def _validate_positive_int(value: Any, label: str) -> None:
    """Reject ``value`` unless it is an int > 0 — ``bool`` excluded, since it subclasses int."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise cw.InvalidCandidateError(f"{label} must be a positive integer")


def _validate_string_or_mapping(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a string or a mapping."""
    if not isinstance(value, str | dict):
        raise cw.InvalidCandidateError(f"{label} must be a string or an object")


def _validate_permission_mode(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a recognized, non-footgun-gated permission mode."""
    if not isinstance(value, str):
        raise cw.InvalidCandidateError(f"{label} must be a string")
    if value == BYPASS_MODE:
        # Fail closed: bypassPermissions stays behind the existing footgun gate,
        # never settable through a subagent's frontmatter either.
        raise cw.InvalidCandidateError(f"{label} cannot be 'bypassPermissions' (footgun-gated)")
    if value not in RECOGNIZED_MODES:
        raise cw.InvalidCandidateError(
            f"unknown frontmatter permissionMode {value!r} "
            f"(want one of {sorted(RECOGNIZED_MODES)})"
        )


def _validate_mcp_servers(value: Any, label: str) -> None:
    """Reject ``value`` unless it maps server names to objects — never connected to."""
    if not isinstance(value, dict) or not all(isinstance(v, dict) for v in value.values()):
        raise cw.InvalidCandidateError(f"{label} must be an object mapping server name to object")


def _validate_hooks_block(value: Any, label: str) -> None:
    """Delegate to the hooks structural validator, prefixing its message with ``label``."""
    try:
        validate_hooks(value)
    except cw.InvalidCandidateError as exc:
        raise cw.InvalidCandidateError(f"{label}: {exc}") from exc


def _validate_env(value: Any, label: str) -> None:
    """Reject ``value`` unless it maps non-empty names to scalars — never exported here."""
    if not isinstance(value, dict):
        raise cw.InvalidCandidateError(
            f"{label} must be an object mapping variable names to scalar values"
        )
    for var_name, var_value in value.items():
        if not isinstance(var_name, str) or not var_name.strip():
            raise cw.InvalidCandidateError(f"{label} keys must be non-empty strings")
        if not isinstance(var_value, str | int | float | bool):
            raise cw.InvalidCandidateError(
                f"{label} value for {var_name!r} must be a scalar (string, number, or boolean)"
            )


#: The signature every entry in :data:`_OPTIONAL_FRONTMATTER_RULES` satisfies:
#: ``(value, label) -> None``, raising :class:`~clauster.config_write.InvalidCandidateError`.
_FrontmatterRule = Callable[[Any, str], None]

#: Optional frontmatter keys → the structural check their value must pass, in the order
#: they are checked. Table-driven (#1155) so the recognized-key set reads as one list
#: rather than a branch per key. Every entry is a pure *shape* check: nothing here ever
#: resolves a tool name, spawns a hook command, or connects to an MCP server. An
#: UNRECOGNIZED key is absent from this table and passes through untouched — see
#: :func:`validate_frontmatter` for why.
_OPTIONAL_FRONTMATTER_RULES: tuple[tuple[str, _FrontmatterRule], ...] = (
    # Claude Code accepts either a comma-separated string or an array for these two —
    # this repo's own shipped subagents use the string form.
    ("tools", _validate_string_or_string_list),
    ("disallowedTools", _validate_string_or_string_list),
    ("skills", _validate_string_list),
    ("model", _validate_non_empty_string),
    ("permissionMode", _validate_permission_mode),
    ("mcpServers", _validate_mcp_servers),
    # A subagent's own hooks are exactly as RCE-sensitive as a project/user hooks block,
    # so the hooks validator is reused wholesale (including its plugin-owned-command
    # rejection) rather than duplicated. STRUCTURE ONLY — no command is ever executed.
    ("hooks", _validate_hooks_block),
    ("maxTurns", _validate_positive_int),
    ("initialPrompt", _validate_string),
    ("memory", _validate_string_or_mapping),
    ("effort", _validate_non_empty_string),
    ("background", _validate_bool),
    ("isolation", _validate_non_empty_string),
    ("color", _validate_non_empty_string),
    # Dropping the unknown-key allowlist made ``env`` reachable for the first time (it was
    # never in the old recognized-key set). Claude Code loads it as a name→value
    # environment map, so a non-mapping payload (``env: 42``) or a non-scalar value would
    # land on disk here and only fail later when Claude Code tries to load the subagent —
    # validate the shape at write time instead.
    ("env", _validate_env),
)


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
    when present, and no key is ever executed — this only stops rejecting keys the
    surface has no opinion about.

    ``name``/``description`` are checked inline because they are required and
    ``name`` additionally cross-checks ``expected_name``; every OPTIONAL key is
    checked by :data:`_OPTIONAL_FRONTMATTER_RULES`, which is the single place to
    read (or extend) the recognized-key set.

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

    for key, check in _OPTIONAL_FRONTMATTER_RULES:
        if key in candidate:
            check(candidate[key], f"frontmatter {key!r}")


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

    Like the read/write/delete paths, this one classifies a symlink **before anything stats
    the target** — ahead of ``is_file()``, not merely ahead of the read, since both it and
    ``read_bytes()`` follow symlinks. Any symlink (in-tree or out) is reported as
    plugin-owned and non-editable with ``description: None``, matching what GET returns for
    one. A dangling or directory symlink therefore still lists, rather than vanishing from
    LIST while GET reports it as present.
    """
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.suffix != ".md":
            continue
        name = entry.stem
        if not is_valid_agent_name(name):
            continue
        # Classify the symlink BEFORE ANYTHING TOUCHES THE TARGET — ahead of `is_file()`, not
        # just ahead of the read. `is_file()` and `read_bytes()` both FOLLOW symlinks, so an
        # `is_file()` test still stats the target: it leaks whether an out-of-tree path exists
        # and is a regular file, and it silently drops a dangling or directory symlink from the
        # listing while `_read_agent` still returns a read-only 200 for it — LIST and GET
        # disagreeing about whether an agent exists. `iterdir`/`.suffix`/`.stem` never stat, and
        # `is_symlink()` uses lstat, so nothing above this line follows the link.
        # `_is_read_only_file` treats any symlink as read-only anyway, so this only moves the
        # decision earlier; it withholds a description that should never have been read.
        if entry.is_symlink():
            out.append({"name": name, "source": "plugin", "editable": False, "description": None})
            continue
        if not entry.is_file():
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
    except OSError as exc:
        # OSError, not just FileNotFoundError: a plain DIRECTORY named `<name>.md` raises
        # IsADirectoryError, which is neither FileNotFoundError nor a ConfigWriteError — so
        # it escaped the route's `except ConfigWriteError` as a 500. LIST already skips such
        # an entry (it fails `is_file()`), so treating it as absent here is what makes LIST
        # and GET agree about existence, which this module's docstring now states as a
        # property. Any other read error (EACCES, ELOOP) fails closed the same way rather
        # than surfacing a traceback.
        raise AgentNotFoundError(f"no subagent named {name!r}") from exc
    read_only = _is_read_only_file(target, raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise cw.InvalidCandidateError(f"{name}.md is not valid UTF-8") from exc
    try:
        parsed, _body = parse_frontmatter(text)
        frontmatter = cw.redact_secrets(parsed)
        # An `mcpServers` block is a map keyed by user-chosen SERVER NAMES, not config keys,
        # so the name must not act as a secret hint — a server called `oauth-gw` would
        # otherwise have its own `type`/`url` masked. Same reason config_write_mcp redacts
        # per entry. Display-only here (PUT round-trips `content`, never `frontmatter`, so a
        # sentinel is never written back), but a transport shown as `********` is misleading.
        servers = parsed.get("mcpServers")
        if isinstance(servers, dict):
            frontmatter["mcpServers"] = {
                name_: cw.redact_secrets(entry) for name_, entry in servers.items()
            }
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

    The stale-hash guard IS atomic with the write: it runs as
    :func:`clauster.config_file_writer.write_file`'s ``verify=`` callback, inside that
    function's per-target lock, so the 409 comparison and the replace are one critical
    section. Two concurrent writes can no longer both compare against the same old bytes
    and have the second silently overwrite the first. (:mod:`clauster.claude_md`'s scoped
    write is the sibling that always did this.) ⚠️ The read-only refusal above still runs
    outside the lock, deliberately — it must precede content validation to keep the
    documented 403-before-422 gate order — so it stays a best-effort pre-check.
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

    def _verify_unchanged(current_bytes: bytes | None) -> None:
        """Reject the write when the on-disk bytes no longer match ``expected_hash``."""
        # Runs INSIDE write_file's per-target lock, so the 409 check and the replace are one
        # critical section. Read outside it (as this did), two concurrent PUTs could both
        # compare against the same old bytes and the second would silently overwrite the
        # first. Mirrors `claude_md`'s scoped write, the other read-modify-write here.
        if expected_hash is None:
            if current_bytes is not None:
                raise cw.StaleConfigWriteError(f"{name}.md already exists; a hash is required")
        elif cw.hash_bytes(current_bytes or b"") != expected_hash:
            raise cw.StaleConfigWriteError(f"{name}.md changed on disk since it was loaded")

    fw.write_file(root, f"{name}.md", content, verify=_verify_unchanged)


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

    The plugin-owned refusal is re-checked as ``delete_path``'s ``verify=`` callback,
    inside its per-target lock, so the decision and the unlink are one critical section.
    The read before it decides whether the file MAY be deleted, which is the same
    read-then-act shape as the write path's stale-hash guard: without the re-check, a file
    that became plugin-owned between the two would be removed on a stale decision.
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

    def _verify_still_deletable(current: bytes | None) -> None:
        """Re-check the plugin-owned refusal against the bytes present under the lock."""
        # The read above decides whether this file MAY be deleted, so it has the same
        # read-then-act shape as the write path's stale-hash guard: without this, a file
        # that became plugin-owned between the read and the unlink would be deleted on the
        # strength of a stale decision. Re-running the check inside delete_path's lock
        # makes the refusal and the removal one critical section.
        if current is not None and _is_read_only_file(target, current):
            raise ReadOnlyAgentError(f"{name!r} is a plugin-provided subagent (read-only)")

    return fw.delete_path(root, f"{name}.md", verify=_verify_still_deletable)


def delete_user_agent(claude_json: Path, name: str) -> bool:
    """Delete a user-scope subagent; return whether it existed."""
    return _delete_agent(user_agents_dir(claude_json), name)


def delete_project_agent(project_dir: Path, name: str) -> bool:
    """Delete a project-scope subagent; return whether it existed."""
    return _delete_agent(project_agents_dir(project_dir), name)
