"""SessionRunner — spawn/stop/observe `claude remote-control` bridges (features 2-4).

The in-memory registry (``_instances``) is keyed by **instance_id** (a stable RFC
4122 UUID minted at spawn time, #777).  Standard (server-mode) bridges are capped
at one per project; interactive (pty) sessions may run any number per project.

Concurrency contract: the registry is mutated ONLY on the event loop. Blocking
work (Popen, os.kill, psutil, log reads, `claude agents --json`) runs in
``asyncio.to_thread`` and *returns* values that the loop applies back; callers
iterate over a ``list(...)`` snapshot, never the live dict.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import unicodedata
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from . import (
    atomicio,
    auth,
    bridge_log,
    code_sessions,
    inspector,
    metrics,
    pointers,
    procutil,
    pty_screen,
    redact,
    usage,
)
from .claude_cli import ClaudeNotFound, resolve_binary
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
from .db.persistence import Persistence
from .db.stores import CostSnapshot
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


def _conpty_keeper_available() -> bool:
    """Return True on Windows when pywinpty (the ConPTY keeper backend, ``pty`` extra) is present.

    Interactive Session on Windows runs the bridge under a ConPTY pseudo-console via
    pywinpty (:mod:`clauster.pty_keeper`); without the extra there is no keeper, so the
    launch falls back to Server Mode. The early platform guard both encodes the
    Windows-only requirement and keeps the type checker from resolving the win32-only
    import on a POSIX host.
    """
    if sys.platform != "win32":
        return False  # pragma: skip-on-win
    try:
        import winpty  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


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


# Cap on the operator-supplied custom bridge/session display name (#780). Generous
# enough for a real label, small enough to keep argv/log lines and the dashboard's
# name chip sane; the bridge binary itself imposes no documented limit on --name.
_CUSTOM_NAME_MAX_LEN = 128

# The 8-4-4-4-12 hex shape of a claude conversation/session UUID (a transcript's
# filename stem). resume_session_id (#303) must match this EXACTLY before it can
# reach a subprocess argv — anything else is rejected as InvalidSpawnOption.
_SESSION_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _is_display_unsafe(ch: str) -> bool:
    """Whether ``ch`` is a control/format or line/paragraph-separator character (#780).

    Rejects any Unicode *control* or *format* character — ``unicodedata.category``
    starting with ``"C"`` (Cc, Cf, Cs, Co, Cn). That covers the C0/C1 controls, DEL,
    and — critically for display safety — the bidi override/isolate format chars
    (U+202A–202E, U+2066–2069) that can visually spoof a rendered name. It also
    rejects the line (Zl, U+2028) and paragraph (Zp, U+2029) separators, which are
    *not* category-C but still break a single-line log/JSON rendering. Ordinary
    non-ASCII letters (accents, CJK, emoji) are category L/N/S/etc. and pass.
    """
    if unicodedata.category(ch).startswith("C"):
        return True
    return ch in ("\u2028", "\u2029")


def _normalize_custom_name(raw: str | None, fallback: str) -> str:
    """Validate and normalize an optional custom bridge display name (#780).

    ``None``, or a string that is empty after stripping surrounding whitespace,
    falls back to ``fallback`` (today's behavior: the project name) — an operator
    who leaves the field blank sees no change. Otherwise the stripped name is
    returned, having first been checked for length and for display-unsafe characters.

    It's list-argv (never ``shell=True``), so this is not a shell-injection concern —
    but the name is rendered in the Alpine dashboard and serialized to JSON, so a
    control/format character would corrupt --debug-file log lines, spoof the rendered
    name (bidi overrides), or break single-line rendering. We fail closed with
    :class:`InvalidSpawnOption` for any Unicode control/format character
    (``unicodedata.category`` category ``C*``, which includes the bidi overrides
    U+202A–202E / U+2066–2069) plus the line/paragraph separators U+2028/U+2029,
    rather than silently stripping or passing them through. Ordinary non-ASCII
    letters (e.g. ``Café-Bridge``) are accepted.
    """
    if raw is None:
        return fallback
    stripped = raw.strip()
    if not stripped:
        return fallback
    if len(stripped) > _CUSTOM_NAME_MAX_LEN:
        raise InvalidSpawnOption(
            f"custom bridge name too long ({len(stripped)} chars; max {_CUSTOM_NAME_MAX_LEN})"
        )
    if any(_is_display_unsafe(ch) for ch in stripped):
        raise InvalidSpawnOption("custom bridge name must not contain control characters")
    return stripped


@dataclass(slots=True)
class SpawnOutcome:
    """What a spawn call actually did, for API callers that must surface it (#778).

    ``created`` is False when the call returned an already-live instance instead of
    launching a new one — the standard-singleton cap (a live standard bridge exists
    for the project) or an idempotent resume of an already-live pty session —
    with ``reason`` saying which. ``warnings`` carries non-blocking advisories the
    caller should show the operator (today: launching an interactive pty session
    without a worktree risks conflicting concurrent edits).
    """

    instance: RemoteControlInstance
    created: bool
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


# How long to wait for a freshly-spawned bridge to reach its poll loop.
_READY_TIMEOUT = 15.0
_READY_POLL_INTERVAL = 0.25
# #867 L3: a *reattach* can reach the poll loop and only THEN have its re-adopted session
# torn down as archived/deleted (#671). After readiness on a reattach (no fresh "Created
# initial session"), watch a brief grace for that poison marker before declaring RUNNING; a
# cold start skips it. And bound how long we wait for the poisoned idle bridge to stop.
_POISON_GRACE = 4.0
_POISON_STOP_TIMEOUT = 5.0
# #867 L4: nothing else prunes bridge-pointer.json, so a project accumulates a pointer that
# outlives its (server-reaped) environment. At startup, clear clauster's OWN pointers that
# are both non-live AND older than this — a live or recently-stopped-resumable session is
# never touched (its reattach is preserved).
_STALE_POINTER_TTL_SECONDS = 14 * 24 * 60 * 60
# Cadence at which the post-spawn startup-watch re-reads the bridge log to detect
# a (late) environment registration or a stuck-but-alive bridge.
_STARTUP_WATCH_INTERVAL = 2.0
# Slack when matching a keeper sidecar's recorded proc-start against the pointer's
# (the two epochs are derived independently). Mirrors procutil.is_live_bridge's
# default tolerance so the two PID-reuse checks can't disagree.
_PROC_START_TOLERANCE = 2.0
# How long shutdown() waits for in-flight fire-and-forget notify sends to finish
# before cancelling them — bounds shutdown while letting a quick send complete.
_NOTIFY_DRAIN_GRACE = 2.0


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
        if not task.cancelled() and task.exception() is None:
            cm.__exit__(None, None, None)

    return _release


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
        # Cross-process lock files live in the deployment state dir (#949). The web app
        # already configures this in create_app; doing it here too means a HEADLESS
        # runner (CLI `clauster start/stop`, the MCP write tools) built from the same
        # config flocks in the same directory as the running service — without this, a
        # headless writer's cross-process lock degrades to a warning and never excludes
        # the web app. Idempotent (same value both times in the web app). The dir is
        # ALSO pinned per-runner: every runner flock passes `lock_dir=self._lock_dir`
        # explicitly, so a later configure_lock_dir for a DIFFERENT state dir in the
        # same process (tests, exotic embedding) can't silently redirect this runner's
        # lock files away from the ones external processes use (Greptile #951 P1).
        self._lock_dir = (config.state_dir / "locks").expanduser()
        atomicio.configure_lock_dir(self._lock_dir)
        # Monotonic spawn counter → unique log filenames even for two same-ms spawns.
        self._log_seq = 0
        # Registry keyed by instance_id (stable UUID, #777). Standard bridges keep
        # one entry per project; pty sessions may have N entries per project.
        self._instances: dict[str, RemoteControlInstance] = {}
        # Per-project bridge-crash tally since process start, exposed as the
        # clauster_bridge_crashes_total counter (#352) — a crash that resumes between
        # scrapes still leaves a trace, unlike the current-status gauge.
        self._crash_counts: dict[str, int] = {}
        # Popen handles keyed by instance_id (parallel to _instances).
        self._procs: dict[str, subprocess.Popen] = {}
        self._sessions: list[WorkingSession] = []
        self._poll_task: asyncio.Task | None = None
        # Server-side metrics snapshot (#354): the metrics task refreshes it off the
        # request path so /api/projects/{name}/metrics + the batch read + the /metrics
        # scrape all serve from the last sample at O(1), no per-request thread. Keyed
        # by instance_id (#778 — a project may run several bridges, so a project key
        # would clobber); the public readers aggregate per project.
        self._metrics_cache: dict[str, dict] = {}
        self._metrics_task: asyncio.Task | None = None
        # Per-spawn background tasks that watch a STARTING bridge until it either
        # registers an environment (-> RUNNING) or proves stuck (-> ERROR).
        # Keyed by instance_id (one watch per spawned instance).
        self._startup_watches: dict[str, asyncio.Task] = {}
        # Per-project locks serializing concurrent spawns of the SAME project (see
        # ``spawn``). Keyed by project name — the per-project standard-singleton check
        # and pty-warning logic must run serially for the same project.
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
        self._persistence = persistence or Persistence(
            config.state_dir, backup_before_migrate=config.db.backup_before_migrate
        )
        self._state = self._persistence.state_store()
        # Append-only session lifecycle / event history (#363). Records spawn/ready/
        # end/crash transitions for the Projects-zone "last used" sort (#298) and the
        # pty resume picker (#303). Best-effort and fail-closed — a lost history row
        # never affects a bridge's lifecycle.
        self._history = self._persistence.session_history_store()
        self._persisted: dict[str, dict] = self._state.load()
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
        # Live hosted-session view for the agents --json cross-check (#592). The app
        # wires this to HostedManager.list_instances once both are built; it stays None
        # in unit tests and whenever the hosted channel is unused — poll_once then sees
        # no hosted sessions to claim and attributes exactly as before.
        self._hosted_instances: Callable[[], list[RemoteControlInstance]] | None = None

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

    def set_hosted_provider(
        self, provider: Callable[[], list[RemoteControlInstance]] | None
    ) -> None:
        """Register the hosted-session snapshot used to attribute hosted sessions (#592).

        The app passes ``HostedManager.list_instances`` after both are built so the
        poll loop's ``agents --json`` cross-check can recognize Clauster's own hosted
        sessions instead of mislabeling them EXTERNAL/unmanaged. ``None`` clears it.
        """
        self._hosted_instances = provider

    def crash_counts(self) -> dict[str, int]:
        """Return a copy of the per-project bridge-crash tally since process start (#352)."""
        return dict(self._crash_counts)

    def metrics_snapshot(self, name: str) -> dict | None:
        """Return the aggregated cached resource sample for project ``name``, or None (#354).

        With several bridges live for one project (#777), the per-instance samples are
        folded into one per-project figure (see :meth:`metrics_snapshots`).
        """
        return self.metrics_snapshots().get(name)

    def metrics_snapshots(self) -> dict[str, dict]:
        """Return per-project aggregated resource samples (#354 batch read).

        The cache holds one sample per *instance* (#778); this folds them into one
        dict per project — ``procs``/``cpu_percent``/``rss_bytes`` summed, the
        ``disk_*`` rates summed when any bridge reports them (``None`` when none do),
        plus ``bridges``: how many live bridges the figure covers. A cache entry whose
        instance vanished from the registry between refreshes is dropped, never
        misattributed.
        """
        out: dict[str, dict] = {}
        for iid, sample in self._metrics_cache.items():
            inst = self._instances.get(iid)
            if inst is None:  # forgotten since the last refresh
                continue
            agg = out.get(inst.project)
            if agg is None:
                out[inst.project] = {**sample, "bridges": 1}
                continue
            agg["bridges"] += 1
            agg["procs"] += sample["procs"]
            agg["cpu_percent"] = round(agg["cpu_percent"] + sample["cpu_percent"], 1)
            agg["rss_bytes"] += sample["rss_bytes"]
            for k in ("disk_read_bps", "disk_write_bps"):
                if sample[k] is not None:
                    agg[k] = (agg[k] or 0) + sample[k]
        return out

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
                # Keyed by instance_id (#778): several bridges may share one project,
                # and a project key would keep only whichever sampled last.
                fresh[inst.instance_id] = sample
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
        """Return the instance with this instance_id, or None if unknown."""
        return self._instances.get(instance_id)

    def get_instance_for_project(self, project_name: str) -> RemoteControlInstance | None:
        """Return the instance the project-keyed dashboard displays for this project.

        A project may hold one standard bridge plus N interactive (pty) sessions
        (#777). The pre-#779 client folds ``GET /api/instances`` into a project-keyed
        map (``map[project] = row``), so the LAST-registered row wins the project's
        card — and its name-identity actions (Stop / Resume / Forget / QR send the
        project name) must target exactly that displayed instance, in any status.
        Preferring anything else (a live bridge, the oldest row) would act on a
        bridge the operator cannot see. Callers that only need liveness use
        :meth:`has_running_instance` instead (#778). Does not raise; ``None`` when
        no instance matches.
        """
        found: RemoteControlInstance | None = None
        for inst in self._instances.values():
            if inst.project == project_name:
                found = inst  # keep scanning: the last-registered match wins
        return found

    def has_running_instance(self, project_name: str) -> bool:
        """Report whether ANY managed instance for the project is RUNNING (#778).

        Liveness-exact, unlike :meth:`get_instance_for_project`, whose canonical
        pick can transiently be a STARTING standard bridge while a pty session for
        the same project is already RUNNING — a caller that only wants "is
        something running here?" (``_bridge_running`` in app.py) must not miss it.
        """
        return any(
            inst.project == project_name and inst.status is InstanceStatus.RUNNING
            for inst in self._instances.values()
        )

    def resolve_bridge_id(self, identity: str) -> str | None:
        """Resolve a bridge identity (instance_id OR project name) to an instance_id.

        The registry is keyed by ``instance_id`` (#777), but the current dashboard
        client still sends the *project name* as the bridge identity on Stop /
        Resume / Forget / QR (the #778 API split will move it to instance_id). This
        keeps that client working: a known ``instance_id`` returns itself; otherwise
        the identity is treated as a project name and mapped to its instance's id.

        Returns ``None`` when the identity matches neither a known instance_id nor a
        managed project — the caller raises the same 404 it would have raised before.
        With N instances per project the name fallback resolves via
        :meth:`get_instance_for_project` (#778) to the instance the project-keyed
        client actually DISPLAYS (its map folds last-registered-wins), so a name
        action never targets a bridge the operator cannot see. Per-session
        operations on a multi-session project must send the instance_id.
        """
        if identity in self._instances:
            return identity
        inst = self.get_instance_for_project(identity)
        return inst.instance_id if inst is not None else None

    def _live_standard_for_project(self, project_name: str) -> RemoteControlInstance | None:
        """Return the first STARTING/RUNNING *standard* bridge for a project, or ``None``.

        Used by :meth:`_spawn_locked` to enforce the one-standard-bridge-per-project
        cap: if a live standard bridge exists, a second spawn returns it idempotently
        rather than starting a second environment server at the same project root.
        """
        for inst in self._instances.values():
            if (
                inst.project == project_name
                and inst.resume_mode == "standard"
                and inst.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING)
            ):
                return inst
        return None

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

    def tracked_sessions_by_instance(self) -> dict[str, list[WorkingSession]]:
        """Group TRACKED working sessions by their owning managed instance (#570).

        A standard ``claude remote-control`` bridge is multi-session: one bridge
        can host several concurrent working sessions, all sharing its cwd. The
        ``agents --json`` cross-check (computed in :meth:`poll_once`) already
        attributes each to its owning bridge via ``parent_instance``; this exposes
        that list so the dashboard can enumerate every live session under a bridge,
        not just its starter session.

        Keyed by ``parent_instance`` (the project/instance id stamped at reconcile).
        Sessions are ordered by ``started_at`` then ``local_uuid`` for a stable
        render order across polls. HOSTED/EXTERNAL/UNTRACKED sessions are excluded.
        """
        out: dict[str, list[WorkingSession]] = {}
        for session in self._sessions:
            if session.attribution is not Attribution.TRACKED:
                continue
            if session.parent_instance is None:
                continue
            out.setdefault(session.parent_instance, []).append(session)
        for sessions in out.values():
            sessions.sort(key=lambda s: (s.started_at, s.local_uuid))
        return out

    def live_session_uuids(self, project_path: Path) -> set[str]:
        """Local session UUIDs of currently-running sessions writing into a project's dir (#614).

        Joins the live ``agents --json`` snapshot (:attr:`_sessions`, already
        terminal-state-filtered at parse) to a project's transcript directory by the
        same key Claude uses to lay the transcripts down: the *sanitized cwd* (see
        :func:`pointers.sanitize_cwd`). A session whose ``cwd`` sanitizes to the same
        directory as ``project_path`` writes its ``<local_uuid>.jsonl`` there, so its
        ``local_uuid`` matches a transcript filename stem listed for that project.

        The result lets the read-only transcript viewer badge a transcript as "live"
        when its session id maps to a running bridge/agent. It covers any kind of live
        session landing in that dir (a bridge child, an external terminal session); a
        worktree-spawn session lives under a *different* sanitized cwd, so it is neither
        listed here nor by :func:`usage.transcript_paths_for` for the project root —
        the two stay consistent. Hosted (claustrum) sessions are folded in separately
        by the route, since they run no ``agents --json`` session.
        """
        target = pointers.sanitize_cwd(project_path)
        return {s.local_uuid for s in self._sessions if pointers.sanitize_cwd(s.cwd) == target}

    # ----- persistence (state.json, D14) ----------------------------------

    def _persist_subset(self) -> dict[str, dict]:
        live = {
            inst.instance_id: {
                "project_name": inst.project,
                "label": inst.label,
                "intentional_stop": inst.intentional_stop,
                "spawn_mode": inst.spawn_mode,
                "permission_mode": inst.permission_mode,
                "resume_mode": inst.resume_mode,
                "sandbox_mode": inst.sandbox_mode,
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
            loaded = await asyncio.to_thread(self._state.load_strict)
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
        dashboard poll. ``_last_saved``/``_persisted`` are left unchanged on failure so
        the next persist retries (mirrors :meth:`HostedManager._persist`).

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
        next persist retries. ``drop`` (:meth:`forget`, the one deletion path)
        excludes that instance id from the freshly refreshed base so the delete is
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
            await asyncio.to_thread(self._state.save, subset)
        except OSError as exc:
            _log.warning("could not persist bridge state: %s", exc)
            return
        self._last_saved = subset
        # Keep the merge base in sync with what's on disk so the next overlay builds
        # on the latest saved state (live modes that changed this round are retained).
        self._persisted = subset
        self._row_backed |= set(subset)  # everything just saved is now row-backed

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

    async def _clear_pointer_if_anchor_poisoned(self, project_path: Path) -> None:
        """#867 L2: pre-spawn, drop a preserved pointer whose anchor was archived/deleted.

        The CLI reattaches an existing environment purely from ``bridge-pointer.json``; if
        the anchor session behind it is gone, that reattach dead-ends into a bridge with no
        session (#671). Probing ``/v1/code/sessions`` and dropping a poisoned pointer forces
        a clean cold start instead. Best-effort throughout: only a non-live pointer is
        considered, any uncertainty leaves the pointer intact, and nothing here blocks or
        fails the spawn.
        """
        resolved = project_path.resolve()
        pointer = await asyncio.to_thread(
            pointers.pointer_for_project, resolved, self._claude_projects_dir
        )
        if pointer is None or not pointer.session_id:
            return  # cold start (or pty, which writes no pointer) — nothing to reattach
        if await asyncio.to_thread(pointers.is_live, pointer):
            return  # a live bridge owns it; never touch a running anchor
        credentials_path = self._claude_json.parent / ".claude" / ".credentials.json"
        health = await asyncio.to_thread(
            code_sessions.anchor_health_for_pointer,
            pointer.session_id,
            credentials_path=credentials_path,
            claude_json_path=self._claude_json,
        )
        if health is not code_sessions.AnchorHealth.POISONED:
            return  # HEALTHY -> reattach as-is; UNKNOWN -> leave it, the backstop covers it
        _log.info(
            "clearing bridge-pointer for %s: anchor session %s is archived/deleted; "
            "would dead-end on reattach (#671)",
            resolved,
            pointer.session_id,
        )
        try:
            await asyncio.to_thread(
                pointers.clear_pointer, resolved, claude_projects_dir=self._claude_projects_dir
            )
        except (pointers.PointerStillLive, OSError) as exc:
            _log.warning("could not clear poisoned bridge-pointer for %s: %s", resolved, exc)

    async def spawn(
        self,
        name: str,
        *,
        spawn_mode: SpawnMode | None = None,
        permission_mode: PermissionMode | None = None,
        resume_mode: ResumeMode | None = None,
        resume: bool = False,
        resume_target: RemoteControlInstance | None = None,
        custom_name: str | None = None,
        sandbox: SandboxMode | None = None,
        resume_session_id: str | None = None,
        trust: bool = False,
    ) -> RemoteControlInstance:
        """Spawn a bridge for ``name`` and return the instance (see :meth:`spawn_detailed`).

        Thin wrapper for callers that only need the instance; :meth:`spawn_detailed`
        additionally reports whether anything was actually launched and any
        non-blocking spawn warnings (#778). ``trust`` (#775) is forwarded unchanged.
        """
        outcome = await self.spawn_detailed(
            name,
            spawn_mode=spawn_mode,
            permission_mode=permission_mode,
            resume_mode=resume_mode,
            resume=resume,
            resume_target=resume_target,
            custom_name=custom_name,
            sandbox=sandbox,
            resume_session_id=resume_session_id,
            trust=trust,
        )
        return outcome.instance

    async def spawn_detailed(
        self,
        name: str,
        *,
        spawn_mode: SpawnMode | None = None,
        permission_mode: PermissionMode | None = None,
        resume_mode: ResumeMode | None = None,
        resume: bool = False,
        resume_target: RemoteControlInstance | None = None,
        custom_name: str | None = None,
        sandbox: SandboxMode | None = None,
        resume_session_id: str | None = None,
        trust: bool = False,
    ) -> SpawnOutcome:
        """Spawn a new bridge for ``name`` (returning the existing one if already up).

        Validates spawn/permission modes, ensures remote control + the recap hook are
        set up, launches the process, and watches it until it reaches RUNNING or ERROR.

        ``resume_mode`` ("standard"/"pty") picks the launch mode for *this* bridge,
        overriding the ``claude.launch_mode`` config default (the per-launch picker).
        When the effective mode is ``"pty"`` (POSIX only), the bridge is the
        ``claude --remote-control`` flag form run under a :mod:`clauster.pty_keeper`
        for true conversation resume; ``resume=True`` (set by :meth:`resume`) adds
        ``--continue`` so the restarted session restores its prior context. The mode
        is fixed at first launch and recorded on the instance, so a resume always
        keeps it (see :meth:`_is_pty_mode`).

        ``custom_name`` (#780) is an optional operator-supplied display name for a
        *standard* (server-mode) bridge, passed as ``claude remote-control --name``
        in place of the project name. Blank/``None`` keeps today's default (the
        project name); it is validated by :func:`_normalize_custom_name` before any
        spawn side effect. The pty (Interactive Session) launch form has no
        equivalent flag, so it's ignored there (see #780 disposition).

        ``sandbox`` (#780) is the per-launch OS-level filesystem/network isolation
        toggle for a *standard* bridge — tri-state ``"default"``/``"on"``/``"off"``
        (``None`` == ``"default"``). ``"default"`` appends neither flag (claude's
        own off-by-default / ``sandbox.*`` settings win — zero behavior change),
        ``"on"`` appends ``--sandbox``, ``"off"`` appends ``--no-sandbox``. Validated
        before any spawn side effect. Like ``custom_name`` it is standard-only; the
        pty form is out of scope for #780.

        Concurrent spawns of the *same* project are serialized by a per-project lock:
        a double-click, retry, or second browser tab must not both pass the
        idempotency check and launch two bridges, because the second would clobber
        the first in ``self._instances``/``self._procs`` and orphan an untracked,
        unreapable process. Different projects still spawn concurrently. Since #949
        the same section also holds a per-project *cross-process* lock
        (:meth:`_bridge_flock`) held through the readiness wait, so a SECOND clauster
        process (headless CLI/MCP writer vs the live web app) serializes here too and
        its own check-then-launch can't interleave with ours; its idempotency check
        additionally probes the on-disk bridge pointer (see ``_spawn_locked``), which
        our bridge has typically written by the time the lock is released.

        ``resume_target`` is the SPECIFIC instance a :meth:`resume` is reviving. It
        pins mode resolution and the pty idempotency check to that instance instead
        of a mode-agnostic project scan — otherwise a resume of a stopped pty session
        while a standard bridge is concurrently live (both allowed per project since
        #777) would resolve against the standard bridge and hand it back instead.

        ``resume_session_id`` (#303) is an operator-picked PAST conversation to fork
        into this NEW session: pty-only, appended as ``--resume <uuid> --fork-session``
        (fork = a fresh session id, so the original conversation is never clobbered —
        the spawn-alongside model, #669). Strictly validated (UUID shape, pty mode,
        never combined with the internal ``resume=True`` revive path) before any spawn
        side effect; invalid values raise :class:`InvalidSpawnOption` (→ 422).

        ``trust`` (the headless CLI's ``--trust``, #775) accepts the workspace-trust
        dialog for the project as part of this spawn. It is applied *after* option
        validation and under the per-project spawn lock — so an invalid option (a bad
        ``custom_name``, a forbidden permission mode, a worktree on a non-git project)
        raises without leaving the directory trusted, and the trust write can't race a
        concurrent spawn/stop. Left False, an untrusted directory raises
        :class:`NotTrusted` (unchanged). The dashboard trusts via a separate explicit
        action (:meth:`trust_project`); this is the headless equivalent, kept atomic.

        Returns a :class:`SpawnOutcome`: ``created`` is False when an already-live
        instance was returned instead of launching (with ``reason``), and
        ``warnings`` carries non-blocking advisories (the pty no-worktree collision
        warning) so the API can surface them (#778).
        """
        async with self._spawn_lock_for(name), self._bridge_flock(name):
            return await self._spawn_locked(
                name,
                spawn_mode=spawn_mode,
                permission_mode=permission_mode,
                resume_mode=resume_mode,
                resume=resume,
                resume_target=resume_target,
                custom_name=custom_name,
                sandbox=sandbox,
                resume_session_id=resume_session_id,
                trust=trust,
            )

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
        be a bare instance id on the :meth:`forget` fallback path (record with no
        resolvable project) — the derived path need not exist, it is only a key.

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

    async def _spawn_locked(
        self,
        name: str,
        *,
        spawn_mode: SpawnMode | None = None,
        permission_mode: PermissionMode | None = None,
        resume_mode: ResumeMode | None = None,
        resume: bool = False,
        resume_target: RemoteControlInstance | None = None,
        custom_name: str | None = None,
        sandbox: SandboxMode | None = None,
        resume_session_id: str | None = None,
        trust: bool = False,
    ) -> SpawnOutcome:
        # Body of spawn_detailed(), always run under the per-project lock (see spawn()).
        proj = self._resolve_project(name)
        # Refresh the persist merge-base under the locks (#949): the persisted-record
        # reads below (the reattach probe's saved modes) and this spawn's trailing
        # _persist must see the shared store as it is NOW, not as it was when this
        # runner was constructed — a headless writer's construction-time snapshot can
        # predate rows the web app has since added or forgotten.
        refreshed = await self._refresh_persisted()
        # Stale-resume gate (#951 round 4): resuming a DEAD card whose row-backed
        # record is gone from the fresh base would relaunch — and re-persist — a
        # session another clauster process explicitly forgot, silently undoing that
        # delete. Fail closed with the truth instead, and drop the card (it was only
        # a view of the deleted row). A LIVE resume target is untouched — it falls
        # through to the idempotent already-running return below. When the refresh
        # itself failed, the gate must not decide from the known-stale snapshot
        # (#951 round 5): refuse the resume as retryable — WITHOUT dropping the card,
        # since we couldn't learn whether its row is actually gone. A plain (non-
        # resume) spawn proceeds on a failed refresh: the store is non-authoritative
        # and launching bridges must not depend on it; _persist re-checks on its own.
        if resume and resume_target is not None:
            iid = resume_target.instance_id
            dead = resume_target.status not in (InstanceStatus.STARTING, InstanceStatus.RUNNING)
            if dead and iid in self._row_backed:
                if not refreshed:
                    raise SpawnError(
                        f"could not verify session {iid} against the shared state store "
                        "(transient read failure) — try the resume again"
                    )
                if iid not in self._persisted:
                    self._instances.pop(iid, None)
                    raise UnknownProject(
                        f"session {iid} was forgotten by another clauster process — "
                        "nothing left to resume"
                    )
        defaults = self._config.instance_defaults
        spawn_mode = spawn_mode or defaults.spawn_mode
        permission_mode = permission_mode or defaults.permission_mode
        # None == "default" (append neither sandbox flag); normalize up front so the
        # value stored on the instance and validated below is always one of SANDBOX_MODES.
        sandbox_mode: SandboxMode = sandbox or "default"
        # Resolve resume_mode early so we can apply the per-mode policy checks below
        # before spending side-effect budget (trust writes, log file creation, etc.).
        # For a resume the prior instance is the SPECIFIC one being revived
        # (resume_target) — NOT a mode-agnostic project scan, which could return a
        # coincidentally-live standard bridge and flip a pty resume to standard (#777).
        prior_for_mode = resume_target if resume else None
        effective_resume_mode: ResumeMode = (
            "pty" if self._is_pty_mode(prior_for_mode, requested=resume_mode) else "standard"
        )
        self._validate_spawn_options(proj, spawn_mode, permission_mode, resume_mode, sandbox_mode)
        # Fork-a-past-conversation (#303): validate BEFORE any spawn side effect, and
        # strictly — this string ends up on a subprocess argv, so nothing but a UUID
        # shape may pass (fail closed; list-argv means no shell, but defense in depth).
        if resume_session_id is not None:
            # Format FIRST: garbage is rejected identically on every platform/mode
            # (on Windows the effective mode is always standard — pty is POSIX-only —
            # so a mode-first ordering would mask the format error there).
            if not _SESSION_UUID_RE.fullmatch(resume_session_id):
                raise InvalidSpawnOption(
                    "resume_session_id must be a session UUID "
                    "(8-4-4-4-12 hex, as listed by the transcripts API)"
                )
            if resume:
                # The internal revive path (resume()) restores the instance's OWN
                # conversation via --continue; combining it with an operator-picked
                # conversation would be ambiguous — reject rather than pick a winner.
                raise InvalidSpawnOption(
                    "resume_session_id cannot be combined with resuming an existing session"
                )
            if effective_resume_mode != "pty":
                raise InvalidSpawnOption(
                    "resume_session_id requires the pty (Interactive Session) mode"
                )
            # Scope the pick to THIS project's own conversations (fail closed): a
            # well-formed uuid belonging to another project's transcript must never
            # fork foreign context into this session. resolve_session_transcript
            # walks only the project's sanitized-cwd transcript dir — the same
            # source the picker lists from — so anything it can't resolve is
            # rejected before any spawn side effect.
            #
            # Ownership requires that dir to be UNAMBIGUOUS. Claude keys transcripts
            # by sanitize_cwd (non-alphanumerics → "-"), so two configured project
            # paths that differ only in punctuation (e.g. ".../foo-bar" vs
            # ".../foo_bar") collide onto ONE transcript dir — membership alone can't
            # then prove which project a conversation belongs to. If any OTHER
            # discovered project shares this project's sanitized dir, ownership is
            # unprovable → refuse the fork (fail closed) rather than risk forking a
            # colliding project's conversation. This is a Claude-storage property the
            # picker listing shares; refusing here keeps the spawn no less strict than
            # the source it validates against.
            proj_dir = pointers.sanitize_cwd(proj.path)
            colliding = [
                other.name
                for other in self._discovered().values()
                if other.name != proj.name and pointers.sanitize_cwd(other.path) == proj_dir
            ]
            if colliding:
                raise InvalidSpawnOption(
                    f"cannot fork a conversation for {name!r}: its transcript directory "
                    f"is shared with project(s) {sorted(colliding)!r} (paths differing only "
                    "in punctuation), so conversation ownership is ambiguous"
                )
            resolved_transcript = await asyncio.to_thread(
                usage.resolve_session_transcript, proj.path, resume_session_id
            )
            if resolved_transcript is None:
                raise InvalidSpawnOption(
                    f"resume_session_id {resume_session_id!r} is not a conversation "
                    f"of project {name!r}"
                )
        # Validate before any spawn side effect (fail closed), same as spawn/permission
        # mode above. Blank/None falls back to the project name (today's behavior); a
        # non-blank value is only actually passed as --name for a *standard* bridge (see
        # spawn_detailed docstring) — resolved_name still gets computed uniformly here so
        # a bad value 422s regardless of which mode ends up launching.
        resolved_name = _normalize_custom_name(custom_name, fallback=name)

        # Non-blocking advisories collected along the way, surfaced on the outcome so
        # the API can show them to the operator (#778).
        spawn_warnings: list[str] = []

        # --- per-mode spawn policy (#777) -----------------------------------
        if effective_resume_mode == "standard":
            # Standard bridges: cap at one per project.
            # If a live standard bridge already exists — for any reason (idempotent
            # re-spawn, double-click, concurrent tabs) — return it without launching
            # a second bridge.  A live PTY instance at the same project does NOT
            # block a standard spawn; the two modes are independent axes.  The cap
            # is enforced by returning the existing bridge (not by raising): a
            # second Start is a no-op the caller already sees as "still running".
            live_standard = self._live_standard_for_project(name)
            if live_standard is not None:
                return SpawnOutcome(
                    instance=live_standard,
                    created=False,
                    reason=(
                        f"a standard bridge for {name!r} is already "
                        f"{live_standard.status.value} — standard bridges are capped at "
                        "one per project, so the existing bridge was returned"
                    ),
                )
            # Cross-process half of the same idempotency check (#949): this process's
            # registry can't see a standard bridge ANOTHER clauster process (the live
            # web app vs a headless CLI/MCP writer) launched — but the bridge-pointer
            # it left on disk can, and we hold the cross-process per-project lock the
            # other writer's spawn held, so the pointer is past its fork-to-visible
            # window. Reattach a live hit and return it idempotently — the same
            # take-over :meth:`adopt` performs, with the same live-standard gate (a
            # dead pointer or a pty/flag-form bridge fails it and we launch normally).
            reattached = await self._reattach_external_standard(proj)
            if reattached is not None:
                return SpawnOutcome(
                    instance=reattached,
                    created=False,
                    reason=(
                        f"a standard bridge for {name!r} is already running (started "
                        "by another clauster process or externally) — reattached it "
                        "instead of launching a second one on the same environment"
                    ),
                )
        else:  # pragma: skip-on-win — pty branch: pywinpty-gated, unreachable on Windows CI
            # PTY sessions: N per project allowed; idempotent ONLY for the specific
            # instance being resumed (resume_target), never a coincidentally-live
            # other-mode/other instance — returning that would hand back the wrong
            # bridge for "resume my stopped pty session".
            if (
                resume
                and resume_target is not None
                and resume_target.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING)
            ):
                return SpawnOutcome(
                    instance=resume_target,
                    created=False,
                    reason=(
                        f"interactive session {resume_target.instance_id} is already "
                        f"{resume_target.status.value} — returned it instead of resuming"
                    ),
                )
            # Warn (don't block) when spawning a pty session without a worktree:
            # two pty sessions sharing the same cwd risk conflicting file edits.
            if spawn_mode != "worktree":
                spawn_warnings.append(
                    f"interactive session for {name!r} launched without a worktree — "
                    "concurrent interactive sessions sharing the same working directory "
                    "may cause conflicting file edits. Use the worktree spawn mode to "
                    "isolate each session."
                )
                _log.warning(
                    "pty session for %r launched without a worktree — concurrent interactive "
                    "sessions sharing the same working directory may cause conflicting file "
                    "edits. Use spawn_mode='worktree' to isolate each session (#777).",
                    name,
                )
        # --- end per-mode spawn policy ---------------------------------------

        # Workspace-trust gate. Without --trust an untrusted directory fails closed here
        # (fast, before any spawn side effect). With --trust we do NOT trust yet — the
        # trust write is deferred until after the capacity check below, so a rejected
        # start (bad option OR a full bridge cap) never leaves the directory trusted.
        if not trust and not await asyncio.to_thread(is_trusted, proj.path, self._claude_json):
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

        # #867 L2: before launching, drop a preserved pointer whose anchor was archived or
        # deleted — otherwise the CLI reattaches it and the bridge comes back with no
        # session (#671). Best-effort; a no-op for a cold start, a pty spawn, or when the
        # anchor is healthy/indeterminate.
        await self._clear_pointer_if_anchor_poisoned(proj.path)

        # Enforce the optional clauster-side concurrent-bridge cap. Past the idempotency
        # early-return, this project is NOT currently live, so every live instance is a
        # different bridge. Fail closed BEFORE any per-spawn side effect (file/process).
        max_bridges = defaults.max_bridges
        if max_bridges is not None:
            live = sum(
                1
                for inst in self._instances.values()
                if inst.project != name
                and inst.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING)
            )
            if live >= max_bridges:
                raise CapacityExceeded(
                    f"max_bridges={max_bridges} reached ({live} live); "
                    "stop a bridge before starting another"
                )

        # --trust (#775): every rejection — option validation, the idempotency
        # early-returns, and the bridge cap above — has now passed, so trust the
        # directory as part of the spawn. Deferred to here, after the LAST raise, so a
        # rejected start never persists trust; still under the per-project spawn lock so
        # it can't race a concurrent spawn/stop. A launch failure or cancellation AFTER
        # this keeps the grant by design — trust is a standalone, persistent operator
        # authorization, exactly as trust_project writes it, independent of any bridge.
        if trust:
            await asyncio.to_thread(trust_directory, proj.path, self._claude_json)
            invalidate_discovery_cache()

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
        # label always reflects what the bridge process is ACTUALLY given as its display
        # name: resolved_name is only ever passed as --name for the standard subcommand
        # form (below); the pty flag form always uses the project name (#780 disposition
        # — no equivalent flag), so label must match that or it'd lie about a running
        # pty session's name.
        label = resolved_name if effective_resume_mode == "standard" else name
        # Sandbox is a standard-only flag (#780); a pty bridge records "default" so its
        # persisted/displayed state never implies a toggle that was never applied.
        effective_sandbox: SandboxMode = (
            sandbox_mode if effective_resume_mode == "standard" else "default"
        )
        instance = RemoteControlInstance(
            project=name,
            label=label,
            status=InstanceStatus.STARTING,
            bridge_debug_log_path=log_path,
            bridge_raw_log_path=raw_path,
            started_at=datetime.now(UTC),
            # Validated above (_validate_spawn_options raises on a bad value), so
            # these str inputs are known-good members of the Literal types.
            spawn_mode=cast(SpawnMode, spawn_mode),
            permission_mode=cast(PermissionMode, permission_mode),
            # resume_mode was resolved above (effective_resume_mode) so the per-mode
            # policy checks could run before any side effects. Assign it now.
            resume_mode=effective_resume_mode,
            sandbox_mode=effective_sandbox,
        )
        if resume and resume_target is not None:
            # A resume REVIVES the same logical session: keep its instance_id so the
            # registry row (and the state-store row keyed on it) is REPLACED instead
            # of a fresh id leaving the old STOPPED row behind as a ghost duplicate.
            # Identity stability is also what keeps per-instance derivations (e.g.
            # the pty worktree name, #779) the same across a stop→resume cycle.
            instance.instance_id = resume_target.instance_id
        # Register under instance_id — the stable UUID minted by RemoteControlInstance
        # (via _new_instance_id default_factory) or carried over from the instance
        # being resumed, NOT the project name.
        self._instances[instance.instance_id] = instance  # on the loop

        # One spawn-event chokepoint for both modes: the instance is registered, STARTING,
        # and its resume_mode is now resolved. A "ready" follows iff it reaches RUNNING.
        self._emit_lifecycle("spawn", instance)
        if instance.resume_mode == "pty":  # pragma: skip-on-win
            return SpawnOutcome(
                instance=await self._spawn_pty(
                    instance,
                    proj,
                    name,
                    log_path,
                    permission_mode,
                    resume,
                    resume_session_id=resume_session_id,
                ),
                created=True,
                warnings=spawn_warnings,
            )

        try:
            # resolved_name (not the bare project name) becomes --name here (#780) —
            # the standard subcommand form is the only one with an equivalent flag.
            # effective_sandbox is standard-only too (see above).
            proc = await asyncio.to_thread(
                self._popen,
                proj.path,
                log_path,
                resolved_name,
                spawn_mode,
                permission_mode,
                raw_path,
                effective_sandbox,
            )
        except (OSError, ClaudeNotFound) as exc:
            # Binary unresolvable / not executable: fail the instance cleanly
            # instead of leaving it stuck in STARTING.
            _log.warning("spawn of %s failed to launch: %s", name, exc)
            instance.status = InstanceStatus.ERROR
            await self._persist()
            # created=True: a new registry row exists (in ERROR), not a reused one.
            return SpawnOutcome(instance=instance, created=True, warnings=spawn_warnings)
        self._procs[instance.instance_id] = proc
        instance.bridge_pid = proc.pid
        instance.bridge_proc_start = await asyncio.to_thread(procutil.proc_create_time, proc.pid)

        markers = await asyncio.to_thread(self._await_ready, raw_path, proc)
        self._apply_markers(instance, markers, proc)
        await asyncio.to_thread(self._flush_redacted_mirror, instance)
        if markers.poison_reason is not None:
            await self._heal_poisoned_reattach(instance, proc, proj.path, markers.poison_reason)
        else:
            await self._post_spawn_enrich(instance, proj.path)
        await self._persist()
        # A bridge still STARTING after the synchronous readiness wait may yet
        # register (slow start) or may be alive-but-stuck (e.g. it couldn't
        # authenticate to the controller). Watch it off the request path so it is
        # only ever promoted to RUNNING once it actually registers an environment.
        if instance.status is InstanceStatus.STARTING:
            self._start_startup_watch(instance.instance_id)
        return SpawnOutcome(instance=instance, created=True, warnings=spawn_warnings)

    async def resume(self, instance_id: str) -> RemoteControlInstance:
        """Re-spawn a stopped/crashed bridge, reconnecting to its prior session.

        Re-running ``claude remote-control`` in the same cwd reconnects to the
        existing environment + session (the bridge-pointer.json the prior run
        left behind drives it — empirically confirmed). We reuse the stopped
        instance's stored ``spawn_mode``/``permission_mode`` so the resume keeps
        the same permission mode (a *fresh* bare start would drop back to the
        default 'ask'). The session id, which a reconnecting bridge does NOT
        re-log, is recovered from the pointer by :meth:`spawn`'s enrich step.

        Also reuses the stopped instance's ``label`` as the custom-name input
        (#780) when — and only when — it differs from the project name: a standard
        bridge's ``label`` is whatever was resolved as its ``--name`` at first launch
        (a real custom name, or the project name as fallback). Threading a *real*
        custom name back through keeps it across a resume; a bare project-name label
        is passed as ``None`` so resume takes the same trusted fast-path as first
        spawn (``_normalize_custom_name(None, …)``) instead of re-running the project
        name through the validator — which would raise on a name a first spawn
        accepted, an asymmetry (Greptile #811).
        """
        existing = self._instances.get(instance_id)
        if existing is None:
            raise UnknownProject(f"no managed instance to resume: {instance_id!r}")
        # Only forward a label that is a genuine custom name; a bare project-name label
        # → None so the fallback path (not the validator) runs on resume, exactly as it
        # did on first spawn where custom_name was None.
        carried_name = existing.label if existing.label != existing.project else None
        return await self.spawn(
            existing.project,
            spawn_mode=existing.spawn_mode,
            permission_mode=existing.permission_mode,
            # Honor the mode recorded at first launch so stop() and resume() always
            # agree: a config flip (e.g. launch_mode: pty) must not silently change the
            # mode of an already-running/stopped bridge (#777).
            resume_mode=existing.resume_mode,
            # In pty mode this adds --continue so the flag-form bridge restores the
            # prior conversation; the standard subcommand path ignores it.
            resume=True,
            # Pin mode resolution + the pty idempotency check to THIS instance, so a
            # resume of a stopped pty session isn't misresolved against a concurrently
            # live standard bridge in the same project (#777).
            resume_target=existing,
            custom_name=carried_name,
            # Keep the sandbox choice across a resume, parity with custom_name (#780):
            # the instance records the resolved tri-state, so a resumed bridge re-passes
            # the same --sandbox/--no-sandbox (or neither) instead of reverting to default.
            sandbox=existing.sandbox_mode,
        )

    def _validate_spawn_options(
        self,
        proj: Project,
        spawn_mode: str,
        permission_mode: str,
        resume_mode: str | None = None,
        sandbox: str | None = None,
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
        if sandbox is not None and sandbox not in SANDBOX_MODES:
            raise InvalidSpawnOption(
                f"invalid sandbox {sandbox!r}; expected one of {SANDBOX_MODES}"
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
    # Longest-match-first so `.keeper.log` / `.raw.log` / `.screen.json` strip whole, not
    # just `.log` / `.json`. `.screen.json` (the #534 live-screen sidecar) is grouped here so
    # retention prunes it with its spawn set instead of orphaning it. (The orphan-keeper sweep
    # in iter_keepers still globs `*.keeper.json` only — a `.screen.json` with no live keeper
    # is harmless and gets pruned by age; revisit if S4+ ever leaves them without a keeper.)
    _LOG_SET_SUFFIXES = (
        ".raw.log",
        ".stderr.log",
        ".keeper.json",
        ".keeper.log",
        ".screen.json",
        ".log",
    )

    @classmethod
    def _log_set_key(cls, filename: str) -> str:
        """Map a log filename to its spawn-set key (the shared `<name>-<ms>-<seq>` stem)."""
        for suf in cls._LOG_SET_SUFFIXES:
            if filename.endswith(suf):
                return filename[: -len(suf)]
        return filename

    def _latest_debug_log_for(self, name: str) -> Path | None:
        """Newest public debug log (`<name>-<ms>-<seq>.log`) Clauster wrote for a project.

        Used to re-bind a rediscovered *standard* survivor's live tail to the log it was
        already writing before the restart — the timestamped path is otherwise lost on a
        cold start (unlike pty, there's no keeper sidecar to derive it from). The live
        survivor is by definition the most recently *spawned* bridge, so order by the
        ``<ms>-<seq>`` the filename already encodes (``_unique_log_path``) — NOT by mtime: the
        filename is the spawn order we actually want, and reading it avoids a ``stat()`` per
        candidate (no TOCTOU against log retention, which prunes on this same thread). Returns
        None when the dir/glob can't be read or no log remains (retention may have pruned a
        long-idle bridge's set).

        The candidate stem is anchored to this project's exact ``<name>-<ms>-<seq>.log`` shape
        — NOT the glob prefix. ``PROJECT_NAME_RE`` allows ``-`` (``discovery.py``), so the bare
        ``glob(f"{name}-*.log")`` also matches a *sibling* project's logs (``app`` ⇒
        ``app-2-…log`` / ``app-staging-…log``); binding to one of those would leak another
        project's tail — its verbatim ``--debug-file`` (session URL / env id) when on-disk
        redaction is off. Anchoring on the two trailing digit groups (``<ms>`` then ``<seq>``)
        rejects siblings while keeping this set, and only the bare ``.log`` matches (never its
        `.raw/.stderr/.keeper` spawn-set kin).
        """
        stem_re = re.compile(rf"{re.escape(name)}-(\d+)-(\d+)\.log")
        try:
            matches = [
                (int(m.group(1)), int(m.group(2)), p)
                for p in self._log_dir.glob(f"{name}-*.log")
                if (m := stem_re.fullmatch(p.name))
            ]
        except OSError:
            return None
        if not matches:
            return None
        return max(matches, key=lambda t: (t[0], t[1]))[2]

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
                except OSError:
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
        self,
        log_path: Path,
        name: str,
        spawn_mode: SpawnMode,
        permission_mode: PermissionMode,
        sandbox: SandboxMode = "default",
    ) -> list[str]:
        """Build the `claude remote-control` argv. Pure (no side effects) so it's unit-testable.

        ``name`` becomes ``--name`` verbatim — the caller (:meth:`_spawn_locked`) has
        already resolved it to either the operator's custom bridge name or the
        project name (#780, via :func:`_normalize_custom_name`).

        ``sandbox`` (#780) adds the OS-level filesystem/network isolation flag:
        ``"on"`` → ``--sandbox``, ``"off"`` → ``--no-sandbox``, ``"default"`` → neither
        (claude's own off-by-default / ``sandbox.*`` settings apply — zero change).
        These are real, documented flags on ``claude remote-control`` (verified on
        claude 2.1.198; see the docs at code.claude.com/docs/en/remote-control).
        """
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
        # Sandbox toggle (#780) — append only for an explicit on/off; "default" leaves it
        # to claude's own setting. Placed before the config-driven flags below, order is
        # immaterial to claude's parser.
        if sandbox == "on":
            cmd += ["--sandbox"]
        elif sandbox == "off":
            cmd += ["--no-sandbox"]
        # Brand auto-generated session names when configured. Multi-session modes only
        # (same-dir/worktree) — `session` is single-session, so the prefix is out of scope.
        if defaults.session_name_prefix and spawn_mode in ("same-dir", "worktree"):
            cmd += ["--remote-control-session-name-prefix", defaults.session_name_prefix]
        # --capacity caps concurrent sessions inside a same-dir/worktree bridge; it does
        # not apply to the single-session `session` spawn mode, so don't pass it there.
        if spawn_mode in ("same-dir", "worktree"):
            cmd += ["--capacity", str(defaults.capacity)]
        # Permanent opt-in observability toggle: detailed connection/session logging
        # for the standard bridge (every spawn mode). Gated on config, never
        # unconditional; off by default. The pty bridge is never passed --verbose.
        if defaults.verbose:
            cmd += ["--verbose"]
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
        (plus, when ``claude.node_from_nvm`` is on, nvm's resolved ``default`` node
        bin dir appended last — see :func:`procutil.resolve_nvm_default_node_bin_dir`
        and issue #792) with any caller ``extra`` (e.g. resume-recap flags). Passing
        it through ``child_env`` re-scrubs Clauster secrets, so config can never
        re-introduce a scrubbed credential name.
        """
        claude = self._config.claude
        path_append = list(claude.path_append)
        if claude.node_from_nvm:
            # Process-memoized (procutil.cached_…): the resolver shells bash (up to its
            # timeout on a slow $NVM_DIR), so the spawn path and the doctor panel share ONE
            # probe rather than re-shelling per spawn/request (#792, Greptile #803/#859).
            nvm_bin_dir = procutil.cached_nvm_default_node_bin_dir()
            if nvm_bin_dir:
                # Appended last, like path_append itself: never overrides a dir
                # already on PATH (e.g. an operator-supplied path_append entry, or
                # an already-resolvable node), only fills a gap.
                path_append.append(nvm_bin_dir)
        return procutil.bridge_env_overlay(path_append=path_append, env=claude.env, extra=extra)

    def _popen(
        self,
        cwd: Path,
        log_path: Path,
        name: str,
        spawn_mode: SpawnMode,
        permission_mode: PermissionMode,
        debug_path: Path | None = None,
        sandbox: SandboxMode = "default",
    ) -> subprocess.Popen:
        # The bridge writes its --debug-file to `debug_path` (the private raw parse-
        # source when on-disk redaction is on); the captured-stderr sibling stays keyed
        # off the public `log_path`. They coincide when redaction is off.
        cmd = self._build_cmd(debug_path or log_path, name, spawn_mode, permission_mode, sandbox)
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
            return subprocess.Popen(  # pragma: skip-on-win
                cmd,
                cwd=str(cwd),
                stdout=err_fh,
                stderr=subprocess.STDOUT,
                env=popen_env,
                start_new_session=True,
            )
        finally:
            err_fh.close()

    # ----- pty / Interactive Session mode (true conversation resume) ------

    def _is_pty_mode(
        self,
        prior: RemoteControlInstance | None = None,
        *,
        requested: str | None = None,
    ) -> bool:
        """Whether the bridge launches under the PTY keeper (Interactive Session).

        A bridge's mode is fixed at first launch. Precedence: an explicit
        *requested* mode (the per-launch picker) wins for a fresh start; else when
        *prior* is given (a resume of an existing instance) its recorded
        ``resume_mode`` wins, so ``stop()`` and ``resume()`` can never disagree
        about the same bridge; else the global ``claude.launch_mode`` seeds a
        brand-new bridge. Without honoring *prior*, editing the config under a
        running/stopped bridge would silently flip its mode on the next resume
        while stop still treated it as the old mode. On Windows the keeper rides a
        ConPTY (pywinpty); without the ``pty`` extra installed it falls back to
        Server Mode (:func:`_conpty_keeper_available`).
        """
        if sys.platform == "win32" and not _conpty_keeper_available():
            return False  # no pywinpty → Server Mode fallback (ConPTY keeper unavailable)
        if requested is not None:
            return requested == "pty"
        if prior is not None:  # pragma: skip-on-win
            return prior.resume_mode == "pty"  # pragma: skip-on-win
        return self._config.claude.launch_mode == "pty"  # pragma: skip-on-win

    @staticmethod
    def _sidecar_path_for(log_path: Path) -> Path:
        """Discovery JSON the keeper writes beside the bridge's --debug-file."""
        return log_path.with_name(log_path.stem + ".keeper.json")  # pragma: skip-on-win

    @staticmethod
    def _screen_sidecar_path_for(log_path: Path) -> Path:
        """Redacted live-screen JSON the keeper writes beside the discovery sidecar (#534)."""
        return pty_screen.screen_sidecar_path(log_path)

    def _build_pty_bridge_argv(
        self,
        log_path: Path,
        name: str,
        permission_mode: PermissionMode,
        *,
        resume: bool,
        resume_session_id: str | None = None,
        worktree_name: str | None = None,
    ) -> list[str]:
        """Build the flag-form bridge argv (`claude --remote-control …`). Pure/testable.

        Unlike the subcommand (`_build_cmd`), the flag form is a single interactive
        session — no `--spawn`/`--capacity`. ``--continue`` (on resume) is what makes
        the restarted session restore its prior conversation context.
        ``resume_session_id`` (#303, fresh spawns only — the ``resume`` revive path
        takes precedence and never carries one, enforced upstream) forks an
        operator-picked PAST conversation into this NEW session:
        ``--resume <uuid> --fork-session`` — fork mints a fresh session id, so the
        picked conversation itself is never clobbered (probed on claude 2.1.211).
        ``worktree_name`` (spawn_mode="worktree", #779) adds ``--worktree <name>`` so
        claude runs the session in its own git worktree under
        ``<repo>/.claude/worktrees/<name>`` — a repeated name REUSES that worktree
        (empirically verified), so the same instance's resume (``--continue`` +
        the same name, stable via its instance_id) restores the conversation IN it.
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
        if worktree_name is not None:
            argv += ["--worktree", worktree_name]
        if resume:
            argv.append("--continue")
        elif resume_session_id is not None:
            argv += ["--resume", resume_session_id, "--fork-session"]
        return argv

    @staticmethod
    def _pty_worktree_name(instance: RemoteControlInstance) -> str | None:
        """Derive the stable per-session worktree name for a worktree-mode pty spawn.

        Derived from the instance_id — which survives stop→resume (a resume revives
        the same identity) — so the revived session lands back in ITS worktree.
        ``None`` for non-worktree spawns (the session runs in the project dir).
        """
        if instance.spawn_mode != "worktree":
            return None
        return f"clauster-{instance.instance_id[:8]}"

    @staticmethod
    def _keeper_launch_cmd(
        sidecar: Path, cwd: Path, bridge_argv: list[str], screen_sidecar: Path | None = None
    ) -> list[str]:
        """Wrap the bridge argv in a PTY-keeper launcher.

        Source/venv: ``<python> -m clauster.pty_keeper …``. A frozen (PyInstaller)
        binary can't use ``-m`` — ``sys.executable`` is the clauster binary, whose
        argparse rejects it — so it re-invokes itself with the hidden
        :data:`~clauster.procutil.KEEPER_SUBCOMMAND` (routed in
        :func:`clauster.__main__.main`, mirroring the recap hook).
        """
        if getattr(sys, "frozen", False):
            launcher = [sys.executable, procutil.KEEPER_SUBCOMMAND]
        else:
            launcher = [sys.executable, "-m", "clauster.pty_keeper"]
        cmd = [
            *launcher,
            "--sidecar",
            str(sidecar),
            "--cwd",
            str(cwd),
        ]
        if screen_sidecar is not None:
            cmd += ["--screen-sidecar", str(screen_sidecar)]
        cmd += ["--", *bridge_argv]
        return cmd

    def _popen_keeper(
        self,
        cwd: Path,
        sidecar: Path,
        bridge_argv: list[str],
        screen_sidecar: Path | None = None,
    ) -> subprocess.Popen:
        """Launch the PTY keeper detached so it outlives a Clauster restart.

        Same detached pattern as the subcommand `_popen` (own session, stdin
        detached, stdout/stderr to a file) — the keeper, not Clauster, holds the
        bridge's terminal, so it survives independently and keeps the bridge alive.
        """
        cmd = self._keeper_launch_cmd(sidecar, cwd, bridge_argv, screen_sidecar)
        keeper_log = sidecar.with_suffix(".log")  # the keeper's own stdout/stderr
        err_fh = keeper_log.open("wb")
        try:
            # Overlay the operator's PATH/env extensions onto the KEEPER's env: the
            # keeper inherits them into its own os.environ and re-emits them (still
            # secret-scrubbed) when it spawns the bridge via child_env(), so the pty
            # bridge gets the same extended PATH/env as the standard path.
            keeper_env = procutil.child_env(self._bridge_env_overlay())
            # Detach the keeper so it outlives a Clauster restart. POSIX: its own session
            # (setsid). Windows: DETACHED_PROCESS drops the shared console so a clauster
            # exit / CTRL can't reach it, plus CREATE_NEW_PROCESS_GROUP for a clean group
            # (start_new_session is a POSIX no-op there).
            if sys.platform == "win32":
                return subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=err_fh,
                    stderr=subprocess.STDOUT,
                    env=keeper_env,
                    creationflags=(
                        subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                    ),
                )
            return subprocess.Popen(  # pragma: skip-on-win
                cmd,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=err_fh,
                stderr=subprocess.STDOUT,
                env=keeper_env,
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
            time.sleep(_READY_POLL_INTERVAL)  # pragma: skip-on-win
        return info  # pragma: skip-on-win

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

    async def _spawn_pty(  # pragma: skip-on-win — pty mode is pywinpty-gated, off on Windows CI
        self,
        instance: RemoteControlInstance,
        proj: Project,
        name: str,
        log_path: Path,
        permission_mode: PermissionMode,
        resume: bool,
        resume_session_id: str | None = None,
    ) -> RemoteControlInstance:
        """Spawn path for `resume_mode == "pty"`: launch the keeper, discover via sidecar."""
        # The sidecar stays keyed off the public log_path; the bridge's --debug-file goes
        # to the private raw parse-source (== log_path unless on-disk redaction is on).
        sidecar = self._sidecar_path_for(log_path)
        # The redacted live-screen tap is opt-in (claude.pty_screen_enabled, #534) and only
        # passed to the keeper when on — off by default, the keeper drains as before with no
        # pyte dependency and no screen sidecar written.
        screen_sidecar = (
            self._screen_sidecar_path_for(log_path)
            if self._config.claude.pty_screen_enabled
            else None
        )
        debug_path = instance.bridge_raw_log_path or log_path
        bridge_argv = self._build_pty_bridge_argv(
            debug_path,
            name,
            permission_mode,
            resume=resume,
            resume_session_id=resume_session_id,
            worktree_name=self._pty_worktree_name(instance),
        )
        try:
            bridge_argv[0] = resolve_binary(bridge_argv[0])
            proc = await asyncio.to_thread(
                self._popen_keeper, proj.path, sidecar, bridge_argv, screen_sidecar
            )
        except (OSError, ClaudeNotFound) as exc:
            _log.warning("pty spawn of %s failed to launch: %s", name, exc)
            instance.status = InstanceStatus.ERROR
            await self._persist()
            return instance
        self._procs[instance.instance_id] = proc
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
            self._start_startup_watch(instance.instance_id)
        return instance

    def _cleanup_keeper(self, pid: int) -> None:
        """Reap the keeper (Clauster's direct child); force it down if it lingers.

        The keeper self-exits once its bridge is gone, so this is usually just a
        reap; the force path covers a keeper that somehow outlives its bridge.
        """
        for _ in range(8):  # ~2s grace for the keeper to follow its bridge out
            procutil.reap_if_exited(pid)
            if procutil.proc_create_time(pid) is None:
                return  # pragma: skip-on-win
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
            if markers.trust_error or markers.poison_reason is not None:
                return markers
            if markers.is_ready:
                # A cold start logs its own "Created initial session" (starter_session_id);
                # a reattach doesn't. Only a reattach can reach the poll loop and then have
                # its re-adopted session torn down as archived/deleted (#671), so give it a
                # bounded grace to surface that poison before we call it RUNNING.
                if markers.starter_session_id is not None:
                    return markers
                grace_deadline = time.monotonic() + _POISON_GRACE
                while time.monotonic() < grace_deadline:
                    time.sleep(_READY_POLL_INTERVAL)
                    if proc.poll() is not None:
                        return self._read_markers(log_path)
                    markers = self._read_markers(log_path)
                    if markers.poison_reason is not None:
                        return markers
                return markers  # grace elapsed clean -> a healthy reattach
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

        if markers.poison_reason is not None:
            # #867 L3: the bridge reached the poll loop but its reattached session was torn
            # down as archived/deleted (#671) — it would sit idle with no usable session.
            # Surface it as ERROR (not a misleading RUNNING); the caller stops the idle
            # bridge and clears the stale pointer so the next launch starts cold.
            instance.status = InstanceStatus.ERROR
        elif markers.is_ready and proc.poll() is None:
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

    async def _heal_poisoned_reattach(
        self,
        instance: RemoteControlInstance,
        proc: subprocess.Popen,
        project_path: Path,
        reason: str,
    ) -> None:
        """Stop a poisoned idle bridge and clear its stale pointer (#867 L3).

        The reattached session was archived/deleted (#671), so the bridge reached its poll
        loop but has no usable session. Record the reason (status is already ERROR), stop
        the idle bridge, then clear ``bridge-pointer.json`` — stop-first so the bridge's own
        shutdown can't out-race the delete — so the next launch registers a fresh session.
        """
        instance.error_detail = (
            f"Could not resume the previous session — it was {reason} and can't be "
            "reattached. Start the session again to begin a fresh one."
        )
        _log.warning(
            "poisoned reattach for project %r: previous session was %s; stopping the idle "
            "bridge and clearing its pointer for a clean restart (#671)",
            instance.project,
            reason,
        )
        self._signal_stop(proc.pid)
        deadline = time.monotonic() + _POISON_STOP_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            await asyncio.sleep(_READY_POLL_INTERVAL)
        else:
            try:
                proc.kill()  # never leave an idle orphan bridge behind
                # Reap + confirm death BEFORE clearing: otherwise clear_pointer's liveness
                # guard can still see the just-killed pid as alive and refuse (a poison loop).
                await asyncio.to_thread(proc.wait)
            except (ProcessLookupError, OSError) as exc:
                _log.debug("force-kill of poisoned bridge %s was a no-op: %s", proc.pid, exc)
        try:
            await asyncio.to_thread(
                pointers.clear_pointer,
                project_path.resolve(),
                claude_projects_dir=self._claude_projects_dir,
            )
        except (pointers.PointerStillLive, OSError) as exc:
            _log.warning(
                "could not clear poisoned bridge-pointer for %r: %s", instance.project, exc
            )

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

    def _start_startup_watch(self, instance_id: str) -> None:
        """Launch (or replace) the background watch for a STARTING bridge."""
        old = self._startup_watches.pop(instance_id, None)
        if old is not None and not old.done():
            old.cancel()
        task = asyncio.create_task(
            self._watch_startup(instance_id), name=f"startup-watch:{instance_id}"
        )
        self._startup_watches[instance_id] = task

        def _done(t: asyncio.Task, _iid: str = instance_id) -> None:
            if self._startup_watches.get(_iid) is t:
                self._startup_watches.pop(_iid, None)
            if not t.cancelled() and (exc := t.exception()) is not None:
                _log.warning("startup-watch for %s failed: %s", _iid, exc)

        task.add_done_callback(_done)

    async def _watch_startup(self, instance_id: str) -> None:
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
            instance = self._instances.get(instance_id)
            proc = self._procs.get(instance_id)
            if instance is None or proc is None or instance.status is not InstanceStatus.STARTING:
                return  # already resolved, stopped, or gone
            if proc.poll() is not None:  # exited during startup
                self._reconcile_status(instance, alive=False)
                await self._persist()
                return
            log_path = instance.bridge_debug_log_path
            if log_path is None:
                return  # nothing to read from; leave it for the poll loop
            if instance.resume_mode == "pty":  # pragma: skip-on-win — pty-mode (pywinpty-gated)
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
                    await self._post_spawn_enrich(
                        instance, self._project_path(instance.project) or log_path
                    )
                    await self._persist()
                    return
            if time.monotonic() >= deadline:
                instance.status = InstanceStatus.ERROR
                _log.warning(
                    "bridge %s (%s) is alive but never registered an environment within %.0fs; "
                    "marking ERROR (it is not connectable). Check the bridge debug log — a "
                    "common cause is the claude user lacking readable remote-control credentials.",
                    instance.project,
                    instance_id,
                    grace,
                )
                await asyncio.to_thread(self._capture_error_detail, instance)
                await self._persist()
                return

    # ----- stop -----------------------------------------------------------

    async def stop(self, instance_id: str) -> RemoteControlInstance:
        """Signal a managed bridge to shut down and mark the stop as intentional."""
        # Look up the instance first (outside any lock) to get the project name for the lock.
        instance = self._instances.get(instance_id)
        if instance is None:
            raise UnknownProject(f"no managed instance: {instance_id!r}")
        project_name = instance.project
        # Serialize against an in-flight spawn() for this project. Without the lock, stop() can
        # read bridge_pid=None while _spawn_locked is suspended in to_thread(_popen), mark the
        # instance STOPPED, and return — orphaning the bridge spawn is about to start tracking.
        # Taking the same per-project lock spawn()/forget()/resume() use makes stop() wait for an
        # in-flight spawn to publish bridge_pid before reading it. No deadlock: stop() has no
        # internal callers and nothing it awaits re-takes this lock. Look the instance up INSIDE
        # the lock (like forget()) so a concurrent forget() can't de-register it between the
        # lookup and the signalling.
        async with self._spawn_lock_for(project_name), self._bridge_flock(project_name):
            instance = self._instances.get(instance_id)
            if instance is None:
                raise UnknownProject(f"no managed instance: {instance_id!r}")
            # Stop racing the startup watch over this instance's status.
            self._cancel_startup_watch(instance_id)
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
                self._procs.pop(instance_id, None)  # release dead Popen handle; resume re-adds it
                self._emit_lifecycle("stop", instance)
                return instance

            # Re-validate identity immediately before signalling (TOCTOU / PID reuse).
            if await asyncio.to_thread(procutil.is_live_bridge, pid, instance.bridge_proc_start):
                # The flag-form (pty) bridge's TUI treats the first SIGINT as "press
                # again to exit"; a second confirms. The subcommand bridge stops on one.
                twice = instance.resume_mode == "pty"
                await asyncio.to_thread(self._signal_stop, pid, twice=twice)
                await self._await_exit(project_name, pid, instance.bridge_proc_start)
            if keeper_pid is not None:  # pragma: skip-on-win
                await asyncio.to_thread(self._cleanup_keeper, keeper_pid)
            instance.status = InstanceStatus.STOPPED
            self._procs.pop(instance_id, None)  # release dead Popen handle; resume re-adds it
            self._emit_lifecycle("stop", instance)
            return instance

    async def forget(self, instance_id: str) -> None:
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
        # Determine project name before taking the lock (needed for per-project lock and
        # the pointer clear below). Fall back to the persisted record — a forgotten bridge
        # may live only in state.json, not the in-memory registry — where the value is a
        # serialized dict keyed by "project_name" (#777), not a RemoteControlInstance.
        instance = self._instances.get(instance_id)
        if instance is not None:
            project_name = instance.project
        else:
            persisted = self._persisted.get(instance_id)
            project_name = persisted.get("project_name") if persisted is not None else None
        # Hold the per-project spawn lock so a concurrent spawn()/resume() can't
        # repopulate _instances/_procs between the liveness check and the pop() —
        # forgetting must never remove tracking for a just-spawned live process.
        lock_name = project_name or instance_id  # fall back to instance_id if not in registry
        async with self._spawn_lock_for(lock_name), self._bridge_flock(lock_name):
            # Refresh the merge base FIRST (#949): forget is the one path that DELETES a
            # row, and it does so by re-saving a filtered map — so the map must start
            # from the CURRENT DB state, not this process's construction-time snapshot.
            # A stale base here would resurrect rows another process already pruned, or
            # prune rows another process added since. Also makes the not-found check
            # below honest: a row another process already forgot raises UnknownProject.
            # (If this refresh degrades on a transient DB read error — warn + keep the
            # old base — the prune below operates on that older base: strictly better
            # than {} would be, bounded to the error window, and the matching save
            # almost certainly fails best-effort too.)
            await self._refresh_persisted()
            instance = self._instances.get(instance_id)
            if instance is None and instance_id not in self._persisted:
                raise UnknownProject(f"no managed instance: {instance_id!r}")
            if instance is None and project_name is None:
                # Re-derive from the REFRESHED base: the pre-lock read above served only
                # to pick the lock key and used the construction-time snapshot, which may
                # predate this record (#949) — without this, the pointer clear below would
                # be silently skipped for a record another process persisted since.
                persisted = self._persisted.get(instance_id)
                project_name = persisted.get("project_name") if persisted is not None else None
            if instance is not None:
                if instance.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING):
                    raise InstanceStillLive(
                        f"{instance_id!r} is {instance.status.value} — Stop it before forgetting"
                    )
                # Defense in depth: never drop a record whose process is actually alive even
                # if the status lags a missed poll — that would orphan a live bridge/keeper.
                if instance.bridge_pid is not None and await asyncio.to_thread(
                    procutil.is_live_bridge, instance.bridge_pid, instance.bridge_proc_start
                ):
                    raise InstanceStillLive(
                        f"{instance_id!r} still has a live bridge — Stop it first"
                    )
                if (
                    instance.keeper_pid is not None
                    and await asyncio.to_thread(procutil.proc_create_time, instance.keeper_pid)
                    is not None
                ):
                    raise InstanceStillLive(
                        f"{instance_id!r} still has a live keeper — Stop it first"
                    )
                self._instances.pop(instance_id, None)
                self._procs.pop(instance_id, None)
            # Rebuild as a NEW dict rather than .pop() in place: _persist aliases _persisted
            # and _last_saved to the same object, so mutating _persisted would also mutate the
            # dedup baseline and _persist would skip the write (leaving the row on disk).
            self._persisted = {k: v for k, v in self._persisted.items() if k != instance_id}
            # drop=… — the deletion must ride INSIDE the persist's own store-locked
            # refresh→save: _persist re-loads the base (which still holds the row), so
            # a bare filtered save computed out here could race another process or be
            # undone by the reload. The in-memory filter above keeps this process's
            # view consistent even when the best-effort save doesn't land.
            await self._persist(drop=instance_id)
            # #867 L1: a forgotten bridge's bridge-pointer.json would otherwise be
            # reattached on the next spawn — reviving an anchor that may have been
            # archived/deleted out from under its env (the #671 dead-end). Clear it so the
            # next start registers a clean session. Best-effort and never fatal to forget:
            # a live pointer is left in place (clear_pointer guards it), and a filesystem
            # hiccup is logged, not raised — the record is already dropped either way.
            if project_name is not None and is_valid_project_name(project_name):
                # Resolve to the absolute path the bridge actually ran in: the CLI keys the
                # pointer directory off the process's real (absolute) cwd, so a *relative*
                # projects_root would otherwise sanitize to the wrong directory and silently
                # miss the pointer (Greptile #868 P1).
                project_path = (self._config.projects_root / project_name).resolve()
                try:
                    await asyncio.to_thread(
                        pointers.clear_pointer,
                        project_path,
                        claude_projects_dir=self._claude_projects_dir,
                    )
                except pointers.PointerStillLive:
                    _log.warning(
                        "forget(%s): bridge-pointer still live despite a stopped record; "
                        "leaving it in place",
                        instance_id,
                    )
                except OSError as exc:
                    _log.warning(
                        "forget(%s): could not clear bridge-pointer: %s", instance_id, exc
                    )

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

    def _persisted_for_project(self, project_name: str) -> tuple[str, dict] | None:
        """Return ``(instance_id, fields)`` for the first persisted record for ``project_name``.

        Since issue 777 ``_persisted`` is keyed by ``instance_id``; the project name lives
        in the ``"project_name"`` field of each value dict.  Returns ``None`` when no match.
        Used by :meth:`_stopped_from_persisted` and :meth:`_reattach_pty_from_sidecar` to
        look up a persisted record by project without assuming a project-keyed dict.
        """
        for iid, fields in self._persisted.items():
            if fields.get("project_name") == project_name:
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

    @staticmethod
    def _saved_sandbox(saved: dict) -> SandboxMode:
        """Coerce a persisted ``sandbox_mode`` against the allowed set (#780).

        Absent (pre-#780 state.json) or corrupt values fall back to ``"default"`` —
        the safe no-flag behavior — so a rebuilt STOPPED card offers the same sandbox
        choice on resume that the original launch used, without failing the model on a
        hand-edited value.
        """
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
            # The process is gone: no pid/keeper/env to recover. intentional_stop is
            # carried through (a host-down bridge has it False — "interrupted" — vs a
            # deliberate Stop's True); both render as a resumable STOPPED card.
            intentional_stop=bool(saved.get("intentional_stop", False)),
            status=InstanceStatus.STOPPED,
            bridge_pid=None,
            bridge_proc_start=None,
            keeper_pid=None,
        )

    def _reattach_pty_from_sidecar(
        self, name: str, saved: dict, instance_id: str | None = None
    ) -> RemoteControlInstance | None:
        """Reattach a self-spawned pty bridge from its keeper sidecar after a restart.

        A pty (flag-form ``claude --remote-control``) bridge writes no Anthropic
        ``bridge-pointer.json``, so the pointer-walk in :meth:`rediscover` can't see
        it — but its keeper is detached and outlives the restart, recording the
        keeper/bridge pids in the sidecar. Without this, rediscover falls through to
        ``_stopped_from_persisted`` and the card reads STOPPED while a live keeper
        leaks: uncontrollable (Stop/observe gone), and a Resume would spawn a *second*
        keeper. Glob the project's sidecars newest-first and, when one names a still-
        live keeper (``is_keeper_process`` — cmdline-gated against PID reuse) holding a
        ready, live bridge (pid + proc-start matched), rebuild it as a managed RUNNING
        instance so stop()/poll_once own it again.

        Returns ``None`` when nothing is reattachable (no persisted record, not pty, or
        no live keeper) — rediscover then resurrects the STOPPED card as before. Only a
        sidecar in the ``"ready"`` state reattaches; a bridge still mid-startup falls
        back to STOPPED (the orphan-keeper sweep can reap a genuinely stuck one).
        """
        if not saved:
            return None
        spawn_mode, permission_mode, resume_mode = self._saved_modes(saved)
        if resume_mode != "pty":
            return None
        for sidecar in sorted(self._log_dir.glob(f"{name}-*.keeper.json"), reverse=True):
            info = self._read_sidecar(sidecar)
            if not info or info.get("state") != "ready":
                continue
            keeper_pid = info.get("keeper_pid")
            bridge_pid = info.get("bridge_pid")
            if not (
                isinstance(keeper_pid, int)
                and not isinstance(keeper_pid, bool)
                and isinstance(bridge_pid, int)
                and not isinstance(bridge_pid, bool)
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
            if not procutil.is_live_bridge(bridge_pid, bridge_proc_start):
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
            kwargs: dict = dict(
                project=name,
                label=saved.get("label") or name,
                spawn_mode=spawn_mode,
                permission_mode=permission_mode,
                resume_mode=resume_mode,
                status=InstanceStatus.RUNNING,
                intentional_stop=False,
                keeper_pid=keeper_pid,
                bridge_pid=bridge_pid,
                bridge_proc_start=bridge_proc_start,
                bridge_debug_log_path=log_path,
                bridge_raw_log_path=tail_source,
                starter_session_id=info.get("session_id") or None,
                url=info.get("connect_url") or None,
            )
            if instance_id is not None:
                kwargs["instance_id"] = instance_id
            return RemoteControlInstance(**kwargs)
        return None

    async def rediscover(self, *, persist: bool = True) -> None:
        """Re-detect bridges after a restart: reattach live ones, resurrect dead ones.

        A bridge found *alive* is reattached as RUNNING. A discovered project whose
        bridge is gone but which has a persisted record (its process died while
        Clauster was down — e.g. a host reboot) is resurrected as a STOPPED,
        resumable card instead of being dropped; one with no persisted record is
        left absent (nothing to resume).

        ``persist=False`` reattaches into the in-memory registry only and skips the
        trailing state write — the read-only mode the headless CLI (#775) uses so a
        ``clauster status`` never clobbers the running service's shared ``state.json``.
        """
        # Fresh merge base (#949): the reattach/resurrection reads below consume
        # ``_persisted``, and a headless runner calls this as its hydrate step — its
        # construction-time snapshot may already lag the live service's store.
        await self._refresh_persisted()
        for proj in self._discovered().values():
            if self.get_instance_for_project(proj.name) is not None:
                continue
            ptr = await asyncio.to_thread(pointers.pointer_for_project, proj.path)
            if ptr is None or not await asyncio.to_thread(pointers.is_live, ptr):
                # A pty (flag-form) bridge writes no Anthropic pointer, yet its
                # detached keeper outlives the restart. Reattach it from the keeper
                # sidecar so a live keeper is re-managed (Stop/observe restored)
                # rather than leaking behind a STOPPED card; fall through to the
                # STOPPED resurrection when no live keeper remains.
                persisted_hit = self._persisted_for_project(proj.name)
                persisted_saved = persisted_hit[1] if persisted_hit is not None else {}
                persisted_iid = persisted_hit[0] if persisted_hit is not None else None
                reattached = await asyncio.to_thread(
                    self._reattach_pty_from_sidecar,
                    proj.name,
                    persisted_saved,
                    persisted_iid,
                )
                if reattached is not None:
                    self._instances[reattached.instance_id] = reattached
                elif (stopped := self._stopped_from_persisted(proj.name)) is not None:
                    self._instances[stopped.instance_id] = stopped
                continue
            # Overlay the few fields the pointer-walk can't recover; a bridge
            # found alive is by definition NOT intentionally stopped.
            persisted_hit = self._persisted_for_project(proj.name)
            saved = persisted_hit[1] if persisted_hit is not None else {}
            persisted_iid = persisted_hit[0] if persisted_hit is not None else None
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
            # Re-bind the live tail to the log this survivor was already writing before the
            # restart, so `/ws/bridge-log` resolves a real path instead of 1008-ing (#584).
            log_path = await asyncio.to_thread(self._latest_debug_log_for, proj.name)
            instance = self._instance_from_pointer(
                proj.name,
                ptr,
                label=saved.get("label") or proj.name,
                spawn_mode=spawn_mode,
                permission_mode=permission_mode,
                resume_mode=resume_mode,
                bridge_proc_start=bridge_proc_start,
                keeper_pid=keeper_pid,
                bridge_debug_log_path=log_path,
                bridge_raw_log_path=(
                    self._raw_log_path_for(log_path) if log_path is not None else None
                ),
                instance_id=persisted_iid,
            )
            self._instances[instance.instance_id] = instance
        if persist:
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
        bridge_debug_log_path: Path | None = None,
        bridge_raw_log_path: Path | None = None,
        instance_id: str | None = None,
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
        """
        kwargs: dict = dict(
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
        async with self._spawn_lock_for(name), self._bridge_flock(name):
            if self.get_instance_for_project(name) is not None:
                raise InstanceStillLive(f"{name!r} is already managed — nothing to adopt")
            proj = self._discovered().get(name)
            if proj is None:
                raise UnknownProject(f"no such project: {name!r}")
            # Fresh merge base (#949) so the saved label/modes read by the reattach
            # come from the store as it is now, and the trailing persist can't
            # resurrect/prune rows another process changed since construction.
            await self._refresh_persisted()
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
        instance = self._instance_from_pointer(
            proj.name,
            ptr,
            label=saved.get("label") or proj.name,
            spawn_mode=spawn_mode,
            permission_mode=permission_mode,
            resume_mode="standard",
            bridge_proc_start=procutil._expected_epoch(ptr.proc_start),
            keeper_pid=None,
        )
        self._instances[instance.instance_id] = instance
        await self._persist()
        return instance

    def adoptable_external_projects(self) -> set[str]:
        """Project names whose live EXTERNAL session is a *standard* bridge safe to adopt.

        A standard external bridge writes an Anthropic pointer whose pid is a live
        ``claude remote-control`` subcommand process; a pty (flag-form) external bridge
        is excluded (unsafe to adopt — see :meth:`adopt`), as is one whose pointer has
        gone stale. Computed from the same pointer + cmdline + cwd-attribution checks
        :meth:`adopt` enforces, so the dashboard's Adopt affordance can never offer an
        adoption that :meth:`adopt` would then refuse. Synchronous (filesystem +
        ``psutil``); call it off-loop.
        """
        discovered = self._discovered()
        adoptable: set[str] = set()
        for name in self.external_sessions_by_project():
            proj = discovered.get(name)
            if proj is None:
                continue
            ptr = pointers.pointer_for_project(proj.path)
            if ptr is None or not procutil.is_live_standard_bridge(ptr.pid, ptr.proc_start):
                continue
            # Mirror adopt()'s positive-attribution gate (#951): a sanitize-collided
            # foreign project's bridge must not be advertised as adoptable only for
            # every resulting Adopt click to 409.
            cwd = procutil.proc_cwd(ptr.pid)
            if cwd is not None and cwd.resolve() == proj.path.resolve():
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
                # The crash notification is now fired inside _emit_lifecycle (#541), so
                # the single chokepoint owns history + webhook + notification together.
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
        managed = {
            Path(discovered[i.project].path): i.project
            for i in self._instances.values()
            if i.project in discovered
            and (i.project in live_projects or i.status is InstanceStatus.STARTING)
        }
        # A worktree-spawn bridge runs each session in a per-session worktree under
        # `<root>/.claude/worktrees/` (`claude remote-control --spawn worktree`), so the
        # session cwd never exactly matches the project-root key above — without this the
        # session reads EXTERNAL and the dashboard shows no live-session count for the
        # bridge. Reconcile attributes such a session to its bridge by containment in
        # that worktree subtree.
        worktree_roots = {
            Path(discovered[i.project].path): i.project
            for i in self._instances.values()
            if i.project in discovered
            and (i.project in live_projects or i.status is InstanceStatus.STARTING)
            and i.spawn_mode == "worktree"
        }
        # Clauster's own hosted (claustrum) sessions run no bridge process, so the
        # cross-check would otherwise see their live `claude` pid and label it
        # EXTERNAL/unmanaged (#592). Claim them by the CT-1 agent_pid (authoritative)
        # and, for a pre-CT-1 daemon with no pid, by their workspace cwd. Only RUNNING/
        # STARTING rows are claimed: a stopped row's pid could be reused by an unrelated
        # process, and a stopped session has no live process to attribute anyway.
        hosted_pids: dict[int, str] = {}
        hosted_cwds: dict[Path, str] = {}
        if self._hosted_instances is not None:
            for inst in self._hosted_instances():
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
        # reused). Roots are UNIONED per cwd, not last-wins: a standard and a pty bridge
        # (or N pty) can be co-located at one project root (mode-independence note above),
        # each owning distinct worker pids — last-wins would flip the other's genuine
        # children to EXTERNAL.
        roots_by_cwd: dict[Path, tuple[int, ...]] = {}
        for i in self._instances.values():
            if i.project in discovered and (
                i.project in live_projects or i.status is InstanceStatus.STARTING
            ):
                roots = tuple(p for p in (i.bridge_pid, i.keeper_pid) if p is not None)
                if roots:
                    cwd = Path(discovered[i.project].path)
                    roots_by_cwd[cwd] = roots_by_cwd.get(cwd, ()) + roots
        # `owned_pids` returns the roots plus their readable descendants — the roots
        # themselves are owned because a single-session flag-form pty
        # (`claude --remote-control`) can report its `agents --json` pid as the bridge
        # process itself (in-process), and a reattached pty with a rotated/missing keeper
        # contributes only bridge_pid. A root whose tree can't be READ (AccessDenied:
        # hardened /proc, hidepid, restricted container) contributes only its own pid, so
        # a keyed cwd always gates: a session that isn't provably owned reads EXTERNAL,
        # never silently re-enabling the cwd-only join #820 removed. The gate stays on
        # for a cwd with any known pid; only a bridge with no resolvable pid yet (STARTING
        # pty pre-sidecar) is absent from roots_by_cwd → cwd-only (#713 window). psutil
        # walk → to_thread.
        owned_pids_by_cwd = await asyncio.to_thread(
            lambda: {cwd: procutil.owned_pids(roots) for cwd, roots in roots_by_cwd.items()}
        )
        self._sessions = inspector.reconcile(
            sessions, managed, hosted_pids, hosted_cwds, worktree_roots, owned_pids_by_cwd
        )
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

    @staticmethod
    def _notify_message(event: str, instance: RemoteControlInstance) -> tuple[str, str]:
        """Build the (title, body) for a bridge-lifecycle notification ``event`` (#541)."""
        mode = f"{instance.resume_mode}/{instance.spawn_mode}"
        proj = repr(instance.project)
        bodies = {
            "crash": (
                f"clauster: bridge crashed — {instance.label}",
                f"The bridge for project {proj} exited unexpectedly (not via Stop) — mode {mode}.",
            ),
            "ready": (
                f"clauster: bridge ready — {instance.label}",
                f"The bridge for project {proj} finished starting — mode {mode}.",
            ),
            "stop": (
                f"clauster: bridge stopped — {instance.label}",
                f"The bridge for project {proj} was stopped — mode {mode}.",
            ),
            "session-ended": (
                f"clauster: session ended — {instance.label}",
                f"The session for project {proj} ended — mode {mode}.",
            ),
            "reconnect-failed": (
                f"clauster: reconnect failed — {instance.label}",
                f"Resuming the bridge for project {proj} failed — mode {mode}.",
            ),
        }
        return bodies.get(
            event,
            (f"clauster: {event} — {instance.label}", f"Project {proj} — mode {mode}."),
        )

    def _notify_event(self, event: str, instance: RemoteControlInstance) -> None:
        """Fire a best-effort lifecycle notification (off-loop; never blocks/raises, #541).

        ``event`` is a key of :data:`~clauster.config._NOTIFY_EVENTS`. No-op unless the
        outbound notifier is active AND this event's per-event toggle is on. Routes
        through the same fire-and-forget Apprise path the crash alert always used.
        """
        if not self._notifier.active or not self._config.notifications.event_enabled(event):
            return
        title, body = self._notify_message(event, instance)
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
        """Single chokepoint for a lifecycle transition: history + webhook + notification.

        ``event`` is one of ``spawn`` / ``ready`` / ``stop`` / ``crash``. All three sinks
        are best-effort and off the loop, so none can affect the bridge lifecycle: the
        history append is fail-closed (a lost row is logged, never raised), the webhook is
        fail-open (a broken endpoint is swallowed), and the notification is fire-and-forget.

        Notifications use a finer-grained event taxonomy than webhooks (#541): a ``stop``
        for a single-shot ``session`` bridge that wasn't ended via the Stop button is a
        ``session-ended`` notification, not a ``stop``. ``spawn`` carries no notification.
        """
        self._record_event(event, instance)
        self._emit_webhook(event, instance)
        notify_event = event
        if event == "stop" and instance.spawn_mode == "session" and not instance.intentional_stop:
            notify_event = "session-ended"
        if notify_event != "spawn":
            self._notify_event(notify_event, instance)

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

        Fail-closed end-to-end: the cheap in-memory prologue, the off-loop cost
        snapshot, and the append are all logged-and-swallowed, on both the async and
        the synchronous path, so a history hiccup never raises into the spawn/stop/
        crash caller. The cost-snapshot's project-path lookup + transcript read both
        live inside ``_usage_snapshot`` so a filesystem hiccup degrades the *cost* to
        null without dropping the row itself — the terminal row is always recorded.
        """
        kind = self._HISTORY_KIND.get(event)
        if kind is None:  # unknown event name — never persist a bogus kind
            return

        def _append(
            project: str, mode: str, session_ref: str | None, usage: ProjectUsage | None
        ) -> None:
            totals = usage.totals if usage is not None else None
            cost = (
                CostSnapshot(
                    cost_usd=usage.cost_usd(),
                    input_tokens=totals.input if totals is not None else None,
                    output_tokens=totals.output if totals is not None else None,
                    cache_creation_tokens=totals.cache_creation if totals is not None else None,
                    cache_read_tokens=totals.cache_read if totals is not None else None,
                )
                if usage is not None
                else CostSnapshot()
            )
            self._history.append(
                project_name=project,
                mode=mode,
                kind=kind,
                session_ref=session_ref,
                cost=cost,
            )

        try:
            # "hosted" sessions run on the claustrum channel; otherwise the resume axis
            # (standard remote-control vs the pty keeper) is the mode worth recording.
            # Fall back to "standard" if the resume axis is somehow unresolved: ``mode``
            # is NOT NULL, so a None here would make the INSERT drop the row entirely.
            mode = (
                "hosted" if instance.channel == "hosted" else (instance.resume_mode or "standard")
            )
            project = instance.project
            # Snapshot the loop-owned values now; the off-loop task only touches locals.
            session_ref = _hash_session_ref(instance.starter_session_id, self._session_ref_key())
            terminal = kind in ("ended", "crashed")
        except Exception as exc:  # noqa: BLE001 — history must never break the lifecycle
            _log.warning(
                "could not prepare session event (%s/%s): %s", instance.project, kind, exc
            )
            return

        def _usage_snapshot() -> ProjectUsage | None:
            """Cost/token rollup for a terminal row, or None (non-terminal / unreadable).

            The project-path lookup (``_project_path`` walks the filesystem) and the
            transcript read both live here, so a discovery / transcript I/O error
            degrades the *cost* to null — the terminal row is still written by the
            caller. This is the documented "an unreadable transcript must not drop the
            terminal row" invariant: only the cost is best-effort, never the row.
            """
            if not terminal:
                return None
            try:
                project_path = self._project_path(project)
                if project_path is None:
                    return None
                return aggregate_project_usage_cached(
                    project_path,
                    project_name=project,
                    claude_projects_dir=self._claude_projects_dir,
                )
            except Exception as exc:  # noqa: BLE001 — cost is best-effort; the row is not
                # ANY snapshot failure (an unreadable transcript / discovery walk → OSError,
                # or a malformed-transcript parse → ValueError, etc.) must degrade only the
                # cost to null — never drop the terminal row. The caller still appends it.
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

    def emit_event(self, event: str, payload: dict) -> None:
        """Fire a non-bridge lifecycle webhook off-loop (fire-and-forget, fail-open, #432).

        The runner owns the single :class:`WebhookEmitter`, so subsystems that don't
        hold one (the bg-agent supervisor, the hosted manager, the clone manager) route
        their lifecycle events through here. ``event`` is a #432 key — ``bg-settled`` /
        ``permission-needed`` / ``clone-done`` — and ``payload`` is an already-redacted,
        event-shaped dict (NOT the bridge ``RemoteControlInstance`` shape). No-op unless
        webhooks are active and this event is enabled (these default OFF). The POST is
        fire-and-forget and fail-open — it can never block or break the caller's path.

        Must be called on the event loop (it schedules a task). Callers off the loop
        (e.g. the threaded supervisor stop) marshal via ``loop.call_soon_threadsafe``.
        """
        if not self._webhooks.wants(event):
            return
        task = asyncio.create_task(self._webhooks.aemit(event, payload))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    def notify_app_event(self, event: str, title: str, body: str) -> None:
        """Fire a non-bridge notification off-loop (fire-and-forget, fail-closed, #541).

        Mirrors :meth:`emit_event` for the *notification* channel: subsystems that
        don't hold the runner's :class:`Notifier` (the hosted manager's parked-prompt
        callback, the resume route) route an event whose source isn't a bridge
        ``RemoteControlInstance`` — e.g. ``permission-needed`` / ``reconnect-failed`` —
        through here with a ready-made title/body. No-op unless the outbound notifier is
        active and this event's per-event toggle is on (these default OFF). The send is
        fire-and-forget and swallows its own errors, so it never affects the caller.

        Must be called on the event loop (it schedules a task).
        """
        if not self._notifier.active or not self._config.notifications.event_enabled(event):
            return
        task = asyncio.create_task(self._notifier.anotify(title, body))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    # ----- lifecycle ------------------------------------------------------

    async def _prune_stale_pointers(self) -> None:
        """GC clauster's own long-dead ``bridge-pointer.json`` files at startup (#867 L4).

        Scoped to projects under ``projects_root`` (clauster's own data — never a pointer
        another tool wrote), and only a pointer that is BOTH non-live AND older than
        :data:`_STALE_POINTER_TTL_SECONDS`, so a live or recently-stopped-resumable session
        keeps its reattach. Best-effort: a listing/stat/delete error is logged, never fatal
        to startup. Runs AFTER :meth:`rediscover` so any live bridge is already adopted.
        """
        try:
            projects = await asyncio.to_thread(
                discover_projects_cached, self._config.projects_root, self._claude_json
            )
        except OSError as exc:
            _log.warning("stale-pointer prune skipped: could not list projects: %s", exc)
            return
        cutoff = time.time() - _STALE_POINTER_TTL_SECONDS
        for proj in projects:
            try:
                await asyncio.to_thread(self._prune_one_pointer, proj.path, cutoff)
            except Exception:
                # Best-effort hygiene: one project's failure (e.g. a resolve() symlink loop)
                # must never abort the GC or startup — mirror the poll loop's resilience.
                _log.exception("stale-pointer prune failed for %s; continuing", proj.path)

    def _prune_one_pointer(self, project_path: Path, cutoff: float) -> None:
        """Clear one project's pointer if it's non-live and its file mtime predates ``cutoff``."""
        resolved = project_path.resolve()
        # Ownership guard: a symlink under projects_root can resolve OUTSIDE it; the canonical
        # target's pointer is not clauster's to GC, so never prune a path that escapes the root.
        if not resolved.is_relative_to(self._config.projects_root.resolve()):
            return
        path = pointers.pointer_path_for(resolved, self._claude_projects_dir)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return  # no pointer -> nothing to prune
        except OSError as exc:
            _log.warning("could not stat bridge-pointer for %s: %s", resolved, exc)
            return
        if mtime >= cutoff:
            return  # recent enough that a resume may still want it
        try:
            # backup=False: a 2-week-dead pointer isn't worth a .bak that would itself linger.
            if pointers.clear_pointer(
                resolved, claude_projects_dir=self._claude_projects_dir, backup=False
            ):
                _log.info("pruned stale non-live bridge-pointer for %s", resolved)
        except pointers.PointerStillLive:
            pass  # became live between the stat and the clear -> leave it
        except OSError as exc:
            _log.warning("could not prune bridge-pointer for %s: %s", resolved, exc)

    async def start_poll_loop(self) -> None:
        """Rediscover already-running bridges, then start the background poll loop."""
        await self.rediscover()
        # #867 L4: after live bridges are adopted, GC long-dead pointers (hygiene).
        await self._prune_stale_pointers()
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

    def _cancel_startup_watch(self, instance_id: str) -> None:
        task = self._startup_watches.pop(instance_id, None)
        if task is not None and not task.done():  # pragma: skip-on-win
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
        # Drain any in-flight fire-and-forget notify sends. Without this a pending
        # anotify is GC-cancelled at interpreter exit ("Task was destroyed but it is
        # pending"). Snapshot first (the done-callback mutates the set), let them finish
        # within a short grace (don't block shutdown on a slow notifier), and swallow
        # every per-task error — a notification failure must never fail shutdown.
        pending = [t for t in self._notify_tasks if not t.done()]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=_NOTIFY_DRAIN_GRACE,
                )
            except TimeoutError:
                for task in pending:
                    task.cancel()
                # Reap the cancelled stragglers so none is GC'd while still pending on
                # the timeout path. Cancelling an I/O-awaiting send completes promptly,
                # so this needs no further timeout.
                await asyncio.gather(*pending, return_exceptions=True)
