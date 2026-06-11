#!/usr/bin/env python3
"""Spawnable fake claustrum daemon for connect-or-spawn tests (CL-2).

Mimics the parts of ``claustrum -serve -socket <path> -token-fd <fd>`` that
:mod:`clauster.claustrum_daemon` depends on: it reads the auth token from the
given fd, self-daemonizes (``fork`` + ``setsid``; the launcher process exits 0
once the real daemon is reparented to ``init``), then serves a minimal auth-gated
NDJSON JSON-RPC subset (``server.ping`` / ``server.version`` / ``server.shutdown``)
on the AF_UNIX socket.

The detached daemon writes its own PID to ``<socket>.pid`` so a test can reap it
as a backstop if the ``server.shutdown`` RPC does not land.

Behaviour knobs (env vars), used to exercise the failure paths:

* ``FAKE_CLAUSTRUM_EXIT=<n>``        — the launcher exits ``<n>`` *before*
  daemonizing (models a daemon that fails to start).
* ``FAKE_CLAUSTRUM_HANG_LAUNCHER=1`` — the launcher blocks instead of forking, so
  it never detaches (models a launcher that hangs → spawn timeout + kill).
* ``FAKE_CLAUSTRUM_NO_LISTEN=1``     — daemonize but never bind the socket (models
  a daemon that comes up but never accepts a connection → poll timeout).
* ``FAKE_CLAUSTRUM_BAD_TOKEN=1``     — the daemon serves with the wrong token, so
  every authed request is rejected (models a token mismatch after spawn).
* ``FAKE_CLAUSTRUM_VERSION=<s>``     — the version reported by ``server.version``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time


def _read_token(fd: int) -> str:
    """Read the auth token from ``fd`` to EOF and strip a trailing newline."""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8").rstrip("\r\n")


def _serve(socket_path: str, token: str, version: str) -> None:
    """Run the AF_UNIX JSON-RPC server until ``server.shutdown`` or a signal."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            try:
                req = json.loads(raw)
            except ValueError:
                continue
            rid = req.get("id")
            method = req.get("method")
            if req.get("auth") != token:
                resp = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32001, "message": "Unauthorized"},
                }
            elif method == "server.ping":
                resp = {"jsonrpc": "2.0", "id": rid, "result": {"pong": True}}
            elif method == "server.version":
                resp = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"version": version, "platform": "linux", "arch": "amd64"},
                }
            elif method == "server.shutdown":
                os._exit(0)
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": "Unknown method"},
                }
            writer.write((json.dumps(resp) + "\n").encode("utf-8"))
            await writer.drain()

    async def main() -> None:
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(handle, path=socket_path)
        async with server:
            await server.serve_forever()

    asyncio.run(main())


def _arg_value(name: str) -> str | None:
    """Return the value following ``name`` in argv, or ``None``."""
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
    return None


def main() -> None:
    """Read the token, self-daemonize, and serve (honoring the env knobs)."""
    socket_path = _arg_value("-socket") or ""
    token_fd = int(_arg_value("-token-fd") or "0")
    version = os.environ.get("FAKE_CLAUSTRUM_VERSION", "fake-claustrum-bin-0")

    token = _read_token(token_fd)

    fail = os.environ.get("FAKE_CLAUSTRUM_EXIT")
    if fail is not None:
        sys.exit(int(fail))

    if os.environ.get("FAKE_CLAUSTRUM_HANG_LAUNCHER") == "1":
        time.sleep(60)  # never detach, so the caller's spawn wait times out

    if os.environ.get("FAKE_CLAUSTRUM_BAD_TOKEN") == "1":
        token += "-mismatch"  # serve with a token the caller will not match

    # Self-daemonize: the launcher (this process) exits 0 once the child is
    # reparented to init, exactly like the real `claustrum -serve`.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()

    # Record the PID early (before any branch) so a test can always reap the
    # detached child as a backstop, even in the never-bind mode below.
    with open(socket_path + ".pid", "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    if os.environ.get("FAKE_CLAUSTRUM_NO_LISTEN") == "1":
        time.sleep(60)  # come up but never bind, so the client's poll times out
        return

    _serve(socket_path, token, version)


if __name__ == "__main__":
    main()
