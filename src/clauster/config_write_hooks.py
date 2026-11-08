"""Hooks config-write surface (#690/#770) over the #347 Foundation seam.

Sibling of the permission-rules surface (#689) and the MCP-server surface (#688): it
adds exactly a **pure structural validator** for a ``hooks`` block and a **router**
that runs the fail-closed Foundation pipeline (gate → confirm → validate → contain →
stale-hash → atomic write) for a write. It owns no gate, no writer, and no redaction
logic of its own — it *consumes* :mod:`clauster.config_write`.

**Scope note (#770):** the ``local`` scope (``.claude/settings.local.json``) and the
enable/disable model were resolved as part of the #766 Foundation extension — this
module already has ``read_project_local_hooks``/``write_project_local_hooks`` below,
and per the maintainer's 2026-06-29 decision, hooks v1 is **add/edit/remove only**
(Claude Code has no native per-hook toggle, so no clauster "disabled" store is
invented). #770's remaining delta is the **plugin-hooks-are-read-only** guard added
to :func:`_validate_hook_entry` below.

This is the **code-executing** config-write surface, and the most security-critical
of the children: a hook is a handler Claude runs on a lifecycle event, so writing a
hook from the browser is the one config-write path that can lead to host RCE. The
off-by-default fail-closed gate and the **validate-never-execute** invariant are the
only protections, and they are absolute here:

* The validator checks **structure only** — event keys, matcher type, entry shape,
  the hook ``type`` and the documented fields of that type (see :data:`HOOK_TYPES`).
  It **never resolves, spawns, runs, shell-parses, or "dry-runs" a command string,
  never sends a request to an ``http`` hook's URL (no DNS lookup either), never
  connects to an ``mcp_tool`` hook's server, and never sends a ``prompt``/``agent``
  hook's prompt to a model.** Every handler is inert data on write; it only ever runs
  later inside a real ``claude`` process. Running it here — even to "check" it — *is*
  the RCE (or the data egress) the gate exists to prevent.
* A bad shape rejects the *whole* write (→ 422 via
  :func:`config_write.validate_candidate`), so a partial/garbled block never lands. An
  event key, a hook type, a field or a ``shell``/``onFailure`` value outside the known
  lists below saves as typed with a warning instead (TASK-524): the lists are copied by
  hand and lag behind each new Claude Code release.

**Five hook types (TASK-449.4).** Claude Code documents ``command``, ``http``,
``mcp_tool``, ``prompt`` and ``agent`` hooks (https://code.claude.com/docs/en/hooks,
"Hook handler fields"), and this surface accepts all five under the same invariant: it
stores the handler as data and never runs it. An ``http`` hook is the one that sends
data off the host (Claude Code POSTs the event JSON to its ``url``), so two more rules
apply to it. Its ``headers`` values are masked on read (they carry tokens, usually by
``$VAR`` interpolation), and its ``url`` keeps the scheme, host, port, plain path
segments and plain query keys visible, so the operator always sees where the data goes,
while credentials, token-like segments and keys, query values and the fragment are
masked (:func:`_url_view`). A masked value sent back keeps the stored value only where
Clauster can say whose value it is (:func:`_restore_destination`): a stored secret never
moves to another host, path, server or tool, nor into a hook edited around its mask.

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

Only the ``hooks`` subtree is written; every sibling key in the file is preserved
verbatim by the locked atomic replace (never a whole-file clobber).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import config_write as cw

#: The top-level key holding the hooks block in ``settings.json``.
HOOKS_KEY = "hooks"

#: The hook ``type`` values (https://code.claude.com/docs/en/hooks, "Hook handler fields").
COMMAND_TYPE = "command"
HTTP_TYPE = "http"
MCP_TOOL_TYPE = "mcp_tool"
PROMPT_TYPE = "prompt"
AGENT_TYPE = "agent"

#: The recognized hook **event** keys: every event the Claude Code hooks reference
#: documents (https://code.claude.com/docs/en/hooks, "Hook lifecycle"), cross-checked
#: against the event enum in the installed CLI (2.1.318). SUGGESTIONS ONLY (TASK-524): an
#: event key outside this set saves as typed, and the save response carries a warning
#: (:func:`~clauster.config_write.warn_unknown_value`), because Claude Code adds events
#: faster than this copy is updated. The Hooks rows offer the set as suggestions.
RECOGNIZED_EVENTS = frozenset(
    {
        "SessionStart",
        "Setup",
        "UserPromptSubmit",
        "UserPromptExpansion",
        "PreToolUse",
        "PermissionRequest",
        "PermissionDenied",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
        "Notification",
        "MessageDisplay",
        "SubagentStart",
        "SubagentStop",
        "TaskCreated",
        "TaskCompleted",
        "Stop",
        "StopFailure",
        "TeammateIdle",
        "InstructionsLoaded",
        "ConfigChange",
        "CwdChanged",
        "DirectoryAdded",
        "FileChanged",
        "WorktreeCreate",
        "WorktreeRemove",
        "PreCompact",
        "PostCompact",
        "PreModelSwitch",
        "PostModelSwitch",
        "Elicitation",
        "ElicitationResult",
        "SessionEnd",
    }
)

#: Fields every hook type accepts (the docs' "Common fields" table). ``once`` is only
#: honored in skill frontmatter, and Claude Code ignores it in a settings file, but it
#: is documented and harmless, so it is accepted rather than refused.
_COMMON_FIELDS = frozenset({"type", "if", "timeout", "statusMessage", "once"})

#: ``type`` → ``(required fields, optional fields)`` beyond :data:`_COMMON_FIELDS`, one
#: row per documented handler type. ``onFailure`` is documented for ``command`` and
#: ``http`` only, ``async``/``asyncRewake`` for ``command`` only ("only available on
#: ``type: "command"`` hooks"), and ``continueOnBlock`` for ``prompt`` only ("agent hooks
#: have ... no ``continueOnBlock`` field"). A field outside its type's row, and any field of
#: a type outside this table, saves with a warning (TASK-524), but its value must still
#: pass :func:`~clauster.config_write.walkable_value`. Where the docs and the installed CLI
#: (2.1.318) differ, what the CLI reads wins (maintainer decision, 2026-11-05): its hook
#: schemas also parse the ``@internal`` fields ``rewakeMessage``/``rewakeSummary``
#: (command) and ``cloud`` (command, http), so those are accepted too, with the CLI's types.
_TYPE_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    COMMAND_TYPE: (
        frozenset({"command"}),
        frozenset(
            {
                "args",
                "async",
                "asyncRewake",
                "shell",
                "onFailure",
                "rewakeMessage",
                "rewakeSummary",
                "cloud",
            }
        ),
    ),
    HTTP_TYPE: (
        frozenset({"url"}),
        frozenset({"headers", "allowedEnvVars", "onFailure", "cloud"}),
    ),
    MCP_TOOL_TYPE: (frozenset({"server", "tool"}), frozenset({"input"})),
    PROMPT_TYPE: (frozenset({"prompt"}), frozenset({"model", "continueOnBlock"})),
    AGENT_TYPE: (frozenset({"prompt"}), frozenset({"model"})),
}

#: The accepted hook types, in the docs' order. Injected into the dashboard as the
#: single source for the Hooks rows editor's type ``<select>``.
HOOK_TYPES = tuple(_TYPE_FIELDS)


def _non_empty_str(value: Any) -> bool:
    """Whether ``value`` is a non-empty string."""
    return isinstance(value, str) and bool(value)


def _is_bool(value: Any) -> bool:
    """Whether ``value`` is a JSON boolean."""
    return isinstance(value, bool)


def _is_positive_number(value: Any) -> bool:
    """Whether ``value`` is a number above zero, as the CLI's ``timeout`` schema requires.

    A JSON ``true`` is a ``bool``, never a number here. Fractions are allowed: the CLI
    reads ``timeout`` as ``number().positive()``. Infinity is refused: ``1e400`` parses to
    ``inf``, which JSON cannot store, so it would be written as ``Infinity`` and leave a
    settings file neither Clauster nor Claude Code can read.
    """
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    # An int is always finite (math.isfinite would raise OverflowError on a huge one).
    return (isinstance(value, int) or math.isfinite(value)) and value > 0


def _all_finite(value: Any) -> bool:
    """Whether every number nested anywhere in ``value`` is finite (no inf, -inf or NaN).

    Iterative, like :func:`_string_leaves`. Applied to an ``mcp_tool`` ``input``, whose
    values are free-form JSON, for the same reason :func:`_is_positive_number` refuses
    infinity.
    """
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, float) and not math.isfinite(node):
            return False
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return True


def _is_str_list(value: Any) -> bool:
    """Whether ``value`` is a list whose every element is a string."""
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _is_header_map(value: Any) -> bool:
    """Whether ``value`` maps non-empty header names to string values."""
    return isinstance(value, dict) and all(
        isinstance(k, str) and k and isinstance(v, str) for k, v in value.items()
    )


def _is_http_url(value: Any) -> bool:
    """Whether ``value`` is an ``http://`` or ``https://`` URL with a host part.

    Parsed only (:func:`urllib.parse.urlsplit`): the host is never resolved and the URL
    is never fetched. Any other scheme cannot receive Claude Code's POST, so it is a
    malformed hook rather than a destination to store.
    """
    if not _non_empty_str(value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:  # e.g. an unbalanced IPv6 bracket
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


#: The deepest container nesting accepted in an ``mcp_tool`` ``input``. The read mask
#: (:func:`~clauster.config_write.redact_secrets`), the keep-stored merge and the JSON
#: renderer all recurse, so an unbounded ``input`` would end in a ``RecursionError`` (a 500,
#: and a file the dashboard can no longer read). Real tool arguments are a few levels deep.
_MAX_INPUT_DEPTH = 32


def _depth_at_most(value: Any, limit: int) -> bool:
    """Whether no dict/list in ``value`` sits ``limit`` or more levels below it (iterative)."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict | list):
            if depth >= limit:
                return False
            children = node.values() if isinstance(node, dict) else node
            stack.extend((child, depth + 1) for child in children)
    return True


#: Field → ``(check, what the value must be)``. Every field in :data:`_COMMON_FIELDS` and
#: :data:`_TYPE_FIELDS` has exactly one row, so a field cannot be accepted unchecked.
_FIELD_CHECKS: dict[str, tuple[Callable[[Any], bool], str]] = {
    "if": (_non_empty_str, "a non-empty string"),
    "timeout": (_is_positive_number, "a number of seconds above 0"),
    "statusMessage": (lambda v: isinstance(v, str), "a string"),
    "once": (_is_bool, "a boolean"),
    "command": (_non_empty_str, "a non-empty string"),
    "args": (_is_str_list, "a list of strings"),
    "async": (_is_bool, "a boolean"),
    "asyncRewake": (_is_bool, "a boolean"),
    # The known values of `shell` and `onFailure` are suggestions (_KNOWN_FIELD_VALUES):
    # any string saves, one outside the list with a warning.
    "shell": (lambda v: isinstance(v, str), "a string ('bash' or 'powershell')"),
    "onFailure": (lambda v: isinstance(v, str), "a string ('continue' or 'block')"),
    "url": (_is_http_url, "an http:// or https:// URL"),
    "headers": (_is_header_map, "an object mapping header names to string values"),
    "allowedEnvVars": (_is_str_list, "a list of strings"),
    "server": (_non_empty_str, "a non-empty string"),
    "tool": (_non_empty_str, "a non-empty string"),
    "input": (
        lambda v: isinstance(v, dict) and _depth_at_most(v, _MAX_INPUT_DEPTH) and _all_finite(v),
        f"an object nested at most {_MAX_INPUT_DEPTH} levels deep, with finite numbers only",
    ),
    "prompt": (_non_empty_str, "a non-empty string"),
    "model": (_non_empty_str, "a non-empty string"),
    "continueOnBlock": (_is_bool, "a boolean"),
    # CLI-parsed, undocumented (see _TYPE_FIELDS). The CLI reads an unknown `cloud` value
    # as "skip", so any string is stored; a non-string is refused.
    "rewakeMessage": (_non_empty_str, "a non-empty string"),
    "rewakeSummary": (_non_empty_str, "a non-empty string"),
    "cloud": (lambda v: isinstance(v, str), "a string ('device' or 'skip')"),
}

#: The known values of a string field (TASK-524). A string outside its tuple saves with a
#: warning; a value that is not a string is refused by :data:`_FIELD_CHECKS`.
_KNOWN_FIELD_VALUES: dict[str, tuple[str, ...]] = {
    "shell": ("bash", "powershell"),
    "onFailure": ("continue", "block"),
}


#: Marker identifying a hook as **plugin-owned**, not project/user/local content. It is
#: checked in every string value of the entry, not only ``command``: an exec-form hook
#: carries the placeholder in ``args`` (``"command": "node", "args":
#: ["${CLAUDE_PLUGIN_ROOT}/x.js"]``), which a ``command``-only check would miss.
#: Claude Code plugins define their own hooks in a ``hooks/hooks.json`` bundled with the
#: plugin — a file this surface never reads or writes (it only ever opens the three
#: settings files behind :func:`read_project_hooks`/:func:`read_user_hooks`/
#: :func:`read_project_local_hooks`) — and that plugin file resolves
#: ``${CLAUDE_PLUGIN_ROOT}`` to the plugin's own install directory so its bundled
#: scripts are portable. That interpolation is meaningless (and never resolved) outside
#: a plugin's own hook definition, so a candidate ``command`` referencing it did not
#: originate as project/user/local content — it was copied from (or intends to shadow)
#: a plugin-owned hook. Plugin hooks are managed **only** through the plugin surface
#: (:mod:`clauster.config_write_plugins`, #342 scope), never by
#: writing this subtree; a candidate carrying the marker is rejected fail-closed
#: (whole write → 422) rather than silently accepted or silently stripped.
_PLUGIN_ROOT_MARKER = "CLAUDE_PLUGIN_ROOT"

#: Allowed keys inside a matcher group (the ``{matcher?, hooks: [...]}`` object).
_MATCHER_GROUP_KEYS = frozenset({"matcher", "hooks"})


def _string_leaves(value: Any) -> Iterator[str]:
    """Yield every string value nested anywhere in ``value`` (dict values, list items).

    Iterative, so a deeply nested ``mcp_tool`` ``input`` cannot exhaust the stack.
    """
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _validate_hook_entry(entry: Any, where: str) -> None:
    """Reject ``entry`` unless it is a structurally valid hook handler object.

    ``type`` must be a string. For a type in :data:`HOOK_TYPES`, its required fields must be
    present, and each field of :data:`_COMMON_FIELDS` and of that type's row
    (:data:`_TYPE_FIELDS`) must pass its :data:`_FIELD_CHECKS` row. A type outside
    :data:`HOOK_TYPES`, a field outside the type's row and a ``shell``/``onFailure`` value
    outside :data:`_KNOWN_FIELD_VALUES` save with a warning (TASK-524); such a field's value
    must still pass :func:`~clauster.config_write.walkable_value`, and a type outside the
    table checks only the common fields. Every value is stored verbatim and **never run**:
    a ``command`` is never parsed, resolved or executed, a ``url`` is never fetched, a
    ``server`` is never connected to, and a ``prompt`` is never sent to a model. Any
    string value carrying the plugin-owned ``CLAUDE_PLUGIN_ROOT`` marker (see
    :data:`_PLUGIN_ROOT_MARKER`) rejects the whole write.
    """
    if not isinstance(entry, dict):
        raise cw.InvalidCandidateError(f"{where} must be an object")

    hook_type = entry.get("type")
    if not isinstance(hook_type, str) or not hook_type:
        # Fail closed: a missing or non-string type is rejected, never silently coerced.
        raise cw.InvalidCandidateError(
            f"{where} 'type' must be a non-empty string, such as one of {list(HOOK_TYPES)} "
            f"(got {hook_type!r})"
        )
    if hook_type in _TYPE_FIELDS:
        required, optional = _TYPE_FIELDS[hook_type]
    else:
        cw.warn_unknown_value(f"{where} 'type'", hook_type, HOOK_TYPES)
        required, optional = frozenset(), frozenset()
    known = _COMMON_FIELDS | required | optional
    if hook_type in _TYPE_FIELDS:
        cw.warn_unknown_keys(entry, known, f"{where} {hook_type!r} hook field")
    else:
        # An unknown type has one warning, on its type: each of its fields is unchecked
        # except for its shape.
        for field, value in entry.items():
            if not isinstance(field, str) or not cw.walkable_value(value):
                raise cw.InvalidCandidateError(
                    f"{where} {field!r} must be a text key whose value is nested at most "
                    f"{cw.MAX_UNKNOWN_VALUE_DEPTH} levels deep, with finite numbers only"
                )

    for field in sorted(required):
        if field not in entry:
            raise cw.InvalidCandidateError(f"{where} {hook_type!r} hook needs {field!r}")
    for field in sorted(set(entry) & (known - {"type"})):
        check, want = _FIELD_CHECKS[field]
        if not check(entry[field]):
            raise cw.InvalidCandidateError(f"{where} {field!r} must be {want}")
        values = _KNOWN_FIELD_VALUES.get(field)
        if values is not None and entry[field] not in values:
            cw.warn_unknown_value(f"{where} {field!r}", entry[field], values)

    for field in sorted(set(entry) - {"type"}, key=repr):
        if any(_PLUGIN_ROOT_MARKER in s for s in _string_leaves(entry[field])):
            # Fail closed (#770): a hook referencing the plugin-root interpolation is
            # plugin-owned content, not project/user/local content — reject the whole
            # write rather than silently store (or silently strip) it. See
            # _PLUGIN_ROOT_MARKER for why this substring is the concrete, structural
            # signal (never a false negative from quoting/braces; a literal false
            # positive would just be an unusual string, safely rejected).
            raise cw.InvalidCandidateError(
                f"{where} {field!r} references a plugin-owned path ({_PLUGIN_ROOT_MARKER}); "
                "plugin hooks are read-only here — manage them via the plugin"
            )


def _validate_matcher_group(group: Any, where: str) -> None:
    """Reject ``group`` unless it is a valid ``{matcher?, hooks: [...]}`` object.

    ``matcher`` is optional and, when present, must be a string. Every documented matcher
    form is a string (``"*"``, ``""``, an exact name, a ``|``/``,`` list, or a JavaScript
    regular expression), and Claude Code ignores it on events without matcher support,
    so it is stored as an opaque pattern and never evaluated here. ``hooks`` is a
    required non-empty list of hook handler entries. Unknown keys reject the whole write.
    """
    if not isinstance(group, dict):
        raise cw.InvalidCandidateError(f"{where} must be an object")
    unknown = set(group) - _MATCHER_GROUP_KEYS
    if unknown:
        raise cw.InvalidCandidateError(f"{where} has unknown keys: {sorted(unknown, key=repr)}")

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

    ``candidate`` is the desired ``hooks`` object: a ``dict`` keyed by a lifecycle
    **event** name, each mapping to a list of matcher groups ``{matcher?: str, hooks:
    [<handler>, ...]}``, where each handler is checked by :func:`_validate_hook_entry`.
    An event name outside :data:`RECOGNIZED_EVENTS` saves with a warning (TASK-524), as do
    the hook types and fields that function names. Wrong types or a bad field value reject
    the whole write (→ 422 via :func:`config_write.validate_candidate`), so a
    partial/garbled block never lands.
    A handler referencing ``${CLAUDE_PLUGIN_ROOT}`` in any string is likewise rejected
    (#770) — that interpolation only resolves inside a plugin's own
    ``hooks/hooks.json``, so its presence marks the entry as plugin-owned content, and
    plugin hooks are read-only through this surface (see :data:`_PLUGIN_ROOT_MARKER`).

    **STRUCTURE ONLY.** No ``command`` string is ever resolved, spawned, shell-parsed,
    or run, no ``url`` is fetched, no MCP ``server`` is called, and no ``prompt`` is
    sent to a model — storing them is fine; running them (even to "check" them) would
    be the RCE this gate exists to prevent. The handler is inert data on write; it only
    ever runs later inside a real ``claude`` process.
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError("hooks must be an object")
    # `key=repr`: frontmatter keys can mix types (`1:` and `foo:`), and a plain sort of an
    # int and a str raises TypeError, a 500.
    for event in sorted(set(candidate) - RECOGNIZED_EVENTS, key=repr):
        if not isinstance(event, str) or not event:
            raise cw.InvalidCandidateError(f"hooks has an event name that is not text: {event!r}")
        cw.warn_unknown_value("hooks event", event, RECOGNIZED_EVENTS)

    for event, groups in candidate.items():
        if not isinstance(groups, list):
            raise cw.InvalidCandidateError(f"hooks {event!r} must be a list of matcher groups")
        for i, group in enumerate(groups):
            _validate_matcher_group(group, f"hooks {event!r}[{i}]")


def _token_like(segment: str) -> bool:
    """Whether a URL path segment or query key looks like a credential, not a route name.

    True for a ``${...}`` interpolation or credential URL
    (:func:`~clauster.config_write.redact_secrets`), for any segment of 20 or more
    characters, and for a segment whose letters and digits alone (punctuation such as
    ``;``, ``:``, ``=``, ``!`` or ``-`` removed) number 8 or more and mix both, such as
    Slack's ``T0123ABCD``/``B0456EFGH``/token segments, ``;tok=ab12cd34`` or a hex id.
    Over-masking a long route name only costs a retype; under-masking would leak.
    """
    if not segment:
        return False
    if cw.redact_secrets(segment) == cw.REDACTION_SENTINEL or len(segment) >= 20:
        return True
    alnum = [c for c in segment if c.isalnum()]
    return len(alnum) >= 8 and any(c.isalpha() for c in alnum) and any(c.isdigit() for c in alnum)


def _is_host_port(host: str) -> bool:
    """Whether ``host`` is a URL host with an optional all-digit port.

    The host is a bracketed IPv6 literal (``[`` up to the first ``]``) or a name with no
    ``:``, ``[`` or ``]``; an optional ``:`` and decimal digits follow. Plain string scans,
    linear in the length, replace a regular expression that CodeQL flagged as polynomial
    (code-scanning alert 184). The accepted set is unchanged; the oracle test in
    ``tests/test_config_write_hooks_host_port.py`` compares the two.
    """
    if host.startswith("["):
        close = host.find("]")
        if close == -1:
            return False
        port = host[close + 1 :]
    else:
        colon = host.find(":")
        end = len(host) if colon == -1 else colon
        if "[" in host[:end] or "]" in host[:end]:
            return False
        port = host[end:]
    return not port or (port[0] == ":" and all(c.isdecimal() for c in port[1:]))


def _url_view(url: str) -> tuple[str, list[str]]:
    """Return ``(read view, hidden parts)`` for an ``http`` hook's URL.

    The view keeps what tells the operator where Claude Code sends the event data: the
    scheme, host, port, plain path segments and plain query keys. It masks with the
    sentinel every ``user:password@``, every token-like path segment and query key
    (:func:`_token_like`), every non-empty query value and a non-empty fragment
    (maintainer decision, 2026-11-05).
    ``hidden`` lists the raw masked parts in order, so two URLs with one view can be told
    apart (:func:`_merge_hooks`).

    A value that is not a well-formed ``http(s)`` URL (:func:`_is_http_url`) is masked
    whole, because a lenient URL parser (WHATWG, which Claude Code runs on) can still
    read credentials or a token out of it. A backslash ends the authority and splits path
    segments, as it does in that parser.
    """
    scheme, _sep, rest = url.partition("://")
    # A tab or line break inside the URL is stripped by both urlsplit and the WHATWG
    # parser, so the raw text and what Claude Code posts to would differ: mask it whole,
    # as for any value whose literal scheme is not the parsed one.
    if (
        not _is_http_url(url)
        or any(ch in url for ch in "\t\r\n")
        or scheme.lower() != urlsplit(url).scheme
    ):
        return cw.REDACTION_SENTINEL, [url]
    hidden: list[str] = []
    end = len(rest)
    for ch in "/?#\\":
        i = rest.find(ch)
        if i != -1:
            end = min(end, i)
    authority, rest = rest[:end], rest[end:]
    host = authority.rsplit("@", 1)[-1]
    if not _is_host_port(host) or urlsplit(url).netloc != authority:
        # The authority did not end where some URL parser ends it: in
        # `https://user:pw\\@evil/x` this text split (and WHATWG) reads `user:pw` as
        # host:port, but urlsplit reads userinfo up to the last `@`, and an all-digit
        # password even passes as a port. The text could be a credential, so mask it all.
        # The parser is compared as exact text and only ever ADDS a reason to mask here;
        # whether a resent URL is unchanged is decided on exact text
        # (_restore_destination), never through a parser.
        return cw.REDACTION_SENTINEL, [url]
    if "@" in authority:
        userinfo = authority.rsplit("@", 1)[0]
        hidden.append(userinfo)
        authority = f"{cw.REDACTION_SENTINEL}@{host}"
    rest, hash_sep, fragment = rest.partition("#")
    path, query_sep, query = rest.partition("?")
    parts = re.split(r"([/\\])", path)
    for i, part in enumerate(parts):
        if i % 2 == 0 and _token_like(part):
            hidden.append(part)
            parts[i] = cw.REDACTION_SENTINEL
    shown_query = []
    for pair in query.split("&") if query else []:
        key, eq, value = pair.partition("=")
        if not eq:  # a bare `?flag` is a value with no key
            key, value = "", key
        if _token_like(key):
            hidden.append(key)
            key = cw.REDACTION_SENTINEL
        if value:
            hidden.append(value)
            value = cw.REDACTION_SENTINEL
        shown_query.append(f"{key}{eq}{value}")
    if fragment:
        hidden.append(fragment)
        fragment = cw.REDACTION_SENTINEL
    view = f"{scheme}://{authority}{''.join(parts)}{query_sep}{'&'.join(shown_query)}"
    return f"{view}{hash_sep}{fragment}", hidden


def _mask_url(url: str) -> str:
    """Return the read view of an ``http`` hook's URL (see :func:`_url_view`)."""
    return _url_view(url)[0]


def mask_url(url: str) -> str:
    """Return the read view of a URL stored outside a hook: an MCP server or marketplace url.

    The same masker as an ``http`` hook's URL (:func:`_url_view`), so every query value,
    the fragment and each token-like path segment or query key show as the sentinel
    (TASK-474). Three differences, each one only adds masking to what these surfaces
    showed before:

    * A value that :func:`~clauster.config_write.redact_secrets` masks whole (a
      ``scheme://user@`` credential URL, a ``${...}`` interpolation) stays masked whole.
    * A ``ws://``/``wss://`` URL is masked as the ``http://``/``https://`` URL with the same
      text after the scheme: WHATWG parses both pairs the same way. Its own scheme shows.
    * Any other value (a ``git@host:owner/repo.git`` source, a ``git://`` URL) is shown as
      stored unless it holds ``?`` or ``#``, and then it is masked whole. A token in such a
      value's path is not detected. Two shapes are masked whole as well, because the WHATWG
      parser Claude Code runs on still reads them as an http(s) or ws(s) URL with any
      credential in them: a value that starts (after control characters or spaces) with one
      of those schemes and a ``:`` but not ``://`` (``https:/user:pw@host``), and a value
      with a tab or line break, which that parser strips anywhere.

    The write side binds a resent view back by exact text against this same function's
    output for the stored value, so the read and the restore can never disagree.
    """
    if cw.redact_secrets(url) == cw.REDACTION_SENTINEL:
        return cw.REDACTION_SENTINEL
    scheme, sep, rest = url.partition("://")
    low = scheme.lower()
    if sep and low in ("http", "https"):
        return _mask_url(url)
    if sep and low in ("ws", "wss"):
        as_http = f"http{low[2:]}"
        view = _mask_url(f"{as_http}://{rest}")
        return view if view == cw.REDACTION_SENTINEL else scheme + view[len(as_http) :]
    if (
        "?" in url
        or "#" in url
        or any(ch in url for ch in "\t\r\n")
        or _LENIENT_WEB_SCHEME_RE.match(url)
    ):
        return cw.REDACTION_SENTINEL
    return url


#: A value a WHATWG parser reads as an http(s) or ws(s) URL although it is not spelled
#: ``scheme://`` exactly: leading C0 controls or spaces, then the scheme and a colon.
_LENIENT_WEB_SCHEME_RE = re.compile(r"[\x00-\x20]*(?:https?|wss?):", re.IGNORECASE)


#: The documented fields that carry no secret and show plain on read when their value is
#: valid: the hook's behavior (``command``, ``args``, ``prompt``, ...), which the operator
#: reviews before a save. ``url``, ``headers`` and ``input`` are masked (:func:`_redact_entry`).
_PLAIN_FIELDS = frozenset(
    _COMMON_FIELDS.union(*(req | opt for req, opt in _TYPE_FIELDS.values()))
    - {"type", "url", "headers", "input"}
)

#: The deepest nesting a stored hooks block may have for the read path to mask it. The
#: documented shape is ``event → [group] → hooks → [entry] → input`` plus the
#: :data:`_MAX_INPUT_DEPTH` levels of an ``input``; a block deeper than this headroom is a
#: hand-edited file, refused (→ 422) before any recursive walk can overflow.
_MAX_BLOCK_DEPTH = _MAX_INPUT_DEPTH + 16


def _redact_entry(entry: Any) -> Any:
    """Return a display copy of one hook handler with its secret-bearing fields masked.

    Masks by field name, whatever the ``type`` says, so a hand-edited entry cannot
    unmask a value by carrying an odd type: every non-empty ``headers`` value (tokens,
    usually ``$VAR``/``${VAR}`` interpolations, and no key-name heuristic can tell a
    benign header from a credential), the credentials and tokens in a ``url``
    (:func:`_url_view`), and the
    :func:`~clauster.config_write.redact_secrets`-shaped values of an ``input``.
    A :data:`_PLAIN_FIELDS` field with a valid value stays plain, as ``command`` always
    has. Any other key, or a plain field holding an invalid value (hand-edited file),
    goes through :func:`~clauster.config_write.redact_secrets`, so an odd shape never
    shows more than a valid one would.
    """
    if not isinstance(entry, dict):
        return cw.redact_secrets(entry)
    out: dict[Any, Any] = {}
    for key, value in entry.items():
        if key == "url" and isinstance(value, str):
            out[key] = _mask_url(value)
        elif key == "headers":
            out[key] = (
                {k: v if v == "" else cw.REDACTION_SENTINEL for k, v in value.items()}
                if isinstance(value, dict)
                else cw.REDACTION_SENTINEL
            )
        elif key == "input" and _depth_at_most(value, _MAX_INPUT_DEPTH):
            out[key] = cw.redact_secrets(value)
        elif key == "input":
            out[key] = cw.REDACTION_SENTINEL  # too deep to walk: masked whole
        elif (key == "type" and isinstance(value, str)) or (
            key in _PLAIN_FIELDS and _FIELD_CHECKS[key][0](value)
        ):
            out[key] = value  # a non-string ``type`` falls through to redact_secrets
        else:
            out[key] = cw.redact_secrets(value, str(key))
    return out


def _redact_hooks(hooks: dict[str, Any]) -> dict[str, Any]:
    """Return a display copy of a stored hooks block with every secret-bearing field masked.

    Walks ``event → [group] → hooks → [entry]`` and masks each entry with
    :func:`_redact_entry`. A string ``matcher`` shows plain: it is a pattern, not a
    secret, and masking it would make an unchanged resave write the mask over it. A node
    that is not the documented shape (hand-edited file) goes through
    :func:`~clauster.config_write.redact_secrets` instead of passing through raw.
    """
    out: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            out[event] = cw.redact_secrets(groups)
            continue
        shown: list[Any] = []
        for group in groups:
            if not isinstance(group, dict):
                shown.append(cw.redact_secrets(group))
                continue
            shown.append(
                {
                    k: (
                        [_redact_entry(e) for e in v]
                        if k == "hooks" and isinstance(v, list)
                        else v
                        if k == "matcher" and isinstance(v, str)
                        else cw.redact_secrets(v, str(k))
                    )
                    for k, v in group.items()
                }
            )
        out[event] = shown
    return out


def _stored_entries(stored: Any) -> list[tuple[Any, Any, dict[str, Any]]]:
    """Return ``(event, matcher, hook)`` for every hook object in a stored hooks block.

    The block may be hand-edited and malformed; anything that is not a hook object under
    a group's ``hooks`` list is skipped.
    """
    found: list[tuple[Any, Any, dict[str, Any]]] = []
    if not isinstance(stored, dict):
        return found
    for event, groups in stored.items():
        for group in groups if isinstance(groups, list) else []:
            entries = group.get("hooks") if isinstance(group, dict) else None
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict):
                    found.append((event, group.get("matcher"), entry))
    return found


def _has_sentinel(value: Any) -> bool:
    """Whether the redaction sentinel appears in any string nested in ``value``."""
    return any(cw.REDACTION_SENTINEL in s for s in _string_leaves(value))


#: The fields the read path masks and a resave can restore (:func:`_restore_destination`).
_RESTORABLE_FIELDS = ("url", "headers", "input")


def _destination(view: dict[str, Any]) -> tuple[Any, ...] | None:
    """Return where a hook's restorable values go, from its READ VIEW, or ``None``.

    ``http``: the masked URL without its query and fragment (a URL that differs only
    there still reaches the same endpoint). ``mcp_tool``: the server and tool. Computed
    from the read view on both sides, so a stored hook and the incoming copy of it agree.

    A hand-edited stored hook whose destination fields are not strings (``"server": []``)
    has no destination: it is never a restore source (a mask cannot restore through it),
    and it never becomes a dict key, so it cannot block a save that removes or replaces
    it. Incoming hooks are validated first, so theirs are always strings.
    """
    if view.get("type") == HTTP_TYPE and isinstance(view.get("url"), str):
        return (HTTP_TYPE, view["url"].split("#", 1)[0].split("?", 1)[0])
    server, tool = view.get("server"), view.get("tool")
    if view.get("type") == MCP_TOOL_TYPE and isinstance(server, str) and isinstance(tool, str):
        return (MCP_TOOL_TYPE, server, tool)
    return None


def _unknown_fields(view: dict[str, Any]) -> str:
    """Return the fields of a hook view outside its type's row, as a stable key.

    TASK-524: such a field saves with a warning, and nothing says it does not name where
    the data goes, so a kept mask is bound to its exact shown value too
    (:func:`_restore_destination`).
    """
    required, optional = _TYPE_FIELDS.get(str(view.get("type")), (frozenset(), frozenset()))
    known = _COMMON_FIELDS | required | optional
    return _key({str(k): v for k, v in view.items() if k not in known})


def _restorable(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the :data:`_RESTORABLE_FIELDS` that ``entry`` carries."""
    return {f: entry[f] for f in _RESTORABLE_FIELDS if f in entry}


def _carries_mask(entry: dict[str, Any]) -> bool:
    """Whether a restorable field of ``entry`` holds the sentinel anywhere."""
    return any(_has_sentinel(v) for v in _restorable(entry).values())


def _masked_leaves(view: Any, raw: Any) -> Iterator[tuple[str, Any]]:
    """Yield ``(path, raw value)`` for each place where ``view`` masks ``raw`` (iterative)."""
    stack: list[tuple[Any, Any, str]] = [(view, raw, "")]
    while stack:
        v, r, path = stack.pop()
        if isinstance(v, dict) and isinstance(r, dict):
            stack.extend((v[k], r.get(k), f"{path}/{k}") for k in v)
        elif isinstance(v, list) and isinstance(r, list) and len(v) == len(r):
            stack.extend(
                (vi, ri, f"{path}[{i}]") for i, (vi, ri) in enumerate(zip(v, r, strict=True))
            )
        elif v != r:
            yield path, r


def _hidden(entry: dict[str, Any], view: dict[str, Any]) -> str:
    """Return the raw values the read view hides for one stored hook, as a stable key."""
    url = entry.get("url")
    hidden = {
        "url": _url_view(url)[1] if isinstance(url, str) else url,
        "headers": dict(_masked_leaves(view.get("headers"), entry.get("headers"))),
        "input": dict(_masked_leaves(view.get("input"), entry.get("input"))),
    }
    return json.dumps(hidden, sort_keys=True, default=repr)


def _key(value: Any) -> str:
    """Return a stable comparison key for a JSON value."""
    return json.dumps(value, sort_keys=True, default=repr)


#: One hook in a destination group: ``(event, matcher, hook, view-or-location)``.
_Placed = tuple[Any, Any, dict[str, Any], Any]


def _restore_destination(incoming: list[_Placed], stored: list[_Placed]) -> None:
    """Restore the masked ``url``/``headers``/``input`` of every incoming hook to ONE destination.

    ``incoming`` holds ``(event, matcher, hook, error location)`` for every incoming hook to
    that destination, ``stored`` holds ``(event, matcher, hook, read view)`` for every stored
    hook there, both in file order. Nothing in the request says which stored hook an
    incoming one came from, so a mask is kept only when that question has one safe answer:

    * **One secret for the destination.** If every stored hook there hides the same raw
      values (:func:`_hidden`), a masked hook whose restorable fields equal one stored
      hook's view exactly takes that hook's raw values. Any edit to a masked field (the
      URL, a header, an ``input`` argument) or to a field outside the type's row
      (:func:`_unknown_fields`, TASK-524) needs its masks retyped. Other fields
      (``timeout``, ``statusMessage``, the event, ...) change freely.
    * **Several secrets for the destination** (two Slack webhooks, one MCP tool with two
      keys). A masked hook cannot be told apart from its neighbour by its masked fields,
      so the hooks there are paired by everything the operator sees: the k-th incoming
      hook with a given event, matcher and whole read view takes the k-th stored hook with
      the same three. The pairing is refused unless both sides hold exactly the same such
      hooks, so moving one of them to another event or matcher, or any edit to any field
      of one of them, needs every mask there retyped. Only hooks identical in every shown
      field can be paired by position, and swapping those changes nothing visible.

    Otherwise the write is refused whole. A stored value therefore only ever comes back
    to its own destination, and never into a hook that was edited or moved around its mask.
    """
    masked = [(e, w) for _ev, _m, e, w in incoming if _carries_mask(e)]
    if not masked:
        return
    refusal = (
        f"keeps masked values ({cw.REDACTION_SENTINEL}), but Clauster cannot tell which "
        "stored hook they belong to — retype every masked value in this hook"
    )
    if len({_hidden(h, v) for _ev, _m, h, v in stored}) == 1:
        by_view: dict[str, dict[str, Any]] = {}
        for _ev, _m, h, v in stored:
            by_view.setdefault(_key([_restorable(v), _unknown_fields(v)]), h)
        for entry, where in masked:
            source = by_view.get(_key([_restorable(entry), _unknown_fields(entry)]))
            if source is None:
                raise cw.InvalidCandidateError(f"{where} {refusal}")
            entry.update(_restorable(source))
        return
    # Pair on the WHOLE read view: hooks that differ only in a shown field (`if`, a
    # statusMessage, ...) must never trade secrets, so any edit among them refuses.
    incoming_keys = [_key([ev, m, e]) for ev, m, e, _w in incoming]
    if sorted(incoming_keys) != sorted(_key([ev, m, v]) for ev, m, _h, v in stored):
        raise cw.InvalidCandidateError(f"{masked[0][1]} {refusal}")
    queues: dict[str, list[dict[str, Any]]] = {}
    for ev, m, h, v in stored:
        queues.setdefault(_key([ev, m, v]), []).append(h)
    for (_ev, _m, entry, _w), key in zip(incoming, incoming_keys, strict=True):
        source = queues[key].pop(0)
        if _carries_mask(entry):
            entry.update(_restorable(source))


def _refuse_bare_mask(entry: dict[str, Any], where: str) -> None:
    """Reject an incoming mask in a field that has no keep-stored restore.

    ``url``, ``headers`` and ``input`` are restored or refused by
    :func:`_restore_destination`.
    Any other field has no keep-stored restore (the read path masks only the secret-shaped
    leaves of a hand-edited invalid value), so writing the sentinel there would replace
    the stored value with the literal mask.

    There is deliberately **no** "unchanged resave" exception. Two earlier versions
    excused a mask whose value a stored hook held literally, matched by content, and both
    let an edited hook whose masked value equalled a neighbour's literal mask write the
    mask over its own stored secret. Nothing in the request identifies which stored hook
    an incoming one came from, so the mask is refused everywhere outside the three
    restorable fields, and those restore only for a whole-hook match. The cost is
    availability only: a settings file whose plain hook field literally holds
    ``"********"`` cannot be saved until that value is changed, and the message says so.
    """
    for field, value in entry.items():
        if field in _RESTORABLE_FIELDS:
            continue
        if any(s == cw.REDACTION_SENTINEL for s in _string_leaves(value)):
            raise cw.InvalidCandidateError(
                f"{where} {field!r} holds the mask {cw.REDACTION_SENTINEL}, which Clauster "
                "cannot tell apart from a masked secret — retype the real value, or change "
                "it if it really is that text"
            )


def _merge_hooks(incoming: dict[str, Any], stored: Any) -> dict[str, Any]:
    """Merge a validated ``incoming`` hooks block over the ``stored`` one (keep-stored).

    The read path masks ``headers`` values, ``url`` credentials and tokens, and
    secret-shaped ``input`` values (:func:`_redact_hooks`). A write that sends a mask back
    keeps the stored value only where :func:`_restore_destination` can say whose value it
    is; every other value in ``incoming`` wins as written. The merged block is validated
    again, because a restored value comes from the file on disk, not from the
    already-validated candidate. Runs under the writer's lock, against the bytes the
    stale-hash guard just accepted. A stored hook deeper than any documented shape is
    ignored as a restore source, so no recursive walk can overflow on it.
    """
    merged: dict[str, Any] = {}
    by_destination: dict[tuple[Any, ...], list[_Placed]] = {}
    for event, groups in incoming.items():
        merged_groups: list[dict[str, Any]] = []
        for gi, group in enumerate(groups):
            entries: list[dict[str, Any]] = []
            for hi, original in enumerate(group["hooks"]):
                entry = dict(original)
                where = f"hooks {event!r}[{gi}] hooks[{hi}]"
                _refuse_bare_mask(entry, where)
                destination = _destination(entry)
                if destination is not None:
                    placed = (event, group.get("matcher"), entry, where)
                    by_destination.setdefault(destination, []).append(placed)
                elif _carries_mask(entry):
                    # A hook type outside HOOK_TYPES (TASK-524) has no destination, so a
                    # masked url/headers/input in it can never be bound to a stored value.
                    raise cw.InvalidCandidateError(
                        f"{where} keeps masked values ({cw.REDACTION_SENTINEL}) in a hook "
                        "type Clauster does not know — retype every masked value in this hook"
                    )
                entries.append(entry)
            merged_groups.append({**group, "hooks": entries})
        merged[event] = merged_groups
    stored_by_destination: dict[tuple[Any, ...], list[_Placed]] = {}
    for event, matcher, s in _stored_entries(stored):
        if not _depth_at_most(s, _MAX_BLOCK_DEPTH):
            continue
        view = _redact_entry(s)
        destination = _destination(view)
        if destination is not None:
            placed = (event, matcher, s, view)
            stored_by_destination.setdefault(destination, []).append(placed)
    for destination, hooks_there in by_destination.items():
        _restore_destination(hooks_there, stored_by_destination.get(destination, []))
    # The incoming block was validated (and warned about) before the merge. This second
    # check runs over values restored from the file, so its warnings are dropped: a warning
    # must never quote a stored value the read view masked (TASK-524).
    with cw.collect_value_warnings():
        validate_hooks(merged)
    return merged


def _read_hooks(path: Path) -> tuple[dict[str, Any], str]:
    """Return ``(hooks, content_hash)`` for a settings file at ``path``.

    The hash is over the *current file bytes* (empty digest when absent) — the caller
    echoes it back on write so the stale-hash guard can reject a stale write (409). The
    returned block is a display copy with secret-bearing fields masked
    (:func:`_redact_hooks`); a ``command`` stays plain. A missing file reads as an empty
    hooks block.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = cw.load_settings_json_obj(raw)
    hooks = data.get(HOOKS_KEY)
    hooks = hooks if isinstance(hooks, dict) else {}
    if not _depth_at_most(hooks, _MAX_BLOCK_DEPTH):
        # A hand-edited block nested deeper than any documented shape. Refuse the read
        # (→ 422, like a corrupt file) before the recursive mask or the JSON encoder can
        # overflow into a 500, and never show it unmasked.
        raise cw.InvalidCandidateError(f"{path.name} 'hooks' is nested too deeply to read")
    return _redact_hooks(hooks), cw.hash_bytes(raw)


def read_project_hooks(project_dir: Path) -> tuple[dict[str, Any], str]:
    """Return ``(hooks, content_hash)`` for a project's ``.claude/settings.json``."""
    return _read_hooks(cw.project_settings_path(project_dir))


def write_project_hooks(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the project ``.claude/settings.json`` ``hooks`` block.

    Ensures the ``<project>/.claude`` parent exists (so a first write to a project that
    has no ``.claude`` dir yet does not fail the atomic writer's ``mkstemp`` in a
    missing directory), then runs the fail-closed Foundation pipeline. The candidate is
    validated *before* the directory is created, so a bad shape (422) leaves the
    filesystem untouched. **No command is ever executed** — only validated for shape
    and stored as inert text.
    """
    cw.validate_candidate(incoming, validate_hooks)
    path = cw.project_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    cw.write_settings_subtree(path, HOOKS_KEY, incoming, expected_hash, merge=_merge_hooks)


def read_user_hooks(settings_json: Path) -> tuple[dict[str, Any], str]:
    """Return ``(hooks, content_hash)`` for the user-scope ``~/.claude/settings.json``."""
    return _read_hooks(settings_json)


def write_user_hooks(
    settings_json: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the user-scope ``~/.claude/settings.json`` ``hooks`` block.

    Ensures the ``~/.claude`` parent exists, then runs the fail-closed Foundation
    pipeline. Like the #689 permission writer (and unlike the #688 user-scope MCP
    writer, which edits a ``~/.claude.json`` subtree), this writes a *separate real
    file* and so carries the same stale-hash guard as the project scope. The candidate
    is validated *before* the directory is created, so a bad shape (422) writes nothing.
    **No command is ever executed** — only validated for shape.
    """
    cw.validate_candidate(incoming, validate_hooks)
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    cw.write_settings_subtree(
        settings_json, HOOKS_KEY, incoming, expected_hash, merge=_merge_hooks
    )


def read_project_local_hooks(project_dir: Path) -> tuple[dict[str, Any], str]:
    """Return ``(hooks, content_hash)`` for a project's ``settings.local.json``."""
    return _read_hooks(cw.project_local_settings_path(project_dir))


def write_project_local_hooks(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the local-scope ``.claude/settings.local.json`` ``hooks`` block.

    Third (local) scope, sibling of the project/user writers above: same fail-closed
    Foundation pipeline and stale-hash guard, targeting a *third* file that is you,
    this project only. :func:`~clauster.config_write.ensure_gitignored` runs *before* the
    write (fail-closed: the write creates a secret-bearing ``.bak``), so
    ``settings.local.json`` and that backup are never accidentally committed (#766) —
    idempotent, so an already-gitignored file is a no-op there. **No command is ever
    executed** — only validated for shape and stored as inert text, exactly like the
    project/user writers.
    """
    cw.validate_candidate(incoming, validate_hooks)
    path = cw.project_local_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Ignore BEFORE writing, never after: the write creates a `.bak` holding the PREVIOUS
    # values, so an ensure_gitignored that failed afterwards would leave that secret-bearing
    # file trackable. Ignoring first is the fail-closed order (the call is idempotent).
    cw.ensure_gitignored(project_dir, ".claude/settings.local.json", ignore_backup_sibling=True)
    cw.write_settings_subtree(path, HOOKS_KEY, incoming, expected_hash, merge=_merge_hooks)
