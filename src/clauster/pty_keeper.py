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

# Cap for the optional live-terminal capture file (#534). The keeper drains the
# PTY master regardless (so the bridge never blocks); when a capture path is given
# it also writes those bytes here for the read-only `/ws/pty-terminal` tail. The
# file is a *live frame buffer*, not an archive — once it exceeds this size it is
# truncated back to its tail, so an unbounded session can't fill the disk. Sized to
# comfortably hold a terminal's worth of recent frames.
_PTY_LOG_MAX_BYTES = 1024 * 1024  # 1 MiB
# How much of the tail to keep when the capture file is truncated.
_PTY_LOG_KEEP_BYTES = 256 * 1024  # 256 KiB


def _write_sidecar(path: Path, data: dict[str, object]) -> None:
    """Atomically write the sidecar JSON so a polling reader never sees a partial file."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Best-effort: a transient write failure must not take the bridge down.
        pass


class _PtyCapture:
    """Append PTY-master bytes to a size-bounded live-terminal capture file (#534).

    The capture backs the read-only ``/ws/pty-terminal`` tail: a *live frame buffer*,
    not an archive. Writes are best-effort — a capture failure must never take the
    bridge down (the keeper drains the master regardless), so every FS error is
    swallowed and capture goes dormant for the rest of the session. The file is
    created ``0600`` from a fresh inode (``O_CREAT | O_EXCL``) so the raw terminal
    output — which can echo secrets/command output — is owner-only, matching the
    bridge's ``--debug-file`` posture; the WS layer redacts each line again in-flight.
    """

    def __init__(self, path: Path) -> None:
        """Open ``path`` ``0600`` for capture; go dormant if it can't be opened."""
        self._path = path
        self._fd: int | None = None
        self._written = 0
        try:
            # O_EXCL refuses a pre-planted symlink at this per-spawn-unique path and
            # guarantees a fresh 0600 inode (so the raw capture is never briefly
            # group/world-readable the way touch()+chmod would leave it).
            self._fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            self._fd = None  # capture unavailable → the tail simply 1008s; bridge unaffected

    def write(self, chunk: bytes) -> None:
        """Append ``chunk``, truncating back to the tail once the cap is exceeded."""
        if self._fd is None or not chunk:
            return
        try:
            os.write(self._fd, chunk)
            self._written += len(chunk)
            if self._written > _PTY_LOG_MAX_BYTES:
                self._truncate_tail()
        except OSError:
            self._close()  # capture broke → go dormant; never propagate to the bridge

    def _truncate_tail(self) -> None:
        """Rewrite the file to only its trailing ``_PTY_LOG_KEEP_BYTES`` and continue."""
        try:
            with open(self._path, "rb") as fh:
                fh.seek(-_PTY_LOG_KEEP_BYTES, os.SEEK_END)
                tail = fh.read()
        except OSError:
            self._close()
            return
        # A truncate can split a multibyte char or a redaction-relevant token across
        # the new head; drop to the first newline so the tail starts on a line boundary
        # (the WS reader buffers whole lines before redacting).
        nl = tail.find(b"\n")
        if nl != -1:
            tail = tail[nl + 1 :]
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)  # type: ignore[arg-type]
            os.ftruncate(self._fd, 0)  # type: ignore[arg-type]
            os.write(self._fd, tail)  # type: ignore[arg-type]
            self._written = len(tail)
        except OSError:
            self._close()

    def _close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def close(self) -> None:
        """Close the capture fd (idempotent)."""
        self._close()


def _proc_start(pid: int) -> float | None:
    """Return the bridge's process start time for Clauster's PID-reuse defense, or None."""
    # Imported lazily so the keeper still runs if procutil grows heavier deps.
    try:
        from clauster import procutil

        return procutil.proc_create_time(pid)
    except Exception:  # noqa: BLE001 — discovery aid only; never fatal
        return None


def run_keeper(
    bridge_argv: list[str],
    sidecar: Path,
    cwd: str | None = None,
    pty_log: Path | None = None,
) -> int:
    """Spawn ``bridge_argv`` under a PTY, publish a discovery sidecar, drain, and wait.

    Returns the bridge's exit status. Any setup failure is recorded in the
    sidecar (``state: "error"``) and returned as a non-zero code rather than
    raised, so the launching process always gets a diagnosable result.

    When ``pty_log`` is given the keeper also mirrors the drained PTY-master bytes
    to that size-bounded file (#534), backing the read-only ``/ws/pty-terminal``
    live-view. Capture is best-effort and never affects the bridge.
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

    # poll(), not select(): the stdlib select() call raises "filedescriptor out of range"
    # once the master fd is >= FD_SETSIZE (1024) — a long-lived Clauster (many bridges/keepers,
    # a busy host, or accumulated fds) can reach that and crash the keeper. poll() has no
    # such ceiling. (This also fixed a -n0 full-suite test flake where the pty master fd
    # crept past 1024.)
    poller = select.poll()
    poller.register(master, select.POLLIN)

    capture = _PtyCapture(pty_log) if pty_log is not None else None

    buf = bytearray()
    url_found = False
    url_deadline = time.monotonic() + _URL_TIMEOUT
    while proc.poll() is None:
        try:
            if poller.poll(500):  # ms; truthy when the master fd has data (no FD_SETSIZE limit)
                chunk = os.read(master, 65536)
                if chunk and capture is not None:
                    # Mirror the live terminal frames for the read-only `/ws/pty-terminal`
                    # tail (#534). Independent of the URL scrape below, which stops once
                    # the connect URL is found — the capture must keep running for the
                    # whole session so the live view stays current.
                    capture.write(chunk)
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
    if capture is not None:
        capture.close()
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
        "--pty-log",
        default=None,
        type=Path,
        help="size-bounded file to mirror the live terminal output to (read-only view)",
    )
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
    return run_keeper(bridge_argv, ns.sidecar, cwd=ns.cwd, pty_log=ns.pty_log)


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
