#!/usr/bin/env python3
"""Spawnable fake claustrum daemon for connect-or-spawn tests (CL-2).

Mimics the parts of ``claustrum -serve -socket <path>`` that
:mod:`clauster.claustrum_daemon` depends on: it reads the auth token (from
``-token-fd`` on POSIX, or the read-then-unlinked ``-token-file`` on Windows),
self-daemonizes, and then serves a minimal auth-gated NDJSON JSON-RPC subset
(``server.ping`` / ``server.version`` / ``server.shutdown``).

* **POSIX** — ``fork`` + ``setsid``; the launcher process exits 0 once the real
  daemon is reparented to ``init`` and serves the ``AF_UNIX`` socket.
* **Windows** — there is no ``fork``; the launcher re-execs a detached child
  (``DETACHED_PROCESS``) and exits 0, mirroring how the real ``claustrum``
  self-daemonizes. The child serves claustrum's opt-in named-pipe transport and
  advertises the chosen pipe name via ``<socket-dir>/rpc.pipe`` (the pipe analogue
  of the real daemon's ``-listen-pipe``), so the Windows
  :class:`~clauster.claustrum_client.ClaustrumClient` discovers and dials it.

The detached daemon writes its own PID to ``<socket>.pid`` so a test can reap it
as a backstop if the ``server.shutdown`` RPC does not land. On Windows the child's
stdout/stderr go to ``<socket>.childlog`` (a detached process has no console).

Behaviour knobs (env vars), used to exercise the failure paths:

* ``FAKE_CLAUSTRUM_EXIT=<n>``        — the launcher exits ``<n>`` *before*
  daemonizing (models a daemon that fails to start).
* ``FAKE_CLAUSTRUM_HANG_LAUNCHER=1`` — the launcher blocks instead of detaching, so
  it never exits (models a launcher that hangs → spawn timeout + kill).
* ``FAKE_CLAUSTRUM_NO_LISTEN=1``     — daemonize but never bind/advertise (models a
  daemon that comes up but never accepts a connection → poll timeout).
* ``FAKE_CLAUSTRUM_BAD_TOKEN=1``     — the daemon serves with the wrong token, so
  every authed request is rejected (models a token mismatch after spawn).
* ``FAKE_CLAUSTRUM_VERSION=<s>``     — the version reported by ``server.version``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Env set on the re-exec'd Windows child so it serves instead of re-launching, plus
# the auth token handed to it out-of-band (its stdin is DEVNULL, so it can't inherit
# the token over fd 0 the way the POSIX fork child does).
_WIN_CHILD_ENV = "_FAKE_CLAUSTRUM_WIN_CHILD"
_WIN_TOKEN_ENV = "_FAKE_CLAUSTRUM_TOKEN"


def _read_token(fd: int) -> str:
    """Read the auth token from ``fd`` to EOF and strip a trailing newline."""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8").rstrip("\r\n")


def _make_handler(token: str, version: str):
    """Build the ``(reader, writer)`` coroutine that serves the auth-gated RPC subset."""

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
                    "result": {"version": version, "platform": sys.platform, "arch": "amd64"},
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

    return handle


def _serve_unix(socket_path: str, token: str, version: str) -> None:
    """Run the AF_UNIX JSON-RPC server until ``server.shutdown`` or a signal."""
    handle = _make_handler(token, version)

    async def main() -> None:
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(handle, path=socket_path)
        async with server:
            await server.serve_forever()

    asyncio.run(main())


def _serve_pipe(socket_path: str, token: str, version: str) -> None:
    """Serve the RPC subset over a Windows named pipe and advertise it via rpc.pipe."""
    handle = _make_handler(token, version)
    pipe_name = rf"\\.\pipe\clauster-fakebin-{os.getpid()}"

    async def main() -> None:
        loop = asyncio.get_running_loop()

        def factory() -> asyncio.StreamReaderProtocol:
            reader = asyncio.StreamReader(loop=loop)
            return asyncio.StreamReaderProtocol(reader, handle, loop=loop)

        # start_serving_pipe lives only on the win32 ProactorEventLoop.
        proactor: Any = loop
        [server] = await proactor.start_serving_pipe(factory, pipe_name)
        # Advertise the pipe like the real -listen-pipe daemon (claustrum #134); only
        # now is the daemon connectable, so a client poll before this sees "unreachable".
        Path(socket_path).parent.joinpath("rpc.pipe").write_text(pipe_name, encoding="utf-8")
        try:
            while True:
                await asyncio.sleep(3600)
        finally:  # pragma: no cover - only on loop teardown, which os._exit pre-empts
            server.close()

    asyncio.run(main())


def _arg_value(name: str) -> str | None:
    """Return the value following ``name`` in argv, or ``None``."""
    argv = sys.argv
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _launch_windows_child(socket_path: str) -> None:
    """Re-exec a detached child that serves the pipe, mirroring claustrum's DETACHED_PROCESS.

    The token reaches the child through the environment (its stdin is ``DEVNULL``);
    its stdout/stderr go to ``<socket>.childlog`` because a detached process has no
    console to inherit. We do not wait — the child outlives us, and this launcher's
    clean exit(0) is what the daemon's ``_spawn`` waits on (the Windows analogue of
    the POSIX fork-parent exit).
    """
    log = open(socket_path + ".childlog", "w", encoding="utf-8")  # noqa: SIM115 - child owns it
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "-socket", socket_path],
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        env=os.environ.copy(),
        close_fds=True,
    )


def _windows_child_main(socket_path: str, version: str) -> None:
    """Entry for the detached Windows child: drop the PID file, then serve (or hang)."""
    token = os.environ[_WIN_TOKEN_ENV]
    # Record the PID early (before any branch) so a test can always reap us, even in
    # the never-listen mode below.
    Path(socket_path + ".pid").write_text(str(os.getpid()), encoding="utf-8")
    if os.environ.get("FAKE_CLAUSTRUM_NO_LISTEN") == "1":
        time.sleep(60)  # come up but never advertise rpc.pipe → the client "never accepts"
        return
    _serve_pipe(socket_path, token, version)


def main() -> None:
    """Read the token, self-daemonize, and serve (honoring the env knobs)."""
    socket_path = _arg_value("-socket") or ""
    version = os.environ.get("FAKE_CLAUSTRUM_VERSION", "fake-claustrum-bin-0")

    # Second entry point: we are the re-exec'd Windows child; the token is in the env.
    if os.environ.get(_WIN_CHILD_ENV) == "1":
        _windows_child_main(socket_path, version)
        return

    token_file = _arg_value("-token-file")
    if token_file is not None:
        # Windows handoff: read-then-unlink, mirroring the real claustrum daemon.
        token = Path(token_file).read_text(encoding="utf-8").rstrip("\r\n")
        Path(token_file).unlink(missing_ok=True)
    else:
        token = _read_token(int(_arg_value("-token-fd") or "0"))

    fail = os.environ.get("FAKE_CLAUSTRUM_EXIT")
    if fail is not None:
        sys.exit(int(fail))

    if os.environ.get("FAKE_CLAUSTRUM_HANG_LAUNCHER") == "1":
        time.sleep(60)  # never detach, so the caller's spawn wait times out

    if os.environ.get("FAKE_CLAUSTRUM_BAD_TOKEN") == "1":
        token += "-mismatch"  # serve with a token the caller will not match

    if sys.platform == "win32":
        # No fork on Windows: hand the token to a detached child via the env, re-exec
        # it, and exit 0 (the real claustrum daemonizes the same way).
        os.environ[_WIN_CHILD_ENV] = "1"
        os.environ[_WIN_TOKEN_ENV] = token
        _launch_windows_child(socket_path)
        return

    # POSIX self-daemonize: the launcher (this process) exits 0 once the child is
    # reparented to init, exactly like the real `claustrum -serve`.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()

    # Record the PID early (before any branch) so a test can always reap the detached
    # child as a backstop, even in the never-bind mode below.
    with open(socket_path + ".pid", "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    if os.environ.get("FAKE_CLAUSTRUM_NO_LISTEN") == "1":
        time.sleep(60)  # come up but never bind, so the client's poll times out
        return

    _serve_unix(socket_path, token, version)


if __name__ == "__main__":
    main()
