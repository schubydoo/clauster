"""Entry point: ``clauster`` / ``python -m clauster``.

Subcommands: ``run`` (default), ``hash-password``, ``doctor``, ``backup``,
``restore``, ``migrate``, ``install-service``. Bare ``clauster`` and
``clauster -c <cfg>`` still mean ``run`` for backward compatibility.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

import uvicorn

from . import __version__, claude_cli, environments, ops, usage
from .app import create_app
from .auth import hash_password, make_hasher
from .config import load_config

_COMMANDS = {
    "run", "hash-password", "doctor", "backup", "restore", "migrate",
    "install-service", "reap-environments", "usage",
}
_TOP_LEVEL_FLAGS = {"-h", "--help", "--version"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="clauster", description=__doc__)
    parser.add_argument("--version", action="version", version=f"clauster {__version__}")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="run the server (default)")
    run_p.add_argument("-c", "--config", help="path to clauster.yml")
    sub.add_parser("hash-password", help="hash a password for auth.password_hash")

    doctor_p = sub.add_parser("doctor", help="diagnose config / environment")
    doctor_p.add_argument("-c", "--config", help="path to clauster.yml")

    backup_p = sub.add_parser("backup", help="back up state_dir + config to a tar.gz")
    backup_p.add_argument("-c", "--config", help="path to clauster.yml")
    backup_p.add_argument("-o", "--output", default=".", help="output file or directory")

    restore_p = sub.add_parser("restore", help="restore state (and optionally config) from a backup")
    restore_p.add_argument("backup", help="path to a clauster backup tar.gz")
    restore_p.add_argument("--state-dir", required=True, help="state_dir to restore into")
    restore_p.add_argument("--config-out", help="also restore the config to this path")
    restore_p.add_argument("--force", action="store_true", help="overwrite a non-empty target")

    migrate_p = sub.add_parser("migrate", help="migrate state.json to the current schema")
    migrate_p.add_argument("-c", "--config", help="path to clauster.yml")

    svc_p = sub.add_parser("install-service", help="print a service unit (systemd/launchd/windows)")
    svc_p.add_argument("kind", choices=("systemd", "launchd", "windows"))
    svc_p.add_argument("-c", "--config", help="config path to embed in the unit")
    svc_p.add_argument("--user", help="run-as user (systemd)")

    reap_p = sub.add_parser(
        "reap-environments", help="archive ghost bridge environments (dry-run by default)")
    reap_p.add_argument("-c", "--config", help="path to clauster.yml")
    reap_p.add_argument("--archive", action="store_true", help="archive the ghosts (reversible)")
    reap_p.add_argument("--force-delete", action="store_true",
                        help="hard-delete ghosts, discarding queued work (instead of archiving)")

    usage_p = sub.add_parser("usage", help="token + approx cost summary for a session transcript")
    usage_p.add_argument("transcript", help="path to a session transcript .jsonl")

    # Treat bare `clauster` / `clauster -c x` as `run` for backward compatibility.
    if argv and argv[0] not in _COMMANDS and argv[0] not in _TOP_LEVEL_FLAGS:
        argv = ["run", *argv]
    args = parser.parse_args(argv)

    if args.command == "hash-password":
        return _hash_password()
    if args.command == "doctor":
        return _doctor(args.config)
    if args.command == "backup":
        return _backup(args.config, args.output)
    if args.command == "restore":
        return _restore(args.backup, args.state_dir, args.config_out, args.force)
    if args.command == "migrate":
        return _migrate(args.config)
    if args.command == "install-service":
        return _install_service(args.kind, args.config, args.user)
    if args.command == "reap-environments":
        return _reap_environments(args.config, args.archive, args.force_delete)
    if args.command == "usage":
        return _usage(args.transcript)
    return _run(getattr(args, "config", None))


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


_STATUS_MARK = {ops.OK: "✓", ops.WARN: "!", ops.FAIL: "✗"}


def _doctor(config_path: str | None) -> int:
    checks, ok = ops.run_doctor(config_path)
    for c in checks:
        print(f"  {_STATUS_MARK.get(c.status, '?')} {c.name:<16} {c.detail}", file=sys.stderr)
    print(("clauster: all checks passed" if ok else "clauster: FAILURES above"), file=sys.stderr)
    return 0 if ok else 1


def _load_or_exit(config_path: str | None):
    try:
        return load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"clauster: config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _backup(config_path: str | None, output: str) -> int:
    config = _load_or_exit(config_path)
    path = ops.make_backup(config, Path(output))
    print(f"clauster: wrote backup {path}", file=sys.stderr)
    print("clauster: note — the backed-up config contains the argon2 password hash; "
          "store the archive securely.", file=sys.stderr)
    print(path)
    return 0


def _restore(backup: str, state_dir: str, config_out: str | None, force: bool) -> int:
    try:
        result = ops.restore_backup(
            Path(backup), state_dir=Path(state_dir),
            config_out=Path(config_out) if config_out else None, force=force,
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
    print(f"clauster: restored {result['state_files']} state file(s)"
          + (f"; config -> {result['config']}" if result["config"] else ""), file=sys.stderr)
    return 0


def _migrate(config_path: str | None) -> int:
    config = _load_or_exit(config_path)
    result = ops.migrate_state(config)
    print(f"clauster: state at schema {result['schema_version']} "
          f"({result['instances']} instance record(s))", file=sys.stderr)
    return 0


def _install_service(kind: str, config_path: str | None, user: str | None) -> int:
    print(ops.render_service_unit(kind, config_path=config_path, user=user))
    return 0


def _reap_environments(config_path: str | None, archive: bool, force_delete: bool) -> int:
    config = _load_or_exit(config_path)
    try:
        creds = environments.load_credentials(now_ms=int(time.time() * 1000))
    except environments.CredentialsError as exc:
        print(f"clauster: credentials error: {exc}", file=sys.stderr)
        return 2
    print(f"clauster: org {creds.organization_uuid}, token {creds.masked_token()}", file=sys.stderr)
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
        print(f"clauster: refusing to reap — could not determine live bridges: {exc}", file=sys.stderr)
        return 2

    ghosts = environments.find_ghosts(envs, live)
    print(f"clauster: {len(envs)} env(s), {len(live)} live dir(s), {len(ghosts)} ghost(s)",
          file=sys.stderr)
    for g in ghosts:
        print(f"  - {g.id}  {g.config.directory or '(no dir)'}  ({g.name})", file=sys.stderr)
    if not ghosts:
        return 0
    if not (archive or force_delete):
        print("clauster: dry-run (no changes). Pass --archive to archive (reversible).",
              file=sys.stderr)
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


def _usage(transcript: str) -> int:
    try:
        u = usage.parse_transcript(Path(transcript))
    except FileNotFoundError as exc:
        print(f"clauster: {exc}", file=sys.stderr)
        return 2
    for model, t in sorted(u.by_model.items()):
        c = usage.cost_usd(model, t)
        cstr = f"≈${c:.4f}" if c is not None else "(unpriced)"
        print(f"  {model:<22} in={t.input} out={t.output} "
              f"cache_w={t.cache_creation} cache_r={t.cache_read}  {cstr}", file=sys.stderr)
    tot = u.totals
    print(f"clauster: {tot.messages} assistant msg(s), {tot.total_tokens} tokens, "
          f"≈${u.cost_usd():.4f} total (approx)", file=sys.stderr)
    return 0


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
    app = create_app(config)
    # proxy_headers=False: keep request.client.host as the real socket peer so the
    # reverse-proxy IP allowlist can't be defeated via a spoofed X-Forwarded-For.
    uvicorn.run(app, host=config.host, port=config.port, log_level="info", proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
