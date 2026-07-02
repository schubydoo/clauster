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
  process's environment, never as a CLI argument. A **stdio server's own env-map
  secret** has no such escape hatch in the upstream CLI (``-e``/``add-json``'s JSON
  argument are both ordinary argv), so :func:`entry_has_secret` routes any entry
  carrying a literal secret value away from this module entirely, into
  :mod:`clauster.config_write_mcp`'s direct (non-spawning) writers instead — see
  that module's "#769 additions" docstring section for the reconciliation this was
  asked to make with the design doc's "hybrid" strategy.

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


def entry_has_secret(entry: dict[str, Any]) -> bool:
    """Whether MCP server ``entry`` carries a literal secret-shaped value anywhere.

    Reuses the Foundation's structural secret detector
    (:func:`clauster.config_write.redact_secrets`): if masking the entry changes it,
    a live secret is present somewhere in it (an ``env``/``headers`` value, or a
    token-bearing ``url``). A caller routes such an entry to
    :mod:`clauster.config_write_mcp`'s direct writer instead of this module's CLI
    add — see the module docstring for why.
    """
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
    :class:`McpCliError` if the binary can't even be spawned (missing binary,
    timeout); a nonzero exit is returned to the caller to classify (see
    :func:`_raise_for_failure`), since "already exists" / "not found" are expected,
    handled outcomes, not this function's problem to interpret.
    """
    resolved = claude_cli.resolve_binary(binary)
    argv = [resolved, "mcp", *args]
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
    except (OSError, subprocess.SubprocessError) as exc:
        raise McpCliError(f"claude mcp {args[0] if args else ''} failed to run: {exc}") from exc


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

    ``entry`` must already be a structurally-valid, **non-secret-bearing** server
    entry (see :func:`entry_has_secret`) — refuses (``ValueError``, a programming
    error, not a 4xx) otherwise, since a secret-bearing entry must never reach this
    function's argv. Re-validates structurally regardless (belt + suspenders: the
    route layer validates first, but this is the last line before a spawn).
    ``client_secret``, when given, is passed via the ``MCP_CLIENT_SECRET`` child-env
    var (never argv, never a TTY prompt) alongside ``--client-secret`` — the OAuth
    client-secret path for a remote server, the one secret ``claude mcp`` itself
    knows how to take out-of-argv. Raises
    :class:`clauster.config_write_mcp.ServerExistsError` if the name is already
    taken in that scope; :class:`McpCliError` on any other nonzero exit.
    """
    if entry_has_secret(entry):
        raise ValueError(
            f"cli_add_server refuses to add {name!r}: entry carries a literal secret value"
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
    run: RunFn = subprocess.run,
) -> None:
    """Edit one MCP server: CLI remove (ignoring "not found") + CLI re-add.

    There is no native ``claude mcp edit`` — confirmed absent from ``claude mcp
    --help`` — so this is the design doc's own "Parity verdict" reconciliation:
    edit = read-then-remove+add. Only used when ``entry`` carries no literal secret
    (see :func:`entry_has_secret`); the route layer routes a secret-bearing edit to
    :func:`clauster.config_write_mcp.write_project_server_entry` (et al.) instead,
    which performs the equivalent merge without ever spawning anything.
    """
    cli_remove_server(binary, project_dir, name, scope, ignore_missing=True, run=run)
    cli_add_server(binary, project_dir, name, entry, scope, client_secret=client_secret, run=run)


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
