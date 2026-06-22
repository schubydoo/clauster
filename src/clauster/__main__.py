"""Entry point: ``clauster`` / ``python -m clauster``.

Subcommands: ``run`` (default), ``hash-password``, ``hash-token``,
``hash-metrics-token``, ``doctor``, ``backup``, ``restore``, ``migrate``,
``install-service``, ``reap-environments``, ``keepers``, ``usage``. Bare ``clauster`` and
``clauster -c <cfg>`` still mean ``run`` for backward compatibility.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

import uvicorn

from . import __version__, claude_cli, environments, ops, pty_keeper, usage
from .app import create_app
from .auth import hash_password, make_hasher, mint_metrics_token, mint_token
from .config import ClausterConfig, load_config
from .logging_config import setup_logging
from .recap import RECAP_SUBCOMMAND
from .state import StateStore

# setproctitle is a required dependency (so the retitle works out of the box). The
# guard is defensive, not optionality: a cosmetic process-rename must never crash
# `clauster run` if the wheel is somehow missing/unbuildable on an exotic platform —
# we degrade to a no-op instead.
try:
    import setproctitle as _setproctitle
except ImportError:  # pragma: no cover - defensive: a cosmetic retitle must not break startup
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
        help="write the unit to PATH (or the conventional location) instead of printing it; "
        "may need privileges (e.g. sudo) for a system path",
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


def _install_service(
    kind: str, config_path: str | None, user: str | None, write: bool | str = False
) -> int:
    """Print a service unit, or write it to disk when ``--write`` is given.

    ``write`` is False (print to stdout — the back-compatible default), True (write to
    the conventional ``ops.default_service_path``), or a path string (write there). A
    write that the process can't perform (a system path without privileges) fails
    closed with a clear hint rather than a traceback.
    """
    unit = ops.render_service_unit(kind, config_path=config_path, user=user)
    if write is False:
        print(unit)
        return 0
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
    elif kind == "launchd":
        print(f"clauster: next: launchctl load {dest}", file=sys.stderr)
    else:
        print(
            f"clauster: next: run {dest} from an elevated prompt (needs nssm on PATH)",
            file=sys.stderr,
        )
    return 0


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
    carded = set(StateStore(config.state_dir).load())
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
    if a.reverse_proxy.enabled:
        return  # a TLS proxy is expected to terminate https and set X-Forwarded-Proto
    print(
        "clauster: WARNING — auth is enabled but the session cookie may ship without "
        "the Secure flag over plain http; put Clauster behind https/a TLS proxy, or set "
        "auth.cookie_secure: always.",
        file=sys.stderr,
    )


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


def _run(config_path: str | None) -> int:
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"clauster: config error: {exc}", file=sys.stderr)
        return 2

    try:
        version = claude_cli.claude_version(config.claude.binary)
    except claude_cli.ClaudeNotFound as exc:
        print(f"clauster: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - surface any probe failure clearly
        print(f"clauster: could not probe claude version: {exc}", file=sys.stderr)
        return 2

    print(
        f"clauster {__version__} | claude {version} | "
        f"projects_root={config.projects_root} | http://{config.host}:{config.port}",
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
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level="info",
        log_config=None,
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
