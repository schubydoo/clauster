"""Shared registry / locks / persist-mirror hub for the bridge lifecycle (part of #1157).

:class:`RunnerState` is the foundation collaborator extracted from
:class:`~clauster.runner.SessionRunner` (issue #1157). It owns the state hub that
spawn / poll / rediscover / adopt / forget / stop all read and write: the
instance-keyed registry (``_instances``), the parallel ``Popen`` map (``_procs``),
the per-spawn startup-watch tasks (``_startup_watches``), the crash tally
(``_crash_counts``), the metrics cache (``_metrics_cache``), the persist merge
mirror (``_persisted`` / ``_row_backed`` / ``_last_saved``) over the DB-backed
``StateStore``, and the four locks that serialize all of the above.

Unlike :class:`~clauster.bridge_launch.BridgeLaunch` /
:class:`~clauster.bridge_prune.BridgePrune` (config + paths only) this collaborator
is the mutable heart of the runner, so its ownership rules are load-bearing:

- **Lock order is acyclic and unchanged.** Every acquisition site takes the locks
  in exactly one order — in-process :meth:`_spawn_lock_for` (per project) →
  per-project :meth:`_bridge_flock` → :attr:`_persist_lock` → store-wide
  :meth:`_store_flock`. The two cross-process ``flock`` layers are always
  inproc-first / cross-process-second (the :mod:`clauster.atomicio` convention), and
  :meth:`_store_flock` is only ever taken AFTER a per-project flock and never before
  one, so the levels cannot deadlock across processes. Moving state does not move a
  single acquisition; the runner's spawn / stop / forget / adopt / resume sites keep
  taking these same locks in this same order through the façade delegators.
- **The persist path stays serialized.** :meth:`_persist` runs its whole
  refresh → merge → save under :attr:`_persist_lock` AND the store-wide
  :meth:`_store_flock`: the save is a full-table replace, and the per-project flocks
  don't exclude a different project's writer in another process, so an unserialized
  load → save could straddle another process's save and prune its fresh row. No
  ``await`` is added between taking those locks and the write, and neither lock is
  dropped early.
- **The registry mutates only on the event loop.** As on the runner, the dicts are
  read/written on the loop; blocking work runs in ``asyncio.to_thread`` and returns
  values the loop applies back. ``RunnerState`` holds the ONE copy of each; the runner
  re-exposes ``_instances`` / ``_procs`` / ``_startup_watches`` / ``_crash_counts`` /
  ``_metrics_cache`` / ``_persisted`` / ``_row_backed`` / ``_last_saved`` as thin proxy
  properties so every existing caller and test seam reaches this single source of truth.
  Correctness therefore depends on the runner holding EXACTLY ONE ``RunnerState``
  (built once in ``SessionRunner.__init__`` as ``self._state``).
- **``_persisted_liveness`` is NOT owned here.** It coerces a row's liveness identity
  with the module-level ``_row_*`` helpers that many still-on-the-runner reattach /
  adopt / rediscover methods also use, so it stays on ``SessionRunner`` and is injected
  once as the ``persisted_liveness`` callable — mirroring how ``RecordFacade`` receives
  ``project_path``. The store is passed in by reference (``StateStore``), never rebuilt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from . import atomicio
from .config import ClausterConfig
from .db.stores import StateStore
from .models import InstanceStatus, RemoteControlInstance

_log = logging.getLogger("clauster.runner_state")


def _release_flock_if_acquired(cm) -> Callable[[asyncio.Task], None]:
    """Build the done-callback that releases a flock a CANCELLED caller still acquired.

    ``_bridge_flock`` acquires the blocking cross-process lock in a worker thread; if
    the awaiting task is cancelled mid-acquire, the thread finishes anyway and would
    otherwise hold the lock until GC reclaims the context manager. The callback exits
    the manager (an ``os.close``, trivially fast on the loop) once the acquisition
    task lands — and only when it actually succeeded (a failed/cancelled acquire
    never entered the lock, so exiting it would raise).
    """

    def _release(task: asyncio.Task) -> None:
        """Release the flock only once the task finished without cancellation or error."""
        if not task.cancelled() and task.exception() is None:
            cm.__exit__(None, None, None)

    return _release


class RunnerState:
    """Own the registry, the four lifecycle locks, and the persist mirror over the store."""

    def __init__(
        self,
        *,
        config: ClausterConfig,
        lock_dir: Path,
        store: StateStore,
        persisted_liveness: Callable[[RemoteControlInstance], dict],
    ) -> None:
        """Bind to config + the lock dir + the state store, and load the persist merge base.

        ``store`` is :meth:`SessionRunner.persistence.state_store` passed by reference,
        never rebuilt. ``persisted_liveness`` is :meth:`SessionRunner._persisted_liveness`
        (kept on the runner — its ``_row_*`` helpers are shared with the still-on-runner
        reattach/adopt methods) injected so :meth:`_persist_subset` can build a row's
        liveness pair without this collaborator holding those helpers. ``lock_dir`` is the
        runner's pinned deployment lock dir, so a later global ``configure_lock_dir`` for a
        different state dir cannot redirect this runner's cross-process locks.
        """
        self._config = config
        self._lock_dir = lock_dir
        self._store = store
        self._persisted_liveness = persisted_liveness
        # Registry keyed by instance_id (stable UUID, #777). Standard bridges keep
        # one entry per project; pty sessions may have N entries per project.
        self._instances: dict[str, RemoteControlInstance] = {}
        # Per-project bridge-crash tally since process start, exposed as the
        # clauster_bridge_crashes_total counter (#352) — a crash that resumes between
        # scrapes still leaves a trace, unlike the current-status gauge.
        self._crash_counts: dict[str, int] = {}
        # Popen handles keyed by instance_id (parallel to _instances).
        self._procs: dict[str, subprocess.Popen] = {}
        # Server-side metrics snapshot (#354): the metrics task refreshes it off the
        # request path so /api/projects/{name}/metrics + the batch read + the /metrics
        # scrape all serve from the last sample at O(1), no per-request thread. Keyed
        # by instance_id (#778 — a project may run several bridges, so a project key
        # would clobber); the public readers either serve that per-instance truth
        # (#1090) or fold it per project.
        self._metrics_cache: dict[str, dict] = {}
        # Per-spawn background tasks that watch a STARTING bridge until it either
        # registers an environment (-> RUNNING) or proves stuck (-> ERROR).
        # Keyed by instance_id (one watch per spawned instance).
        self._startup_watches: dict[str, asyncio.Task] = {}
        # Per-project locks serializing concurrent spawns of the SAME project (see
        # ``spawn``). Keyed by project name — the per-project standard-singleton check
        # and pty-warning logic must run serially for the same project.
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        self._persisted: dict[str, dict] = self._store.load()
        # Instance ids whose row this process has OBSERVED in (or saved to) the store —
        # grown on every base load/refresh and every successful save, never pruned by
        # a refresh. The persist subset uses it as the ownership signal (#951): a dead
        # card that is row-backed yet absent from the fresh base was forgotten by
        # another process and must not be written back; a never-saved instance still
        # gets its first save. Bounded by the instances this process ever sees.
        self._row_backed: set[str] = set(self._persisted)
        self._last_saved: dict[str, dict] | None = None
        # Serialize concurrent persists (startup-watch / stop / poll loop can interleave
        # on the event loop). The DB store's per-row prune raises StaleDataError when a
        # racing writer already removed the row; one lock makes each save atomic (mirrors
        # :attr:`HostedManager._persist_lock`).
        self._persist_lock = asyncio.Lock()

    # ----- persistence (state.json, D14) ----------------------------------

    def _persist_subset(self) -> dict[str, dict]:
        """Build the record to persist, keyed by instance id.

        The previously-persisted map overlaid with the currently-tracked instances — not
        the live instances alone: the comprehension below also admits a dead card that is
        not row-backed or is still present in ``_persisted``, and the overlay retains every
        earlier row (see the comment on the return).
        """
        live = {
            inst.instance_id: {
                "project_name": inst.project,
                "label": inst.label,
                "intentional_stop": inst.intentional_stop,
                "spawn_mode": inst.spawn_mode,
                "permission_mode": inst.permission_mode,
                "resume_mode": inst.resume_mode,
                "sandbox_mode": inst.sandbox_mode,
                # Only set when the name is NOT derivable from this row's instance_id
                # (#1241) — a keeper-only reattach that had to mint a fresh id. Persisted
                # so the recovery survives the next restart: by then the keeper may be
                # gone, and the row would otherwise be rebuilt with the derived name and
                # resume into a second worktree. None for every ordinary session.
                "worktree_name": inst.worktree_name,
                # Liveness identity (#1088/#1091): without these persisted, a fresh process
                # cannot tell which rows are live, and `rediscover` could only ever resolve
                # one instance per project via the (project-keyed) pointer walk. A dead
                # card's pair is carried from its ROW rather than from the card, which
                # holds None by design — see `_persisted_liveness` (#1115).
                **self._persisted_liveness(inst),
                # Always as a PAIR (#1178), never the pid alone: a keeper pid with a stale
                # or absent start time is what lets a DIFFERENT live keeper on that pid
                # answer for this one. All three are set and cleared together on the
                # instance — the epoch identifies the keeper and the boot-relative ticks
                # keep that identification from moving with the host clock (#1402).
                "keeper_pid": inst.keeper_pid,
                "keeper_proc_start": inst.keeper_proc_start,
                "keeper_start_ticks": inst.keeper_start_ticks,
            }
            for inst in self._instances.values()
            # #951 rounds 2+3: a dead card (STOPPED/CRASHED/ERROR) whose row this
            # process KNOWS reached the store (``_row_backed``) but is gone from the
            # freshly refreshed base was forgotten by another process — the card is
            # only a view of that row, and writing it back through this overlay would
            # undo the delete on every later persist. Row-backedness (not status) is
            # the ownership signal: a NEVER-saved instance (fresh spawn, or a spawn
            # that failed straight to ERROR) is not in the base either, but it isn't
            # row-backed, so it still gets its first save. A live STARTING/RUNNING
            # bridge is ground truth regardless and always persists.
            if (
                inst.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING)
                or inst.instance_id not in self._row_backed
                or inst.instance_id in self._persisted
            )
        }
        # Overlay live instances onto the previously-persisted map rather than
        # replacing it: an instance whose bridge isn't currently tracked — its bridge
        # died while Clauster was down, or rediscover hasn't (re)detected it — keeps
        # its saved label/modes/intentional_stop instead of being silently wiped on
        # the next save (which would later resume it with default modes). Live entries
        # win for tracked instances. An entry whose project directory was removed
        # lingers harmlessly (discovery is filesystem-based, so it's never consumed)
        # until state.json is reset.
        return {**self._persisted, **live}

    async def _refresh_persisted(self) -> bool:
        """Replace the persist merge-base with the CURRENT DB state (#949).

        ``_persisted`` is otherwise a snapshot from construction time, advanced only
        by this process's own saves — so a second clauster process (web app vs a
        headless CLI/MCP writer) mutating the shared store leaves it stale, and the
        next full-replace save here would resurrect rows the other process pruned
        and prune rows it added. Refreshing before merging keeps every writer's
        base current.

        Read failures keep the OLD base (:meth:`StateStore.load_strict` raises
        instead of degrading to ``{}``): replacing a known-good base with an empty
        one on a transient DB error would turn the next save into a mass prune —
        a stale cursor is the safe degrade, a data loss is not.
        """
        async with self._persist_lock:
            return await self._refresh_persisted_locked()

    async def _refresh_persisted_locked(self) -> bool:
        """Body of :meth:`_refresh_persisted`; caller must hold ``_persist_lock``.

        Returns whether the base was actually refreshed — ``False`` on a DB read
        error (old base kept). :meth:`_persist` aborts its save on ``False``.
        """
        try:
            loaded = await asyncio.to_thread(self._store.load_strict)
        except OSError as exc:
            _log.warning(
                "could not refresh persisted bridge state (keeping the previous snapshot): %s",
                exc,
            )
            return False
        self._persisted = loaded
        # UNION, never replace: an id we saved that is now missing from the store is
        # exactly the cross-process-deletion signal the persist subset keys on.
        self._row_backed |= set(loaded)
        return True

    async def _persist(self, *, drop: str | None = None) -> None:
        """Write the persisted subset off-loop, but only when it actually changed.

        Best-effort: the state store is non-authoritative, so a write failure (disk
        full, revoked perms — surfaced as :class:`OSError` per the store contract)
        degrades to a stale on-disk record, never a failed spawn/stop or a 500 on the
        dashboard poll. ``_last_saved`` is left unchanged on failure, which is what makes
        the next persist retry; ``_persisted`` has already been advanced to the freshly
        refreshed base by then, and the next attempt re-reads it anyway. Mirrors
        :meth:`HostedManager._persist`.

        Held under ``_persist_lock`` so interleaving callers can't race the store's
        per-row prune into a :class:`StaleDataError` (#471) — and, since #949, under
        the STORE-WIDE cross-process lock (:meth:`_store_flock`) for the whole
        refresh→merge→save: the save is a FULL-TABLE replace, and the per-project
        flocks don't exclude a different project's writer in another process, so an
        unserialized load→save could straddle its save and prune its fresh row.

        The refresh re-loads the merge base from the store so this save can't
        resurrect a row another clauster process pruned since our snapshot, or prune
        a row it added. A FAILED refresh aborts the attempt — writing a full replace
        from a known-stale base is exactly the prune hazard this exists to close; the
        next persist retries. ``drop`` (:meth:`SessionRunner.forget`, the one deletion
        path) excludes that instance id from the freshly refreshed base so the delete is
        atomic with the reload — and skips the no-change dedup, which was computed
        against OUR last write and can't know whether the store still holds the row.
        """
        async with self._persist_lock:
            async with self._store_flock():
                await self._persist_locked(drop=drop)

    async def _persist_locked(self, *, drop: str | None) -> None:
        """Body of :meth:`_persist`; caller holds ``_persist_lock`` + the store flock."""
        if not await self._refresh_persisted_locked():
            return
        if drop is not None:
            self._persisted = {k: v for k, v in self._persisted.items() if k != drop}
        subset = self._persist_subset()
        if drop is None and subset == self._last_saved:
            return
        try:
            await asyncio.to_thread(self._store.save, subset)
        except OSError as exc:
            _log.warning("could not persist bridge state: %s", exc)
            return
        self._last_saved = subset
        # Keep the merge base in sync with what's on disk so the next overlay builds
        # on the latest saved state (live modes that changed this round are retained).
        self._persisted = subset
        self._row_backed |= set(subset)  # everything just saved is now row-backed

    # ----- locks (acyclic order: spawn-lock -> bridge-flock -> persist-lock -> store-flock) --

    def _spawn_lock_for(self, name: str) -> asyncio.Lock:
        """Return the per-project spawn lock, creating it on first use.

        Synchronous (no ``await``) so the get-or-create itself can't race on the loop.
        """
        lock = self._spawn_locks.get(name)
        if lock is None:
            lock = self._spawn_locks[name] = asyncio.Lock()
        return lock

    @contextlib.asynccontextmanager
    async def _bridge_flock(self, name: str) -> AsyncIterator[None]:
        """Hold the cross-process per-project bridge-lifecycle lock (#949).

        The per-project ``_spawn_lock_for`` is an in-process ``asyncio.Lock`` — it
        never excludes a SECOND clauster process (the live web app vs a headless
        CLI ``clauster start``/``stop`` or MCP writer sharing the same config).
        This layers the deployment-wide ``flock`` (:func:`atomicio.cross_process_lock`,
        the same primitive the config/CLAUDE.md writers use) under it, keyed by the
        project directory so both processes derive the same lock file. Ordering is
        ALWAYS inproc-first, cross-process-second (the atomicio convention), so the
        two layers can't deadlock; the blocking ``flock`` is entered/exited in a
        worker thread so a contended lock never stalls the event loop. ``name`` may
        be a bare instance id on the :meth:`SessionRunner.forget` fallback path (record
        with no resolvable project) — the derived path need not exist, it is only a key.

        On Windows (no ``fcntl``) the flock layer yields without locking — behavior
        there is unchanged (in-process serialization only), exactly like the config
        writers; see :func:`atomicio.cross_process_lock`.
        """
        async with self._flock((self._config.projects_root / name).expanduser()):
            yield

    @contextlib.asynccontextmanager
    async def _store_flock(self) -> AsyncIterator[None]:
        """Hold the STORE-WIDE cross-process lock for a read-merge-replace save (#949).

        The per-project flock only excludes SAME-project writers, but
        :meth:`StateStore.save` is a full-table replace — without a store-wide lock,
        this process's refresh→save could straddle another process's save of a
        *different* project's row and prune it. Held only across :meth:`_persist`'s
        refresh+merge+save (milliseconds; the store is small). Ordering: always
        acquired AFTER any per-project flock (spawn/stop/forget/adopt persist inside
        their sections) and no holder ever acquires a per-project flock afterwards,
        so the two levels can't deadlock across processes.
        """
        async with self._flock((self._config.state_dir / "state-store").expanduser()):
            yield

    @contextlib.asynccontextmanager
    async def _flock(self, target: Path) -> AsyncIterator[None]:
        """Hold :func:`atomicio.cross_process_lock` on ``target``, event-loop-safely.

        The shared acquire behind :meth:`_bridge_flock` / :meth:`_store_flock`: the
        blocking ``flock`` is entered/exited in a worker thread, and the lock dir is
        pinned to THIS runner's deployment (``self._lock_dir``) so a later global
        ``configure_lock_dir`` for a different state dir can't redirect it.
        """
        cm = atomicio.cross_process_lock(target, lock_dir=self._lock_dir)
        acquire = asyncio.ensure_future(asyncio.to_thread(cm.__enter__))
        try:
            await asyncio.shield(acquire)
        except asyncio.CancelledError:
            # The worker thread may still complete the blocking flock AFTER this frame
            # is torn down (a cancelled to_thread doesn't stop the thread). Release the
            # lock the moment the acquisition lands instead of holding it until GC
            # reclaims the context manager — a cancelled caller must never pin the
            # cross-process lock.
            acquire.add_done_callback(_release_flock_if_acquired(cm))
            raise
        try:
            yield
        finally:
            await asyncio.to_thread(cm.__exit__, None, None, None)
