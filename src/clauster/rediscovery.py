"""Reattach / adopt / rediscover surface for managed bridges (part of #1157).

:class:`Rediscovery` is the read-mostly collaborator extracted from
:class:`~clauster.runner.SessionRunner`. It owns the cold-start reattach of persisted rows,
the runtime take-over of externally-started bridges (:meth:`adopt`), and the poll-time
adoption of rows another process created (:meth:`_adopt_rows_from_store`).

Ownership rules (load-bearing):

- **It holds no registry of its own.** Every instance it materializes is written into the
  ONE :class:`~clauster.runner_state.RunnerState` (passed in as ``registry``): reattach adds
  to ``registry._instances`` and persists through ``registry._persist``, on the event loop,
  under the same per-project spawn lock and cross-process bridge flock the spawn path uses.
  :meth:`adopt` takes ``registry._spawn_lock_for`` + ``registry._bridge_flock`` exactly as
  before (the anti-orphan gate), and :meth:`_resync_pids_from_row` re-checks the generation
  under that spawn lock with no ``await`` between the check and the mutation.
- **The two bridge modes stay separate.** A pty row is answered by the keeper sidecars, a
  standard row by the Anthropic pointer; nothing here unifies them.
- **Still-on-``runner`` helpers are injected**, mirroring the other collaborators
  (``BridgeLaunch`` gets ``stderr_path_for``, ``RecordFacade`` gets ``project_path``): the
  sidecar reader, the two log-path resolvers, the discovery snapshot, the log-set key, and
  the project-instance lookup are passed as callables, so this collaborator never holds a
  back-reference to the runner.
- **The row/pointer/sidecar field decoders live in :mod:`clauster.field_decode`** — shared
  with the still-on-``runner`` methods that also decode rows, so both import the one copy
  without a circular import. ``_PROC_START_TOLERANCE`` and ``_WORKTREE_NAME_RE`` moved here
  because this module is now their only user. The public exception classes stay on
  :mod:`clauster.runner`; :meth:`adopt` imports them lazily (see there) to break the cycle.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from . import config, pointers, procutil
from .config import (
    PERMISSION_MODES,
    RESUME_MODES,
    SANDBOX_MODES,
    SPAWN_MODES,
    ClausterConfig,
    PermissionMode,
    ResumeMode,
    SandboxMode,
    SpawnMode,
)
from .field_decode import (
    _pointer_start_ticks,
    _row_float,
    _row_int,
    _row_str,
    _sidecar_notice,
    _ticks_on_exact_match,
)
from .models import BridgePointer, InstanceStatus, Project, RemoteControlInstance
from .runner_state import RunnerState

_log = logging.getLogger("clauster.rediscovery")

# Slack when matching a keeper sidecar's recorded proc-start against the pointer's
# (the two epochs are derived independently). Mirrors procutil.is_live_bridge's
# default tolerance so the two PID-reuse checks can't disagree.
_PROC_START_TOLERANCE = 2.0
# The exact shape `_pty_worktree_name` mints: "clauster-" + the first 8 chars of an RFC
# 4122 instance_id, which are always lowercase hex. A RECOVERED name (#1241) comes off a
# keeper sidecar or a state.json row — both on-disk files a hand edit or a corrupt write
# can put anything into — and is then interpolated into `--worktree <name>` argv AND into
# `<project>/.claude/worktrees/<name>` for the stop-time git unlock. So it is matched
# against the minting rule rather than merely type-checked: anything else (a traversal
# segment, an absolute path, a flag-looking token) is not a name we could have produced,
# and falls back to the derived value instead of reaching a subprocess.
_WORKTREE_NAME_RE = re.compile(r"clauster-[0-9a-f]{8}\Z")


class Rediscovery:
    """Reattach persisted rows, adopt external bridges, and rediscover survivors."""

    def __init__(
        self,
        *,
        config: ClausterConfig,
        log_dir: Path,
        registry: RunnerState,
        read_sidecar: Callable[[Path], dict | None],
        raw_log_path_for: Callable[[Path], Path],
        latest_debug_log_for: Callable[[str], Path | None],
        discovered: Callable[[], dict[str, Project]],
        log_set_key: Callable[[str], str],
        get_instance_for_project: Callable[[str], RemoteControlInstance | None],
    ) -> None:
        """Bind to the shared registry, config, log dir, and the injected runner helpers.

        ``registry`` is the runner's ONE :class:`RunnerState`, so every instance a reattach
        materializes reaches the single source of truth. The callables are still-on-``runner``
        helpers this collaborator calls but does not own (the sidecar reader, log-path
        resolvers, discovery snapshot, log-set key, and project-instance lookup), stored under
        the names the moved method bodies already use so those bodies are unchanged.
        """
        self._config = config
        self._log_dir = log_dir
        self._registry = registry
        self._read_sidecar = read_sidecar
        self._raw_log_path_for = raw_log_path_for
        self._latest_debug_log_for = latest_debug_log_for
        self._discovered = discovered
        self._log_set_key = log_set_key
        self._get_instance_for_project = get_instance_for_project

    def _keeper_sidecars_for(self, name: str) -> list[Path]:
        """Keeper sidecars belonging to *name* exactly — the anchored form of the bare glob.

        ``glob(f"{name}-*.keeper.json")`` is an UNANCHORED prefix match, and
        ``PROJECT_NAME_RE`` allows ``-`` (``discovery.py``), so for project ``app`` it also
        returns sibling ``app-staging``'s sidecars. Nothing downstream re-pins a candidate to
        this project, so a sibling's live keeper could be adopted as *this* project's RUNNING
        instance — and stop()/poll_once would then reap another project's bridge. Anchor the
        stem exactly as :meth:`_latest_debug_log_for` does, on the two trailing digit groups
        of ``<name>-<ms>-<seq>`` (``_unique_log_path``); that rejects siblings while keeping
        this project's whole set, and only ``.keeper.json`` matches (never its ``.log`` /
        ``.keeper.log`` / ``.screen.json`` spawn-set kin).

        Returned in glob order — each caller applies the ordering it wants. Whatever
        ``Path.glob`` does with a missing or unreadable log dir is unchanged, since this is
        the same call filtered (measured on 3.13: it yields nothing rather than raising).
        """
        stem_re = re.compile(rf"{re.escape(name)}-(\d+)-(\d+)\.keeper\.json")
        return [
            p for p in self._log_dir.glob(f"{name}-*.keeper.json") if stem_re.fullmatch(p.name)
        ]

    def _recover_keeper_pid(
        self,
        name: str,
        bridge_pid: int | None,
        bridge_proc_start: float | None,
        *,
        # Keyword-REQUIRED, no default (#1399). Threading this by hand is how two review
        # rounds each found a caller silently left on the drifting epoch arm; without a
        # default a missed call site is a type error instead.
        bridge_start_ticks: int | None,
    ) -> int | None:
        """Find a rediscovered pty bridge's keeper pid from its sidecar.

        After a Clauster restart we know the bridge pid + proc-start (from the
        pointer-walk) but not the timestamped ``--debug-file`` path, so the sidecar
        can't be addressed directly. Take this project's sidecars — anchored to the
        ``<name>-<ms>-<seq>`` stem by :meth:`_keeper_sidecars_for`, so a sibling
        ``app-staging``'s sidecar can never be read here as ``app``'s — and match on
        ``bridge_pid`` **and** ``bridge_proc_start`` — the latter is the
        PID-reuse defense: a stale sidecar that merely recycled the pid is rejected,
        so stop()/poll_once can never reap an unrelated process tree. The keeper is
        alive iff the bridge is (it holds its terminal), already confirmed before this.

        ``bridge_start_ticks`` decides when the caller AND the sidecar both carry it: both
        are raw ``/proc/<pid>/stat`` field-22 counts, so the match is exact and immune to the
        clock drift that moves every epoch on this host (#1399). The sidecar's recorded
        ``boot_id`` (#1401) settles what ticks alone cannot — ticks restart at zero each boot,
        so a sidecar that survived a reboot could collide on both pid and count. A boot id that
        differs from the live one rejects such a sidecar on identity, and a match lets the exact
        tick pairing stand as a complete identity without the coarse epoch, which an NTP step
        could not survive. A pre-#1401 sidecar (no boot id) keeps that coarse epoch conjunct.
        Otherwise proc-start falls back to the same slack :func:`procutil.is_live_bridge` uses
        (the pointer's stored value and the sidecar's psutil create-time are derived
        independently, so exact float equality would be brittle); when either side is unknown,
        fall back to the pid-only match.
        """
        if bridge_pid is None:
            return None
        for sidecar in sorted(self._keeper_sidecars_for(name)):
            info = self._read_sidecar(sidecar)
            if info is None or info.get("bridge_pid") != bridge_pid:
                continue
            # Ticks first when BOTH sides have them (#1399). The epoch arm below compares
            # the sidecar's frozen `psutil.create_time` against a value the pointer walk
            # recomputes with TODAY's btime, so a clock correction larger than
            # `_PROC_START_TOLERANCE` (2.0s; a 4s spread was measured) makes a live keeper
            # fail to match its own bridge — and the rediscovered pty bridge then orphans it,
            # because `stop()` never learns a keeper pid to clean up. The sidecar and the
            # pointer both carry raw field-22 ticks, so that comparison is exact and immune.
            ps = info.get("bridge_proc_start")
            try:
                sidecar_epoch = (
                    float(ps)
                    if isinstance(ps, (int, float)) and not isinstance(ps, bool)
                    else None
                )
            except OverflowError:
                # A sidecar is an on-disk file (see the negative-pid note in the reattach
                # leg): a hand-edited int wider than a float must read as "no comparable
                # epoch", not raise out of `rediscover`.
                sidecar_epoch = None
            epoch_gap = (
                abs(sidecar_epoch - bridge_proc_start)
                if sidecar_epoch is not None and bridge_proc_start is not None
                else None
            )
            sidecar_ticks = _row_int(info.get("bridge_start_ticks"))
            sidecar_boot = _row_str(info.get("boot_id"))
            live_boot = procutil.proc_boot_id()
            if sidecar_boot is not None and live_boot is not None:
                # The sidecar records the boot it was written in (#1401), and `pty_keeper` now
                # writes it. The bridge here is live in the CURRENT boot (the caller gated on
                # `is_live_bridge` first), so a sidecar boot id that differs names an EARLIER
                # boot's process — reject on identity, immune to the clock STEP the coarse epoch
                # below could not survive. A match confirms same-boot; the SAME-PROCESS-within-the-
                # boot question is still decided on the exact tick pairing (a pid recycled within
                # the boot differs by whole ticks), with the epoch NOT consulted, so an NTP step
                # larger than `_DRIFT_EPOCH_TOLERANCE` no longer orphans a live keeper. When the
                # ticks are unavailable (a transient field-22 read miss), fall back to the coarse
                # `_PROC_START_TOLERANCE` epoch bound the boot-id-less path uses — boot id alone
                # proves same-boot, NOT same process, and the recovered keeper pid reaches
                # `_cleanup_keeper`'s `force_kill_tree`, so it must never stand on pid+boot alone.
                if sidecar_boot != live_boot:
                    continue
                if bridge_start_ticks is not None and sidecar_ticks is not None:
                    if sidecar_ticks != bridge_start_ticks:
                        continue
                elif epoch_gap is not None and epoch_gap > _PROC_START_TOLERANCE:
                    continue
            elif bridge_start_ticks is not None and sidecar_ticks is not None:
                # No boot id (a pre-#1401 sidecar) or no live id: keep the exact-ticks + coarse-
                # epoch pairing. Ticks restart at zero each boot, so on their own they cannot
                # rule out a STALE sidecar that survived a reboot and happens to collide on both
                # the bridge pid and the tick count; the epoch rejects that for free (a reboot
                # moves it by uptime + downtime). Dropping the conjunct here would NARROW the
                # PID-reuse defense — and the recovered keeper pid reaches `_cleanup_keeper`'s
                # `force_kill_tree`, where a different live keeper on that pid passes the cmdline
                # gate and takes another session's bridge down with it.
                if sidecar_ticks != bridge_start_ticks or (
                    epoch_gap is not None and epoch_gap > procutil._DRIFT_EPOCH_TOLERANCE
                ):
                    continue
            elif epoch_gap is not None and epoch_gap > _PROC_START_TOLERANCE:
                continue
            keeper_pid = info.get("keeper_pid")
            if isinstance(keeper_pid, int) and not isinstance(keeper_pid, bool):
                return keeper_pid
            return None
        return None

    def _recover_keeper_identity(
        self,
        name: str,
        bridge_pid: int | None,
        bridge_proc_start: float | None,
        *,
        bridge_start_ticks: int | None,  # keyword-required — see _recover_keeper_pid
    ) -> tuple[int | None, float | None, int | None]:
        """Return the ``(keeper_pid, keeper_proc_start, keeper_start_ticks)`` TRIO (#1178).

        Thin wrapper over :meth:`_recover_keeper_pid` that snapshots the pid's start
        identity at the moment of classification — the same "captured when the keeper was
        classified" value :func:`clauster.pty_keeper.stop_keeper` documents for its
        ``expect_create_time`` guard.

        Both halves come from ONE :func:`procutil.proc_start_pair` read (#1402), never two
        samples: two reads can straddle a death plus a pid recycle and yield halves that
        describe DIFFERENT processes, and :func:`procutil.is_live_process` would then
        authenticate the recycled occupant — it matches the ticks exactly (they are the new
        process's) and the epoch only within ``_DRIFT_EPOCH_TOLERANCE``.

        Callers must publish **all three or none**. A keeper pid carrying the *previous*
        generation's start values is worse than carrying none: the comparison then fails for
        a keeper that is genuinely alive, and :meth:`forget` would drop the record of a
        running process — the exact failure the trio exists to prevent. ``None`` for either
        start value (psutil error, no ``/proc``, or a keeper that exited between the reads)
        is the honest unknown; with neither the gate degrades to cmdline-only.

        The snapshot is **fenced by validation time** (review catch): if the keeper exits
        and the OS recycles its pid between :meth:`_recover_keeper_pid` and the start read,
        the values would otherwise authenticate the NEW occupant — and a keeper-shaped
        occupant would strand ``forget`` with ``InstanceStillLive``, the very bug this
        defends against. A process created *after* validation began cannot be the keeper
        that validation saw, so it is rejected and the whole recovery reports "no keeper" —
        honest, since the validated keeper is gone. The fence reads the EPOCH because that
        is the only half comparable with a wall-clock instant; ticks are boot-relative. It
        therefore cannot fire on a host with no comparable epoch, exactly as before.
        """
        validated_at = time.time()
        keeper_pid = self._recover_keeper_pid(
            name, bridge_pid, bridge_proc_start, bridge_start_ticks=bridge_start_ticks
        )
        if keeper_pid is None:
            return None, None, None
        created, ticks = procutil.proc_start_pair(keeper_pid)
        if created is not None and created > validated_at:
            # pid recycled mid-recovery: the keeper we validated is gone. Drop the ticks
            # too — they are the same rejected process's, and publishing them beside a
            # None pid is exactly the split identity the trio rule forbids.
            return None, None, None
        return keeper_pid, created, ticks

    def _persisted_for_project(
        self,
        project_name: str,
        *,
        unclaimed_only: bool = False,
        resume_mode: ResumeMode | None = None,
    ) -> tuple[str, dict] | None:
        """Return ``(instance_id, fields)`` for the first persisted record for ``project_name``.

        Since issue 777 ``_persisted`` is keyed by ``instance_id``; the project name lives
        in the ``"project_name"`` field of each value dict.  Returns ``None`` when no match.
        Used by :meth:`_stopped_from_persisted` and :meth:`_reattach_pty_from_sidecar` to
        look up a persisted record by project without assuming a project-keyed dict.

        ``unclaimed_only`` skips records already materialized in ``self._registry._instances``. The
        pointer walk needs that: it resolves by PROJECT and adopts the id it finds, but
        since #1088 the row pass may already have inserted cards for several of that
        project's rows. First-match would then hand the walk an id that is somebody else's
        card, and the live bridge's fields would be written over a different session's
        record — losing it, because the next ``_persist`` rewrites that row too.

        ``resume_mode`` narrows to rows of that mode. The keeper-sidecar leg needs it,
        because first-match over a project's rows is arbitrary in MODE as well as identity: a
        project holding a standard row and a pty row would hand the keeper-sidecar leg
        the standard row, whose ``resume_mode`` makes ``_reattach_pty_from_sidecar``
        return before it ever globs a sidecar — leaving a live detached keeper unmanaged
        behind STOPPED cards, which is the leak the leg exists to prevent. Compared
        through :meth:`_saved_modes`, not the raw field, so a pre-#1088 row with no
        recorded mode is judged by the same coercion the caller will apply.
        """
        for iid, fields in self._registry._persisted.items():
            if fields.get("project_name") != project_name:
                continue
            if unclaimed_only and iid in self._registry._instances:
                continue
            if resume_mode is not None and self._saved_modes(fields)[2] != resume_mode:
                continue
            return iid, fields
        return None

    def _saved_modes(self, saved: dict) -> tuple[SpawnMode, PermissionMode, ResumeMode]:
        """Coerce persisted spawn/permission/resume modes against the allowed sets.

        A hand-edited or corrupt ``state.json`` that holds an unknown mode must not
        fail the (Literal-typed) model and abort startup — fall back to the
        configured defaults instead. ``launch_mode`` lives on ``ClaudeConfig``, the
        other two on ``InstanceDefaults``.
        """
        defaults = self._config.instance_defaults
        sm = saved.get("spawn_mode")
        pm = saved.get("permission_mode")
        rm = saved.get("resume_mode")
        return (
            sm if sm in SPAWN_MODES else defaults.spawn_mode,
            pm if pm in PERMISSION_MODES else defaults.permission_mode,
            rm if rm in RESUME_MODES else self._config.claude.launch_mode,
        )

    def _sweep_modes(self, saved: dict) -> set[str]:
        """Return the resume-modes to sweep for ``saved``; ALL of them when it only guessed.

        :meth:`_saved_modes` coerces a row with no recorded ``resume_mode`` to the host's
        configured ``claude.launch_mode``. That is a fact about this deployment, not about
        the row, so routing a *liveness* check on it repeats the mistake the pointer walk
        already refuses to make (see its ``resume_mode`` note): on a ``launch_mode: pty``
        host a pre-#1088 row — which necessarily ran a STANDARD bridge, since it predates
        pty — would coerce to "pty", skip the pointer check, and be carded STOPPED while
        its bridge is still live.

        So a row that RECORDED its mode is swept by that one mechanism; a row that did not
        is swept by every mechanism. Costs some over-hiding for legacy rows on a project
        that genuinely has a live unclaimed bridge, which is the trade this pass already
        makes everywhere else: a hidden card is recoverable, a duplicate bridge is not.
        """
        if saved.get("resume_mode") in RESUME_MODES:
            return {self._saved_modes(saved)[2]}
        return set(RESUME_MODES)

    @staticmethod
    def _recovered_worktree_name(value: object) -> str | None:
        """Coerce a worktree name read back off disk, or ``None`` (#1241).

        Applied to both sources of a non-derived name — the keeper sidecar and the
        persisted row — so the argv and the git-unlock path can only ever see a name
        Clauster itself could have minted. See :data:`_WORKTREE_NAME_RE` for why the
        check is the minting rule rather than a type test. Absent, wrong-typed, or
        non-conforming values return ``None``, which sends the caller back to the derived
        name: fail closed to the safe value rather than to a stranger's path.
        """
        if not isinstance(value, str) or not _WORKTREE_NAME_RE.match(value):
            return None
        return value

    @staticmethod
    def _saved_sandbox(saved: dict) -> SandboxMode:
        """Coerce a persisted ``sandbox_mode`` against the allowed set (#780).

        Absent (pre-#780 state.json) or corrupt values fall back to ``"default"`` —
        the safe no-flag behavior — so a rebuilt STOPPED card offers the same sandbox
        choice on resume that the original launch used, without failing the model on a
        hand-edited value.

        While the toggle is DISABLED for 1.0 (#1037), every persisted value coerces to
        ``"default"`` so an existing STOPPED card that recorded ``"on"``/``"off"`` resumes
        safely with no flag — matching the (now inert) live behavior.
        """
        if not config.SANDBOX_TOGGLE_ENABLED:
            return "default"
        sb = saved.get("sandbox_mode")
        return cast(SandboxMode, sb) if sb in SANDBOX_MODES else "default"

    def _stopped_from_persisted(self, name: str) -> RemoteControlInstance | None:
        """Rebuild a STOPPED, resumable instance from a gone bridge's persisted record.

        The process is typically gone because the host rebooted while Clauster (and
        the bridge) were down. ``rediscover`` only re-materializes bridges still found
        *alive*; without
        this, a reboot-killed bridge stays in ``state.json`` but never reappears in
        the UI, so the operator loses the (still-resumable) session entirely. We
        instead surface it as a STOPPED card: a "pty" bridge then offers Resume
        (``--continue`` recovers the conversation) and a "standard" bridge offers a
        fresh Start (its environment server died with the host). Returns ``None``
        when nothing was persisted for ``name`` — then there's genuinely no prior
        session to offer, so we don't invent a phantom card.
        """
        hit = self._persisted_for_project(name)
        if hit is None:
            return None
        instance_id, saved = hit
        spawn_mode, permission_mode, resume_mode = self._saved_modes(saved)
        return RemoteControlInstance(
            instance_id=instance_id,
            project=name,
            label=saved.get("label") or name,
            spawn_mode=spawn_mode,
            permission_mode=permission_mode,
            resume_mode=resume_mode,
            # Carry the persisted sandbox choice (#780) so a resume of this STOPPED card
            # re-applies the same --sandbox/--no-sandbox (or neither). pty is out of
            # scope, so a pty record coerces to "default" harmlessly.
            sandbox_mode=self._saved_sandbox(saved),
            # Carried so a Resume of this card lands back in the worktree the session
            # actually ran in, not one derived from an id it was rediscovered under (#1241).
            worktree_name=self._recovered_worktree_name(saved.get("worktree_name")),
            # The process is gone: no pid/keeper/env to recover. intentional_stop is
            # carried through (a host-down bridge has it False — "interrupted" — vs a
            # deliberate Stop's True); both render as a resumable STOPPED card.
            intentional_stop=bool(saved.get("intentional_stop", False)),
            status=InstanceStatus.STOPPED,
            bridge_pid=None,
            bridge_proc_start=None,
            # Named rather than left to the model default: this is a deliberate ZEROING of
            # the identity, so every field of both the bridge and keeper identity belongs here
            # together. Relying on the default would let a future default change silently carry
            # a stale tick count or boot id onto a dead card, where a pid reuse could read as
            # the bridge coming back.
            bridge_start_ticks=None,
            bridge_boot_id=None,
            keeper_pid=None,
            keeper_proc_start=None,
            keeper_start_ticks=None,
        )

    def _modes_with_an_unclaimed_live_bridge(
        self,
        pending: dict[str, tuple[frozenset[str], Path]],
        held_keepers: set[int],
        held_pids: set[int],
    ) -> set[tuple[str, str]]:
        """Return the ``(project, resume_mode)`` pairs that still have an untracked bridge.

        The blocking half of :meth:`rediscover`'s pid-less pass, run in ONE worker thread.
        ``pending`` maps a project to the resume-modes of its uncarded pid-less rows plus
        its path; ``held_keepers``/``held_pids`` are snapshots of the pids LIVE tracked
        instances already own, taken on the event loop so this never iterates
        ``_instances`` off-thread.

        Resolved per MODE because the two bridge shapes are discovered differently and a
        project can hold both: a pty row is answered by the keeper sidecars, everything
        else by the project's Anthropic pointer. A mode with a live-but-untracked bridge is
        returned so the caller leaves those rows uncarded — it deliberately does NOT say
        WHICH row owns the process, because nothing here can (#1108).
        """
        blocked: set[tuple[str, str]] = set()
        for name, (modes, path) in pending.items():
            # Allowlisted per mode, never `modes - {"pty"}`: a denylist would route a future
            # third ResumeMode to the pointer mechanism, silently sweeping it with the wrong
            # one. But an allowlist alone is fail-OPEN — an unmatched mode would get NO sweep
            # and still be carded, because `_sweep_modes` puts it in `pending` (so the
            # `unswept` guard clears) and nothing puts it in `blocked`. Hence the explicit
            # raise: a mode with no sweep must land in the handler below and BLOCK.
            try:
                if unknown := set(modes) - {"pty", "standard"}:
                    raise AssertionError(f"no liveness sweep for resume_mode {sorted(unknown)}")
                if "pty" in modes and self._has_unclaimed_live_keeper(name, held_keepers):
                    blocked.add((name, "pty"))
                if "standard" in modes and self._has_unclaimed_live_pointer(path, held_pids):
                    blocked.add((name, "standard"))
            except Exception:  # noqa: BLE001 - deliberate catch-all, see below
                # This runs inside a to_thread on `rediscover`'s path, which the web app
                # awaits during lifespan STARTUP with no handler of its own — so an escaping
                # exception takes the service down rather than losing one project's sweep.
                # Degrading to BLOCKED keeps the fail-closed posture: the rows stay hidden
                # (recoverable next start) instead of being carded unswept. `exception`, not
                # `warning`: this silently hides cards, so the cause must reach the log —
                # matching the other degrade-and-continue sites in this file.
                _log.exception("rediscover: liveness sweep failed for %s; blocking its rows", name)
                blocked.update((name, mode) for mode in modes)
        return blocked

    @staticmethod
    def _has_unclaimed_live_pointer(path: Path, held_pids: set[int]) -> bool:
        """Whether ``path``'s Anthropic pointer names a live bridge no instance holds.

        The standard-mode counterpart of :meth:`_has_unclaimed_live_keeper`. A project
        publishes at most one pointer, so this cannot distinguish several standard rows —
        it only answers "is the pointer bridge still unaccounted for", which is enough for
        the caller to decline to card them.
        """
        ptr = pointers.pointer_for_project(path)
        if ptr is None or not pointers.is_live(ptr):
            return False
        return ptr.pid not in held_pids

    def _has_unclaimed_live_keeper(self, name: str, held_keepers: set[int]) -> bool:
        """Whether ``name`` still has a live keeper that no tracked instance holds.

        The pid-less pass in :meth:`rediscover` needs this because the pointer walk does
        NOT always run: it skips a project that already has a live row (``row_claimed``)
        or any STARTING/RUNNING instance, both *before* the pointer read and the keeper
        leg. On such a project a pid-less pty row can still own a live detached keeper,
        and carding it STOPPED would offer a Resume that spawns a **second** keeper on
        the same ``--continue`` conversation — the exact leak
        :meth:`_reattach_pty_from_sidecar` exists to prevent.

        Answers only "is one still unaccounted for", never "which row owns it": nothing
        here correlates a keeper to a ROW (that is #1108). So the caller does what the
        phantom-prune does with an ambiguous project — leaves every candidate alone
        rather than guessing — which keeps a live keeper reachable at the cost of the
        stopped cards for that one project staying hidden until it exits.

        ``held_keepers`` is passed in rather than read from ``_instances`` because this
        runs in a worker thread; see :meth:`_modes_with_an_unclaimed_live_bridge`.

        The sweep is anchored to THIS project's ``<name>-<ms>-<seq>`` stem
        (:meth:`_keeper_sidecars_for`), so a live keeper of sibling ``app-staging`` no
        longer answers True for ``app``. The answer now reads exactly as it says: "a live
        keeper of this project is unaccounted for".
        """
        for sidecar in self._keeper_sidecars_for(name):
            info = self._read_sidecar(sidecar)
            if not info or info.get("state") != "ready":
                continue
            keeper_pid = info.get("keeper_pid")
            bridge_pid = info.get("bridge_pid")
            # `> 0` as well as int-not-bool: a sidecar is an on-disk file that can hold a
            # negative pid. `is_keeper_process` now also catches the `ValueError` psutil
            # raises for one, so this is defense in depth rather than the only guard.
            if (
                not isinstance(keeper_pid, int)
                or isinstance(keeper_pid, bool)
                or keeper_pid <= 0
                or not isinstance(bridge_pid, int)
                or isinstance(bridge_pid, bool)
                or bridge_pid <= 0
                or keeper_pid in held_keepers
            ):
                continue
            ps = info.get("bridge_proc_start")
            proc_start = (
                float(ps) if isinstance(ps, (int, float)) and not isinstance(ps, bool) else None
            )
            # Same PID-reuse defense as the reattach: keeper by cmdline AND the bridge
            # matched on its pid + proc-start pair. The sidecar's `boot_id` (#1401) is passed
            # too, so `is_live_bridge` rejects a bridge from an EARLIER boot on identity where
            # the ticks alone (restarting at zero each boot) could collide; it falls back to
            # the coarse epoch for a pre-#1401 sidecar with no boot id.
            if procutil.is_keeper_process(keeper_pid) and procutil.is_live_bridge(
                bridge_pid,
                proc_start,
                start_ticks=_row_int(info.get("bridge_start_ticks")),
                boot_id=_row_str(info.get("boot_id")),
            ):
                return True
        return False

    def _reattach_pty_from_sidecar(self, name: str, saved: dict) -> RemoteControlInstance | None:
        """Reattach a self-spawned pty bridge from its keeper sidecar after a restart.

        A pty (flag-form ``claude --remote-control``) bridge writes no Anthropic
        ``bridge-pointer.json``, so the pointer-walk in :meth:`rediscover` can't see
        it — but its keeper is detached and outlives the restart, recording the
        keeper/bridge pids in the sidecar. Without this, rediscover falls through to
        ``_stopped_from_persisted`` and the card reads STOPPED while a live keeper
        leaks: uncontrollable (Stop/observe gone), and a Resume would spawn a *second*
        keeper. Glob the sidecars newest-first and, when one names a still-live keeper
        (``is_keeper_process`` — cmdline-gated against PID reuse) holding a ready, live
        bridge (pid + proc-start matched), rebuild it as a managed RUNNING instance so
        stop()/poll_once own it again.

        Returns ``None`` when nothing is reattachable (no persisted record, not pty, or
        no live keeper) — rediscover then resurrects the STOPPED card as before. Only a
        sidecar in the ``"ready"`` state reattaches; a bridge still mid-startup falls
        back to STOPPED (the orphan-keeper sweep can reap a genuinely stuck one).

        The sweep reads only sidecars whose stem anchors to THIS project
        (:meth:`_keeper_sidecars_for`), so a sibling ``app-staging`` keeper can never be
        adopted as ``app``'s instance. A sidecar holding a non-positive pid is skipped
        rather than raising: the gate below checks ``> 0``, and ``is_keeper_process``
        fails closed on psutil's ``ValueError`` besides.

        The reattached bridge always gets a **fresh** ``instance_id`` (#1108). It used to
        take one the caller resolved by PROJECT — first match over that project's unclaimed
        pty rows — chosen before anything knew *whose* keeper the glob would find, so the
        keeper's pids were written onto an unrelated session's row and the next
        :meth:`_persist` rewrote that row too, losing it. Correlating the sidecar to a row
        instead would be the ideal answer but is not reachable here: by the time this runs,
        :meth:`_reattach_rows_with_pids` has already carded every row of this project that
        carries a pid (live rows claim the project outright, so the walk skips it; dead ones
        become STOPPED cards, so they are no longer unclaimed). Every row still unclaimed is
        therefore pid-LESS — it has no liveness identity to match a sidecar against — and
        adopting one would be a guess in every case, not just the ambiguous ones.
        ``saved`` still supplies the label and modes (it is the same shape of first match,
        but those are cosmetic and mode-pinned by the caller, not an identity), so the cost
        is one extra card for the project rather than a lost session — the trade the pointer
        leg makes for an ambiguous project too.
        """
        if not saved:
            return None
        spawn_mode, permission_mode, resume_mode = self._saved_modes(saved)
        if resume_mode != "pty":
            return None
        for sidecar in sorted(self._keeper_sidecars_for(name), reverse=True):
            info = self._read_sidecar(sidecar)
            if not info or info.get("state") != "ready":
                continue
            keeper_pid = info.get("keeper_pid")
            bridge_pid = info.get("bridge_pid")
            # `> 0` as well as int-not-bool, mirroring `_has_unclaimed_live_keeper`: a
            # sidecar is an on-disk file that can hold a negative pid.
            if not (
                isinstance(keeper_pid, int)
                and not isinstance(keeper_pid, bool)
                and keeper_pid > 0
                and isinstance(bridge_pid, int)
                and not isinstance(bridge_pid, bool)
                and bridge_pid > 0
            ):
                continue
            ps = info.get("bridge_proc_start")
            bridge_proc_start = (
                float(ps) if isinstance(ps, (int, float)) and not isinstance(ps, bool) else None
            )
            # PID-reuse defense (mirrors _recover_keeper_pid): the keeper must still be
            # a keeper by cmdline, AND the bridge must match pid + proc-start — so a
            # recycled pid can never reattach an unrelated process tree.
            if not procutil.is_keeper_process(keeper_pid):
                continue
            bridge_start_ticks = _row_int(info.get("bridge_start_ticks"))
            # The sidecar's `boot_id` (#1401) is passed to the gate, so `is_live_bridge` rejects
            # a bridge from an EARLIER boot on identity — the more important site, since this
            # rebuilds the RUNNING instance whose `stop()` force-kills the keeper tree. It falls
            # back to the coarse epoch for a pre-#1401 sidecar with no boot id. The reattached
            # instance below is STAMPED with the current boot id regardless, so the persisted row
            # carries the stronger cross-boot defense across the next restart.
            if not procutil.is_live_bridge(
                bridge_pid,
                bridge_proc_start,
                start_ticks=bridge_start_ticks,
                boot_id=_row_str(info.get("boot_id")),
            ):
                continue
            # Re-bind the live tail to the log this bridge is still writing. The sidecar
            # shares its spawn-set stem with the bridge's log (`_sidecar_path_for` is just
            # `<stem>.keeper.json`), so the timestamped — otherwise unrecoverable — log path
            # is derivable from the matched sidecar. Without this, `bridge_debug_log_path`
            # stays None and `/ws/bridge-log` 1008s every connect → the live tail flickers
            # and gives up after a reattach even though the bridge is alive (#584).
            log_path = sidecar.with_name(f"{self._log_set_key(sidecar.name)}.log")
            raw_path = self._raw_log_path_for(log_path)
            # Bind the tail only if the parse-source the WS will actually read exists. The
            # bridge pre-creates it at spawn and is still writing it, so it normally does —
            # but if retention pruned a long-idle bridge's set, leave both None so
            # `/ws/bridge-log` 1008s and the operator sees the "disconnected" banner (a
            # prompt to act) rather than a silently-empty live panel. This keeps the pty
            # path symmetric with the standard path's `_latest_debug_log_for` (#584).
            tail_source = raw_path if raw_path.exists() else None
            log_path = log_path if tail_source is not None else None
            # Snapshotted once `is_keeper_process` above has classified this pid as a keeper,
            # so the pid is carried as a full identity from the moment we adopt it (#1178 /
            # #1402). The sidecar itself records neither half. ONE `proc_start_pair` call, not
            # two reads: separate samples can straddle a pid recycle and describe two
            # different processes, and the coarse epoch conjunct would then authenticate the
            # newcomer on its own exact ticks. Only filesystem reads separate the two, and
            # the pair is an identity rather than a liveness claim, so the gap is benign.
            keeper_proc_start, keeper_start_ticks = procutil.proc_start_pair(keeper_pid)
            # No `instance_id=`: the model mints a fresh one. See the identity paragraph
            # above — nothing here can say which row owns this keeper (#1108).
            return RemoteControlInstance(
                project=name,
                label=saved.get("label") or name,
                spawn_mode=spawn_mode,
                permission_mode=permission_mode,
                resume_mode=resume_mode,
                status=InstanceStatus.RUNNING,
                # Carried onto the instance, not just used for the gate above: without it
                # `rediscover` accepts this bridge on a tick match and the very first
                # `poll_once` then rejects it on the epoch alone. `_reconcile_status` never
                # promotes back, so the card sticks STOPPED under a running keeper — the
                # whole #1399 failure, reproduced on a fully-upgraded host.
                bridge_start_ticks=bridge_start_ticks,
                # Stamped with the CURRENT boot, not carried from the sidecar (which records
                # none): the live keeper checked above proves this bridge is running in this
                # boot, so its boot id is the live one (#1401). Without this the reattached row
                # persists boot-id-less and loses the cross-boot defense after the next
                # restart+reboot, on a row this method itself creates. Read here inside the
                # `to_thread` this method already runs in (see `rediscover`), so no event-loop
                # block.
                bridge_boot_id=procutil.proc_boot_id(),
                intentional_stop=False,
                keeper_pid=keeper_pid,
                keeper_proc_start=keeper_proc_start,
                keeper_start_ticks=keeper_start_ticks,
                bridge_pid=bridge_pid,
                bridge_proc_start=bridge_proc_start,
                bridge_debug_log_path=log_path,
                bridge_raw_log_path=tail_source,
                starter_session_id=info.get("session_id") or None,
                url=info.get("connect_url") or None,
                # Carried across the restart with the URL it explains (#1390). This leg
                # reattaches a `ready` sidecar, which is exactly the state the keeper
                # promotes a screen-fault session into with `connect_url: null` — so
                # dropping the note here would rebuild the card with the missing link and
                # none of the reason, the failure the note exists to close.
                notice=_sidecar_notice(info),
                # The bridge's real `--worktree` name (#1241). Without it a reattach that
                # had to mint a fresh instance_id would derive a name for a worktree that
                # does not exist — resuming into a NEW one and orphaning the original,
                # and leaving the real one locked at stop. Only meaningful for a
                # worktree-mode session; `_pty_worktree_name` gates on spawn_mode anyway.
                worktree_name=self._recovered_worktree_name(info.get("worktree_name")),
            )
        return None

    async def _adopt_rows_from_store(self) -> None:
        """Adopt live instances another process created, so they stop reading EXTERNAL (#1091).

        The server's registry was fixed at startup: ``start_poll_loop`` called
        :meth:`rediscover` once and ``poll_once`` then iterated only ``self._registry._instances``,
        never re-reading the shared store. A bridge started by ``clauster start`` or the MCP
        server was therefore never adopted — the ``agents --json`` cross-check saw its live
        process, found no managed instance owning it, and correctly-by-its-own-logic labelled
        it EXTERNAL/unmanaged, with no controls in the dashboard.

        Refresh the merge base, then take over any persisted row we don't already track whose
        pids are live. **Live rows only**: a dead row is left to :meth:`rediscover` at startup
        rather than resurrected as a STOPPED card on every tick.

        Because :meth:`_refresh_persisted` replaces the base with the CURRENT store contents,
        a row another process *forgot* is simply absent and cannot be re-adopted here — the
        adoption can't undo a ``clauster forget``.
        """
        if not await self._registry._refresh_persisted():
            return  # store read failed; keep the old base rather than acting on nothing
        rows = dict(self._registry._persisted)
        row_pairs = {
            iid: (
                pid,
                _row_float(saved.get("bridge_proc_start")),
                _row_int(saved.get("bridge_start_ticks")),
                _row_str(saved.get("bridge_boot_id")),
            )
            for iid, saved in rows.items()
            if (pid := _row_int(saved.get("bridge_pid"))) is not None
        }
        # Every liveness question this tick needs, answered in ONE thread hop rather than a
        # sequential hop per row: this runs on the poll loop, and rows are never GC'd.
        # The GENERATION we would be overwriting, not just its pids. A `stop()` that lands
        # on the near side of the liveness probe mutates `status` and `intentional_stop`
        # while deliberately leaving `bridge_pid` in place, so a pid-only compare passes
        # and silently reverts the operator's Stop to RUNNING. Keyed only on instances that
        # HAVE a pid, so one whose pid was unknown at snapshot (the mid-`_popen` window)
        # yields None below and is skipped rather than clobbered.
        gen = {
            iid: (
                i.bridge_pid,
                i.bridge_proc_start,
                i.status,
                i.intentional_stop,
                i.bridge_start_ticks,
                i.bridge_boot_id,
            )
            for iid, i in self._registry._instances.items()
            if i.bridge_pid is not None
        }
        # Read in the SAME pass as `gen` rather than re-indexing the registry: correct
        # either way today (no await separates them), but this removes the implicit invariant
        # instead of trusting a future editor not to insert one.
        ours = {iid: (g[0], g[1], g[4], g[5]) for iid, g in gen.items()}
        live = await asyncio.to_thread(
            lambda: {
                key: {
                    iid: procutil.is_live_bridge(pid, start, start_ticks=ticks, boot_id=boot)
                    for iid, (pid, start, ticks, boot) in pairs.items()
                }
                for key, pairs in (("row", row_pairs), ("ours", ours))
            }
        )
        discovered = self._discovered()
        for iid, saved in rows.items():
            inst = self._registry._instances.get(iid)
            if inst is None:
                continue
            pair = row_pairs.get(iid)
            # Ticks are NOT in this generation compare (#1399): the row and the live object
            # can legitimately disagree on them — a row written by a pre-#1399 build has
            # none while the object we adopted it into does — and that is not a different
            # process generation. pid + proc_start remain the identity.
            if pair is not None and pair[:2] == (inst.bridge_pid, inst.bridge_proc_start):
                # Same process generation: the row is describing the bridge we hold, so its
                # recorded intent is ours too. `stop()` writes the intent BEFORE the process
                # exits, so without this an adopted bridge stopped from the CLI reconciles to
                # CRASHED ("exited unexpectedly") for a stop the operator asked for.
                if saved.get("intentional_stop") and not inst.intentional_stop:
                    inst.intentional_stop = True
            elif pair is not None and live["row"].get(iid) and not live["ours"].get(iid):
                await self._resync_pids_from_row(iid, inst, saved, pair, gen.get(iid), discovered)
        await self._reattach_rows_with_pids(discovered, live_only=True, liveness=live["row"])

    async def _resync_pids_from_row(
        self,
        iid: str,
        inst: RemoteControlInstance,
        saved: dict,
        pair: tuple[int, float | None, int | None, str | None],
        ours_generation: (
            tuple[int | None, float | None, InstanceStatus, bool, int | None, str | None] | None
        ),
        discovered: dict[str, Project],
    ) -> None:
        """Take over the pids another process resumed this instance with (#1088).

        The row's process is live while the one we recorded is dead, so another process
        resumed this instance. Adopt its pids, or our next ``_persist`` overlays the stale
        ones back over the row and the bridge reads dead to every reader — the #1088
        symptom, via the very columns added to fix it.

        **Under the project's spawn lock**, because this is the only ``bridge_pid`` mutator
        on the lock-free poll path and it races the two operations that own that field:

        * ``resume()``/``_spawn_locked`` registers a new instance under the same id and
          publishes its pid *after* a ``to_thread(self._popen)``. Landing inside that window
          overwrites a live, freshly-spawned pid with the row's — orphaning a bridge nothing
          can stop, which runs until reboot.
        * ``stop()`` reads the pid, signals in a thread, then sets STOPPED without clearing
          ``bridge_pid``. Interleaving leaves another process's LIVE bridge persisted as
          ``bridge_pid=<live>`` + ``intentional_stop=True``, and ``_reconcile_status`` only
          ever demotes — so it reads STOPPED to every reader, permanently.

        A held lock means one of those is in flight: skip the row and retry next tick, which
        is always safe (the row is not going anywhere). But the lock only covers a
        *concurrent* mutation — one that COMPLETED while we were in the liveness probe holds
        no lock by the time we look. That is what ``ours_generation`` is for: pid,
        proc-start, status, intent AND start-ticks are compared against the live object, so a
        finished ``stop()`` (which changes the middle two and deliberately leaves
        ``bridge_pid`` alone) is detected and left alone. Not to be confused with the ROW
        compare in :meth:`_adopt_rows_from_store`, which deliberately excludes ticks — a
        pre-#1399 row and the object it was adopted into can differ on them without being a
        different process generation. An instance whose pid was unknown at snapshot
        time (``ours_generation is None`` — precisely the mid-``_popen`` window) is skipped
        rather than overwritten.

        Connect facts are recomputed rather than carried over: the previous generation's
        ``url``/``environment_id`` point at a dead environment, and publishing them as
        RUNNING offers the operator a link that can never work. Same readiness rule as
        :meth:`_reattach_rows_with_pids` — no connect evidence means STARTING, not RUNNING.
        """
        proj = discovered.get(inst.project)
        if proj is None:
            return  # project vanished from discovery; nothing to attribute it to
        # boot_id is written back through `pair` below, not read here: the keeper/connect
        # helpers correlate a sidecar/pointer that carries no boot id (see
        # `procutil._DRIFT_EPOCH_TOLERANCE`), so only the identity re-check consumes it.
        pid, proc_start, start_ticks, _boot_id = pair
        # Resolved BEFORE the lock. All three are pure filesystem reads keyed entirely on
        # snapshot values (project, pid, proc_start) — none reads live mutable state — so
        # holding the project's spawn lock across them would stall every spawn/resume/stop
        # for that project behind up to three globs of the bridge-log dir, per resynced
        # row, per tick, for no correctness gain. What must be inside the lock is the
        # re-check plus the mutation, and that block contains no ``await`` at all.
        keeper_pid, keeper_proc_start, keeper_start_ticks = (
            await asyncio.to_thread(
                self._recover_keeper_identity,
                inst.project,
                pid,
                proc_start,
                bridge_start_ticks=start_ticks,
            )
            if inst.resume_mode == "pty"
            else (None, None, None)
        )
        connect = await asyncio.to_thread(
            self._connect_facts_for,
            proj,
            inst.resume_mode,
            pid,
            proc_start,
            start_ticks=start_ticks,
        )
        log_path = await asyncio.to_thread(self._latest_debug_log_for, inst.project)

        lock = self._registry._spawn_lock_for(inst.project)
        if lock.locked():
            return  # a spawn/resume/stop owns this project right now; next tick is fine
        async with lock:
            # `locked()` is a cheap HINT, not a try-acquire: `release()` clears the flag
            # before the first waiter resumes, so this can read False with a queue behind
            # it and the acquire below then blocks. Correctness rests entirely on the
            # generation re-check that follows, never on the hint.
            current = self._registry._instances.get(iid)
            if current is None or current is not inst or iid not in self._registry._persisted:
                return  # replaced by a lock holder, or forgotten — don't resurrect either
            if (
                current.bridge_pid,
                current.bridge_proc_start,
                current.status,
                current.intentional_stop,
                current.bridge_start_ticks,
                current.bridge_boot_id,
            ) != ours_generation:
                # Status and intent are in the tuple deliberately — see the docstring: a
                # completed `stop()` leaves `bridge_pid` set, so a pid-only compare would
                # revert the operator's Stop to RUNNING, permanently.
                return
            # All four bridge fields together, and the keeper trio together (the same rule the
            # keeper block below states): a `bridge_start_ticks`/`bridge_boot_id`, or a keeper
            # field, left over from the generation we just replaced would be compared against
            # the NEW pid's values and never match, reporting the bridge we just adopted as dead
            # on the very next poll (#1399 / #1401 / #1402).
            (
                current.bridge_pid,
                current.bridge_proc_start,
                current.bridge_start_ticks,
                current.bridge_boot_id,
            ) = pair
            # Every field of the keeper trio, together (#1178 / #1402): leaving the old
            # `keeper_proc_start` or `keeper_start_ticks` beside a NEW `keeper_pid` would
            # make the live keeper of the generation we just adopted read as gone.
            current.keeper_pid = keeper_pid
            current.keeper_proc_start = keeper_proc_start
            current.keeper_start_ticks = keeper_start_ticks
            current.intentional_stop = bool(saved.get("intentional_stop"))
            current.url = connect.get("url")
            current.environment_id = connect.get("environment_id")
            current.starter_session_id = connect.get("starter_session_id")
            # Recovered from the same ready sidecar as the connect facts (#1438), so a
            # screen-fault advisory survives a pid-adoption resync too and does not
            # appear-then-vanish across the tick that re-owns the bridge. Absent from the
            # dict clears a stale note, matching `_apply_pty_info`'s unconditional assign.
            current.notice = connect.get("notice")
            current.bridge_debug_log_path = log_path
            current.bridge_raw_log_path = (
                self._raw_log_path_for(log_path) if log_path is not None else None
            )
            current.status = InstanceStatus.RUNNING if connect else InstanceStatus.STARTING

    @staticmethod
    def _judge_row(
        pid: int, proc_start: float | None, ticks: int | None, boot_id: str | None
    ) -> tuple[int | None, bool]:
        """Judge a row's liveness; stamp a tick-less live row's ticks while its epoch matches.

        Returns ``(ticks, alive)`` from one thread hop. ``boot_id`` (the row's recorded
        ``bridge_boot_id``) rejects a row from an earlier boot on identity, which is the case
        cold startup most needs: a persisted pid recycled across a reboot (#1401). The stamp
        does not consume the verdict's evidence: :func:`_ticks_on_exact_match` re-derives its
        own exact match from one fresh read, so it is equally sound from the poll loop's
        separate hop. ``alive`` merely says a stamp is worth attempting.
        """
        alive = procutil.is_live_bridge(pid, proc_start, start_ticks=ticks, boot_id=boot_id)
        if alive and ticks is None:
            ticks = _ticks_on_exact_match(pid, proc_start)
        return ticks, alive

    async def _reattach_rows_with_pids(
        self,
        discovered: dict[str, Project],
        *,
        live_only: bool = False,
        liveness: dict[str, bool] | None = None,
    ) -> tuple[set[str], set[str]]:
        """Reattach every persisted row that carries its own pids; return their projects.

        This is the instance-keyed half of :meth:`rediscover` (#1088). Each row is judged on
        **its own** ``bridge_pid``/``bridge_proc_start`` — live becomes RUNNING, dead becomes
        a STOPPED resumable card — so a project running a standard bridge plus N interactive
        sessions materializes all of them, each under the id it was spawned with.

        The pointer walk this replaces could only ever resolve ONE instance per project, and
        picked it by first-match rather than by any correlation with the live process, which
        is what made ``clauster status`` / ``clauster mcp`` show a single stale row and
        ``clauster stop <id>`` fail for every id but the oldest.

        Liveness is the (pid, proc_start) PAIR — never a bare pid, which is reusable — so a
        recycled pid cannot resurrect an unrelated process as somebody's bridge. A pty row's
        keeper is re-derived from the keeper sidecar (:meth:`_recover_keeper_pid`, correlated
        to this bridge's pid + proc-start) and never taken from the row, whose keeper pid may
        be stale — reusing it would let ``stop()`` reap a stranger's process tree.

        Returns ``(claimed, stopped)``. ``claimed`` is the projects that had at least one
        LIVE row, so the legacy pointer walk can skip them and the two paths never both
        claim the same bridge. ``stopped`` is the projects for which this pass inserted a
        STOPPED card from a dead row — the walk must NOT treat those as "already resolved"
        (they are exactly the projects whose live externally-started bridge it still has to
        discover), and must not stack its project-level stopped-card fallback on top of the
        per-row cards this pass already produced.
        """
        claimed: set[str] = set()
        stopped_projects: set[str] = set()
        for iid, saved in list(self._registry._persisted.items()):
            name = saved.get("project_name")
            proj = discovered.get(name) if isinstance(name, str) else None
            pid = _row_int(saved.get("bridge_pid"))
            if proj is None or pid is None:
                continue  # unknown project, or a pre-#1088 row -> legacy pointer walk
            if iid in self._registry._instances:
                continue  # already tracked in this process (a spawn we own)
            proc_start = _row_float(saved.get("bridge_proc_start"))
            start_ticks = _row_int(saved.get("bridge_start_ticks"))
            boot_id = _row_str(saved.get("bridge_boot_id"))
            # Startup judges each row itself and may stamp a pre-#1399 row's ticks on the
            # spot (`_judge_row`); the instance built below must then carry the SAME ticks
            # the verdict used, or its very next poll re-judges the bridge on the epoch
            # alone. A poll-time caller hands in verdicts it already has, and the poll loop
            # stamps a tick-less adopted instance itself on its next exact match.
            if liveness is not None and iid in liveness:
                alive = liveness[iid]
            else:
                start_ticks, alive = await asyncio.to_thread(
                    self._judge_row, pid, proc_start, start_ticks, boot_id
                )
            if not alive:
                if live_only:
                    continue  # poll-time adoption never resurrects dead cards
                stopped = self._stopped_from_row(iid, saved)
                self._registry._instances[stopped.instance_id] = stopped
                # Record it, but do NOT claim the project: a dead row proves nothing about
                # a live externally-started bridge, which only the pointer walk can find.
                # The walk's presence guard must therefore ignore this card (#1088 MF-1).
                stopped_projects.add(proj.name)
                continue
            # Claim ONLY once the row proves live. `stop()` leaves `bridge_pid` on the
            # instance, so every user-stopped row keeps a stale pid — claiming on the pid
            # alone would mark the project resolved forever and permanently suppress the
            # pointer walk, which is still the only way a live externally-started bridge is
            # discovered at startup.
            claimed.add(proj.name)
            spawn_mode, permission_mode, resume_mode = self._saved_modes(saved)
            # Resolve the keeper from the SIDECAR, correlated to this bridge's pid +
            # proc-start, rather than trusting the pid the row carries. `is_keeper_process`
            # alone only proves "some keeper", not "OUR keeper" — and `stop()` hands
            # `keeper_pid` to `_cleanup_keeper`, which force-kills the whole tree. A recycled
            # pid landing on another instance's keeper would reap a stranger's processes.
            keeper_pid, keeper_proc_start, keeper_start_ticks = (
                await asyncio.to_thread(
                    self._recover_keeper_identity,
                    proj.name,
                    pid,
                    proc_start,
                    bridge_start_ticks=start_ticks,
                )
                if resume_mode == "pty"
                else (None, None, None)
            )
            log_path = await asyncio.to_thread(self._latest_debug_log_for, proj.name)
            # The row carries identity and liveness but NOT the connect facts — those live in
            # the Anthropic pointer (standard) or the keeper sidecar (pty). Without this the
            # dashboard sits on "Preparing connect link…" forever for every reattached or
            # adopted bridge, which is the whole point of having the bridge.
            connect = await asyncio.to_thread(
                self._connect_facts_for,
                proj,
                resume_mode,
                pid,
                proc_start,
                start_ticks=start_ticks,
            )
            # A post-#1401 spawn's row carries its boot id; a pre-#1401 row (or one whose bridge
            # was pointer/sidecar-reattached before) carries none. This build is reached only
            # for a row judged live in THIS boot, so stamp the current boot for a boot-id-less
            # one — otherwise the reattached row persists boot-id-less and loses the cross-boot
            # defense on the next restart, a row this pass itself rewrites (#1401). The recorded
            # value already went to `_judge_row` above; this only affects what we persist. Read
            # off-thread BEFORE the recheck below, so the recheck stays the last await-free
            # statement before the build — its whole job is to close the lock-free race this
            # extra await would otherwise reopen.
            live_boot_id = (
                boot_id if boot_id is not None else await asyncio.to_thread(procutil.proc_boot_id)
            )
            # Re-check across the awaits above. This runs lock-free on the poll loop, so a
            # lock-holding adopt()/spawn() can have registered this id — overwriting it would
            # discard the object the HTTP caller was handed — and a concurrent forget() can
            # have dropped the row, which we must not resurrect.
            if iid in self._registry._instances or iid not in self._registry._persisted:
                continue
            # Liveness is not usability (see `_reconcile_status`): a bridge whose process is
            # up but which has not registered an environment / reached a ready sidecar is
            # STARTING, not RUNNING. `connect` IS that evidence — it only resolves from a
            # live pointer or a ready sidecar — so promoting without it would report
            # uncontrollable bridges as RUNNING, the exact thing that invariant forbids.
            status = InstanceStatus.RUNNING if connect else InstanceStatus.STARTING
            self._registry._instances[iid] = RemoteControlInstance(
                instance_id=iid,
                project=proj.name,
                label=saved.get("label") or proj.name,
                spawn_mode=spawn_mode,
                permission_mode=permission_mode,
                resume_mode=resume_mode,
                sandbox_mode=self._saved_sandbox(saved),
                worktree_name=self._recovered_worktree_name(saved.get("worktree_name")),
                # Seeded from the row, not hardcoded False: `stop()` records the intent and
                # THEN signals, so a poll landing in that grace window adopts a still-live
                # pid whose stop was already requested. Hardcoding False reports CRASHED.
                intentional_stop=bool(saved.get("intentional_stop")),
                status=status,
                bridge_pid=pid,
                bridge_proc_start=proc_start,
                # Carried from the row alongside its epoch, never re-measured HERE: a fresh
                # `proc_start_ticks` read would agree with the live process by construction
                # and so could authenticate a recycled pid the row's own pair rejects. The
                # one sanctioned re-measure is `_judge_row` above, which runs only after the
                # row's own pair ACCEPTED the process and accepts the read only inside the
                # exact 0.05s bound (`_ticks_on_exact_match`), so it cannot admit more.
                bridge_start_ticks=start_ticks,
                # The boot the bridge is running in (#1401): the row's own recorded id when it
                # has one, else the current boot (`live_boot_id` above) — this build is reached
                # only for a row judged live THIS boot, so the reattached row keeps the
                # cross-boot defense across the next restart instead of persisting boot-id-less.
                bridge_boot_id=live_boot_id,
                keeper_pid=keeper_pid,
                keeper_proc_start=keeper_proc_start,
                keeper_start_ticks=keeper_start_ticks,
                bridge_debug_log_path=log_path,
                bridge_raw_log_path=(
                    self._raw_log_path_for(log_path) if log_path is not None else None
                ),
                **connect,
            )
        return claimed, stopped_projects

    def _connect_facts_for(
        self,
        proj: Project,
        resume_mode: ResumeMode,
        pid: int,
        proc_start: float | None,
        *,
        start_ticks: int | None,  # keyword-required — see _recover_keeper_pid (#1399)
    ) -> dict:
        """Recover a reattached bridge's connect URL / env id / starter session / notice.

        The persisted row carries identity and liveness, deliberately — but not the facts a
        human needs to actually *use* the bridge (#1088). Those are written by the bridge
        itself:

        * **standard** — the Anthropic ``bridge-pointer.json`` (environment id + session id);
        * **pty** — the keeper sidecar, which records the connect URL directly, plus the
          advisory ``notice`` a screen-fault keeper writes instead of a link (#1438).

        Both are matched against THIS instance's pid before being believed, so a pointer left
        by a different bridge at the same project path can't lend its environment to somebody
        else's row. Returns ``{}`` when nothing matches — the dashboard then shows the
        "preparing connect link" state, which is honest, rather than a wrong link. Every
        caller gates promotion on the dict being non-empty. A correlated *ready* pty sidecar
        always carries a ``notice`` key (its value or ``None``), so its dict is non-empty even
        with no link and no note: a ready sidecar IS a running session (#1452, see
        :func:`_sidecar_notice`), and each caller copies ``notice`` onto the card too.
        """
        if resume_mode == "pty":
            for sidecar in sorted(self._keeper_sidecars_for(proj.name), reverse=True):
                info = self._read_sidecar(sidecar)
                if not info or info.get("bridge_pid") != pid:
                    continue
                # Correlate on proc-start too, and require a READY state — matching
                # `_reattach_pty_from_sidecar` (`_recover_keeper_pid` matches on the pid pair
                # only, deliberately: it wants the keeper, not usability). Several interactive
                # sessions share one project's log dir, so a stale sidecar left by a recycled
                # pid would otherwise hand its connect URL to a different session.
                if info.get("state") != "ready":
                    continue
                sidecar_start = info.get("bridge_proc_start")
                if (
                    proc_start is not None
                    and isinstance(sidecar_start, (int, float))
                    and not isinstance(sidecar_start, bool)
                    and abs(float(sidecar_start) - proc_start) > _PROC_START_TOLERANCE
                ):
                    continue
                facts = {}
                if info.get("connect_url"):
                    facts["url"] = info["connect_url"]
                if info.get("session_id"):
                    facts["starter_session_id"] = info["session_id"]
                # Lift the advisory note here too (#1438), the same field `_apply_pty_info`
                # reads on the spawn path — and UNCONDITIONALLY (#1452), so this key is always
                # present once the `state == "ready"` gate above is passed. That readiness is
                # the evidence the spawn path promotes on, so the dict a correlated ready
                # sidecar returns is now non-empty even with no url, no session id and no note:
                # a screen-fault sidecar (`connect_url` nulled, a note written) AND a bare
                # ready sidecar (a URL scrape that missed, nothing to report) both read RUNNING
                # here, matching `_apply_pty_info`. A conditional note left the no-note case
                # returning `{}` — read as "no evidence" — so a row rebuilt through this leg
                # after a restart stayed STARTING for the life of the process (the gap #1390
                # half-closed and #1452 named). The note rides a dict already gated on
                # readiness, so it promotes on the ready state, never on itself, and `None`
                # here clears a stale note exactly as the spawn path's assign does.
                facts["notice"] = _sidecar_notice(info)
                return facts
            return {}
        ptr = pointers.pointer_for_project(proj.path)
        if ptr is None or ptr.pid != pid:
            return {}
        # Same pid AND same start time, or a recycled pid could hand over its environment.
        #
        # ⚠️ The tolerance arm below is the #1399 comparison: our `proc_start` is FROZEN
        # (psutil's create_time at spawn, persisted) while `_expected_epoch(ptr.proc_start)`
        # is re-derived with TODAY's btime — so after a clock correction they differ by the
        # drift and the 0.05s bound rejects a bridge that never restarted. The consequence is
        # not a wrong link but NO link: the row gets no connect facts, stays STARTING, and
        # `_promote_ready_unwatched` (which calls this) can never promote it, so the card is
        # stuck on "preparing connect link…" for as long as the drift lasts.
        #
        # An earlier version of this comment claimed the two derivations "agree on Linux
        # today only because the rounding happens to coincide". They do not agree; #1399 is
        # exactly them disagreeing. Both sides carry raw field-22 ticks, so compare THOSE
        # when both are available — an exact, drift-free match — and keep the epoch arm only
        # as the fallback for a row or pointer that has none.
        #
        # An UNKNOWN expected value (unparseable / non-Linux `procStart`) skips the check
        # rather than failing it, matching `is_live_process` and `_recover_keeper_pid` —
        # treating unknown as mismatch would strip the connect link on any platform where the
        # jiffies form isn't comparable, reintroducing the bug this recovers from.
        pointer_ticks = _pointer_start_ticks(ptr.proc_start)
        expected = procutil._expected_epoch(ptr.proc_start)
        epoch_gap = (
            abs(expected - proc_start) if expected is not None and proc_start is not None else None
        )
        if start_ticks is not None and pointer_ticks is not None:
            # Exact on the ticks, coarse on the epoch (`procutil._DRIFT_EPOCH_TOLERANCE`), the same
            # guard `_recover_keeper_pid` keeps. Ticks restart at zero each boot, so a pointer
            # that survived a reboot can collide on pid AND ticks, and the epoch rejects that
            # for free. `is_live_process` rejects it on a recorded boot id now (#1401), but an
            # Anthropic pointer carries none, so this path keeps the coarse epoch guard. Every
            # caller gates on `is_live_bridge` first, which already rejects a previous-boot pid
            # on identity; the helper keeps its own guard so a future caller without that gate
            # cannot inherit a weaker rule.
            if pointer_ticks != start_ticks or (
                epoch_gap is not None and epoch_gap > procutil._DRIFT_EPOCH_TOLERANCE
            ):
                return {}
        elif epoch_gap is not None and epoch_gap > procutil._EXACT_PROC_START_TOLERANCE:
            return {}
        # No presence guards: `BridgePointer` declares both as required `str`, so a parsed
        # pointer always carries them — a guard here would be unreachable, not defensive.
        return {
            "environment_id": ptr.environment_id,
            "url": f"https://claude.ai/code?environment={ptr.environment_id}",
            "starter_session_id": ptr.session_id,
        }

    def _stopped_from_row(self, instance_id: str, saved: dict) -> RemoteControlInstance:
        """Rebuild ONE persisted row as a STOPPED, resumable card (#1088).

        The per-row counterpart of :meth:`_stopped_from_persisted`, which resolves by project
        and so can only ever rebuild one row of the several a project may hold (#778).
        """
        spawn_mode, permission_mode, resume_mode = self._saved_modes(saved)
        return RemoteControlInstance(
            instance_id=instance_id,
            project=str(saved.get("project_name")),
            label=saved.get("label") or str(saved.get("project_name")),
            spawn_mode=spawn_mode,
            permission_mode=permission_mode,
            resume_mode=resume_mode,
            sandbox_mode=self._saved_sandbox(saved),
            worktree_name=self._recovered_worktree_name(saved.get("worktree_name")),
            intentional_stop=bool(saved.get("intentional_stop", False)),
            status=InstanceStatus.STOPPED,
            # The process is gone: drop the stale pids rather than carry them onto a dead
            # card, where a later reuse of that pid could read as this bridge coming back.
            bridge_pid=None,
            bridge_proc_start=None,
            # Named rather than left to the model default: this is a deliberate ZEROING of
            # the identity, so every field of both the bridge and keeper identity belongs here
            # together. Relying on the default would let a future default change silently carry
            # a stale tick count or boot id onto a dead card, where a pid reuse could read as
            # the bridge coming back.
            bridge_start_ticks=None,
            bridge_boot_id=None,
            keeper_pid=None,
            keeper_proc_start=None,
            keeper_start_ticks=None,
        )

    async def rediscover(self, *, persist: bool = True) -> None:
        """Re-detect bridges after a restart: reattach live ones, resurrect dead ones.

        A bridge found *alive* is reattached as RUNNING once its connect facts resolve, and
        as STARTING while they do not — liveness is not usability, see
        :meth:`_reattach_rows_with_pids`. A discovered project whose
        bridge is gone but which has a persisted record (its process died while
        Clauster was down — e.g. a host reboot) is resurrected as a STOPPED,
        resumable card instead of being dropped; one with no persisted record is
        left absent (nothing to resume).

        Three passes. First :meth:`_reattach_rows_with_pids` walks the persisted rows **by
        instance** (#1088), judging each on its own pids — that is what lets a project
        surface its standard bridge AND its N interactive sessions instead of a single
        first-match row. Then the original **project-keyed** pointer walk runs for whatever
        it did not claim: rows written before the pids existed (so an upgrade never declares
        a surviving bridge dead) and live bridges with no persisted row at all, which is
        still the only way to discover an externally-started one at startup.

        Finally a per-ROW pass cards whatever pid-less rows remain (#1115). Without it the
        project-keyed walk was the only thing that ever saw them, so a project surfaced ONE
        of its pid-less rows and the rest stayed invisible with their rows still in the DB.
        That pass is gated on a live-keeper sweep for pty rows — see the comment there.

        ``persist=False`` reattaches into the in-memory registry only and skips the
        trailing state write — the read-only mode the headless CLI (#775) uses so a
        ``clauster status`` never clobbers the running service's shared ``state.json``.
        """
        # Fresh merge base (#949): the reattach/resurrection reads below consume
        # ``_persisted``, and a headless runner calls this as its hydrate step — its
        # construction-time snapshot may already lag the live service's store.
        await self._registry._refresh_persisted()
        discovered = self._discovered()
        row_claimed, row_stopped = await self._reattach_rows_with_pids(discovered)
        # Projects whose live keeper the sidecar leg re-managed under a FRESH id because no
        # row could be correlated to it (#1108). Consumed by the pid-less pass below.
        uncorrelated_keepers: set[str] = set()
        for proj in discovered.values():
            if proj.name in row_claimed:
                continue  # resolved per-instance above; don't let the walk re-claim it
            # Liveness-exact on purpose. `get_instance_for_project` matches in ANY status
            # (by design — #778, see its docstring), so the STOPPED cards the row pass just
            # inserted would suppress the walk for precisely the projects that still need
            # it: a dead row plus a live externally-started bridge, which only the pointer
            # walk (or, for a flag-form pty, the keeper sidecar below) can discover. That
            # regressed both legs against `main` and leaked live detached keepers (#1088).
            if any(
                inst.project == proj.name
                and inst.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING)
                for inst in self._registry._instances.values()
            ):
                continue
            ptr = await asyncio.to_thread(pointers.pointer_for_project, proj.path)
            if ptr is None or not await asyncio.to_thread(pointers.is_live, ptr):
                # A pty (flag-form) bridge writes no Anthropic pointer, yet its
                # detached keeper outlives the restart. Reattach it from the keeper
                # sidecar so a live keeper is re-managed (Stop/observe restored)
                # rather than leaking behind a STOPPED card; fall through to the
                # STOPPED resurrection when no live keeper remains.
                # ONE lookup, pinned to pty (only a pty bridge has a sidecar, and a standard
                # row's fields would describe a different bridge shape): modes/label from ANY
                # pty row of the project. Resolving it from an `unclaimed_only=True` call
                # instead disabled the leg outright — the row pass has already carded every
                # row that carries a pid, so `saved` came back empty and the leg bailed on
                # its first line, for precisely the dead-row-plus-live-keeper case MF-1
                # exists to fix. No pty row -> empty `saved` -> the leg bails, correctly.
                #
                # No id is resolved here any more (#1108). The leg used to be handed the
                # first UNCLAIMED pty row's id, picked before the glob had found *whose*
                # keeper this is — see `_reattach_pty_from_sidecar` for why no row reachable
                # at this point can be correlated to it. The reattach mints a fresh id: one
                # extra card for the project, the same trade the pointer leg below makes for
                # an ambiguous project, instead of overwriting a resumable record (#1088 SF-4).
                modes_hit = self._persisted_for_project(proj.name, resume_mode="pty")
                persisted_saved = modes_hit[1] if modes_hit is not None else {}
                reattached = await asyncio.to_thread(
                    self._reattach_pty_from_sidecar,
                    proj.name,
                    persisted_saved,
                )
                if reattached is not None:
                    self._registry._instances[reattached.instance_id] = reattached
                    # The keeper is managed again, but under an id of its own — so this
                    # project's pid-less pty rows are still UNRESOLVED: one of them may be
                    # the session this keeper is holding. The pid-less pass below would
                    # otherwise card them STOPPED (its sweep now sees the keeper as
                    # "accounted for", held by the card just inserted) and offer a Resume
                    # that spawns a SECOND keeper on the same `--continue` conversation.
                    # Block them here instead: a hidden card is recoverable on the next
                    # start, a duplicate bridge is not (#1108).
                    uncorrelated_keepers.add(proj.name)
                elif proj.name not in row_stopped and (
                    (stopped := self._stopped_from_persisted(proj.name)) is not None
                ):
                    # Only when the row pass didn't already leave one. This fallback is the
                    # last resort for a project the row pass could NOT cover, not a second
                    # source of cards for one it did. It resolves by PROJECT — first match
                    # over `_persisted` — so on a project with several rows it can land on
                    # a different row than the one already carded and leave two cards up
                    # where the row pass had deliberately produced one per live-or-dead row.
                    # (It always takes an id that already keys a row, so it cannot mint an
                    # id for a session that does not exist; the risk is duplication, not
                    # invention.) Whether a pre-#1088 pid-less row deserves a card of its
                    # own is a real question, deliberately left alone here — this guard
                    # keeps the walk from answering it as a side effect.
                    self._registry._instances[stopped.instance_id] = stopped
                continue
            # Overlay the few fields the pointer-walk can't recover; a bridge
            # found alive is by definition NOT intentionally stopped.
            # `unclaimed_only` because this attaches the LIVE bridge under the id it is
            # handed, so a claimed one would overwrite a different session's record
            # irrecoverably (#1088 SF-4). Unlike the pty leg above, this one still ADOPTS
            # that unclaimed id rather than minting a fresh one — the same
            # picked-before-we-know-whose class #1108 describes, deliberately left alone
            # here because that issue scopes to the keeper-sidecar leg and changing the
            # pointer leg would drop the row association every standard reattach relies on.
            #
            # Deliberately NOT also pinned to `resume_mode="standard"`, unlike the pty leg.
            # A pointer means the live bridge is standard, so the mirror mismatch is real —
            # but a mode-less pre-#1088 row coerces to the host's CONFIGURED default, so on
            # a `launch_mode: pty` host that filter would reject a standard bridge's own
            # legacy row and mint a fresh id, losing the association it was meant to keep.
            # The pty leg has no such exposure: a keeper sidecar only exists for a bridge
            # Clauster spawned in pty mode, and those rows always record their mode.
            persisted_hit = self._persisted_for_project(proj.name, unclaimed_only=True)
            saved = persisted_hit[1] if persisted_hit is not None else {}
            persisted_iid = persisted_hit[0] if persisted_hit is not None else None
            spawn_mode, permission_mode, resume_mode = self._saved_modes(saved)
            # _expected_epoch (not bare int()) so an unparseable procStart degrades to
            # None (cmdline-only liveness) instead of raising ValueError out of startup.
            # Mirrors is_live_bridge, so the liveness check and this construction can't
            # disagree. Computed once: reused for keeper matching AND the instance.
            bridge_proc_start = procutil._expected_epoch(ptr.proc_start)
            bridge_start_ticks = _pointer_start_ticks(ptr.proc_start)
            # A "pty" bridge is held by a detached keeper that outlives a Clauster
            # restart; recover its pid from the sidecar so stop()/poll_once can reap
            # it — otherwise a rediscovered pty bridge would leak its keeper. The log
            # path is timestamped (not derivable), so match the sidecar by bridge pid
            # + proc-start (PID-reuse defense — see _recover_keeper_pid). The pid, its
            # create-time and its boot-relative ticks travel as one identity (#1178 /
            # #1402): recovering only the pid here would persist a row whose forget gate
            # degrades to cmdline-only, and recovering it without the ticks would leave that
            # gate on the epoch a clock correction moves.
            keeper_pid, keeper_proc_start, keeper_start_ticks = (
                await asyncio.to_thread(
                    self._recover_keeper_identity,
                    proj.name,
                    ptr.pid,
                    bridge_proc_start,
                    bridge_start_ticks=bridge_start_ticks,
                )
                if resume_mode == "pty"
                else (None, None, None)
            )
            # Re-bind the live tail to the log this survivor was already writing before the
            # restart, so `/ws/bridge-log` resolves a real path instead of 1008-ing (#584).
            log_path = await asyncio.to_thread(self._latest_debug_log_for, proj.name)
            # The pointer's bridge is live NOW (this survivor loop only runs for a live
            # pointer), so the current boot is its boot (#1401). Read off-thread.
            boot_id = await asyncio.to_thread(procutil.proc_boot_id)
            instance = self._instance_from_pointer(
                proj.name,
                ptr,
                label=saved.get("label") or proj.name,
                spawn_mode=spawn_mode,
                permission_mode=permission_mode,
                resume_mode=resume_mode,
                bridge_proc_start=bridge_proc_start,
                bridge_start_ticks=bridge_start_ticks,
                bridge_boot_id=boot_id,
                keeper_pid=keeper_pid,
                keeper_proc_start=keeper_proc_start,
                keeper_start_ticks=keeper_start_ticks,
                bridge_debug_log_path=log_path,
                bridge_raw_log_path=(
                    self._raw_log_path_for(log_path) if log_path is not None else None
                ),
                instance_id=persisted_iid,
                worktree_name=self._recovered_worktree_name(saved.get("worktree_name")),
            )
            self._registry._instances[instance.instance_id] = instance
        # Third pass (#1115): rows carrying NO pid at all. The two passes above resolve at
        # most ONE such row per project between them — the row pass skips them (no pair to
        # judge) and the pointer walk is project-keyed — so every other pid-less row of a
        # project stayed invisible while its record sat in the DB. On the dogfood that was
        # 16 of 17 rows, because the pre-fix ratchet had already erased their pids; no
        # backfill can restore those (the processes are long gone), so carding them here is
        # the only way they come back.
        #
        # The "resumable card for a bridge that is actually alive" trap is REAL here, and is
        # NOT ruled out by the walk above: that loop `continue`s for a project in
        # `row_claimed` or holding any STARTING/RUNNING instance *before* it reads the
        # pointer or consults the keeper sidecar. So on those projects nothing has looked
        # for a pid-less row's process at all. NEITHER mode is exempt. `_reattach_rows_with_pids`
        # claims a project when ANY row of it proves live, before it even reads that row's
        # mode — so one live pty row makes the walk skip the project, and a pid-less STANDARD
        # row there can still own the live pointer bridge nobody looked for. Carding either
        # live shape STOPPED offers a Resume that spawns a duplicate: a second keeper on the
        # same `--continue` conversation, or a second bridge that overwrites the pointer of
        # the running one and orphans it.
        #
        # So sweep BOTH discovery mechanisms for the projects that still have uncarded
        # pid-less rows — keeper sidecars for pty rows, the Anthropic pointer for the rest —
        # and when one names a live process no tracked instance holds, leave that project's
        # rows of that mode alone rather than guess which row owns it (#1108). Same
        # discipline as the phantom-prune's ">1 candidate -> prune none": a hidden card is
        # recoverable, a duplicate bridge is not.
        #
        # Snapshotted on the event loop and swept in ONE thread hop, mirroring the poll
        # loop's `gen`/`ours` discipline: the sweep does blocking IO, so it must not iterate
        # `_instances` from a worker thread. Held pids are read from LIVE instances only —
        # `stop()` leaves `keeper_pid`/`bridge_pid` on a dead card, and letting a dead card's
        # stale pid mark a live process "accounted for" would fail OPEN.
        held_keepers = {
            i.keeper_pid
            for i in self._registry._instances.values()
            if i.keeper_pid is not None
            and i.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING)
        }
        held_pids = {
            i.bridge_pid
            for i in self._registry._instances.values()
            if i.bridge_pid is not None
            and i.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING)
        }
        pending: dict[str, set[str]] = {}
        for iid, saved in self._registry._persisted.items():
            pname = saved.get("project_name")
            if (
                iid in self._registry._instances
                or _row_int(saved.get("bridge_pid")) is not None
                or not isinstance(pname, str)
                or pname not in discovered
            ):
                continue
            pending.setdefault(pname, set()).update(self._sweep_modes(saved))
        blocked: set[tuple[str, str]] = (
            await asyncio.to_thread(
                self._modes_with_an_unclaimed_live_bridge,
                {n: (frozenset(modes), discovered[n].path) for n, modes in pending.items()},
                held_keepers,
                held_pids,
            )
            if pending
            else set()
        )
        # Unioned, never derived from the sweep: `_has_unclaimed_live_keeper` asks whether a
        # live keeper is UNACCOUNTED FOR, and the walk above has just accounted for these —
        # under a fresh id that proves nothing about which pid-less row owns them (#1108).
        blocked |= {(name, "pty") for name in uncorrelated_keepers}
        # And one restart LATER (the review catch on #1108): the fresh-id row now reattaches
        # by its persisted pids, so the keeper is accounted for, `uncorrelated_keepers` is
        # empty — and the original pid-less row would be carded STOPPED, offering a Resume
        # that spawns a second keeper on the same conversation. A pty resume is `--continue`,
        # which grabs the project's LATEST conversation, so while ANY managed pty instance is
        # live in a project, resuming a pid-less pty row can only duplicate or steal that
        # conversation. Hidden-but-recoverable beats a duplicate bridge, again.
        blocked |= {
            (i.project, "pty")
            for i in self._registry._instances.values()
            if i.resume_mode == "pty"
            and i.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING)
        }
        for iid, saved in list(self._registry._persisted.items()):
            if iid in self._registry._instances:
                continue  # live-claimed, or already carded by a pass above
            name = saved.get("project_name")
            if not isinstance(name, str) or name not in discovered:
                continue  # unknown/undiscoverable project: nothing to resume into
            if _row_int(saved.get("bridge_pid")) is not None:
                continue  # has a pair; the row pass owns its verdict
            modes = self._sweep_modes(saved)
            if blocked_modes := {m for m in modes if (name, m) in blocked}:
                _log.warning(
                    "rediscover: %s still has an unresolved live %s bridge; leaving its "
                    "pid-less row %s uncarded rather than offer a duplicate Resume",
                    name,
                    "/".join(sorted(blocked_modes)),
                    iid,
                )
                continue
            # Mode-EXACT, not just `name in pending`: the sweep only reads the mechanism a
            # project had rows for, so a project swept for "pty" alone never had its pointer
            # read, and a standard row arriving during the await would pass a project-level
            # check while its pointer bridge is unexamined. Anything not swept for THIS
            # row's mode arrived during the await; fail CLOSED, since an uncarded row is
            # recoverable on the next start and a duplicate spawn is not.
            if unswept := modes - pending.get(name, set()):
                _log.warning(
                    "rediscover: row %s (%s, mode %s) arrived during the liveness sweep and "
                    "was never swept; deferring it to the next start",
                    iid,
                    name,
                    "/".join(sorted(unswept)),
                )
                continue
            self._registry._instances[iid] = self._stopped_from_row(iid, saved)
        if persist:
            await self._registry._persist()

    @staticmethod
    def _instance_from_pointer(
        name: str,
        ptr: BridgePointer,
        *,
        label: str,
        spawn_mode: SpawnMode,
        permission_mode: PermissionMode,
        resume_mode: ResumeMode,
        bridge_proc_start: float | None,
        keeper_pid: int | None,
        keeper_proc_start: float | None = None,
        # Keyword-required, no default, unlike its epoch sibling above (#1402): a caller that
        # forgot it would silently publish a keeper the drifting epoch alone has to defend,
        # and that is exactly the shape #1399's review caught three times. `adopt`'s standard
        # external bridge has no keeper and states so by passing None.
        keeper_start_ticks: int | None,
        bridge_start_ticks: int | None = None,
        bridge_boot_id: str | None = None,
        bridge_debug_log_path: Path | None = None,
        bridge_raw_log_path: Path | None = None,
        instance_id: str | None = None,
        worktree_name: str | None = None,
    ) -> RemoteControlInstance:
        """Build a RUNNING managed instance from a live Anthropic-written pointer.

        The pointer supplies the live-derived facts (bridge pid, env id, connect URL);
        the modes/label/keeper come from the caller (the persisted record or config
        defaults — the pointer carries none of them). Shared by :meth:`rediscover`
        (startup reattach of survivors) and :meth:`adopt` (runtime take-over of a
        standard external session) so both synthesize an identical managed shape. A
        bridge found alive is by definition NOT intentionally stopped.

        ``bridge_debug_log_path`` / ``bridge_raw_log_path`` re-bind the live tail to the
        log a *Clauster-spawned* survivor was already writing (rediscover passes the
        recovered set); they stay None for :meth:`adopt`, whose external bridge Clauster
        never spawned and has no log of (#584).

        ``instance_id`` — when supplied (re-discovered survivor whose id was persisted),
        the returned instance carries the same stable UUID so the registry key is
        consistent across restarts.  When ``None`` a fresh UUID is minted (adopt path,
        or a rediscovered bridge with no prior persisted record).

        ``worktree_name`` carries a row's EXPLICIT pty worktree name through (#1241) — set
        only for a session an earlier keeper-only reattach had to card under a fresh id,
        where the name is no longer derivable from that id. ``None`` (the normal case, and
        always for :meth:`adopt`'s external standard bridge) leaves it derived.
        """
        kwargs: dict = dict(
            project=name,
            label=label,
            spawn_mode=spawn_mode,
            permission_mode=permission_mode,
            resume_mode=resume_mode,
            worktree_name=worktree_name,
            keeper_pid=keeper_pid,
            # The keeper pid's start identity travels with it (#1178 / #1402) — a
            # pointer-walk survivor must regain the full PID-reuse defense, not the
            # cmdline-only degrade a pre-upgrade row gets, and not an epoch-only one that a
            # host clock correction turns into "keeper dead, row deleted".
            keeper_proc_start=keeper_proc_start,
            keeper_start_ticks=keeper_start_ticks,
            intentional_stop=False,
            status=InstanceStatus.RUNNING,
            bridge_pid=ptr.pid,
            bridge_proc_start=bridge_proc_start,
            # A pointer's ``procStart`` IS the boot-relative tick count, and converting it to
            # an epoch (as ``bridge_proc_start`` above does) is precisely what freezes today's
            # btime into the row and hands the result to a 0.05s bound. Carrying the original
            # keeps the immune value the pointer already gave us (#1399).
            bridge_start_ticks=bridge_start_ticks,
            # The current boot id (#1401). A pointer records none, but the caller only builds
            # this after `is_live_bridge`/cwd confirm the pointer's bridge is live NOW, so its
            # boot is the live one. Passed in (read off-thread by the async caller) so the row
            # persists with the cross-boot defense rather than boot-id-less.
            bridge_boot_id=bridge_boot_id,
            bridge_debug_log_path=bridge_debug_log_path,
            bridge_raw_log_path=bridge_raw_log_path,
            environment_id=ptr.environment_id,
            starter_session_id=ptr.session_id,
            url=f"https://claude.ai/code?environment={ptr.environment_id}",
        )
        if instance_id is not None:
            kwargs["instance_id"] = instance_id
        return RemoteControlInstance(**kwargs)

    async def adopt(self, name: str) -> RemoteControlInstance:
        """Take over a live *standard* external bridge as a managed instance (#330).

        Promotes an externally-started ``claude remote-control`` bridge — one Clauster
        didn't spawn (a terminal- or Desktop-launched session, surfaced as EXTERNAL by
        the ``agents --json`` cross-check) — into a fully-managed RUNNING instance so it
        gains the Stop/observe controls, *without* waiting for a restart. (``rediscover``
        already adopts such bridges at startup; this is the runtime equivalent, reusing
        the same pointer-synthesis.)

        Fail closed — never kills, never guesses:

        - unknown project -> :class:`UnknownProject` (404);
        - already managed -> :class:`InstanceStillLive` (409);
        - no live *standard* bridge at its pointer (it ended, or it's a pty/flag-form
          bridge, which is unsafe to adopt — no recoverable keeper, terminal-coupled
          Stop), or a live one that can't be positively attributed to THIS project
          (its cwd isn't the project directory — a ``sanitize_cwd`` pointer collision,
          or an unreadable cwd) -> :class:`AdoptionUnavailable` (409). pty external
          sessions stay display-only.

        Caveat the UI must carry: a standard bridge's environment server dies with its
        host, so a later Resume of the adopted session is a *fresh* Start, not a
        continuation of its prior conversation.
        """
        # Hold the per-project spawn lock so a concurrent spawn()/resume()/forget()
        # can't race the registry between the liveness check and the insert — and the
        # cross-process lock (#949) so a second clauster process's spawn can't be
        # mid-launch (pointer not yet visible) while we probe.
        # Deferred import breaks the runner<->rediscovery import cycle: these public
        # exception classes stay defined on ``clauster.runner`` (its façade contract and
        # the routes/tests import them from there), and by call time that module is fully
        # loaded. See the module docstring.
        from .runner import AdoptionUnavailable, InstanceStillLive, UnknownProject

        async with self._registry._spawn_lock_for(name), self._registry._bridge_flock(name):
            if self._get_instance_for_project(name) is not None:
                raise InstanceStillLive(f"{name!r} is already managed — nothing to adopt")
            proj = self._discovered().get(name)
            if proj is None:
                raise UnknownProject(f"no such project: {name!r}")
            # Fresh merge base (#949) so the saved label/modes read by the reattach
            # come from the store as it is now, and the trailing persist can't
            # resurrect/prune rows another process changed since construction.
            await self._registry._refresh_persisted()
            instance = await self._reattach_external_standard(proj)
            if instance is None:
                raise AdoptionUnavailable(
                    f"{name!r} has no live standard bridge to adopt — it may have ended, "
                    "or it's a pty (true-resume) bridge, which can't be adopted"
                )
            return instance

    async def _reattach_external_standard(self, proj: Project) -> RemoteControlInstance | None:
        """Reattach a live standard bridge this process didn't spawn; ``None`` if there is none.

        The shared take-over step behind :meth:`adopt` (explicit operator action) and
        :meth:`_spawn_locked`'s cross-process idempotency probe (#949). Reads the
        project's ``bridge-pointer.json`` and gates on
        :func:`procutil.is_live_standard_bridge` — liveness AND the standard-subcommand
        cmdline shape, checked at call time: a stale pointer (the bridge died since it
        was written) or a pty/flag-form bridge (no recoverable keeper, terminal-coupled
        Stop — unsafe to manage) both return ``None`` — as does a live bridge whose
        actual cwd is NOT this project's directory (a ``sanitize_cwd`` pointer-dir
        collision with another project; taking it over would misattribute a foreign
        pid). A hit is synthesized into a
        managed RUNNING instance (fresh ``instance_id``, ``resume_mode`` pinned
        ``"standard"`` from the positive cmdline gate rather than a possibly-stale
        persisted value), registered, and persisted.

        Caller must hold the per-project spawn lock and the cross-process bridge lock;
        the persisted-record read wants a fresh merge base (see the callers' preceding
        ``_refresh_persisted``).
        """
        ptr = await asyncio.to_thread(pointers.pointer_for_project, proj.path)
        if ptr is None or not await asyncio.to_thread(
            procutil.is_live_standard_bridge, ptr.pid, ptr.proc_start
        ):
            return None
        # Positive attribution: the pointer directory is keyed by the SANITIZED cwd
        # (non-alphanumerics → "-"), so two punctuation-differing project paths can
        # share one pointer file — and a take-over that trusted the pointer alone
        # would register ANOTHER project's bridge here, handing its Stop button a
        # foreign pid. Only proceed when the live process's actual cwd is this
        # project's directory; an unreadable cwd fails closed (never take over on a
        # guess). Same fail-closed posture as the #948 fork gate for the same
        # collision (#949 review).
        cwd = await asyncio.to_thread(procutil.proc_cwd, ptr.pid)
        if cwd is None or cwd.resolve() != proj.path.resolve():
            return None
        persisted_hit = self._persisted_for_project(proj.name)
        saved = persisted_hit[1] if persisted_hit is not None else {}
        spawn_mode, permission_mode, _resume_mode = self._saved_modes(saved)
        # The external bridge is live at this project's cwd right now, so the current boot is
        # its boot (#1401). Read off-thread, like the rediscover survivor path.
        boot_id = await asyncio.to_thread(procutil.proc_boot_id)
        instance = self._instance_from_pointer(
            proj.name,
            ptr,
            label=saved.get("label") or proj.name,
            spawn_mode=spawn_mode,
            permission_mode=permission_mode,
            resume_mode="standard",
            bridge_proc_start=procutil._expected_epoch(ptr.proc_start),
            bridge_start_ticks=_pointer_start_ticks(ptr.proc_start),
            bridge_boot_id=boot_id,
            # An adopted EXTERNAL session is a standard bridge with no keeper at all
            # (`is_standard_bridge_cmdline` gates adoption on exactly that), so the whole
            # keeper trio is stated as absent rather than left to a default.
            keeper_pid=None,
            keeper_proc_start=None,
            keeper_start_ticks=None,
        )
        self._registry._instances[instance.instance_id] = instance
        await self._registry._persist()
        return instance
