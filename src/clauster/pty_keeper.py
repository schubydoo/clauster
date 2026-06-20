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
from dataclasses import dataclass
from pathlib import Path

from . import procutil

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
        tmp.write_text(json.dumps(data), encoding="utf-8")
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
            # Scrub Clauster secrets at the layer that directly spawns the
            # project-code bridge, so it stays secret-free however the keeper
            # was launched (the launcher scrubs too — defense in depth).
            env=procutil.child_env(),
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
            # The connect URL never appeared. Stop accumulating an unbounded buffer.
            url_found = True
            buf = bytearray()
            # A bridge still alive past the URL timeout is connected and usable: the
            # connect URL is a deep-link nicety, not a liveness signal. Publish "ready"
            # (URL stays null if unseen) so Clauster promotes it to RUNNING instead of
            # false-ERRORing a healthy bridge. This covers BOTH a `--continue` resume
            # (which reconnects without re-printing the URL) AND a fresh start on a
            # newer claude build (>2.1.161) that connects without ever printing the
            # claude.ai/code/session_… line the scrape depends on. A bridge that
            # genuinely failed to start has already exited, so the loop has broken; we
            # only reach here while proc.poll() is None — i.e. the bridge is alive.
            if base.get("state") == "starting":
                base["state"] = "ready"
                _write_sidecar(sidecar, base)

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


# --- orphan-keeper management (#301) -----------------------------------------
#
# A keeper is a detached process that outlives a Clauster restart. The normal
# stop path (runner.stop -> _cleanup_keeper) covers a keeper still attached to a
# project card. But if the card is gone (the project was removed), no card can
# show or stop it — it is invisible and unkillable. These helpers let a sweep
# (the `clauster keepers` CLI) list and stop such orphans from the sidecar files
# alone, with no running runner.

# A keeper sidecar is `<name>-<ms>-<seq>.keeper.json`; this reverses the stem to
# the project name for display only — orphan classification uses the same
# forward glob the runner uses (see find_orphan_keepers), never this parse.
_KEEPER_STEM_RE = re.compile(r"^(?P<name>.+)-\d+-\d+$")
_KEEPER_SUFFIX = ".keeper.json"
_KEEPER_START_TOLERANCE = 2.0  # PID-reuse guard slack, matching runner's bridge tolerance


@dataclass(frozen=True)
class KeeperInfo:
    """A discovered keeper sidecar plus its derived liveness/identity (#301)."""

    sidecar: Path
    project: str | None
    keeper_pid: int | None
    bridge_pid: int | None
    session_id: str | None
    state: str | None
    alive: bool
    keeper_create_time: float | None  # the keeper PID's create-time (PID-reuse guard)


def _read_sidecar(path: Path) -> dict:
    """Read a keeper sidecar, tolerating absent / mid-write / invalid files (-> {})."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _project_from_sidecar(filename: str) -> str | None:
    """Best-effort project name from a `<name>-<ms>-<seq>.keeper.json` filename."""
    stem = filename[: -len(_KEEPER_SUFFIX)] if filename.endswith(_KEEPER_SUFFIX) else filename
    m = _KEEPER_STEM_RE.match(stem)
    return m.group("name") if m else None


def _int_or_none(value: object) -> int | None:
    # ``bool`` is a subclass of ``int``, so a corrupt sidecar carrying e.g.
    # ``"keeper_pid": true`` would otherwise resolve to PID 1. Exclude it,
    # matching the convention in procutil.py.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def iter_keepers(log_dir: Path) -> list[KeeperInfo]:
    """Discover every `*.keeper.json` sidecar under ``log_dir`` with liveness (#301)."""
    try:
        files = sorted(log_dir.glob(f"*{_KEEPER_SUFFIX}"))
    except OSError:
        return []
    out: list[KeeperInfo] = []
    for f in files:
        data = _read_sidecar(f)
        keeper_pid = _int_or_none(data.get("keeper_pid"))
        create_time = procutil.proc_create_time(keeper_pid) if keeper_pid is not None else None
        state = data.get("state")
        session_id = data.get("session_id")
        out.append(
            KeeperInfo(
                sidecar=f,
                project=_project_from_sidecar(f.name),
                keeper_pid=keeper_pid,
                bridge_pid=_int_or_none(data.get("bridge_pid")),
                session_id=session_id if isinstance(session_id, str) else None,
                state=state if isinstance(state, str) else None,
                # A live PID alone isn't enough: confirm its cmdline is still our keeper,
                # so a PID the keeper left behind and the OS recycled onto an unrelated
                # process is never listed as a live orphan (#301 / RUNOPS-1). The leading
                # `keeper_pid is not None` also narrows the type for is_keeper_process.
                alive=keeper_pid is not None
                and create_time is not None
                and procutil.is_keeper_process(keeper_pid),
                keeper_create_time=create_time,
            )
        )
    return out


def find_orphan_keepers(log_dir: Path, carded_projects: set[str]) -> list[KeeperInfo]:
    """Return live keepers whose sidecar belongs to no current project card (#301).

    Classification uses the same forward glob the runner uses to attach a keeper
    to a project (``<project>-*.keeper.json``), so it can't mis-attribute a
    keeper via an ambiguous filename reverse-parse. A dead keeper is not an
    orphan (nothing to stop).
    """
    carded_files: set[Path] = set()
    protected_names: set[str] = set()
    for project in carded_projects:
        try:
            carded_files.update(log_dir.glob(f"{project}-*{_KEEPER_SUFFIX}"))
        except OSError:
            # Can't enumerate this project's sidecars — protect it by parsed name
            # instead, so a live carded keeper can never surface as an orphan. This
            # over-protects (a name parse is approximate), never under-protects.
            protected_names.add(project)
    return [
        k
        for k in iter_keepers(log_dir)
        if k.alive and k.sidecar not in carded_files and k.project not in protected_names
    ]


def stop_keeper(keeper_pid: int, *, expect_create_time: float | None = None) -> bool:
    """Stop a keeper process (graceful reap, then force-kill its whole tree).

    Returns True once the process is gone. The reap loop is a no-op for a keeper
    that is not the caller's child (the CLI is a separate process), so the force
    path is what actually stops a detached orphan and its bridge subtree.

    ``expect_create_time`` (the create-time captured when the keeper was
    classified) is a PID-reuse guard: if, after the grace window, the PID's
    create-time no longer matches, the original keeper already exited and the PID
    was recycled onto an unrelated process — refuse the SIGKILL rather than kill a
    stranger.
    """
    for _ in range(8):  # ~2s grace, mirroring runner._cleanup_keeper
        procutil.reap_if_exited(keeper_pid)
        if procutil.proc_create_time(keeper_pid) is None:
            return True
        time.sleep(0.25)
    if expect_create_time is not None:
        current = procutil.proc_create_time(keeper_pid)
        if current is None:
            return True  # exited during the grace window — nothing left to kill
        if abs(current - expect_create_time) > _KEEPER_START_TOLERANCE:
            return False  # PID reused onto another process — do not kill it
    # Final cmdline re-verify right before the SIGKILL (TOCTOU): the PID must still be
    # our keeper. If it exited and the OS recycled the PID onto an unrelated process
    # during the grace window, never hard-kill that stranger (#301 / RUNOPS-1).
    if not procutil.is_keeper_process(keeper_pid):
        return True  # keeper already gone (or PID recycled to a non-keeper)
    procutil.force_kill_tree(keeper_pid)
    # SIGKILL is asynchronous: the process may still be running/zombie for a beat
    # after force_kill_tree returns, so poll briefly (reaping our own child if it is
    # one) before reporting the outcome rather than racing the kill.
    for _ in range(10):
        procutil.reap_if_exited(keeper_pid)
        current = procutil.proc_create_time(keeper_pid)
        if current is None:
            return True
        if (
            expect_create_time is not None
            and abs(current - expect_create_time) > _KEEPER_START_TOLERANCE
        ):
            return True  # PID recycled onto a new process → the keeper we killed is gone
        time.sleep(0.1)
    return False


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    sys.exit(main())
