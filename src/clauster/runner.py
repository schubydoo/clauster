"""SessionRunner — spawn/stop/observe `claude remote-control` bridges (features 2-4).

One managed bridge per project; the instance id IS the project name. The
in-memory registry is the source of truth for bridges Clauster spawned this
process-lifetime; a startup pointer-walk re-detects bridges that are already
running (read-only — see ``pointers``).

Concurrency contract: the registry is mutated ONLY on the event loop. Blocking
work (Popen, os.kill, psutil, log reads, `claude agents --json`) runs in
``asyncio.to_thread`` and *returns* values that the loop applies back; callers
iterate over a ``list(...)`` snapshot, never the live dict.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from . import auth, bridge_log, inspector, metrics, pointers, procutil, redact
from .claude_cli import ClaudeNotFound, resolve_binary
from .config import (
    PERMISSION_MODES,
    RESUME_MODES,
    SPAWN_MODES,
    ClausterConfig,
    PermissionMode,
    ResumeMode,
    SpawnMode,
)
from .db.persistence import Persistence
from .discovery import (
    discover_projects_cached,
    invalidate_discovery_cache,
    is_valid_project_name,
)
from .models import (
    Attribution,
    BridgePointer,
    InstanceStatus,
    Project,
    RemoteControlInstance,
    WorkingSession,
)
from .notify import Notifier
from .recap import ensure_recap_hook_installed
from .trust import ensure_remote_control_enabled, is_trusted, trust_directory
from .usage import ProjectUsage, aggregate_project_usage_cached
from .webhooks import WebhookEmitter

_log = logging.getLogger("clauster.runner")


def _hash_session_ref(session_id: str | None, secret: bytes) -> str | None:
    """Return a stable, non-reversible correlation token for a starter session id.

    ``None`` in, ``None`` out. Otherwise a 16-hex-char (64-bit) HMAC-SHA256 prefix
    keyed by a per-deployment ``secret``: stable across an instance's lifecycle
    events so a webhook receiver can group them, but it never carries the
    bearer-equivalent ``session_<ULID>`` itself (which redaction strips from every
    other egress surface — see ``redact.py``). Keying with the secret (rather than a
    bare SHA-256) means a receiver can't even *verify* a guessed session id against
    the token without the secret — matching how ``session_<ULID>`` is treated as a
    bearer credential everywhere else.
    """
    if not session_id:
        return None
    return hmac.new(secret, session_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


class SpawnError(RuntimeError):
    """Raised when a bridge cannot be spawned (unknown project, untrusted, etc.)."""


class UnknownProject(SpawnError):
    """The named project does not exist under projects_root."""


class NotTrusted(SpawnError):
    """The project directory has not accepted Claude's workspace-trust dialog."""


class InvalidSpawnOption(SpawnError):
    """Bad spawn_mode/permission_mode value, or worktree requested for a non-git project."""


class PermissionModeNotAllowed(SpawnError):
    """bypassPermissions requested for a project whose config ceiling forbids it."""


class CapacityExceeded(SpawnError):
    """A new bridge would exceed instance_defaults.max_bridges (clauster-enforced cap)."""


class InstanceStillLive(RuntimeError):
    """Raised when forget() is asked to drop a bridge that is still STARTING/RUNNING.

    Not a SpawnError: forget is a lifecycle op, not a spawn, and the caller maps this
    to 409 (Stop it first) rather than the 4xx the spawn errors map to.
    """


class AdoptionUnavailable(RuntimeError):
    """Raised when adopt() can't take over an external session.

    The project has no live *standard* bridge to adopt — it ended between the poll
    that surfaced it and the click, or it's a pty (flag-form) bridge, which is unsafe
    to adopt (no recoverable keeper, terminal-coupled Stop). A lifecycle op, not a
    spawn; the caller maps it to 409.
    """


# How long to wait for a freshly-spawned bridge to reach its poll loop.
_READY_TIMEOUT = 15.0
_READY_POLL_INTERVAL = 0.25
# Cadence at which the post-spawn startup-watch re-reads the bridge log to detect
# a (late) environment registration or a stuck-but-alive bridge.
_STARTUP_WATCH_INTERVAL = 2.0
# Slack when matching a keeper sidecar's recorded proc-start against the pointer's
# (the two epochs are derived independently). Mirrors procutil.is_live_bridge's
# default tolerance so the two PID-reuse checks can't disagree.
_PROC_START_TOLERANCE = 2.0


class SessionRunner:
    """Owns the lifecycle of managed bridges: spawn, resume, stop, and status polling."""

    def __init__(
        self,
        config: ClausterConfig,
        claude_json: Path | None = None,
        persistence: Persistence | None = None,
    ) -> None:
        """Bind the runner to config and the ``~/.claude.json`` trust file.

        Builds (or reuses) the :class:`Persistence` container — engine + migrated,
        imported database. A fresh one runs the fail-closed startup (migrate to
        head, then a one-time legacy-JSON import); the app passes its own so the
        whole process shares a single engine. ``persistence`` is exposed so the app
        can reuse it for the hosted-session store.
        """
        self._config = config
        self._binary = config.claude.binary
        self._claude_json = claude_json or Path("~/.claude.json").expanduser()
        self._log_dir = (config.state_dir / "logs").expanduser()
        # Monotonic spawn counter → unique log filenames even for two same-ms spawns.
        self._log_seq = 0
        self._instances: dict[str, RemoteControlInstance] = {}
        # Per-project bridge-crash tally since process start, exposed as the
        # clauster_bridge_crashes_total counter (#352) — a crash that resumes between
        # scrapes still leaves a trace, unlike the current-status gauge.
        self._crash_counts: dict[str, int] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._sessions: list[WorkingSession] = []
        self._poll_task: asyncio.Task | None = None
        # Server-side per-project metrics snapshot (#354): the metrics task refreshes it
        # off the request path so /api/projects/{name}/metrics + the batch read + the
        # /metrics scrape all serve from the last sample at O(1), no per-request thread.
        self._metrics_cache: dict[str, dict] = {}
        self._metrics_task: asyncio.Task | None = None
        # Per-spawn background tasks that watch a STARTING bridge until it either
        # registers an environment (-> RUNNING) or proves stuck (-> ERROR).
        self._startup_watches: dict[str, asyncio.Task] = {}
        # Per-project locks serializing concurrent spawns of the SAME project (see
        # ``spawn``). One bridge per project is an invariant; without this, two
        # near-simultaneous spawns race across the awaits in ``_spawn_locked``.
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        # Mark remote control as acknowledged once, before the first spawn.
        self._rc_setting_ensured = False
        # Install the resume-recap SessionStart hook once, before the first spawn.
        self._recap_hook_ensured = False
        # ~/.claude/settings.json sits beside the ~/.claude.json we honor for trust.
        self._settings_json = self._claude_json.parent / ".claude" / "settings.json"
        # ~/.claude/projects holds the per-session transcripts the cost/token rollup
        # reads (#363 terminal-event snapshot). Anchored to the same claude home as
        # the trust file, so a HOME-isolated test points it at its tmp dir, not the
        # host's real transcripts.
        self._claude_projects_dir = self._claude_json.parent / ".claude" / "projects"
        # Persistence of label / intentional_stop / spawn_mode (D14), now DB-backed
        # (#362) behind the same load()/save() dict contract the JSON store had.
        self._persistence = persistence or Persistence(config.state_dir, config.database_url)
        self._state = self._persistence.state_store()
        # Append-only session lifecycle / event history (#363). Records spawn/ready/
        # end/crash transitions for the Projects-zone "last used" sort (#298) and the
        # pty resume picker (#303). Best-effort and fail-closed — a lost history row
        # never affects a bridge's lifecycle.
        self._history = self._persistence.session_history_store()
        self._persisted: dict[str, dict] = self._state.load()
        self._last_saved: dict[str, dict] | None = None
        # Serialize concurrent persists (startup-watch / stop / poll loop can interleave
        # on the event loop). The DB store's per-row prune raises StaleDataError when a
        # racing writer already removed the row; one lock makes each save atomic (mirrors
        # :attr:`HostedManager._persist_lock`).
        self._persist_lock = asyncio.Lock()
        # Best-effort outbound notifications (Apprise; optional extra). No-op unless
        # enabled + configured + Apprise installed. Fire-and-forget crash alerts are
        # tracked here so the tasks aren't garbage-collected mid-flight.
        self._notifier = Notifier(config.notifications)
        self._notify_tasks: set[asyncio.Task] = set()
        # Outbound lifecycle webhooks (#371). Fail-open and fire-and-forget, sharing the
        # GC-safety task set above; no-op unless enabled with a usable url.
        self._webhooks = WebhookEmitter(config.webhooks)
        # Lazily-loaded per-deployment key for the webhook session_ref HMAC (#408).
        # Loaded on first webhook emit, not at construction, so a bad
        # CLAUSTER_SESSION_SECRET can't break runner construction or the bridge
        # lifecycle (webhooks are fail-open by design).
        self._session_ref_secret: bytes | None = None

    # ----- read API -------------------------------------------------------

    @property
    def claude_json(self) -> Path:
        """The claude.json whose trusted-dirs this runner honors (for trust checks)."""
        return self._claude_json

    @property
    def persistence(self) -> Persistence:
        """The shared persistence container (engine + DB-backed stores)."""
        return self._persistence

    def list_instances(self) -> list[RemoteControlInstance]:
        """Return a snapshot list of all managed bridge instances."""
        return list(self._instances.values())

    def crash_counts(self) -> dict[str, int]:
        """Return a copy of the per-project bridge-crash tally since process start (#352)."""
        return dict(self._crash_counts)

    def metrics_snapshot(self, name: str) -> dict | None:
        """Return a copy of the last cached resource sample for ``name``, or None (#354)."""
        sample = self._metrics_cache.get(name)
        return dict(sample) if sample is not None else None

    def metrics_snapshots(self) -> dict[str, dict]:
        """Return a copy of the per-project cached resource samples (#354 batch read)."""
        return dict(self._metrics_cache)

    async def _sample_one_bridge(self, inst: RemoteControlInstance) -> dict | None:
        """Sample a single running bridge's resource tree, or None to skip it (#407).

        The whole per-bridge cost — the PID create-time reuse guard and the blocking
        ``metrics.sample_tree`` walk — is offloaded via ``asyncio.to_thread`` here so a
        caller can ``gather`` the bridges and pay ~max-per-bridge wall-time instead of the
        sum. Returns the sample dict on success, or ``None`` when the bridge is not running,
        has no pid, fails the create-time guard (recycled pid), or the sampler returns
        nothing. Exceptions propagate to the gather caller, which isolates them per bridge.
        """
        pid = inst.bridge_pid
        if inst.status is not InstanceStatus.RUNNING or pid is None:
            return None
        start = inst.bridge_proc_start
        if start is not None:
            cur = await asyncio.to_thread(procutil.proc_create_time, pid)
            # bridge_proc_start is OUR OWN proc_create_time() measurement of this
            # same pid (set at spawn), so a live match is near-exact — use the tight
            # _EXACT_PROC_START_TOLERANCE that procutil.is_live_process applies to a
            # self-measured float, not the loose pointer-jiffies slack. The loose
            # 2.0s left a wide window in which a recycled pid that started up to two
            # seconds later still passed and got mis-attributed to the bridge.
            if cur is None or abs(cur - start) > procutil._EXACT_PROC_START_TOLERANCE:
                return None  # PID reused onto an unrelated process — skip
        return await asyncio.to_thread(
            metrics.sample_tree,
            pid,
            interval=self._config.metrics.sample_interval_seconds,
            normalize_cpu=self._config.metrics.normalize_cpu,
        )

    async def _refresh_metrics_cache(self) -> None:
        """Re-sample every running bridge into ``_metrics_cache`` (#354, #407).

        Samples all bridges CONCURRENTLY — each per-bridge ``to_thread`` is launched
        together and ``gather``ed — so refresh wall-time is ~max-per-bridge (capped by the
        default ``asyncio`` thread-pool, ``min(32, cpu+4)``; past that the samples batch),
        not the sum
        (#407; previously serial, which is why a high bridge count outran ``poll_seconds``
        and triggered ``_warn_if_refresh_slow``). The PID create-time guard mirrors the
        per-request path so a recycled PID is never attributed to a bridge. Each bridge is
        isolated via ``return_exceptions`` — one failing sampler is logged and dropped, never
        the rest. The cache is replaced wholesale, so a stopped/crashed bridge's stale sample
        drops out.
        """
        targets = list(self._instances.values())
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
                fresh[inst.project] = sample
        self._metrics_cache = fresh

    def _warn_if_refresh_slow(self, elapsed: float) -> None:
        """Warn when a refresh outran the poll period (samples are going stale, #354).

        If sampling N bridges (each ~sample_interval_seconds) outgrows poll_seconds, the
        effective refresh rate degrades silently — surface it instead.
        """
        poll = self._config.metrics.poll_seconds
        if elapsed > poll:
            _log.warning(
                "metrics refresh took %.1fs, exceeding poll_seconds=%.1f — samples may "
                "be stale; reduce running bridges or raise metrics.poll_seconds",
                elapsed,
                poll,
            )

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

    def get_instance(self, instance_id: str) -> RemoteControlInstance | None:
        """Return the instance with this id, or None if unknown."""
        return self._instances.get(instance_id)

    def running_count(self) -> int:
        """Count instances currently in the RUNNING state."""
        return sum(1 for i in self._instances.values() if i.status is InstanceStatus.RUNNING)

    def external_sessions_by_project(self) -> dict[str, list[WorkingSession]]:
        """Group EXTERNAL working sessions by the project at their cwd (bug #4).

        Covers sessions not tied to a managed bridge, keyed by project name.

        Lets the UI surface "external session active" for a project Clauster
        isn't managing — e.g. a bridge started from the terminal or Claude
        Desktop, which the pointer-walk misses but the ``agents --json``
        cross-check (computed in :meth:`poll_once`) already sees.
        """
        by_path = {p.path.resolve(): name for name, p in self._discovered().items()}
        out: dict[str, list[WorkingSession]] = {}
        for session in self._sessions:
            if session.attribution is not Attribution.EXTERNAL:
                continue
            name = by_path.get(session.cwd.resolve())
            if name is not None:
                out.setdefault(name, []).append(session)
        return out

    # ----- persistence (state.json, D14) ----------------------------------

    def _persist_subset(self) -> dict[str, dict]:
        live = {
            name: {
                "label": inst.label,
                "intentional_stop": inst.intentional_stop,
                "spawn_mode": inst.spawn_mode,
                "permission_mode": inst.permission_mode,
                "resume_mode": inst.resume_mode,
            }
            for name, inst in self._instances.items()
        }
        # Overlay live instances onto the previously-persisted map rather than
        # replacing it: a project whose bridge isn't currently tracked — its bridge
        # died while Clauster was down, or rediscover hasn't (re)detected it — keeps
        # its saved label/modes/intentional_stop instead of being silently wiped on
        # the next save (which would later resume it with default modes). Live entries
        # win for tracked projects. An entry whose project directory was removed
        # lingers harmlessly (discovery is filesystem-based, so it's never consumed)
        # until state.json is reset.
        return {**self._persisted, **live}

    async def _persist(self) -> None:
        """Write the persisted subset off-loop, but only when it actually changed.

        Best-effort: the state store is non-authoritative, so a write failure (disk
        full, revoked perms — surfaced as :class:`OSError` per the store contract)
        degrades to a stale on-disk record, never a failed spawn/stop or a 500 on the
        dashboard poll. ``_last_saved``/``_persisted`` are left unchanged on failure so
        the next persist retries (mirrors :meth:`HostedManager._persist`).

        Held under ``_persist_lock`` so interleaving callers can't race the store's
        per-row prune into a :class:`StaleDataError` (#471).
        """
        async with self._persist_lock:
            subset = self._persist_subset()
            if subset == self._last_saved:
                return
            try:
                await asyncio.to_thread(self._state.save, subset)
            except OSError as exc:
                _log.warning("could not persist bridge state: %s", exc)
                return
            self._last_saved = subset
            # Keep the merge base in sync with what's on disk so the next overlay builds
            # on the latest saved state (live modes that changed this round are retained).
            self._persisted = subset

    # ----- discovery helpers ---------------------------------------------

    def _discovered(self) -> dict[str, Project]:
        # Cached (short TTL + mtime-invalidated): this runs on every poll_once and
        # many lookup paths. A trust write invalidates the cache explicitly
        # (trust_project), so the post-write re-read still reflects the new state.
        return {
            p.name: p
            for p in discover_projects_cached(self._config.projects_root, self._claude_json)
        }

    def _resolve_project(self, name: str) -> Project:
        # Path-traversal defense (spec §9): only ever spawn a discovered project.
        if not is_valid_project_name(name):
            raise UnknownProject(f"invalid project name: {name!r}")
        proj = self._discovered().get(name)
        if proj is None:
            raise UnknownProject(f"no such project under projects_root: {name!r}")
        return proj

    # ----- trust ----------------------------------------------------------

    async def trust_project(self, name: str) -> Project:
        """Accept the workspace-trust dialog for ``name`` and return its refreshed state."""
        proj = self._resolve_project(name)
        await asyncio.to_thread(trust_directory, proj.path, self._claude_json)
        # The trust write mutates ~/.claude.json; drop the discovery cache so the
        # re-read below (and the next poll) reflect the new trust state immediately,
        # not after a coarse-mtime/TTL delay.
        invalidate_discovery_cache()
        # Re-read so the returned Project reflects the new trust state.
        return self._discovered().get(name, proj)

    # ----- spawn ----------------------------------------------------------

    async def spawn(
        self,
        name: str,
        *,
        spawn_mode: SpawnMode | None = None,
        permission_mode: PermissionMode | None = None,
        resume_mode: ResumeMode | None = None,
        resume: bool = False,
    ) -> RemoteControlInstance:
        """Spawn a new bridge for ``name`` (returning the existing one if already up).

        Validates spawn/permission modes, ensures remote control + the recap hook are
        set up, launches the process, and watches it until it reaches RUNNING or ERROR.

        ``resume_mode`` ("standard"/"pty") picks the launch mode for *this* bridge,
        overriding the ``claude.resume_mode`` config default (the per-launch picker).
        When the effective mode is ``"pty"`` (POSIX only), the bridge is the
        ``claude --remote-control`` flag form run under a :mod:`clauster.pty_keeper`
        for true conversation resume; ``resume=True`` (set by :meth:`resume`) adds
        ``--continue`` so the restarted session restores its prior context. The mode
        is fixed at first launch and recorded on the instance, so a resume always
        keeps it (see :meth:`_is_pty_mode`).

        Concurrent spawns of the *same* project are serialized by a per-project lock:
        a double-click, retry, or second browser tab must not both pass the
        idempotency check and launch two bridges, because the second would clobber
        the first in ``self._instances``/``self._procs`` and orphan an untracked,
        unreapable process. Different projects still spawn concurrently.
        """
        async with self._spawn_lock_for(name):
            return await self._spawn_locked(
                name,
                spawn_mode=spawn_mode,
                permission_mode=permission_mode,
                resume_mode=resume_mode,
                resume=resume,
            )

    def _spawn_lock_for(self, name: str) -> asyncio.Lock:
        """Return the per-project spawn lock, creating it on first use.

        Synchronous (no ``await``) so the get-or-create itself can't race on the loop.
        """
        lock = self._spawn_locks.get(name)
        if lock is None:
            lock = self._spawn_locks[name] = asyncio.Lock()
        return lock

    async def _spawn_locked(
        self,
        name: str,
        *,
        spawn_mode: SpawnMode | None = None,
        permission_mode: PermissionMode | None = None,
        resume_mode: ResumeMode | None = None,
        resume: bool = False,
    ) -> RemoteControlInstance:
        # Body of spawn(), always run under the per-project lock (see spawn()).
        existing = self._instances.get(name)
        if existing is not None and existing.status in (
            InstanceStatus.STARTING,
            InstanceStatus.RUNNING,
        ):
            return existing

        proj = self._resolve_project(name)
        defaults = self._config.instance_defaults
        spawn_mode = spawn_mode or defaults.spawn_mode
        permission_mode = permission_mode or defaults.permission_mode
        self._validate_spawn_options(proj, spawn_mode, permission_mode, resume_mode)

        if not await asyncio.to_thread(is_trusted, proj.path, self._claude_json):
            raise NotTrusted(
                f"directory not trusted: {proj.path}. Use the Trust action before starting."
            )

        if self._config.claude.auto_enable_remote_control and not self._rc_setting_ensured:
            try:
                changed = await asyncio.to_thread(ensure_remote_control_enabled, self._claude_json)
                if changed:
                    _log.info(
                        "marked remote control acknowledged in %s so the bridge skips the "
                        "interactive enable prompt",
                        self._claude_json,
                    )
            except OSError as exc:
                # Best-effort: if we can't write the flag the bridge may hang on the
                # prompt, but the startup-watch surfaces that honestly as ERROR rather
                # than a false RUNNING — so don't fail the spawn over it.
                _log.warning(
                    "could not pre-enable remote control in %s: %s", self._claude_json, exc
                )
            self._rc_setting_ensured = True

        if self._config.claude.resume_recap and not self._recap_hook_ensured:
            try:
                changed = await asyncio.to_thread(ensure_recap_hook_installed, self._settings_json)
                if changed:
                    _log.info(
                        "installed the resume-recap SessionStart hook in %s so a restarted "
                        "bridge gets its prior conversation recapped into context",
                        self._settings_json,
                    )
            except OSError as exc:
                # Best-effort, same as the remote-control flag: a failure here only
                # means a restart won't be recapped, not that the bridge can't run.
                _log.warning(
                    "could not install resume-recap hook in %s: %s", self._settings_json, exc
                )
            self._recap_hook_ensured = True

        # Enforce the optional clauster-side concurrent-bridge cap. Past the idempotency
        # early-return, this project is NOT currently live, so every live instance is a
        # different bridge. Fail closed BEFORE any per-spawn side effect (file/process).
        max_bridges = defaults.max_bridges
        if max_bridges is not None:
            live = sum(
                1
                for other, inst in self._instances.items()
                if other != name
                and inst.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING)
            )
            if live >= max_bridges:
                raise CapacityExceeded(
                    f"max_bridges={max_bridges} reached ({live} live); "
                    "stop a bridge before starting another"
                )

        # Prune old bridge-log sets per the retention policy before creating this
        # spawn's set (so the new files are never a deletion candidate). Off the loop;
        # best-effort — a retention error must never block a spawn. Snapshot the live
        # instances' protected set keys HERE on the loop — reading self._instances from
        # the worker thread could race a concurrent spawn's write to it.
        protected = {
            self._log_set_key(Path(p).name)
            for inst in self._instances.values()
            for p in (inst.bridge_debug_log_path, inst.bridge_raw_log_path)
            if p is not None
        }
        await asyncio.to_thread(self._prune_logs, protected)
        log_path = self._unique_log_path(name)
        raw_path = self._raw_log_path_for(log_path)
        # Create the verbatim parse-source 0600 from the first inode — UNCONDITIONALLY:
        # when on-disk redaction is off, raw_path == log_path and IS the verbatim debug
        # log (it holds the unredacted session URL + bridge output), so it must be
        # owner-only too. os.open(O_CREAT | O_EXCL, 0o600), NOT touch()+chmod: touch()
        # honours the umask, so the verbatim session URL would be briefly group/world-
        # readable in the window before chmod ran (and a reader's open fd survives the
        # chmod). O_EXCL also refuses a pre-planted symlink at this per-spawn-unique path;
        # the bridge's --debug-file open then appends to this existing 0600 inode.
        os.close(os.open(raw_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        instance = RemoteControlInstance(
            project=name,
            label=name,
            status=InstanceStatus.STARTING,
            bridge_debug_log_path=log_path,
            bridge_raw_log_path=raw_path,
            started_at=datetime.now(UTC),
            # Validated above (_validate_spawn_options raises on a bad value), so
            # these str inputs are known-good members of the Literal types.
            spawn_mode=cast(SpawnMode, spawn_mode),
            permission_mode=cast(PermissionMode, permission_mode),
        )
        self._instances[name] = instance  # on the loop

        # A bridge's resume_mode is fixed at first launch and recorded on the
        # instance. An explicit resume_mode (the per-launch picker) wins for a
        # fresh start; otherwise a resume honors the prior instance's mode and a
        # brand-new bridge falls back to the config default — so stop() and
        # resume() can't disagree (see _is_pty_mode).
        prior = existing if resume else None
        instance.resume_mode = (
            "pty" if self._is_pty_mode(prior, requested=resume_mode) else "standard"
        )
        # One spawn-event chokepoint for both modes: the instance is registered, STARTING,
        # and its resume_mode is now resolved. A "ready" follows iff it reaches RUNNING.
        self._emit_lifecycle("spawn", instance)
        if instance.resume_mode == "pty":
            return await self._spawn_pty(instance, proj, name, log_path, permission_mode, resume)

        try:
            proc = await asyncio.to_thread(
                self._popen, proj.path, log_path, name, spawn_mode, permission_mode, raw_path
            )
        except (OSError, ClaudeNotFound) as exc:
            # Binary unresolvable / not executable: fail the instance cleanly
            # instead of leaving it stuck in STARTING.
            _log.warning("spawn of %s failed to launch: %s", name, exc)
            instance.status = InstanceStatus.ERROR
            await self._persist()
            return instance
        self._procs[name] = proc
        instance.bridge_pid = proc.pid
        instance.bridge_proc_start = await asyncio.to_thread(procutil.proc_create_time, proc.pid)

        markers = await asyncio.to_thread(self._await_ready, raw_path, proc)
        self._apply_markers(instance, markers, proc)
        await asyncio.to_thread(self._flush_redacted_mirror, instance)
        await self._post_spawn_enrich(instance, proj.path)
        await self._persist()
        # A bridge still STARTING after the synchronous readiness wait may yet
        # register (slow start) or may be alive-but-stuck (e.g. it couldn't
        # authenticate to the controller). Watch it off the request path so it is
        # only ever promoted to RUNNING once it actually registers an environment.
        if instance.status is InstanceStatus.STARTING:
            self._start_startup_watch(name)
        return instance

    async def resume(self, name: str) -> RemoteControlInstance:
        """Re-spawn a stopped/crashed bridge, reconnecting to its prior session.

        Re-running ``claude remote-control`` in the same cwd reconnects to the
        existing environment + session (the bridge-pointer.json the prior run
        left behind drives it — empirically confirmed). We reuse the stopped
        instance's stored ``spawn_mode``/``permission_mode`` so the resume keeps
        the same permission mode (a *fresh* bare start would drop back to the
        default 'ask'). The session id, which a reconnecting bridge does NOT
        re-log, is recovered from the pointer by :meth:`spawn`'s enrich step.
        """
        existing = self._instances.get(name)
        if existing is None:
            raise UnknownProject(f"no managed instance to resume: {name!r}")
        return await self.spawn(
            name,
            spawn_mode=existing.spawn_mode,
            permission_mode=existing.permission_mode,
            # In pty mode this adds --continue so the flag-form bridge restores the
            # prior conversation; the standard subcommand path ignores it.
            resume=True,
        )

    def _validate_spawn_options(
        self,
        proj: Project,
        spawn_mode: str,
        permission_mode: str,
        resume_mode: str | None = None,
    ) -> None:
        if spawn_mode not in SPAWN_MODES:
            raise InvalidSpawnOption(
                f"invalid spawn_mode {spawn_mode!r}; expected one of {SPAWN_MODES}"
            )
        if permission_mode not in PERMISSION_MODES:
            raise InvalidSpawnOption(
                f"invalid permission_mode {permission_mode!r}; expected one of {PERMISSION_MODES}"
            )
        if resume_mode is not None and resume_mode not in RESUME_MODES:
            raise InvalidSpawnOption(
                f"invalid resume_mode {resume_mode!r}; expected one of {RESUME_MODES}"
            )
        if spawn_mode == "worktree" and not proj.is_git_repo:
            raise InvalidSpawnOption(
                f"worktree mode requires a git repository: {proj.name!r} is not one"
            )
        if self._config.bypass_denied(proj.name, permission_mode):
            raise PermissionModeNotAllowed(
                f"bypassPermissions is not enabled for project {proj.name!r}. Set "
                "projects.<name>.allow_bypass_permissions: true in clauster.yml first."
            )

    def _unique_log_path(self, name: str) -> Path:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        # Unique per spawn so the parser never reads a previous run's markers — AND so the
        # 0600 O_EXCL pre-create can't FileExistsError. The millisecond timestamp alone
        # collides for two same-project spawns in the same ms (and a retry on it wouldn't
        # advance the clock), so a monotonic per-runner counter guarantees a fresh path
        # every call. _unique_log_path only runs on the event loop, so the bump is safe.
        self._log_seq += 1
        return self._log_dir / f"{name}-{int(time.time() * 1000)}-{self._log_seq}.log"

    # Suffixes of one spawn's log "set" — all share the `<name>-<ms>-<seq>` stem.
    # Longest-match-first so `.keeper.log` / `.raw.log` strip whole, not just `.log`.
    _LOG_SET_SUFFIXES = (".raw.log", ".stderr.log", ".keeper.json", ".keeper.log", ".log")

    @classmethod
    def _log_set_key(cls, filename: str) -> str:
        """Map a log filename to its spawn-set key (the shared `<name>-<ms>-<seq>` stem)."""
        for suf in cls._LOG_SET_SUFFIXES:
            if filename.endswith(suf):
                return filename[: -len(suf)]
        return filename

    def _prune_logs(self, protected: set[str]) -> None:
        """Apply the ``logs.retention_*`` policy to the bridge-log dir (best-effort).

        Groups files into per-spawn sets (a ``.log`` and its ``.raw.log`` /
        ``.stderr.log`` / ``.keeper.json`` / ``.keeper.log`` siblings share a stem) and
        deletes whole sets that exceed the configured age / count / total-size limits,
        oldest first. ``protected`` (the set keys of live instances' logs, snapshotted on
        the event loop by the caller) is never pruned. A ``0`` limit disables that
        dimension. Runs off the event loop (via ``to_thread``) on each spawn; a transient
        FS error is logged and never aborts the spawn.
        """
        logs = self._config.logs
        max_age_days, max_files, max_total_mb = (
            logs.retention_max_age_days,
            logs.retention_max_files,
            logs.retention_max_total_mb,
        )
        if not (max_age_days or max_files or max_total_mb):
            return
        try:
            entries = [p for p in self._log_dir.iterdir() if p.is_file()]
        except OSError as exc:
            _log.warning("bridge-log retention: could not list %s: %s", self._log_dir, exc)
            return

        sets: dict[str, list[Path]] = {}
        for p in entries:
            sets.setdefault(self._log_set_key(p.name), []).append(p)

        def _stat(paths: list[Path]) -> tuple[float, int]:
            mtime, size = 0.0, 0
            for p in paths:
                try:
                    st = p.stat()
                except OSError:  # pragma: no cover - TOCTOU only; is_file() already stat-filtered
                    continue
                mtime, size = max(mtime, st.st_mtime), size + st.st_size
            return mtime, size

        info = {k: _stat(v) for k, v in sets.items()}
        ordered = sorted(sets, key=lambda k: info[k][0], reverse=True)  # newest first
        doomed: set[str] = set()
        if max_age_days:
            cutoff = time.time() - max_age_days * 86400
            # A set with no datable file (mtime stays 0.0 — every file failed to stat) is
            # never age-pruned: we don't delete what we can't date.
            doomed.update(k for k in ordered if info[k][0] and info[k][0] < cutoff)
        if max_files:
            survivors = [k for k in ordered if k not in doomed]
            doomed.update(survivors[max_files:])
        if max_total_mb:
            survivors = [k for k in ordered if k not in doomed]  # newest first
            total = sum(info[k][1] for k in survivors)
            for k in reversed(survivors):  # oldest first
                if total <= max_total_mb * 1024 * 1024:
                    break
                doomed.add(k)
                total -= info[k][1]

        doomed -= protected  # keep live bridges' log sets regardless of age/count/size
        for k in doomed:
            for p in sets[k]:
                try:
                    p.unlink()
                except OSError as exc:
                    _log.debug("bridge-log retention: could not delete %s: %s", p, exc)
        if doomed:
            _log.info("bridge-log retention pruned %d log set(s)", len(doomed))

    def _raw_log_path_for(self, log_path: Path) -> Path:
        """Return the verbatim parse-source the bridge writes its ``--debug-file`` to.

        With ``logs.redact_session_url`` false (default) this **is** ``log_path``: a
        single verbatim debug log, exactly as before. When true the bridge writes to a
        private ``0600`` sibling instead, which Clauster parses for readiness markers +
        the session-URL deep link, while ``log_path`` (the public, ops-facing bridge
        log) becomes a redacted mirror of it (see :meth:`_flush_redacted_mirror`).
        """
        if not self._config.logs.redact_session_url:
            return log_path
        return log_path.with_name(log_path.stem + ".raw.log")

    def _flush_redacted_mirror(self, instance: RemoteControlInstance) -> None:
        """Refresh the public bridge log as a redacted copy of the private raw log.

        No-op unless ``logs.redact_session_url`` redirected the bridge to a separate raw
        file. Re-redacts the whole raw file and overwrites the public log each call —
        simple and correct under rotation/truncation (the debug log is bounded by
        ``logs.bridge_log_max_size_mb``). Best-effort: a transient FS error must never
        break the poll loop or a spawn, only delay the at-rest redaction by a tick.
        """
        raw = instance.bridge_raw_log_path
        public = instance.bridge_debug_log_path
        if raw is None or public is None or raw == public:
            return
        try:
            text = raw.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return  # bridge hasn't written yet; nothing to mirror
        except OSError as exc:
            _log.warning("could not read raw bridge log for %s: %s", instance.project, exc)
            return
        try:
            public.write_text(redact.redact_for_disk(text), encoding="utf-8")
        except OSError as exc:
            _log.warning("could not write redacted bridge log for %s: %s", instance.project, exc)

    def _build_cmd(
        self, log_path: Path, name: str, spawn_mode: SpawnMode, permission_mode: PermissionMode
    ) -> list[str]:
        """Build the `claude remote-control` argv. Pure (no side effects) so it's unit-testable."""
        defaults = self._config.instance_defaults
        cmd = [
            self._binary,
            "remote-control",
            "--name",
            name,
            "--debug-file",
            str(log_path),
            "--spawn",
            spawn_mode,
            "--permission-mode",
            permission_mode,
        ]
        # Brand auto-generated session names when configured. Multi-session modes only
        # (same-dir/worktree) — `session` is single-session, so the prefix is out of scope.
        if defaults.session_name_prefix and spawn_mode in ("same-dir", "worktree"):
            cmd += ["--remote-control-session-name-prefix", defaults.session_name_prefix]
        # --capacity caps concurrent sessions inside a same-dir/worktree bridge; it does
        # not apply to the single-session `session` spawn mode, so don't pass it there.
        if spawn_mode in ("same-dir", "worktree"):
            cmd += ["--capacity", str(defaults.capacity)]
        return cmd

    @staticmethod
    def _stderr_path_for(log_path: Path) -> Path:
        """Sibling of the --debug-file that captures the bridge's stdout+stderr.

        The bridge writes startup *failures* (e.g. ``Error: Workspace not
        trusted``, controller-auth errors) to its stderr, NOT the --debug-file.
        Routing both streams here — instead of DEVNULL — lets a failed spawn
        surface a real reason instead of a bare timeout.
        """
        return log_path.with_name(log_path.stem + ".stderr.log")

    def _bridge_env_overlay(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Build the config-driven env overlay (``claude.path_append`` / ``claude.env``).

        Returns an ``extra`` mapping for :func:`procutil.child_env`, merging the
        operator's ``claude.env`` and a ``PATH`` extended by ``claude.path_append``
        with any caller ``extra`` (e.g. resume-recap flags). Passing it through
        ``child_env`` re-scrubs Clauster secrets, so config can never re-introduce
        a scrubbed credential name.
        """
        claude = self._config.claude
        return procutil.bridge_env_overlay(
            path_append=claude.path_append, env=claude.env, extra=extra
        )

    def _popen(
        self,
        cwd: Path,
        log_path: Path,
        name: str,
        spawn_mode: SpawnMode,
        permission_mode: PermissionMode,
        debug_path: Path | None = None,
    ) -> subprocess.Popen:
        # The bridge writes its --debug-file to `debug_path` (the private raw parse-
        # source when on-disk redaction is on); the captured-stderr sibling stays keyed
        # off the public `log_path`. They coincide when redaction is off.
        cmd = self._build_cmd(debug_path or log_path, name, spawn_mode, permission_mode)
        # Exec the RESOLVED absolute path, not the bare configured name: Windows
        # CreateProcess only auto-appends .exe (never the .cmd/.ps1 shim npm installs
        # for `claude`), so a bare name that the version probe resolves via
        # shutil.which would fail to spawn here. Also pins the binary we validated.
        cmd[0] = resolve_binary(cmd[0])
        # Always build the child env from the SCRUBBED base (procutil.child_env)
        # so the bridge — which runs project-controlled code — can never read a
        # Clauster secret (session signing key, password hash) from its own
        # os.environ. When resume-recap is enabled, flag it in the bridge's env:
        # the detached bridge's child sessions inherit this, and the SessionStart
        # hook (wired into ~/.claude/settings.json) acts only when it is set — so
        # the recap never fires for the user's non-Clauster sessions sharing that
        # config. The recap flags overlay AFTER scrubbing, so they are never lost.
        recap_env: dict[str, str] = {}
        if self._config.claude.resume_recap:
            recap_env = {
                "CLAUSTER_RESUME_RECAP": "1",
                "CLAUSTER_RESUME_RECAP_MAX_CHARS": str(self._config.claude.resume_recap_max_chars),
            }
        # Overlay the operator's PATH/env extensions (claude.path_append/claude.env)
        # on top of the recap flags; child_env re-scrubs secrets so config can never
        # re-introduce a scrubbed credential name.
        popen_env = procutil.child_env(self._bridge_env_overlay(recap_env))
        # Capture stdout+stderr to a file so a failed start leaves a diagnosable
        # reason behind (the bridge logs the *why* there, not to --debug-file).
        # The detached child inherits its own dup of the fd, so the parent closes
        # its copy right after spawn; the child keeps writing.
        err_fh = self._stderr_path_for(log_path).open("wb")
        try:
            # Detach the bridge into its own session/group so it survives a clauster
            # restart and a SIGINT to clauster never propagates to it. On Windows,
            # CREATE_NEW_PROCESS_GROUP additionally makes the bridge addressable by a
            # CTRL_BREAK_EVENT for graceful stop (POSIX uses start_new_session); stdin
            # is detached so a wrapping cmd.exe never blocks on an interactive prompt.
            if sys.platform == "win32":
                return subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=err_fh,
                    stderr=subprocess.STDOUT,
                    env=popen_env,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            return subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=err_fh,
                stderr=subprocess.STDOUT,
                env=popen_env,
                start_new_session=True,
            )
        finally:
            err_fh.close()

    # ----- pty / true-resume mode -----------------------------------------

    def _is_pty_mode(
        self,
        prior: RemoteControlInstance | None = None,
        *,
        requested: str | None = None,
    ) -> bool:
        """Whether the bridge launches under the PTY keeper (true resume). POSIX only.

        A bridge's mode is fixed at first launch. Precedence: an explicit
        *requested* mode (the per-launch picker) wins for a fresh start; else when
        *prior* is given (a resume of an existing instance) its recorded
        ``resume_mode`` wins, so ``stop()`` and ``resume()`` can never disagree
        about the same bridge; else the global ``claude.resume_mode`` seeds a
        brand-new bridge. Without honoring *prior*, editing the config under a
        running/stopped bridge would silently flip its mode on the next resume
        while stop still treated it as the old mode. Windows always falls back to
        standard (pty is POSIX-only).
        """
        if sys.platform == "win32":
            return False
        if requested is not None:
            return requested == "pty"
        if prior is not None:
            return prior.resume_mode == "pty"
        return self._config.claude.resume_mode == "pty"

    @staticmethod
    def _sidecar_path_for(log_path: Path) -> Path:
        """Discovery JSON the keeper writes beside the bridge's --debug-file."""
        return log_path.with_name(log_path.stem + ".keeper.json")

    def _build_pty_bridge_argv(
        self, log_path: Path, name: str, permission_mode: PermissionMode, *, resume: bool
    ) -> list[str]:
        """Build the flag-form bridge argv (`claude --remote-control …`). Pure/testable.

        Unlike the subcommand (`_build_cmd`), the flag form is a single interactive
        session — no `--spawn`/`--capacity`. ``--continue`` (on resume) is what makes
        the restarted session restore its prior conversation context.
        """
        argv = [
            self._binary,
            "--remote-control",
            name,
            "--debug-file",
            str(log_path),
            "--permission-mode",
            permission_mode,
        ]
        if resume:
            argv.append("--continue")
        return argv

    @staticmethod
    def _keeper_launch_cmd(sidecar: Path, cwd: Path, bridge_argv: list[str]) -> list[str]:
        """Wrap the bridge argv in a `python -m clauster.pty_keeper` launcher."""
        return [
            sys.executable,
            "-m",
            "clauster.pty_keeper",
            "--sidecar",
            str(sidecar),
            "--cwd",
            str(cwd),
            "--",
            *bridge_argv,
        ]

    def _popen_keeper(self, cwd: Path, sidecar: Path, bridge_argv: list[str]) -> subprocess.Popen:
        """Launch the PTY keeper detached so it outlives a Clauster restart.

        Same detached pattern as the subcommand `_popen` (own session, stdin
        detached, stdout/stderr to a file) — the keeper, not Clauster, holds the
        bridge's terminal, so it survives independently and keeps the bridge alive.
        """
        cmd = self._keeper_launch_cmd(sidecar, cwd, bridge_argv)
        keeper_log = sidecar.with_suffix(".log")  # the keeper's own stdout/stderr
        err_fh = keeper_log.open("wb")
        try:
            # Overlay the operator's PATH/env extensions onto the KEEPER's env: the
            # keeper inherits them into its own os.environ and re-emits them (still
            # secret-scrubbed) when it spawns the bridge via child_env(), so the pty
            # bridge gets the same extended PATH/env as the standard path.
            return subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=err_fh,
                stderr=subprocess.STDOUT,
                env=procutil.child_env(self._bridge_env_overlay()),
                start_new_session=True,
            )
        finally:
            err_fh.close()

    @staticmethod
    def _read_sidecar(sidecar: Path) -> dict | None:
        """Read the keeper's discovery JSON, or None if absent / mid-write / invalid."""
        try:
            return json.loads(sidecar.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
            # UnicodeDecodeError (a ValueError) for a non-UTF-8 sidecar must still
            # honor the invalid -> None contract, not break readiness polling.
            return None

    def _recover_keeper_pid(
        self, name: str, bridge_pid: int | None, bridge_proc_start: float | None
    ) -> int | None:
        """Find a rediscovered pty bridge's keeper pid from its sidecar.

        After a Clauster restart we know the bridge pid + proc-start (from the
        pointer-walk) but not the timestamped ``--debug-file`` path, so the sidecar
        can't be addressed directly. Glob the log dir for ``{name}-*.keeper.json``
        and match on ``bridge_pid`` **and** ``bridge_proc_start`` — the latter is the
        PID-reuse defense: a stale sidecar that merely recycled the pid is rejected,
        so stop()/poll_once can never reap an unrelated process tree. The keeper is
        alive iff the bridge is (it holds its terminal), already confirmed before this.

        proc-start is compared with the same slack as :func:`procutil.is_live_bridge`
        (the pointer's stored value and the sidecar's psutil create-time are derived
        independently, so exact float equality would be brittle); when either side
        is unknown, fall back to the pid-only match.
        """
        if bridge_pid is None:
            return None
        for sidecar in sorted(self._log_dir.glob(f"{name}-*.keeper.json")):
            info = self._read_sidecar(sidecar)
            if info is None or info.get("bridge_pid") != bridge_pid:
                continue
            ps = info.get("bridge_proc_start")
            if (
                bridge_proc_start is not None
                and isinstance(ps, (int, float))
                and not isinstance(ps, bool)
                and abs(float(ps) - bridge_proc_start) > _PROC_START_TOLERANCE
            ):
                continue
            keeper_pid = info.get("keeper_pid")
            if isinstance(keeper_pid, int) and not isinstance(keeper_pid, bool):
                return keeper_pid
            return None
        return None

    def _await_ready_pty(self, sidecar: Path, proc: subprocess.Popen) -> dict:
        """Block until the keeper publishes a connect URL, the keeper exits, or timeout."""
        deadline = time.monotonic() + _READY_TIMEOUT
        info: dict = {}
        while time.monotonic() < deadline:
            info = self._read_sidecar(sidecar) or info
            if proc.poll() is not None:  # keeper (and thus bridge) gone before ready
                return self._read_sidecar(sidecar) or info
            if info.get("connect_url") or info.get("state") in ("ready", "error"):
                return info
            time.sleep(_READY_POLL_INTERVAL)
        return info

    def _apply_pty_info(
        self, instance: RemoteControlInstance, info: dict, proc: subprocess.Popen
    ) -> None:
        """Fold the keeper sidecar into the instance (the pty analogue of `_apply_markers`)."""
        prev_status = instance.status
        bp = info.get("bridge_pid")
        if isinstance(bp, int):
            instance.bridge_pid = bp
            ps = info.get("bridge_proc_start")
            if isinstance(ps, (int, float)) and not isinstance(ps, bool):
                instance.bridge_proc_start = float(ps)
        if sid := info.get("session_id"):
            instance.starter_session_id = sid
        if url := info.get("connect_url"):
            instance.url = url

        keeper_dead = proc.poll() is not None
        # A pty bridge is RUNNING once the keeper reports readiness: either a captured
        # connect URL, or state == "ready" (a --continue resume that reconnected without
        # re-printing the URL). A live keeper+bridge must never read as ERROR.
        ready = bool(info.get("connect_url")) or info.get("state") == "ready"
        if ready and not keeper_dead:
            instance.status = InstanceStatus.RUNNING
        elif info.get("state") == "error" or keeper_dead:
            instance.status = InstanceStatus.ERROR
        else:
            instance.status = InstanceStatus.STARTING  # let the startup-watch promote it
        if prev_status is not InstanceStatus.RUNNING and instance.status is InstanceStatus.RUNNING:
            self._emit_lifecycle("ready", instance)  # only on the transition, not every poll

    async def _spawn_pty(
        self,
        instance: RemoteControlInstance,
        proj: Project,
        name: str,
        log_path: Path,
        permission_mode: PermissionMode,
        resume: bool,
    ) -> RemoteControlInstance:
        """Spawn path for `resume_mode == "pty"`: launch the keeper, discover via sidecar."""
        # The sidecar stays keyed off the public log_path; the bridge's --debug-file goes
        # to the private raw parse-source (== log_path unless on-disk redaction is on).
        sidecar = self._sidecar_path_for(log_path)
        debug_path = instance.bridge_raw_log_path or log_path
        bridge_argv = self._build_pty_bridge_argv(debug_path, name, permission_mode, resume=resume)
        try:
            bridge_argv[0] = resolve_binary(bridge_argv[0])
            proc = await asyncio.to_thread(self._popen_keeper, proj.path, sidecar, bridge_argv)
        except (OSError, ClaudeNotFound) as exc:
            _log.warning("pty spawn of %s failed to launch: %s", name, exc)
            instance.status = InstanceStatus.ERROR
            await self._persist()
            return instance
        self._procs[name] = proc
        instance.keeper_pid = proc.pid
        info = await asyncio.to_thread(self._await_ready_pty, sidecar, proc)
        self._apply_pty_info(instance, info, proc)
        await asyncio.to_thread(self._flush_redacted_mirror, instance)
        if instance.status is InstanceStatus.ERROR:
            # Surface whatever the keeper recorded (openpty/spawn failure); the
            # bridge's own failure reason, if any, is in its --debug-file on disk.
            instance.error_detail = info.get("error")
        await self._persist()
        if instance.status is InstanceStatus.STARTING:
            self._start_startup_watch(name)
        return instance

    def _cleanup_keeper(self, pid: int) -> None:
        """Reap the keeper (Clauster's direct child); force it down if it lingers.

        The keeper self-exits once its bridge is gone, so this is usually just a
        reap; the force path covers a keeper that somehow outlives its bridge.
        """
        for _ in range(8):  # ~2s grace for the keeper to follow its bridge out
            procutil.reap_if_exited(pid)
            if procutil.proc_create_time(pid) is None:
                return
            time.sleep(0.25)
        procutil.force_kill_tree(pid)
        procutil.reap_if_exited(pid)

    def _await_ready(self, log_path: Path, proc: subprocess.Popen) -> bridge_log.BridgeMarkers:
        """Block until the bridge is ready, errors, or times out.

        The log file is created by the bridge after exec, so poll-until-exists.
        Because the path is unique to this spawn, any markers found are ours.
        """
        deadline = time.monotonic() + _READY_TIMEOUT
        markers = bridge_log.BridgeMarkers()
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                # Exited before becoming ready — read whatever it logged.
                markers = self._read_markers(log_path)
                return markers
            markers = self._read_markers(log_path)
            if markers.trust_error or markers.is_ready:
                return markers
            time.sleep(_READY_POLL_INTERVAL)
        return markers

    @staticmethod
    def _read_markers(log_path: Path) -> bridge_log.BridgeMarkers:
        try:
            # errors="replace": the debug log is raw bridge output; a stray
            # non-UTF-8 byte must not raise UnicodeDecodeError (a ValueError,
            # which the OSError guard below would NOT catch) and lose all markers.
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            return bridge_log.BridgeMarkers()
        return bridge_log.parse_bridge_markers(text)

    def _apply_markers(
        self,
        instance: RemoteControlInstance,
        markers: bridge_log.BridgeMarkers,
        proc: subprocess.Popen,
    ) -> None:
        prev_status = instance.status
        instance.bridge_id = markers.bridge_id or instance.bridge_id
        instance.environment_id = markers.environment_id or instance.environment_id
        instance.starter_session_id = markers.starter_session_id or instance.starter_session_id
        if markers.environment_id:
            instance.url = f"https://claude.ai/code?environment={markers.environment_id}"

        if markers.is_ready and proc.poll() is None:
            instance.status = InstanceStatus.RUNNING
        elif markers.trust_error or proc.poll() is not None:
            # Genuine, terminal failure: the bridge rejected workspace trust, or it
            # exited before ever reaching the poll loop. Surface it as ERROR.
            instance.status = InstanceStatus.ERROR
        else:
            # Alive but hasn't logged readiness within _READY_TIMEOUT. A slow start
            # is not a failure: stay STARTING and let the poll loop promote it to
            # RUNNING (or CRASHED if it later dies). Prevents a false "Failed to
            # start" on a bridge that is simply still coming up.
            instance.status = InstanceStatus.STARTING
        if prev_status is not InstanceStatus.RUNNING and instance.status is InstanceStatus.RUNNING:
            self._emit_lifecycle("ready", instance)  # only on the transition, not every poll

    async def _post_spawn_enrich(
        self, instance: RemoteControlInstance, project_path: Path
    ) -> None:
        """After readiness is decided, fill in what the log alone can't tell us.

        - RUNNING: a *reconnecting* bridge never re-logs ``Created initial
          session``, so ``starter_session_id`` (and thus ``session_url``) would
          be empty after a resume — backfill it from the pointer.
        - ERROR/CRASHED: capture the bridge's stderr tail so the failure has a
          visible reason instead of a bare "Failed to start".
        """
        if instance.status is InstanceStatus.RUNNING:
            await asyncio.to_thread(self._backfill_starter_session, instance, project_path)
        elif instance.status in (InstanceStatus.ERROR, InstanceStatus.CRASHED):
            await asyncio.to_thread(self._capture_error_detail, instance)

    @classmethod
    def _backfill_starter_session(
        cls, instance: RemoteControlInstance, project_path: Path
    ) -> None:
        """Recover the session id when the log/keeper omitted it, for the deep link.

        A *reconnecting* bridge re-logs its environment but NOT "Created initial
        session", so ``starter_session_id`` (and thus ``session_url``, the primary
        deep link) would be empty after a resume without this. No-op for a fresh
        start, which logs the session directly. (The environment id never needs
        backfilling: this only runs once RUNNING, which already requires it.)

        Two sources, in order: the subcommand bridge-pointer, then — for a pty/
        flag-form true-resume, which leaves no pointer and whose keeper can't capture
        the connect URL (a reconnect never reprints it) — the bridge's ``--debug-file``,
        where a ``--continue`` logs the session it resumed as ``[remote-bridge]
        Unarchive session_<id>`` (see ``bridge_log._RE_RESUME_SESSION``).
        """
        if instance.starter_session_id is not None:
            return
        ptr = pointers.pointer_for_project(project_path)
        if ptr is not None and ptr.session_id:
            instance.starter_session_id = ptr.session_id
            return
        # Parse the verbatim raw log (the public mirror has the session id redacted).
        log_path = instance.bridge_raw_log_path or instance.bridge_debug_log_path
        if log_path is not None:
            sid = cls._read_markers(log_path).starter_session_id
            if sid:
                instance.starter_session_id = sid

    @classmethod
    def _capture_error_detail(cls, instance: RemoteControlInstance) -> None:
        """Read the tail of the bridge's captured stderr into ``error_detail``."""
        log_path = instance.bridge_debug_log_path
        if log_path is None:
            return
        try:
            text = (
                cls._stderr_path_for(log_path)
                .read_text(encoding="utf-8", errors="replace")
                .strip()
            )
        except OSError:
            return
        if text:
            # Redact before storing: this tail is surfaced inline in the UI, and the bridge's
            # startup banner prints env_/session_/cse_ bearer-credential ids — same posture as
            # the at-rest log mirror. Redact first (strips ANSI so an escape-split id can't slip
            # through), THEN bound it: the UI shows a reason, not a full transcript.
            instance.error_detail = redact.redact_for_disk(text)[-2000:]

    def _project_path(self, name: str) -> Path | None:
        proj = self._discovered().get(name)
        return proj.path if proj is not None else None

    # ----- startup watch --------------------------------------------------

    def _start_startup_watch(self, name: str) -> None:
        """Launch (or replace) the background watch for a STARTING bridge."""
        old = self._startup_watches.pop(name, None)
        if old is not None and not old.done():
            old.cancel()
        task = asyncio.create_task(self._watch_startup(name), name=f"startup-watch:{name}")
        self._startup_watches[name] = task

        def _done(t: asyncio.Task, _name: str = name) -> None:
            if self._startup_watches.get(_name) is t:
                self._startup_watches.pop(_name, None)
            if not t.cancelled() and (exc := t.exception()) is not None:
                _log.warning("startup-watch for %s failed: %s", _name, exc)

        task.add_done_callback(_done)

    async def _watch_startup(self, name: str) -> None:
        """Resolve a STARTING bridge off the request path.

        Re-reads the bridge log until the bridge registers an environment (-> the
        existing :meth:`_apply_markers` promotes it to RUNNING) or until the
        ``startup_grace_seconds`` budget expires while it is still alive but
        unregistered — which is a failed start (ERROR), not a running bridge.
        Process death during startup is delegated to :meth:`_reconcile_status` so
        the CRASHED/STOPPED outcome matches the poll loop exactly.
        """
        grace = self._config.claude.startup_grace_seconds
        deadline = time.monotonic() + grace
        while True:
            await asyncio.sleep(_STARTUP_WATCH_INTERVAL)
            instance = self._instances.get(name)
            proc = self._procs.get(name)
            if instance is None or proc is None or instance.status is not InstanceStatus.STARTING:
                return  # already resolved, stopped, or gone
            if proc.poll() is not None:  # exited during startup
                self._reconcile_status(instance, alive=False)
                await self._persist()
                return
            log_path = instance.bridge_debug_log_path
            if log_path is None:
                return  # nothing to read from; leave it for the poll loop
            if instance.resume_mode == "pty":
                # PTY bridges register via the keeper sidecar, not the subcommand's
                # bridge-log markers; readiness is the connect URL appearing there.
                sidecar = self._sidecar_path_for(log_path)
                info = await asyncio.to_thread(self._read_sidecar, sidecar)
                self._apply_pty_info(instance, info or {}, proc)
                # Keep the at-rest mirror current during pty startup too: poll_once
                # can't yet (bridge_pid is still unknown until the sidecar reveals it),
                # so without this the public log would stale out after _spawn_pty's
                # one-time flush if the bridge logs more before registering.
                await asyncio.to_thread(self._flush_redacted_mirror, instance)
                if instance.status is not InstanceStatus.STARTING:
                    await self._persist()
                    return
            else:
                raw = instance.bridge_raw_log_path or log_path
                markers = await asyncio.to_thread(self._read_markers, raw)
                self._apply_markers(instance, markers, proc)
                await asyncio.to_thread(self._flush_redacted_mirror, instance)  # at-rest log
                if instance.status is not InstanceStatus.STARTING:  # promoted, or trust ERROR
                    await self._post_spawn_enrich(instance, self._project_path(name) or log_path)
                    await self._persist()
                    return
            if time.monotonic() >= deadline:
                instance.status = InstanceStatus.ERROR
                _log.warning(
                    "bridge %s is alive but never registered an environment within %.0fs; "
                    "marking ERROR (it is not connectable). Check the bridge debug log — a "
                    "common cause is the claude user lacking readable remote-control credentials.",
                    name,
                    grace,
                )
                await asyncio.to_thread(self._capture_error_detail, instance)
                await self._persist()
                return

    # ----- stop -----------------------------------------------------------

    async def stop(self, name: str) -> RemoteControlInstance:
        """Signal a managed bridge to shut down and mark the stop as intentional."""
        instance = self._instances.get(name)
        if instance is None:
            raise UnknownProject(f"no managed instance: {name!r}")
        self._cancel_startup_watch(name)  # stop racing the watch over this instance's status
        instance.intentional_stop = True  # mark intent BEFORE signalling (spec §3 feat 4)
        await self._persist()  # persist the intent so a restart doesn't mislabel it CRASHED

        pid = instance.bridge_pid
        # A pty bridge needs its keeper reaped even if the bridge pid is already
        # gone (the keeper is Clauster's direct child); capture it up front.
        keeper_pid = instance.keeper_pid
        if pid is None:
            if keeper_pid is not None:
                await asyncio.to_thread(self._cleanup_keeper, keeper_pid)
            instance.status = InstanceStatus.STOPPED
            self._procs.pop(name, None)  # release the dead Popen handle; resume re-adds it
            self._emit_lifecycle("stop", instance)
            return instance

        # Re-validate identity immediately before signalling (TOCTOU / PID reuse).
        if await asyncio.to_thread(procutil.is_live_bridge, pid, instance.bridge_proc_start):
            # The flag-form (pty) bridge's TUI treats the first SIGINT as "press
            # again to exit"; a second confirms. The subcommand bridge stops on one.
            twice = instance.resume_mode == "pty"
            await asyncio.to_thread(self._signal_stop, pid, twice=twice)
            await self._await_exit(name, pid, instance.bridge_proc_start)
        if keeper_pid is not None:
            await asyncio.to_thread(self._cleanup_keeper, keeper_pid)
        instance.status = InstanceStatus.STOPPED
        self._procs.pop(name, None)  # release the dead Popen handle; resume re-adds it
        self._emit_lifecycle("stop", instance)
        return instance

    async def forget(self, name: str) -> None:
        """Drop a NON-LIVE bridge's record from memory and state.json (fail closed).

        Lets the operator clear a stopped / crashed / interrupted bridge out of the
        Recent/resumable list to start fresh. Removes the entry from BOTH the in-memory
        registry and the persisted map — dropping only one leaves the other to
        resurrect it (``_persist_subset`` overlays ``_persisted``; ``rediscover``
        rebuilds a STOPPED card from it) — then re-persists so ``state.json`` no longer
        carries it.

        Fail closed: a live bridge (STARTING/RUNNING, or one whose bridge/keeper process
        is still alive despite a lagging status) is refused with
        :class:`InstanceStillLive` — it must be Stopped first; forget never kills a
        process. Raises :class:`UnknownProject` when there's no such record at all.
        """
        # Hold the per-project spawn lock so a concurrent spawn()/resume() can't
        # repopulate _instances/_procs between the liveness check and the pop() —
        # forgetting must never remove tracking for a just-spawned live process.
        async with self._spawn_lock_for(name):
            instance = self._instances.get(name)
            if instance is None and name not in self._persisted:
                raise UnknownProject(f"no managed instance: {name!r}")
            if instance is not None:
                if instance.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING):
                    raise InstanceStillLive(
                        f"{name!r} is {instance.status.value} — Stop it before forgetting"
                    )
                # Defense in depth: never drop a record whose process is actually alive even
                # if the status lags a missed poll — that would orphan a live bridge/keeper.
                if instance.bridge_pid is not None and await asyncio.to_thread(
                    procutil.is_live_bridge, instance.bridge_pid, instance.bridge_proc_start
                ):
                    raise InstanceStillLive(f"{name!r} still has a live bridge — Stop it first")
                if (
                    instance.keeper_pid is not None
                    and await asyncio.to_thread(procutil.proc_create_time, instance.keeper_pid)
                    is not None
                ):
                    raise InstanceStillLive(f"{name!r} still has a live keeper — Stop it first")
                self._instances.pop(name, None)
                self._procs.pop(name, None)
            # Rebuild as a NEW dict rather than .pop() in place: _persist aliases _persisted
            # and _last_saved to the same object, so mutating _persisted would also mutate the
            # dedup baseline and _persist would skip the write (leaving the row on disk).
            self._persisted = {k: v for k, v in self._persisted.items() if k != name}
            await self._persist()

    @staticmethod
    def _signal_stop(pid: int, *, twice: bool = False) -> None:
        """Ask a bridge to shut down gracefully.

        SIGINT on POSIX, CTRL_BREAK on Windows (deliverable because the bridge is
        its own process group). When ``twice`` (pty mode), send a second SIGINT
        after a short beat — the flag-form TUI requires a confirming second press.
        """
        sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
        try:
            os.kill(pid, sig)
            if twice:
                time.sleep(0.4)  # let the TUI surface "press Ctrl-C again to exit"
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            # Already exited / reused / not signalable — _await_exit's liveness
            # poll and force-kill fallback handle the outcome; don't raise out of stop().
            _log.debug("stop signal to pid %s was a no-op: %s", pid, exc)

    async def _await_exit(self, name: str, pid: int, proc_start: float | None) -> None:
        for _ in range(20):  # ~5s grace for a clean shutdown
            alive = await asyncio.to_thread(procutil.is_live_bridge, pid, proc_start)
            if not alive:
                break
            await asyncio.sleep(0.25)
        else:
            # Ignored the graceful signal (or a wrapper process is lingering, e.g.
            # a Windows .cmd shim parked at cmd.exe's prompt) -> force the tree down.
            await asyncio.to_thread(procutil.force_kill_tree, pid)
        await asyncio.to_thread(procutil.reap_if_exited, pid)

    # ----- background poll (source #2 + liveness reconcile) ---------------

    def _saved_modes(self, saved: dict) -> tuple[SpawnMode, PermissionMode, ResumeMode]:
        """Coerce persisted spawn/permission/resume modes against the allowed sets.

        A hand-edited or corrupt ``state.json`` that holds an unknown mode must not
        fail the (Literal-typed) model and abort startup — fall back to the
        configured defaults instead. ``resume_mode`` lives on ``ClaudeConfig``, the
        other two on ``InstanceDefaults``.
        """
        defaults = self._config.instance_defaults
        sm = saved.get("spawn_mode")
        pm = saved.get("permission_mode")
        rm = saved.get("resume_mode")
        return (
            sm if sm in SPAWN_MODES else defaults.spawn_mode,
            pm if pm in PERMISSION_MODES else defaults.permission_mode,
            rm if rm in RESUME_MODES else self._config.claude.resume_mode,
        )

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
        saved = self._persisted.get(name)
        if not saved:
            return None
        spawn_mode, permission_mode, resume_mode = self._saved_modes(saved)
        return RemoteControlInstance(
            project=name,
            label=saved.get("label") or name,
            spawn_mode=spawn_mode,
            permission_mode=permission_mode,
            resume_mode=resume_mode,
            # The process is gone: no pid/keeper/env to recover. intentional_stop is
            # carried through (a host-down bridge has it False — "interrupted" — vs a
            # deliberate Stop's True); both render as a resumable STOPPED card.
            intentional_stop=bool(saved.get("intentional_stop", False)),
            status=InstanceStatus.STOPPED,
            bridge_pid=None,
            bridge_proc_start=None,
            keeper_pid=None,
        )

    async def rediscover(self) -> None:
        """Re-detect bridges after a restart: reattach live ones, resurrect dead ones.

        A bridge found *alive* is reattached as RUNNING. A discovered project whose
        bridge is gone but which has a persisted record (its process died while
        Clauster was down — e.g. a host reboot) is resurrected as a STOPPED,
        resumable card instead of being dropped; one with no persisted record is
        left absent (nothing to resume).
        """
        for proj in self._discovered().values():
            if proj.name in self._instances:
                continue
            ptr = await asyncio.to_thread(pointers.pointer_for_project, proj.path)
            if ptr is None or not await asyncio.to_thread(pointers.is_live, ptr):
                stopped = self._stopped_from_persisted(proj.name)
                if stopped is not None:
                    self._instances[proj.name] = stopped
                continue
            # Overlay the few fields the pointer-walk can't recover; a bridge
            # found alive is by definition NOT intentionally stopped.
            saved = self._persisted.get(proj.name, {})
            spawn_mode, permission_mode, resume_mode = self._saved_modes(saved)
            # _expected_epoch (not bare int()) so an unparseable procStart degrades to
            # None (cmdline-only liveness) instead of raising ValueError out of startup.
            # Mirrors is_live_bridge, so the liveness check and this construction can't
            # disagree. Computed once: reused for keeper matching AND the instance.
            bridge_proc_start = procutil._expected_epoch(ptr.proc_start)
            # A "pty" bridge is held by a detached keeper that outlives a Clauster
            # restart; recover its pid from the sidecar so stop()/poll_once can reap
            # it — otherwise a rediscovered pty bridge would leak its keeper. The log
            # path is timestamped (not derivable), so match the sidecar by bridge pid
            # + proc-start (PID-reuse defense — see _recover_keeper_pid).
            keeper_pid = (
                await asyncio.to_thread(
                    self._recover_keeper_pid, proj.name, ptr.pid, bridge_proc_start
                )
                if resume_mode == "pty"
                else None
            )
            self._instances[proj.name] = self._instance_from_pointer(
                proj.name,
                ptr,
                label=saved.get("label") or proj.name,
                spawn_mode=spawn_mode,
                permission_mode=permission_mode,
                resume_mode=resume_mode,
                bridge_proc_start=bridge_proc_start,
                keeper_pid=keeper_pid,
            )
        await self._persist()

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
    ) -> RemoteControlInstance:
        """Build a RUNNING managed instance from a live Anthropic-written pointer.

        The pointer supplies the live-derived facts (bridge pid, env id, connect URL);
        the modes/label/keeper come from the caller (the persisted record or config
        defaults — the pointer carries none of them). Shared by :meth:`rediscover`
        (startup reattach of survivors) and :meth:`adopt` (runtime take-over of a
        standard external session) so both synthesize an identical managed shape. A
        bridge found alive is by definition NOT intentionally stopped.
        """
        return RemoteControlInstance(
            project=name,
            label=label,
            spawn_mode=spawn_mode,
            permission_mode=permission_mode,
            resume_mode=resume_mode,
            keeper_pid=keeper_pid,
            intentional_stop=False,
            status=InstanceStatus.RUNNING,
            bridge_pid=ptr.pid,
            bridge_proc_start=bridge_proc_start,
            environment_id=ptr.environment_id,
            starter_session_id=ptr.session_id,
            url=f"https://claude.ai/code?environment={ptr.environment_id}",
        )

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
          Stop) -> :class:`AdoptionUnavailable` (409). pty external sessions stay
          display-only.

        Caveat the UI must carry: a standard bridge's environment server dies with its
        host, so a later Resume of the adopted session is a *fresh* Start, not a
        continuation of its prior conversation.
        """
        # Hold the per-project spawn lock so a concurrent spawn()/resume()/forget()
        # can't race the registry between the liveness check and the insert.
        async with self._spawn_lock_for(name):
            if name in self._instances:
                raise InstanceStillLive(f"{name!r} is already managed — nothing to adopt")
            proj = self._discovered().get(name)
            if proj is None:
                raise UnknownProject(f"no such project: {name!r}")
            ptr = await asyncio.to_thread(pointers.pointer_for_project, proj.path)
            # Re-check liveness AND the standard-subcommand cmdline at click time: a
            # stale pointer (the bridge died since the poll) or a pty/flag-form bridge
            # both fail the gate, so adoption can only ever take over a live standard
            # bridge — the one shape it can safely Stop.
            if ptr is None or not await asyncio.to_thread(
                procutil.is_live_standard_bridge, ptr.pid, ptr.proc_start
            ):
                raise AdoptionUnavailable(
                    f"{name!r} has no live standard bridge to adopt — it may have ended, "
                    "or it's a pty (true-resume) bridge, which can't be adopted"
                )
            saved = self._persisted.get(name, {})
            spawn_mode, permission_mode, _resume_mode = self._saved_modes(saved)
            instance = self._instance_from_pointer(
                name,
                ptr,
                label=saved.get("label") or name,
                spawn_mode=spawn_mode,
                permission_mode=permission_mode,
                # The live process is positively confirmed standard above (cmdline
                # gate), so pin "standard" rather than trusting a possibly-stale
                # persisted resume_mode — keeper recovery / double-SIGINT stop stay off.
                resume_mode="standard",
                bridge_proc_start=procutil._expected_epoch(ptr.proc_start),
                keeper_pid=None,
            )
            self._instances[name] = instance
            await self._persist()
            return instance

    def adoptable_external_projects(self) -> set[str]:
        """Project names whose live EXTERNAL session is a *standard* bridge safe to adopt.

        A standard external bridge writes an Anthropic pointer whose pid is a live
        ``claude remote-control`` subcommand process; a pty (flag-form) external bridge
        is excluded (unsafe to adopt — see :meth:`adopt`), as is one whose pointer has
        gone stale. Computed from the same pointer + cmdline checks :meth:`adopt`
        enforces, so the dashboard's Adopt affordance can never offer an adoption that
        :meth:`adopt` would then refuse. Synchronous (filesystem + ``psutil``); call it
        off-loop.
        """
        discovered = self._discovered()
        adoptable: set[str] = set()
        for name in self.external_sessions_by_project():
            proj = discovered.get(name)
            if proj is None:
                continue
            ptr = pointers.pointer_for_project(proj.path)
            if ptr is not None and procutil.is_live_standard_bridge(ptr.pid, ptr.proc_start):
                adoptable.add(name)
        return adoptable

    async def poll_once(self) -> None:
        """Reconcile bridge liveness and cross-check `claude agents --json`.

        Off-loop work, applied on-loop.
        """
        # Projects whose bridge PROCESS is actually alive (PID + proc-start match).
        # This is the source of truth for "do we own the sessions at this cwd" below —
        # NOT the instance's status field, which can lag or be wrong (a fresh pty bridge
        # stuck pre-ready, a crash misdetection). See the `managed` set.
        live_projects: set[str] = set()
        for instance in list(self._instances.values()):
            pid = instance.bridge_pid
            # A pty keeper is Clauster's direct child and self-exits with its bridge;
            # reap it here so it never lingers as a zombie after an organic exit.
            if instance.keeper_pid is not None:
                await asyncio.to_thread(procutil.reap_if_exited, instance.keeper_pid)
            if pid is None:
                continue
            await asyncio.to_thread(procutil.reap_if_exited, pid)
            alive = await asyncio.to_thread(
                procutil.is_live_bridge, pid, instance.bridge_proc_start
            )
            prev_status = instance.status
            self._reconcile_status(instance, alive)
            if (
                prev_status is not InstanceStatus.CRASHED
                and instance.status is InstanceStatus.CRASHED
            ):
                self._crash_counts[instance.project] = (
                    self._crash_counts.get(instance.project, 0) + 1
                )
                self._notify_crash(instance)
                self._emit_lifecycle("crash", instance)
            if alive:
                live_projects.add(instance.project)
                # Keep the public bridge log redacted-current as the bridge writes.
                # No-op unless on-disk redaction split the raw/public paths.
                await asyncio.to_thread(self._flush_redacted_mirror, instance)

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
        # A managed bridge owns the working sessions at its cwd iff its PROCESS is alive
        # (computed above), NOT merely if its status is RUNNING/STARTING. Keying on
        # status mislabels our OWN live bridge as external whenever its status is wrong —
        # e.g. a fresh pty bridge that connected but never printed a scrapeable connect
        # URL is left pre-ready and would otherwise have its own session flagged
        # "external session active" and the record phantom-deleted below. A genuinely
        # dead instance (no live process — a `_stopped_from_persisted` phantom from a
        # stale pointer) is correctly absent here, so a real flag-form/tmux bridge at its
        # cwd still surfaces as external.
        managed = {
            Path(discovered[i.project].path): i.project
            for i in self._instances.values()
            if i.project in discovered and i.project in live_projects
        }
        self._sessions = inspector.reconcile(sessions, managed)
        # Drop a non-live managed instance whose project has a live EXTERNAL session:
        # the bridge IS alive, just unmanaged (flag-form/tmux), so the persisted record
        # is a phantom. Showing it as a Stopped/Resume card is misleading and invites a
        # double-spawn — let the card fall back to "external session active" instead.
        external_cwds = {
            s.cwd.resolve() for s in self._sessions if s.attribution is Attribution.EXTERNAL
        }
        # No _persist() after this delete, by design: this is continuous reconciliation
        # (every poll, and the first poll runs immediately on startup), not a one-time
        # edit — so it self-heals after any restart. Persisting would be a no-op anyway:
        # _persist_subset overlays `live` onto the retained `_persisted` map, which keeps
        # the record (intentionally — it preserves the project's modes for a later managed
        # spawn). Re-materialization by `_stopped_from_persisted` is cleaned by the next poll.
        for n, inst in list(self._instances.items()):
            # Only prune non-live (STOPPED/CRASHED) phantoms. A RUNNING/STARTING instance
            # here that isn't in live_projects was inserted AFTER this poll snapshotted
            # live_projects (the lock-free poll races a lock-held adopt()/spawn() that lands
            # during the list_working_sessions suspension) — pruning it would silently undo
            # a just-adopted/spawned bridge. The first loop already reconciled every instance
            # it saw, so a genuinely-dead record is no longer RUNNING by the time we get here.
            if inst.status in (InstanceStatus.RUNNING, InstanceStatus.STARTING):
                continue
            if (
                inst.project not in live_projects
                and inst.project in discovered
                and Path(discovered[inst.project].path).resolve() in external_cwds
            ):
                del self._instances[n]
                self._procs.pop(n, None)  # don't leak the phantom's dead Popen handle

    @staticmethod
    def _reconcile_status(instance: RemoteControlInstance, alive: bool) -> None:
        status = instance.status
        if status in (InstanceStatus.RUNNING, InstanceStatus.STARTING) and not alive:
            # session mode is single-shot: the bridge exits when its session ends, so a
            # disappearance is expected (STOPPED), not a crash. same-dir/worktree persist,
            # so an unintended exit there IS a crash. A STARTING bridge that vanishes
            # died during startup — the same expected/unexpected distinction applies.
            expected_exit = instance.intentional_stop or instance.spawn_mode == "session"
            instance.status = InstanceStatus.STOPPED if expected_exit else InstanceStatus.CRASHED
        # NB: a STARTING bridge that is merely *alive* is NOT promoted to RUNNING
        # here. Promotion requires a confirmed environment registration (handled by
        # the startup-watch via _apply_markers). A bridge can stay alive without
        # ever authenticating to the controller — liveness is not usability, and
        # promoting on it reported uncontrollable bridges as RUNNING.

    def _notify_crash(self, instance: RemoteControlInstance) -> None:
        """Fire a best-effort crash notification (off-loop; never blocks/raises the poll).

        Called when a bridge transitions to CRASHED — an unexpected exit, i.e. not via
        the Stop button. No-op unless notifications are active and crash alerts are on.
        """
        if not self._notifier.active or not self._config.notifications.notify_on_crash:
            return
        title = f"clauster: bridge crashed — {instance.label}"
        body = (
            f"The bridge for project {instance.project!r} exited unexpectedly "
            f"(not via Stop) — mode {instance.resume_mode}/{instance.spawn_mode}."
        )
        # Fire-and-forget: anotify sends off-thread and swallows its own errors. Keep a
        # reference so the task isn't GC'd mid-send; drop it on completion.
        task = asyncio.create_task(self._notifier.anotify(title, body))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    def _session_ref_key(self) -> bytes:
        """Return the per-deployment HMAC key for ``session_ref``, loading it once.

        Reuses the session-signing secret so the correlation token is unverifiable
        without it. Fail-open: if the secret can't be loaded (e.g. a misconfigured
        ``CLAUSTER_SESSION_SECRET``), fall back to a process-stable random key so the
        webhook still emits a non-reversible token and the bridge lifecycle is never
        affected — webhooks are best-effort by design.
        """
        if self._session_ref_secret is None:
            try:
                self._session_ref_secret = auth.load_or_create_secret(self._config.state_dir)
            except Exception:
                # Never let a secret-load error break a fire-and-forget webhook.
                _log.warning(
                    "webhook session_ref: session secret unavailable; using an "
                    "ephemeral per-process key (correlation works within this run only)"
                )
                self._session_ref_secret = secrets.token_bytes(32)
        return self._session_ref_secret

    def _emit_lifecycle(self, event: str, instance: RemoteControlInstance) -> None:
        """Single chokepoint for a lifecycle transition: record history + fire webhook.

        ``event`` is one of ``spawn`` / ``ready`` / ``stop`` / ``crash``. Both sinks
        are best-effort and off the loop, so neither can affect the bridge lifecycle:
        the history append is fail-closed (a lost row is logged, never raised) and the
        webhook is fail-open (a broken endpoint is swallowed).
        """
        self._record_event(event, instance)
        self._emit_webhook(event, instance)

    # Maps the internal webhook event name to the persisted history ``kind``.
    _HISTORY_KIND = {"spawn": "spawned", "ready": "ready", "stop": "ended", "crash": "crashed"}

    def _record_event(self, event: str, instance: RemoteControlInstance) -> None:
        """Append a session-history row for ``event`` off-loop (#363; best-effort).

        Snapshots the loop-owned instance fields *here* (the worker thread must never
        read ``self._instances``), then does the transcript parse + DB write in a
        background task. A terminal (``stop`` / ``crash``) event carries the project's
        cumulative end-of-session cost/token snapshot from :mod:`clauster.usage`;
        non-terminal rows carry no cost. Any failure is swallowed by the store — a
        lost history row never affects a spawn or stop.

        Called from a lifecycle path that is normally on the event loop. If invoked
        without a running loop (a synchronous status-apply call), it falls back to a
        direct best-effort synchronous append so the row is still recorded.

        The whole method is fail-closed: every error — the loop-owned prologue (which
        touches the filesystem via ``_project_path``), the off-loop snapshot, and the
        append — is logged and swallowed, on both the async and the synchronous path,
        so a history hiccup can never raise into the spawn/stop/crash caller.
        """
        kind = self._HISTORY_KIND.get(event)
        if kind is None:  # unknown event name — never persist a bogus kind
            return

        def _append(
            project: str, mode: str, session_ref: str | None, usage: ProjectUsage | None
        ) -> None:
            totals = usage.totals if usage is not None else None
            self._history.append(
                project_name=project,
                mode=mode,
                kind=kind,
                session_ref=session_ref,
                cost_usd=usage.cost_usd() if usage is not None else None,
                input_tokens=totals.input if totals is not None else None,
                output_tokens=totals.output if totals is not None else None,
                cache_creation_tokens=totals.cache_creation if totals is not None else None,
                cache_read_tokens=totals.cache_read if totals is not None else None,
            )

        try:
            # "hosted" sessions run on the claustrum channel; otherwise the resume axis
            # (standard remote-control vs the pty keeper) is the mode worth recording.
            mode = "hosted" if instance.channel == "hosted" else instance.resume_mode
            project = instance.project
            # Snapshot the loop-owned values now; the off-loop task only touches locals.
            # ``_project_path`` walks the filesystem and can raise — it stays inside this
            # guard so a discovery I/O error never reaches the lifecycle caller.
            session_ref = _hash_session_ref(instance.starter_session_id, self._session_ref_key())
            terminal = kind in ("ended", "crashed")
            project_path = self._project_path(project) if terminal else None
        except Exception as exc:  # noqa: BLE001 — history must never break the lifecycle
            _log.warning(
                "could not prepare session event (%s/%s): %s", instance.project, kind, exc
            )
            return

        def _usage_snapshot() -> ProjectUsage | None:
            """Cost/token rollup for a terminal row, or None (non-terminal / unreadable)."""
            if not (terminal and project_path is not None):
                return None
            try:
                return aggregate_project_usage_cached(
                    project_path,
                    project_name=project,
                    claude_projects_dir=self._claude_projects_dir,
                )
            except OSError as exc:
                # An unreadable transcript must not drop the terminal row — record the
                # event with a null cost rather than skip the history entirely.
                _log.warning("session-history cost snapshot failed for %s: %s", project, exc)
                return None

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (a synchronous status-apply call path): do the snapshot +
            # append inline. Best-effort — the store's append already fails closed, so a
            # DB error is swallowed there; guard the snapshot's own non-OSError too.
            try:
                _append(project, mode, session_ref, _usage_snapshot())
            except Exception as exc:  # noqa: BLE001 — history must never break the lifecycle
                _log.warning("could not record session event (%s/%s): %s", project, kind, exc)
            return

        async def _write() -> None:
            # Mirror the sync path's swallow-and-log so a parser/DB error surfaces as a
            # tidy warning, not asyncio's "Task exception was never retrieved" noise.
            try:
                usage = await asyncio.to_thread(_usage_snapshot)
                await asyncio.to_thread(_append, project, mode, session_ref, usage)
            except Exception as exc:  # noqa: BLE001 — history must never break the lifecycle
                _log.warning("could not record session event (%s/%s): %s", project, kind, exc)

        task = asyncio.create_task(_write())
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    def _emit_webhook(self, event: str, instance: RemoteControlInstance) -> None:
        """Fire a best-effort lifecycle webhook (off-loop; never blocks/raises, #371).

        ``event`` is one of ``spawn`` / ``ready`` / ``stop`` / ``crash``. No-op unless
        webhooks are active and this event is enabled. The POST is fire-and-forget and
        fail-open — a slow or broken endpoint can't affect the bridge lifecycle.
        """
        if not self._webhooks.wants(event):
            return
        payload = {
            "project": instance.project,
            "label": instance.label,
            "status": instance.status.value,
            "resume_mode": instance.resume_mode,
            "spawn_mode": instance.spawn_mode,
            # Item-8 (#408): the raw starter_session_id is a session_<ULID> that
            # redaction treats as bearer-equivalent everywhere else (anyone holding
            # it can open a New Session composer for the bridge) — see redact.py and
            # the WS log-stream stripping. Egressing it raw to an arbitrary operator
            # webhook endpoint is the same leak that surface forbids, so we send a
            # hashed, non-reversible correlation token instead: a receiver can still
            # correlate the spawn/ready/stop/crash events of one session without ever
            # holding the credential-equivalent value. Keyed with a per-deployment
            # secret so it can't even be VERIFIED against a guessed session id.
            "session_ref": _hash_session_ref(instance.starter_session_id, self._session_ref_key()),
        }
        task = asyncio.create_task(self._webhooks.aemit(event, payload))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    # ----- lifecycle ------------------------------------------------------

    async def start_poll_loop(self) -> None:
        """Rediscover already-running bridges, then start the background poll loop."""
        await self.rediscover()
        self._poll_task = asyncio.create_task(self._poll_forever())
        # Server-side metrics sampler (#354): only when the feature is on. Keeps the
        # per-project / batch / scrape reads at O(1) with no per-request thread.
        if self._config.metrics.enabled:
            self._metrics_task = asyncio.create_task(self._metrics_refresh_forever())

    async def _poll_forever(self) -> None:
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

    def _cancel_startup_watch(self, name: str) -> None:
        task = self._startup_watches.pop(name, None)
        if task is not None and not task.done():
            task.cancel()

    async def shutdown(self) -> None:
        """Cancel the poll loop and startup watches, leaving managed bridges running."""
        # Cancel the poll task and any in-flight startup watches; leave bridges
        # running (they are detached and survive a Clauster restart).
        for task in list(self._startup_watches.values()):
            if not task.done():
                task.cancel()
        self._startup_watches.clear()
        for attr in ("_poll_task", "_metrics_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
