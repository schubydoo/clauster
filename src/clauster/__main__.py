"""Entry point: ``clauster`` / ``python -m clauster``."""

from __future__ import annotations

import argparse
import sys

import uvicorn

from . import __version__, claude_cli
from .app import create_app
from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clauster", description=__doc__)
    parser.add_argument("-c", "--config", help="path to clauster.yml")
    parser.add_argument("--version", action="version", version=f"clauster {__version__}")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
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
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
