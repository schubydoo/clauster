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

import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

import psutil

_log = logging.getLogger("clauster.procutil")

# The token sequence that identifies a managed bridge in a process cmdline.
_BRIDGE_CMDLINE = ("remote-control",)
_BRIDGE_BINARY_HINT = "claude"

# The `-m` module name identifying a PTY keeper process (it wraps a pty bridge as
# `python -m clauster.pty_keeper --sidecar P -- <bridge argv>`).
_KEEPER_MODULE = "clauster.pty_keeper"

# Hidden CLI subcommand a frozen (PyInstaller) binary re-invokes to run the PTY
# keeper. Under a one-file build ``sys.executable`` is the clauster binary itself,
# so the source-mode ``<python> -m clauster.pty_keeper`` form can't work — clauster's
# argparse would reject the bare ``-m``. The frozen launcher uses
# ``<exe> __pty-keeper__ --sidecar P -- <argv>`` instead (see
# :func:`clauster.runner.SessionRunner._keeper_launch_cmd` and
# :func:`clauster.__main__.main`, mirroring the recap hook's ``__recap-hook__``).
# Lives here so the cmdline gate below recognizes either form.
KEEPER_SUBCOMMAND = "__pty-keeper__"

# A proc_start we measured ourselves (a float create_time) is the SAME
# measurement as the live process's create_time, so the same process matches it
# near-exactly — only a hair of float jitter is plausible. A reused PID, having
# started later, carries a measurably later create_time, so this tight bound
# rejects it. (A pointer's jiffies epoch is derived independently and keeps the
# caller's looser ``tolerance``.)
#
# ⚠️ Only sound while the epoch is the SOLE evidence. psutil derives create_time on
# Linux as ``starttime/CLK_TCK + boot_time()``, and ``boot_time()`` re-reads
# ``/proc/stat`` btime on every call — btime tracks the live realtime-vs-uptime offset,
# so NTP slew moves it under a process that never restarted (#1399: five distinct btime
# values, a 4-second spread, inside 3.5 minutes on a drifting host). Against 0.05s that
# reads as "not our process". Where a boot-relative tick count is also recorded,
# :func:`is_live_process` compares THAT and falls back to `_DRIFT_EPOCH_TOLERANCE` below,
# or to a recorded per-boot id (:func:`proc_boot_id`) that supersedes the epoch entirely.
_EXACT_PROC_START_TOLERANCE = 0.05

# The coarse epoch bound used alongside an exact boot-relative tick match, when no per-boot id
# is recorded to settle the one thing ticks cannot: they restart at zero each boot, so after a
# reboot an unrelated process can hold both the same pid and the same count. The epoch is
# demoted to a "same boot?" discriminator — wide enough to absorb any plausible clock
# correction, far narrower than the gap a reboot puts between two epochs. It has two known
# limits (a clock STEP over an hour still fails it; a reboot inside an hour can admit a pid +
# tick collision). A recorded ``bridge_boot_id`` settles both and, when present,
# :func:`is_live_process` uses it INSTEAD of this bound (#1401). The keeper and hosted callers
# (#1402 / #1404) record no boot id, so this bound remains their cross-boot guard.
_DRIFT_EPOCH_TOLERANCE = 3600.0


def _clk_tck() -> int:
    """Return the kernel clock-tick rate, defaulting to 100 where it is unavailable."""
    try:
        return os.sysconf("SC_CLK_TCK") or 100
    except (ValueError, AttributeError, OSError):
        return 100


def proc_start_ticks(pid: int) -> int | None:
    """Boot-relative start time of ``pid`` in clock ticks, or ``None`` where unavailable.

    Field 22 of ``/proc/<pid>/stat`` — the same quantity a bridge pointer's ``procStart``
    carries. Unlike :func:`proc_create_time` this is measured against the boot instant, not
    the wall clock, so it does not move when NTP corrects the clock (#1399). That makes it
    the trustworthy half of the PID-reuse pair: within one boot, two different processes at
    the same pid have different tick counts, and re-reading a live process always returns
    the value it was born with.

    ``None`` on any host without ``/proc`` (macOS, Windows) and on any read failure —
    caller falls back to the epoch comparison, which on those platforms is *stable*
    anyway: their create-times come from the kernel as absolute timestamps recorded at
    exec, not re-derived from a moving boot-time baseline.

    Parsed from the last ``)`` rather than by splitting the whole line: field 2 is the
    executable name, unquoted and free to contain spaces AND parentheses, so a
    left-to-right split miscounts every later field for a process named ``foo) bar``.
    """
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    try:
        # Fields after comm: index 0 is state, so starttime (field 22) is index 19.
        return int(stat[stat.rindex(")") + 1 :].split()[19])
    except (ValueError, IndexError):
        return None


def proc_boot_id() -> str | None:
    """Return the current boot's stable id (``/proc/sys/kernel/random/boot_id``), else ``None``.

    A per-boot UUID the kernel regenerates on every boot and holds constant until the next
    one. It is the discriminator a boot-relative tick count needs to become a COMPLETE
    process identity. Ticks restart at zero each boot, so on their own two processes at the
    same pid in different boots can share a count. A recorded boot id that differs from this
    one proves a row is from an earlier boot, so its pid names a different process even on an
    exact tick match — and it settles that WITHOUT the wall-clock epoch, which NTP moves
    under a process that never restarted (#1399).

    ``None`` on any host without the file (macOS, Windows) and on any read failure — the
    caller then falls back to ticks alone, exactly as :func:`proc_start_ticks` degrades.
    Those platforms record an absolute create-time and are exposed to neither fault.
    """
    try:
        raw = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return raw.strip() or None


def start_time_is_drift_prone() -> bool:
    """Whether this platform's process create-time moves when the wall clock is corrected.

    True on Linux only. psutil derives ``create_time`` there as ``starttime/CLK_TCK +
    boot_time()``, re-reading ``/proc/stat`` btime every call — and btime tracks the live
    realtime-vs-uptime offset, so NTP slew shifts it under a process that never restarted
    (#1399). macOS and Windows record an absolute timestamp at exec (``proc_pidinfo`` /
    ``GetProcessTimes``) and hand back the same value forever, so a start-time mismatch
    there really does mean a different process.

    Callers use it to tell a *conclusive* "not our process" from an *inconclusive* one, which
    matters wherever the answer drives a destructive action.
    """
    return sys.platform == "linux"


def jiffies_to_epoch(jiffies: int) -> float | None:
    """Convert a Linux starttime (jiffies since boot) to an epoch timestamp.

    Returns None when the host can't express boot time (non-Linux), in which
    case callers fall back to a cmdline-only trust check.
    """
    try:
        return psutil.boot_time() + (jiffies / _clk_tck())
    except (OSError, AttributeError, RuntimeError):
        # RuntimeError is psutil's raise when `/proc/stat` carries no `btime` line (an
        # emulated procfs — gVisor, some container runtimes, WSL1) — exactly the "host can't
        # express boot time" case this docstring promises to answer None for, and it was
        # escaping into the pointer paths and, via `proc_start_pair`, into the keeper, where
        # the sidecar then lost BOTH halves of the pair and `_recover_keeper_pid` fell
        # through to a pid-only match. `proc_create_time` and `is_live_process` absorb the
        # same raise for the same reason — psutil's `create_time()` ends in
        # `self._ctime + boot_time()`, so on those hosts they hit it FIRST, before this
        # function is ever consulted. One rule for the family, not three.
        _log.debug("host cannot express boot time; start-time checks degrade to cmdline+alive")
        return None


def is_bridge_cmdline(cmdline: list[str]) -> bool:
    """Whether a process command line is a ``claude … remote-control`` bridge.

    Matches the two spellings that carry the literal ``remote-control`` token: the
    subcommand (standard) and the ``--remote-control`` flag. The bare ``--rc`` alias is
    deliberately NOT matched (#1107).

    The reason is the direction this gate fails in. :func:`bridge_ancestor` feeds the
    phantom-prune (it replaced ``is_bridge_process`` there in #1116, which had been asking
    whether the *session* pid was a bridge — it never is), and a match there DELETES a
    resumable card — so a false positive costs an operator their session, while a false
    negative only leaves a phantom card lingering. Widening the match is now strictly worse
    than it was: the walk tests up to four processes per session rather than one, so an
    alias that matched here would have four chances to find a false owner. Clauster's own
    use of ``--rc`` is
    :func:`~clauster.supervisor.build_dispatch_argv`'s ``claude --bg --rc <name>``: a
    BACKGROUND AGENT that opens a cloud door, not a bridge. Matching the alias would let
    a background agent stand as proof that "the bridge is alive, just unmanaged" and
    prune the card out from under it.

    That question was answered deliberately rather than by widening the gate: a
    ``claude --bg --rc <name>`` job does **not** count as bridge liveness (maintainer
    decision on #1107, closed 2026-08-29). Bridge liveness stays with the bridge lifecycle
    and supervisor jobs stay with the supervisor; the delete gate fails closed, so the
    cheap failure (a lingering phantom card) wins over the expensive one (a deleted
    resumable session); and matching the bare alias would reopen the documented
    ``claude``-in-path false-match class that made the earlier attempt a real incident on a
    host whose service user is ``claude``. The behavior is pinned by
    ``test_is_bridge_cmdline_does_not_match_the_bare_rc_alias``.

    Use :func:`is_standard_bridge_cmdline` when the subcommand form must be told apart.
    """
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
    ``--remote-control`` **flag** instead. :func:`is_bridge_cmdline` matches both
    of those (it substring-tests the joined cmdline for ``remote-control``), so it
    can't tell them apart — this exact-token check can, and only the subcommand
    form passes. Neither matches the bare ``--rc`` alias (#1107).

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
    """Whether a command line is a clauster PTY keeper (source or frozen form).

    Source/venv: ``<python> -m clauster.pty_keeper --sidecar P -- <argv>``.
    Frozen binary: ``<clauster-exe> __pty-keeper__ --sidecar P -- <argv>`` — under a
    PyInstaller one-file build ``sys.executable`` is the clauster binary, so the ``-m``
    form is impossible (see :data:`KEEPER_SUBCOMMAND`). Both forms must be recognized or
    orphan classification / hard-kill would miss a frozen keeper.

    As :func:`is_bridge_cmdline` / :func:`is_hosted_cmdline` gate on the ``claude`` binary
    before the distinguishing token, each form pairs the identifying token with its
    launcher: the source form needs ``python`` in argv[0] AND ``-m`` before the module;
    the frozen form needs the ``clauster`` binary in argv[0] AND the subcommand as argv[1].
    So an unrelated process that merely carries the string as a data argument (a ``grep``,
    a ``python -c`` script) is never mistaken for a keeper and killed in the TOCTOU window
    (#301 / RUNOPS-1).
    """
    if not cmdline:
        return False
    # Frozen form: `<clauster-exe> __pty-keeper__ …` — the binary, then the subcommand.
    if (
        len(cmdline) > 1
        and cmdline[1] == KEEPER_SUBCOMMAND
        and "clauster" in os.path.basename(cmdline[0]).lower()
    ):
        return True
    # Source form: `<python> -m clauster.pty_keeper …`.
    if _KEEPER_MODULE not in cmdline:
        return False
    idx = cmdline.index(_KEEPER_MODULE)
    return "python" in cmdline[0].lower() and idx > 0 and cmdline[idx - 1] == "-m"


def proc_create_time(pid: int) -> float | None:
    """Epoch create-time of a live, non-zombie PID, else None.

    Fails closed on a negative pid too (psutil raises ``ValueError``, not
    ``NoSuchProcess``), because callers feed this pids read out of keeper sidecars and
    persisted rows — untrusted on-disk ints. See :func:`is_keeper_process`; the whole
    predicate family absorbs that raise on the same terms, so no caller has to pre-gate.
    """
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
        return None
    except RuntimeError:
        # psutil's `create_time()` ends in `self._ctime + boot_time()`, and `boot_time()`
        # raises a bare RuntimeError on a procfs with no `btime` line — not wrapped by
        # psutil's own exception decorator, so it reaches us as-is. Uncaught it left
        # `poll_once` aborting on every tick (crash detection, adoption and the prune all
        # dead for the process's lifetime) and `stop()` skipping its grace + force-kill.
        # See :func:`jiffies_to_epoch`, which states the rule for the family.
        return None


def proc_start_pair(pid: int) -> tuple[float | None, int | None]:
    """Both halves of a process's start identity, from ONE read (#1399).

    ``(epoch, boot-relative ticks)``. Sampling them separately puts a suspension point
    between two ``/proc`` reads, and a process that dies in that window can have its pid
    recycled — leaving a pair whose halves describe DIFFERENT processes. That pair then
    authenticates the recycled occupant, because :func:`is_live_process` matches the ticks
    exactly (they are the new process's) and pairs them only with a coarse epoch (or, for a
    bridge, a boot id that a same-boot recycle shares). On a destructive path (``stop`` signals
    and force-kills the tree behind this pair) that is strictly worse than the pre-#1399 tight
    epoch bound, which rejected the mismatch — so both halves must come from the one read this
    function performs.

    Deriving the epoch FROM the ticks makes the halves agree by construction — it is the
    same arithmetic psutil does on Linux, so the epoch is bit-identical to what
    :func:`proc_create_time` would return. Where ticks are unavailable (non-Linux, unreadable
    ``/proc``) it falls back to sampling the epoch directly, which is the pre-#1399 shape and
    has no second read to straddle.

    ⚠️ One asymmetry with :func:`proc_create_time`, which filters zombies: a zombie still has
    a readable ``/proc/<pid>/stat``, so on Linux this returns a real pair for one where that
    function answers ``None``. Deliberate — the pair is an IDENTITY, not a liveness claim,
    and every gate rejects zombies separately before comparing it. It is also the stricter
    direction: a ``(None, ticks)`` pair made :func:`is_live_process` skip the start-time
    check entirely and trust cmdline+alive, which a recycled pid passes.
    """
    ticks = proc_start_ticks(pid)
    if ticks is None:
        return proc_create_time(pid), None
    return jiffies_to_epoch(ticks), ticks


def proc_is_gone(pid: int) -> bool:
    """Whether ``pid`` names no live, non-zombie process — the grace-loop probe (#1402).

    ``proc_create_time(pid) is None`` was the idiom at both keeper grace loops
    (:meth:`clauster.runner.SessionRunner._cleanup_keeper` and
    :func:`clauster.pty_keeper.stop_keeper`), and it is wrong on an emulated procfs with no
    ``btime`` (gVisor, some container runtimes, WSL1 — see :func:`jiffies_to_epoch`): psutil's
    ``create_time`` ends in ``+ boot_time()``, which raises there, so a keeper that is plainly
    running reads as gone. Both loops then returned before their identity compare and
    force-killed nothing, silently — a lingering keeper and its pty bridge were never wound
    down and nothing was logged. Boot-relative ticks read fine on exactly that host, which is
    what makes the bug invisible rather than total.

    Asking the status directly answers the same question identically everywhere. A **zombie
    counts as gone**: it has already exited, only its exit status remains, and its tree went
    with it — force-killing it is pointless and the identity compare downstream would report
    it as "no longer that keeper", which is untrue and misleading. Gone / denied / a pid that
    cannot name a process are all gone, matching what ``proc_create_time is None`` answered
    for them.

    ⚠️ ``status()`` needs no clock, but ``psutil.Process(pid)`` itself can still want one:
    up to and including psutil 7.0.0 its ``_init`` calls ``create_time()``, so on the very
    btime-less host this function exists for it raises the bare ``RuntimeError``
    :func:`jiffies_to_epoch` documents for the family. That must answer **not gone** — a live
    process we cannot do clock arithmetic for has not exited — and it cannot swallow a real
    exit, because a dead pid raises ``NoSuchProcess`` from the stat read first. Left
    uncaught it propagated out of ``asyncio.to_thread`` in :meth:`stop`, past the
    ``STOPPED`` transition and the worktree unlock, which is worse than the silent no-kill
    it replaced. ``pyproject.toml`` therefore floors psutil at 7.1, the first release whose
    constructor takes the boot-relative start on Linux and never asks for a clock — that
    floor is what keeps :func:`is_keeper_process` and :func:`is_live_process`, which
    construct outside any ``RuntimeError`` arm, from raising on the same host. The arm
    below stays as defence in depth for the floor.
    """
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
        return True
    except RuntimeError:
        return False


def proc_cwd(pid: int) -> Path | None:
    """Return ``pid``'s current working directory, or ``None`` when it can't be read.

    The positive-attribution input for the adopt/reattach gate (#949 review): the
    bridge-pointer directory is keyed by the *sanitized* cwd, so two punctuation-
    differing project paths can share one pointer file — a live standard bridge is
    only taken over when its ACTUAL cwd is the project's own directory. ``None``
    (process gone, zombie, access denied, or an OS that can't report it) must be
    treated as "not attributable": fail closed, never take over on a guess.
    """
    try:
        return Path(psutil.Process(pid).cwd())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return None


def is_live_process(
    pid: int,
    proc_start: str | float | None,
    *,
    tolerance: float = 2.0,
    require_cmdline: Callable[[list[str]], bool] | None = None,
    start_ticks: int | None = None,
    boot_id: str | None = None,
) -> bool:
    """Whether ``pid`` is alive, non-zombie, and its start-time matches ``proc_start``.

    The generic liveness+PID-reuse core. ``require_cmdline`` adds an optional cmdline
    gate (``is_live_bridge`` passes :func:`is_bridge_cmdline`; the hosted channel
    passes :func:`is_hosted_cmdline`). ``proc_start`` may be a pointer's jiffies
    string, our own stored psutil create-time (float/epoch), or None to skip the
    start-time match. A float we measured ourselves matches the same process
    near-exactly, so it uses the tight ``_EXACT_PROC_START_TOLERANCE`` (closing the
    PID-reuse window); a jiffies string keeps the looser ``tolerance``.

    ``start_ticks`` is the boot-relative half of the same pair (:func:`proc_start_ticks`),
    and when it is recorded AND readable for this pid it carries the PID-reuse defense: the
    ticks must match **exactly**. Ticks are measured against the boot instant, so NTP cannot
    move them under a process that never restarted (#1399); the 0.05s epoch bound would read a
    live bridge as dead after a correction. Within one boot the tick match is COMPLETE: a pid
    recycled a second later differs by a whole ``CLK_TCK`` of ticks and fails exactly.

    ``boot_id`` is the persisted ``/proc/sys/kernel/random/boot_id`` (:func:`proc_boot_id`),
    the one case ticks alone cannot settle: they restart at zero each boot, so a row from an
    earlier boot could name a recycled pid holding the same count. When ``boot_id`` is
    recorded AND readable now, it decides that question on identity — a mismatch rejects the
    row — and, being boot-relative like the ticks, it is immune to the clock STEP that a coarse
    epoch bound could not survive (#1401), so it is used INSTEAD of the epoch. When ``boot_id``
    is absent (the keeper and hosted callers, or a pre-#1401 bridge row) or unreadable, the
    exact tick match pairs with the coarse ``_DRIFT_EPOCH_TOLERANCE`` as a "same boot?"
    discriminator — the behaviour #1402 and #1404 rely on. A btime-less host cannot compare
    the epoch, so ticks stand alone there. A bridge row re-stamps its boot_id on the next
    spawn or reattach.

    With ticks recorded but unreadable for this pid, or none recorded at all, the answer
    falls through to the epoch comparison below: ``proc_start`` None or an underivable
    ``create_time()`` still answers on cmdline+alive, and a comparable epoch keeps the tight
    (our own float) or ``tolerance`` (pointer jiffies) bound. With neither ticks nor a
    comparable epoch the answer stays "no comparable start-time → trust cmdline+alive".
    """
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        cmdline = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
        # ValueError: psutil's raise for a non-positive pid. `is_live_bridge` is handed pids
        # from persisted rows and bridge pointers, so this is the same untrusted-int path the
        # rest of the family absorbs — it must answer "not live", not raise into the caller.
        return False
    create_time: float | None
    try:
        create_time = proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    except RuntimeError:
        # A procfs with no `btime` line: `create_time()` ends in `self._ctime + boot_time()`,
        # which raises bare and unwrapped. This is the FIRST place the family touches it —
        # `_expected_epoch` below is never reached — and uncaught it aborted `poll_once` on
        # every tick and made `stop()` skip its grace and force-kill. A HOST capability
        # failure, not a fact about this pid: the boot-relative ticks are still readable
        # there, so recorded ticks decide below; without them the answer is "not live".
        create_time = None

    if require_cmdline is not None and not require_cmdline(cmdline):
        return False

    expected = _expected_epoch(proc_start)
    if start_ticks is not None:
        observed_ticks = proc_start_ticks(pid)
        if observed_ticks is not None:
            if observed_ticks != start_ticks:
                return False  # a pid recycled within this boot differs by whole ticks
            # Ticks match exactly — the PID-reuse defense within one boot (a pid recycled a
            # second later differs by a whole CLK_TCK). ``boot_id``, when both recorded and
            # readable, settles the one thing ticks cannot: a row from an EARLIER boot whose
            # pid was recycled onto a process holding the same count. A mismatch rejects that
            # on identity, and — being boot-relative like the ticks — it is immune to the clock
            # step that defeats the epoch (#1401), so when it is present the epoch is not
            # consulted at all.
            if boot_id is not None:
                live_boot_id = proc_boot_id()
                if live_boot_id is not None:
                    return boot_id == live_boot_id
            # No recorded boot id (the keeper/hosted callers, or a pre-#1401 bridge row): fall
            # back to the coarse epoch as a "same boot?" discriminator, the behaviour #1402 and
            # #1404 rely on. A btime-less host (create_time / expected underivable) cannot
            # compare it, so ticks stand alone there — the only defense such a host has.
            if create_time is None or expected is None:
                return True
            return abs(create_time - expected) <= _DRIFT_EPOCH_TOLERANCE
    if create_time is None:
        return False  # btime-less host and no ticks to fall back on: not provably ours
    if expected is None:
        # No comparable start-time (skip or non-Linux pointer): cmdline+alive is
        # the best available trust signal.
        return True
    # A float is our own exact create_time; a string is a pointer's jiffies.
    exact = isinstance(proc_start, (int, float)) and not isinstance(proc_start, bool)
    bound = _EXACT_PROC_START_TOLERANCE if exact else tolerance
    return abs(create_time - expected) <= bound


def is_live_bridge(
    pid: int,
    proc_start: str | float | None,
    *,
    tolerance: float = 2.0,
    start_ticks: int | None = None,
    boot_id: str | None = None,
) -> bool:
    """Whether ``pid`` is a trustworthy, currently-running managed *bridge*.

    Thin wrapper over :func:`is_live_process` with the bridge cmdline gate — alive,
    non-zombie, start-time matches, AND a ``claude … remote-control`` cmdline.

    ``start_ticks`` is the persisted ``bridge_start_ticks`` and makes the start-time half
    immune to clock drift; ``boot_id`` is the persisted ``bridge_boot_id`` and rejects a
    row from an earlier boot on identity (#1401). See :func:`is_live_process`. Passing them
    matters most here of the whole family, because a false "not live" from this predicate
    does not merely mislead: it demotes the instance to STOPPED and thereby hands its
    still-running card to the phantom-prune, which deletes it (#1399).
    """
    return is_live_process(
        pid,
        proc_start,
        tolerance=tolerance,
        require_cmdline=is_bridge_cmdline,
        start_ticks=start_ticks,
        boot_id=boot_id,
    )


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


def is_killable_hosted(
    pid: int, proc_start: str | float | None, *, start_ticks: int | None
) -> bool:
    """Whether ``pid`` is a live hosted agent we can *safely* hard-kill (CL-8).

    The single predicate behind both orphan classification (``HostedManager._is_orphan``)
    and the guarded kill (:func:`kill_if_match`), so a row is only ever treated as a
    recoverable orphan when it is actually killable. Fails closed: unlike the lenient
    liveness check, a missing or uncomparable ``proc_start`` (None, or a non-numeric
    string) returns False rather than degrading to cmdline+alive alone — without a
    create-time match a kill could hit an unrelated process that reused the PID, and a
    state transition gated on a "kill" that never happened would silently mislead.

    ``start_ticks`` is the persisted ``agent_start_ticks``, the boot-relative half of the
    same pair, and it is what makes the comparison survive a clock correction (#1404): an
    epoch-only compare against ``_EXACT_PROC_START_TOLERANCE`` reads a *survived* agent as
    a different process, so the row is classified as lost instead of as a recoverable
    orphan and this refuses its kill. Keyword-required with no default so a call site that
    forgets the drift-immune half is a type error rather than a silent regression to the
    epoch-only behaviour — the failure mode PR #1400 hit three review rounds running.

    The entry guard above deliberately still requires the *epoch*, and is not loosened to
    "ticks alone are enough". Within one boot that is not a claim ticks are weaker —
    :func:`is_live_process` compares them EXACTLY, which is stricter than the 0.05s epoch
    bound — but a row can only reach here with ticks and no epoch on a procfs with no
    ``btime`` line (gVisor, some container runtimes, WSL1), where :func:`proc_start_pair`
    yields ``(None, ticks)``. Such a row stays un-killable and its agent leaks. That is the
    safe error for a kill gate: a leaked agent is recoverable by hand, a kill aimed at the
    wrong process is not.

    ⚠️ TODO(1401): ACROSS a reboot the pair is LAXER than what this gate used to apply, and
    this is the first call site to route a ``SIGKILL`` through that residue — state it rather
    than let the paragraph above read as "strictly stricter". Before ticks were recorded
    this gate reached :func:`is_live_process` with none, so it compared the epoch at
    ``_EXACT_PROC_START_TOLERANCE`` (0.05s), which no post-reboot process can pass: its
    create-time lies after the reboot and the recorded one before it, so the gap is at
    least the downtime. Cross-boot PID reuse was excluded *structurally*. With ticks the
    conjunct becomes exact-ticks AND ``_DRIFT_EPOCH_TOLERANCE`` (1h), so a host that reboots
    within an hour of its previous boot can admit a process holding the same pid AND the
    same tick offset from boot — see that constant's own note, which named this residue when
    #1399 accepted it for a card-liveness READ. A force-kill is a worse place to spend it.
    Accepted as a documented residue, not overlooked. The fix is a second column
    (``/proc/sys/kernel/random/boot_id``, issue #1401) that settles it exactly for all three
    halves at once; #1404 would otherwise grow a schema change it does not need. Both hosted
    call sites are OPERATOR-INITIATED — ``kill_orphan`` is the dashboard's Kill button and
    ``resume`` is its Resume — so nothing reaps on a timer through this window.
    ``test_a_cross_boot_tick_collision_inside_the_epoch_window_is_admitted`` pins the residue
    so it cannot widen unnoticed, and is the test #1401 flips to ``is False``.
    """
    if _expected_epoch(proc_start) is None:
        return False
    return is_live_process(
        pid, proc_start, require_cmdline=is_hosted_cmdline, start_ticks=start_ticks
    )


def kill_if_match(pid: int, proc_start: str | float | None, *, start_ticks: int | None) -> bool:
    """Hard-kill a hosted agent ``pid`` (and its tree) only if it's still that process.

    Gated on :func:`is_killable_hosted` (live + hosted cmdline + a start-time match), so a
    reused/unrelated/uncomparable PID is never killed. Returns whether a kill was issued.
    Used by hosted orphan cleanup (CL-8).

    ``start_ticks`` is keyword-required with no default for the reason given on
    :func:`is_killable_hosted`: this signals ``SIGKILL`` at a process tree, and a call site
    that silently fell back to the drifting epoch would refuse a kill the operator asked
    for, leaving a survived agent running with no dashboard control (#1404).
    """
    if not is_killable_hosted(pid, proc_start, start_ticks=start_ticks):
        return False
    force_kill_tree(pid)
    return True


def is_keeper_process(pid: int) -> bool:
    """Whether ``pid`` is a live, non-zombie ``clauster.pty_keeper`` process.

    The cmdline gate for the keeper path: orphan classification
    (:func:`clauster.pty_keeper.iter_keepers`) and the hard-kill
    (:func:`clauster.pty_keeper.stop_keeper`) both require it, so a PID the original
    keeper left behind and the OS recycled onto an unrelated process is never listed as
    a live orphan nor SIGKILLed (#301 / RUNOPS-1).

    **Fails closed on gone / denied / zombie / negative pid** — the last via the
    ``ValueError`` psutil raises below zero, which it does on every platform because it
    validates the argument before any OS call. That matters because several callers feed
    this a pid read straight out of a keeper sidecar, which is an on-disk file that can
    hold a negative value; while it was uncaught, such a sidecar raised out of
    ``rediscover``'s ``to_thread`` and failed lifespan startup rather than being skipped.
    "Not a keeper" is the right answer for a pid that cannot name a process at all.
    :func:`proc_create_time`, :func:`is_live_process` and :func:`is_bridge_process` absorb
    the same raise, so no caller has to pre-gate a pid.

    ⚠️ Pid ``0`` is deliberately NOT characterised here: it is absent on Linux but a real
    kernel process on macOS and Windows, so which arm of the catch it takes is platform
    specific. The cmdline gate answers it either way; nothing should depend on the route.

    The ``RuntimeError`` arm is defence in depth for the psutil floor (#1402): before 7.1
    the constructor itself called ``create_time()``, which raises bare on a procfs with no
    ``btime``, and since :func:`proc_is_gone` lets the keeper grace loops run out on that
    host this gate is reachable there. ``pyproject.toml`` floors psutil at 7.1 so the
    constructor never asks for a clock; should one still raise, "not a keeper" spares the
    pid — a leak an operator can end by hand, where a raise aborted :meth:`stop` past its
    ``STOPPED`` transition and a wrong answer the other way would front a SIGKILL.
    """
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return is_keeper_cmdline(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
        return False
    except RuntimeError:
        return False


def is_live_keeper(
    pid: int,
    proc_start: str | float | None,
    *,
    tolerance: float = 2.0,
    # Keyword-REQUIRED, no default (#1402). Threading a start-ticks argument through some
    # call sites and defaulting it on the rest is the failure #1399's review found three
    # rounds running, and it is silent; without a default a missed site is a type error
    # pyright names on the spot. `is_live_bridge` below still carries the defaulted form it
    # shipped with — the two are deliberately NOT symmetric yet, and converting it means
    # touching every bridge call site, which belongs in its own change.
    start_ticks: int | None,
) -> bool:
    """Whether ``pid`` is a trustworthy, currently-running ``clauster.pty_keeper`` (#1178).

    The keeper counterpart of :func:`is_live_bridge`, and the same wrapper over
    :func:`is_live_process` — alive, non-zombie, start-time matches, AND a keeper
    cmdline. :func:`is_keeper_process` answers only "is this pid *a* keeper", which
    rules out a recycled pid running something else but NOT a *different* live keeper
    that happens to hold that pid; on a host running many interactive sessions those
    are exactly the pids most likely to be reused by another keeper.

    ``start_ticks`` is the persisted ``keeper_start_ticks`` and makes the start-time half
    immune to clock drift; see :func:`is_live_process` for which of the two then carries
    which job. It matters here for the mirror image of the reason it matters on the bridge
    (#1402): a false "not live" from this predicate lets ``forget`` delete the record of a
    keeper that is still holding a pty bridge's terminal, and ``forget`` never kills — so
    the keeper and its bridge run on with neither card nor row, and nothing automated
    recovers them.

    ``proc_start is None`` **with no ticks either** degrades to :func:`is_keeper_process`'s
    cmdline+alive answer, deliberately: a row written before ``keeper_proc_start`` was
    persisted has no start-time to compare, and treating unknown as a mismatch would report
    a live keeper as dead — the very failure the check exists to prevent. Recorded ticks
    with no epoch decide alone and exactly, which is stricter; see :func:`is_live_process`.
    """
    return is_live_process(
        pid,
        proc_start,
        tolerance=tolerance,
        require_cmdline=is_keeper_cmdline,
        start_ticks=start_ticks,
    )


def is_bridge_process(pid: int) -> bool:
    """Whether ``pid`` is a live, non-zombie ``claude`` **bridge** process (#1096).

    The cmdline counterpart to :func:`is_keeper_process`, for deciding whether an EXTERNAL
    working session is an unmanaged *bridge* or merely a hand-run ``claude`` sharing a
    project directory. The phantom-prune needs that distinction: its premise is "the bridge
    IS alive, just unmanaged", and deleting a resumable card because an operator opened a
    terminal in the project would be wrong.

    **Fails closed on gone / denied / zombie / negative pid**, on exactly the same terms
    as :func:`is_keeper_process` (including its note on pid ``0``). The two share a shape and
    move together; leaving one uncaught would put the same startup-failure edge back on the
    phantom-prune path.
    """
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return is_bridge_cmdline(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
        return False


# How far above a working session its bridge can sit. MEASURED distance, with no slack:
# 1 hop for Server Mode (session pid is the SDK worker, its parent is the bridge —
# confirmed on a live host) and 0 for an in-process flag-form pty (the session pid IS the
# bridge). Slack is a liability here, not safety margin: this gate feeds a prune that
# DELETES a resumable card, `is_bridge_cmdline` is a substring test over the joined
# cmdline, and a host can easily carry unrelated processes that satisfy it (a leaked test
# stub, `vim claude-remote-control.md`). Every extra hop is another chance for one of those
# to be mistaken for a session's owner. If a real wrapper ever appears, the prune goes
# INERT — a stale card lingers, visibly and recoverably — which is the safe way to be
# wrong; raise this deliberately then, rather than pre-paying for it now.
_MAX_BRIDGE_ANCESTRY = 1


def bridge_ancestor(pid: int, *, max_depth: int = _MAX_BRIDGE_ANCESTRY) -> int | None:
    """Return the nearest live bridge at or above ``pid`` in its ancestry, else ``None``.

    :func:`is_bridge_process` asks whether ``pid`` **is** a bridge, which is the wrong
    question for a working session (#1116): ``agents --json`` reports a Server Mode
    session's pid as the *SDK worker* — ``…/versions/<ver> --print --sdk-url …`` — whose
    own cmdline is never a bridge cmdline, so testing it always answered False and the
    phantom-prune could never fire. The bridge is that worker's PARENT.

    Ancestry is the right relation here and not the ``ancestry != ownership`` trap of
    #1020: this does not attribute a session to a *managed instance* (that stays with
    :func:`owned_pids` and the #820 pid gate) — it only answers "is a live bridge process
    responsible for this session", which is a parent/child fact. The distinction still
    matters at the call site, which must exclude bridges Clauster manages before treating
    the answer as evidence of an *unmanaged* one.

    Bounded by ``max_depth`` and stopped at pid 1 so a session whose bridge already exited
    can never charge an unrelated ancestor. Fails closed (``None``) on any psutil error, so
    an unreadable process tree never manufactures prune evidence.
    """
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
        return None
    for _ in range(max_depth + 1):
        try:
            cmdline = proc.cmdline()
            # A PTY keeper matches `is_bridge_cmdline` — it carries the bridge argv after
            # `--`, and that test is a substring match over the joined cmdline. The keeper
            # is the bridge's PARENT, so without this a pty bridge that died with its
            # keeper not yet reaped hands the walk to the keeper, which is a different pid
            # from the one the caller excludes as managed: our own keeper would become
            # evidence for deleting our own card. Checked FIRST because a keeper satisfies
            # both predicates.
            if is_keeper_cmdline(cmdline):
                return None
            if proc.status() != psutil.STATUS_ZOMBIE and is_bridge_cmdline(cmdline):
                return proc.pid
            parent = proc.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, RuntimeError):
            # RuntimeError: `parent()` compares epochs to reject a reparented pid, and that
            # read raises on a procfs with no `btime` (#1402). No ancestor, not a crash.
            return None
        if parent is None or parent.pid <= 1:
            return None
        proc = parent
    return None


# The native `claude` installer keeps every downloaded release at
# `…/claude/versions/<version>` and points the launcher on PATH at one of them by SYMLINK,
# so a process exec'd from it carries the versioned path in its `exe` (and, for a worker
# the bridge re-execs, in `argv[0]`). Both anchors below are load-bearing:
#
#   * `claude/` as the PARENT of `versions/` is what makes the match specific. A bare
#     `versions/<x>` is the layout pyenv, nvm AND rbenv all use, and a bridge's process
#     tree really does carry such paths — a `…/.nvm/versions/node/v24.19.0/bin/…` language
#     server was a direct grandchild of a live bridge on the dogfood host. Matching one of
#     those would put a confidently WRONG release on the card, which is the single outcome
#     #1275 rules out ("show nothing rather than a wrong value").
#   * the segment after `versions/` must itself look like a release number, so a
#     `versions/node/…` style layout is rejected rather than rendered verbatim.
_CLAUDE_VERSIONS_PARENT = "claude"
_CLAUDE_VERSIONS_DIR = "versions"
_CLAUDE_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?")
# Split on BOTH separators rather than `os.sep`: the string being parsed is a path read out
# of ANOTHER process, and a Windows host yields backslashes regardless of our own platform.
_PATH_SPLIT_RE = re.compile(r"[\\/]+")


def parse_claude_version(path: str | None) -> str | None:
    """Extract the release from a versioned ``claude`` binary path, else ``None`` (#1275).

    Recognises only the native installer's ``…/claude/versions/<version>`` layout (see the
    module constants above for why both path anchors are required). An npm/global install,
    an unrecognised layout, or another version manager's ``versions/`` directory all return
    ``None`` — this feeds a dashboard label, and a blank label is honest where a guess is not.
    """
    if not path:
        return None
    parts = [p for p in _PATH_SPLIT_RE.split(path) if p]
    lowered = [p.lower() for p in parts]
    # Needs a segment BEFORE (the `claude/` parent) and AFTER (the version) `versions/`.
    for idx in range(1, len(parts) - 1):
        if (
            lowered[idx] == _CLAUDE_VERSIONS_DIR
            and lowered[idx - 1] == _CLAUDE_VERSIONS_PARENT
            and _CLAUDE_VERSION_RE.fullmatch(parts[idx + 1])
        ):
            return parts[idx + 1]
    return None


def _proc_claude_version(proc: psutil.Process) -> str | None:
    """Release carried by one process's own ``exe``/``argv[0]``, else ``None``.

    ``exe`` first because it is RESOLVED: the launcher on PATH is a symlink, so
    ``argv[0]`` is typically the unversioned ``…/bin/claude`` while ``exe`` is the
    versioned target the process actually runs. Fails closed on every psutil error —
    a process we cannot read reports no version rather than raising into a poll tick.
    """
    try:
        exe: str | None = proc.exe()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        exe = None
    version = parse_claude_version(exe)
    if version:
        return version
    try:
        cmdline = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return None
    return parse_claude_version(cmdline[0]) if cmdline else None


def running_claude_version(pid: int) -> str | None:
    """Best-effort ``claude`` release that a live bridge ``pid`` is running (#1275).

    Read-only process introspection — it observes the tree and mutates nothing (invariant 5).

    Asks the BRIDGE PROCESS ITSELF first. On the native install layout the bridge is exec'd
    straight from the versioned binary (``~/.local/bin/claude`` is a symlink into
    ``versions/``), so one ``exe`` read answers for BOTH bridge modes — the standard
    ``remote-control`` subcommand and the pty ``--remote-control`` flag form under a keeper —
    without depending on either one's distinct process shape. Verified against a live bridge
    and its worker on the dogfood host: both resolve to ``…/claude/versions/2.1.251``.

    Only if that yields nothing does it fall back to the bridge's DIRECT children, which is
    the route #1275 measured: a standard bridge's SDK worker execs
    ``…/claude/versions/<version> --print --sdk-url …``, so the version is in its ``argv[0]``
    even on a layout whose launcher is a wrapper rather than the versioned binary itself.
    Direct children only, never the recursive tree — a bridge's descendants include language
    servers and tools carrying OTHER version managers' paths, and widening the walk only adds
    chances to match one of those.

    The child ``comm`` name #1275 lists as a last resort is deliberately NOT consulted:
    ``exe`` is readable on the same terms, is not truncated at 15 characters, and is not
    platform-dependent in its naming, so it strictly dominates.

    Returns ``None`` — never a stale or guessed value — for a dead/denied/unreadable process
    and for any install layout the parse doesn't recognise.
    """
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
        return None
    version = _proc_claude_version(proc)
    if version:
        return version
    try:
        children = proc.children()
    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        psutil.ZombieProcess,
        OSError,
        RuntimeError,
    ):
        # OSError too: this runs inside poll_once's tick, and a raw errno from child
        # enumeration must degrade to "no version", never abort the whole poll.
        # RuntimeError: `children()` reads the epoch, which raises on a procfs with no
        # `btime` (#1402); same degrade.
        return None
    for child in children:
        version = _proc_claude_version(child)
        if version:
            return version
    return None


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


def is_windows() -> bool:
    """Whether we're on Windows — the seam for the "reap the tree, not just the child" guards.

    Isolated behind a function (like ``login_shepherd._is_win32``) so a POSIX test can drive a
    win32-only branch by patching THIS, instead of ``monkeypatch.setattr(sys, "platform", …)``
    — which mutates the interpreter-wide singleton for every module and every live thread in
    the xdist worker, and incidentally flips ``shutil.which``/``os.path`` behaviour too.
    """
    return sys.platform == "win32"


def force_kill_tree(pid: int, *, wait_timeout: float | None = None) -> None:
    """Best-effort hard kill of ``pid`` and all its descendants.

    The graceful-stop fallback: used when a bridge ignores SIGINT/CTRL_BREAK, or
    to reap a wrapper process (e.g. a Windows ``.cmd`` shim) that outlives the
    bridge it launched. A dead or absent PID is a no-op.

    It does **not** verify process identity: a REUSED pid is killed along with its whole
    tree. Callers must gate on a create-time match themselves (see :func:`kill_if_match`).

    ``wait_timeout`` opts into waiting (bounded) for the reaped processes to actually
    die. Killing is ASYNCHRONOUS — SIGKILL is delivered, not awaited, and Windows
    ``TerminateProcess`` likewise returns before the process is gone — so without this
    a caller that immediately re-checks liveness can still see a target alive. That
    matters when the next step is gated on death: ``runner``'s poison heal clears a
    ``bridge-pointer.json`` whose recorded pid is the DESCENDANT, and
    ``pointers.is_live`` refusing sends it back into the reattach loop the heal exists
    to break.

    **Opt-in, not the default**, because waiting blocks the calling thread: the
    ``claustrum_daemon`` call site runs synchronously on the event loop (deliberately —
    an await there would let the subprocess transport tear down before its ``kill()``),
    and must not gain a wait. Callers that can afford to block (a thread, or
    ``asyncio.to_thread``) pass a timeout; everyone else keeps fire-and-forget.

    ⚠️ **Never pass ``wait_timeout`` for a pid you hold a ``Popen`` / asyncio subprocess
    transport for.** On POSIX ``psutil.Process.wait()`` goes through ``os.waitpid``, so it
    REAPS a process that is our own child — and ``subprocess.Popen._try_wait`` then
    swallows the ``ChildProcessError`` and records ``returncode = 0``. A bridge we just
    SIGKILLed would be observed as a **clean exit 0** instead of ``-SIGKILL``, i.e.
    STOPPED instead of CRASHED (with an asyncio transport the child watcher logs "Unknown
    child process" and reports 255 instead).

    The only caller passing it (``runner``'s poison heal) is safe **solely because it is
    gated on Windows**, where ``psutil.Process.wait()`` does not go through ``os.waitpid``
    and so cannot reap our own child. It is NOT safe by virtue of reaping descendants:
    ``targets`` includes the root (see below), and that call site passes the very pid it
    holds a ``Popen`` for. Dropping the platform gate is therefore what makes it unsafe —
    which is precisely the scenario the reader of this paragraph is likely to be in.
    ``runner._await_exit``'s force-kill fallback and ``pty_keeper``'s post-kill poll are
    the two places that look like they want this parameter and must NOT get it.
    """
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return
    try:
        targets = proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, RuntimeError):
        # `children()` reads the epoch (`create_time()` without `monotonic`) to tell a
        # reused pid from a real child, and that ends in `boot_time()`, which raises a
        # bare RuntimeError on a procfs with no `btime` line (#1402). The tree is what is
        # unreadable there, not the root: returning would be the silent no-kill, and a
        # raise aborted `stop()` past its STOPPED transition. Kill the root anyway.
        targets = []
    targets.append(proc)
    for p in targets:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    if wait_timeout is None:
        return
    # Bounded and best-effort. The guard is LOAD-BEARING, not belt-and-braces: `wait_procs`
    # returns (gone, alive) for a plain timeout, but its per-process `proc.wait()` is
    # `@wrap_exceptions`-decorated on Windows, so an OSError from the cext surfaces as
    # `AccessDenied`/`NoSuchProcess` and propagates out. One protected process would then
    # abort the wait for every remaining target — degrading to the old fire-and-forget
    # behaviour, which is exactly what the caller had before, so swallowing is right.
    # Never let a psutil failure here undo the kills we already delivered.
    try:
        _, alive = psutil.wait_procs(targets, timeout=wait_timeout)
    except Exception as exc:  # noqa: BLE001 — the kills already landed; the wait is a bonus
        _log.debug("force_kill_tree: waiting for %s's tree failed: %s", pid, exc)
        return
    if alive:
        _log.debug(
            "force_kill_tree: %d of %d processes under %s outlived the %ss wait",
            len(alive),
            len(targets),
            pid,
            wait_timeout,
        )


def owned_pids(root_pids: Iterable[int]) -> set[int]:
    """PIDs a set of managed bridge roots owns — the roots plus their readable descendants.

    A managed ``claude remote-control`` bridge is the parent process of every working
    session it spawns — ``agents --json`` reports each session's pid as a child of the
    bridge, or (an in-process flag-form pty) the bridge pid itself. Membership in this
    set is the authoritative "Clauster owns this session" signal (#820) that cwd
    containment alone can't give: an external SSH/terminal ``claude`` sharing a bridge's
    cwd descends from no managed root, so it isn't here.

    **Per-root, fail closed.** A root whose children can't be enumerated —
    ``AccessDenied`` (hardened ``/proc``, ``hidepid``, a restricted container) — or that
    is dead/absent (``NoSuchProcess``/``ZombieProcess``) contributes only its own pid,
    never any descendants. So a bridge whose tree we can't read owns (as far as we can
    prove) just its own process, and a child session it spawned reads EXTERNAL rather
    than being trusted on cwd alone. Crucially this is **per root**: an unreadable root
    never discards a *co-located* readable root's descendants — the roots for one cwd are
    walked independently and unioned, so one bridge's inaccessibility can't flip another
    co-located bridge's genuine children to EXTERNAL.
    """
    roots = tuple(root_pids)
    owned: set[int] = set(roots)  # the roots themselves are owned
    for pid in roots:
        try:
            owned.update(child.pid for child in psutil.Process(pid).children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, RuntimeError):
            # RuntimeError: `children()` reads the epoch, which raises on a procfs with no
            # `btime` (#1402). The root still counts as owned; its tree is unreadable.
            continue
    return owned


def reap_if_exited(pid: int) -> None:
    """Best-effort non-blocking reap of one of our own children (avoid zombies).

    Safe to call on a non-child or already-reaped PID. A no-op on Windows, which
    has no ``waitpid``/``WNOHANG`` and does not leave zombies to reap.
    """
    if not hasattr(os, "WNOHANG"):  # Windows: no child reaping needed
        return
    try:  # pragma: skip-on-win
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):  # pragma: skip-on-win
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


def bridge_env_overlay(
    path_append: list[str] | None = None,
    env: dict[str, str] | None = None,
    *,
    path_prepend: list[str] | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the env overlay for a bridge subprocess from operator-config knobs.

    Returns an ``extra`` mapping to hand :func:`child_env`: the operator's
    ``env`` map, any caller ``extra`` (e.g. resume-recap flags), and — when
    ``path_prepend`` and/or ``path_append`` is set — a merged ``PATH`` =
    ``~``-expanded prepend dirs, then the base ``PATH``, then the ``~``-expanded
    append dirs, joined with ``os.pathsep``. The base is the operator's own
    ``env['PATH']`` when they set one, otherwise the inherited service ``PATH`` —
    so appends never silently discard an explicit operator override. **Prepend**
    dirs sit BEFORE the base so they WIN name resolution (``node_from_nvm`` uses it
    so nvm's node beats a distro ``/usr/bin/node``, #1018); **append** dirs sit
    after so they only fill a gap. Neither replaces the base, keeping the base
    service toolchain resolvable.

    Returning only the overlay (not the full env) lets the result flow through
    :func:`child_env`, which scrubs Clauster secrets one more time — so an
    operator ``env`` key matching a scrubbed secret name is dropped and can never
    re-introduce a credential the firewall removed.
    """
    overlay: dict[str, str] = {}
    if env:
        overlay.update(env)
    if extra:
        overlay.update(extra)
    prepend = [os.path.expanduser(p) for p in (path_prepend or []) if p]
    append = [os.path.expanduser(p) for p in (path_append or []) if p]
    if prepend or append:
        # Prefer an operator-supplied env['PATH'] as the base (popped so it
        # isn't left stale), else the inherited service PATH. Prepend dirs go
        # BEFORE the base so they WIN name resolution (e.g. node_from_nvm's node,
        # #1018); append dirs go after so they only fill a gap.
        inherited = overlay.pop("PATH", os.environ.get("PATH", ""))
        parts = prepend + ([inherited] if inherited else []) + append
        overlay["PATH"] = os.pathsep.join(parts)
    return overlay


def resolve_nvm_default_node_bin_dir(
    nvm_dir: str | None = None, *, timeout: float = 5.0
) -> str | None:
    """Resolve nvm's ``default`` node version's bin dir, or ``None`` if unavailable.

    Claude Code spawns MCP **stdio** servers by exec'ing the configured ``command``
    directly (``execvp`` / ``sh -c``; ``sh`` is dash and ignores ``BASH_ENV``), not
    through a login or non-interactive bash shell. So a `command: "npx"` MCP server
    can only resolve `npx`/`node` from the bridge subprocess's own ``PATH`` — and
    nvm's bin dir is version-specific, so it is deliberately never baked into a
    static ``path_append`` (issue #792). This resolves it dynamically, the same way
    the verified stable-shim workaround does: source ``$NVM_DIR/nvm.sh`` and ask
    ``nvm which default``, so the result tracks nvm's current default across node
    upgrades instead of pinning a version string that rots.

    POSIX-only (nvm is a bash shell function, not a binary) — always ``None`` on
    Windows. Best-effort everywhere else: returns ``None`` rather than raising when
    ``bash`` isn't on ``PATH``, no nvm install exists at ``nvm_dir``, no `default`
    alias is set, or the lookup errors/times out — a broken/absent nvm must never
    block a bridge spawn. ``nvm_dir`` defaults to ``$NVM_DIR`` then ``~/.nvm``,
    mirroring nvm's own resolution order.
    """
    if sys.platform == "win32":
        return None
    bash = shutil.which("bash")
    if not bash:
        return None
    nvm_home = os.path.expanduser(nvm_dir or os.environ.get("NVM_DIR") or "~/.nvm")
    # NVM_DIR is passed via the subprocess env, never interpolated into the script
    # text, so a path containing shell metacharacters can't inject into the script.
    script = (
        '[ -s "$NVM_DIR/nvm.sh" ] || exit 1; '
        '. "$NVM_DIR/nvm.sh" --no-use >/dev/null 2>&1; '
        "nvm which default 2>/dev/null"
    )
    try:
        result = subprocess.run(
            [bash, "-c", script],
            env={**os.environ, "NVM_DIR": nvm_home},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.debug("node_from_nvm: nvm lookup failed (%s): %s", nvm_home, exc)
        return None
    node_path = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    # Require an EXECUTABLE regular file: a present-but-non-executable node would still get
    # its dir appended to PATH, and node/npx MCP servers would then fail with the exact
    # connection symptom this feature exists to fix — so treat non-executable as unresolved.
    if not node_path or not os.path.isfile(node_path) or not os.access(node_path, os.X_OK):
        _log.debug(
            "node_from_nvm: no resolvable executable nvm 'default' node (NVM_DIR=%s)", nvm_home
        )
        return None
    return os.path.dirname(node_path)


# Process-wide memo for the nvm bin dir. Both hot paths that need it — bridge spawns
# (``claude.node_from_nvm``) and the ``clauster doctor`` panel (re-hit on every dashboard
# refresh / concurrent tab) — go through :func:`cached_nvm_default_node_bin_dir`, so a
# slow/broken ``$NVM_DIR`` is probed at most once per process, not per request. Reset only
# in tests (see conftest) to restore per-test isolation.
_nvm_bin_dir_cache: str | None = None
_nvm_bin_dir_resolved: bool = False


def cached_nvm_default_node_bin_dir() -> str | None:
    """Resolve nvm's default node bin dir once per process, then serve from the memo.

    Wraps :func:`resolve_nvm_default_node_bin_dir`, which shells ``bash`` (up to its
    timeout on a slow home mount) — so callers on hot paths (bridge spawn AND the doctor
    panel) share ONE probe rather than each re-shelling on every request. A restart
    re-resolves (fresh process), picking up an nvm-default change. No lock: a concurrent
    first-call double-resolve is harmless (idempotent compute, atomic assignment) and the
    value is written before the flag, so a racing reader that sees the flag sees the dir.
    """
    global _nvm_bin_dir_cache, _nvm_bin_dir_resolved
    if not _nvm_bin_dir_resolved:
        _nvm_bin_dir_cache = resolve_nvm_default_node_bin_dir()
        _nvm_bin_dir_resolved = True
    return _nvm_bin_dir_cache
