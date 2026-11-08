"""CLI-driven plugin + marketplace management (#771), over the #766/#687 Foundation.

**Highest blast-radius child of the config-management epic.** A plugin *is* code —
installing one introduces a new executable source (skills/agents/hooks/MCP servers
it bundles) that the spawned ``claude`` process will load and run as the clauster
runtime user. Unlike every sibling surface (MCP servers #688/#769, permissions #689,
hooks #690, subagents, CLAUDE.md, settings #772), **validate-never-execute does not
apply here**: there is no structural shape that makes a plugin "safe" the way an
``mcpServers`` entry's shape can be checked without running it. The control is
therefore, per the design doc's own resolution (2026-06-29, issue #771): the
off-by-default :func:`clauster.config_write.require_capability` gate, **plus** a
*second*, STRONGER confirm on top of the ordinary scope confirm for ``install``
specifically (:func:`require_install_confirm`) — the operator must retype the exact
plugin id being introduced, not just the project/scope name, since that is the one
op that pulls new code onto the host. Marketplaces are operator-added sources (the
gate is the control there too; no extra source allowlist, per the design doc).

First verified against a live ``claude`` 2.1.198, then re-verified on 2026-11-04
against a live ``claude`` 2.1.318 (every subcommand's own ``--help`` plus a throwaway
``HOME``-isolated probe of each verb this module runs — never the real account), and
against the current docs (``code.claude.com/docs/en/plugins/cli-reference``,
``.../plugins/marketplace-reference``, ``.../settings-reference``):

* ``claude plugin enable|disable <plugin> --scope user|project|local`` — the ONLY
  native verbs for enable/disable (unlike MCP server approvals, which had no CLI
  verb and needed a direct settings write). Writes ``enabledPlugins`` in the
  target scope's ``settings.json`` as ``{"<plugin>@<marketplace>": true|false}`` —
  a single map of booleans, confirmed live and by the docs' own examples. There is
  **no** separate ``disabledPlugins`` list (the current docs do not name one) — a
  disabled plugin is simply ``false`` in the same map.
  (:mod:`clauster.config_write_settings`'s ``OWNED_KEYS`` carves out ``enabledPlugins``
  only; Claude Code 2.1.318 does not read ``disabledPlugins``, so the Settings tab shows
  it and can remove it.) Enable of an id that is not installed still blind-writes the
  map entry and exits 0 (confirmed on 2.1.318). Two cases that 2.1.198 accepted now exit
  1, and this module surfaces both as a 400 carrying the CLI's own message rather than
  swallowing them: an id already in the requested state at that scope ("is already
  enabled at project scope"), and a ``--scope`` that does not take precedence over the
  declaring scope ("is installed at project scope, not user"). The dashboard's
  ``enabled`` badge is the MERGED state, so that message is what tells the operator
  which scope already holds the value. ``--json`` and
  ``disable --all`` are not used (``--all`` cannot take ``--scope``, so it would
  write past the one scope the confirm named).
* ``claude plugin install <plugin> --scope user|project|local`` — a plugin from an
  ordinary marketplace installs with no prompt even with stdin closed. Re-installing
  an already-installed id is a clean idempotent success ("already installed").
  ``-y``/``--yes`` (2.1.229+) and ``--accept-command <sha256>`` (2.1.271+) now
  exist; this module **never passes either**. They accept a marketplace-declared
  command (a ``command`` plugin source, or a ``headersHelper``) without a person
  reading it first, so passing one would let a stored marketplace value run code on
  the host. With stdin closed and neither flag, the CLI refuses such an install and
  exits 1, which surfaces as a 400: fail closed. ``--config key=value`` (values can
  be secrets, and argv is no place for a secret), ``--marketplace <source>`` (adds a
  marketplace as a side effect, skipping the marketplace route's own validation), and
  ``--json`` are out of scope.
* ``claude plugin uninstall|remove <plugin> --scope ... [--keep-data] [--prune
  [-y|--yes]]`` — ``-y``/``--yes`` is real here (and on bare ``prune``), gating
  only the ``--prune`` confirmation; it is **required whenever stdin/stdout is
  not a TTY**, so this module always appends it alongside ``--prune`` (never
  passes ``--prune`` without ``-y``) — a spawned child with ``stdin=DEVNULL`` can
  never answer an interactive prompt, so omitting it would hang a real
  dependency-pruning uninstall until the timeout. Uninstalling a not-installed id
  is an error ("not found in installed plugins").
* ``claude plugin update <plugin> --scope user|project|local|managed`` — the
  ``-y``/``--accept-command`` pair exists here too, for a changed marketplace-declared
  command, and is never passed, for the same reason as ``install``. ``managed`` is
  excluded here (out of clauster's scope model, see the design doc's canonical scope
  table — Managed is MDM/system, never clauster-writable). Updating a not-installed
  id is an error ("not found").
* ``claude plugin list --json`` / ``claude plugin details <name>`` — CLI-only
  reads: cache path, install timestamp, and (per-invocation-cwd) live
  ``enabled``/``projectPath`` state have no settings.json equivalent to read
  directly. Confirmed live: the ``enabled`` field in ``list --json`` genuinely
  depends on invocation cwd (a project-scope entry reads ``enabled: true`` only
  from *that* project's directory, ``false`` elsewhere) — the cwd this module
  spawns from is not cosmetic. Each entry carries its install ``scope`` (and, for a
  project/local install, its ``projectPath``), and the list holds every install from
  every cwd, other projects' too (2.1.319, TASK-523; see :func:`annotate_plugin_scopes`).
  ``list --json`` now also carries a plugin's
  ``mcpServers`` definitions, which can hold ``env``/``headers`` credentials, so its
  output goes through :func:`redact_plugin_display` before it leaves this module.
* ``claude plugin list --available --json`` (TASK-516, confirmed on 2.1.319 under a
  throwaway ``HOME``) — an object ``{"installed": [...], "available": [...]}``.
  ``installed`` is the ``list --json`` array. Each ``available`` entry carries
  ``pluginId``, ``name``, ``marketplaceName``, ``source`` and, where the marketplace gives
  them, ``description`` and ``version``. ``available`` leaves out a plugin that is
  installed for the invocation cwd (a User install everywhere, a Project install only in
  its own project), so the cwd is not cosmetic here either. It also lists a built-in
  plugin directory (about 3600 entries, 2 MB) that ``marketplace list`` does not name, so
  :func:`cli_list_offered_plugins` keeps one marketplace's entries and drops the rest.
* ``claude plugin marketplace add <source> --scope ... [--sparse ...]`` — source
  is ``owner/repo[#ref|@ref]``, ``user@host:path``, a URL, or a path; ``--sparse``
  and ``--claudeai`` (an account-hosted marketplace that no settings file declares,
  and that refuses ``--scope``) are out of scope. Re-adding an already-known name
  from the same source is an idempotent success ("already on disk"), never an
  error — confirmed live, so this module treats a nonzero exit here as a genuine
  failure, never something to swallow.
* ``claude plugin marketplace remove|rm <name> [--scope ...]`` — ``--scope`` is
  **optional**; omitting it removes the declaration from *every* scope it
  appears in (confirmed by ``--help``). This module never omits it: every mutation
  in this app is scoped to the one settings file the caller confirmed against, so
  ``--scope`` is always passed explicitly here, matching the confirm token's own
  scope. Removing an unknown name is an error ("not found"), and so is a name that
  the chosen scope does not declare ("is not declared in user settings"); both
  map to a 404.
* ``claude plugin marketplace list --json`` / ``claude plugin marketplace update
  [name]`` — **neither takes ``--scope``**: confirmed live that ``list`` reflects
  ``~/.claude/plugins/known_marketplaces.json``, a single merged pool independent
  of invocation cwd (a marketplace declared at *any* scope, from *any* project,
  shows up from every cwd) — unlike plugin ``list``'s cwd-dependent ``enabled``
  field. Its ``url`` field can carry credentials (``https://user:token@host``), so
  this output is redacted too. This module still routes both through the ordinary
  scope/project/confirm plumbing (for capability gating + which settings file's
  confirm token applies + a stable cwd to spawn from) even though the resulting argv
  never carries ``--scope`` — consistent with every other route in this app rather
  than a special-cased "always user scope" carve-out.

Every spawn here is validate-before-spawn (:mod:`clauster.claude_cli`, list-argv,
never ``shell=True``, ``stdin=DEVNULL`` so a surprise interactive prompt fails
fast rather than hanging the request) — the same discipline
:mod:`clauster.config_write_mcp_cli` uses. Unlike that module, a plugin/marketplace
identifier is never secret-shaped, so there is no "must route around the CLI"
predicate here — every mutation goes through the CLI, always.

Every read that leaves this module (plugin list, marketplace list, declared
marketplaces) is redacted (:func:`redact_plugin_display`): a ``url`` marketplace
source's ``headers`` and ``headersHelper``, and a plugin MCP server's ``env`` and
``headers`` (and ``args``), are the documented places a credential lives on this
surface. Every ``url`` string gets the read view of an ``http`` hook's URL
(:func:`clauster.config_write_hooks.mask_url`): query values, the fragment and token-like
path segments are masked in place, and a credential URL (``scheme://user@host``) whole.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import claude_cli, procutil
from . import config_write as cw
from . import config_write_hooks as cwh
from .config_write_hooks import mask_url

#: Signature every injected subprocess runner must match (``subprocess.run``'s).
RunFn = Callable[..., subprocess.CompletedProcess]

#: A `claude plugin` invocation may need to fetch/checkout a git-backed marketplace
#: (`marketplace add`/`update`) or resolve a plugin's dependency graph (`install`),
#: so this is a little more generous than the MCP CLI's 30s local-JSON-edit bound.
DEFAULT_TIMEOUT_SECONDS = 60.0

_NOT_FOUND_RE = re.compile(r"not found|not installed", re.IGNORECASE)

#: ``marketplace remove --scope``'s message for a name the chosen scope's settings file
#: does not declare (``Marketplace 'x' is not declared in user settings``, confirmed on
#: 2.1.318): absent at that scope, so a 404 like "not found" — for that verb only.
_NOT_DECLARED_RE = re.compile(r"not declared", re.IGNORECASE)

#: Verbs whose failure is never a 404, whatever the message says. ``marketplace-add``
#: fetches its source, so "not found" there is a git/network failure. The two reads
#: name no entity at all, and the dashboard shows a 404 on a read as "this config
#: surface is unavailable", which would hide a real CLI failure behind a wrong state.
_NEVER_NOT_FOUND_OPS = frozenset({"marketplace-add", "list", "marketplace-list"})

#: Identifier charset for a single name segment (plugin name, marketplace name):
#: no leading ``-`` (would be parsed as a CLI option by ``claude``'s argument
#: parser — verified live: an unescaped ``-evil`` positional is rejected as
#: "unknown option"), conservative identifier chars only. Mirrors
#: ``config_write_mcp._REMOVABLE_NAME_RE`` exactly (same threat, same fix).
_NAME_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")

#: A marketplace **source** (``owner/repo``, a URL, or a filesystem path) has a much
#: wider legal charset than an identifier (``/``, ``:``, ``.``, ``~`` are all
#: legitimate), so this only rejects the arg-injection shape (a leading ``-``,
#: which would be parsed as an option) and raw control characters — never a
#: charset allowlist that would reject a real URL/path.
_SOURCE_LEADING_DASH_RE = re.compile(r"^-")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

#: Keys whose EVERY leaf value is masked on read, whatever that leaf's own key is called.
#: A ``url`` marketplace source's ``headers`` (and an inline ``settings``-source plugin
#: entry's) and a plugin MCP server's ``env``/``headers`` routinely hold a literal
#: credential under a benign name (``Cookie``, ``X-Registry``) that the key-name heuristic
#: in :func:`clauster.config_write.redact_secrets` cannot see — the same reason the
#: settings surface masks every ``env`` value. These are maps, so a resent sentinel is
#: restored by NAME on the Settings tab's write path (``merge_redacted``).
_MASK_ALL_LEAVES_KEYS = frozenset({"env", "headers"})

#: Fields replaced by ONE sentinel whatever their shape. A ``headersHelper`` is a command
#: line, and a command can carry a token in its own arguments. A plugin MCP server's
#: ``args`` can hold ``--api-key VALUE``, a secret in a list element with no key at all.
#: One sentinel for the whole value, not one per element, because ``merge_redacted``
#: restores a list by POSITION: per-element sentinels let a deleted or inserted element
#: on the Settings tab shift a stored secret to the wrong place. A single sentinel keeps
#: the stored value whole, and any edit has to retype it in full.
_MASK_WHOLE_KEYS = frozenset({"headersHelper", "args"})


def _mask_leaves(value: Any) -> Any:
    """Return ``value`` with every leaf replaced by the sentinel (dict keys and lists kept)."""
    if isinstance(value, dict):
        return {k: _mask_leaves(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_leaves(v) for v in value]
    return cw.REDACTION_SENTINEL


def _mask_credential_fields(data: Any) -> Any:
    """Return a copy of ``data`` with the documented credential fields masked, at any depth.

    A string ``url`` (a marketplace source's, a plugin MCP server's) gets the read view an
    ``http`` hook's URL gets (:func:`clauster.config_write_hooks.mask_url`), so a token in
    its query string or path is masked in place (TASK-474).
    """
    if isinstance(data, dict):
        out: dict[Any, Any] = {}
        for k, v in data.items():
            if k in _MASK_ALL_LEAVES_KEYS:
                out[k] = _mask_leaves(v)
            elif k in _MASK_WHOLE_KEYS and v not in (None, "", [], {}):
                out[k] = cw.REDACTION_SENTINEL
            elif k == "url" and isinstance(v, str):
                out[k] = mask_url(v)
            else:
                out[k] = _mask_credential_fields(v)
        return out
    if isinstance(data, list):
        return [_mask_credential_fields(v) for v in data]
    return data


def redact_plugin_display(data: Any) -> Any:
    """Return a display copy of a plugin/marketplace read with its secrets masked.

    Two passes. First the documented credential fields are masked unconditionally
    (:data:`_MASK_ALL_LEAVES_KEYS`, :data:`_MASK_WHOLE_KEYS`). Then the shared
    structural heuristic (:func:`clauster.config_write.redact_secrets`) masks the rest:
    a secret-shaped key, a ``${...}`` interpolation, or a credential-bearing URL such as
    a marketplace ``url`` of ``https://user:token@host/repo.git``. Over-masking only
    hides a value from the dashboard; this surface never writes a read value back.
    """
    return cw.redact_secrets(_mask_credential_fields(data))


def redact_named_map(value: dict[str, Any]) -> dict[str, Any]:
    """Redact each entry of a map keyed by a user-chosen name, one entry at a time.

    :func:`clauster.config_write.redact_secrets` treats a secret-shaped key as marking
    its whole subtree secret, so a marketplace named ``auth-tools`` or an MCP server
    named ``github-token`` would lose its type and location to over-masking. Redacting
    per entry keeps the name out of the hint, as ``config_write_mcp`` does for its
    server maps. The name itself is never masked (it is an identifier, not a value).
    """
    return {k: redact_plugin_display(v) for k, v in value.items()}


def _redact_plugin_entry(entry: Any) -> Any:
    """Redact one ``plugin list --json`` entry, its ``mcpServers`` map entry by entry."""
    if not isinstance(entry, dict) or not isinstance(entry.get("mcpServers"), dict):
        return redact_plugin_display(entry)
    out = redact_plugin_display({k: v for k, v in entry.items() if k != "mcpServers"})
    out["mcpServers"] = redact_named_map(entry["mcpServers"])
    return out


class PluginCliError(cw.ConfigWriteError):
    """A ``claude plugin`` invocation failed unexpectedly (→ 400; not a conflict)."""


class PluginNotFoundError(cw.ConfigWriteError):
    """An uninstall/update/install targeted a plugin id not found (→ 404)."""


class MarketplaceNotFoundError(cw.ConfigWriteError):
    """A marketplace remove/update targeted a name not found (→ 404)."""


def _validate_name_segment(value: Any, label: str) -> str:
    """Reject ``value`` unless it is a non-empty, option-safe identifier; return it."""
    if not isinstance(value, str) or not value:
        raise cw.InvalidCandidateError(f"{label} must be a non-empty string")
    # fullmatch, not match: `$` also matches before a trailing newline, so `match` let
    # "a\n" pass. A marketplace's own list now supplies ids (TASK-516).
    if not _NAME_SEGMENT_RE.fullmatch(value):
        raise cw.InvalidCandidateError(
            f"{label} {value!r} must match {_NAME_SEGMENT_RE.pattern} "
            "(identifier chars, no leading '-')"
        )
    return value


def validate_plugin_id(value: Any) -> None:
    """Structurally validate a plugin identifier: ``name`` or ``name@marketplace``.

    Both segments (when the ``@marketplace`` suffix is present) are validated
    against :data:`_NAME_SEGMENT_RE` — a name that starts with ``-`` would be
    parsed by ``claude``'s own argument parser as an option rather than the
    positional plugin id (arg-injection / positional shift, verified live against
    ``claude plugin install -evil``, which errors ``unknown option '-evil'`` rather
    than looking up a plugin named ``-evil``). **Never resolves, fetches, or
    installs anything** — shape only.
    """
    if not isinstance(value, str) or not value:
        raise cw.InvalidCandidateError("plugin id must be a non-empty string")
    if "@" in value:
        name, _, marketplace = value.partition("@")
        _validate_name_segment(name, "plugin name")
        _validate_name_segment(marketplace, "marketplace name")
    else:
        _validate_name_segment(value, "plugin id")


def validate_marketplace_name(value: Any) -> None:
    """Structurally validate a marketplace **name** (the ``@marketplace`` suffix shape).

    Used for ``marketplace remove``/``marketplace update``'s positional name — the
    marketplace's own declared name, never its source (see
    :func:`validate_marketplace_source` for that, a much wider charset).
    """
    _validate_name_segment(value, "marketplace name")


def validate_marketplace_source(value: Any) -> None:
    """Structurally validate a marketplace **source** (``marketplace add``'s argument).

    A source is ``owner/repo``, a full URL, or a filesystem path — far too wide a
    legal charset for an identifier allowlist (see :data:`_NAME_SEGMENT_RE`'s
    docstring). The only two rejections are arg-injection shape (a leading ``-``,
    which ``claude``'s parser would consume as an option — verified live against
    ``claude plugin marketplace add -evil-source``, which the CLI itself refuses
    as an invalid source format) and raw control characters. **Never resolves,
    fetches, or clones the source** — shape only.

    A source that holds the redaction sentinel is refused too: the marketplace views mask
    a url's query values in place (``?token=********``, TASK-474), and a view pasted into
    ``marketplace add`` would make the CLI store the literal mask. The message does not
    echo the source.
    """
    if not isinstance(value, str) or not value:
        raise cw.InvalidCandidateError("marketplace source must be a non-empty string")
    if cw.REDACTION_SENTINEL in value:
        raise cw.InvalidCandidateError(
            f"marketplace source holds the mask {cw.REDACTION_SENTINEL}, which a masked view "
            "shows in place of a stored value; there is nothing to keep here. Type the full url"
        )
    if _SOURCE_LEADING_DASH_RE.match(value):
        raise cw.InvalidCandidateError(
            f"marketplace source {mask_url(value)!r} must not start with '-' "
            "(would be parsed as a CLI option)"
        )
    if _CONTROL_CHAR_RE.search(value):
        raise cw.InvalidCandidateError("marketplace source contains a control character")


def _run(
    binary: str,
    args: list[str],
    *,
    cwd: Path,
    run: RunFn = subprocess.run,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    """Validate-before-spawn ``claude plugin <args>``: resolve binary, list-argv, no shell.

    ``cwd`` matters here in a way it does not for the MCP CLI module: several
    plugin/marketplace verbs' output (notably ``list --json``'s per-entry
    ``enabled`` field) genuinely depends on invocation directory (see the module
    docstring's live-verified findings), so callers must resolve the correct
    project directory (or a neutral one for user scope) before calling this.
    ``stdin`` is closed (``DEVNULL``): confirmed live that a plain ``install``
    never prompts even non-interactively, but a child that unexpectedly falls
    back to an interactive prompt (a future CLI version, an untrusted-source
    dialog) must fail fast, not hang the request.

    **Never lets the argv leak into the error text** — mirrors
    ``config_write_mcp_cli._run`` exactly: the timeout branch builds its message
    from the verb only (never ``str(exc)``, which embeds the whole argv), and the
    generic spawn-failure branch prints only a redacted ``str(exc)``.
    """
    verb = args[0] if args else ""
    resolved = claude_cli.resolve_binary(binary)
    argv = [resolved, "plugin", *args]
    # #958 P6: capture the redacted argv for the audit line. A `marketplace add` source
    # can carry a token in its query string, which the line redaction does not see.
    cw.record_cli_argv("plugin", [mask_url(a) for a in args])
    try:
        return run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            # The CLI writes UTF-8. Without this, Windows decodes with the locale code page
            # (cp1252 has no 0x81), and a UnicodeDecodeError escaped as a 500.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=procutil.child_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PluginCliError(f"claude plugin {verb} timed out after {exc.timeout}s") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise PluginCliError(
            f"claude plugin {verb} failed to run: {cw.redact_secret_lines(str(exc))}"
        ) from exc


def _shown_source(ident: str) -> str:
    """Return ``ident`` (a plugin id or marketplace source) as an error message may show it.

    The read view (:func:`~clauster.config_write_hooks.mask_url`). Where that masks a whole
    ``http(s)`` URL for its ``user:password@`` part, the http hook view is used instead, so
    the host and path stay visible to diagnose a failed ``marketplace add`` while the
    credential, the query values and the fragment stay masked.
    """
    view = mask_url(ident)
    if view == cw.REDACTION_SENTINEL:
        view = cwh._mask_url(ident)
    return cw.redact_secret_lines(view)


def _raise_for_failure(op: str, ident: str, proc: subprocess.CompletedProcess) -> None:
    """Classify a nonzero ``claude plugin`` exit into a typed error, or return on success.

    ``stdout``/``stderr`` are run through :func:`clauster.config_write.
    redact_secret_lines` before ever appearing in an exception message — defense
    in depth (nothing here is expected to be secret-shaped, but the CLI's own
    stdout could in principle echo back something from an existing config).

    A "not found"/"not installed" message maps to the appropriate typed 404 — but
    **only for the verbs where "not found" means the named entity is genuinely
    absent** (uninstall/update/enable/disable of an absent id, install of a plugin
    absent from its marketplace, marketplace remove/update of an unknown name, and
    marketplace remove at a scope that does not declare the name).
    An ``add`` (``marketplace-add``) is deliberately **excluded**: its ``source``
    is a URL/path/repo the CLI must fetch, so a failed add commonly reports
    ``fatal: repository '…' not found`` — a git/network *failure*, not an absent
    clauster-side entity. Classifying that as 404 would be misleading, so an add
    failure always surfaces as a generic :class:`PluginCliError` (→ 400), with the
    redacted stderr kept in the detail so the operator can still diagnose it. The
    ``list``/``marketplace-list`` reads are excluded for the same reason (see
    :data:`_NEVER_NOT_FOUND_OPS`). Every other nonzero exit is likewise a generic 400.
    """
    if proc.returncode == 0:
        return
    # `ident` is a marketplace SOURCE for `marketplace-add`, and a git or url source can
    # embed credentials (`https://user:token@host/repo.git`) or a token in its query string
    # (TASK-474). The CLI often repeats it (`fatal: repository '<source>' not found`), so
    # every copy is replaced by its masked view before the line redaction runs.
    shown = _shown_source(ident)
    raw = (proc.stderr or proc.stdout or "").strip()
    detail = cw.redact_secret_lines(raw.replace(ident, shown) if ident else raw)
    absent = (op not in _NEVER_NOT_FOUND_OPS and bool(_NOT_FOUND_RE.search(detail))) or (
        op == "marketplace-remove" and bool(_NOT_DECLARED_RE.search(detail))
    )
    if absent:
        if op.startswith("marketplace"):
            raise MarketplaceNotFoundError(detail)
        raise PluginNotFoundError(detail)
    raise PluginCliError(f"claude plugin {op} {shown!r} exited {proc.returncode}: {detail}")


# ---------------------------------------------------------------------------
# Plugin mutations: enable / disable / install / uninstall / update
# ---------------------------------------------------------------------------


def cli_enable_plugin(
    binary: str, cwd: Path, plugin_id: str, scope: cw.Scope, *, run: RunFn = subprocess.run
) -> None:
    """Enable ``plugin_id`` at ``scope`` via ``claude plugin enable``.

    Re-validates ``plugin_id`` structurally (belt + suspenders: the route layer
    validates first, but this is the last line before a spawn). Does **not**
    require the plugin to already be installed — the CLI itself blind-writes the
    ``enabledPlugins`` map entry (verified live), so this simply mirrors that. An id
    already enabled at ``scope`` exits 1 on current CLIs and raises
    :class:`PluginCliError` (→ 400) with the CLI's message (see the module docstring).
    """
    validate_plugin_id(plugin_id)
    proc = _run(binary, ["enable", plugin_id, "--scope", scope], cwd=cwd, run=run)
    _raise_for_failure("enable", plugin_id, proc)


def cli_disable_plugin(
    binary: str, cwd: Path, plugin_id: str, scope: cw.Scope, *, run: RunFn = subprocess.run
) -> None:
    """Disable ``plugin_id`` at ``scope`` via ``claude plugin disable``. See enable's docstring."""
    validate_plugin_id(plugin_id)
    proc = _run(binary, ["disable", plugin_id, "--scope", scope], cwd=cwd, run=run)
    _raise_for_failure("disable", plugin_id, proc)


def cli_install_plugin(
    binary: str, cwd: Path, plugin_id: str, scope: cw.Scope, *, run: RunFn = subprocess.run
) -> None:
    """Install ``plugin_id`` at ``scope`` via ``claude plugin install``.

    **The highest-blast-radius single call in this module** — a successful
    install pulls a new marketplace's code (skills/agents/hooks/MCP servers) onto
    the host for ``claude`` to load and run. The route layer's own STRONG
    per-install confirm (:func:`require_install_confirm`) gates this, on top of
    the ordinary scope confirm; this function itself does not re-check that
    confirm (it is a route-layer concern, checked before this is ever called) —
    it only re-validates the identifier shape and spawns. ``-y``/``--yes`` and
    ``--accept-command`` are **never** passed: a plugin from an ordinary marketplace
    installs without a prompt, and one whose marketplace entry declares a command to
    run (a ``command`` source or a ``headersHelper``) is refused by the CLI when stdin
    is closed, which surfaces as :class:`PluginCliError` (→ 400). Clauster never
    accepts such a command on the operator's behalf.
    """
    validate_plugin_id(plugin_id)
    proc = _run(binary, ["install", plugin_id, "--scope", scope], cwd=cwd, run=run)
    _raise_for_failure("install", plugin_id, proc)


def cli_uninstall_plugin(
    binary: str,
    cwd: Path,
    plugin_id: str,
    scope: cw.Scope,
    *,
    keep_data: bool = False,
    prune: bool = False,
    run: RunFn = subprocess.run,
) -> None:
    """Uninstall ``plugin_id`` at ``scope`` via ``claude plugin uninstall``.

    ``keep_data=True`` preserves the plugin's persistent data directory
    (``--keep-data``). ``prune=True`` also removes now-unneeded auto-installed
    dependencies (``--prune``) — **always** paired with ``-y`` when set: the
    ``--prune`` confirmation is required whenever stdin/stdout is not a TTY
    (verified via ``--help``), and this module's spawn always closes stdin, so
    omitting ``-y`` would hang a real prune until the timeout rather than fail
    fast. Uninstalling an id that is not installed at ``scope`` raises
    :class:`PluginNotFoundError` (→ 404).
    """
    validate_plugin_id(plugin_id)
    args = ["uninstall", plugin_id, "--scope", scope]
    if keep_data:
        args.append("--keep-data")
    if prune:
        args.extend(["--prune", "-y"])
    proc = _run(binary, args, cwd=cwd, run=run)
    _raise_for_failure("uninstall", plugin_id, proc)


def cli_update_plugin(
    binary: str, cwd: Path, plugin_id: str, scope: cw.Scope, *, run: RunFn = subprocess.run
) -> None:
    """Update ``plugin_id`` at ``scope`` to its latest version via ``claude plugin update``.

    ``scope`` is restricted to the three clauster manages (project/user/local) —
    the CLI's own fourth value, ``managed``, is never passed here (out of scope,
    see the module docstring). Updating a not-installed id raises
    :class:`PluginNotFoundError` (→ 404). Like :func:`cli_install_plugin`, never passes
    ``-y``/``--accept-command``, so a changed marketplace-declared command is refused
    (→ 400) rather than run unseen.
    """
    validate_plugin_id(plugin_id)
    proc = _run(binary, ["update", plugin_id, "--scope", scope], cwd=cwd, run=run)
    _raise_for_failure("update", plugin_id, proc)


# ---------------------------------------------------------------------------
# Plugin reads: list / details (CLI-only — no settings.json equivalent)
# ---------------------------------------------------------------------------


def cli_list_plugins(
    binary: str, cwd: Path, *, run: RunFn = subprocess.run
) -> list[dict[str, Any]]:
    """Return installed plugins (``claude plugin list --json``), from ``cwd``'s context.

    ``cwd`` is not cosmetic: a project-scope entry's ``enabled`` field reads
    ``true`` only from that project's own directory, ``false`` elsewhere
    (verified live) — so a caller must resolve the intended project directory
    first, exactly like every other scoped read in this app. The result is redacted
    (:func:`redact_plugin_display`): an entry's ``mcpServers`` can carry credentials.
    """
    proc = _run(binary, ["list", "--json"], cwd=cwd, run=run)
    _raise_for_failure("list", "", proc)
    try:
        data = json.loads(proc.stdout or "[]")
    except (ValueError, RecursionError) as exc:
        # Not just JSONDecodeError: a >4300-digit int literal raises a bare ValueError and
        # deeply-nested output raises RecursionError. Either escaped as a 500 instead of
        # the PluginCliError this reader promises.
        raise PluginCliError(f"claude plugin list returned invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise PluginCliError("claude plugin list returned a non-list JSON value")
    _refuse_deep_output("claude plugin list", data)
    return [_redact_plugin_entry(entry) for entry in data]


def _refuse_deep_output(command: str, data: list[Any]) -> None:
    """Raise :class:`PluginCliError` when ``data`` nests past the tier's depth cap.

    ``json.loads`` parses nesting well past what the recursive redaction walk can take
    (1500 levels on 3.12+), and FastAPI's response encoder fails at about 250. Either one
    was a 500 on a read (PR 1838 review, TASK-528). The check is iterative
    (:func:`~clauster.config_write.exceeds_nesting_depth`) and runs before both.
    """
    if cw.exceeds_nesting_depth(data):
        raise PluginCliError(
            f"{command} returned output nested too deeply (more than "
            f"{cw.MAX_NESTING_DEPTH} levels)"
        )


def cli_plugin_details(
    binary: str, cwd: Path, plugin_id: str, *, run: RunFn = subprocess.run
) -> str:
    """Return the human-readable ``claude plugin details <plugin_id>`` text.

    There is no ``--json`` option on ``details`` (confirmed via ``--help``), so
    this returns the CLI's own formatted text verbatim (redacted defensively,
    though nothing here is expected to be secret-shaped).
    """
    validate_plugin_id(plugin_id)
    proc = _run(binary, ["details", plugin_id], cwd=cwd, run=run)
    _raise_for_failure("details", plugin_id, proc)
    return cw.redact_secret_lines(proc.stdout)


# ---------------------------------------------------------------------------
# Marketplace mutations: add / remove / update
# ---------------------------------------------------------------------------


def cli_marketplace_add(
    binary: str, cwd: Path, source: str, scope: cw.Scope, *, run: RunFn = subprocess.run
) -> None:
    """Add a marketplace from ``source`` (URL / ``owner/repo`` / path) at ``scope``.

    Re-adding an already-known name from the same source is an idempotent
    success ("already on disk", verified live) — never an error, so a repeated
    add is safe to retry.
    """
    validate_marketplace_source(source)
    proc = _run(binary, ["marketplace", "add", source, "--scope", scope], cwd=cwd, run=run)
    _raise_for_failure("marketplace-add", source, proc)


def cli_marketplace_remove(
    binary: str, cwd: Path, name: str, scope: cw.Scope, *, run: RunFn = subprocess.run
) -> None:
    """Remove the marketplace declared as ``name`` at ``scope``.

    ``--scope`` is **always** passed explicitly (never omitted): the CLI itself
    treats an omitted ``--scope`` as "remove from every scope it appears in"
    (confirmed via ``--help``), which would let a single project-scope-confirmed
    request reach into another scope's settings file — this module never does
    that; every mutation stays confined to the one scope its confirm token named.
    Removing an unknown name raises :class:`MarketplaceNotFoundError` (→ 404).
    """
    validate_marketplace_name(name)
    proc = _run(binary, ["marketplace", "remove", name, "--scope", scope], cwd=cwd, run=run)
    _raise_for_failure("marketplace-remove", name, proc)


def cli_marketplace_update(
    binary: str, cwd: Path, name: str | None, *, run: RunFn = subprocess.run
) -> None:
    """Refresh one (or, when ``name`` is ``None``, every) marketplace from its source.

    ``marketplace update`` takes **no** ``--scope`` (confirmed via ``--help``) —
    unlike ``add``/``remove``, a marketplace's git checkout is not scope-specific.
    The route layer still gates this through the ordinary scope/project/confirm
    plumbing (for capability gating and a stable ``cwd``), it just never adds a
    ``--scope`` flag to the argv. Updating an unknown ``name`` raises
    :class:`MarketplaceNotFoundError` (→ 404).
    """
    args = ["marketplace", "update"]
    if name is not None:
        validate_marketplace_name(name)
        args.append(name)
    proc = _run(binary, args, cwd=cwd, run=run)
    _raise_for_failure("marketplace-update", name or "", proc)


# ---------------------------------------------------------------------------
# Marketplace reads: list (CLI-only — a single merged pool, cwd-independent)
# ---------------------------------------------------------------------------


def cli_list_marketplaces(
    binary: str, cwd: Path, *, run: RunFn = subprocess.run
) -> list[dict[str, Any]]:
    """Return every known marketplace (``claude plugin marketplace list --json``).

    Confirmed live: unlike plugin ``list``, this reflects a single merged pool
    (``~/.claude/plugins/known_marketplaces.json``) independent of invocation
    ``cwd`` — a marketplace declared at any scope, from any project, appears from
    every directory. ``cwd`` is still taken (for a stable, valid spawn directory
    and API symmetry with the rest of this module) even though it does not change
    the result. The result is redacted (:func:`redact_plugin_display`): a ``git`` or
    ``url`` entry's ``url`` can embed credentials.
    """
    data = _list_marketplaces_raw(binary, cwd, run=run)
    _refuse_deep_output("claude plugin marketplace list", data)
    return redact_plugin_display(data)


def _list_marketplaces_raw(binary: str, cwd: Path, *, run: RunFn) -> list[Any]:
    """Return the UNREDACTED ``marketplace list --json`` entries, for this module's own use.

    Only :func:`cli_list_marketplaces` (which redacts) and :func:`marketplace_plugin_names`
    (which reads each ``installLocation`` and returns plugin names only) call this; the raw
    entries never leave the module.
    """
    proc = _run(binary, ["marketplace", "list", "--json"], cwd=cwd, run=run)
    _raise_for_failure("marketplace-list", "", proc)
    try:
        data = json.loads(proc.stdout or "[]")
    except (ValueError, RecursionError) as exc:
        # The same int-digit ValueError / RecursionError pair as cli_list_plugins.
        raise PluginCliError(
            f"claude plugin marketplace list returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise PluginCliError("claude plugin marketplace list returned a non-list JSON value")
    return data


# ---------------------------------------------------------------------------
# What one marketplace offers (TASK-516): `plugin list --available --json`
# ---------------------------------------------------------------------------


#: An offered plugin's description is cut after this many characters (after masking), so
#: one marketplace cannot send megabytes of text to the browser.
OFFERED_TEXT_MAX = 500


def _offered_text(value: Any) -> str:
    """Return ``value`` masked line by line and then cut, when it is a string, else ``""``.

    Masked BEFORE the cut, as in :func:`_names_text`: a cut inside a credential URL would
    drop the part the line redaction keys on. A lone surrogate (a ``udc00`` escape in the
    JSON) becomes ``?``: the JSON response cannot encode it, so it raised a 500.
    """
    if not isinstance(value, str):
        return ""
    masked = cw.redact_secret_lines(value).encode("utf-8", "replace").decode("utf-8")
    return masked if len(masked) <= OFFERED_TEXT_MAX else masked[:OFFERED_TEXT_MAX] + "…"


def _offered_id(value: Any, suffix: str) -> str | None:
    """Return ``value`` when it is a valid plugin id of the marketplace ``suffix``, else None.

    The id is what an install from the list sends, so it must be the id the row names: a
    valid id (:func:`validate_plugin_id`) whose marketplace part is this marketplace.
    """
    if not isinstance(value, str) or not value.endswith(suffix):
        return None
    try:
        validate_plugin_id(value)
    except cw.InvalidCandidateError:
        return None
    return value


def _install_rank(entry: dict[str, Any], cwd: Path) -> int:
    """Return how closely an ``installed`` entry applies to ``cwd``: lower applies more.

    Claude Code's own precedence: a Local install of this directory (0), then a Project
    install of it (1), then a User install (2). An install for another directory, or with
    no or another scope, ranks last (3).
    """
    scope = entry.get("scope")
    if scope in ("local", "project") and _same_dir(entry.get("projectPath"), cwd):
        return 0 if scope == "local" else 1
    return 2 if scope == "user" else 3


def cli_list_offered_plugins(
    binary: str, cwd: Path, marketplace: str, *, run: RunFn = subprocess.run
) -> list[dict[str, Any]]:
    """Return the plugins ``marketplace`` offers, each marked ``installed`` or not.

    Reads ``claude plugin list --available --json`` from ``cwd`` (see the module
    docstring for its shape). The CLI leaves a plugin that is installed for ``cwd`` out of
    ``available``, so such a plugin comes from ``installed`` instead and is marked
    ``installed``. One id can be installed at several scopes and projects; its row shows
    the install that applies to ``cwd`` (:func:`_install_rank`). A plugin installed only in
    another project stays in ``available``, so it is offered as not installed, as the CLI
    sees it.

    The dashboard installs from this list without a retype of the id, so the row's id and
    label must be the plugin that installs. An entry is kept only when its id is a valid
    plugin id that ends in ``@<marketplace>`` (:func:`_offered_id`), and its ``name`` is
    the name part of that id, never the marketplace's own ``name`` text. Each entry keeps
    only ``id``, ``name``, ``description``, ``version`` and ``installed``: the ``source``
    (a git url) is not needed to show or install the plugin. The description and version
    are marketplace text: masked line by line, cut (:data:`OFFERED_TEXT_MAX`), and then
    masked structurally (:func:`redact_plugin_display`).

    Fails closed: a failed run, empty output, output that is not an object with an
    ``available`` list, or an ``installed`` that is not a list raises
    :class:`PluginCliError`, never an empty list that reads as "offers no plugins".
    """
    validate_marketplace_name(marketplace)
    proc = _run(binary, ["list", "--available", "--json"], cwd=cwd, run=run)
    _raise_for_failure("list", "", proc)
    try:
        # No `or "{}"` fallback: empty output is a failure here, not an empty list.
        data = json.loads(proc.stdout or "")
    except (ValueError, RecursionError) as exc:  # the same pair as cli_list_plugins
        raise PluginCliError(
            f"claude plugin list --available returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("available"), list):
        raise PluginCliError("claude plugin list --available returned no 'available' list")
    installed = data.get("installed", [])
    if not isinstance(installed, list):
        raise PluginCliError("claude plugin list --available returned a non-list 'installed'")
    suffix = "@" + marketplace
    out: dict[str, dict[str, Any]] = {}
    for entry in data["available"]:
        if not isinstance(entry, dict) or entry.get("marketplaceName") != marketplace:
            continue
        plugin_id = _offered_id(entry.get("pluginId"), suffix)
        if plugin_id is None or plugin_id in out:
            continue
        out[plugin_id] = {
            "description": entry.get("description"),
            "version": entry.get("version"),
            "installed": False,
        }
    # `installed` holds every project's installs (TASK-523): per id, keep the one that applies
    # to `cwd` (Greptile, PR 1857), so the row shows the version installed here.
    chosen: dict[str, tuple[int, dict[str, Any]]] = {}
    for entry in installed:
        plugin_id = _offered_id(entry.get("id") if isinstance(entry, dict) else None, suffix)
        if plugin_id is None or plugin_id in out:
            continue
        rank = _install_rank(entry, cwd)
        if plugin_id not in chosen or rank < chosen[plugin_id][0]:
            chosen[plugin_id] = (rank, entry)
    for plugin_id, (_rank, entry) in chosen.items():
        out[plugin_id] = {"description": None, "version": entry.get("version"), "installed": True}
    shown = [
        {
            "id": plugin_id,
            "name": plugin_id[: -len(suffix)],
            "description": _offered_text(e["description"]),
            "version": _offered_text(e["version"]),
            "installed": e["installed"],
        }
        # By the shown name, then the id as a fixed tie-break for names that differ in case.
        for plugin_id, e in sorted(
            out.items(), key=lambda item: (item[0][: -len(suffix)].lower(), item[0])
        )
    ]
    return redact_plugin_display(shown)


# ---------------------------------------------------------------------------
# Install scope of each listed plugin (TASK-523)
# ---------------------------------------------------------------------------
#
# ``plugin list --json`` returns every install the CLI knows, whatever the cwd: a
# ``user`` entry, and ``project``/``local`` entries that carry the ``projectPath`` they
# belong to, including other projects' (confirmed on 2.1.319). ``update`` and
# ``uninstall`` work only at the entry's own scope and project ("Plugin "x" is not
# installed at scope project", "is installed in user scope, not project"), so the tab
# must know which entries are installed where it is looking.


def _same_dir(raw: Any, project_dir: Path) -> bool:
    """Return whether the path text ``raw`` names ``project_dir`` (resolved, case-folded)."""
    if not isinstance(raw, str) or not raw:
        return False
    try:
        left, right = Path(raw).resolve(), project_dir.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def installed_at(entry: Any, scope: str, project_dir: Path | None) -> bool:
    """Return whether a ``plugin list`` entry is installed at ``scope`` (in ``project_dir``).

    A ``user`` entry matches user scope. A ``project`` or ``local`` entry matches only when
    its ``projectPath`` is ``project_dir``. An entry with no ``scope`` field, or another
    value (``managed``), never matches: Clauster does not guess where it lives.
    """
    if not isinstance(entry, dict) or entry.get("scope") != scope:
        return False
    if scope == "user":
        return True
    return project_dir is not None and _same_dir(entry.get("projectPath"), project_dir)


def _project_name(raw: Any, projects_root: Path) -> str | None:
    """Return the project name (posix path under ``projects_root``) for ``raw``, or ``None``."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        rel = Path(raw).resolve().relative_to(projects_root.resolve())
    except (OSError, ValueError, RuntimeError):
        return None
    name = rel.as_posix()
    return None if name in ("", ".") else name


def annotate_plugin_scopes(
    entries: list[Any], scope: str, project_dir: Path | None, projects_root: Path
) -> list[Any]:
    """Return copies of ``entries`` with ``atScope`` and, where they apply, project fields.

    ``atScope`` is :func:`installed_at` for the scope (and project) the tab is viewing.
    A ``project`` or ``local`` entry also gets ``inProject``: whether its ``projectPath`` is
    ``project_dir`` (resolved, so a project name that is a symlink alias of another one
    still matches; always ``False`` at user scope). The tab decides "same project" from
    this, never by comparing names. It also gets ``projectName``, the posix path under
    ``projects_root`` (``None`` when it lies elsewhere), so the tab can link to that
    project's scope. The entry's own fields, ``scope`` included, are kept as the CLI gave
    them.
    """
    out: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        annotated = dict(entry)
        annotated["atScope"] = installed_at(entry, scope, project_dir)
        if entry.get("scope") in ("project", "local"):
            annotated["inProject"] = project_dir is not None and _same_dir(
                entry.get("projectPath"), project_dir
            )
            annotated["projectName"] = _project_name(entry.get("projectPath"), projects_root)
        out.append(annotated)
    return out


# ---------------------------------------------------------------------------
# What an update changed (TASK-517): a read before and after, never a guess
# ---------------------------------------------------------------------------

#: ``plugin list --json`` fields that a real update changes. ``version`` can stay the same
#: when only the commit moved, but ``lastUpdated`` and ``installPath`` change with every
#: re-install; an "already at the latest version" run changes none (confirmed on 2.1.319).
#: ``gitCommitSha`` is compared too, for a CLI that starts to list it.
_UPDATE_FIELDS = ("version", "lastUpdated", "installPath", "gitCommitSha")


def _version_text(entry: dict[str, Any]) -> str:
    """Return ``version X`` for an entry, or ``version unknown`` when it carries none."""
    version = entry.get("version")
    return f"version {version}" if isinstance(version, str) and version else "version unknown"


def describe_plugin_update(
    plugin_id: str,
    scope: str,
    project_dir: Path | None,
    before: list[Any],
    after: list[Any] | None,
) -> str:
    """Return what a ``plugin update`` did, from ``plugin list`` reads before and after it.

    ``after`` is ``None`` when the read after the update failed: the message then says the
    update ran and the version could not be read, and claims nothing else. The entry is
    matched by id AND scope (and project, :func:`installed_at`), because one id can be
    installed at more than one scope. The message is masked line by line before it leaves.
    """

    def find(entries: list[Any]) -> dict[str, Any] | None:
        """Return the entry for ``plugin_id`` installed at this scope and project, or None."""
        return next(
            (
                e
                for e in entries
                if isinstance(e, dict)
                and e.get("id") == plugin_id
                and installed_at(e, scope, project_dir)
            ),
            None,
        )

    old = find(before)
    new = find(after) if after is not None else None
    if after is None:
        msg = (
            f"The update of {plugin_id} ran, but Clauster could not read the installed "
            "version afterwards."
        )
    elif new is None:
        msg = f"The update of {plugin_id} ran, but the plugin is no longer listed at this scope."
    elif old is None:
        msg = f"Updated {plugin_id}. It is now at {_version_text(new)}."
    elif old.get("version") != new.get("version"):
        msg = f"Updated {plugin_id} from {_version_text(old)} to {_version_text(new)}."
    elif any(old.get(k) != new.get(k) for k in _UPDATE_FIELDS):
        msg = f"Updated {plugin_id}. It is still at {_version_text(new)}."
    else:
        msg = f"{plugin_id} is already up to date ({_version_text(new)})."
    return cw.redact_secret_lines(msg)


#: Upper bound on one marketplace file read for the update summary. The official
#: marketplace's file is about 190 KB (2026-11-06); a larger file is not read at all, so a
#: pathological file cannot parse into millions of objects twice per update.
MARKETPLACE_FILE_MAX_BYTES = 8 * 1024 * 1024

#: How many plugin names an update summary lists before "and N more".
_NAMES_SHOWN = 5

#: A plugin name longer than this is cut in the summary (the name is marketplace text).
_NAME_SHOWN_MAX = 80


def _read_marketplace_plugin_names(location: Any) -> list[str] | None:
    """Return the plugin names a marketplace checkout offers, or ``None`` if unreadable.

    ``location`` is the CLI's own ``installLocation``: a directory holding
    ``.claude-plugin/marketplace.json`` (git and directory sources), or the file itself.
    Read-only and for display only. A path that is not a regular file (a FIFO would block
    the read), an oversized file, or invalid JSON yields ``None``. In a checkout directory,
    a ``marketplace.json`` that resolves outside that directory (a committed symlink) is
    not read either. A plugin entry with no string ``name`` counts as ``""`` so the total
    stays the file's own count.
    """
    if not isinstance(location, str) or not location:
        return None
    path = Path(location)
    try:
        if path.is_dir():
            root = path.resolve()
            path = path / ".claude-plugin" / "marketplace.json"
            if not path.resolve().is_relative_to(root):
                return None
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            raw = fh.read(MARKETPLACE_FILE_MAX_BYTES + 1)
    except (OSError, RuntimeError):
        # RuntimeError: Python 3.11/3.12 `resolve()` raises it on a symlink loop (3.13
        # does not). Uncaught it would turn a committed update into a 500.
        return None
    if len(raw) > MARKETPLACE_FILE_MAX_BYTES:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, list):
        return None
    return [
        p["name"] if isinstance(p, dict) and isinstance(p.get("name"), str) else ""
        for p in plugins
    ]


#: The ``marketplace list --json`` source types that have no checkout of their own: the CLI
#: reads the local directory or file in place (claude 2.1.319: ``installLocation`` is that
#: source, and ``marketplace update`` only validates it). A read before an update of one
#: already sees what the update reads, so it is no before-state (TASK-532).
LIVE_MARKETPLACE_SOURCES = frozenset({"directory", "file"})


def marketplace_plugin_names(
    binary: str, cwd: Path, name: str | None, *, run: RunFn = subprocess.run
) -> tuple[dict[str, list[str] | None], frozenset[str]]:
    """Return ``{marketplace: plugin names or None}`` for ``name`` (or every marketplace).

    Reads ``marketplace list --json`` for each ``installLocation``, then that checkout's
    ``marketplace.json`` (:func:`_read_marketplace_plugin_names`). The second value names
    the marketplaces whose source the CLI reads in place
    (:data:`LIVE_MARKETPLACE_SOURCES`). A failed list raises :class:`PluginCliError`, as
    :func:`cli_list_marketplaces` does.
    """
    out: dict[str, list[str] | None] = {}
    live: set[str] = set()
    for entry in _list_marketplaces_raw(binary, cwd, run=run):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        if name is not None and entry["name"] != name:
            continue
        out[entry["name"]] = _read_marketplace_plugin_names(entry.get("installLocation"))
        if entry.get("source") in LIVE_MARKETPLACE_SOURCES:
            live.add(entry["name"])
    return out, frozenset(live)


def _plugins_text(count: int) -> str:
    """Return ``1 plugin`` / ``N plugins``."""
    return f"{count} plugin" if count == 1 else f"{count} plugins"


def _names_text(names: list[str]) -> str:
    """Return up to :data:`_NAMES_SHOWN` names, each masked, then cut, then ``and N more``.

    Masked BEFORE the cut: a cut inside a credential URL's ``user:pass@`` drops the ``@``
    the line redaction keys on, and the rest of the secret would show unmasked.
    """
    masked = [cw.redact_secret_lines(n) for n in names]
    shown = [n if len(n) <= _NAME_SHOWN_MAX else n[:_NAME_SHOWN_MAX] + "…" for n in masked]
    text = ", ".join(shown[:_NAMES_SHOWN])
    if len(shown) > _NAMES_SHOWN:
        text += f" and {len(shown) - _NAMES_SHOWN} more"
    return text


def describe_marketplace_update(
    name: str | None,
    before: dict[str, list[str] | None],
    after: dict[str, list[str] | None] | None,
    *,
    live: frozenset[str],
) -> str:
    """Return what a ``marketplace update`` did, from plugin-name reads before and after it.

    One sentence group per marketplace: the plugin list did not change (with its count),
    or it now offers N plugins with the names added and removed. A side that could not be
    read (``None``, or ``after`` as a whole when the read after the refresh failed) says so
    instead of claiming a result. A marketplace in ``live`` (its source is read in place,
    :data:`LIVE_MARKETPLACE_SOURCES`) has no before-state, so it gets only its current
    count, never "did not change" (TASK-532). The message is masked line by line before it
    leaves.
    """
    targets = [name] if name is not None else sorted(set(before) | set(after or {}))
    if not targets:
        return "Refreshed the marketplaces. No marketplace is listed."
    parts: list[str] = []
    for target in targets:
        old = before.get(target)
        new = after.get(target) if after is not None else None
        if new is not None and target in live:
            parts.append(
                f"Refreshed marketplace {target}. It offers {_plugins_text(len(new))}. "
                "Claude Code reads its local source in place, so Clauster has no earlier "
                "list to compare."
            )
        elif old is None or new is None:
            parts.append(
                f"Refreshed marketplace {target}. Clauster could not read its plugin list, "
                "so it cannot tell whether the list changed."
            )
        elif sorted(old) == sorted(new):
            parts.append(
                f"Refreshed marketplace {target}. Its plugin list did not change "
                f"({_plugins_text(len(new))})."
            )
        else:
            text = (
                f"Refreshed marketplace {target}. It now offers {_plugins_text(len(new))} "
                f"(before: {len(old)})."
            )
            added = sorted(set(new) - set(old) - {""})
            removed = sorted(set(old) - set(new) - {""})
            if added:
                text += f" Added: {_names_text(added)}."
            if removed:
                text += f" Removed: {_names_text(removed)}."
            parts.append(text)
    return cw.redact_secret_lines(" ".join(parts))


# ---------------------------------------------------------------------------
# Direct (non-CLI) reads: enabledPlugins map, per scope's settings.json
# ---------------------------------------------------------------------------

#: The ``settings.json`` key holding the enable/disable map (see the module
#: docstring: a single ``{"<plugin>@<marketplace>": true|false}`` map — there is
#: no separate ``disabledPlugins`` list in practice).
ENABLED_PLUGINS_KEY = "enabledPlugins"


def _read_enabled_plugins(path: Path) -> dict[str, bool]:
    """Return the ``enabledPlugins`` map from a settings file at ``path``.

    Filtered to ``str`` -> ``bool`` entries (hence the declared return type); a
    non-dict value yields ``{}``. Direct file read (no spawn) — mirrors the MCP
    surface's "file read for display" doctrine. No secret ever lives in this map
    (plugin ids only), so unlike the MCP server maps this needs no redaction. A path
    that is not a readable file raises :class:`~clauster.config_write.ConfigWriteError`
    (see :func:`~clauster.config_write.read_config_bytes`).
    """
    data = cw.load_settings_json_obj(cw.read_config_bytes(path))
    enabled = data.get(ENABLED_PLUGINS_KEY)
    if not isinstance(enabled, dict):
        return {}
    return {k: v for k, v in enabled.items() if isinstance(k, str) and isinstance(v, bool)}


def read_project_enabled_plugins(project_dir: Path) -> dict[str, bool]:
    """Return the project-scope ``enabledPlugins`` map (``<project>/.claude/settings.json``)."""
    return _read_enabled_plugins(cw.project_settings_path(project_dir))


def read_project_local_enabled_plugins(project_dir: Path) -> dict[str, bool]:
    """Return the local-scope ``enabledPlugins`` map (``.claude/settings.local.json``)."""
    return _read_enabled_plugins(cw.project_local_settings_path(project_dir))


def read_user_enabled_plugins(settings_json: Path) -> dict[str, bool]:
    """Return the user-scope ``enabledPlugins`` map (``~/.claude/settings.json``)."""
    return _read_enabled_plugins(settings_json)


# ---------------------------------------------------------------------------
# Direct (non-CLI) reads: extraKnownMarketplaces map, per scope's settings.json
# ---------------------------------------------------------------------------

#: The ``settings.json`` key declaring marketplaces at a given scope.
EXTRA_MARKETPLACES_KEY = "extraKnownMarketplaces"

#: The alias Claude Code 2.1.232+ reads exactly as :data:`EXTRA_MARKETPLACES_KEY`. When
#: one file sets both spellings, Claude Code uses the canonical key and ignores the alias
#: (settings-reference, "Marketplace key aliases").
EXTRA_MARKETPLACES_ALIAS_KEY = "additionalMarketplaces"


def _read_declared_marketplaces(path: Path) -> dict[str, Any]:
    """Return the redacted ``extraKnownMarketplaces`` map from a settings file at ``path``.

    Direct file read (no spawn) — the *declaration* side (which scope's
    settings.json names which marketplace), as distinct from
    :func:`cli_list_marketplaces`'s CLI-merged, cwd-independent pool view (which
    also carries resolved ``installLocation`` the declaration alone doesn't have).
    Reads the ``additionalMarketplaces`` alias only when the canonical key is absent,
    matching Claude Code's own precedence. Every source type passes through as it is
    stored (``github``, ``git``, ``url``, ``file``, ``directory``, ``settings``, and any
    type a newer CLI adds); nothing is fetched or resolved. The map is redacted
    (:func:`redact_plugin_display`), because a ``url`` source's ``headers`` and
    ``headersHelper`` hold credentials. A path that is not a readable file raises
    :class:`~clauster.config_write.ConfigWriteError` (see
    :func:`~clauster.config_write.read_config_bytes`).
    """
    data = cw.load_settings_json_obj(cw.read_config_bytes(path))
    key = (
        EXTRA_MARKETPLACES_KEY if EXTRA_MARKETPLACES_KEY in data else EXTRA_MARKETPLACES_ALIAS_KEY
    )
    marketplaces = data.get(key)
    return redact_named_map(marketplaces) if isinstance(marketplaces, dict) else {}


def read_project_marketplaces(project_dir: Path) -> dict[str, Any]:
    """Return the project-scope declared marketplaces (``<project>/.claude/settings.json``)."""
    return _read_declared_marketplaces(cw.project_settings_path(project_dir))


def read_project_local_marketplaces(project_dir: Path) -> dict[str, Any]:
    """Return the local-scope declared marketplaces (``.claude/settings.local.json``)."""
    return _read_declared_marketplaces(cw.project_local_settings_path(project_dir))


def read_user_marketplaces(settings_json: Path) -> dict[str, Any]:
    """Return the user-scope declared marketplaces (``~/.claude/settings.json``)."""
    return _read_declared_marketplaces(settings_json)


# ---------------------------------------------------------------------------
# The per-install STRONG confirm (#771's "type-the-name confirm PER INSTALL")
# ---------------------------------------------------------------------------


def require_install_confirm(plugin_id: str, supplied: object) -> None:
    """Reject (400) unless ``supplied`` exactly equals ``plugin_id``.

    The design doc's resolution for #771 (2026-06-29): installing a plugin
    introduces new executable code from the browser, so it gets a confirm
    *stronger* than the ordinary scope confirm (:func:`clauster.config_write.
    require_confirm`, which the install route also runs first) — the operator
    must retype the **plugin id being installed**, not just the project/scope
    name. This makes the confirm specific to *what* is being introduced, not just
    *where*: a confirm typed for installing ``a@market`` can never be replayed to
    silently install ``b@market`` instead. The dashboard's install form asks the
    operator to retype the id. Its install from a marketplace's list (TASK-516, by the
    maintainer's decision of 2026-11-07) sends the listed id here instead, so that the
    row the operator clicked names the id: :func:`cli_list_offered_plugins` keeps only
    rows whose label is the name part of their own valid id. This check is the same for
    both. Raises :class:`fastapi.HTTPException`
    directly (400) — mirroring :func:`clauster.config_write.require_confirm`'s own
    contract exactly, so the route calls it unwrapped: after the base scope confirm
    and the op/plugin-id shape + candidate checks, and before any CLI dispatch.
    """
    if not isinstance(supplied, str) or supplied != plugin_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"install confirmation text must be the plugin id being installed: {plugin_id!r}"
            ),
        )
