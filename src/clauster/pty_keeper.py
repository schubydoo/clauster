"""PTY keeper — the sidecar that owns a true-resume bridge's pseudo-terminal.

The ``claude --remote-control`` *flag* form (main command, not the
``remote-control`` subcommand) is the only entrypoint that genuinely restores
prior conversation context on restart (via ``--continue`` / ``--resume``). But
it is an interactive single session: spawned detached with no controlling
terminal it dies within seconds. So Clauster runs it under a real PTY.

This module is that PTY holder. Clauster launches it **detached**
(``start_new_session=True``), exactly like it launches a subcommand bridge, so
the keeper outlives a Clauster restart; the keeper in turn owns the bridge's
controlling terminal, so closing it would hang the bridge up — which is why the
keeper, not Clauster, must hold the master fd. The keeper:

1. opens a PTY and makes the slave its session's controlling terminal,
2. execs the bridge argv (passed after ``--``) with that PTY,
3. writes a JSON *sidecar* file that Clauster polls for the bridge pid and the
   ``claude.ai/code/session_<id>`` connect URL (discovery stays file-based, the
   same shape as the subcommand path's ``--debug-file`` marker parse),
4. drains the master to ``/dev/null`` so the bridge's writes never block, and
5. exits when the bridge exits.

Discovery is file-based and stop is a signal to the bridge pid, so Clauster
needs no socket to the keeper: a restarted Clauster re-reads the sidecar and
stops the bridge with ``SIGINT`` (twice — see :mod:`clauster.runner`). POSIX
only; Windows has no ``pty`` and keeps the subcommand / recap path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

# The flag form prints its connect URL as a session path, NOT the subcommand's
# `?environment=env_<ULID>` query form (verified, claude 2.1.159).
_RE_CONNECT_URL = re.compile(rb"https?://claude\.ai/code/(session_[A-Za-z0-9]+)")

# How long to wait for the bridge to print its connect URL before giving up on
# discovery (the bridge stays running regardless; this only bounds URL capture).
_URL_TIMEOUT = 30.0


def _write_sidecar(path: Path, data: dict[str, object]) -> None:
    """Atomically write the sidecar JSON so a polling reader never sees a partial file."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError:
        # Best-effort: a transient write failure must not take the bridge down.
        pass


def _proc_start(pid: int) -> float | None:
    """Return the bridge's process start time for Clauster's PID-reuse defense, or None."""
    # Imported lazily so the keeper still runs if procutil grows heavier deps.
    try:
        from clauster import procutil

        return procutil.proc_create_time(pid)
    except Exception:  # noqa: BLE001 — discovery aid only; never fatal
        return None


def run_keeper(bridge_argv: list[str], sidecar: Path, cwd: str | None = None) -> int:
    """Spawn ``bridge_argv`` under a PTY, publish a discovery sidecar, drain, and wait.

    Returns the bridge's exit status. Any setup failure is recorded in the
    sidecar (``state: "error"``) and returned as a non-zero code rather than
    raised, so the launching process always gets a diagnosable result.
    """
    import fcntl
    import pty
    import termios

    keeper_pid = os.getpid()
    base: dict[str, object] = {
        "keeper_pid": keeper_pid,
        "bridge_pid": None,
        "bridge_proc_start": None,
        "connect_url": None,
        "session_id": None,
        "state": "starting",
        "error": None,
    }
    _write_sidecar(sidecar, base)

    try:
        master, slave = pty.openpty()
        slave_name = os.ttyname(slave)
    except OSError as exc:
        base.update(state="error", error=f"openpty failed: {exc}")
        _write_sidecar(sidecar, base)
        return 70

    def _acquire_ctty() -> None:  # pragma: no cover — runs in the forked child, pre-exec
        # Make the bridge its own session leader FIRST: TIOCSCTTY only works for a
        # session leader with no controlling terminal. Without this setsid the
        # bridge stays in the launcher's session and never acquires the PTY as its
        # controlling terminal (the interactive flag form needs a real one).
        os.setsid()
        fd = os.open(slave_name, os.O_RDWR)
        try:
            fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
        except OSError:
            pass
        os.close(fd)

    try:
        proc = subprocess.Popen(
            bridge_argv,
            cwd=cwd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=_acquire_ctty,
            close_fds=True,
        )
    except OSError as exc:
        os.close(master)
        os.close(slave)
        base.update(state="error", error=f"spawn failed: {exc}")
        _write_sidecar(sidecar, base)
        return 71
    os.close(slave)  # the child holds its own dup; we only need the master
    # Now that the bridge has been exec'd (it does NOT inherit this), make the
    # keeper itself impervious to a stray terminal hangup so it reliably outlives
    # a Clauster restart and keeps the bridge's master fd open.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    base["bridge_pid"] = proc.pid
    base["bridge_proc_start"] = _proc_start(proc.pid)
    _write_sidecar(sidecar, base)

    flags = fcntl.fcntl(master, fcntl.F_GETFL)
    fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    buf = bytearray()
    url_found = False
    url_deadline = time.monotonic() + _URL_TIMEOUT
    while proc.poll() is None:
        try:
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                chunk = os.read(master, 65536)
                if chunk and not url_found:
                    buf.extend(chunk)
                    hit = _RE_CONNECT_URL.search(buf)
                    if hit is not None:
                        session_id = hit.group(1).decode()
                        base.update(
                            connect_url=f"https://claude.ai/code/{session_id}",
                            session_id=session_id,
                            state="ready",
                        )
                        _write_sidecar(sidecar, base)
                        url_found = True
                        buf = bytearray()  # keep draining, stop accumulating
        except (OSError, BlockingIOError):
            time.sleep(0.2)
        if not url_found and time.monotonic() > url_deadline:
            # Stop accumulating an unbounded buffer if the URL never appears; the
            # bridge keeps running and Clauster's startup-watch decides its fate.
            url_found = True
            buf = bytearray()

    rc = proc.poll() or 0
    base.update(state="exited", bridge_exit=rc)
    _write_sidecar(sidecar, base)
    try:
        os.close(master)
    except OSError:
        pass
    return rc


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``python -m clauster.pty_keeper --sidecar P -- <bridge argv>``."""
    parser = argparse.ArgumentParser(prog="clauster.pty_keeper", description=__doc__)
    parser.add_argument("--sidecar", required=True, type=Path, help="JSON discovery file to write")
    parser.add_argument("--cwd", default=None, help="working directory for the bridge")
    parser.add_argument(
        "bridge_argv",
        nargs=argparse.REMAINDER,
        help="the bridge command line, after a `--` separator",
    )
    ns = parser.parse_args(argv)
    bridge_argv = ns.bridge_argv
    if bridge_argv and bridge_argv[0] == "--":
        bridge_argv = bridge_argv[1:]
    if not bridge_argv:
        parser.error("no bridge argv given after `--`")
    return run_keeper(bridge_argv, ns.sidecar, cwd=ns.cwd)


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    sys.exit(main())
