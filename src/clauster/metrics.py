"""Live per-bridge resource metrics (CPU / memory / disk) for the dashboard.

A point-in-time sample over the process tree rooted at a bridge's PID — the
runtime sibling of ``usage.py`` (which reports cumulative token/cost). Per-process
**network** I/O is deliberately out of scope for v1: psutil has no per-process
counter (``net_io_counters`` is system-wide), so it needs ``/proc/<pid>/net``
parsing or eBPF — tracked for a v2.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

import psutil

_log = logging.getLogger("clauster.metrics")

# Errors meaning "this process is gone / not inspectable" — treated as skip.
_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess)
# io_counters is Linux/Windows-only; macOS (and some sandboxes) raise.
_IO_UNSUPPORTED = (AttributeError, NotImplementedError, *_GONE)


def _io_bytes(proc: psutil.Process) -> tuple[int, int] | None:
    """Return ``(read_bytes, write_bytes)`` for ``proc``, or None where unsupported."""
    try:
        io = proc.io_counters()
        return io.read_bytes, io.write_bytes
    except _IO_UNSUPPORTED:
        return None


def _session_owners(
    root_pid: int, parent: dict[int, int], session_roots: set[int]
) -> dict[int, int]:
    """Map each process of the bridge tree to the session root that owns it.

    ``parent`` is the pid → ppid map read during the first snapshot. A process belongs to
    the NEAREST session root on its ancestor chain, so a session started inside another
    session counts once, under itself, and the per-session figures never overlap. A
    process with no session root above it (the bridge itself, a helper the bridge started)
    belongs to none. The walk is bounded by the map size, so a ppid loop built from a
    recycled pid cannot spin.
    """
    owners: dict[int, int] = {}
    for start in parent:
        cur = start
        for _ in range(len(parent) + 1):
            if cur in session_roots:
                owners[start] = cur
                break
            if cur == root_pid or cur not in parent:
                break
            cur = parent[cur]
    return owners


def sample_tree(
    pid: int,
    *,
    interval: float = 0.15,
    normalize_cpu: bool = False,
    sessions: Mapping[int, str],
) -> dict | None:
    """Sample CPU% / RSS / disk-I/O rate over the process tree rooted at ``pid``.

    Walks ``pid`` plus all descendants (a bridge spawns a ``claude`` process tree),
    takes two snapshots ``interval`` seconds apart, and returns aggregate figures —
    or ``None`` if ``pid`` is already gone. ``cpu_percent`` is summed across the
    tree and may exceed 100% on multiple cores; with ``normalize_cpu`` it is divided
    by the host core count instead (0–100% of the machine) and ``cpu_normalized`` is
    set. ``rss_bytes`` is summed (shared pages are slightly double-counted). The
    ``disk_*`` fields are ``None`` on platforms without ``io_counters`` (e.g. macOS).

    On a host whose procfs has no ``btime`` line the descendant walk cannot run, so
    the sample degrades to the root process alone and reports ``procs`` as 1. A debug
    line records that, because ``procs: 1`` otherwise reads like a bridge that
    genuinely has no children.

    ``sessions`` maps a session's own pid (as ``claude agents --json`` names it) to the
    key it is reported under. The returned ``sessions`` dict carries, per key, the
    ``pid``/``procs``/``cpu_percent``/``rss_bytes`` of that session's subtree, measured in
    the SAME two snapshots as the bridge total, so each figure is a part of it. A session
    is reported only when its pid is a strict descendant of ``pid`` in this walk; a pid
    outside the tree, equal to the root, or gone before the second snapshot is left out
    rather than guessed. Pass an empty mapping when no breakdown is wanted.

    Blocks for ``interval`` seconds — call it off the event loop
    (``asyncio.to_thread``), as the dashboard endpoint does.
    """
    try:
        root = psutil.Process(pid)
    except _GONE:
        return None
    try:
        procs = [root, *root.children(recursive=True)]
    except _GONE:
        return None
    except RuntimeError:
        # `children()` reads the epoch (`create_time()` without `monotonic`), which ends in
        # `boot_time()` and raises a bare RuntimeError on a procfs with no `btime` line
        # (gVisor, WSL1, some container runtimes; #1429). The descendants are what is
        # unreadable there, not the root — sample the root's own CPU and memory rather than
        # failing the whole request. `procutil.proc_create_time` and its predicate family
        # absorb the same `RuntimeError` on the same terms. Logged because `RuntimeError` is
        # broad: any other psutil RuntimeError also lands here, and the debug line is what
        # tells a real root-only sample from a masked one afterwards.
        _log.debug(
            "host cannot express boot time; metrics sample for pid %s degrades to root only", pid
        )
        procs = [root]

    # First snapshot: cumulative cpu time + io byte counters, keyed by pid. The ppid is read
    # only when a per-session breakdown is asked for, so a plain sample costs what it did.
    first: dict[int, tuple[float, tuple[int, int] | None]] = {}
    parent: dict[int, int] = {}
    for p in procs:
        try:
            ct = p.cpu_times()
            first[p.pid] = (ct.user + ct.system, _io_bytes(p))
            if sessions and p.pid != pid:
                parent[p.pid] = p.ppid()
        except _GONE:
            continue

    time.sleep(interval)

    cpu_delta = 0.0
    rss = 0
    read_delta = write_delta = 0
    disk_ok = False
    measured: dict[int, tuple[float, int]] = {}  # pid → (cpu delta, rss), for the breakdown
    for p in procs:
        prior = first.get(p.pid)
        if prior is None:  # appeared after the first snapshot, or already gone
            continue
        try:
            ct = p.cpu_times()
            p_cpu = (ct.user + ct.system) - prior[0]
            p_rss = p.memory_info().rss
        except _GONE:  # exited mid-sample — drop it
            continue
        cpu_delta += p_cpu
        rss += p_rss
        measured[p.pid] = (p_cpu, p_rss)
        io_now = _io_bytes(p)
        if prior[1] is not None and io_now is not None:
            read_delta += max(0, io_now[0] - prior[1][0])
            write_delta += max(0, io_now[1] - prior[1][1])
            disk_ok = True

    cores = (psutil.cpu_count() or 1) if normalize_cpu else 1

    def _pct(delta: float) -> float:
        return round(round(max(0.0, delta) / interval * 100, 1) / cores, 1)

    # A session root must be measured in BOTH snapshots: one that exited mid-sample gets
    # no figure at all, not a partial one made of the children it left behind.
    roots = {sp for sp in sessions if sp in measured and sp in parent}
    owners = _session_owners(pid, parent, roots) if roots else {}
    per_session: dict[str, dict] = {}
    for sp in roots:
        members = [q for q, owner in owners.items() if owner == sp and q in measured]
        per_session[sessions[sp]] = {
            "pid": sp,
            "procs": len(members),
            "cpu_percent": _pct(sum(measured[q][0] for q in members)),
            "rss_bytes": sum(measured[q][1] for q in members),
        }
    return {
        "procs": len(procs),
        "cpu_percent": _pct(cpu_delta),
        "cpu_normalized": normalize_cpu,
        "rss_bytes": rss,
        "disk_read_bps": round(read_delta / interval) if disk_ok else None,
        "disk_write_bps": round(write_delta / interval) if disk_ok else None,
        "sessions": per_session,
    }
