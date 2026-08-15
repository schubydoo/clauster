"""CLI-driven MCP add/remove/edit (#769) — the "hybrid" write half of #766/#688.

The config-management design (``scratch/config-management-expansion-design-2026-06-29.md``)
locks MCP as a **CLI-driven** surface: ``claude mcp`` owns approval flow, OAuth, and
the one-true-parser for its own config files, so clauster drives *mutations*
through the CLI rather than hand-writing JSON, and keeps *display* on the existing
redacted file readers (:mod:`clauster.config_write_mcp`). This module is the CLI
half; :mod:`clauster.config_write_mcp` is the file half both this module and the
route layer call back into.

Verified against a live ``claude`` 2.1.198 (``claude mcp --help`` / ``add --help`` /
``add-json --help`` / ``remove --help`` / ``reset-project-choices --help``, plus a
throwaway ``HOME``/cwd probe — never the real account) rather than assumed from
memory or docs alone, per #769:

* ``claude mcp add-json <name> <json> --scope local|user|project [--client-secret]``
  — the entry shape is the *same* JSON object :func:`clauster.config_write_mcp.
  validate_mcp_servers` already validates, so ``add-json`` (not ``add``, which wants
  a split ``command``/``args``) is the one CLI verb this module needs for "add".
  Fails (exit 1, stderr ``"... already exists ..."``) if the name is already taken
  in that scope — mapped to :class:`clauster.config_write_mcp.ServerExistsError`.
* ``claude mcp remove <name> --scope local|user|project`` — fails (exit 1, stderr
  ``"No MCP server named ..."``) if absent — mapped to
  :class:`clauster.config_write_mcp.ServerNotFoundError`. There is **no** ``edit``
  verb (confirmed absent from ``--help``): edit = remove (ignoring "not found") +
  re-add, per the design doc's own "Parity verdict".
* ``claude mcp reset-project-choices`` — clears the current *cwd's* project
  ``.mcp.json`` approve/reject lists; no ``--scope``/name argument.
* **Secrets never touch argv or a TTY.** ``add-json``'s ``--client-secret`` flag
  reads Claude Code's own ``MCP_CLIENT_SECRET`` env var instead of prompting
  (confirmed: with the env var set and stdin closed, the OAuth flow does not hang)
  — used for a remote server's OAuth client secret, passed here via the child
  process's environment, never as a CLI argument. A server's own ``env``/``headers``
  values (and a token-bearing ``url`` or stdio ``args``) have **no** such escape hatch
  in the upstream CLI (``-e`` and ``add-json``'s JSON argument are both ordinary argv),
  so :func:`entry_needs_direct_write` routes any entry carrying an inline ``env`` or
  ``headers`` value — a ``url`` that is not provably bare (any userinfo, query,
  **fragment**, or **path segment** could carry a credential: ``user:pass@host``,
  ``?api_key=…``, ``#api_key=…``, ``/mcp/sk-live-…/sse``) or that
  :func:`urllib.parse.urlsplit` cannot parse (fail-closed), or a non-empty stdio
  ``args`` list — away from this module entirely, into
  :mod:`clauster.config_write_mcp`'s direct (non-spawning) writers instead. The
  predicate deliberately errs toward "keep it off the CLI": key-name-based redaction
  (:func:`clauster.config_write.redact_secrets`) cannot see a real secret stored under
  a benign key (``{"env": {"DEPLOY_KEY": "AKIA…"}}``) or a token in a ``url`` query/
  fragment, so we treat *any* non-empty ``env``/``headers`` value — and any of those
  ``url``/``args`` shapes — as potentially secret rather than trusting the key name.
  The direct writer yields the same ``mcpServers`` file state as the CLI would, minus
  the CLI's cosmetic ``"args": []`` normalization. See that module's "#769 additions"
  docstring for the reconciliation with the design doc's "hybrid" strategy.

  **Path-embedded credentials — closed (#1074).** A secret in the ``url`` *path*
  (``https://host/mcp/sk-live-…/sse``) once reached the CLI argv because only the
  query/userinfo/fragment were inspected. :func:`entry_needs_direct_write` now applies a
  **field allowlist**: a ``url`` is CLI-eligible only when it is provably bare
  (``scheme://host[:port]`` — no path segment, query, fragment, or userinfo; see
  :func:`_url_is_bare`), so every path-bearing ``url`` routes to the direct writer. The
  cost is that a legitimate path-only ``url`` also skips the CLI (harmless — equivalent
  on-disk state); the one CLI-only capability, an OAuth ``client_secret`` via
  ``MCP_CLIENT_SECRET``, still works for a bare ``url``, and the route returns a clear 422
  for a path-bearing OAuth ``url``.

Every spawn here is validate-before-spawn: the binary is resolved to an absolute
path (:func:`clauster.claude_cli.resolve_binary`), argv is always a list (never
``shell=True``), stdin is ``DEVNULL`` (a hung interactive prompt must fail loud, not
hang the request), and the child env is built through
:func:`clauster.procutil.child_env` (which already strips clauster's own secrets)
plus at most one added var (``MCP_CLIENT_SECRET``) — never appended to argv.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import claude_cli, procutil
from . import config_write as cw
from . import config_write_mcp as mcp

#: Signature every injected subprocess runner must match (``subprocess.run``'s).
RunFn = Callable[..., subprocess.CompletedProcess]

#: A `claude mcp` invocation should be near-instant (local JSON file edit); bound it
#: generously so a hung child (unexpected network/OAuth probe) can't wedge a request.
DEFAULT_TIMEOUT_SECONDS = 30.0

_NOT_FOUND_RE = re.compile(r"no mcp server named", re.IGNORECASE)
_ALREADY_EXISTS_RE = re.compile(r"already exists", re.IGNORECASE)


class McpCliError(cw.ConfigWriteError):
    """A ``claude mcp`` invocation failed unexpectedly (→ 400; not a shape/conflict error)."""


def _has_nonempty_value(block: Any) -> bool:
    """Whether ``block`` is a dict holding at least one non-empty string value."""
    return isinstance(block, dict) and any(isinstance(v, str) and v for v in block.values())


def _url_is_bare(url: str) -> bool:
    """Whether ``url`` is provably ``scheme://host[:port]`` only — safe to reach argv.

    The field-allowlist half of :func:`entry_needs_direct_write` (#1074). An MCP ``url``
    reaches ``claude mcp add-json`` as an ordinary argv token (``ps`` / ``/proc/<pid>/
    cmdline``-visible), so ANY component that could hide a credential makes it unsafe for
    the CLI: userinfo (``user[:pass]@host``), query (``?api_key=…``), fragment
    (``#api_key=…``), **or a path segment** (``/mcp/sk-live-…/sse``). Rather than ask the
    undecidable "does this value look secret?" — a credential path segment is
    indistinguishable from a benign ``/v1``/``/sse`` without the key-name/shape heuristic
    this module deliberately avoids — this asks the decidable question "is the shape
    provably safe?": a ``url`` is bare **only** when it carries nothing but a scheme, host,
    and optional port. A url :func:`urllib.parse.urlsplit` cannot parse is treated as not
    bare (fail closed).

    Closes the path-embedded-credential residual (#1074): before, a secret in the ``url``
    path still reached argv because only query/userinfo/fragment were inspected. The cost
    is that a legitimate path-only ``url`` (``https://host/sse``) also skips the CLI —
    harmless, the direct writer produces equivalent on-disk state. The one CLI-only
    capability (deliver an OAuth ``client_secret`` via ``MCP_CLIENT_SECRET``) still works
    for a bare ``url``; a path-bearing OAuth ``url`` takes the route's documented 422.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False  # unparseable -> not provably safe, keep it off the CLI
    return not (
        parts.path.strip("/") or parts.query or parts.fragment or parts.username or parts.password
    )


def entry_needs_direct_write(entry: dict[str, Any]) -> bool:
    """Whether ``entry`` must be written by the direct file writer, never the CLI.

    Returns True when the entry carries anything that could place a **secret in
    ``claude mcp add-json``'s argv** (visible via ``ps``/``/proc``):

    * any non-empty ``env`` value (stdio or remote), **or**
    * any non-empty ``headers`` value (remote), **or**
    * a ``url`` that is not provably bare — anything beyond ``scheme://host[:port]``: a
      path segment, query, fragment, or userinfo, or a ``url`` that cannot be parsed at
      all (see :func:`_url_is_bare`), **or**
    * a non-empty stdio ``args`` list (a token can hide as ``["--api-key", "sk-…"]``),
      **or**
    * a token-bearing / interpolated value the Foundation's structural detector
      catches anywhere else (e.g. a ``scheme://user@host`` ``url``, a ``${VAR}``).

    This is a **field allowlist** (#1074), not a "does this look secret?" scan: the CLI
    ``add-json`` path is reached only when every field is provably safe to serialize onto
    argv — no ``env``/``headers``, no ``args``, and a bare ``url`` (scheme+host+port). That
    is decidable and does not degrade as new URL shapes appear, unlike key-name-based
    redaction (:func:`clauster.config_write.redact_secrets`), which can only spot a secret
    by a KEY (``token``/``secret``/… — its own, or an ancestor's, since a credential-shaped
    key marks its whole subtree) and so misses a real secret under a benign key
    (``{"DEPLOY_KEY": "AKIA…"}``, ``{"GH_PAT": "ghp_…"}``, ``{"X-Custom": "Bearer sk-…"}``),
    a token in a ``url`` query/fragment/**path** (``/mcp/sk-live-…/sse``), or an ``args``
    element. The OAuth ``--client-secret`` case still uses the CLI (its secret rides
    ``MCP_CLIENT_SECRET`` in the child env, not argv) for a bare ``url``. The cost is only
    that a genuinely non-secret ``env``/``url``/``args`` also skips the CLI — harmless,
    since the direct writer produces equivalent file state.
    """
    if _has_nonempty_value(entry.get("env")) or _has_nonempty_value(entry.get("headers")):
        return True
    url = entry.get("url")
    if isinstance(url, str) and url and not _url_is_bare(url):
        return True
    args = entry.get("args")
    if isinstance(args, list) and args:
        return True
    return cw.redact_secrets(entry) != entry


def _run(
    binary: str,
    args: list[str],
    *,
    cwd: Path,
    env_extra: dict[str, str] | None = None,
    run: RunFn = subprocess.run,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    """Validate-before-spawn ``claude mcp <args>``: resolve binary, list-argv, no shell.

    ``cwd`` is the project directory for ``local``/``project`` scope (the CLI keys
    both by the resolved cwd) — an arbitrary safe directory for ``user`` scope,
    where the CLI ignores cwd. ``env_extra`` (e.g. ``MCP_CLIENT_SECRET``) is merged
    into the child environment via :func:`clauster.procutil.child_env`, never
    appended to ``args``. ``stdin`` is closed (``DEVNULL``): a CLI that falls back to
    an interactive prompt must fail fast, not hang the request. Raises
    :class:`McpCliError` when the spawn itself fails (timeout, ``OSError`` from exec); a
    ``claude`` binary missing from PATH raises
    :class:`clauster.claude_cli.ClaudeNotFound` from
    :func:`~clauster.claude_cli.resolve_binary` before any spawn and propagates unchanged.
    A nonzero exit is returned to the caller to classify (see :func:`_raise_for_failure`),
    since "already exists" / "not found" are expected, handled outcomes, not this
    function's problem to interpret.

    **Never lets the argv leak into the error text.** ``TimeoutExpired.__str__``
    embeds the full command — including ``json.dumps(entry)``, which for an OAuth add
    could carry a header/env value — so the timeout branch builds its message from the
    *verb only* (``args[0]``) and the timeout seconds, never ``str(exc)``. The generic
    spawn-failure branch (an ``OSError`` such as a non-executable binary) prints only a
    redacted ``str(exc)`` (the binary path, not the argv) as defense in depth.
    """
    verb = args[0] if args else ""
    resolved = claude_cli.resolve_binary(binary)
    argv = [resolved, "mcp", *args]
    cw.record_cli_argv("mcp", args)  # #958 P6: capture the redacted argv for the audit line
    try:
        return run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=procutil.child_env(env_extra),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # exc's repr contains the whole argv (incl. the json entry) — build from the
        # verb + timeout only, never str(exc).
        raise McpCliError(f"claude mcp {verb} timed out after {exc.timeout}s") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise McpCliError(
            f"claude mcp {verb} failed to run: {cw.redact_secret_lines(str(exc))}"
        ) from exc


def _raise_for_failure(op: str, name: str, proc: subprocess.CompletedProcess) -> None:
    """Classify a nonzero ``claude mcp`` exit into a typed error, or return on success.

    ``stdout``/``stderr`` are run through :func:`clauster.config_write.
    redact_secret_lines` before ever appearing in an exception message (and hence a
    log or an HTTP error body) — defense in depth, since this module never puts a
    secret in argv in the first place, but the CLI's own stdout could in principle
    echo back something secret-shaped from an existing config it read.
    """
    if proc.returncode == 0:
        return
    detail = cw.redact_secret_lines((proc.stderr or proc.stdout or "").strip())
    if op == "remove" and _NOT_FOUND_RE.search(detail):
        raise mcp.ServerNotFoundError(detail)
    if op == "add-json" and _ALREADY_EXISTS_RE.search(detail):
        raise mcp.ServerExistsError(detail)
    raise McpCliError(f"claude mcp {op} {name!r} exited {proc.returncode}: {detail}")


def cli_add_server(
    binary: str,
    project_dir: Path,
    name: str,
    entry: dict[str, Any],
    scope: cw.Scope,
    *,
    client_secret: str | None = None,
    run: RunFn = subprocess.run,
) -> None:
    """Add one MCP server via ``claude mcp add-json ... --scope <scope>``.

    ``entry`` must already be a structurally-valid server entry that does **not**
    need the direct writer (see :func:`entry_needs_direct_write`) — refuses
    (``ValueError``, a programming error, not a 4xx) otherwise, since an entry with an
    inline ``env``/``headers`` value must never reach this function's argv. Re-validates
    structurally regardless (belt + suspenders: the route layer validates first, but
    this is the last line before a spawn). ``client_secret``, when given, is passed via
    the ``MCP_CLIENT_SECRET`` child-env var (never argv, never a TTY prompt) alongside
    ``--client-secret`` — the OAuth client-secret path for a remote server, the one
    secret ``claude mcp`` itself knows how to take out-of-argv. Raises
    :class:`clauster.config_write_mcp.ServerExistsError` if the name is already
    taken in that scope; :class:`McpCliError` on any other nonzero exit.
    """
    if entry_needs_direct_write(entry):
        raise ValueError(
            f"cli_add_server refuses to add {name!r}: entry carries an inline env/headers "
            "value, a credential-bearing url, or non-empty args that must not reach the "
            "CLI argv (route to the direct writer instead)"
        )
    cw.validate_candidate({name: entry}, mcp.validate_mcp_servers)
    args = ["add-json", name, json.dumps(entry), "--scope", scope]
    env_extra: dict[str, str] | None = None
    if client_secret is not None:
        args.append("--client-secret")
        env_extra = {"MCP_CLIENT_SECRET": client_secret}
    proc = _run(binary, args, cwd=project_dir, env_extra=env_extra, run=run)
    _raise_for_failure("add-json", name, proc)


def cli_remove_server(
    binary: str,
    project_dir: Path,
    name: str,
    scope: cw.Scope,
    *,
    ignore_missing: bool = False,
    run: RunFn = subprocess.run,
) -> None:
    """Remove one MCP server via ``claude mcp remove ... --scope <scope>``.

    ``ignore_missing=True`` swallows :class:`clauster.config_write_mcp.
    ServerNotFoundError` (the edit-remove step: a fresh "edit" of a not-yet-existing
    name has nothing to remove, and that's fine) — an explicit remove call leaves it
    ``False`` so a caller asking to remove something absent gets a clean 404.
    """
    proc = _run(binary, ["remove", name, "--scope", scope], cwd=project_dir, run=run)
    try:
        _raise_for_failure("remove", name, proc)
    except mcp.ServerNotFoundError:
        if ignore_missing:
            return
        raise


def cli_edit_server(
    binary: str,
    project_dir: Path,
    name: str,
    entry: dict[str, Any],
    scope: cw.Scope,
    *,
    client_secret: str | None = None,
    restore: Callable[[], bool] | None = None,
    run: RunFn = subprocess.run,
) -> None:
    """Edit one MCP server: CLI remove (ignoring "not found") + CLI re-add, with rollback.

    There is no native ``claude mcp edit`` — confirmed absent from ``claude mcp
    --help`` — so this is the design doc's own "Parity verdict" reconciliation:
    edit = remove+re-add. Only used when ``entry`` carries no inline ``env``/``headers``
    value (see :func:`entry_needs_direct_write`); the route layer routes such an edit to
    :func:`clauster.config_write_mcp.write_project_server_entry` (et al.) instead.

    **Data-loss guard.** remove-then-add has a window: if the re-add fails after the
    remove succeeded, the server is gone. ``restore`` (supplied by the route layer as a
    best-effort closure that writes the *previous* definition back through the direct,
    non-spawning writer — so a prior entry's real secret is never re-exposed on argv)
    is invoked on a re-add failure. It returns whether a prior definition actually
    existed and was restored, so the raised message is accurate:

    * re-add fails, ``restore`` restored a real prior ⇒ :class:`McpCliError` stating the
      edit failed **and the previous definition was restored** (no loss).
    * re-add fails, but there was **no** prior to restore (``restore`` returns False, or
      none was given) ⇒ :class:`McpCliError` stating the add failed and **no server is
      present** — there was nothing to restore, so no loss is claimed falsely.
    * re-add fails **and** ``restore`` itself raised ⇒ :class:`McpCliError` that
      **explicitly says the server was removed and could not be restored** — the loss is
      surfaced loudly, never silent-by-omission.

    Catches ``ValueError`` as well as :class:`~clauster.config_write.ConfigWriteError`
    around the re-add: :func:`cli_add_server` raises ``ValueError`` for an entry that
    should have gone to the direct writer, and if that ever fired *after* the remove had
    succeeded the restore path must still run (the route pre-checks, but this function is
    public — close the defensive gap).

    The captured ``restore`` closure must snapshot the previous entry *before* this
    call runs the remove (the route layer does so), or there is nothing left to read.
    """
    cli_remove_server(binary, project_dir, name, scope, ignore_missing=True, run=run)
    try:
        cli_add_server(
            binary, project_dir, name, entry, scope, client_secret=client_secret, run=run
        )
    except (cw.ConfigWriteError, ValueError) as exc:
        if restore is None:
            raise McpCliError(
                f"MCP server {name!r} edit failed ({exc}); it was already REMOVED and no "
                "restore of its previous definition was available — the server is now missing"
            ) from exc
        try:
            restored = restore()
        except Exception as restore_exc:  # noqa: BLE001 - best-effort; surface loss loudly
            raise McpCliError(
                f"MCP server {name!r} edit failed ({exc}); it was REMOVED and restoring its "
                f"previous definition ALSO failed ({restore_exc}) — the server is now missing"
            ) from exc
        if restored:
            raise McpCliError(
                f"MCP server {name!r} edit failed ({exc}); its previous definition was restored"
            ) from exc
        raise McpCliError(
            f"MCP server {name!r} add failed ({exc}); it had no previous definition, so no "
            "server is present (nothing to restore)"
        ) from exc


def cli_reset_project_choices(
    binary: str, project_dir: Path, *, run: RunFn = subprocess.run
) -> None:
    """Reset ``project_dir``'s ``.mcp.json`` server approvals via the CLI.

    ``claude mcp reset-project-choices`` takes no scope/name argument — it clears
    both ``enabledMcpjsonServers`` and ``disabledMcpjsonServers`` for the *cwd's*
    project, confirmed by a live probe. Unlike enable/disable itself (which has no
    CLI verb, hence :func:`clauster.config_write_mcp.write_project_approvals`'s
    direct write), this one has a real CLI verb, so the design doc's CLI-first
    strategy applies here without qualification.
    """
    proc = _run(binary, ["reset-project-choices"], cwd=project_dir, run=run)
    _raise_for_failure("reset-project-choices", "", proc)
