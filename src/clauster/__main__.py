"""Entry point: ``clauster`` / ``python -m clauster``.

Subcommands: ``run`` (default), ``hash-password``, ``hash-token``,
``hash-metrics-token``, ``doctor``, ``backup``, ``restore``, ``migrate``,
``install-service``, ``reap-environments``, ``keepers``, ``usage``,
``config reconcile``, ``mcp``, ``api-token issue/list/rotate/revoke``, and the
headless engine commands ``projects``/``status``/``sessions``/``logs``/``open``
(read) plus ``start``/``stop`` (write) that drive clauster with no server running.
Bare ``clauster`` and ``clauster -c <cfg>`` still mean ``run`` for
backward compatibility.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import ssl
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .reconcile import Decision, Finding

import uvicorn

from . import __version__, claude_cli, deps, environments, ops, pty_keeper, usage
from .app import create_app
from .auth import hash_password, make_hasher, mint_metrics_token, mint_token
from .config import (
    PERMISSION_MODES,
    RESUME_MODES,
    SANDBOX_MODES,
    SPAWN_MODES,
    ClausterConfig,
    first_config_path,
    load_config,
    resolve_cert_path,
)
from .db.bootstrap import MigrationError
from .db.persistence import Persistence
from .logging_config import setup_logging
from .procutil import KEEPER_SUBCOMMAND
from .recap import RECAP_SUBCOMMAND
from .tls_provision import generate_self_signed

# setproctitle is a required dependency (so the retitle works out of the box). The
# guard is defensive, not optionality: a cosmetic process-rename must never crash
# `clauster run` if the wheel is somehow missing/unbuildable on an exotic platform —
# we degrade to a no-op instead.
try:
    import setproctitle as _setproctitle
except ImportError:  # defensive: a cosmetic retitle must not break startup
    _setproctitle = None

_COMMANDS = {
    "run",
    "hash-password",
    "hash-token",
    "hash-metrics-token",
    "doctor",
    "backup",
    "restore",
    "migrate",
    "install-service",
    "reap-environments",
    "keepers",
    "usage",
    "config",
    "deps",
    "mcp",
    "api-token",
    "projects",
    "status",
    "sessions",
    "logs",
    "open",
    "start",
    "stop",
}
_TOP_LEVEL_FLAGS = {"-h", "--help", "--version"}

# Surfaced in `clauster --help` so needing `-c` isn't a surprise. Keep the order and
# env-var names in lockstep with `clauster.config` (module docstring + `_candidate_paths`)
# and docs/configuration.md, which are the canonical descriptions of the search order.
_CONFIG_DISCOVERY_EPILOG = (
    "config discovery (when -c/--config is omitted, the first existing file wins):\n"
    "  1. $CLAUSTER_CONFIG\n"
    "  2. ./clauster.yml\n"
    "  3. $CLAUSTER_HOME/clauster.yml\n"
)


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv``, dispatch to the requested subcommand, and return its exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    # Internal frozen-binary entry point, handled BEFORE argparse so it never appears
    # in --help: a one-file build re-invokes itself to run the SessionStart recap hook
    # (its bundled resume_recap.py lives in an ephemeral _MEIxxx dir). See clauster.recap.
    if argv and argv[0] == RECAP_SUBCOMMAND:
        return _recap_hook()
    # Same frozen-binary trick for the PTY keeper: a one-file build re-invokes itself as
    # `<exe> __pty-keeper__ …` because `sys.executable -m clauster.pty_keeper` can't work
    # when sys.executable is the binary. See runner._keeper_launch_cmd / procutil.
    if argv and argv[0] == KEEPER_SUBCOMMAND:
        return pty_keeper.main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="clauster",
        description=__doc__,
        epilog=_CONFIG_DISCOVERY_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"clauster {__version__}")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="run the server (default)")
    run_p.add_argument("-c", "--config", help="path to clauster.yml")
    sub.add_parser("hash-password", help="hash a password for auth.password_hash")
    sub.add_parser("hash-token", help="mint an API token + hash for auth.api_token_hash")
    sub.add_parser(
        "hash-metrics-token",
        help="mint a /metrics scrape token + hash for observability.metrics_token_hash",
    )

    doctor_p = sub.add_parser("doctor", help="diagnose config / environment")
    doctor_p.add_argument("-c", "--config", help="path to clauster.yml")

    backup_p = sub.add_parser("backup", help="back up state_dir + config to a tar.gz")
    backup_p.add_argument("-c", "--config", help="path to clauster.yml")
    backup_p.add_argument("-o", "--output", default=".", help="output file or directory")

    restore_p = sub.add_parser(
        "restore", help="restore state (and optionally config) from a backup"
    )
    restore_p.add_argument("backup", help="path to a clauster backup tar.gz")
    restore_p.add_argument("--state-dir", required=True, help="state_dir to restore into")
    restore_p.add_argument("--config-out", help="also restore the config to this path")
    restore_p.add_argument("--force", action="store_true", help="overwrite a non-empty target")

    migrate_p = sub.add_parser("migrate", help="migrate state.json to the current schema")
    migrate_p.add_argument("-c", "--config", help="path to clauster.yml")

    svc_p = sub.add_parser(
        "install-service", help="print (or, with --write, install) a service unit"
    )
    svc_p.add_argument("kind", choices=("systemd", "launchd", "windows"))
    svc_p.add_argument("-c", "--config", help="config path to embed in the unit")
    svc_p.add_argument("--user", help="run-as user (systemd)")
    svc_p.add_argument(
        "--write",
        nargs="?",
        const=True,
        default=False,
        metavar="PATH",
        help="apply the service instead of printing it: write the unit to PATH / the conventional "
        "location (systemd, launchd), or register + start it (windows). May need elevation "
        "(sudo / an Administrator prompt).",
    )

    reap_p = sub.add_parser(
        "reap-environments",
        help="archive ghost bridge environments (dry-run by default)",
    )
    reap_p.add_argument("-c", "--config", help="path to clauster.yml")
    reap_p.add_argument("--archive", action="store_true", help="archive the ghosts (reversible)")
    reap_p.add_argument(
        "--force-delete",
        action="store_true",
        help="hard-delete ghosts, discarding queued work (instead of archiving)",
    )

    keepers_p = sub.add_parser(
        "keepers", help="list or stop orphaned pty keepers (no project card)"
    )
    keepers_p.add_argument("-c", "--config", help="path to clauster.yml")
    keepers_p.add_argument(
        "--kill",
        type=int,
        metavar="PID",
        help="stop the orphaned keeper with this keeper PID (refuses a carded keeper)",
    )

    usage_p = sub.add_parser("usage", help="token + approx cost summary for a session transcript")
    usage_p.add_argument("transcript", help="path to a session transcript .jsonl")

    config_p = sub.add_parser("config", help="inspect / clean up the config file")
    config_sub = config_p.add_subparsers(dest="config_command")
    reconcile_p = config_sub.add_parser(
        "reconcile", help="remove deprecated config keys, writing their replacements"
    )
    reconcile_p.add_argument("-c", "--config", help="path to clauster.yml")
    reconcile_p.add_argument(
        "--dry-run",
        action="store_true",
        help="show the proposed changes without writing anything",
    )
    reconcile_p.add_argument(
        "--yes",
        action="store_true",
        help="apply the proposed replacements non-interactively (no prompts)",
    )

    # deps: manage optional pip extras for the standalone binary (#904). The binary can't
    # pip-install into itself, so `deps install <extra>` side-installs the extra's wheels into
    # <state_dir>/deps, which the server adds to sys.path at startup (frozen only).
    deps_p = sub.add_parser("deps", help="manage optional extras for the standalone binary")
    deps_sub = deps_p.add_subparsers(dest="deps_command")
    deps_list_p = deps_sub.add_parser("list", help="show optional extras + their status")
    deps_list_p.add_argument("-c", "--config", help="path to clauster.yml")
    deps_install_p = deps_sub.add_parser(
        "install", help="side-install an extra's wheels (or a managed binary) beside the binary"
    )
    deps_install_p.add_argument("-c", "--config", help="path to clauster.yml")
    deps_install_p.add_argument(
        "extra",
        choices=(*deps.extra_names(), *deps.binary_dep_names()),
        help="what to install (e.g. pty, notify, or the shawl service wrapper)",
    )
    deps_install_p.add_argument(
        "--yes", action="store_true", help="skip the provenance confirmation prompt"
    )
    deps_uninstall_p = deps_sub.add_parser(
        "uninstall", help="remove a side-installed extra/binary"
    )
    deps_uninstall_p.add_argument("-c", "--config", help="path to clauster.yml")
    deps_uninstall_p.add_argument(
        "extra", choices=(*deps.extra_names(), *deps.binary_dep_names()), help="what to remove"
    )

    mcp_p = sub.add_parser(
        "mcp", help="run the read-only MCP server over stdio (list + status, #527)"
    )
    mcp_p.add_argument("-c", "--config", help="path to clauster.yml")

    token_p = sub.add_parser(
        "api-token", help="issue / list / rotate / revoke named public-API bearer tokens (#302)"
    )
    token_sub = token_p.add_subparsers(dest="token_verb")
    token_issue_p = token_sub.add_parser("issue", help="mint a new named token")
    token_issue_p.add_argument("-c", "--config", help="path to clauster.yml")
    _add_token_label_args(token_issue_p)
    token_list_p = token_sub.add_parser(
        "list", help="list named tokens (label / created / last-used — never the secret)"
    )
    token_list_p.add_argument("-c", "--config", help="path to clauster.yml")
    token_rotate_p = token_sub.add_parser(
        "rotate", help="mint a fresh secret for an existing label"
    )
    token_rotate_p.add_argument("-c", "--config", help="path to clauster.yml")
    _add_token_label_args(token_rotate_p)
    token_revoke_p = token_sub.add_parser("revoke", help="permanently delete a named token")
    token_revoke_p.add_argument("-c", "--config", help="path to clauster.yml")
    _add_token_label_args(token_revoke_p)

    # Headless read commands over the shared engine facade (#775, Slice A).
    projects_p = sub.add_parser("projects", help="list discoverable projects (no server)")
    projects_p.add_argument("-c", "--config", help="path to clauster.yml")
    projects_p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    status_p = sub.add_parser("status", help="list bridge instances and their status")
    status_p.add_argument("-c", "--config", help="path to clauster.yml")
    status_p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    sessions_p = sub.add_parser("sessions", help="list live working sessions")
    sessions_p.add_argument("-c", "--config", help="path to clauster.yml")
    sessions_p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    logs_p = sub.add_parser("logs", help="tail a bridge's redacted log")
    logs_p.add_argument("-c", "--config", help="path to clauster.yml")
    logs_p.add_argument("instance", help="instance id (or a prefix / bridge identity)")
    logs_p.add_argument(
        "-f", "--follow", action="store_true", help="stream new lines until Ctrl-C"
    )
    open_p = sub.add_parser("open", help="print a bridge's connect URL")
    open_p.add_argument("-c", "--config", help="path to clauster.yml")
    open_p.add_argument("instance", help="instance id (or a prefix / bridge identity)")
    open_p.add_argument("--launch", action="store_true", help="also open the URL in a browser")
    start_p = sub.add_parser("start", help="start a bridge for a project (no server)")
    start_p.add_argument("-c", "--config", help="path to clauster.yml")
    start_p.add_argument("project", help="project name to start a bridge for")
    start_p.add_argument(
        "--mode",
        dest="resume_mode",
        choices=RESUME_MODES,
        help="bridge mode: standard (remote-control) or pty (true-resume interactive session)",
    )
    start_p.add_argument(
        "--spawn-mode", choices=SPAWN_MODES, help="working directory strategy (default same-dir)"
    )
    start_p.add_argument(
        "--permission-mode",
        choices=PERMISSION_MODES,
        help="claude permission mode for the session",
    )
    start_p.add_argument(
        "--name", help="custom display name for a standard bridge (ignored for pty)"
    )
    start_p.add_argument(
        "--sandbox", choices=SANDBOX_MODES, help="per-launch sandbox toggle for a standard bridge"
    )
    start_p.add_argument(
        "--trust",
        action="store_true",
        help="accept the workspace-trust dialog for the project before starting",
    )
    start_p.add_argument("--json", action="store_true", help="emit JSON instead of a status line")
    stop_p = sub.add_parser("stop", help="stop a bridge by instance id / identity")
    stop_p.add_argument("-c", "--config", help="path to clauster.yml")
    stop_p.add_argument("instance", help="instance id (or a prefix / bridge identity)")
    stop_p.add_argument("--json", action="store_true", help="emit JSON instead of a status line")

    # Treat bare `clauster` / `clauster -c x` as `run` for backward compatibility.
    if argv and argv[0] not in _COMMANDS and argv[0] not in _TOP_LEVEL_FLAGS:
        argv = ["run", *argv]
    args = parser.parse_args(argv)

    if args.command == "hash-password":
        return _hash_password()
    if args.command == "hash-token":
        return _hash_token()
    if args.command == "hash-metrics-token":
        return _hash_metrics_token()
    if args.command == "doctor":
        return _doctor(args.config)
    if args.command == "backup":
        return _backup(args.config, args.output)
    if args.command == "restore":
        return _restore(args.backup, args.state_dir, args.config_out, args.force)
    if args.command == "migrate":
        return _migrate(args.config)
    if args.command == "install-service":
        return _install_service(args.kind, args.config, args.user, args.write)
    if args.command == "reap-environments":
        return _reap_environments(args.config, args.archive, args.force_delete)
    if args.command == "keepers":
        return _keepers(args.config, args.kill)
    if args.command == "usage":
        return _usage(args.transcript)
    if args.command == "config":
        if args.config_command == "reconcile":
            return _reconcile(args.config, dry_run=args.dry_run, assume_yes=args.yes)
        config_p.print_help(sys.stderr)
        return 2
    if args.command == "deps":
        if args.deps_command == "list":
            return _deps_list(args.config)
        if args.deps_command == "install":
            return _deps_install(args.config, args.extra, assume_yes=args.yes)
        if args.deps_command == "uninstall":
            return _deps_uninstall(args.config, args.extra)
        deps_p.print_help(sys.stderr)
        return 2
    if args.command == "mcp":
        # Imported lazily so the common `run` path never pays for the MCP server's
        # import graph, and the rest of the CLI works even if it's unused.
        from .mcp_server import main as mcp_main

        return mcp_main(["-c", args.config] if args.config else [])
    if args.command == "api-token":
        if args.token_verb == "list":  # noqa: S105 — a subcommand name, not a secret
            return _api_token_list(args.config)
        label_verbs = {  # noqa: S105 — subcommand names, not secrets
            "issue": _api_token_issue,
            "rotate": _api_token_rotate,
            "revoke": _api_token_revoke,
        }
        action = label_verbs.get(args.token_verb)
        if action is not None:
            label = _resolve_token_label(args.token_verb, args)
            if label is None:
                return 2
            return action(args.config, label)
        token_p.print_help(sys.stderr)
        return 2
    if args.command in ("projects", "status", "sessions", "logs", "open"):
        # Lazy-imported so the hot `run` path never pays for the engine/CLI import graph.
        from . import cli_read

        config = _load_or_exit(args.config)
        if args.command == "projects":
            return cli_read.cmd_projects(config, as_json=args.json)
        if args.command == "status":
            return cli_read.cmd_status(config, as_json=args.json)
        if args.command == "sessions":
            return cli_read.cmd_sessions(config, as_json=args.json)
        if args.command == "logs":
            return cli_read.cmd_logs(config, args.instance, follow=args.follow)
        return cli_read.cmd_open(config, args.instance, launch=args.launch)
    if args.command in ("start", "stop"):
        # Lazy-imported so the hot `run` path never pays for the engine/CLI import graph.
        from . import cli_write

        config = _load_or_exit(args.config)
        if args.command == "start":
            return cli_write.cmd_start(
                config,
                args.project,
                spawn_mode=args.spawn_mode,
                permission_mode=args.permission_mode,
                resume_mode=args.resume_mode,
                custom_name=args.name,
                sandbox=args.sandbox,
                trust=args.trust,
                as_json=args.json,
            )
        return cli_write.cmd_stop(config, args.instance, as_json=args.json)
    return _run(getattr(args, "config", None))


def _recap_hook() -> int:
    """Run the SessionStart recap hook in-process (frozen-binary entry point).

    A one-file binary registers ``<exe> __recap-hook__`` as the bridge's hook
    command because the bundled ``resume_recap.py`` lives in an ephemeral
    ``_MEIxxx`` dir. Mirror the script's own guard: a hook must never break the
    session it serves, so any error is swallowed and we still exit 0.
    """
    from .hooks.resume_recap import main as recap_main

    try:
        recap_main()
    except Exception:  # noqa: BLE001, S110 — a hook must never break the session it serves
        pass
    return 0


def _hash_password() -> int:
    password = getpass.getpass("Password: ")
    if not password:
        print("clauster: empty password", file=sys.stderr)
        return 2
    if password != getpass.getpass("Confirm:  "):
        print("clauster: passwords do not match", file=sys.stderr)
        return 2
    print(hash_password(make_hasher(), password))
    return 0


def _hash_token() -> int:
    """Mint an API token: print the raw token once + the hash for the config.

    The raw token is shown exactly once — store it in the client now, it cannot be
    recovered. Only the hash goes in ``auth.api_token_hash`` (or the
    ``CLAUSTER_AUTH_API_TOKEN_HASH`` env var). Guidance is on stderr so
    ``clauster hash-token`` can be piped without capturing the prose.
    """
    raw, token_hash = mint_token()
    print("Token (shown once — copy it into your client now):", file=sys.stderr)
    print(raw)
    print(file=sys.stderr)
    print(
        "Add this to clauster.yml under auth (or set CLAUSTER_AUTH_API_TOKEN_HASH):",
        file=sys.stderr,
    )
    print(f"  api_token_hash: {token_hash}", file=sys.stderr)
    return 0


def _hash_metrics_token() -> int:
    """Mint a `/metrics` scrape token: print the raw token once + the hash (#473).

    Parity with ``hash-token``: the raw token is shown exactly once — give it to the
    scraper (e.g. Prometheus) now, it cannot be recovered. Only the hash goes in
    ``observability.metrics_token_hash`` (or the
    ``CLAUSTER_OBSERVABILITY_METRICS_TOKEN_HASH`` env var). Guidance is on stderr so
    the command can be piped without capturing the prose.
    """
    raw, token_hash = mint_metrics_token()
    print("Token (shown once — copy it into your scraper now):", file=sys.stderr)
    print(raw)
    print(file=sys.stderr)
    print(
        "Add this to clauster.yml under observability (or set "
        "CLAUSTER_OBSERVABILITY_METRICS_TOKEN_HASH):",
        file=sys.stderr,
    )
    print(f"  metrics_token_hash: {token_hash}", file=sys.stderr)
    return 0


# ----- api-token: named public-API bearer tokens (#302) --------------------
#
# CLI-first token management: the running app never mints/rotates/revokes a
# token itself, only verifies one on the request hot path (see app.py's
# `_authenticate`). Each verb here opens its own short-lived `Persistence` (the
# same fail-closed migrate-to-head + legacy-import the server runs) and
# disposes it before returning — there is no long-lived DB connection in the CLI.


def _add_token_label_args(parser: argparse.ArgumentParser) -> None:
    """Accept the token label as EITHER a positional or ``--label`` (#958 P7).

    The three verbs that take a label (``issue`` / ``rotate`` / ``revoke``) used to
    disagree — ``issue`` required ``--label`` while ``rotate``/``revoke`` took a
    positional, so ``api-token revoke --label X`` errored. Both forms are now accepted
    on all three; neither is required at the argparse level (:func:`_resolve_token_label`
    enforces "exactly one label" in the dispatch, so a missing label is a clean exit 2).
    """
    parser.add_argument("label", nargs="?", default=None, help="the token's label")
    parser.add_argument(
        "--label",
        dest="label_flag",
        default=None,
        metavar="LABEL",
        help="the token's label (equivalent to the positional form)",
    )


def _resolve_token_label(verb: str, args: argparse.Namespace) -> str | None:
    """Return the label from the positional OR ``--label`` form, or ``None`` on error.

    The two forms are equivalent; supplying both with DIFFERENT values is rejected (so a
    typo can't silently pick one). Prints the error itself and returns ``None`` — the
    caller exits 2 — when no label was given or the two forms disagree.
    """
    positional, flag = args.label, args.label_flag
    if positional is not None and flag is not None and positional != flag:
        print(
            f"clauster: api-token {verb}: give the label once (positional or --label), not both",
            file=sys.stderr,
        )
        return None
    label = flag if flag is not None else positional
    if not label:
        print(
            f"clauster: api-token {verb}: a label is required (positional or --label)",
            file=sys.stderr,
        )
        return None
    return label


def _open_persistence_or_exit(config) -> Persistence:
    """Open the DB (migrate-to-head + legacy import) or exit with a clean CLI error.

    ``Persistence(...)`` is fail-closed and can raise before any verb-level error
    handling runs (a failed migration -> :class:`MigrationError`, an unreadable
    state_dir -> :class:`OSError`). Without this guard every ``api-token`` verb
    would crash with a traceback instead of a command error (mirrors
    :func:`_load_or_exit`).
    """
    try:
        return Persistence(config.state_dir)
    except (MigrationError, OSError) as exc:
        print(f"clauster: could not open the database: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _api_token_issue(config_path: str | None, label: str) -> int:
    """Mint a new named token; print the raw secret once (never persisted)."""
    config = _load_or_exit(config_path)
    persistence = _open_persistence_or_exit(config)
    try:
        raw, record = persistence.api_token_store().issue(label)
    except ValueError as exc:
        print(f"clauster: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"clauster: api-token issue failed: {exc}", file=sys.stderr)
        return 1
    finally:
        persistence.dispose()
    print("Token (shown once — copy it into your client now):", file=sys.stderr)
    print(raw)
    print(file=sys.stderr)
    print(
        f"clauster: issued {record.label!r} "
        f"(created {record.created_at.isoformat(timespec='seconds')})",
        file=sys.stderr,
    )
    return 0


def _api_token_list(config_path: str | None) -> int:
    """List every named token — label / created / last-used, never the secret."""
    config = _load_or_exit(config_path)
    persistence = _open_persistence_or_exit(config)
    try:
        records = persistence.api_token_store().list_all()
    except OSError as exc:
        # A locked/corrupt DB must NOT read as "no named tokens" — fail loudly.
        print(f"clauster: {exc}", file=sys.stderr)
        return 1
    finally:
        persistence.dispose()
    if not records:
        print("clauster: no named tokens", file=sys.stderr)
    else:
        print(f"{'LABEL':<24} {'CREATED':<21} LAST USED")
        for record in records:
            created = record.created_at.isoformat(timespec="seconds")
            last_used = (
                record.last_used_at.isoformat(timespec="seconds")
                if record.last_used_at
                else "never"
            )
            print(f"{record.label:<24} {created:<21} {last_used}")
    if config.auth.api_token_hash:
        print(
            "clauster: a legacy auth.api_token_hash is also configured and still "
            "authenticates (not listed above — it carries no label).",
            file=sys.stderr,
        )
    return 0


def _api_token_rotate(config_path: str | None, label: str) -> int:
    """Mint a fresh secret for an existing label; print the new raw secret once."""
    config = _load_or_exit(config_path)
    persistence = _open_persistence_or_exit(config)
    try:
        raw, record = persistence.api_token_store().rotate(label)
    except ValueError as exc:
        print(f"clauster: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"clauster: api-token rotate failed: {exc}", file=sys.stderr)
        return 1
    finally:
        persistence.dispose()
    print("Token (shown once — copy it into your client now):", file=sys.stderr)
    print(raw)
    print(file=sys.stderr)
    print(
        f"clauster: rotated {record.label!r} — the previous secret no longer works",
        file=sys.stderr,
    )
    return 0


def _api_token_revoke(config_path: str | None, label: str) -> int:
    """Permanently delete a named token by label."""
    config = _load_or_exit(config_path)
    persistence = _open_persistence_or_exit(config)
    try:
        found = persistence.api_token_store().revoke(label)
    except OSError as exc:
        print(f"clauster: api-token revoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        persistence.dispose()
    if not found:
        print(f"clauster: no token labeled {label!r}", file=sys.stderr)
        return 2
    print(f"clauster: revoked {label!r}", file=sys.stderr)
    return 0


_STATUS_MARK = {ops.OK: "✓", ops.WARN: "!", ops.FAIL: "✗"}


def _doctor(config_path: str | None) -> int:
    checks, ok = ops.run_doctor(config_path)
    for c in checks:
        print(
            f"  {_STATUS_MARK.get(c.status, '?')} {c.name:<16} {c.detail}",
            file=sys.stderr,
        )
    print(
        ("clauster: all checks passed" if ok else "clauster: FAILURES above"),
        file=sys.stderr,
    )
    return 0 if ok else 1


def _load_or_exit(config_path: str | None):
    try:
        return load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"clauster: config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _deps_list(config_path: str | None) -> int:
    """List each optional extra: capability, whether it's importable, and any managed version.

    ``loaded`` = importable now (installed or already on ``sys.path``); ``installed`` = present in
    ``<state_dir>/deps`` but pending a restart; ``missing`` = absent; ``n/a`` = a platform-scoped
    entry (e.g. ``pywinpty``) that doesn't apply here. The table goes to stdout so it can be piped.
    """
    config = _load_or_exit(config_path)
    installed = deps.installed_versions(config.state_dir)
    print(f"{'EXTRA':<8} {'DIST':<10} {'STATUS':<10} DETAIL")
    for name in deps.extra_names():
        for entry in deps.extras_for(name):
            if not deps.applies(entry):
                status, detail = "n/a", f"{entry.capability_label} (other platform)"
            elif deps.probe(entry):
                status, detail = "loaded", entry.capability_label
            elif version := installed.get(deps.canonical_name(entry.dist)):
                status = "installed"
                detail = f"{entry.capability_label} — {version} in deps dir; restart to load"
            else:
                status, detail = "missing", entry.capability_label
            print(f"{name:<8} {entry.dist:<10} {status:<10} {detail}")
    for dep in deps.BINARY_DEPS:
        if not deps.applies(dep):
            status, detail = "n/a", f"{dep.label} (other platform)"
        elif deps.installed_binary_path(dep.key, config.state_dir):
            status, detail = "installed", f"{dep.label} {dep.version} in deps dir"
        else:
            status, detail = "missing", dep.label
        print(f"{dep.key:<8} {'(binary)':<10} {status:<10} {detail}")
    return 0


def _deps_install(config_path: str | None, target: str, *, assume_yes: bool) -> int:
    """Side-install ``target`` (a pip extra, or a managed binary like ``shawl``) into deps."""
    config = _load_or_exit(config_path)
    if target in deps.binary_dep_names():
        return deps.install_binary_dep(target, config.state_dir, assume_yes=assume_yes)
    return deps.install_extra(target, config.state_dir, assume_yes=assume_yes)


def _deps_uninstall(config_path: str | None, target: str) -> int:
    """Remove ``target`` (a pip extra, or a managed binary like ``shawl``) from the deps dir."""
    config = _load_or_exit(config_path)
    if target in deps.binary_dep_names():
        return deps.uninstall_binary_dep(target, config.state_dir)
    return deps.uninstall_extra(target, config.state_dir)


def _backup(config_path: str | None, output: str) -> int:
    config = _load_or_exit(config_path)
    try:
        path = ops.make_backup(config, Path(output))
    except OSError as exc:  # disk full / unwritable dest — clean exit, not a traceback
        print(f"clauster: backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"clauster: wrote backup {path}", file=sys.stderr)
    print(
        "clauster: note — the backed-up config contains the argon2 password hash; "
        "store the archive securely.",
        file=sys.stderr,
    )
    print(path)
    return 0


def _restore(backup: str, state_dir: str, config_out: str | None, force: bool) -> int:
    try:
        result = ops.restore_backup(
            Path(backup),
            state_dir=Path(state_dir),
            config_out=Path(config_out) if config_out else None,
            force=force,
        )
    except FileNotFoundError as exc:
        print(f"clauster: {exc}", file=sys.stderr)
        return 2
    except FileExistsError as exc:
        print(f"clauster: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:  # unsafe archive member
        print(f"clauster: refused unsafe backup: {exc}", file=sys.stderr)
        return 1
    print(
        f"clauster: restored {result['state_files']} state file(s)"
        + (f"; config -> {result['config']}" if result["config"] else ""),
        file=sys.stderr,
    )
    return 0


def _migrate(config_path: str | None) -> int:
    config = _load_or_exit(config_path)
    result = ops.migrate_state(config)
    print(
        f"clauster: state at schema {result['schema_version']} "
        f"({result['instances']} instance record(s))",
        file=sys.stderr,
    )
    return 0


def _format_value(value: object) -> str:
    """Render a config value for the reconcile summary (lower-case booleans like YAML)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _interactive_decide(finding: Finding) -> Decision:
    """Prompt the operator about one reconcile finding; return a Decision.

    Reads from stdin (the only I/O in this path, so :func:`reconcile.build_plan` stays
    testable with an injected callback). Accepts the proposed value by default, lets the
    operator pick another allowed value, or skip the key entirely. EOF / empty input
    means "accept the proposal".
    """
    from .reconcile import Decision

    dep = finding.deprecation
    choices = dep.choices
    if finding.has_replacement:
        proposal = f"{dep.replacement_key}: {_format_value(finding.proposed_value)}"
    elif finding.replacement_present:
        proposal = f"remove {dep.deprecated_key} ({dep.replacement_key} is already set — kept)"
    else:
        proposal = f"remove {dep.deprecated_key} (no replacement value needed)"
    old = _format_value(finding.old_value)
    print(f"\n  deprecated: {dep.deprecated_key} = {old}", file=sys.stderr)
    print(f"  reason:     {dep.explain}", file=sys.stderr)
    print(f"  proposed:   {proposal}", file=sys.stderr)
    prompt = "  apply? [Y]es / [n]o"
    # Offer a value choice only when writing a replacement is actually on the table. When the
    # replacement key is already set (both-present case) the existing value is kept, so picking
    # a value would silently override it — don't even offer it.
    if choices and not finding.replacement_present:
        prompt += " / a value (" + ", ".join(choices) + ")"
    prompt += ": "
    try:
        answer = input(prompt).strip()
    except EOFError:  # non-interactive stdin closed — treat as accept-default
        answer = ""
    if answer == "" or answer.lower() in {"y", "yes"}:
        return Decision(
            apply=True, value=finding.proposed_value, has_value=finding.has_replacement
        )
    if answer.lower() in {"n", "no"}:
        return Decision(apply=False)
    if answer in choices and finding.replacement_present:
        # Defence in depth: even if the operator types a value (or scripts stdin), the
        # already-set replacement is kept — never silently overridden by the dead alias.
        print(
            f"  clauster: {dep.replacement_key} is already set — keeping it; "
            f"removing {dep.deprecated_key} without overriding.",
            file=sys.stderr,
        )
        return Decision(apply=True, value=finding.proposed_value, has_value=False)
    if answer in choices:
        return Decision(apply=True, value=answer, has_value=True)
    print(
        f"  clauster: '{answer}' is not a valid choice — skipping {dep.deprecated_key}.",
        file=sys.stderr,
    )
    return Decision(apply=False)


def _reconcile(config_path: str | None, *, dry_run: bool, assume_yes: bool) -> int:
    """Scan the config for deprecated keys and rewrite them via the atomic config writer.

    ``--dry-run`` prints the plan and writes nothing; ``--yes`` accepts every proposed
    replacement without prompting. The interactive path prompts per finding. The rewrite
    reuses ``config_writer.write_edits`` (backup + atomic replace).
    """
    from .reconcile import Decision, apply_plan, build_plan, scan_config_file

    config = _load_or_exit(config_path)
    source = config.source_path
    if source is None:  # pragma: no cover - load_config always sets it; defensive
        print("clauster: could not determine the loaded config file path.", file=sys.stderr)
        return 1
    source_str = str(source)

    try:
        findings = scan_config_file(source_str)
    except OSError as exc:
        print(f"clauster: could not read config: {exc}", file=sys.stderr)
        return 1
    if not findings:
        print(f"clauster: no deprecated keys in {source_str}.", file=sys.stderr)
        return 0

    def decide(finding: Finding) -> Decision:
        # --dry-run is a non-interactive PREVIEW: accept every proposal to show the full
        # would-be plan, never prompt. build_plan() runs decide() eagerly, BEFORE the
        # dry_run guard below, so without this a --dry-run blocks on input() on a real TTY
        # (#650 — the docstring already promised dry-run "prints the plan and writes nothing").
        if assume_yes or dry_run:
            return Decision(
                apply=True, value=finding.proposed_value, has_value=finding.has_replacement
            )
        return _interactive_decide(finding)

    plan = build_plan(findings, decide)

    # Summarize what will change (one line per accepted finding).
    for finding in findings:
        dep = finding.deprecation
        if dep.deprecated_key in plan.removals:
            if dep.replacement_key in plan.edits:
                target = f"{dep.replacement_key}: {_format_value(plan.edits[dep.replacement_key])}"
            elif finding.replacement_present:
                target = f"(removed; existing {dep.replacement_key} kept)"
            else:
                target = "(removed; no replacement value)"
            print(f"clauster: {dep.deprecated_key} -> {target}", file=sys.stderr)

    if plan.is_empty:
        print("clauster: nothing to change (all findings skipped).", file=sys.stderr)
        return 0

    if dry_run:
        print(
            f"clauster: --dry-run — {len(plan.removals)} key(s) would be rewritten in "
            f"{source_str}; nothing written.",
            file=sys.stderr,
        )
        return 0

    try:
        apply_plan(source_str, plan)
    except OSError as exc:
        print(f"clauster: rewrite failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # validation / stale-hash — surface, never silently swallow
        print(f"clauster: rewrite rejected: {exc}", file=sys.stderr)
        return 1
    print(
        f"clauster: rewrote {source_str} ({len(plan.removals)} deprecated key(s) removed); "
        "a timestamped .bak-* backup was kept.",
        file=sys.stderr,
    )
    return 0


def _shawl_available(state_dir: str | None) -> bool:
    """Return whether Shawl is installed — in the managed ``deps/bin`` dir or on ``PATH``."""
    if state_dir is not None and deps.installed_binary_path("shawl", state_dir) is not None:
        return True
    return shutil.which("shawl") is not None


def _install_service(
    kind: str, config_path: str | None, user: str | None, write: bool | str = False
) -> int:
    """Print a service unit, or write it to disk when ``--write`` is given.

    ``write`` is False (print to stdout — the back-compatible default), True (write to
    the conventional ``ops.default_service_path``), or a path string (write there). A
    write that the process can't perform (a system path without privileges) fails
    closed with a clear hint rather than a traceback.
    """
    state_dir: str | None = None
    if kind == "windows":
        # Point the generated service at the managed shawl.exe. Best-effort: install-service must
        # still render without a fully-valid config, so a load failure falls back to a bare `shawl`
        # on PATH (state_dir=None) rather than erroring.
        try:
            state_dir = str(load_config(config_path).state_dir)
        except (FileNotFoundError, ValueError):
            state_dir = None
    unit = ops.render_service_unit(kind, config_path=config_path, user=user, state_dir=state_dir)
    if write is False:
        # Inspection form: print the unit / plist / .bat. On Windows, note if Shawl is missing so
        # the `--write` that follows (which registers the service via Shawl) will succeed.
        print(unit)
        if kind == "windows" and not _shawl_available(state_dir):
            print(
                "clauster: note — Shawl isn't installed yet; run `clauster deps install shawl` "
                "before `clauster install-service windows --write`.",
                file=sys.stderr,
            )
        return 0
    if kind == "windows":
        # --write on Windows REGISTERS + starts the service directly (needs an elevated prompt),
        # the imperative equivalent of writing a systemd unit / launchd plist — no .bat to run.
        return _register_windows_service(config_path, state_dir)
    dest = Path(write) if isinstance(write, str) else ops.default_service_path(kind)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(unit, encoding="utf-8")
    except OSError as exc:
        print(f"clauster: could not write {dest}: {exc}", file=sys.stderr)
        if isinstance(exc, PermissionError):
            print("clauster: re-run with sufficient privileges (e.g. sudo).", file=sys.stderr)
        return 1
    print(f"clauster: wrote {kind} service unit to {dest}", file=sys.stderr)
    if kind == "systemd":
        print(
            "clauster: next: sudo systemctl daemon-reload && sudo systemctl restart clauster",
            file=sys.stderr,
        )
    else:  # launchd
        print(f"clauster: next: launchctl load {dest}", file=sys.stderr)
    return 0


def _register_windows_service(config_path: str | None, state_dir: str | None) -> int:
    """Register + start the Clauster Windows service via Shawl (run from an elevated prompt).

    Runs the same commands ``install-service windows`` prints — ``shawl add`` (which does the
    ``sc create``), ``sc config … start= auto``, then ``sc start`` — in sequence. Fails closed:
    a missing Shawl or a non-zero ``sc``/``shawl`` exit (e.g. not elevated → access denied) is
    surfaced with a clear hint and a non-zero return, never a partial success reported as done.
    """
    if not _shawl_available(state_dir):
        print(
            "clauster: Shawl isn't installed — run `clauster deps install shawl` first.",
            file=sys.stderr,
        )
        return 1
    try:
        commands = ops.windows_service_commands(config_path=config_path, state_dir=state_dir)
    except ValueError as exc:  # an illegal `"` in a path
        print(f"clauster: {exc}", file=sys.stderr)
        return 2
    # commands[0] is `shawl add` (it does the `sc create`); once it succeeds the service EXISTS.
    # If a later step (sc config / sc start) fails we roll it back — otherwise a retry dies at
    # `shawl add` ("already exists"), stranding a half-registered service to delete by hand.
    for index, argv in enumerate(commands):
        try:
            result = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
        except OSError as exc:
            print(f"clauster: could not run {argv[0]!r}: {exc}", file=sys.stderr)
            _rollback_windows_service(created=index > 0)
            return 1
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            print(
                f"clauster: `{' '.join(argv)}` failed (exit {result.returncode})"
                + (f": {detail}" if detail else ""),
                file=sys.stderr,
            )
            print(
                "clauster: registering a Windows service needs elevation — re-run this "
                "from an Administrator prompt.",
                file=sys.stderr,
            )
            _rollback_windows_service(created=index > 0)
            return 1
    print("clauster: registered and started the Clauster service (via Shawl).", file=sys.stderr)
    return 0


def _rollback_windows_service(*, created: bool) -> None:
    """Delete a half-registered ``Clauster`` service so a retry starts clean (best-effort).

    Only runs when ``shawl add`` already created the service (``created``) but a later step failed;
    without it, the next ``install-service windows --write`` would fail at ``shawl add`` ("already
    exists"). A failure to delete (e.g. still not elevated) is surfaced, not swallowed.
    """
    if not created:
        return
    manual = (
        "clauster: could not roll back the partial 'Clauster' service — remove it with "
        "`sc delete Clauster` (elevated) before retrying."
    )
    try:
        result = subprocess.run(  # noqa: S603, S607 - fixed argv, `sc` is a System32 binary
            ["sc", "delete", "Clauster"], capture_output=True, text=True, check=False
        )
    except OSError:
        # The rollback itself couldn't even launch `sc` — surface the manual step, never let this
        # helper raise (it runs on an already-failing path and must not mask the real error).
        print(manual, file=sys.stderr)
        return
    if result.returncode == 0:
        print("clauster: rolled back the partially-registered service.", file=sys.stderr)
    else:
        print(manual, file=sys.stderr)


def _reap_environments(config_path: str | None, archive: bool, force_delete: bool) -> int:
    config = _load_or_exit(config_path)
    try:
        creds = environments.load_credentials(now_ms=int(time.time() * 1000))
    except environments.CredentialsError as exc:
        print(f"clauster: credentials error: {exc}", file=sys.stderr)
        return 2
    print(
        f"clauster: org {creds.organization_uuid}, token {creds.masked_token()}",
        file=sys.stderr,
    )
    client = environments.EnvironmentsClient(creds)
    try:
        envs = client.list_environments()
    except environments.EnvironmentsAPIError as exc:
        print(f"clauster: {exc}", file=sys.stderr)
        return 2
    # SAFETY: never reap without a trustworthy live set. If we can't enumerate live
    # bridges, abort — proceeding could archive a still-live environment.
    try:
        live = environments.live_bridge_directories(config.claude.binary, config.projects_root)
    except Exception as exc:  # noqa: BLE001 - fail closed on ANY liveness-probe failure
        print(
            f"clauster: refusing to reap — could not determine live bridges: {exc}",
            file=sys.stderr,
        )
        return 2

    ghosts = environments.find_ghosts(envs, live)
    print(
        f"clauster: {len(envs)} env(s), {len(live)} live dir(s), {len(ghosts)} ghost(s)",
        file=sys.stderr,
    )
    for g in ghosts:
        print(
            f"  - {g.id}  {g.config.directory or '(no dir)'}  ({g.name})",
            file=sys.stderr,
        )
    if not ghosts:
        return 0
    if not (archive or force_delete):
        print(
            "clauster: dry-run (no changes). Pass --archive to archive (reversible).",
            file=sys.stderr,
        )
        return 0

    action = "delete" if force_delete else "archive"
    for g in ghosts:
        try:
            if force_delete:
                client.delete_environment(g.id, force=True)
            else:
                client.archive_environment(g.id)
        except environments.EnvironmentsAPIError as exc:
            print(f"clauster: failed to {action} {g.id}: {exc}", file=sys.stderr)
            return 1
    print(f"clauster: {action}d {len(ghosts)} ghost environment(s)", file=sys.stderr)
    return 0


def _keepers(config_path: str | None, kill_pid: int | None) -> int:
    """List orphaned pty keepers, or stop one by keeper PID (#301).

    An orphan is a *live* keeper whose sidecar belongs to no current project card
    (e.g. its project was removed), so no dashboard row can show or stop it.
    ``--kill`` refuses any PID that isn't a current orphan — it never touches a
    keeper still attached to a card.
    """
    config = _load_or_exit(config_path)
    log_dir = (config.state_dir / "logs").expanduser()
    # The authoritative card set lives in the DB now. The flat state.json is renamed to
    # *.imported after the one-time JSON->DB migration, so reading it directly would see
    # an EMPTY card set and mislabel every live keeper — including carded ones — as an
    # orphan, letting `--kill` reap a managed keeper. Build Persistence (the same
    # fail-closed migrate + legacy-import the app runs) and read the DB-backed store.
    persistence = Persistence(
        config.state_dir, backup_before_migrate=config.db.backup_before_migrate
    )
    try:
        # Since issue 777 the store is keyed by instance_id; the project name each
        # card belongs to lives in the record's project_name field. Reading keys()
        # here would yield UUIDs, match no sidecar, and mislabel every carded
        # keeper an orphan — the exact fail-open this block exists to prevent.
        carded = {
            fields["project_name"]
            for fields in persistence.state_store().load().values()
            if fields.get("project_name")
        }
    finally:
        persistence.dispose()
    orphans = pty_keeper.find_orphan_keepers(log_dir, carded)
    if kill_pid is not None:
        target = next((k for k in orphans if k.keeper_pid == kill_pid), None)
        if target is None:
            print(
                f"clauster: no orphaned keeper with pid {kill_pid} — it may be carded, "
                "already gone, or not a keeper; refusing to kill.",
                file=sys.stderr,
            )
            return 2
        if not pty_keeper.stop_keeper(kill_pid, expect_create_time=target.keeper_create_time):
            print(
                f"clauster: failed to stop keeper {kill_pid} "
                "(it may have exited or its PID was reused)",
                file=sys.stderr,
            )
            return 1
        try:
            target.sidecar.unlink()  # drop the now-stale sidecar so it stops being listed
        except OSError:
            pass
        print(f"clauster: stopped orphaned keeper {kill_pid} (project {target.project or '?'})")
        return 0
    if not orphans:
        print("clauster: no orphaned keepers")
        return 0
    print(
        f"clauster: {len(orphans)} orphaned keeper(s) — stop with `clauster keepers --kill <pid>`:"
    )
    for k in orphans:
        print(
            f"  keeper_pid={k.keeper_pid}  project={k.project or '?'}  "
            f"bridge_pid={k.bridge_pid}  session={k.session_id or '-'}  state={k.state or '-'}"
        )
    return 0


def _usage(transcript: str) -> int:
    try:
        u = usage.parse_transcript(Path(transcript))
    except FileNotFoundError as exc:
        print(f"clauster: {exc}", file=sys.stderr)
        return 2
    for model, t in sorted(u.by_model.items()):
        c = usage.cost_usd(model, t)
        cstr = f"≈${c:.4f}" if c is not None else "(unpriced)"
        print(
            f"  {model:<22} in={t.input} out={t.output} "
            f"cache_w={t.cache_creation} cache_r={t.cache_read}  {cstr}",
            file=sys.stderr,
        )
    tot = u.totals
    print(
        f"clauster: {tot.messages} assistant msg(s), {tot.total_tokens} tokens, "
        f"≈${u.cost_usd():.4f} total (approx)",
        file=sys.stderr,
    )
    return 0


def _warn_if_cookie_insecure(config) -> None:
    """Warn when auth is on but the session cookie will likely ship without Secure.

    Happens on a plain-http LAN with no TLS-terminating proxy — the cookie is then
    sniffable on the wire.
    """
    a = config.auth
    if not (a.enabled and a.password_required):
        return
    if a.cookie_secure == "always":
        return  # Secure forced regardless of scheme
    if config.tls_active:
        return  # Clauster terminates TLS itself: request.url.scheme is https, so the
        # cookie ships Secure under `auto` (see app._cookie_secure). No warning needed.
    if a.reverse_proxy.enabled:
        return  # a TLS proxy is expected to terminate https and set X-Forwarded-Proto
    print(
        "clauster: WARNING — auth is enabled but the session cookie may ship without "
        "the Secure flag over plain http; put Clauster behind https/a TLS proxy, or set "
        "auth.cookie_secure: always.",
        file=sys.stderr,
    )


def _tls_files(config: ClausterConfig) -> tuple[str, str] | None:
    """Resolve the ``(ssl_certfile, ssl_keyfile)`` pair for uvicorn, or ``None`` if no TLS.

    For ``provision = off``: defense-in-depth re-resolve — the config validator already
    checked both paths at load, but a cert could be deleted or chmod-ed away between
    load and serve.  Aborts (never silently falls back to plain HTTP) if either file is
    now missing/unreadable.  A final pre-flight builds the SSL context exactly as uvicorn
    will, so a malformed/mismatched cert also aborts cleanly here — with our ``TLS
    error`` message and exit 2 — rather than crashing uvicorn with a raw traceback.

    For ``provision = self-signed``: calls the provisioner to generate (or reuse) the
    cert+key under ``state_dir/tls/``, then runs the same pre-flight verify.

    The SSL error message is a generic PEM/parse reason; it never carries key material.
    Returns the canonical absolute paths uvicorn opens, or ``None`` when ``tls`` is
    unset.
    """
    if config.tls is None:
        return None
    if config.tls.provision == "self-signed":
        # Provisioner writes cert+key under state_dir/tls/ (or reuses them if still
        # current).  A RuntimeError here means `cryptography` isn't installed — surface
        # it the same way as a cert-resolve failure so the operator sees a clear message.
        state_dir = config.state_dir.expanduser().resolve()
        cert, key = generate_self_signed(state_dir, config.tls.hostnames)
    else:
        # provision = off: cert_file / key_file are guaranteed non-None by the validator.
        cert = resolve_cert_path("cert_file", config.tls.cert_file)  # type: ignore[arg-type]
        key = resolve_cert_path("key_file", config.tls.key_file)  # type: ignore[arg-type]
    _verify_cert_chain(cert, key)
    # After the fail-closed checks: a non-fatal hygiene warning if the private key is
    # readable beyond its owner. Advisory only — an over-permissive key still serves.
    _warn_if_key_world_readable(key)
    return str(cert), str(key)


def _warn_if_key_world_readable(key: Path) -> None:
    """Warn (non-fatal) when the TLS private key is accessible beyond its owner.

    A private key should be ``0600``-ish; any group/other bit (read, write, or
    execute) exposes it to other local users. This is a hygiene nudge, not a gate —
    it never aborts startup (a cosmetic permission check must not fail-closed) and is
    skipped on non-POSIX platforms (Windows) where these mode bits don't carry the
    same meaning.
    """
    if os.name != "posix":
        return
    try:  # pragma: skip-on-win
        mode = key.stat().st_mode
    except OSError:  # pragma: skip-on-win — racey unlink only; POSIX-only (os.name guard above)
        return
    if mode & 0o077:  # pragma: skip-on-win
        print(
            f"clauster: WARNING — tls.key_file {key} is group/other-accessible "
            f"(mode {mode & 0o777:#o}); a private key should be accessible only by its owner. "
            "Restrict it, e.g. `chmod 600`.",
            file=sys.stderr,
        )


def _verify_cert_chain(cert: Path, key: Path) -> None:
    """Pre-flight the cert + key by building the SSL context uvicorn will, or fail closed.

    Catches a malformed/mismatched cert (one that passes existence/readability but won't
    parse) so it aborts here with our ``TLS error`` message + exit 2, instead of crashing
    uvicorn with a raw traceback at serve time. The re-raised ``ValueError`` carries only
    the cert + key PATHS and the generic SSL/PEM reason — never the private-key bytes. A
    seam so tests can stub the parse without a real keypair (the parse itself is tested
    directly).
    """
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(cert), str(key))
    except (ssl.SSLError, OSError) as exc:
        raise ValueError(
            f"tls cert/key at {cert} (key: {key}) could not be loaded (check the PEM cert "
            f"and that the key matches it): {exc}"
        ) from None


def _process_title(config: ClausterConfig) -> str | None:
    """Process title for this instance (``clauster[<name>]``), or None for the default."""
    if not config.instance_name:
        return None
    return f"clauster[{config.instance_name}]"


def _set_process_title(config: ClausterConfig) -> None:
    """Retitle the process so co-resident instances are distinct in ps/pgrep.

    No-op without ``instance_name`` or if setproctitle isn't installed. This is the
    only safe way to tell a dev instance apart from the prod service — the cmdline
    is otherwise identical, which makes ``pkill -f`` a footgun.
    """
    title = _process_title(config)
    if title and _setproctitle is not None:
        try:
            _setproctitle.setproctitle(title)
        except Exception:  # noqa: BLE001, S110 - a cosmetic retitle must never break startup
            pass


def _reexec() -> None:  # pragma: no cover - replaces the process image; tested via monkeypatch
    """Re-exec this interpreter in place with the same argv (the #483 restart mechanism).

    ``os.execv`` replaces the current process image — same PID on POSIX, fresh code + config
    (config is read at startup). On Windows ``os.execv`` is emulated as spawn-new-then-exit,
    so the PID changes and a Shawl-managed service sees the exit and restarts it (Shawl restarts
    on a non-zero exit) — the in-place, same-PID guarantee is POSIX-only (#914). Called only after
    the uvicorn server has shut down
    gracefully and released its listening socket, so the new image can re-bind. The
    indirection is a deliberate seam: tests monkeypatch this to assert the restart
    endpoint triggers exactly one re-exec without actually replacing the test process.
    """
    # S606: re-exec the SAME interpreter with our own argv — no shell, no user input
    # (argv is the process's own ``sys.argv``). This is the whole point of the action.
    os.execv(sys.executable, [sys.executable, *sys.argv])  # noqa: S606


def _run_setup_wizard(config_path: str | None) -> int:
    """Serve the loopback first-run setup wizard, then re-exec onto the new config (#978).

    Reached only when no ``clauster.yml`` exists. The wizard writes one (auth enabled) and
    asks its server to shut down; we then re-exec so ``load_config`` succeeds on the restart.
    """
    from . import setup_wizard

    write_path = first_config_path(config_path)
    # The wizard binds a fixed loopback port (nothing else is running on first run). Allow an
    # override for the rare case that port is already taken, and so tests can isolate it.
    try:
        port = int(os.environ.get("CLAUSTER_SETUP_PORT", setup_wizard.DEFAULT_PORT))
    except ValueError:
        port = setup_wizard.DEFAULT_PORT
    setup_logging("text")
    app = setup_wizard.create_setup_app(write_path, port=port)
    print(
        f"clauster {__version__}: no configuration found — starting first-run setup at "
        f"http://{setup_wizard.SETUP_HOST}:{port}/  (will write {write_path})",
        file=sys.stderr,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=setup_wizard.SETUP_HOST,
            port=port,
            log_level="info",
            log_config=None,
            proxy_headers=False,
        )
    )
    app.state.uvicorn_server = server
    server.run()  # blocks until the wizard submits (should_exit) or the operator quits
    if getattr(app.state, "setup_complete", False):
        print("clauster: setup complete — restarting on the new configuration.", file=sys.stderr)
        _reexec()
    return 0


def _run(config_path: str | None) -> int:
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        # First run: no clauster.yml exists yet. Serve the loopback setup wizard to write one,
        # then re-exec onto it (#978). A malformed EXISTING config still errors below.
        return _run_setup_wizard(config_path)
    except ValueError as exc:
        print(f"clauster: config error: {exc}", file=sys.stderr)
        return 2

    # Put any side-installed optional extras (`clauster deps install <extra>`, #904) on sys.path
    # before create_app imports anything that needs them. Frozen-binary-only + best-effort — a
    # no-op on a normal install, where extras resolve through site-packages.
    deps.add_deps_dir_to_sys_path(config.state_dir)

    try:
        version = claude_cli.claude_version(config.claude.binary)
    except claude_cli.ClaudeNotFound as exc:
        print(f"clauster: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - surface any probe failure clearly
        print(f"clauster: could not probe claude version: {exc}", file=sys.stderr)
        return 2

    # Fail closed BEFORE serving: re-resolve the TLS material (the config validator
    # already checked it at load, but a cert could vanish or lose read permission
    # between load and serve). For self-signed, generate/renew the cert+key now.
    # A bad cert here aborts startup — Clauster must never silently fall back to
    # plain HTTP when TLS was asked for.  RuntimeError surfaces a missing
    # `cryptography` package with a clear install hint.
    try:
        tls_files = _tls_files(config)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"clauster: TLS error: {exc}", file=sys.stderr)
        return 2

    ssl_certfile, ssl_keyfile = tls_files if tls_files is not None else (None, None)
    scheme = "https" if tls_files else "http"
    print(
        f"clauster {__version__} | claude {version} | "
        f"projects_root={config.projects_root} | {scheme}://{config.host}:{config.port}",
        file=sys.stderr,
    )
    _warn_if_cookie_insecure(config)
    _set_process_title(config)
    # Configure logging before serving (#361). log_config=None below stops uvicorn from
    # reconfiguring logging, so its loggers propagate to the root handler set here and
    # share the chosen text/JSON format + redaction.
    setup_logging(config.log_format)
    app = create_app(config)
    # proxy_headers=False: keep request.client.host as the real socket peer so the
    # reverse-proxy IP allowlist can't be defeated via a spoofed X-Forwarded-For.
    # ssl_certfile/ssl_keyfile (when tls is configured) make uvicorn terminate TLS
    # itself — `request.url.scheme` is then https and the session cookie ships Secure.
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level="info",
            log_config=None,
            proxy_headers=False,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
        )
    )
    # Hand the live server to the app so the in-app "Restart Clauster" action (#483)
    # can request a graceful shutdown via `server.should_exit = True`. That lets
    # uvicorn close the listening socket cleanly before we re-exec — an unclosed,
    # non-CLOEXEC socket FD would otherwise make the re-exec'd process fail to
    # re-bind (address already in use).
    app.state.uvicorn_server = server
    server.run()  # blocks until graceful shutdown (Ctrl-C, SIGTERM, or restart request)
    # If the shutdown was an in-app restart request, re-exec in place now that the
    # socket is released. Re-exec keeps the same PID on POSIX (systemd's MainPID stays valid)
    # and reloads config (read at startup); on Windows `os.execv` changes the PID and a
    # Shawl-managed service restarts it on the non-zero exit (#914). Running bridges + hosted
    # sessions survive the swap (their processes outlive the re-exec) and reattach on startup
    # (#663). Any other shutdown path (signal) falls through to a normal exit.
    if getattr(app.state, "restart_requested", False):
        _reexec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
