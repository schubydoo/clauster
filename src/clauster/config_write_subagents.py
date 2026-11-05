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

**Plugin / built-in agents are read-only.** Claude Code ships built-in subagents
(:data:`BUILTIN_AGENT_NAMES`) that are compiled in, not
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

**Redaction (TASK-427).** The freeform *body* (the system prompt) is never redacted or
rewritten — like CLAUDE.md, it is operator-authored content, not credential storage, and
masking it would corrupt the operator's own prose. The *frontmatter* can carry secrets (an
inline ``mcpServers`` server's ``env``, ``headers`` or credentialed URL, a ``hooks``
header), so both the parsed ``frontmatter`` display field and the editor's ``content``
show its secret values as ``********``, masked by key AND by value shape with the masking
of the tab that owns each field (:func:`frontmatter_view`). ``content`` has them written
in place of the values, every other byte as stored (:func:`_mask_agent_text`). A save that
sends a mask back keeps the stored value only where the mask can be bound to it with that
tab's own rules, and is refused (→ 422) otherwise, never written as the literal mask
(:func:`_keep_masked_values`): a masked credential is not kept when its server's url text
changed (exact text, never parsed), and a masked stdio ``env`` value is not kept when the
server's ``command`` or ``args`` changed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import yaml

from . import config_file_writer as fw
from . import config_write as cw
from .config_write_hooks import (
    _MAX_BLOCK_DEPTH,
    _depth_at_most,
    _has_sentinel,
    _merge_hooks,
    _redact_hooks,
    validate_hooks,
)
from .config_write_mcp import (
    _convert_legacy_entry,
    _merge_kept,
    _redact_entry,
    _validate_server_fields,
    contains_sentinel,
)
from .config_write_permissions import BYPASS_MODE, RECOGNIZED_MODES
from .config_write_settings import _orphan_sentinel_path

#: The directory name holding subagent files under ``.claude/`` at either scope.
AGENTS_DIRNAME = "agents"

#: Size cap for a single subagent file (frontmatter + body), matching the CLAUDE.md cap.
#: ⚠️ COUPLED to :data:`clauster.config_write.MAX_EXPANDED_CHARS` — raising this without
#: raising that would make the alias-expansion guard reject an alias-FREE header. Read the
#: comment there before changing this number; ``test_byte_caps_stay_under_the_expansion_cap``
#: is the gate.
MAX_BYTES = 64 * 1024

#: Claude Code's own subagent-name shape: lowercase letters, digits, and hyphens; must
#: start with a letter (no leading hyphen/digit) and never contain a path separator,
#: ``.``, or ``..`` — the regex itself is the containment defense-in-depth, on top of
#: :func:`clauster.config_file_writer.resolve_contained_path`.
_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

#: Longest accepted subagent name. Claude Code caps a ``name`` at 256 characters, but the
#: name is also this surface's filename, and 255 bytes is the common filename limit
#: (``NAME_MAX``): a longer name raised a bare ``OSError`` (``ENAMETOOLONG``) on read and
#: write, a 500 instead of a 400/422. The longest name this surface puts on disk is the
#: atomic writer's temp file, ``<name>.md.<8 random chars>.tmp`` (``tempfile.mkstemp``),
#: 16 characters over the name, so 239 + 16 is 255. The name is ASCII (see
#: :data:`_NAME_RE`), so characters and bytes agree. ⚠️ This is the POSIX bound. On Windows
#: without long paths, the 260-character limit on the WHOLE path can bind first.
MAX_NAME_CHARS = 239

#: Claude Code's built-in subagents — compiled into the CLI, never files on disk.
#: Matched case-insensitively against a candidate name (``Explore``/``Plan`` are
#: capitalized in Claude Code's own vocabulary; the rest are lowercase). The list is the
#: "Built-in subagents" section of https://code.claude.com/docs/en/sub-agents, including
#: its "Other" helper agents, and each one is an agent type in the installed CLI (2.1.318).
BUILTIN_AGENT_NAMES = frozenset(
    {"general-purpose", "explore", "plan", "claude", "statusline-setup", "claude-code-guide"}
)

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
    return (
        isinstance(name, str)
        and len(name) <= MAX_NAME_CHARS
        and _NAME_RE.fullmatch(name) is not None
    )


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
    data = cw.load_frontmatter_yaml(
        cw.frontmatter_header(text, match), what="frontmatter", line_offset=1
    )
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise cw.InvalidCandidateError("frontmatter must be a YAML mapping (an object)")
    return data, text[match.end() :]


def _cli_tool_entries(items: list[str]) -> list[str]:
    """Split tool-list strings into entries the way the 2.1.318 CLI does (its ``fu``).

    A comma or a space ends an entry unless it is inside parentheses, so
    ``Agent(worker, researcher), Read`` is two entries. Splitting here only decides
    whether the list is empty or holds ``*``; no entry is resolved against a tool registry.
    """
    entries: list[str] = []
    for item in items:
        current, in_parens = "", False
        for char in item:
            if char == "(":
                in_parens = True
                current += char
            elif char == ")":
                in_parens = False
                current += char
            elif char == ",":
                if in_parens:
                    current += char
                else:
                    if current.strip(_JS_WHITESPACE):
                        entries.append(current.strip(_JS_WHITESPACE))
                    current = ""
            elif char == " ":
                if in_parens:
                    current += char
                elif current.strip(_JS_WHITESPACE):
                    entries.append(current.strip(_JS_WHITESPACE))
                    current = ""
            elif current or char.strip(_JS_WHITESPACE):
                current += char
        if current.strip(_JS_WHITESPACE):
            entries.append(current.strip(_JS_WHITESPACE))
    return entries


def _validate_string_or_string_list(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a string or list of strings naming at least one tool.

    Structural only: the entries are never resolved against a real tool registry — that
    grant is enforced later, by Claude Code itself, when the subagent actually runs. A value
    that splits to no entry at all (``""``, ``","``, ``[]``) is refused, because Claude Code
    then refuses to launch the subagent with zero tools.
    """
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list) and all(
        isinstance(v, str) and v.strip(_JS_WHITESPACE) for v in value
    ):
        items = value
    elif isinstance(value, list):
        raise cw.InvalidCandidateError(f"{label} must be a list of non-empty strings")
    else:
        raise cw.InvalidCandidateError(f"{label} must be a string or a list of strings")
    if not _cli_tool_entries(items):
        raise cw.InvalidCandidateError(f"{label} must name at least one tool")


def _validate_non_empty_string(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a string with non-whitespace content."""
    if not isinstance(value, str) or not value.strip():
        raise cw.InvalidCandidateError(f"{label} must be a non-empty string")


def _validate_string(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a string; an empty one is allowed."""
    if not isinstance(value, str):
        raise cw.InvalidCandidateError(f"{label} must be a string")


def _validate_bool_or_bool_string(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a boolean or the string ``"true"``/``"false"``.

    The 2.1.318 agent-file loader reads both spellings for ``background`` (and logs any
    other value as invalid) and for ``omitClaudeMd``, where ``false`` in either spelling is
    the same as leaving the field out.
    """
    if not isinstance(value, bool) and value not in ("true", "false"):
        raise cw.InvalidCandidateError(f"{label} must be true or false")


def _optional_string_list_items(value: Any, label: str) -> list[str]:
    """Return ``value`` as the list of strings the CLI splits, or reject it.

    Accepted: ``null`` and any string (the CLI splits a string on commas; an empty one is
    an empty list), or a list of non-empty strings (an empty list passes). A list entry that
    is not a non-empty string is refused, because the CLI silently drops it.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(
        isinstance(v, str) and v.strip(_JS_WHITESPACE) for v in value
    ):
        return value
    raise cw.InvalidCandidateError(f"{label} must be a string or a list of non-empty strings")


def _validate_optional_string_list(value: Any, label: str) -> None:
    """Reject ``value`` unless the CLI reads it as a (possibly empty) list of names."""
    _optional_string_list_items(value, label)


def _validate_disallowed_tools(value: Any, label: str) -> None:
    """Like :func:`_validate_optional_string_list`, but refuse a ``*`` entry.

    The CLI turns a ``*`` anywhere in the denylist into NO denylist, so an operator who
    means "deny everything" would get "deny nothing". (``tools: "*"`` is different: it
    means every tool, which is what it says.)
    """
    if "*" in _cli_tool_entries(_optional_string_list_items(value, label)):
        raise cw.InvalidCandidateError(
            f"{label} cannot contain '*': Claude Code reads it as no denylist at all"
        )


# --- integer parsing the way the 2.1.318 agent-file loader does it ------------------------
# `maxTurns` and `autoCompactWindow` go through one CLI helper (`bbr`): a number is used as
# is, anything else is stringified, trimmed, and then read as exponent notation, as a
# grouped integer (`200,000`, `200_000`), or with JavaScript's `parseInt`. `effort` uses a
# second helper (`dq`) with its own rules. These mirror both, for the scalar YAML types only:
# a list or mapping reaches those helpers solely through JavaScript's string coercion of an
# object, which this surface does not treat as a value the CLI reads.

#: The CLI's exponent-notation and grouped-integer patterns (`R` and `E` in its source).
#: JavaScript regular expressions and ``parseInt`` take ASCII digits only, where Python's
#: ``\d`` matches any Unicode digit (``٥``, ``５``), hence ``[0-9]`` throughout.
_CLI_EXPONENT_RE = re.compile(r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)[eE][+-]?[0-9]+$")
_CLI_GROUPED_INT_RE = re.compile(r"^[+-]?[0-9]{1,3}([_,\u00a0\u202f ])[0-9]{3}(?:\1[0-9]{3})*$")
_CLI_GROUP_SEPARATORS_RE = re.compile(r"[_,\u00a0\u202f ]")
#: The characters JavaScript's ``trim`` and ``parseInt`` skip (WhiteSpace and
#: LineTerminator). Python's ``str.strip``/``\s`` differ: they also skip ``\x1c``-``\x1f``
#: and ``\x85``, and they keep the BOM, ``\ufeff``.
_JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
#: JavaScript ``parseInt(s, 10)``: optional leading whitespace, an optional sign, then the
#: longest run of digits; anything after the digits is ignored.
_JS_PARSE_INT_RE = re.compile(f"[{_JS_WHITESPACE}]*([+-]?[0-9]+)")


def _as_js_number(value: int) -> int | None:
    """Return ``value``, or None when JavaScript would hold it as ``Infinity``.

    A JavaScript number is a double, so an integer past the double range becomes
    ``Infinity``, which ``Number.isInteger`` rejects. (Precision loss below that range keeps
    a whole number whole, so it does not change the answer.)
    """
    try:
        float(value)
    except OverflowError:
        return None
    return value


def _js_parse_int(text: str) -> int | None:
    """Return what JavaScript ``parseInt(text, 10)`` returns, or None for ``NaN``/``Infinity``."""
    match = _JS_PARSE_INT_RE.match(text)
    # Bounded: int() on 5000 digits raises ValueError (Greptile on PR 1810).
    return cw.js_integer(match.group(1)) if match else None


def _integral(value: float) -> int | None:
    """Return ``value`` as an int when it is a finite whole number (``Number.isInteger``)."""
    return int(value) if value == value and abs(value) != float("inf") and value % 1 == 0 else None


def _cli_int_from_text(raw: str) -> int | None:
    """Return the CLI's ``Bf`` reading of a string: exponent, grouped, then ``parseInt``."""
    text = raw.strip(_JS_WHITESPACE)
    if len(text) <= 32:
        if _CLI_EXPONENT_RE.match(text):
            return _integral(float(text))
        if _CLI_GROUPED_INT_RE.match(text):
            return _as_js_number(int(_CLI_GROUP_SEPARATORS_RE.sub("", text)))
    return _js_parse_int(text)


def _cli_positive_int(value: Any) -> int | None:
    """Return the positive int the CLI reads from ``value`` (its ``bbr``), or None."""
    if value is None or isinstance(value, bool):
        # JavaScript stringifies a boolean to "true"/"false", which parses to NaN.
        return None
    if isinstance(value, int):
        number: int | None = _as_js_number(value)
    elif isinstance(value, float):
        number = _integral(value)
    elif isinstance(value, str):
        number = _cli_int_from_text(value)
    else:
        return None
    return number if number is not None and number > 0 else None


def _validate_positive_int(value: Any, label: str) -> None:
    """Reject ``value`` unless the CLI reads a positive integer from it (``bool`` excluded)."""
    if _cli_positive_int(value) is None:
        raise cw.InvalidCandidateError(f"{label} must be a positive integer")


#: The CLI's effort levels and its one effort alias (``med``).
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_EFFORT_ALIASES = {"med": "medium"}


def _cli_effort(value: Any) -> str | int | None:
    """Return the effort the CLI reads from ``value`` (its ``dq``), or None when it ignores it.

    A number must be a whole number, and any whole number counts. Anything else is
    stringified and lowercased (not trimmed), mapped through the alias table, and matched
    against the level names. Failing that, ``parseInt`` decides, so ``"3 tokens"`` is ``3``.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return _as_js_number(value)
    if isinstance(value, float):
        whole = _integral(value)
        if whole is not None:
            return whole
        text = repr(value)  # what JavaScript's String() gives for a finite fraction
    elif isinstance(value, str):
        text = value
    else:
        return None
    lowered = text.lower()
    level = _EFFORT_ALIASES.get(lowered, lowered)
    if level in _EFFORT_LEVELS:
        return level
    return _js_parse_int(lowered)


def _validate_effort(value: Any, label: str) -> None:
    """Reject ``value`` unless the CLI reads an effort level or an integer from it."""
    if _cli_effort(value) is None:
        raise cw.InvalidCandidateError(
            f"{label} must be one of {list(_EFFORT_LEVELS)} (any case, or 'med') or an integer"
        )


#: The signature every entry in :data:`_OPTIONAL_FRONTMATTER_RULES` satisfies:
#: ``(value, label) -> None``, raising :class:`~clauster.config_write.InvalidCandidateError`.
_FrontmatterRule = Callable[[Any, str], None]


def _one_of(*allowed: str) -> _FrontmatterRule:
    """Return a rule that rejects any value other than one of the ``allowed`` strings.

    Used for the frontmatter fields the CLI matches exactly against a closed list. Claude
    Code silently ignores a value outside the list, so the write would land and do nothing;
    refusing it here tells the operator at save time instead.
    """

    def _check(value: Any, label: str) -> None:
        """Reject ``value`` unless it is exactly one of the closed list."""
        if not isinstance(value, str) or value not in allowed:
            raise cw.InvalidCandidateError(f"{label} must be one of {list(allowed)}")

    return _check


#: Claude Code's documented ``autoCompactWindow`` range for a subagent, in tokens.
_AUTO_COMPACT_WINDOW_MIN = 100_000
_AUTO_COMPACT_WINDOW_MAX = 1_000_000


def _validate_auto_compact_window(value: Any, label: str) -> None:
    """Reject ``value`` unless the CLI reads a token count inside the documented range."""
    # The CLI reads this field with the same integer parse as `maxTurns`, so `"200000"`,
    # `2e5` and `200,000` all count. `200k` is refused: the CLI reads it as 200, which is
    # out of range, and ignores the field.
    number = _cli_positive_int(value)
    if number is None or not _AUTO_COMPACT_WINDOW_MIN <= number <= _AUTO_COMPACT_WINDOW_MAX:
        raise cw.InvalidCandidateError(
            f"{label} must be an integer from {_AUTO_COMPACT_WINDOW_MIN} "
            f"to {_AUTO_COMPACT_WINDOW_MAX}"
        )


#: The documented values of ``experimental.cacheTtl``.
_CACHE_TTL_VALUES = ("5m", "1h")


def _validate_experimental(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a mapping whose ``cacheTtl``, if set, is documented.

    Only ``cacheTtl`` is documented inside the map. Any other key passes through
    untouched, the same forward-compatible policy :func:`validate_frontmatter` applies to
    unknown top-level keys (Claude Code ignores an unknown ``experimental`` key too).
    """
    if not isinstance(value, dict):
        raise cw.InvalidCandidateError(f"{label} must be an object")
    for key, ttl in value.items():
        # Claude Code finds the key after it drops `-`/`_` and lowercases it (so `cachettl`
        # and `cache_ttl` are `cacheTtl` too), then ignores a bad value. Normalize the same
        # way, or a misspelled key with a bad value would slip past as an unknown key.
        if not isinstance(key, str) or key.replace("-", "").replace("_", "").lower() != (
            "cachettl"
        ):
            continue
        if ttl not in _CACHE_TTL_VALUES:
            raise cw.InvalidCandidateError(
                f"{label} {key!r} must be one of {list(_CACHE_TTL_VALUES)}"
            )


#: ``manual`` is the documented frontmatter alias of ``default`` (Claude Code maps it
#: before the lookup). It is local to this surface: the settings ``defaultMode`` vocabulary
#: in :mod:`clauster.config_write_permissions` is a different field with its own audit.
_MANUAL_MODE_ALIAS = "manual"


def _validate_permission_mode(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a recognized, non-footgun-gated permission mode."""
    if not isinstance(value, str):
        raise cw.InvalidCandidateError(f"{label} must be a string")
    if value == BYPASS_MODE:
        # Fail closed: bypassPermissions stays behind the existing footgun gate,
        # never settable through a subagent's frontmatter either.
        raise cw.InvalidCandidateError(f"{label} cannot be 'bypassPermissions' (footgun-gated)")
    if value not in RECOGNIZED_MODES and value != _MANUAL_MODE_ALIAS:
        raise cw.InvalidCandidateError(
            f"unknown frontmatter permissionMode {value!r} "
            f"(want one of {sorted(RECOGNIZED_MODES | {_MANUAL_MODE_ALIAS})})"
        )


#: Inline server names the 2.1.318 agent-file loader skips with "Skipping reserved MCP
#: server name" (probed one name per agent file, TASK-479): the built-in servers the MCP docs
#: list as reserved, the ``Object.prototype`` accessor names, and the Slack sign-in server.
#: ``__proto__`` is refused too, because the loader reads that item as having no key and
#: skips it. ⚠️ This is NOT the ``.mcp.json`` name rule of :mod:`clauster.config_write_mcp`:
#: the agent-file loader applies no charset to an inline name, and it connects a dotted name,
#: a name with a space or a leading hyphen, and ``constructor`` or ``prototype``. An inline
#: name never reaches a ``claude mcp`` argv (this surface writes the file directly), so the
#: MCP tab's arg-injection reason for its charset does not apply here either.
_FRONTMATTER_SKIPPED_SERVER_NAMES = frozenset(
    {
        "workspace",
        "claude-in-chrome",
        "computer-use",
        "Claude Preview",
        "Claude Browser",
        "Slack sign-in (Claude Code tag)",
        "__proto__",
        "__defineGetter__",
        "__defineSetter__",
        "__lookupGetter__",
        "__lookupSetter__",
    }
)


def _validate_mcp_servers(value: Any, label: str) -> None:
    """Reject ``value`` unless it is the documented LIST of server entries — never connected.

    Each entry is either a non-empty string naming an already-configured server, or a
    mapping of ONE server name to an inline definition. Claude Code reads only the list
    form: it ignores an ``mcpServers`` mapping entirely (verified in the 2.1.318 agent-file
    loader), so a mapping is refused here rather than written as a silent no-op. The loader
    also skips an item with more than one server, an inline name in
    :data:`_FRONTMATTER_SKIPPED_SERVER_NAMES`, and a second inline server of the same name
    (only one of the two connects), so each of those is refused.

    The loader reads an inline definition with the same entry schema as ``.mcp.json``, so
    the definition goes through the MCP tab's field validator
    (:func:`clauster.config_write_mcp._validate_server_fields`): the same per-transport
    fields, and the same refusals, ``transport`` and a remote ``env`` included. A stored
    file that has those two loads converted (:func:`_convert_inline_servers`). Nothing is
    resolved, spawned or connected to here.
    """
    if isinstance(value, dict):
        raise cw.InvalidCandidateError(
            f"{label} must be a list, not a mapping: Claude Code reads only the list form "
            "and ignores a mapping. Write each server as a list item, '- name' for a server "
            "you configured already or '- name:' followed by its definition"
        )
    if not isinstance(value, list):
        raise cw.InvalidCandidateError(
            f"{label} must be a list of server names or {{name: definition}} objects"
        )
    inline_names: set[str] = set()
    for entry in value:
        if isinstance(entry, str):
            if not entry.strip():
                raise cw.InvalidCandidateError(f"{label} server names must be non-empty")
            continue
        if not isinstance(entry, dict) or not entry:
            raise cw.InvalidCandidateError(
                f"{label} entries must be a server name or a {{name: definition}} object"
            )
        if len(entry) != 1:
            raise cw.InvalidCandidateError(
                f"{label} item defines {len(entry)} servers; Claude Code skips an item with "
                "more than one. Write each server as its own '- name:' item"
            )
        ((server_name, definition),) = entry.items()
        if not isinstance(server_name, str) or not server_name.strip():
            raise cw.InvalidCandidateError(f"{label} server names must be non-empty strings")
        if server_name in _FRONTMATTER_SKIPPED_SERVER_NAMES:
            raise cw.InvalidCandidateError(
                f"{label} inline server name {server_name!r} is reserved by Claude Code, "
                "which skips it. Rename the server"
            )
        if server_name in inline_names:
            raise cw.InvalidCandidateError(
                f"{label} defines inline server {server_name!r} twice; Claude Code connects "
                "only one of them. Remove or rename one"
            )
        inline_names.add(server_name)
        try:
            _validate_server_fields(server_name, definition)
        except cw.InvalidCandidateError as exc:
            raise cw.InvalidCandidateError(
                f"{label} inline server {server_name!r}: {exc}.{_by_hand_hint(definition)}"
            ) from exc


def _by_hand_hint(definition: Any) -> str:
    """Return the manual fix for a field the convert-on-load handles, or ``""``.

    The MCP message promises a convert-on-load. Here that is a textual rewrite, which
    declines some shapes (a flow-style definition, a merge key), so the message also says
    what to change when the reload did not convert it. It names every converted field, so
    one manual fix clears them all, and each fix matches what the conversion would do: a
    ``transport`` with no ``type`` becomes ``type``, because removing it would only lead to
    the next refusal.
    """
    fixes = [
        "rename 'transport' to 'type'"
        if note["field"] == "transport" and "type" not in definition
        else f"remove {note['field']!r}"
        for note in _convert_legacy_entry(definition)[1]
    ]
    if not fixes:
        return ""
    fix = " and ".join(fixes)
    return f" If the editor did not convert it when it loaded the file, {fix} by hand."


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


#: Optional frontmatter keys → the structural check their value must pass, in the order
#: they are checked. Table-driven (#1155) so the recognized-key set reads as one list
#: rather than a branch per key. Every entry is a pure *shape* check: nothing here ever
#: resolves a tool name, spawns a hook command, or connects to an MCP server. An
#: UNRECOGNIZED key is absent from this table and passes through untouched — see
#: :func:`validate_frontmatter` for why.
#:
#: The key set follows the "Frontmatter reference" table of
#: https://code.claude.com/docs/en/sub-agents. The accepted VALUES are everything the
#: installed CLI's agent-file loader (2.1.318) reads, which is a superset of the documented
#: lists in places (an integer or upper-case ``effort``, ``isolation: remote``, a string
#: ``maxTurns``) — the maintainer's call on TASK-449.5. A value the CLI rejects or silently
#: ignores is refused. ⚠️ Every check sees the value as PyYAML (YAML 1.1) loaded it, and the
#: CLI loads YAML 1.2, so a few spellings read differently: a plain ``yes`` is a boolean
#: here and a string there, ``010`` is 8 here and 10 there.
#: :func:`_refuse_yaml11_only_spellings` refuses those for the boolean and number fields.
#: Other differences are stricter here, for example a plain ``2024-01-01`` is a date to
#: PyYAML, so it is refused as a ``maxTurns`` that the CLI would read as 2024. A top-level
#: field this module checks only as "a string" (``model``) is not compared spelling by
#: spelling; the string values of an inline MCP definition are.
_OPTIONAL_FRONTMATTER_RULES: tuple[tuple[str, _FrontmatterRule], ...] = (
    # Claude Code accepts either a comma-separated string or an array for these —
    # this repo's own shipped subagents use the string form. `tools` must name at least
    # one tool: an empty grant makes the CLI refuse to launch the subagent.
    ("tools", _validate_string_or_string_list),
    # An empty or null denylist / preload list is read as "none", so it is accepted.
    ("disallowedTools", _validate_disallowed_tools),
    ("skills", _validate_optional_string_list),
    # An alias (`sonnet`, `opus`, `haiku`, `fable`), `inherit`, or a full model id. The
    # id set is open-ended, so this stays a non-empty-string check.
    ("model", _validate_non_empty_string),
    ("permissionMode", _validate_permission_mode),
    ("mcpServers", _validate_mcp_servers),
    # A subagent's own hooks are exactly as RCE-sensitive as a project/user hooks block,
    # so the hooks validator is reused wholesale (including its plugin-owned-command
    # rejection) rather than duplicated. STRUCTURE ONLY — no command is ever executed.
    ("hooks", _validate_hooks_block),
    ("maxTurns", _validate_positive_int),
    ("autoCompactWindow", _validate_auto_compact_window),
    ("initialPrompt", _validate_string),
    ("memory", _one_of("user", "project", "local")),
    ("effort", _validate_effort),
    ("background", _validate_bool_or_bool_string),
    ("omitClaudeMd", _validate_bool_or_bool_string),
    # `remote` is read by the CLI for a user/project agent though the docs list only
    # `worktree` (a plugin agent reads only `worktree`, but plugin files are read-only here).
    ("isolation", _one_of("worktree", "remote")),
    ("color", _one_of("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan")),
    ("experimental", _validate_experimental),
    # Dropping the unknown-key allowlist made ``env`` reachable for the first time (it was
    # never in the old recognized-key set). ⚠️ It is NOT a documented subagent field, and
    # the 2.1.318 agent-file loader never reads it, so Claude Code ignores it. The shape
    # check is kept so an existing file's `env` still gets the scalar-map guard it had.
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
            f"(no leading hyphen, no path separators, at most {MAX_NAME_CHARS} characters)"
        )
    if expected_name is not None and name != expected_name:
        raise cw.InvalidCandidateError(
            f"frontmatter 'name' ({name!r}) must match the target agent name ({expected_name!r})"
        )

    description = candidate["description"]
    # Non-empty, not non-blank: the CLI skips a file whose description is missing, empty or
    # not a string, but it loads one whose description is only whitespace.
    if not isinstance(description, str) or not description:
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
    header = _FRONTMATTER_RE.match(candidate)
    if header is not None:  # pragma: no branch — always, once parse_frontmatter accepted it
        with cw.refuse_check_failures("frontmatter"):  # it walks the composed tree itself
            _refuse_yaml11_only_spellings(cw.frontmatter_header(candidate, header))


#: The fields whose loaded value this module checks as a boolean or a number, and the
#: YAML 1.2 core spellings those loaded values must come from.
_BOOLEAN_FIELDS = frozenset({"background", "omitClaudeMd"})
_NUMBER_FIELDS = frozenset({"maxTurns", "autoCompactWindow", "effort"})
_YAML12_BOOLEANS = frozenset({"true", "True", "TRUE", "false", "False", "FALSE"})
#: Decimal without a leading zero, or hex, which both versions read alike. YAML 1.1 also
#: reads ``010`` (octal 8), ``0b101``, ``1_000`` and ``1:30`` (sexagesimal) as integers;
#: YAML 1.2 reads ``010`` as 10 and the others as strings.
_YAML12_INT_RE = re.compile(r"[-+]?(?:0|[1-9][0-9]*)|0x[0-9a-fA-F]+")
_YAML12_FLOAT_RE = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?")
_BOOL_TAG = "tag:yaml.org,2002:bool"
_INT_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
_MERGE_TAG = "tag:yaml.org,2002:merge"


def _check_spelling(label: str, node: yaml.Node, *, boolean: bool, number: bool) -> None:
    """Refuse a checked field whose value PyYAML and the CLI's YAML 1.2 read differently."""
    if not isinstance(node, yaml.ScalarNode):
        return
    if boolean and node.tag == _BOOL_TAG and node.value not in _YAML12_BOOLEANS:
        raise cw.InvalidCandidateError(
            f"{label}: Claude Code reads {node.value!r} as a string, not a "
            "boolean, and ignores it. Write true or false"
        )
    if number and (
        (node.tag == _INT_TAG and not _YAML12_INT_RE.fullmatch(node.value))
        or (node.tag == _FLOAT_TAG and not _YAML12_FLOAT_RE.fullmatch(node.value))
    ):
        raise cw.InvalidCandidateError(
            f"{label}: {node.value!r} is a number to YAML 1.1 but not, or not "
            "the same number, to the YAML 1.2 that Claude Code reads. Write a plain decimal"
        )


def _check_mapping(
    node: yaml.MappingNode, seen: set[int], *, visit: Callable[[str, yaml.Node], None]
) -> None:
    """Call ``visit`` on each key of ``node``, following ``<<`` merge keys into their sources.

    ``compose`` leaves merges unflattened, so a ``<<: {background: yes}`` would otherwise
    never be looked at. A source is checked even where an explicit key overrides it, which
    only ever refuses more. ``seen`` skips a source merged twice; an alias cycle never gets
    here, because the shared seam (:func:`~clauster.config_write.load_frontmatter_yaml`)
    refuses an alias to an enclosing node first.

    Iterative: a chain of ``<<`` merges declared in document order loads without trouble
    (PyYAML flattens each mapping as it reaches it) and passes the merge-size guard (each
    link is one pair), so a recursive walk here met its full length, one frame per link, and
    a 29 KB chain of about 1200 links raised ``RecursionError``. The sources of a mapping are
    still walked after its own keys, in their written order.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        sources: list[yaml.MappingNode] = []
        for key_node, value_node in current.value:
            if key_node.tag == _MERGE_TAG:
                merged = (
                    value_node.value if isinstance(value_node, yaml.SequenceNode) else [value_node]
                )
                # Always mappings: PyYAML's load already refused a merge of anything else.
                sources.extend(s for s in merged if isinstance(s, yaml.MappingNode))
            # A non-scalar key cannot be hashed into a dict, so the load already refused it.
            elif isinstance(key_node, yaml.ScalarNode):  # pragma: no branch
                visit(key_node.value, value_node)
        stack.extend(reversed(sources))


def _refuse_yaml11_only_spellings(header: str) -> None:
    """Refuse header text that PyYAML (YAML 1.1) and the CLI (YAML 1.2) read differently.

    Every check in :func:`validate_frontmatter` sees the value as PyYAML loaded it. This
    looks at how the checked values are SPELLED, so a value is not accepted on a reading
    the CLI does not share:

    * a ``background``/``omitClaudeMd`` boolean spelled ``yes``/``no``/``on``/``off`` (a
      string to YAML 1.2, which the CLI logs or ignores), plain, quoted with an explicit
      ``!!bool``, or brought in through a ``<<`` merge;
    * a ``maxTurns``/``autoCompactWindow``/``effort`` number spelled the YAML 1.1 way
      (``010``, ``0b101``, ``1_000``, ``1:30``), which YAML 1.2 reads as another number or
      as a string;

    These are the TOP-LEVEL fields. The shared seam
    (:func:`~clauster.config_write.load_frontmatter_yaml`) already refused a NEL, LINE
    SEPARATOR or PARAGRAPH SEPARATOR, an alias to an enclosing node, and every NESTED value
    that the two read differently before this runs (TASK-449.6). That covers the boolean
    and number fields of an inline ``mcpServers`` definition and its ``oauth`` object
    (``alwaysLoad: yes``, ``timeout: 1_000``), and a plain string value or server-name
    reference that YAML 1.2 reads as a number (``env: {A: 1e3}``, ``args: [0o17]``,
    ``- -0o17``), which the CLI's entry schema refuses, so it would skip the whole server.
    The inline walk TASK-479 added here did the same, server by server; the seam's walker
    replaces it, so the composed tree is walked once, memoized and iteratively. The two
    differ only where both YAML readings agree: the seam accepts a leading-zero value below
    8 (``timeout: 07``, 7 to both) and an explicit ``!!str`` on a spelling PyYAML would
    read as a number (``args: [!!str 10]``), which that walk refused.

    ``header`` already loaded as a mapping, so composing the text the CLI parses (the header,
    or its retry text: :func:`~clauster.config_write.cli_frontmatter_text`) yields a mapping
    node and does not fail.
    """
    root = yaml.compose(cw.cli_frontmatter_text(header), Loader=yaml.SafeLoader)
    _check_mapping(
        root,
        set(),
        visit=lambda key, node: _check_spelling(
            f"frontmatter {key!r}",
            node,
            boolean=key in _BOOLEAN_FIELDS,
            number=key in _NUMBER_FIELDS,
        ),
    )


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
                # Masked as the editor masks it (a `${VAR}` or a credential URL in it).
                description = cw.redact_secrets(value, "description")
        except (UnicodeDecodeError, cw.InvalidCandidateError):
            description = None
        out.append(
            {
                "name": name,
                "source": "plugin" if read_only else source,
                # A file named like a built-in overrides it in Claude Code, but PUT and DELETE
                # refuse a built-in name, so LIST must not offer an Edit it cannot honor.
                "editable": not read_only and not is_builtin_agent(name),
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


def _is_pair_list(value: Any) -> bool:
    """Whether ``value`` is what ``safe_load`` builds for an ``!!omap``/``!!pairs`` block.

    Both tags construct a list of two-element **tuples**, and nothing else a
    ``SafeConstructor`` builds produces a tuple — so this identifies the ordered-mapping
    spelling of a block that is otherwise written as a plain YAML map.

    False for an EMPTY list, deliberately: ``all`` over nothing is true, and an empty block
    has no entries to carve out, so answering yes would have every caller re-guard for it.
    """
    return bool(value) and all(isinstance(entry, tuple) and len(entry) == 2 for entry in value)


#: Shown in the editor when :func:`_mcp_servers_as_list` converted a file on load.
MCP_SERVERS_CONVERSION_NOTE = (
    "mcpServers was converted from the old mapping form to the list form, because Claude "
    "Code reads only the list form and ignores a mapping. Save to write the list form."
)

#: A top-level ``mcpServers:`` key line, at column 0 of the frontmatter (any value).
_MCP_SERVERS_KEY_LINE_RE = re.compile(r"^mcpServers[ \t]*:")
#: The same line with NO value on it (an optional comment only): the block form, whose
#: entries follow on indented lines. A flow mapping on the key line is not rewritten.
_MCP_SERVERS_BLOCK_KEY_RE = re.compile(r"^mcpServers[ \t]*:[ \t]*(?:#.*)?$")


def _indent_as_list_items(block: list[str]) -> list[str] | None:
    """Re-indent the lines of a block mapping so each entry becomes a list item.

    An entry line, at the block's own indent ``n``, gains ``- `` at column ``n``; every
    deeper line, and any comment at or past ``n``, gains two spaces, so it stays under its
    entry. No scalar is touched, so every value keeps its exact spelling and quoting and
    reads the same in YAML 1.1 and 1.2 (the caller refuses a NEL/LS/PS character, the one
    case where the two disagree on where a line ends). Returns None for a shape this cannot shift
    safely: a tab in the indentation, or a content line shallower than the first entry.
    """
    indent: int | None = None
    out: list[str] = []
    for line in block:
        stripped = line.lstrip(" ")
        if not stripped.strip():
            out.append(line)  # a blank line stays as it is
            continue
        lead = len(line) - len(stripped)
        if stripped.startswith("\t"):
            return None
        if indent is None and not stripped.startswith("#"):
            indent = lead
        if stripped.startswith("#") and (indent is None or lead >= indent):
            out.append("  " + line)
        elif indent is not None and lead == indent:
            out.append(line[:lead] + "- " + stripped)
        elif indent is not None and lead > indent:
            out.append("  " + line)
        else:
            return None
    return out


def _mcp_servers_block_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Return ``(key line, end)`` of the one block-form ``mcpServers`` in header ``lines``.

    None when there is no such key line, more than one, or a value on the key line (a flow
    collection). The block runs until the next line that starts at column 0 (the next
    top-level key), so ``lines[start + 1 : end]`` are its entries.
    """
    starts = [i for i, line in enumerate(lines) if _MCP_SERVERS_KEY_LINE_RE.match(line)]
    if len(starts) != 1 or not _MCP_SERVERS_BLOCK_KEY_RE.match(lines[starts[0]].rstrip("\r\n")):
        return None
    start = starts[0]
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i][:1] not in ("", " ", "\t", "\r", "\n")
        ),
        len(lines),
    )
    return start, end


def _mcp_servers_as_list(text: str) -> str | None:
    """Return ``text`` with a mapping ``mcpServers`` rewritten as the list form, or None.

    The old mapping spelling (``mcpServers:`` then ``name: definition`` lines) is the one
    Claude Code ignores; the list spelling (``- name: definition``) is the one it reads.
    The rewrite is textual (:func:`_indent_as_list_items`): each entry becomes one list
    item, in order, and only the indentation of the ``mcpServers`` block changes. Values,
    quoting and comments are kept byte for byte, so a value PyYAML (YAML 1.1) and the CLI's
    YAML 1.2 loader read differently (``yes``, ``0755``) still reads as it did.

    Fail closed: the rewrite is re-parsed, and it is used only when it parses to exactly the
    original frontmatter with ``mcpServers`` as that list and the body unchanged. That check
    also catches two entries with the same name, which a mapping collapses and a list keeps.
    Anything else returns None — a flow mapping on the key line, a duplicate top-level
    ``mcpServers`` line, a block the line scan cannot bound — and the editor shows the file
    as it is. Its save is then refused with a message that explains the list form. Nothing
    here writes to disk.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    try:
        frontmatter, body = parse_frontmatter(text)
    except cw.InvalidCandidateError:
        return None
    servers = frontmatter.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        return None
    # A NEL/LS/PS character never reaches here: `parse_frontmatter` above went through the
    # shared seam, which refuses it, so "the next line" means the same to both YAML versions.
    lines = match.group(1).splitlines(keepends=True)
    bounds = _mcp_servers_block_bounds(lines)
    if bounds is None:
        return None
    start, end = bounds
    block = _indent_as_list_items(lines[start + 1 : end])
    if block is None:
        return None
    rewritten = "".join(lines[: start + 1]) + "".join(block) + "".join(lines[end:])
    candidate = text[: match.start(1)] + rewritten + text[match.end(1) :]
    try:
        new_frontmatter, new_body = parse_frontmatter(candidate)
    except cw.InvalidCandidateError:
        return None
    expected = [{server_name: definition} for server_name, definition in servers.items()]
    if new_body != body or new_frontmatter != {**frontmatter, "mcpServers": expected}:
        return None
    return candidate


#: Ends the editor note when :func:`_convert_inline_servers` converted an inline server.
INLINE_SERVERS_CONVERSION_TAIL = "Save to write the converted servers."

#: A key line of an inline definition that :func:`_convert_inline_servers` may edit.
_CONVERTED_KEY_RE = re.compile(r"(transport|env)[ \t]*:")


def _is_item_line(stripped: str) -> bool:
    """Whether a line, without its indentation, starts a block sequence item (``- ``)."""
    return stripped[:1] == "-" and stripped[1:2] in ("", " ", "\t", "\r", "\n")


def _edit_inline_definitions(block: list[str], edits: list[dict[str, str]]) -> list[str]:
    """Apply ``edits`` (one per list item, in order) to the lines of a list-form block.

    Each item starts with a ``- `` line at the block's item column. Inside an item, the
    definition's keys sit at the column of the first content line under the item line. A
    key line there that names an edited field is renamed to ``type`` (``"rename"``) or
    removed with every deeper line under it (``"drop"``). Every other line is kept as it
    is, so a shape this cannot place is left unchanged, and the caller's re-parse check
    then refuses the result.
    """
    out: list[str] = []
    item_indent: int | None = None
    key_indent: int | None = None
    index = -1
    dropping = False
    for line in block:
        stripped = line.lstrip(" ")
        if not stripped.strip():
            out.append(line)
            continue
        lead = len(line) - len(stripped)
        deeper = key_indent is not None and lead > key_indent
        if stripped.startswith("#"):
            if not (dropping and deeper):
                out.append(line)
            continue
        if item_indent is None:
            item_indent = lead
        if lead == item_indent and _is_item_line(stripped):
            index, key_indent, dropping = index + 1, None, False
            out.append(line)
            continue
        if key_indent is None and lead > item_indent:
            key_indent = lead
        if dropping and deeper:
            continue
        dropping = False
        key = _CONVERTED_KEY_RE.match(stripped) if lead == key_indent else None
        action = edits[index].get(key.group(1)) if key and 0 <= index < len(edits) else None
        if action == "rename":
            out.append(line[:lead] + "type" + stripped[len("transport") :])
        elif action == "drop":
            dropping = True
        else:
            out.append(line)
    return out


def _convert_inline_servers(text: str) -> tuple[str, list[str]] | None:
    """Return ``text`` without the inline-server fields Claude Code ignores, and the notes.

    This is the MCP tab's convert-on-load, the same
    :func:`clauster.config_write_mcp._convert_legacy_entry`, for each inline definition of
    a list-form ``mcpServers``: ``transport`` becomes ``type`` (or is removed when ``type``
    is set), and ``env`` is removed from a remote server. Each note names the server and
    the change, never a value.

    The rewrite is textual, like :func:`_mcp_servers_as_list`: only those key lines (and
    the lines under a removed key) change, so every other value and comment keeps its
    exact spelling. Fail closed: the result is used only when it re-parses to exactly the
    converted frontmatter with the body unchanged. A definition in flow style, a field
    that comes in through a ``<<`` merge, or any other shape the line scan cannot edit
    returns None, and the editor shows the file as it is. Its save is then refused with
    the validator's message. Nothing here writes to disk.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    try:
        frontmatter, body = parse_frontmatter(text)
    except cw.InvalidCandidateError:
        return None
    servers = frontmatter.get("mcpServers")
    if not isinstance(servers, list):
        return None
    expected: list[Any] = []
    edits: list[dict[str, str]] = []
    notes: list[str] = []
    for item in servers:
        edit: dict[str, str] = {}
        new_item = item
        if isinstance(item, dict) and len(item) == 1:
            ((server_name, definition),) = item.items()
            converted, entry_notes = _convert_legacy_entry(definition)
            for note in entry_notes:
                field = note["field"]
                renames = field == "transport" and "type" not in definition
                edit[field] = "rename" if renames else "drop"
                notes.append(f"Inline MCP server {server_name!r}: {note['message']}.")
            if entry_notes:
                new_item = {server_name: converted}
        expected.append(new_item)
        edits.append(edit)
    # A NEL/LS/PS character never reaches here: `parse_frontmatter` above went through the
    # shared seam, which refuses it, so "the next line" means the same to both YAML versions.
    if not notes:
        return None
    lines = match.group(1).splitlines(keepends=True)
    bounds = _mcp_servers_block_bounds(lines)
    if bounds is None:
        return None
    start, end = bounds
    block = _edit_inline_definitions(lines[start + 1 : end], edits)
    rewritten = "".join(lines[: start + 1]) + "".join(block) + "".join(lines[end:])
    candidate = text[: match.start(1)] + rewritten + text[match.end(1) :]
    try:
        new_frontmatter, new_body = parse_frontmatter(candidate)
    except cw.InvalidCandidateError:
        return None
    if new_body != body or new_frontmatter != {**frontmatter, "mcpServers": expected}:
        return None
    return candidate, notes


# ---------------------------------------------------------------------------
# Masked frontmatter (TASK-427): the editor view and its keep-stored save
# ---------------------------------------------------------------------------

#: The editor note when a file's frontmatter holds a secret that cannot be masked in place.
MASK_UNAVAILABLE_NOTE = (
    "This file's frontmatter holds a secret value in a YAML form the editor cannot mask in "
    "place (an anchor, an alias or a merge key on it, or an ordered map or set around it), "
    "so the file is not shown here. Edit it by hand."
)

#: The fields :func:`_merge_frontmatter` merges with their own tab's rules.
_OWN_MERGE_FIELDS = frozenset({"mcpServers", "hooks"})


def _server_item_view(item: Any) -> Any:
    """Return the display copy of one ``mcpServers`` list item; a server NAME is never a hint."""
    if isinstance(item, dict) and len(item) == 1:
        ((server_name, definition),) = item.items()
        return {server_name: _redact_entry(definition)}
    return cw.redact_secrets(item)


def frontmatter_view(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Return the editor's copy of a parsed frontmatter, with every secret value masked.

    Each field is masked by the tab that owns its shape, so a value masks here exactly as it
    does there, by key name AND by value shape:

    * every field: :func:`~clauster.config_write.redact_secrets` (a secret-shaped key, a
      ``${VAR}`` interpolation, a credential URL);
    * ``mcpServers``: the MCP tab's masking of each inline definition
      (:func:`clauster.config_write_mcp._redact_entry`: every ``env`` and ``headers`` value,
      a ``headersHelper``). It runs per definition, so a server NAME never acts as a secret
      hint: a server called ``oauth-gw`` keeps its ``type`` and ``url`` visible;
    * ``hooks``: the Hooks tab's masking (:func:`clauster.config_write_hooks._redact_hooks`:
      every ``headers`` value, URL credentials and token-like URL parts, ``input``);
    * ``env``: every non-empty string value, as on the MCP tab.

    :func:`_merge_frontmatter` is the write-side twin. Whole keys are replaced on the copy,
    never a nested value in place (``redact_secrets`` may share nested objects).
    """
    view = cw.redact_secrets(frontmatter)
    servers = frontmatter.get("mcpServers")
    if isinstance(servers, dict):
        view["mcpServers"] = {name: _redact_entry(entry) for name, entry in servers.items()}
    elif isinstance(servers, list) and _is_pair_list(servers):
        # The `!!omap`/`!!pairs` spelling, a list of 2-tuples. The NAME goes through the
        # redactor too, unlike a dict key: `construct_yaml_omap` puts no hashability
        # constraint on a key, so an omap "name" can be a whole container holding a secret.
        view["mcpServers"] = [
            [cw.redact_secrets(name), _redact_entry(entry)] for name, entry in servers
        ]
    elif isinstance(servers, list):
        view["mcpServers"] = [_server_item_view(item) for item in servers]
    hooks = frontmatter.get("hooks")
    if isinstance(hooks, dict):
        deep_ok = _depth_at_most(hooks, _MAX_BLOCK_DEPTH)
        view["hooks"] = _redact_hooks(hooks) if deep_ok else cw.REDACTION_SENTINEL
    env = frontmatter.get("env")
    if isinstance(env, dict):
        shown = view["env"]
        view["env"] = {
            k: cw.REDACTION_SENTINEL if isinstance(v, str) and v else shown[k]
            for k, v in env.items()
        }
    return view


def _texts(value: Any, *, unordered_only: bool = False) -> Iterator[str]:
    """Yield every string leaf of a loaded YAML value (iterative, every container kind).

    ``unordered_only`` yields only the strings inside an ``!!omap``/``!!pairs`` entry or a
    ``!!set``, the containers a mask cannot be bound in by key or position.
    """
    stack: list[tuple[Any, bool]] = [(value, False)]
    while stack:
        node, inside = stack.pop()
        if isinstance(node, str):
            if inside or not unordered_only:
                yield node
        elif isinstance(node, dict):
            stack.extend((v, inside) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, inside) for v in node)
        elif isinstance(node, tuple | set | frozenset):
            stack.extend((v, True) for v in node)


def _carries_mask(value: Any, *, unordered_only: bool = False) -> bool:
    """Whether a string leaf of ``value`` holds the redaction sentinel."""
    return any(
        cw.REDACTION_SENTINEL in text for text in _texts(value, unordered_only=unordered_only)
    )


def _merge_servers(incoming: Any, stored: Any) -> Any:
    """Merge the ``mcpServers`` list with the MCP tab's keep-stored rules, or refuse (→ 422).

    An inline definition is paired with the stored definition of the same server NAME (the
    stored block may still be the old mapping form, which the editor converted on load) and
    merged by :func:`clauster.config_write_mcp._merge_kept`. That refuses a mask with no
    stored value, a masked credential whose server's ``type`` or url text changed (compared
    as exact text, never parsed), and a masked ``env`` value whose stdio ``command`` or
    ``args`` changed. A masked server reference by name keeps the stored one only at the
    same position of an unchanged-length list.
    """
    # `incoming` is a list: the validator refused any other shape before this runs.
    stored_defs: dict[Any, Any] = {}
    repeated: set[Any] = set()
    pairs: list[tuple[Any, Any]] = []
    if isinstance(stored, dict):
        pairs = list(stored.items())
    elif isinstance(stored, list):
        pairs = [
            next(iter(item.items()))
            for item in stored
            if isinstance(item, dict) and len(item) == 1
        ]
    for server_name, definition in pairs:
        if server_name in stored_defs:
            repeated.add(server_name)
        stored_defs[server_name] = definition
    merged: list[Any] = []
    for index, item in enumerate(incoming):
        if isinstance(item, dict) and len(item) == 1:
            ((server_name, definition),) = item.items()
            if contains_sentinel(definition):
                if server_name in repeated:
                    raise cw.InvalidCandidateError(
                        f"frontmatter 'mcpServers': server {server_name!r} keeps a masked "
                        f"value {cw.REDACTION_SENTINEL}, but the stored file defines it twice. "
                        "Type the real value"
                    )
                try:
                    definition = _merge_kept(
                        definition, stored_defs.get(server_name), path=str(server_name)
                    )
                except cw.InvalidCandidateError as exc:
                    raise cw.InvalidCandidateError(f"frontmatter 'mcpServers': {exc}") from exc
            merged.append({server_name: definition})
        elif item == cw.REDACTION_SENTINEL:
            same_list = isinstance(stored, list) and len(stored) == len(incoming)
            held = stored[index] if same_list else None
            if not isinstance(held, str) or cw.redact_secrets(held) != cw.REDACTION_SENTINEL:
                raise cw.InvalidCandidateError(
                    f"frontmatter 'mcpServers'[{index}] is the masked value "
                    f"{cw.REDACTION_SENTINEL}, but no stored server name is masked at that "
                    "place. Type the real name"
                )
            merged.append(held)
        else:
            merged.append(item)
    return merged


def _merge_frontmatter(incoming: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """Merge a saved frontmatter over the stored one, keeping each bound mask (→ 422 if not).

    The write-side twin of :func:`frontmatter_view`, each field with its own tab's rules:
    ``mcpServers`` with :func:`_merge_servers`, ``hooks`` with
    :func:`clauster.config_write_hooks._merge_hooks` (a masked URL or header is kept only
    for a hook whose masked fields are unchanged, at one stored destination), and every
    other field by key with :func:`~clauster.config_write.merge_redacted`, after the
    Settings tab's check (:func:`clauster.config_write_settings._orphan_sentinel_path`)
    refuses a mask with no stored value at its key or list position. A mask is never
    written as the literal ``********``: it is bound to a stored value or refused.
    """
    if _carries_mask(incoming, unordered_only=True):
        raise cw.InvalidCandidateError(
            f"frontmatter holds the mask {cw.REDACTION_SENTINEL} inside an ordered map or a "
            "set, where it cannot be matched to a stored value. Type the real value"
        )
    rest_in = {k: v for k, v in incoming.items() if k not in _OWN_MERGE_FIELDS}
    rest_stored = {k: v for k, v in stored.items() if k not in _OWN_MERGE_FIELDS}
    where = _orphan_sentinel_path(rest_in, rest_stored, "frontmatter")
    if where is not None:
        raise cw.InvalidCandidateError(
            f"{where} is the masked value {cw.REDACTION_SENTINEL}, but no stored value can be "
            "kept there (a key was renamed, or a list changed). Type the real value"
        )
    rest = cw.merge_redacted(rest_in, rest_stored)
    merged: dict[str, Any] = {}
    for key, value in incoming.items():
        if key == "mcpServers":
            merged[key] = _merge_servers(value, stored.get(key))
        elif key == "hooks" and _has_sentinel(value):
            try:
                merged[key] = _merge_hooks(value, stored.get(key))
            except cw.InvalidCandidateError as exc:
                raise cw.InvalidCandidateError(f"frontmatter 'hooks': {exc}") from exc
        elif key == "hooks":
            merged[key] = value
        else:
            merged[key] = rest[key]
    return merged


def _changed_scalars(
    root: yaml.Node, value: Any, target: Any
) -> list[tuple[yaml.ScalarNode, Any]] | None:
    """Pair each scalar node of a header whose loaded value differs in ``target`` with it.

    Walks the composed header and its loaded value side by side. ``None`` when a change
    cannot be placed on the text of one scalar: a node reached at a second place (an
    alias), a mapping with a merge key, a container whose shape changed, or a container the
    walk cannot pair with its node (an ``!!omap``, ``!!pairs`` or ``!!set``).
    """
    slots: list[tuple[yaml.ScalarNode, Any]] = []
    changed: set[int] = set()
    stack: list[tuple[yaml.Node, Any, Any]] = [(root, value, target)]
    while stack:
        node, have, want = stack.pop()
        if cw.same_yaml_value(have, want):
            continue
        if id(node) in changed:
            return None
        changed.add(id(node))
        if isinstance(node, yaml.ScalarNode) and not isinstance(want, cw._YAML_CONTAINERS):
            slots.append((node, want))
        elif (
            isinstance(node, yaml.MappingNode)
            and type(have) is dict
            and type(want) is dict
            and len(node.value) == len(have)
            and have.keys() == want.keys()
            and all(key.tag != cw._YAML_MERGE_TAG for key, _sub in node.value)
        ):
            # No merge key and no repeated key (the seam refuses one), so the loaded dict
            # holds the node's pairs in their order.
            stack.extend(
                (sub, inner, want[key])
                for (_key, sub), (key, inner) in zip(node.value, have.items(), strict=True)
            )
        elif (
            isinstance(node, yaml.SequenceNode)
            and type(have) is list
            and type(want) is list
            and len(node.value) == len(have) == len(want)
        ):
            stack.extend(zip(node.value, have, want, strict=True))
        else:
            return None
    return slots


def _render_scalar(value: Any) -> str | None:
    """Return a one-line YAML spelling of a scalar that both YAML 1.1 and 1.2 load alike.

    A string is double-quoted by PyYAML's own emitter, which escapes every line break and
    every character a plain or single-quoted scalar could misread. ``None`` for a value that
    has no one-line spelling here (``bytes``, a date).
    """
    if isinstance(value, str):
        text = yaml.safe_dump(value, default_style='"', width=float("inf"), allow_unicode=True)
        return text.rstrip("\n")
    if value is None or isinstance(value, bool | int | float):
        return yaml.safe_dump(value, width=float("inf")).removesuffix("...\n").rstrip("\n")
    return None


def _scalar_spellings(text: str | None) -> dict[tuple[type, Any], str]:
    """Return the source text of each scalar in the frontmatter of ``text``, by its value.

    Used to put a kept value back spelled as it is stored, so an unchanged save leaves the
    bytes as they were. A spelling is only a first try: the rewritten header must still
    load to exactly the merged value, or a quoted spelling is used instead.
    """
    out: dict[tuple[type, Any], str] = {}
    match = _FRONTMATTER_RE.match(text) if text is not None else None
    if text is None or match is None:
        return out
    # The caller passes only a stored file whose frontmatter loaded, so this composes; the
    # caller's `refuse_check_failures` would turn a failure into "cannot rewrite".
    source = cw.cli_frontmatter_text(cw.frontmatter_header(text, match))
    root = yaml.compose(source, Loader=yaml.SafeLoader)
    constructor = yaml.constructor.SafeConstructor()
    stack = [root] if root is not None else []
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            stack.extend(child for pair in node.value for child in pair)
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
        else:  # a ScalarNode, the only other kind `compose` builds
            spelling = source[node.start_mark.index : node.end_mark.index].rstrip(" \t\r\n")
            if spelling.startswith("&"):
                continue  # a second copy of an anchor would be a duplicate anchor
            try:
                loaded = constructor.construct_object(node)
                out.setdefault((type(loaded), loaded), spelling)
            except cw._CHECK_FAILURES:
                continue
    return out


def _splice(
    text: str,
    match: re.Match[str],
    source: str,
    slots: list[tuple[yaml.ScalarNode, Any]],
    spellings: dict[tuple[type, Any], str],
) -> str | None:
    """Return ``text`` with each slot's scalar in header ``source`` replaced by its new value."""
    out: list[str] = []
    prev = 0
    # Distinct scalar nodes never overlap, and a slot's value is never a container, so it
    # is hashable (`_changed_scalars`).
    for node, want in sorted(slots, key=lambda slot: slot[0].start_mark.index):
        start, end = node.start_mark.index, node.end_mark.index
        span = source[start:end]
        core = span.rstrip(" \t\r\n")
        rendered = spellings.get((type(want), want)) or _render_scalar(want)
        if rendered is None:
            return None
        # A block scalar's span ends after its line breaks: keep them for the next line.
        out.extend((source[prev:start], rendered, span[len(core) :]))
        prev = end
    out.append(source[prev:])
    header = "".join(out)
    if text.startswith("\r", match.end(1)):
        # `frontmatter_header` added the CRLF closing fence's `\r`, which stays in the text.
        header = header.removesuffix("\r")
    return text[: match.start(1)] + header + text[match.end(1) :]


def _loads_as(candidate: str, target: dict[str, Any], body: str) -> bool:
    """Whether ``candidate`` keeps ``body`` and its frontmatter loads to exactly ``target``."""
    match = _FRONTMATTER_RE.match(candidate)
    if match is None or candidate[match.end() :] != body:
        return False
    try:
        loaded, _body = parse_frontmatter(candidate)  # the shared seam, every check of a write
    except cw.InvalidCandidateError:
        return False
    return cw.same_yaml_value(loaded, target)


def _rewrite_values(text: str, target: dict[str, Any], *, stored: str | None) -> str | None:
    """Return ``text`` with the frontmatter values that differ in ``target`` rewritten, or None.

    Text-level, like the convert-on-load rewrites above: only the scalars whose value
    changes are replaced (:func:`_changed_scalars`), each where it stands, so every other
    byte of the header, every comment and the whole body stay as they are. A value is
    spelled as in ``stored`` when it can be (:func:`_scalar_spellings`), else double-quoted
    (:func:`_render_scalar`).

    Fail closed: the result is used only when it passes the whole shared seam and loads to
    exactly ``target`` with the body unchanged (:func:`_loads_as`). ``None`` when no
    rewrite does. When the header loads only through Claude Code's re-quoting retry
    (:func:`~clauster.config_write.cli_frontmatter_text`), the retry text is the one
    rewritten, so those values come back quoted the way Claude Code reads them.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    source = cw.cli_frontmatter_text(cw.frontmatter_header(text, match))
    body = text[match.end() :]
    try:
        with cw.refuse_check_failures("frontmatter"):
            value, _body = parse_frontmatter(text)
            if cw.same_yaml_value(value, target):
                return text
            root = yaml.compose(source, Loader=yaml.SafeLoader)
            slots = None if root is None else _changed_scalars(root, value, target)
            if not slots:
                return None
            attempts = [_scalar_spellings(stored), {}] if stored is not None else [{}]
            for spellings in attempts:
                candidate = _splice(text, match, source, slots, spellings)
                if candidate is not None and _loads_as(candidate, target, body):
                    return candidate
    except cw.InvalidCandidateError:
        return None
    return None


def _mask_agent_text(text: str) -> str | None:
    """Return a subagent file with its frontmatter secret values masked, or None.

    The masked values are :func:`frontmatter_view`'s, written in place by
    :func:`_rewrite_values`; the Markdown body is never masked or rewritten, and a byte
    order mark is kept. A file with no frontmatter fence is all body and is returned as it
    is. A header that does not load is masked line by line
    (:func:`~clauster.config_write.redact_secret_lines`), the best that can be done without
    its structure. ``None`` when the view cannot be written in place
    (:data:`MASK_UNAVAILABLE_NOTE`); the caller then shows nothing.
    """
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    core = text[len(bom) :]
    match = _FRONTMATTER_RE.match(core)
    if match is None:
        return text
    try:
        frontmatter, _body = parse_frontmatter(core)
        with cw.refuse_check_failures("frontmatter"):
            view = frontmatter_view(frontmatter)
    except cw.InvalidCandidateError:
        masked = cw.redact_secret_lines(match.group(1))
        return bom + core[: match.start(1)] + masked + core[match.end(1) :]
    shown = _rewrite_values(core, view, stored=None)
    return None if shown is None else bom + shown


def _keep_masked_values(
    content: str, stored_bytes: bytes | None, expected_hash: str | None, name: str
) -> str:
    """Return ``content`` with each mask from the editor view bound back to its stored value.

    A save with no mask in its frontmatter is returned as sent. Otherwise the stored file
    must be the one the editor loaded (its hash), the frontmatter is merged
    (:func:`_merge_frontmatter`, which refuses any mask it cannot bind), and the kept values
    are written in place of their masks (:func:`_rewrite_values`). The result is validated
    again, since a kept value comes from the file on disk, not from the checked candidate.
    """
    incoming, _body = parse_frontmatter(content)
    if not _carries_mask(incoming):
        return content
    stored: dict[str, Any] = {}
    stored_text: str | None = None
    if stored_bytes is not None:
        if expected_hash is None:
            raise cw.StaleConfigWriteError(f"{name}.md already exists; a hash is required")
        if cw.hash_bytes(stored_bytes) != expected_hash:
            raise cw.StaleConfigWriteError(f"{name}.md changed on disk since it was loaded")
        try:
            # The editor showed a stored byte order mark, and its save strips it, so the
            # stored text is read without it too (as `_mask_agent_text` read it).
            text = stored_bytes.decode("utf-8").removeprefix("\ufeff")
            stored, _stored_body = parse_frontmatter(text)
            stored_text = text  # only a stored file that loads lends its spellings
        except (UnicodeDecodeError, cw.InvalidCandidateError) as exc:
            # The editor masked this header line by line (`_mask_agent_text`), so a mask can
            # sit inside a value (`run ********`), where no merge sees it as a mask. Nothing
            # stored loads to bind it to: refuse rather than write it as typed.
            raise cw.InvalidCandidateError(
                f"frontmatter keeps a masked value {cw.REDACTION_SENTINEL}, but the stored "
                "frontmatter does not load, so there is no stored value to keep. Type the "
                "real values"
            ) from exc
    merged = _merge_frontmatter(incoming, stored)
    rewritten = _rewrite_values(content, merged, stored=stored_text)
    if rewritten is None:
        raise cw.InvalidCandidateError(
            f"frontmatter keeps a masked value {cw.REDACTION_SENTINEL} that cannot be put back "
            "in its place (an anchor, an alias or a merge key on it). Type the real value"
        )
    try:
        cw.validate_candidate(rewritten, lambda c: validate_agent_content(c, expected_name=name))
    except cw.InvalidCandidateError as exc:
        # The sent text passed these same checks, so the kept stored values are the cause.
        # Their message can name a size or a column of that stored text, which tells the
        # length of a hidden value in one answer: refuse without it. (Repeated saves near
        # the size cap can still find that length; a documented residual.)
        raise cw.InvalidCandidateError(
            "frontmatter does not pass the checks once its masked values are kept: the "
            "stored text behind a mask is not valid here. Type the real values, or fix the "
            "stored file on the host"
        ) from exc
    return rewritten


def _is_builtin_override_file(root: Path, name: str) -> bool:
    """Whether a regular, non-symlink ``<name>.md`` sits in ``root`` for a built-in ``name``.

    The symlink test runs first, through lstat, so a link is never followed here: a
    symlinked file named like a built-in keeps reading as the synthetic built-in doc. A
    built-in name that is not a valid file name (``Explore``) has no file to find.
    """
    if not is_valid_agent_name(name):
        return False
    candidate = root / f"{name}.md"
    return not candidate.is_symlink() and candidate.is_file()


def _read_agent(root: Path, name: str, source: str) -> dict[str, Any]:
    """Return the full detail doc for one subagent, or raise a typed read error.

    A built-in name reads as a synthetic, non-existent, non-editable doc (200-shaped
    — it is "shown but not editable", never a 404: Claude Code really does offer that
    agent, there is just no file backing it here). A missing real file raises
    :class:`AgentNotFoundError` (→ 404). ``frontmatter`` is a **derived, masked**
    display field (:func:`frontmatter_view`); ``content`` is the file text with the same
    frontmatter values masked in place as ``********`` (:func:`_mask_agent_text`) and the
    Markdown body verbatim. A write that sends a mask back keeps the stored value
    (:func:`_keep_masked_values`). When an editable file still uses the old ``mcpServers``
    mapping, ``content`` is that text with the block rewritten in the list form
    (:func:`_mcp_servers_as_list`), and when an inline server has a field Claude Code
    ignores, ``content`` is without it (:func:`_convert_inline_servers`). ``converted`` then
    carries the one note the editor shows for both; otherwise ``converted`` is None. The
    ``frontmatter`` display field always describes the file as it is on disk. When the
    masked values cannot be written in place, ``content`` is empty, the doc is not
    editable, and ``converted`` is :data:`MASK_UNAVAILABLE_NOTE`.

    A built-in name with a regular FILE behind it is the exception: Claude Code lets a user
    or project agent override a built-in of the same name, so that file is real
    configuration. It reads like any other file, but never as editable, because write and
    delete still refuse every built-in name.
    """
    if is_builtin_agent(name) and not _is_builtin_override_file(root, name):
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
        # The same masked view the editor's `content` shows (an `mcpServers` server NAME is
        # never a secret hint there, in the list, mapping and `!!omap` spellings alike).
        with cw.refuse_check_failures("frontmatter"):
            frontmatter = frontmatter_view(parsed)
    except cw.InvalidCandidateError:
        # An unparsable frontmatter block still surfaces via `content` (so the
        # operator can see and fix it); the derived display field just degrades.
        frontmatter = {}
    editable = not read_only and not is_builtin_agent(name)
    # Convert-on-save for the old `mcpServers` mapping (the maintainer's call on
    # TASK-449.5): an editable file is handed to the editor already in the list form, with a
    # note saying so, so the operator sees the change and the next save writes it. `hash`
    # stays the hash of the bytes ON DISK, so the stale-write guard is unaffected. The inline
    # servers of the (possibly just converted) list form then get the MCP tab's convert-on-
    # load (TASK-479), and the one note names every change.
    content, notes = text, []
    if editable:
        as_list = _mcp_servers_as_list(text)
        if as_list is not None:
            content = as_list
            notes.append(MCP_SERVERS_CONVERSION_NOTE)
        inline = _convert_inline_servers(content)
        if inline is not None:
            content = inline[0]
            notes.extend([*inline[1], INLINE_SERVERS_CONVERSION_TAIL])
    # TASK-427: the editor shows each frontmatter secret value as `********`, and a save
    # that sends a mask back keeps the stored value (`_keep_masked_values`). Masked after
    # the conversions, so the view describes the text the editor shows. The body is never
    # masked. When the view cannot be written in place, nothing is shown at all.
    shown = _mask_agent_text(content)
    if shown is None:
        content, editable, notes = "", False, [MASK_UNAVAILABLE_NOTE]
    else:
        content = shown
    return {
        "name": name,
        "source": "plugin" if read_only else source,
        "editable": editable,
        "exists": True,
        "content": content,
        "converted": " ".join(notes) or None,
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
    # TASK-427: the editor shows frontmatter secrets masked. Bind each mask that comes back
    # to its stored value, or refuse (→ 422); never write the literal mask.
    content = _keep_masked_values(content, current if found else None, expected_hash, name)

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
