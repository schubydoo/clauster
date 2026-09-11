"""Poll + metrics background loops for the bridge lifecycle (part of #1157).

:class:`PollLoop` is the sixth collaborator extracted from
:class:`~clauster.runner.SessionRunner` (issue #1157). It owns the two long-lived
background loops the server runs while it is up:

- the **poll loop** — :meth:`poll_once` reconciles bridge liveness, cross-checks
  ``claude agents --json``, promotes stuck-STARTING adopted bridges
  (:meth:`_promote_ready_unwatched`), and prunes phantom cards; :meth:`_poll_forever`
  runs it on the configured interval.
- the **metrics loop** — :meth:`_refresh_metrics_cache` re-samples every running
  bridge concurrently; :meth:`_metrics_refresh_forever` runs it every
  ``metrics.poll_seconds``.

Unlike :class:`~clauster.bridge_launch.BridgeLaunch` (config + paths only), this
collaborator drives mutable runtime state on the event loop, so several ownership
rules are load-bearing:

- **Task ownership (async).** :meth:`start_poll_loop` creates the two long-lived tasks
  and stores them in :attr:`_poll_task` / :attr:`_metrics_task`, which ``PollLoop``
  OWNS. ``SessionRunner`` exposes each as a read/write proxy property, so
  ``SessionRunner.shutdown()`` still cancels + awaits + clears the SAME two task
  objects (``getattr``/``setattr`` over the proxies) — no loop task is orphaned and no
  ``Task was destroyed but it is pending`` warning is possible. Correctness therefore
  depends on the runner holding EXACTLY ONE ``PollLoop`` (built once in
  ``SessionRunner.__init__`` as ``self._poll_loop``).
- **Crash resilience (async), moved byte-for-byte.** :meth:`_poll_forever` and
  :meth:`_metrics_refresh_forever` catch per-iteration ``Exception`` and keep looping —
  one bad poll or sample must never kill the loop — while re-raising
  ``asyncio.CancelledError`` so task cancellation stops the loop promptly. The
  swallow path stays observable (a logged ``exception``), never a silent ``pass``.
- **All writes stay on the loop, through the single collaborators.** The registry
  writes (status reconcile, the crash tally, the phantom-card prune, the boot-id/ticks
  heal, the metrics cache) go through the ONE :class:`~clauster.runner_state.RunnerState`
  (``self._registry``); every lifecycle emission (``crash`` / ``ready``) goes through
  the ONE :class:`~clauster.record_facade.RecordFacade` (``self._record``). No second
  registry, no second notifier, and no registry mutation is moved off the event loop.
- **``_sessions`` is the poll loop's own cache.** The reconciled working-session list
  produced by :meth:`poll_once` is owned here; ``SessionRunner`` re-exposes it as a
  proxy property so its query methods (``tracked_sessions_by_instance``,
  ``external_sessions_by_project``, ``live_session_uuids``) and the tests that seed
  ``runner._sessions`` reach the one list.
- **No runner handle, late-resolving injections.** The still-on-runner helpers this loop
  calls but does not own — the per-bridge metrics sampler, the slow-refresh warner, the
  status reconciler, the session-ownership predicate, the redacted-mirror flush, the
  discovery snapshot, the connect-evidence reader, the cross-process adoption, the
  hosted-session provider, and the rediscover + stale-pointer GC that
  :meth:`start_poll_loop` runs first — are injected as callables (the same pattern the
  earlier collaborators use). ``PollLoop`` keeps no ``_runner`` attribute; each injected
  callable is a deferring lambda that closes over the runner and resolves its CURRENT
  attribute at call time, so a monkeypatched seam is honored and no bound method is frozen
  at construction. The row-tick decoder lives in ``field_decode`` (shared with the runner's
  readers) and is imported directly.

The runner delegates the moved methods to the single instance, preserving the exact
public signatures (and async-ness) that callers (app startup, the headless MCP server)
and the tests reach on the runner. :meth:`poll_once` and :meth:`start_poll_loop` are the
public members of that surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import inspector, procutil
from .claude_cli import ClaudeNotFound
from .config import ClausterConfig, ResumeMode
from .field_decode import _ticks_on_exact_match
from .models import (
    Attribution,
    InstanceStatus,
    Project,
    RemoteControlInstance,
    WorkingSession,
)
from .record_facade import RecordFacade
from .runner_state import RunnerState

_log = logging.getLogger("clauster.poll_loop")


class PollLoop:
    """Own the poll + metrics background loops for the bridge lifecycle."""

    def __init__(
        self,
        *,
        config: ClausterConfig,
        binary: str,
        registry: RunnerState,
        record: RecordFacade,
        sample_one_bridge: Callable[[RemoteControlInstance], Awaitable[dict | None]],
        warn_if_refresh_slow: Callable[[float], None],
        reconcile_status: Callable[[RemoteControlInstance, bool], None],
        can_own_sessions: Callable[[RemoteControlInstance], bool],
        flush_redacted_mirror: Callable[[RemoteControlInstance], None],
        discovered: Callable[[], dict[str, Project]],
        connect_facts_for: Callable[..., dict],
        adopt_rows_from_store: Callable[[], Awaitable[None]],
        hosted_provider: Callable[[], Callable[[], list[RemoteControlInstance]] | None],
        rediscover: Callable[[], Awaitable[None]],
        prune_stale_pointers: Callable[[], Awaitable[None]],
    ) -> None:
        """Bind to config + the shared registry/record and the injected runner helpers.

        ``registry`` is the ONE :class:`~clauster.runner_state.RunnerState` and ``record``
        the ONE :class:`~clauster.record_facade.RecordFacade`, both built in
        ``SessionRunner.__init__`` and passed by reference so every loop write and
        lifecycle emission lands on the same source of truth. The remaining arguments are
        callables the runner still owns (or that other collaborators own, reached through
        the runner's own delegators): they are injected — never captured as back-references
        — so ``PollLoop`` holds no runner. Each is a deferring lambda that resolves the
        runner's CURRENT attribute at call time (not a bound method captured at construction),
        so a test that swaps ``runner.rediscover`` / ``runner._flush_redacted_mirror`` /
        ``SessionRunner._emit_lifecycle``'s siblings (or any of them) after the runner is
        built still intercepts the call. Lifecycle emissions, by contrast, go straight to the
        single :class:`RecordFacade` (``self._record``) — no runner indirection.
        """
        self._config = config
        self._binary = binary
        self._registry = registry
        self._record = record
        self._sample_one_bridge = sample_one_bridge
        self._warn_if_refresh_slow = warn_if_refresh_slow
        self._reconcile_status = reconcile_status
        self._can_own_sessions = can_own_sessions
        self._flush_redacted_mirror = flush_redacted_mirror
        self._discovered = discovered
        self._connect_facts_for = connect_facts_for
        self._adopt_rows_from_store = adopt_rows_from_store
        self._hosted_provider = hosted_provider
        self._rediscover = rediscover
        self._prune_stale_pointers = prune_stale_pointers
        # The reconciled working-session cache produced by `poll_once`. Owned here; the
        # runner re-exposes it as a proxy property for its query methods and the tests.
        self._sessions: list[WorkingSession] = []
        # The two long-lived loop tasks (created in `start_poll_loop`). Owned here, but
        # cancelled + awaited + cleared by `SessionRunner.shutdown()` through the runner's
        # read/write proxy properties — the same task objects, the same teardown.
        self._poll_task: asyncio.Task | None = None
        self._metrics_task: asyncio.Task | None = None

    async def _refresh_metrics_cache(self) -> None:
        """Re-sample every running bridge into ``_metrics_cache`` (#354, #407).

        Samples all bridges CONCURRENTLY — each per-bridge ``to_thread`` is launched
        together and ``gather``ed — so refresh wall-time is ~max-per-bridge (capped by the
        default ``asyncio`` thread-pool, ``min(32, cpu+4)``; past that the samples batch),
        not the sum
        (#407; previously serial, which is why a high bridge count outran ``poll_seconds``
        and triggered ``_warn_if_refresh_slow``). Each sample goes through
        :meth:`_sample_one_bridge`, whose PID create-time guard drops a recycled PID rather
        than attributing it to the bridge. Each bridge is
        isolated via ``return_exceptions`` — one failing sampler is logged and dropped, never
        the rest. The cache is replaced wholesale, so a stopped/crashed bridge's stale sample
        drops out.
        """
        targets = list(self._registry._instances.values())
        results = await asyncio.gather(
            *(self._sample_one_bridge(inst) for inst in targets),
            return_exceptions=True,
        )
        fresh: dict[str, dict] = {}
        for inst, sample in zip(targets, results, strict=True):
            # BaseException, not Exception: a per-task CancelledError is stored by
            # gather(return_exceptions=True) and is NOT an Exception — drop it too,
            # never mis-store it as a sample (the outer cancel propagates separately).
            if isinstance(sample, BaseException):  # drop this bridge, never the loop
                _log.debug("metrics sample failed for %s: %s", inst.project, sample)
                continue
            if sample:
                # Keyed by instance_id (#778): several bridges may share one project,
                # and a project key would keep only whichever sampled last.
                fresh[inst.instance_id] = sample
        self._registry._metrics_cache = fresh

    async def _metrics_refresh_forever(self) -> None:
        """Refresh the metrics cache every ``metrics.poll_seconds`` until cancelled."""
        while True:
            started = time.monotonic()
            try:
                await self._refresh_metrics_cache()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("metrics cache refresh failed; continuing")
            self._warn_if_refresh_slow(time.monotonic() - started)
            await asyncio.sleep(self._config.metrics.poll_seconds)

    async def poll_once(self, *, side_effects: bool = True) -> None:
        """Reconcile bridge liveness and cross-check `claude agents --json`.

        Off-loop work, applied on-loop.

        ``side_effects=False`` makes this write-free, not read-only: it still mutates the
        in-memory registry (status reconcile — a reader must be able to SEE a crashed
        bridge — plus cross-process adoption and the phantom-card prune) and still computes
        the session cross-check, but emits no lifecycle event and writes no file. Reserved
        for read paths that need the cross-check
        :meth:`tracked_sessions_by_instance` depends on — the headless MCP server (#1104)
        — where the alternative was either mutating the live deployment on every session
        list or reporting no sessions at all. Pair it with ``rediscover(persist=False)``;
        on its own it still leaves the shared ``state.json`` written.

        The default stays ``True`` so the server's own poll loop is unaffected and a new
        caller cannot silently lose crash detection by forgetting the flag.
        """
        # Take over anything another process started before reconciling (#1091). Without
        # this the registry stays frozen at whatever `rediscover` found at startup, so a
        # `clauster start` / MCP-spawned bridge is never adopted and the cross-check below
        # labels its live process EXTERNAL/unmanaged.
        #
        # Best-effort, like the `agents --json` cross-check below: this reads the store and
        # builds instances from operator-writable fields, so a malformed row or an unreadable
        # log dir must not abort the whole tick. `_poll_forever` would swallow and retry, but
        # everything downstream — crash detection, notifications, the prune — would be dead
        # for as long as that row existed.
        try:
            await self._adopt_rows_from_store()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("cross-process adoption failed (continuing this tick)")
        # Projects whose bridge PROCESS is actually alive (PID + proc-start match).
        # This is the source of truth for "do we own the sessions at this cwd" below —
        # NOT the instance's status field, which can lag or be wrong (a fresh pty bridge
        # stuck pre-ready, a crash misdetection). See the `managed` set.
        live_projects: set[str] = set()
        # Per-instance "this pid is still OUR process", captured from the PAIR check this
        # loop already performs (pid + proc_start), so the phantom-prune below can reuse it
        # without a second psutil pass — and without blocking the event loop, where it has
        # to resolve its managed-pid exclusion post-await.
        pid_is_ours: dict[str, bool] = {}
        # Whether a pre-#1399 instance gained its `bridge_start_ticks` this tick (see the
        # stamp below): the only thing in this loop that must reach the store on its own,
        # because nothing else on the poll path writes and a restart would otherwise
        # re-open the same lottery.
        stamped = False
        for instance in list(self._registry._instances.values()):
            pid = instance.bridge_pid
            # A pty keeper is Clauster's direct child and self-exits with its bridge;
            # reap it here so it never lingers as a zombie after an organic exit.
            if instance.keeper_pid is not None:
                await asyncio.to_thread(procutil.reap_if_exited, instance.keeper_pid)
            if pid is None:
                continue
            await asyncio.to_thread(procutil.reap_if_exited, pid)
            alive = await asyncio.to_thread(
                procutil.is_live_bridge,
                pid,
                instance.bridge_proc_start,
                start_ticks=instance.bridge_start_ticks,
                boot_id=instance.bridge_boot_id,
            )
            pid_is_ours[instance.instance_id] = alive
            if (
                alive
                and instance.bridge_start_ticks is None
                and procutil.start_time_is_drift_prone()
            ):
                # A pre-#1399 row claimed or adopted on an epoch match is still judged on
                # that epoch every tick, and the first correction demotes it for good.
                # This tick matched exactly, which is the one moment the drift-immune half
                # can be read safely — see `_ticks_on_exact_match`. Gated on the platform
                # here as well, so a host that never drifts never pays the extra hop.
                # Assigned only on success: the hop is a suspension point, and an
                # unconditional write could put None over a value set meanwhile.
                ticks = await asyncio.to_thread(
                    _ticks_on_exact_match, pid, instance.bridge_proc_start
                )
                if ticks is not None:
                    instance.bridge_start_ticks = ticks
                    stamped = True
            if (
                alive
                and instance.bridge_start_ticks is not None
                and instance.bridge_boot_id is None
                and procutil.start_time_is_drift_prone()
            ):
                # The row has (or just healed, above) its drift-immune ticks and matched them
                # this tick, so it is our process in the CURRENT boot (#1401). Stamp the live
                # boot id, which then supersedes the coarse-epoch fallback across the next
                # restart — the same evidence the reattach stamp relies on, taken for free here.
                # Gated on the ticks so a tick-less row judged only on the drifting epoch never
                # reaches it. Assigned only on success, like the tick stamp above.
                boot = await asyncio.to_thread(procutil.proc_boot_id)
                if boot is not None:
                    instance.bridge_boot_id = boot
                    stamped = True
            # The running `claude` release for the card (#1275). Re-derived every tick
            # rather than memoized: it is one `exe` readlink for a live bridge, it is never
            # persisted, and re-reading is what guarantees the label can only ever describe
            # the process that is running NOW. Cleared for a dead bridge on the same tick
            # its status is reconciled, so a stopped card can't keep wearing a version.
            instance.claude_version = (
                await asyncio.to_thread(procutil.running_claude_version, pid) if alive else None
            )
            prev_status = instance.status
            self._reconcile_status(instance, alive)
            if (
                side_effects
                and prev_status is not InstanceStatus.CRASHED
                and instance.status is InstanceStatus.CRASHED
            ):
                # `_reconcile_status` above already marked the instance CRASHED, so an
                # observation-only caller still REPORTS the crash — it just doesn't
                # announce it. A one-shot reader has no standing to tell the operator a
                # bridge died: the live service owns that, and both firing would
                # double-notify for one death (#1104).
                self._registry._crash_counts[instance.project] = (
                    self._registry._crash_counts.get(instance.project, 0) + 1
                )
                # The crash notification is now fired inside _emit_lifecycle (#541), so
                # the single chokepoint owns history + webhook + notification together.
                self._record._emit_lifecycle("crash", instance)
            if alive:
                live_projects.add(instance.project)
                if side_effects:
                    # Keep the public bridge log redacted-current as the bridge writes.
                    # No-op unless on-disk redaction split the raw/public paths.
                    # Skipped when observing: the live service flushes this on its own
                    # loop, and a read must not write into the instance's log set.
                    await asyncio.to_thread(self._flush_redacted_mirror, instance)

        if stamped and side_effects:
            # At most once per tick-less row, ever: after this the row carries ticks and the
            # stamp never fires for it again. Skipped when observing, like every other
            # write on this path; the live service's own loop will stamp and persist.
            await self._registry._persist()
        # Deliberately ABOVE the cross-check, which returns early whenever `agents --json`
        # is degraded: a bridge pinned at STARTING must not stay pinned for as long as that
        # probe keeps failing.
        await self._promote_ready_unwatched(pid_is_ours, side_effects=side_effects)

        try:
            sessions = await asyncio.to_thread(inspector.list_working_sessions, self._binary)
        except (
            ClaudeNotFound,
            subprocess.SubprocessError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            # Cross-check is best-effort — keep the loop alive, but log so a degraded
            # `agents --json` probe is observable instead of silently freezing sessions.
            _log.warning("agents --json cross-check failed (continuing): %s", exc)
            return
        discovered = self._discovered()
        # A managed bridge owns the working sessions at its cwd if its PROCESS is alive
        # (computed above), or — the one status-based exception, see the explicit arm below
        # (#713) — if it is still STARTING. Keying on a *live* process rather than a stale
        # RUNNING status is what keeps a genuinely dead instance from hiding a real external
        # bridge: a `_stopped_from_persisted` phantom (no live process) is correctly absent
        # here, so a real flag-form/tmux bridge at its cwd still surfaces as external. The
        # STARTING arm is safe for the same reason — a dead STARTING row was already
        # reconciled to CRASHED/STOPPED in the loop above, so it can't shadow an external
        # bridge; it only covers a just-spawned bridge whose pid isn't live yet but whose
        # auto-created session `agents --json` already reports.
        #
        # Values are LISTS of instance_id, never a project name (#1020 symptom A3): a
        # project may run a standard bridge AND N interactive bridges at once (#778), so
        # a project-keyed value folds them into one bucket and the standard bridge's row
        # then lists the independent interactive sessions as if it owned them. Reconcile
        # picks the one owner out of the candidates by pid ownership.
        managed: dict[Path, list[str]] = {}
        for i in self._registry._instances.values():
            if (
                i.project in discovered
                and (i.project in live_projects or i.status is InstanceStatus.STARTING)
                and self._can_own_sessions(i)
            ):
                managed.setdefault(Path(discovered[i.project].path), []).append(i.instance_id)
        # A worktree-spawn bridge runs each session in a per-session worktree under
        # `<root>/.claude/worktrees/` (`claude remote-control --spawn worktree`), so the
        # session cwd never exactly matches the project-root key above — without this the
        # session reads EXTERNAL and the dashboard shows no live-session count for the
        # bridge. Reconcile attributes such a session to its bridge by containment in
        # that worktree subtree.
        # Same per-instance shape as `managed` above, and load-bearing here: N interactive
        # bridges share ONE project root, so a project-keyed value cannot say which of them
        # a given worktree session belongs to (#1020 A3).
        worktree_roots: dict[Path, list[str]] = {}
        for i in self._registry._instances.values():
            if (
                i.project in discovered
                and (i.project in live_projects or i.status is InstanceStatus.STARTING)
                and i.spawn_mode == "worktree"
                and self._can_own_sessions(i)
            ):
                worktree_roots.setdefault(Path(discovered[i.project].path), []).append(
                    i.instance_id
                )
        # Clauster's own hosted (claustrum) sessions run no bridge process, so the
        # cross-check would otherwise see their live `claude` pid and label it
        # EXTERNAL/unmanaged (#592). Claim them by the CT-1 agent_pid (authoritative)
        # and, for a pre-CT-1 daemon with no pid, by their workspace cwd. Only RUNNING/
        # STARTING rows are claimed: a stopped row's pid could be reused by an unrelated
        # process, and a stopped session has no live process to attribute anyway.
        hosted_pids: dict[int, str] = {}
        hosted_cwds: dict[Path, str] = {}
        hosted_provider = self._hosted_provider()
        if hosted_provider is not None:
            for inst in hosted_provider():
                hid = inst.claustrum_process_id
                if hid is None:
                    continue
                # Claim a row only when it can have a live process: RUNNING/STARTING, or an
                # orphan (CL-8) — a CRASHED row whose agent survived a daemon restart and
                # whose live pid must be claimed too, or the survivor reads as EXTERNAL. A
                # genuinely dead row is skipped so a reused pid/cwd isn't mis-claimed.
                if inst.status not in (InstanceStatus.RUNNING, InstanceStatus.STARTING) and (
                    not inst.is_orphan
                ):
                    continue
                if inst.agent_pid is not None:
                    hosted_pids[inst.agent_pid] = hid
                else:
                    # Pre-CT-1 daemon: with no pid to match, fall back to the workspace
                    # cwd. Reached only by RUNNING/STARTING pre-CT-1 rows — an orphan always
                    # carries a pid (HostedManager._is_orphan requires it), so it never lands
                    # here. Skipped when a pid IS known: a cwd claim there would also swallow
                    # a genuine EXTERNAL bridge co-located at the project path (hiding it from
                    # adoption + the phantom-prune), the very stale-card symptom #592 removes.
                    proj = discovered.get(inst.project)
                    if proj is not None:
                        hosted_cwds[Path(proj.path)] = hid
        # Ownership gate for the exact-cwd join (#820): an external SSH/terminal
        # `claude` sharing a managed bridge's cwd must stay EXTERNAL, not fold into the
        # bridge's tracked sessions. A managed session's worker pid descends from its
        # bridge (or pty keeper) process, so a bridge's own live pid(s) plus their
        # descendants are the authoritative "we spawned this" set cwd containment can't
        # give. Same live/STARTING + discovered filter as `managed`, keeping only bridges
        # with at least one resolvable pid: a bridge with no known pid yet (STARTING pty,
        # pre-sidecar) is left unkeyed → cwd-only, preserving the #713 startup-window
        # attribution. Never root ownership at a dead instance's stale pid (it could be
        # reused).
        #
        # Keyed per INSTANCE, not per cwd (#1020 A3). Co-located bridges — a standard plus
        # N pty at one project root — each own distinct worker pids, and it is exactly that
        # distinction that says which bridge a session belongs to. The old per-cwd union
        # deliberately merged them (so neither flipped the other's children to EXTERNAL),
        # but merging is also what made every session on a project attribute to one bucket.
        # Reconcile now unions across candidates itself when it has to, and separates them
        # when it can.
        roots_by_instance: dict[str, tuple[int, ...]] = {}
        for i in self._registry._instances.values():
            if i.project in discovered and (
                i.project in live_projects or i.status is InstanceStatus.STARTING
            ):
                roots = tuple(p for p in (i.bridge_pid, i.keeper_pid) if p is not None)
                if roots:
                    roots_by_instance[i.instance_id] = roots
        # `owned_pids` returns the roots plus their readable descendants — the roots
        # themselves are owned because a single-session flag-form pty
        # (`claude --remote-control`) can report its `agents --json` pid as the bridge
        # process itself (in-process), and a reattached pty with a rotated/missing keeper
        # contributes only bridge_pid. A root whose tree can't be READ (AccessDenied:
        # hardened /proc, hidepid, restricted container) contributes only its own pid, so a
        # keyed INSTANCE always gates: a session that isn't provably owned reads EXTERNAL,
        # never silently re-enabling the cwd-only join #820 removed. Only a bridge with no
        # resolvable pid yet (STARTING pty pre-sidecar) is absent from `roots_by_instance`
        # → cwd-only (#713 window); a pid-less row that ISN'T starting was already dropped
        # from the candidate lists above (`_can_own_sessions`), so it can never become the
        # ungated candidate that would swallow an unowned pid. psutil walk → to_thread.
        owned_pids_by_instance = await asyncio.to_thread(
            lambda: {iid: procutil.owned_pids(roots) for iid, roots in roots_by_instance.items()}
        )
        self._sessions = inspector.reconcile(
            sessions, managed, hosted_pids, hosted_cwds, worktree_roots, owned_pids_by_instance
        )
        # Drop a non-live managed instance whose project has a live EXTERNAL session:
        # the bridge IS alive, just unmanaged (flag-form/tmux), so the persisted record
        # is a phantom. Showing it as a Stopped/Resume card is misleading and invites a
        # double-spawn — let the card fall back to "external session active" instead.
        # Only sessions that are actually BRIDGES count as evidence. The prune's whole
        # premise is "the bridge IS alive, just unmanaged, so this Stopped card is a
        # phantom" — an operator's hand-run `claude` at the project root is EXTERNAL by
        # design (#820) but is NOT a bridge, and deleting a resumable card because someone
        # opened a terminal there is wrong. `live_projects` used to mask this by accident;
        # testing the thing the premise actually claims is the honest replacement.
        #
        # Walked up the ANCESTRY, not tested on the session pid itself (#1116): a Server
        # Mode session's pid is the SDK worker and the bridge is its parent, so a direct
        # `is_bridge_process(s.pid)` never matched it. A flag-form pty session reports the
        # BRIDGE's own pid and matches at depth 0, so that shape already worked — this
        # widens a half-working gate rather than turning on a dead one, and the accurate
        # risk framing is that the Server Mode arm is newly live.
        #
        # Then excluded by MANAGED pid, which is what keeps this honest: finding a bridge
        # ancestor proves a bridge is responsible for the session, NOT that it is unmanaged.
        # A session of OUR bridge that reads EXTERNAL because the #820 pid gate could not
        # enumerate the tree would otherwise become evidence for deleting that same
        # project's stopped cards.
        #
        # The exclusion is applied AFTER the await, against a freshly-read `_instances` —
        # never against a set snapshotted before it. `poll_once` runs lock-free, so a
        # lock-holding adopt()/spawn() can publish a `bridge_pid` while this walk is
        # suspended; a pre-await snapshot would miss it, that bridge's own session would
        # read unmanaged, and a sibling STOPPED card would be deleted on the strength of a
        # bridge Clauster had adopted meanwhile. The thread therefore returns raw OWNERS and
        # the loop decides, so the read and the `del` sit in one synchronous block.
        #
        # `attribution is EXTERNAL` stays the FIRST conjunct on purpose: it is what bounds
        # the psutil walk to external sessions instead of every session, every tick.
        # ALL owners per cwd, as a set — never one owner per cwd. Several EXTERNAL sessions
        # can share a resolved cwd with DIFFERENT bridge ancestors (a managed bridge whose
        # session read EXTERNAL because the #820 pid gate could not enumerate its tree, beside
        # a genuinely unmanaged one). A `{cwd: owner}` dict keeps only the last, so whichever
        # `agents --json` happened to list last would decide: a managed owner landing last hid
        # the live unmanaged bridge and left its phantom card up with a Resume that spawns a
        # duplicate. Order of an external list is not a fact about ownership.
        reconciled = list(self._sessions)

        def _owners_by_cwd() -> dict[Path, set[int]]:
            """Map each external session's resolved cwd to the bridge pids that own it."""
            out: dict[Path, set[int]] = {}
            for s in reconciled:
                if s.attribution is not Attribution.EXTERNAL:
                    continue  # first: bounds the psutil walk to external sessions
                owner = procutil.bridge_ancestor(s.pid)
                if owner is not None:
                    out.setdefault(s.cwd.resolve(), set()).add(owner)
            return out

        owners_by_cwd = await asyncio.to_thread(_owners_by_cwd)
        # Keeper pids unioned in as well: a keeper's cmdline is never a bridge cmdline, so
        # `bridge_ancestor` cannot return one today — but if the pty spawn shape ever
        # changes, an unexcluded keeper fails in the DESTRUCTIVE direction, and excluding it
        # only ever under-prunes.
        # Only pids that are still OUR process. `stop()` leaves `bridge_pid` on a dead card,
        # so an unfiltered set treats every historical pid as current ownership — and once
        # the OS recycles one onto a genuinely unmanaged bridge, that bridge reads "managed",
        # its evidence is discarded, and the phantom card stays up offering a Resume that
        # spawns a duplicate beside it. That is the exact hazard this prune exists to remove,
        # so "it only under-prunes" is not a defence here.
        #
        # Judged on the PAIR (pid + proc_start) captured above, never the bare pid — the rule
        # this file states for liveness everywhere else. That keeps the stop() grace window
        # safe too: a card already marked STOPPED whose process is still alive still matches
        # its pair, so it stays excluded and cannot prune its own project. Default True for an
        # instance the loop above never saw — one adopted DURING the walk — which is the
        # post-await TOCTOU case; assuming ours there is the non-destructive direction.
        #
        managed_pids = {
            pid
            for inst in self._registry._instances.values()
            if pid_is_ours.get(inst.instance_id, True)
            for pid in (inst.bridge_pid, inst.keeper_pid)
            if pid is not None
        }
        # ⚠️ #1399: `managed_pids` alone is not enough, because `pid_is_ours` IS
        # `is_live_bridge` and that predicate has a false-negative mode this delete cannot
        # absorb. Where the start-time pair degrades to the epoch alone AND the platform's
        # create-time drifts — a pre-#1399 row on Linux — an NTP correction makes a LIVE
        # bridge answer False. (macOS and Windows record an absolute create-time at exec and
        # never drift, which is why the exclusion below asks `start_time_is_drift_prone`
        # rather than assuming every host is exposed.) Both halves
        # of the prune then fail together: the instance is demoted to STOPPED so it becomes a
        # candidate, AND its own pid drops out of the set above so its own bridge becomes the
        # "unmanaged" evidence against it. Observed 19 times in 2.5 hours on the dogfood host,
        # every log line naming clauster's own bridge pid.
        #
        # The fix is to scope ownership evidence to the project it is evidence ABOUT: a pid
        # held by an instance OF THIS PROJECT cannot demonstrate an *unmanaged* bridge at that
        # project's cwd, because the prune's premise is "some bridge here is alive and we do
        # not have it" and we demonstrably do have it.
        #
        # Restricted to instances whose liveness verdict is INCONCLUSIVE, which is what keeps
        # it from reopening the hazard the paragraph above protects. A verdict is conclusive
        # when the pair could be judged on the drift-immune half — ticks recorded, or a
        # platform whose create-time does not drift at all
        # (:func:`procutil.start_time_is_drift_prone`) — and a conclusive "not our process"
        # is exactly what it says, so that pid stays evidence even at its own project's cwd.
        # Only where the epoch was the sole evidence can a False mean "the clock moved", and
        # there the destructive reading is the one we refuse.
        #
        # ⚠️ For those tick-less rows this DOES re-admit the shape the `managed_pids`
        # paragraph above calls indefensible: a long-dead card's stale pid, recycled onto a
        # genuinely unmanaged bridge at the same cwd, cancels that bridge as evidence and its
        # phantom card lingers. That trade is deliberate and bounded — it applies only where
        # we cannot tell drift from reuse, and the alternative there is deleting a running
        # session's card. Read that paragraph as absolute for a row WITH ticks, which is
        # every row a current build writes.
        #
        # Deliberately NOT "was live when this tick started". That covered the demotion tick
        # only: `_reconcile_status` never promotes back, so on the NEXT tick the victim is
        # already STOPPED, drops out again, and the card is deleted one poll later — the same
        # bug, one tick down the road. Being inconclusive is a property of the evidence, not
        # of when we looked, so it does not expire.
        #
        # Keyed by resolved cwd rather than by project NAME so there is no sentinel to get
        # wrong for a row whose `project` degraded to "" — an absent cwd simply has no
        # entry, and a project we cannot locate contributes no exclusion.
        # Only projects with a live registry entry: this is N `realpath` syscalls per poll on
        # the hot path (and `resolve()` is materially costlier on Windows), and a project with
        # no instance can contribute neither an exclusion nor a prune candidate.
        cwd_of_project = {
            name: Path(discovered[name].path).resolve()
            for name in {i.project for i in self._registry._instances.values()}
            if name in discovered
        }
        own_project_pids: dict[Path, set[int]] = {}
        for inst in self._registry._instances.values():
            proj_cwd = cwd_of_project.get(inst.project)
            if proj_cwd is None or not (
                inst.bridge_start_ticks is None and procutil.start_time_is_drift_prone()
            ):
                continue
            for pid in (inst.bridge_pid, inst.keeper_pid):
                if pid is not None:
                    own_project_pids.setdefault(proj_cwd, set()).add(pid)
        # ANY unmanaged owner makes the cwd evidence — the remainder non-empty. A managed
        # owner sharing the cwd neither creates evidence nor cancels it.
        external_cwds = {
            cwd
            for cwd, owners in owners_by_cwd.items()
            if owners - managed_pids - own_project_pids.get(cwd, set())
        }
        # No _persist() after this delete, by design: the ROW survives, so the deletion is
        # recoverable. Persisting would be a no-op anyway: _persist_subset overlays `live`
        # onto the retained `_persisted` map, which keeps the record (intentionally — it
        # preserves the project's modes for a later managed spawn).
        #
        # ⚠️ Recoverable by RESTART, not by the next poll (an earlier revision of this
        # comment claimed the latter). `_stopped_from_persisted` is reachable only from
        # `rediscover`, which the server runs ONCE (`start_poll_loop`), and the per-tick
        # `_adopt_rows_from_store` is live-rows-only by explicit design — so a card pruned
        # here stays gone from the dashboard until Clauster restarts. That asymmetry is why
        # the gate above resolves its exclusion post-await instead of trusting a stale
        # snapshot: the Server Mode arm of this path was inert until #1116 (see above), so
        # its latent false positives all go from unreachable to live at once.
        # Gathered per PROJECT first, then decided per INSTANCE (#1096). The old
        # `inst.project not in live_projects` test was project-level, which since #778 (N
        # instances per project) is wrong in BOTH directions: over-prune — one unmanaged
        # bridge at the project root deleted EVERY stopped row for that project; under-prune
        # — a live sibling put the project in `live_projects`, so a genuine phantom was never
        # pruned. A live sibling demonstrably CAN coexist with an EXTERNAL session (ownership
        # is pid DESCENT, #820), so project liveness never tested the prune's premise — "the
        # bridge IS alive, just unmanaged" — and the stricter guard below does: the external
        # session must actually BE a bridge.
        candidates: dict[str, list[str]] = {}
        for n, inst in list(self._registry._instances.items()):
            # Only prune non-live (STOPPED/CRASHED) phantoms. A RUNNING/STARTING instance
            # here was inserted AFTER this poll snapshotted its state (the lock-free poll
            # races a lock-held adopt()/spawn() that lands during the list_working_sessions
            # suspension) — pruning it would silently undo a just-adopted/spawned bridge.
            # The first loop already reconciled every instance it saw, so a genuinely-dead
            # record is no longer RUNNING by the time we get here.
            if inst.status in (InstanceStatus.RUNNING, InstanceStatus.STARTING):
                continue
            if inst.project in cwd_of_project and cwd_of_project[inst.project] in external_cwds:
                candidates.setdefault(inst.project, []).append(n)
        for project, ids in candidates.items():
            # One external bridge can only be ONE of these rows. With several candidates
            # nothing here says which, so prune none rather than delete N-1 resumable cards
            # to explain a single unmanaged process. Left visible and logged: a stale card
            # the operator can Forget is recoverable; a silently deleted one is not.
            if len(ids) > 1:
                _log.debug(
                    "not pruning %d stopped cards for %r: one external session can't "
                    "identify which is the phantom",
                    len(ids),
                    project,
                )
                continue
            n = ids[0]
            cwd = cwd_of_project[project]
            # INFO, not debug: this DELETES a resumable card, and the row it leaves behind
            # is only re-materialized by `rediscover` — i.e. on restart, not next tick. The
            # skip above was logged while the destructive branch was silent, which is
            # backwards. Names the owning bridge pid so an operator can tell a genuine
            # unmanaged bridge from a mis-attributed one (#1116 turned this path on for
            # Server Mode, where it had never fired).
            _log.info(
                "pruning stopped card %s for %r: live unmanaged bridge pid %s at its cwd",
                n,
                project,
                # Only the UNMANAGED owners: those are the evidence this delete rests on.
                # A managed owner sharing the cwd is not why the card is going.
                sorted(
                    owners_by_cwd.get(cwd, set()) - managed_pids - own_project_pids.get(cwd, set())
                )
                or "?",
            )
            del self._registry._instances[n]
            self._registry._procs.pop(n, None)  # don't leak the phantom's dead Popen handle

    async def _promote_ready_unwatched(
        self, pid_is_ours: dict[str, bool], *, side_effects: bool
    ) -> None:
        """Promote a STARTING bridge that nothing else will ever look at again (#1106).

        Every other promotion path belongs to a bridge THIS process spawned:
        :meth:`_apply_markers` and :meth:`_apply_pty_info`, both driven by
        :meth:`_watch_startup`. A bridge taken over from another process's row has none
        of them — :meth:`_adopt_rows_from_store`'s re-sync is one-shot (once the pids
        agree, the first arm of its loop wins on every later tick and the connect facts
        are never re-read), and :meth:`_reconcile_status` only ever demotes, by design.

        So an instance adopted *before* its connect evidence existed — the peer's own
        ``_await_ready`` timed out and persisted a live pid ahead of its pointer or ready
        sidecar — is pinned at STARTING for the life of the process, and the dashboard
        shows "preparing connect link…" forever for a bridge that is fine.

        Re-read that evidence here for exactly the rows that can be stuck: STARTING, pid
        provably still ours, no connect URL yet, and no startup watch of their own.
        Promotion still requires the evidence itself — ``_connect_facts_for`` resolves
        only from a live pointer or a ready sidecar — so a bridge that is merely alive
        stays STARTING, which is the honest state and the rule ``_reconcile_status``
        states. This never demotes and never touches a row it has no evidence for.

        ``pid_is_ours`` defaults to **False** for an instance the caller's loop never saw
        (one adopted mid-tick): not promoting is the conservative direction here, and the
        next tick sees it. Runs under ``side_effects=False`` too — a reader must SEE the
        real state — but announces the transition only when side effects are allowed,
        matching the crash arm in :meth:`poll_once`.
        """
        stuck = [
            inst
            for inst in self._registry._instances.values()
            if inst.status is InstanceStatus.STARTING
            and inst.url is None
            and inst.bridge_pid is not None
            and pid_is_ours.get(inst.instance_id, False)
            and inst.instance_id not in self._registry._startup_watches
        ]
        if not stuck:
            return
        discovered = self._discovered()
        pending: dict[
            str, tuple[Project, ResumeMode, int, float | None, int | None, str | None]
        ] = {}
        for inst in stuck:
            proj = discovered.get(inst.project)
            pid = inst.bridge_pid
            if proj is None or pid is None:
                continue  # project gone from discovery: nothing to read the evidence from
            pending[inst.instance_id] = (
                proj,
                inst.resume_mode,
                pid,
                inst.bridge_proc_start,
                inst.bridge_start_ticks,
                inst.bridge_boot_id,
            )
        if not pending:
            return
        # One thread hop for the whole tick's worth of pointer reads / sidecar globs,
        # matching `_adopt_rows_from_store`'s batched liveness probe. The liveness
        # probe runs AFTER the evidence read (tuple order is evaluation order): a
        # bridge that exits mid-read would otherwise be promoted on stale-but-matching
        # evidence — a false `ready` webhook, then a crash on the next tick. Rechecked
        # here, the death is seen and the reconcile pass owns the verdict instead.
        facts = await asyncio.to_thread(
            lambda: {
                iid: (
                    self._connect_facts_for(*args[:4], start_ticks=args[4]),
                    procutil.is_live_bridge(
                        args[2], args[3], start_ticks=args[4], boot_id=args[5]
                    ),
                )
                for iid, args in pending.items()
            }
        )
        for iid, (connect, alive) in facts.items():
            if not connect:
                continue  # still no evidence: STARTING is the honest state, leave it
            if not alive:
                continue  # died during the evidence read: never a ready for a dead bridge
            inst = self._registry._instances.get(iid)
            expected = pending[iid]
            # Re-checked across the await, like every other mutation on this lock-free
            # path: a lock-holding spawn()/resume()/stop() can have replaced this object,
            # resolved it, or moved it onto a different process generation meanwhile —
            # publishing the evidence we gathered for the OLD generation would hand the
            # operator a link into an environment that is already gone.
            if (
                inst is None
                or inst.status is not InstanceStatus.STARTING
                or inst.instance_id in self._registry._startup_watches
                or inst.bridge_pid != expected[2]
                or inst.bridge_proc_start != expected[3]
            ):
                continue
            inst.url = connect.get("url") or inst.url
            inst.environment_id = connect.get("environment_id") or inst.environment_id
            inst.starter_session_id = connect.get("starter_session_id") or inst.starter_session_id
            # Unconditional, unlike the fields above (#1438): a note is a statement about the
            # CURRENT sidecar, not a fact to preserve. This is the THIRD `_connect_facts_for`
            # caller that gates on the dict — a ready screen-fault sidecar now returns a
            # notice-only dict, so this leg promotes it too, and without this line it would go
            # RUNNING with no link AND no reason (the exact #1438 state) on a row reattached
            # STARTING and later promoted here.
            inst.notice = connect.get("notice")
            inst.status = InstanceStatus.RUNNING
            if side_effects:
                self._record._emit_lifecycle("ready", inst)

    async def _poll_forever(self) -> None:
        """Run ``poll_once`` on the configured interval, logging and continuing on error."""
        interval = self._config.claude.agents_json_poll_interval_seconds
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let an unexpected error kill the daemon's poll loop, but make
                # it observable — a silent `pass` here hid status-reconcile failures.
                _log.exception("poll_once failed; continuing")
            await asyncio.sleep(interval)

    async def start_poll_loop(self) -> None:
        """Rediscover already-running bridges, then start the background poll loop."""
        await self._rediscover()
        # #867 L4: after live bridges are adopted, GC long-dead pointers (hygiene).
        await self._prune_stale_pointers()
        self._poll_task = asyncio.create_task(self._poll_forever())
        # Server-side metrics sampler (#354): only when the feature is on. Keeps the
        # per-project / batch / scrape reads at O(1) with no per-request thread.
        if self._config.metrics.enabled:
            self._metrics_task = asyncio.create_task(self._metrics_refresh_forever())
