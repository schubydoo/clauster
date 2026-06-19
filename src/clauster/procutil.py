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

# The `-m` module name identifying a PTY keeper process (it wraps a pty bridge as
# `python -m clauster.pty_keeper --sidecar P -- <bridge argv>`).
_KEEPER_MODULE = "clauster.pty_keeper"

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


def is_standard_bridge_cmdline(cmdline: list[str]) -> bool:
    """Whether a cmdline is the *standard* ``claude remote-control`` subcommand bridge.

    The standard (multi-session) form carries ``remote-control`` as a standalone
    argv token (``claude remote-control …``); the pty true-resume form is the
    ``--remote-control`` / ``--rc`` **flag** instead. :func:`is_bridge_cmdline`
    matches both (it substring-tests the joined cmdline), so it can't tell them
    apart — this exact-token check can, and only the subcommand form passes.

    External-session adoption (#330) is gated on this: a standard external bridge is
    safely adoptable (its pid is its own process group, Stop is a clean single
    SIGINT), whereas a pty external bridge is not (no recoverable keeper, its Stop is
    terminal-coupled). A flag-form bridge must therefore fail this gate.
    """
    if not cmdline:
        return False
    if _BRIDGE_BINARY_HINT not in " ".join(cmdline):
        return False
    # Reject the flag form explicitly FIRST: `--remote-control <name>` / `--rc <name>` /
    # `--remote-control=…`. Otherwise a project literally named "remote-control", passed as
    # the flag's positional, would put the bare token into argv and sneak past the
    # membership check below — misclassifying a pty bridge as adoptable.
    if any(tok == "--rc" or tok.startswith("--remote-control") for tok in cmdline):
        return False
    return "remote-control" in cmdline  # exact argv element (the subcommand), not a flag


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


def is_keeper_cmdline(cmdline: list[str]) -> bool:
    """Whether a command line is a ``python -m clauster.pty_keeper`` PTY keeper.

    The keeper is launched as ``<python> -m clauster.pty_keeper --sidecar P -- <argv>``.
    As :func:`is_bridge_cmdline` / :func:`is_hosted_cmdline` gate on the ``claude`` binary
    before the distinguishing token, this requires BOTH the python interpreter (argv[0])
    AND the module passed via ``-m`` — so an unrelated process that merely carries the
    string as a data argument (a ``grep``, a ``python -c`` script) is never mistaken for a
    keeper and killed in the TOCTOU window (#301 / RUNOPS-1).
    """
    if not cmdline or _KEEPER_MODULE not in cmdline:
        return False
    idx = cmdline.index(_KEEPER_MODULE)
    return "python" in cmdline[0].lower() and idx > 0 and cmdline[idx - 1] == "-m"


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


def is_live_standard_bridge(
    pid: int, proc_start: str | float | None, *, tolerance: float = 2.0
) -> bool:
    """Whether ``pid`` is a live bridge AND the *standard* subcommand form.

    Like :func:`is_live_bridge`, but the cmdline gate is the stricter
    :func:`is_standard_bridge_cmdline` — used to confirm an external session is a
    standard bridge before adopting it (a pty/flag-form bridge fails the gate; see
    that function for why pty adoption is unsafe).
    """
    return is_live_process(
        pid, proc_start, tolerance=tolerance, require_cmdline=is_standard_bridge_cmdline
    )


def is_killable_hosted(pid: int, proc_start: str | float | None) -> bool:
    """Whether ``pid`` is a live hosted agent we can *safely* hard-kill (CL-8).

    The single predicate behind both orphan classification (``HostedManager._is_orphan``)
    and the guarded kill (:func:`kill_if_match`), so a row is only ever treated as a
    recoverable orphan when it is actually killable. Fails closed: unlike the lenient
    liveness check, a missing or uncomparable ``proc_start`` (None, or a non-numeric
    string) returns False rather than degrading to cmdline+alive alone — without a
    create-time match a kill could hit an unrelated process that reused the PID, and a
    state transition gated on a "kill" that never happened would silently mislead.
    """
    if _expected_epoch(proc_start) is None:
        return False
    return is_live_process(pid, proc_start, require_cmdline=is_hosted_cmdline)


def kill_if_match(pid: int, proc_start: str | float | None) -> bool:
    """Hard-kill a hosted agent ``pid`` (and its tree) only if it's still that process.

    Gated on :func:`is_killable_hosted` (live + hosted cmdline + exact create-time match),
    so a reused/unrelated/uncomparable PID is never killed. Returns whether a kill was
    issued. Used by hosted orphan cleanup (CL-8).
    """
    if not is_killable_hosted(pid, proc_start):
        return False
    force_kill_tree(pid)
    return True


def is_keeper_process(pid: int) -> bool:
    """Whether ``pid`` is a live, non-zombie ``clauster.pty_keeper`` process.

    The cmdline gate for the keeper path: orphan classification
    (:func:`clauster.pty_keeper.iter_keepers`) and the hard-kill
    (:func:`clauster.pty_keeper.stop_keeper`) both require it, so a PID the original
    keeper left behind and the OS recycled onto an unrelated process is never listed as
    a live orphan nor SIGKILLed (#301 / RUNOPS-1). Fails closed on any psutil error.
    """
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return is_keeper_cmdline(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


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


# ----- spawn environment --------------------------------------------------

# Env var names that hold a Clauster secret a spawned child must never inherit.
# A bridge runs project-controlled code, a clone runs an attacker-controllable
# repo, and a ``claude --bg`` agent runs project code — any of them could read
# these from its own ``os.environ`` and forge an auth-session cookie (the signing
# secret) or exfiltrate the password hash for offline cracking. Stripping them at
# EVERY spawn makes that leak structurally impossible, not patched at one site.
_SECRET_ENV_NAMES = frozenset(
    {
        "CLAUSTER_SESSION_SECRET",
        "CLAUSTER_AUTH_PASSWORD_HASH",
    }
)
# Defense in depth for a future ``CLAUSTER_*`` secret added without updating the
# set above: any Clauster-prefixed name carrying a secret-shaped token is scrubbed
# too. The path pointers (CLAUSTER_CONFIG/HOME) and the recap flags
# (CLAUSTER_RESUME_RECAP[_MAX_CHARS]) carry none of these tokens, so they survive.
_SECRET_ENV_TOKENS = ("SECRET", "PASSWORD", "PASSWD", "TOKEN", "HASH")


def is_secret_env_name(name: str) -> bool:
    """Whether an env var name holds a Clauster secret a child must not inherit."""
    if name in _SECRET_ENV_NAMES:
        return True
    return name.startswith("CLAUSTER_") and any(tok in name for tok in _SECRET_ENV_TOKENS)


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of the parent environment safe to hand a spawned child.

    Strips Clauster secrets (:func:`is_secret_env_name`) so project-controlled
    bridge code, a cloned repo's hooks, or a background agent can never read the
    session-signing secret or password hash from its own ``os.environ`` and forge
    an auth cookie. ``extra`` overlays caller keys (e.g. the resume-recap flags)
    AFTER scrubbing and is itself scrubbed, so the chokepoint never emits a
    Clauster secret — not even one a caller passes back in via ``extra``.

    Scope is deliberately Clauster-only — a spawned ``claude`` legitimately needs
    e.g. ``ANTHROPIC_API_KEY``, so this is not a general secret firewall; it
    removes only Clauster's own credentials, which no child has any reason to read.
    """
    env = {k: v for k, v in os.environ.items() if not is_secret_env_name(k)}
    if extra:
        env.update({k: v for k, v in extra.items() if not is_secret_env_name(k)})
    return env
