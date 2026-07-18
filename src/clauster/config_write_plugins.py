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

Verified against a live ``claude`` 2.1.198 (``claude plugin --help`` / every
subcommand's own ``--help``, plus a throwaway ``HOME``-isolated probe — never the
real account) rather than assumed from memory or docs alone, per #771. Cross-checked
against the published docs (``code.claude.com/docs/en/plugins-reference``,
``.../plugin-marketplaces``, ``.../settings``) via context7:

* ``claude plugin enable|disable <plugin> --scope user|project|local`` — the ONLY
  native verbs for enable/disable (unlike MCP server approvals, which had no CLI
  verb and needed a direct settings write). Writes ``enabledPlugins`` in the
  target scope's ``settings.json`` as ``{"<plugin>@<marketplace>": true|false}`` —
  a single map of booleans, confirmed live and by the docs' own examples. There is
  **no** separate ``disabledPlugins`` list in practice (unlike the MCP
  ``enabledMcpjsonServers``/``disabledMcpjsonServers`` pair) — a disabled plugin is
  simply ``false`` in the same map. (:mod:`clauster.config_write_settings`'s
  ``OWNED_KEYS`` carves out both key names defensively; this surface only ever
  produces/reads ``enabledPlugins``.) Enable/disable does **not** check whether the
  named plugin is actually installed — it blind-writes the map entry (confirmed:
  enabling a nonexistent id exits 0) — so it is exactly the settings-write
  operation the design doc describes, just reached via a real CLI verb instead of
  a direct file write.
* ``claude plugin install <plugin> --scope user|project|local`` — succeeds with
  **no** confirmation prompt even with stdin closed (confirmed: a non-TTY install
  of an already-added, trusted local marketplace's plugin exits 0 immediately);
  there is **no** ``-y``/``--yes`` option on ``install`` at all (confirmed:
  passing either is "unknown option"). Re-installing an already-installed id is a
  clean idempotent success ("already installed"), not an error. ``--config
  key=value`` (repeatable) is out of scope for this pass (not named in #771).
* ``claude plugin uninstall|remove <plugin> --scope ... [--keep-data] [--prune
  [-y|--yes]]`` — ``-y``/``--yes`` is real here (and on bare ``prune``), gating
  only the ``--prune`` confirmation; it is **required whenever stdin/stdout is
  not a TTY**, so this module always appends it alongside ``--prune`` (never
  passes ``--prune`` without ``-y``) — a spawned child with ``stdin=DEVNULL`` can
  never answer an interactive prompt, so omitting it would hang a real
  dependency-pruning uninstall until the timeout. Uninstalling a not-installed id
  is an error ("not found in installed plugins").
* ``claude plugin update <plugin> --scope user|project|local|managed`` — no
  ``-y``. ``managed`` is excluded here (out of clauster's scope model, see the
  design doc's canonical scope table — Managed is MDM/system, never
  clauster-writable). Updating a not-installed id is an error ("not found").
* ``claude plugin list --json`` / ``claude plugin details <name>`` — CLI-only
  reads: cache path, install timestamp, and (per-invocation-cwd) live
  ``enabled``/``projectPath`` state have no settings.json equivalent to read
  directly, unlike the MCP surface's "file read beats CLI for display" doctrine
  (which was about avoiding an extra spawn + about redaction, neither of which
  applies to plugin metadata — there is nothing secret in a plugin id or cache
  path). Confirmed live: the ``enabled`` field in ``list --json`` genuinely
  depends on invocation cwd (a project-scope entry reads ``enabled: true`` only
  from *that* project's directory, ``false`` elsewhere) — the cwd this module
  spawns from is not cosmetic.
* ``claude plugin marketplace add <source> --scope ... [--sparse ...]`` — source
  is ``owner/repo``, a URL, or a path; ``--sparse`` is out of scope for this pass
  (monorepo checkout limiting, not named in #771). Re-adding an already-known name
  from the same source is an idempotent success ("already on disk"), never an
  error — confirmed live, so this module treats a nonzero exit here as a genuine
  failure, never something to swallow.
* ``claude plugin marketplace remove|rm <name> [--scope ...]`` — ``--scope`` is
  **optional**; omitting it removes the declaration from *every* scope it
  appears in (confirmed by ``--help``). This module never omits it: every mutation
  in this app is scoped to the one settings file the caller confirmed against, so
  ``--scope`` is always passed explicitly here, matching the confirm token's own
  scope. Removing an unknown name is an error ("not found").
* ``claude plugin marketplace list --json`` / ``claude plugin marketplace update
  [name]`` — **neither takes ``--scope``**: confirmed live that ``list`` reflects
  ``~/.claude/plugins/known_marketplaces.json``, a single merged pool independent
  of invocation cwd (a marketplace declared at *any* scope, from *any* project,
  shows up from every cwd) — unlike plugin ``list``'s cwd-dependent ``enabled``
  field. This module still routes both through the ordinary scope/project/confirm
  plumbing (for capability gating + which settings file's confirm token applies +
  a stable cwd to spawn from) even though the resulting argv never carries
  ``--scope`` — consistent with every other route in this app rather than a
  special-cased "always user scope" carve-out.

Every spawn here is validate-before-spawn (:mod:`clauster.claude_cli`, list-argv,
never ``shell=True``, ``stdin=DEVNULL`` so a surprise interactive prompt fails
fast rather than hanging the request) — the same discipline
:mod:`clauster.config_write_mcp_cli` uses. Unlike that module, a plugin/marketplace
identifier is never secret-shaped, so there is no "must route around the CLI"
predicate here — every mutation goes through the CLI, always.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import claude_cli, procutil
from . import config_write as cw

#: Signature every injected subprocess runner must match (``subprocess.run``'s).
RunFn = Callable[..., subprocess.CompletedProcess]

#: A `claude plugin` invocation may need to fetch/checkout a git-backed marketplace
#: (`marketplace add`/`update`) or resolve a plugin's dependency graph (`install`),
#: so this is a little more generous than the MCP CLI's 30s local-JSON-edit bound.
DEFAULT_TIMEOUT_SECONDS = 60.0

_NOT_FOUND_RE = re.compile(r"not found|not installed", re.IGNORECASE)

#: Identifier charset for a single name segment (plugin name, marketplace name):
#: no leading ``-`` (would be parsed as a CLI option by ``claude``'s argument
#: parser — verified live: an unescaped ``-evil`` positional is rejected as
#: "unknown option"), conservative identifier chars only. Mirrors
#: ``config_write_mcp._SERVER_NAME_RE`` exactly (same threat, same fix).
_NAME_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")

#: A marketplace **source** (``owner/repo``, a URL, or a filesystem path) has a much
#: wider legal charset than an identifier (``/``, ``:``, ``.``, ``~`` are all
#: legitimate), so this only rejects the arg-injection shape (a leading ``-``,
#: which would be parsed as an option) and raw control characters — never a
#: charset allowlist that would reject a real URL/path.
_SOURCE_LEADING_DASH_RE = re.compile(r"^-")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


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
    if not _NAME_SEGMENT_RE.match(value):
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
    """
    if not isinstance(value, str) or not value:
        raise cw.InvalidCandidateError("marketplace source must be a non-empty string")
    if _SOURCE_LEADING_DASH_RE.match(value):
        raise cw.InvalidCandidateError(
            f"marketplace source {value!r} must not start with '-' "
            "(would be parsed as a CLI option)"
        )
    if _CONTROL_CHAR_RE.search(value):
        raise cw.InvalidCandidateError(
            f"marketplace source {value!r} contains a control character"
        )


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
    cw.record_cli_argv("plugin", args)  # #958 P6: capture the redacted argv for the audit line
    try:
        return run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
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


def _raise_for_failure(op: str, ident: str, proc: subprocess.CompletedProcess) -> None:
    """Classify a nonzero ``claude plugin`` exit into a typed error, or return on success.

    ``stdout``/``stderr`` are run through :func:`clauster.config_write.
    redact_secret_lines` before ever appearing in an exception message — defense
    in depth (nothing here is expected to be secret-shaped, but the CLI's own
    stdout could in principle echo back something from an existing config).

    A "not found"/"not installed" message maps to the appropriate typed 404 — but
    **only for the verbs where "not found" means the named entity is genuinely
    absent** (uninstall/update/enable/disable of an absent id, install of a plugin
    absent from its marketplace, marketplace remove/update of an unknown name).
    An ``add`` (``marketplace-add``) is deliberately **excluded**: its ``source``
    is a URL/path/repo the CLI must fetch, so a failed add commonly reports
    ``fatal: repository '…' not found`` — a git/network *failure*, not an absent
    clauster-side entity. Classifying that as 404 would be misleading, so an add
    failure always surfaces as a generic :class:`PluginCliError` (→ 400), with the
    redacted stderr kept in the detail so the operator can still diagnose it.
    Every other nonzero exit is likewise a generic 400.
    """
    if proc.returncode == 0:
        return
    detail = cw.redact_secret_lines((proc.stderr or proc.stdout or "").strip())
    if _NOT_FOUND_RE.search(detail) and not op.endswith("add"):
        if op.startswith("marketplace"):
            raise MarketplaceNotFoundError(detail)
        raise PluginNotFoundError(detail)
    raise PluginCliError(f"claude plugin {op} {ident!r} exited {proc.returncode}: {detail}")


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
    ``enabledPlugins`` map entry (verified live), so this simply mirrors that.
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
    it only re-validates the identifier shape and spawns. No ``-y``/interactive
    handling is needed: confirmed live that ``install`` never prompts, even
    non-interactively, for a plugin from an already-trusted (operator-added)
    marketplace.
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
    :class:`PluginNotFoundError` (→ 404).
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
    first, exactly like every other scoped read in this app.
    """
    proc = _run(binary, ["list", "--json"], cwd=cwd, run=run)
    _raise_for_failure("list", "", proc)
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise PluginCliError(f"claude plugin list returned invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise PluginCliError("claude plugin list returned a non-list JSON value")
    return data


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
    the result.
    """
    proc = _run(binary, ["marketplace", "list", "--json"], cwd=cwd, run=run)
    _raise_for_failure("marketplace-list", "", proc)
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise PluginCliError(
            f"claude plugin marketplace list returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise PluginCliError("claude plugin marketplace list returned a non-list JSON value")
    return data


# ---------------------------------------------------------------------------
# Direct (non-CLI) reads: enabledPlugins map, per scope's settings.json
# ---------------------------------------------------------------------------

#: The ``settings.json`` key holding the enable/disable map (see the module
#: docstring: a single ``{"<plugin>@<marketplace>": true|false}`` map — there is
#: no separate ``disabledPlugins`` list in practice).
ENABLED_PLUGINS_KEY = "enabledPlugins"


def _read_enabled_plugins(path: Path) -> dict[str, bool]:
    """Return the raw ``enabledPlugins`` map from a settings file at ``path``.

    Direct file read (no spawn) — mirrors the MCP surface's "file read for
    display" doctrine. No secret ever lives in this map (plugin ids only), so
    unlike the MCP server maps this needs no redaction.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = cw.load_settings_json_obj(raw)
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


def _read_declared_marketplaces(path: Path) -> dict[str, Any]:
    """Return the raw ``extraKnownMarketplaces`` map from a settings file at ``path``.

    Direct file read (no spawn) — the *declaration* side (which scope's
    settings.json names which marketplace), as distinct from
    :func:`cli_list_marketplaces`'s CLI-merged, cwd-independent pool view (which
    also carries resolved ``installLocation`` the declaration alone doesn't have).
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = cw.load_settings_json_obj(raw)
    marketplaces = data.get(EXTRA_MARKETPLACES_KEY)
    return marketplaces if isinstance(marketplaces, dict) else {}


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
    silently install ``b@market`` instead. Raises :class:`fastapi.HTTPException`
    directly (400) — mirroring :func:`clauster.config_write.require_confirm`'s own
    contract exactly, so the route calls both unwrapped, before any
    ``ConfigWriteError``-catching try/except, and before any other validation.
    """
    if not isinstance(supplied, str) or supplied != plugin_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"install confirmation text must be the plugin id being installed: {plugin_id!r}"
            ),
        )
