"""Process introspection for bridge liveness (spec §7, source #3 liveness).

A bridge-pointer.json (Anthropic-controlled) records the bridge **parent** PID
plus a ``procStart`` value (Linux starttime, in jiffies). A dead bridge leaves a
stale pointer, and a PID can be reused, so liveness alone is never enough. A PID
is only trusted as a live bridge when ALL hold:

  1. the process exists and is not a zombie,
  2. its start time matches the recorded one (PID-reuse defense), and
  3. its cmdline is actually ``claude remote-control`` (HANDOFF locked decision:
     require a cmdline match before trusting a pointer).

Uses ``psutil`` for cross-platform process ops; the only Linux-specific bit is
converting a pointer's jiffies ``procStart`` to an epoch for comparison, which
degrades gracefully on other platforms.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import psutil

# The token sequence that identifies a managed bridge in a process cmdline.
_BRIDGE_CMDLINE = ("remote-control",)
_BRIDGE_BINARY_HINT = "claude"

# A proc_start we measured ourselves (a float create_time) is the SAME
# measurement as the live process's create_time, so the same process matches it
# near-exactly — only a hair of float jitter is plausible. A reused PID, having
# started later, carries a measurably later create_time, so this tight bound
# rejects it. (A pointer's jiffies epoch is derived independently and keeps the
# caller's looser ``tolerance``.)
_EXACT_PROC_START_TOLERANCE = 0.05


def _clk_tck() -> int:
    try:
        return os.sysconf("SC_CLK_TCK") or 100
    except (ValueError, AttributeError, OSError):
        return 100


def jiffies_to_epoch(jiffies: int) -> float | None:
    """Convert a Linux starttime (jiffies since boot) to an epoch timestamp.

    Returns None when the host can't express boot time (non-Linux), in which
    case callers fall back to a cmdline-only trust check.
    """
    try:
        return psutil.boot_time() + (jiffies / _clk_tck())
    except (OSError, AttributeError):
        return None


def is_bridge_cmdline(cmdline: list[str]) -> bool:
    """Whether a process command line is a ``claude … remote-control`` bridge."""
    if not cmdline:
        return False
    joined = " ".join(cmdline)
    if _BRIDGE_BINARY_HINT not in joined:
        return False
    return all(tok in cmdline or tok in joined for tok in _BRIDGE_CMDLINE)


def is_hosted_cmdline(cmdline: list[str]) -> bool:
    """Whether a command line is a headless ``claude … stream-json`` hosted agent.

    The hosted channel spawns ``claude --output-format stream-json …`` (no
    ``remote-control``), so it needs its own cmdline gate — distinct from a bridge —
    to confirm a CT-1-reported PID is a hosted agent before trusting or killing it.
    """
    if not cmdline:
        return False
    joined = " ".join(cmdline)
    return _BRIDGE_BINARY_HINT in joined and "stream-json" in joined


def proc_create_time(pid: int) -> float | None:
    """Epoch create-time of a live, non-zombie PID, else None."""
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def is_live_process(
    pid: int,
    proc_start: str | float | None,
    *,
    tolerance: float = 2.0,
    require_cmdline: Callable[[list[str]], bool] | None = None,
) -> bool:
    """Whether ``pid`` is alive, non-zombie, and its start-time matches ``proc_start``.

    The generic liveness+PID-reuse core. ``require_cmdline`` adds an optional cmdline
    gate (``is_live_bridge`` passes :func:`is_bridge_cmdline`; the hosted channel
    passes :func:`is_hosted_cmdline`). ``proc_start`` may be a pointer's jiffies
    string, our own stored psutil create-time (float/epoch), or None to skip the
    start-time match. A float we measured ourselves matches the same process
    near-exactly, so it uses the tight ``_EXACT_PROC_START_TOLERANCE`` (closing the
    PID-reuse window); a jiffies string keeps the looser ``tolerance``.
    """
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        cmdline = proc.cmdline()
        create_time = proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False

    if require_cmdline is not None and not require_cmdline(cmdline):
        return False

    expected = _expected_epoch(proc_start)
    if expected is None:
        # No comparable start-time (skip or non-Linux pointer): cmdline+alive is
        # the best available trust signal.
        return True
    # A float is our own exact create_time; a string is a pointer's jiffies.
    exact = isinstance(proc_start, (int, float)) and not isinstance(proc_start, bool)
    bound = _EXACT_PROC_START_TOLERANCE if exact else tolerance
    return abs(create_time - expected) <= bound


def is_live_bridge(pid: int, proc_start: str | float | None, *, tolerance: float = 2.0) -> bool:
    """Whether ``pid`` is a trustworthy, currently-running managed *bridge*.

    Thin wrapper over :func:`is_live_process` with the bridge cmdline gate — alive,
    non-zombie, start-time matches, AND a ``claude … remote-control`` cmdline.
    """
    return is_live_process(pid, proc_start, tolerance=tolerance, require_cmdline=is_bridge_cmdline)


def kill_if_match(pid: int, proc_start: str | float | None) -> bool:
    """Hard-kill a hosted agent ``pid`` (and its tree) only if it's still that process.

    Gated on :func:`is_live_process` with the hosted cmdline + exact create-time match,
    so a reused/unrelated PID is never killed. Returns whether a kill was issued. Used
    by hosted orphan cleanup (CL-8).

    Fails closed: unlike the lenient liveness check, a missing or uncomparable
    ``proc_start`` (None, or a non-numeric string) is refused rather than degrading to
    cmdline+alive alone — without a create-time match this destructive path could kill
    an unrelated process that reused the PID.
    """
    if _expected_epoch(proc_start) is None:
        return False
    if not is_live_process(pid, proc_start, require_cmdline=is_hosted_cmdline):
        return False
    force_kill_tree(pid)
    return True


def _expected_epoch(proc_start: str | float | None) -> float | None:
    """Normalize a stored proc_start to an epoch, or None to skip the check."""
    if proc_start is None:
        return None
    if isinstance(proc_start, (int, float)) and not isinstance(proc_start, bool):
        # Already an epoch create-time: our own spawn stores create_time as a
        # float. A pointer's jiffies arrive as a string and are parsed below.
        return float(proc_start)
    try:
        jiffies = int(str(proc_start))
    except (TypeError, ValueError):
        return None
    return jiffies_to_epoch(jiffies)


def force_kill_tree(pid: int) -> None:
    """Best-effort hard kill of ``pid`` and all its descendants.

    The graceful-stop fallback: used when a bridge ignores SIGINT/CTRL_BREAK, or
    to reap a wrapper process (e.g. a Windows ``.cmd`` shim) that outlives the
    bridge it launched. Safe on a dead/reused/absent PID.
    """
    try:
        proc = psutil.Process(pid)
        targets = proc.children(recursive=True)
        targets.append(proc)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return
    for p in targets:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass


def reap_if_exited(pid: int) -> None:
    """Best-effort non-blocking reap of one of our own children (avoid zombies).

    Safe to call on a non-child or already-reaped PID. A no-op on Windows, which
    has no ``waitpid``/``WNOHANG`` and does not leave zombies to reap.
    """
    if not hasattr(os, "WNOHANG"):  # Windows: no child reaping needed
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
