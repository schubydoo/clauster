"""Entry point: ``clauster`` / ``python -m clauster``.

Subcommands: ``run`` (default) and ``hash-password``. Bare ``clauster`` and
``clauster -c <cfg>`` still mean ``run`` for backward compatibility.
"""

from __future__ import annotations

import argparse
import getpass
import sys

import uvicorn

from . import __version__, claude_cli
from .app import create_app
from .auth import hash_password, make_hasher
from .config import load_config

_COMMANDS = {"run", "hash-password"}
_TOP_LEVEL_FLAGS = {"-h", "--help", "--version"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="clauster", description=__doc__)
    parser.add_argument("--version", action="version", version=f"clauster {__version__}")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="run the server (default)")
    run_p.add_argument("-c", "--config", help="path to clauster.yml")
    sub.add_parser("hash-password", help="hash a password for auth.password_hash")

    # Treat bare `clauster` / `clauster -c x` as `run` for backward compatibility.
    if argv and argv[0] not in _COMMANDS and argv[0] not in _TOP_LEVEL_FLAGS:
        argv = ["run", *argv]
    args = parser.parse_args(argv)

    if args.command == "hash-password":
        return _hash_password()
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
