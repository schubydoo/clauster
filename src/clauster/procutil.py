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
    prune the card out from under it. Whether a ``--bg --rc`` job should count as bridge
    liveness is a real question, but it is one to answer deliberately, not by widening a
    delete gate.
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
    """Epoch create-time of a live, non-zombie PID, else None."""
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


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


def is_bridge_process(pid: int) -> bool:
    """Whether ``pid`` is a live, non-zombie ``claude`` **bridge** process (#1096).

    The cmdline counterpart to :func:`is_keeper_process`, for deciding whether an EXTERNAL
    working session is an unmanaged *bridge* or merely a hand-run ``claude`` sharing a
    project directory. The phantom-prune needs that distinction: its premise is "the bridge
    IS alive, just unmanaged", and deleting a resumable card because an operator opened a
    terminal in the project would be wrong. Fails closed on any psutil error.
    """
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return is_bridge_cmdline(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
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
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None
        if parent is None or parent.pid <= 1:
            return None
        proc = parent
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
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
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
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the env overlay for a bridge subprocess from operator-config knobs.

    Returns an ``extra`` mapping to hand :func:`child_env`: the operator's
    ``env`` map, any caller ``extra`` (e.g. resume-recap flags), and — when
    ``path_append`` is set — a merged ``PATH`` = base ``PATH`` followed by the
    ``~``-expanded append directories joined with ``os.pathsep``. The base is the
    operator's own ``env['PATH']`` when they set one, otherwise the inherited
    service ``PATH`` — so ``path_append`` appends to (never silently discards) an
    explicit operator override. Appending (never replacing) keeps the base service
    toolchain resolvable.

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
    if path_append:
        expanded = [os.path.expanduser(p) for p in path_append if p]
        if expanded:
            # Prefer an operator-supplied env['PATH'] as the base (popped so it
            # isn't left stale), else the inherited service PATH.
            inherited = overlay.pop("PATH", os.environ.get("PATH", ""))
            parts = ([inherited] if inherited else []) + expanded
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
