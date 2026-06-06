"""Live per-bridge resource metrics (CPU / memory / disk) for the dashboard.

A point-in-time sample over the process tree rooted at a bridge's PID — the
runtime sibling of ``usage.py`` (which reports cumulative token/cost). Per-process
**network** I/O is deliberately out of scope for v1: psutil has no per-process
counter (``net_io_counters`` is system-wide), so it needs ``/proc/<pid>/net``
parsing or eBPF — tracked for a v2.
"""

from __future__ import annotations

import time

import psutil

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


def sample_tree(pid: int, *, interval: float = 0.15) -> dict | None:
    """Sample CPU% / RSS / disk-I/O rate over the process tree rooted at ``pid``.

    Walks ``pid`` plus all descendants (a bridge spawns a ``claude`` process tree),
    takes two snapshots ``interval`` seconds apart, and returns aggregate figures —
    or ``None`` if ``pid`` is already gone. ``cpu_percent`` is summed across the
    tree and may exceed 100% on multiple cores. ``rss_bytes`` is summed (shared
    pages are slightly double-counted). The ``disk_*`` fields are ``None`` on
    platforms without ``io_counters`` (e.g. macOS).

    Blocks for ``interval`` seconds — call it off the event loop
    (``asyncio.to_thread``), as the dashboard endpoint does.
    """
    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except _GONE:
        return None

    # First snapshot: cumulative cpu time + io byte counters, keyed by pid.
    first: dict[int, tuple[float, tuple[int, int] | None]] = {}
    for p in procs:
        try:
            ct = p.cpu_times()
            first[p.pid] = (ct.user + ct.system, _io_bytes(p))
        except _GONE:
            continue

    time.sleep(interval)

    cpu_delta = 0.0
    rss = 0
    read_delta = write_delta = 0
    disk_ok = False
    for p in procs:
        prior = first.get(p.pid)
        if prior is None:  # appeared after the first snapshot, or already gone
            continue
        try:
            ct = p.cpu_times()
            cpu_delta += (ct.user + ct.system) - prior[0]
            rss += p.memory_info().rss
        except _GONE:  # exited mid-sample — drop it
            continue
        io_now = _io_bytes(p)
        if prior[1] is not None and io_now is not None:
            read_delta += max(0, io_now[0] - prior[1][0])
            write_delta += max(0, io_now[1] - prior[1][1])
            disk_ok = True

    return {
        "procs": len(procs),
        "cpu_percent": round(max(0.0, cpu_delta) / interval * 100, 1),
        "rss_bytes": rss,
        "disk_read_bps": round(read_delta / interval) if disk_ok else None,
        "disk_write_bps": round(write_delta / interval) if disk_ok else None,
    }
