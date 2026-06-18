#!/usr/bin/env python3
"""Smoke-test a built standalone ``clauster`` binary: boot it and hit ``/healthz``.

A PyInstaller one-file bundle can build cleanly yet fail at *runtime* when a
dynamically-imported module (uvicorn's loop/protocol backends, jinja2-fragments,
…) was not collected into the bundle. ``--version`` only forces import-time
loading; this script actually starts the server and polls ``/healthz`` — the real
one-file failure mode — so CI catches a broken bundle before it ships.

Cross-OS, standard library only. Usage::

    python scripts/smoke_binary.py <path-to-binary>
"""

from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The committed fake `claude` (used by the E2E suite) satisfies clauster's
# startup binary-resolution probe without a real Claude install on the runner.
FAKE_CLAUDE = (
    REPO / "tests" / "fixtures" / "fake_claude" / ("claude.cmd" if os.name == "nt" else "claude")
)


def _free_port() -> int:
    """Grab an OS-assigned free loopback port, then release it for the server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_healthy(port: int, proc: subprocess.Popen, timeout: float = 45.0) -> None:
    """Poll ``/healthz`` until it returns 200, the process dies, or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise SystemExit(f"binary exited early (code {proc.returncode}):\n{out}")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/healthz")
            if conn.getresponse().status == 200:
                return
        except OSError:
            time.sleep(0.3)
        finally:
            conn.close()
    raise SystemExit(f"binary never became healthy on :{port} within {timeout:.0f}s")


def _check_version(binary: Path) -> None:
    """Assert ``<binary> --version`` runs and identifies clauster (import-graph loads)."""
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or "clauster" not in result.stdout:
        raise SystemExit(
            f"`--version` failed (rc={result.returncode}):\n{result.stdout}{result.stderr}"
        )
    print(f"version: {result.stdout.strip()}")


def _check_boot(binary: Path) -> None:
    """Boot ``<binary> run`` against a throwaway config and confirm ``/healthz`` serves.

    ``clauster run`` probes ``claude --version`` before serving and exits 2 if it
    fails, so boot needs ``python3`` (POSIX) / ``python`` (Windows ``claude.cmd``)
    on PATH to execute the fake-claude fixture — both present on GitHub runners.
    """
    # ignore_cleanup_errors: on Windows the boot opens a SQLite state DB
    # (clauster.db + its -wal/-shm sidecars); the hard proc.terminate() below skips
    # the app's clean engine-dispose, so the OS may still hold the file when this
    # dir is torn down (WinError 32). Cleanup is irrelevant to the smoke check — the
    # runner wipes temp between jobs — so don't let a teardown lock fail the test.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        (tmp / "projects").mkdir()
        port = _free_port()
        cfg = tmp / "clauster.yml"
        cfg.write_text(
            f"host: 127.0.0.1\n"
            f"port: {port}\n"
            f"projects_root: {tmp / 'projects'}\n"
            f"state_dir: {tmp / 'state'}\n"
            f"claude:\n  binary: {FAKE_CLAUDE}\n",
            encoding="utf-8",
        )
        # Isolate HOME/USERPROFILE so the runtime never reads/writes a real
        # ~/.claude.json on the runner.
        home = tmp / "home"
        home.mkdir()
        proc = subprocess.Popen(
            [str(binary), "run", "-c", str(cfg)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "HOME": str(home), "USERPROFILE": str(home)},
            start_new_session=(os.name != "nt"),
        )
        try:
            _wait_healthy(port, proc)
            print(f"healthz OK on :{port}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def main() -> int:
    """Run the version + boot smoke checks against the binary in ``argv[1]``."""
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke_binary.py <path-to-binary>")
    binary = Path(sys.argv[1]).resolve()
    if not binary.is_file():
        raise SystemExit(f"binary not found: {binary}")
    if not FAKE_CLAUDE.is_file():
        raise SystemExit(f"fake claude fixture not found: {FAKE_CLAUDE}")
    _check_version(binary)
    _check_boot(binary)
    print("smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
