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
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import unicodedata
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    atomicio,
    bridge_launch,
    bridge_log,
    bridge_prune,
    code_sessions,
    metrics,
    pointers,
    poll_loop,
    procutil,
    pty_screen,
    record_facade,
    redact,
    rediscovery,
    runner_state,
    spawn_coordinator,
)

# Re-exported so ``clauster.runner.auth.load_or_create_secret`` monkeypatches keep landing
# after ``_session_ref_key`` moved to ``record_facade`` (#1157). ``RecordFacade`` holds the
# live caller (via its own ``from . import auth``, the same module object); the runner itself
# no longer uses ``auth`` — it re-exports it only so the ``clauster.runner.auth`` patch path
# still resolves.
from . import auth as auth

# Re-exported so ``from clauster.runner import _STALE_POINTER_TTL_SECONDS`` (used by the
# tests) still resolves after ``_prune_stale_pointers`` moved to ``bridge_prune`` (#1157).
from .bridge_prune import _STALE_POINTER_TTL_SECONDS as _STALE_POINTER_TTL_SECONDS

# Re-exported so ``clauster.runner.ClaudeNotFound`` / ``clauster.runner.resolve_binary`` keep
# resolving after the spawn/pty launch path (their only callers) moved to
# ``spawn_coordinator`` (#1157). The coordinator imports them from ``.claude_cli`` directly;
# ``SpawnCoordinator`` raises/catches them there.
from .claude_cli import ClaudeNotFound as ClaudeNotFound
from .claude_cli import resolve_binary as resolve_binary
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
from .discovery import (
    discover_projects_cached,
    invalidate_discovery_cache,
    is_valid_project_name,
)

# Re-exported so ``from clauster.runner import _row_int`` (and the other row / pointer /
# sidecar field decoders the tests reach — ``_row_float``, ``_row_str``, ``_sidecar_notice``,
# ``_NOTICE_MAX_CHARS``) still resolves after they moved to ``field_decode`` (#1157). The
# still-on-runner ``_persisted_liveness`` and poll-loop promotion call the same one copy; the
# reattach/adopt/rediscover methods in ``rediscovery`` import it too, so both share it without
# a circular import.
from .field_decode import (
    _NOTICE_MAX_CHARS as _NOTICE_MAX_CHARS,
)
from .field_decode import (
    _pointer_start_ticks as _pointer_start_ticks,
)
from .field_decode import (
    _row_float as _row_float,
)
from .field_decode import (
    _row_int as _row_int,
)
from .field_decode import (
    _row_str as _row_str,
)
from .field_decode import (
    _sidecar_notice as _sidecar_notice,
)
from .field_decode import (
    _ticks_on_exact_match as _ticks_on_exact_match,
)
from .models import (
    Attribution,
    InstanceStatus,
    Project,
    RemoteControlInstance,
    TrustState,
    WorkingSession,
)
from .notify import Notifier

# Re-exported so ``clauster.runner.ensure_recap_hook_installed`` keeps resolving after its
# only caller (``_ensure_claude_side_settings``) moved to ``spawn_coordinator`` (#1157).
from .recap import ensure_recap_hook_installed as ensure_recap_hook_installed

# Re-exported so ``from clauster.runner import _release_flock_if_acquired`` (used by the
# cross-process flock tests) still resolves after the flock machinery moved to
# ``runner_state`` (#1157). ``RunnerState._flock`` holds the live caller.
from .runner_state import _release_flock_if_acquired as _release_flock_if_acquired

# Re-exported so ``from clauster.runner import _READY_TIMEOUT`` (and the other three the tests
# reach) still resolves after the readiness/startup-watch path moved to ``spawn_coordinator``
# (#1157). The still-on-runner ``_heal_poisoned_reattach`` reads ``_READY_POLL_INTERVAL`` from
# this re-export. ⚠️ A test that patches the value the SPAWN PATH reads must target
# ``clauster.spawn_coordinator.<name>``, not ``clauster.runner.<name>`` — the bare-name
# namespace trap: rebinding this runner-module alias does not reach the coordinator's own
# module global that ``_await_ready`` / ``_await_ready_pty`` / ``_watch_startup`` read.
from .spawn_coordinator import _POISON_GRACE as _POISON_GRACE
from .spawn_coordinator import _READY_POLL_INTERVAL as _READY_POLL_INTERVAL
from .spawn_coordinator import _READY_TIMEOUT as _READY_TIMEOUT
from .spawn_coordinator import _STARTUP_WATCH_INTERVAL as _STARTUP_WATCH_INTERVAL

# ``trust_directory`` is still called directly by ``trust_project`` / ``trust_all_projects``
# below; ``ensure_remote_control_enabled`` and ``is_trusted`` moved with the spawn path to
# ``spawn_coordinator`` (#1157) and are re-exported so the ``clauster.runner.<name>`` patch
# path and ``from clauster.runner import ...`` keep resolving.
from .trust import ensure_remote_control_enabled as ensure_remote_control_enabled
from .trust import is_trusted as is_trusted
from .trust import trust_directory
from .webhooks import WebhookEmitter

_log = logging.getLogger("clauster.runner")


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
    """The named project does not exist under projects_root — or the instance is unknown.

    This module's generic 404. Besides the two spawn-path project raises it also covers a
    referenced ``instance_id`` that is not (or is no longer) a managed instance: resume,
    stop, and forget all raise it, as does a session another clauster process forgot.
    """


class NotTrusted(SpawnError):
    """The project directory has not accepted Claude's workspace-trust dialog."""


class InvalidSpawnOption(SpawnError):
    """Bad spawn/permission/resume/sandbox mode, name, or resume_session_id (mapped to 422).

    Also raised for a worktree spawn on a non-git project, an unusable ``custom_name``
    (too long, or control/format characters — #780), and a ``resume_session_id`` that is
    not a UUID, is combined with the internal resume path, is used outside pty mode, or
    does not name a conversation of this project (#303).
    """


class PermissionModeNotAllowed(SpawnError):
    """bypassPermissions requested for a project whose config ceiling forbids it."""


class CapacityExceeded(SpawnError):
    """A new bridge would exceed instance_defaults.max_bridges (clauster-enforced cap).

    The tally counts EVERY STARTING/RUNNING bridge, including the spawning project's own
    on the other mode axis — N pty sessions per project are allowed, and a live pty session
    does not block a standard spawn, so excluding same-project bridges let one project run
    unbounded sessions against a cap it never registered against.
    """


class InstanceStillLive(RuntimeError):
    """Raised when a lifecycle op is refused because the instance is still live.

    Two raise sites: forget() on a bridge that is STARTING/RUNNING or still has a live
    bridge/keeper pid, and adopt() on a project that is already managed in ANY status.
    Not a SpawnError: these are lifecycle ops, not spawns, and the caller maps this to
    409 (Stop it first) rather than the 4xx the spawn errors map to.
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


# How long stop()/the poison-heal wait for a bridge to shut itself down. Stays on the runner:
# its only reader is the still-on-runner ``_heal_poisoned_reattach``. (``_READY_TIMEOUT``,
# ``_READY_POLL_INTERVAL``, ``_POISON_GRACE`` and ``_STARTUP_WATCH_INTERVAL`` moved to
# ``spawn_coordinator`` with the readiness/startup-watch path, #1157, and are re-exported near
# the top of this module.)
_POISON_STOP_TIMEOUT = 5.0
# How long to wait for a force-killed process TREE to actually die. Distinct from
# `_POISON_STOP_TIMEOUT` on purpose: that one is a GRACEFUL-stop grace period (how long
# to let a bridge shut itself down), whereas this bounds a post-SIGKILL/TerminateProcess
# death, which is sub-100ms unless the target is stuck in an uninterruptible kernel wait
# — and 5s would not save that either. Kept separate so retuning the grace period can't
# silently retune the reap.
_TREE_REAP_WAIT = 2.0
# How long shutdown() waits for in-flight fire-and-forget notify sends to finish
# before cancelling them — bounds shutdown while letting a quick send complete.
_NOTIFY_DRAIN_GRACE = 2.0


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
        # Config-only launch/argv helpers (#1157): builds the two bridge argvs and
        # launches the detached bridge/keeper subprocesses. Owns the monotonic spawn
        # counter that keeps log filenames unique even for two same-ms spawns. The two
        # bridge modes stay separate here as on the runner — see :class:`BridgeLaunch`.
        # `_stderr_path_for` stays on the runner (its non-launch callers still use it)
        # and is injected so `_popen` shares one source of truth for the sibling path.
        self._launch = bridge_launch.BridgeLaunch(
            config=config,
            binary=self._binary,
            log_dir=self._log_dir,
            stderr_path_for=self._stderr_path_for,
        )
        # The reconciled working-session cache (`_sessions`) and the two long-lived loop
        # tasks (`_poll_task` / `_metrics_task`) are owned by :class:`PollLoop` (#1157,
        # built at the end of __init__) and re-exposed here as proxy properties (below), so
        # ``shutdown()`` and the tests reach the one copy. The remote-control / recap-hook
        # latches (`_rc_setting_ensured` / `_recap_hook_ensured`) are owned by
        # :class:`SpawnCoordinator` (built last), where their only reader/writer
        # (`_ensure_claude_side_settings`) now lives; the runner re-exposes them as proxy
        # properties (below) so the tests that assert on them still read the one copy.
        # ~/.claude/settings.json sits beside the ~/.claude.json we honor for trust.
        self._settings_json = self._claude_json.parent / ".claude" / "settings.json"
        # ~/.claude/projects holds the per-session transcripts the cost/token rollup
        # reads (#363 terminal-event snapshot). Anchored to the same claude home as
        # the trust file, so a HOME-isolated test points it at its tmp dir, not the
        # host's real transcripts.
        self._claude_projects_dir = self._claude_json.parent / ".claude" / "projects"
        # Filesystem-only prune/log-retention helpers (#1157): applies the bridge-log
        # retention policy and GCs long-dead bridge-pointers. Like BridgeLaunch it holds
        # config + paths ONLY — no registry, no locks. `protected` (live instances' log-set
        # keys) is snapshotted on the loop by the caller and passed to `_prune_logs`; the
        # pointer GC reads only config/discovery/pointer mtimes. `_log_set_key` stays on the
        # runner (its non-prune callers still use it) and is injected — see :class:`BridgePrune`.
        self._prune = bridge_prune.BridgePrune(
            config=config,
            log_dir=self._log_dir,
            claude_json=self._claude_json,
            claude_projects_dir=self._claude_projects_dir,
            log_set_key=self._log_set_key,
        )
        # Persistence of label / intentional_stop / spawn_mode (D14), now DB-backed
        # (#362) behind the same load()/save() dict contract the JSON store had.
        self._persistence = persistence or Persistence(
            config.state_dir, backup_before_migrate=config.db.backup_before_migrate
        )
        # Append-only session lifecycle / event history (#363). Records spawn/ready/
        # end/crash transitions for the Projects-zone "last used" sort (#298) and the
        # pty resume picker (#303). Best-effort and fail-closed — a lost history row
        # never affects a bridge's lifecycle.
        self._history = self._persistence.session_history_store()
        # Shared registry / locks / persist-mirror hub (#1157): the state core that
        # spawn / poll / rediscover / adopt / forget / stop all read and write — the
        # instance-keyed registry (`_instances`), the `Popen` map (`_procs`), the
        # per-spawn startup-watch tasks (`_startup_watches`), the crash tally
        # (`_crash_counts`), the metrics cache (`_metrics_cache`), the persist merge
        # mirror (`_persisted` / `_row_backed` / `_last_saved`) over the DB-backed store,
        # and the four lifecycle locks (`_spawn_locks`, the per-project + store-wide
        # flocks, `_persist_lock`) exposed as context managers. Held as the ONE
        # `RunnerState` so every caller and test seam reaches the same source of truth;
        # the runner re-exposes each dict/set as a thin proxy property (below) and keeps
        # thin delegators for the moved persist/lock methods. `_persisted_liveness` stays
        # on the runner (its module-level `_row_*` helpers are shared with the
        # still-on-runner reattach/adopt methods) and is injected — see
        # :class:`RunnerState`.
        self._registry = runner_state.RunnerState(
            config=config,
            lock_dir=self._lock_dir,
            store=self._persistence.state_store(),
            persisted_liveness=self._persisted_liveness,
        )
        # Reattach / adopt / rediscover surface (#1157): the read-mostly collaborator that
        # cold-start reattaches persisted rows, adopts externally-started bridges, and does the
        # poll-time row take-over. Built AFTER `_registry` and handed it, so every instance a
        # reattach materializes lands in the ONE registry (on the loop, under the same spawn
        # lock + bridge flock the spawn path uses). The still-on-runner helpers it calls but
        # does not own — the sidecar reader, the two log-path resolvers, the discovery
        # snapshot, the log-set key, and the project-instance lookup — are injected as callables
        # (the same pattern BridgeLaunch/RecordFacade use), so it holds no back-reference to the
        # runner. The row/pointer/sidecar field decoders live in `field_decode` (shared with the
        # still-on-runner readers); the public exception classes stay here (adopt imports them
        # lazily). The runner keeps thin delegators (below) for the moved methods so the façade
        # and the direct-call/patch test seams still reach them — see :class:`Rediscovery`.
        self._rediscovery = rediscovery.Rediscovery(
            config=config,
            log_dir=self._log_dir,
            registry=self._registry,
            read_sidecar=self._read_sidecar,
            raw_log_path_for=self._raw_log_path_for,
            latest_debug_log_for=self._latest_debug_log_for,
            discovered=self._discovered,
            log_set_key=self._log_set_key,
            get_instance_for_project=self.get_instance_for_project,
        )
        # Notify / webhook / session-event surface (#1157): owns the fire-and-forget
        # lifecycle sinks — the run-history append, the outbound webhook, and the outbound
        # notification — plus the single `_emit_lifecycle` chokepoint. It OWNS the
        # `_notify_tasks` set, the notifier, the webhook emitter, and the lazily-loaded
        # session_ref secret. The runner re-exposes `_notifier` / `_webhooks` /
        # `_notify_tasks` as thin properties (below) so the existing test seams and
        # `shutdown()`'s drain reach the SAME objects. `_history` stays owned by the runner
        # (not extracted until a later PR of #1157) and is passed by reference; the
        # project-path lookup is injected so the collaborator never holds `_instances` —
        # see :class:`RecordFacade`.
        self._record = record_facade.RecordFacade(
            config=config,
            history=self._history,
            project_path=self._project_path,
            claude_projects_dir=self._claude_projects_dir,
        )
        # Live hosted-session view for the agents --json cross-check (#592). The app
        # wires this to HostedManager.list_instances once both are built; it stays None
        # in unit tests and whenever the hosted channel is unused — poll_once then sees
        # no hosted sessions to claim and attributes exactly as before.
        self._hosted_instances: Callable[[], list[RemoteControlInstance]] | None = None
        # Poll + metrics background loops (#1157): the two long-lived loops the server runs
        # while up — the liveness/cross-check/prune poll (`poll_once` / `_poll_forever`) and
        # the server-side metrics sampler (`_refresh_metrics_cache` / `_metrics_refresh_forever`).
        # Built LAST and handed the ONE `RunnerState` + `RecordFacade`, so every loop write
        # (status reconcile, crash tally, phantom prune, ticks/boot-id heal, metrics cache)
        # lands on the single registry and every `crash`/`ready` emission on the single record
        # facade — no second registry, no write moved off the loop. It OWNS the two loop tasks
        # (`_poll_task` / `_metrics_task`) and the reconciled `_sessions` cache; the runner
        # re-exposes all three as proxy properties (below) so `shutdown()` still cancels +
        # awaits + clears the SAME task objects and the query methods + tests reach the one
        # `_sessions`. The still-on-runner helpers it calls but does not own — the metrics
        # sampler, the slow-refresh warner, the status reconciler, the session-ownership
        # predicate, the redacted-mirror flush, discovery, the connect-evidence reader, the
        # cross-process adoption, the hosted snapshot, and the rediscover + stale-pointer GC
        # `start_poll_loop` runs first — are injected as callables (the same pattern the
        # earlier collaborators use); the deferring lambdas resolve `self`'s CURRENT attribute
        # at call time so a test that swaps e.g. `runner.rediscover` still intercepts it. The
        # runner keeps thin delegators (below) for the moved methods — see :class:`PollLoop`.
        self._poll_loop = poll_loop.PollLoop(
            config=config,
            binary=self._binary,
            registry=self._registry,
            record=self._record,
            sample_one_bridge=lambda inst: self._sample_one_bridge(inst),
            warn_if_refresh_slow=lambda elapsed: self._warn_if_refresh_slow(elapsed),
            reconcile_status=lambda inst, alive: self._reconcile_status(inst, alive),
            can_own_sessions=lambda inst: self._can_own_sessions(inst),
            flush_redacted_mirror=lambda inst: self._flush_redacted_mirror(inst),
            discovered=lambda: self._discovered(),
            connect_facts_for=lambda *a, **kw: self._connect_facts_for(*a, **kw),
            adopt_rows_from_store=lambda: self._adopt_rows_from_store(),
            hosted_provider=lambda: self._hosted_instances,
            rediscover=lambda: self.rediscover(),
            prune_stale_pointers=lambda: self._prune_stale_pointers(),
        )
        # Spawn / resume path (#1157): the security-critical collaborator that owns the
        # fail-closed gate sequence, both bridge launches (standard + pty), the readiness
        # waits, and the off-request startup watch. Built LAST — after every other
        # collaborator — and handed the ONE `RunnerState` (registry + locks + persist),
        # `RecordFacade` (the `spawn`/`ready` lifecycle emits), `BridgeLaunch` (the two
        # subprocess launches), and `Rediscovery` (the cross-process standard reattach the
        # per-mode idempotency check performs). The gate ORDER, the two synchronous gates
        # (`_gate_stale_resume` / `_enforce_bridge_cap`), the two-mode separation, and the
        # lock order are preserved byte-for-byte by the move. `stop` / `forget` / `adopt`
        # STAY on the runner and keep taking the SAME shared locks from `_registry` — the
        # coordinator does not own the locks. The still-on-runner helpers it calls but does
        # not own — project resolution, the mode picker, option validation, the
        # poisoned-pointer clear, the log-retention prune + path resolvers, the
        # redacted-mirror flush, the poison-heal, the post-spawn enrich, the readiness
        # parsers, the status reconciler, and the error-detail capture — are injected as
        # deferring lambdas (the same pattern the earlier collaborators use), so a
        # monkeypatched seam is honored and the coordinator holds no runner. The public
        # exception classes, `SpawnOutcome`, and `_normalize_custom_name` stay on this module
        # and are imported lazily inside the coordinator's methods (no module-level `runner`
        # import — the cycle break). The runner keeps thin delegators (below) for the moved
        # methods so the façade and the direct-call/patch test seams still reach them — see
        # :class:`SpawnCoordinator`.
        self._spawner = spawn_coordinator.SpawnCoordinator(
            config=config,
            registry=self._registry,
            record=self._record,
            launch=self._launch,
            rediscovery=self._rediscovery,
            claude_json=self._claude_json,
            settings_json=self._settings_json,
            resolve_project=lambda name: self._resolve_project(name),
            discovered=lambda: self._discovered(),
            is_pty_mode=lambda prior=None, *, requested=None: self._is_pty_mode(
                prior, requested=requested
            ),
            validate_spawn_options=lambda *a, **kw: self._validate_spawn_options(*a, **kw),
            live_standard_for_project=lambda name: self._live_standard_for_project(name),
            clear_pointer_if_anchor_poisoned=lambda path: self._clear_pointer_if_anchor_poisoned(
                path
            ),
            log_set_key=lambda filename: self._log_set_key(filename),
            prune_logs=lambda protected: self._prune_logs(protected),
            unique_log_path=lambda name: self._unique_log_path(name),
            raw_log_path_for=lambda log_path: self._raw_log_path_for(log_path),
            flush_redacted_mirror=lambda inst: self._flush_redacted_mirror(inst),
            heal_poisoned_reattach=lambda inst, proc, path, reason: self._heal_poisoned_reattach(
                inst, proc, path, reason
            ),
            post_spawn_enrich=lambda inst, path: self._post_spawn_enrich(inst, path),
            sidecar_path_for=lambda log_path: self._sidecar_path_for(log_path),
            screen_sidecar_path_for=lambda log_path: self._screen_sidecar_path_for(log_path),
            pty_worktree_name=lambda inst: self._pty_worktree_name(inst),
            read_markers=lambda log_path: self._read_markers(log_path),
            read_sidecar=lambda sidecar: self._read_sidecar(sidecar),
            reconcile_status=lambda inst, alive: self._reconcile_status(inst, alive),
            project_path=lambda name: self._project_path(name),
            capture_error_detail=lambda inst: self._capture_error_detail(inst),
        )

    # ----- read API -------------------------------------------------------

    @property
    def claude_json(self) -> Path:
        """The claude.json whose trusted-dirs this runner honors (for trust checks)."""
        return self._claude_json

    @property
    def persistence(self) -> Persistence:
        """The shared persistence container (engine + DB-backed stores)."""
        return self._persistence

    # The two claude-side-settings latches are owned by :class:`SpawnCoordinator` (#1157),
    # where their only reader/writer (`_ensure_claude_side_settings`) now lives. Expose them
    # as read/write proxy properties so a test that asserts on `runner._rc_setting_ensured`
    # after a spawn (and any test that pre-flips one) reaches the collaborator's single copy.

    @property
    def _rc_setting_ensured(self) -> bool:
        """Whether remote control was pre-acknowledged this runner (on :attr:`_spawner`)."""
        return self._spawner._rc_setting_ensured

    @_rc_setting_ensured.setter
    def _rc_setting_ensured(self, value: bool) -> None:
        """Set the remote-control-acknowledged latch on the collaborator (test seam)."""
        self._spawner._rc_setting_ensured = value

    @property
    def _recap_hook_ensured(self) -> bool:
        """Whether the resume-recap hook was installed this runner (on :attr:`_spawner`)."""
        return self._spawner._recap_hook_ensured

    @_recap_hook_ensured.setter
    def _recap_hook_ensured(self, value: bool) -> None:
        """Set the recap-hook-installed latch on the collaborator (test seam)."""
        self._spawner._recap_hook_ensured = value

    # The notify / webhook / task-set surface is owned by :class:`RecordFacade` (#1157),
    # but the runner is the public façade and these three attributes are part of its
    # long-standing contract: subsystems and tests read/replace ``_notifier`` / ``_webhooks``
    # as seams, and ``shutdown()`` drains ``_notify_tasks``. Expose them as thin proxies so
    # every caller reaches the collaborator's single copy — no second notifier, emitter, or
    # task set can exist.

    @property
    def _notifier(self) -> Notifier:
        """The outbound notifier (owned by :attr:`_record`); reassignable as a test seam."""
        return self._record._notifier

    @_notifier.setter
    def _notifier(self, value: Notifier) -> None:
        """Replace the collaborator's notifier (test seam)."""
        self._record._notifier = value

    @property
    def _webhooks(self) -> WebhookEmitter:
        """The outbound webhook emitter (owned by :attr:`_record`); reassignable as a seam."""
        return self._record._webhooks

    @_webhooks.setter
    def _webhooks(self, value: WebhookEmitter) -> None:
        """Replace the collaborator's webhook emitter (test seam)."""
        self._record._webhooks = value

    @property
    def _notify_tasks(self) -> set[asyncio.Task]:
        """The fire-and-forget task set (owned by :attr:`_record`) that ``shutdown()`` drains."""
        return self._record._notify_tasks

    # The registry / crash tally / metrics cache / startup-watch tasks and the persist
    # mirror (`_persisted` / `_row_backed` / `_last_saved`) are owned by :class:`RunnerState`
    # (#1157), but the runner is the public façade and its ~60 internal refs + the test seams
    # reach them by these names. Expose each as a thin proxy property forwarding to
    # ``self._registry`` so every reader reaches the collaborator's single copy — no second
    # registry can exist. Only the two attrs a caller REASSIGNS wholesale get a write-through
    # setter (`_metrics_cache`, refreshed by the metrics loop; `_persisted`, dropped-from by
    # :meth:`forget`); the rest are mutated in place through the getter and stay read-only.

    @property
    def _instances(self) -> dict[str, RemoteControlInstance]:
        """The instance-keyed bridge registry (on :attr:`_registry`)."""
        return self._registry._instances

    @property
    def _procs(self) -> dict[str, subprocess.Popen]:
        """The ``Popen`` handles keyed by instance_id (on :attr:`_registry`)."""
        return self._registry._procs

    @property
    def _startup_watches(self) -> dict[str, asyncio.Task]:
        """The per-spawn startup-watch tasks keyed by instance_id (on :attr:`_registry`)."""
        return self._registry._startup_watches

    @property
    def _crash_counts(self) -> dict[str, int]:
        """The per-project bridge-crash tally since process start (on :attr:`_registry`)."""
        return self._registry._crash_counts

    @property
    def _metrics_cache(self) -> dict[str, dict]:
        """The per-instance server-side metrics snapshot (on :attr:`_registry`)."""
        return self._registry._metrics_cache

    @_metrics_cache.setter
    def _metrics_cache(self, value: dict[str, dict]) -> None:
        """Replace the metrics cache (the metrics loop reassigns it wholesale)."""
        self._registry._metrics_cache = value

    @property
    def _persisted(self) -> dict[str, dict]:
        """The persist merge base mirroring the store (on :attr:`_registry`)."""
        return self._registry._persisted

    @_persisted.setter
    def _persisted(self, value: dict[str, dict]) -> None:
        """Route a persist-mirror reassignment through the collaborator (#1157 invariant).

        The sole outside writer is :meth:`forget`, which drops the forgotten id from the
        base; every other mutation stays inside :class:`RunnerState`'s own persist path.
        """
        self._registry._persisted = value

    @property
    def _row_backed(self) -> set[str]:
        """The persist-ownership set of store-observed instance ids (on :attr:`_registry`)."""
        return self._registry._row_backed

    @property
    def _last_saved(self) -> dict[str, dict] | None:
        """The last subset written, for the persist no-change dedup (on :attr:`_registry`)."""
        return self._registry._last_saved

    # The reconciled working-session cache and the two long-lived loop tasks are owned by
    # :class:`PollLoop` (#1157), but the runner is the public façade: its query methods read
    # ``_sessions``, ``shutdown()`` cancels + awaits + clears ``_poll_task`` / ``_metrics_task``
    # (via ``getattr``/``setattr``), and tests seed ``_sessions`` / assert on the two tasks.
    # Expose each as a read/write proxy forwarding to ``self._poll_loop`` so every reader and
    # writer reaches the collaborator's single copy — no second loop, no orphaned task.

    @property
    def _sessions(self) -> list[WorkingSession]:
        """The reconciled working-session cache from ``poll_once`` (on :attr:`_poll_loop`)."""
        return self._poll_loop._sessions

    @_sessions.setter
    def _sessions(self, value: list[WorkingSession]) -> None:
        """Replace the working-session cache (the tests reassign it; poll_once writes its own)."""
        self._poll_loop._sessions = value

    @property
    def _poll_task(self) -> asyncio.Task | None:
        """The background poll-loop task (on :attr:`_poll_loop`); ``shutdown()`` reaps it."""
        return self._poll_loop._poll_task

    @_poll_task.setter
    def _poll_task(self, value: asyncio.Task | None) -> None:
        """Set/clear the poll-loop task handle (``shutdown()`` clears it to None)."""
        self._poll_loop._poll_task = value

    @property
    def _metrics_task(self) -> asyncio.Task | None:
        """The background metrics-loop task (on :attr:`_poll_loop`); ``shutdown()`` reaps it."""
        return self._poll_loop._metrics_task

    @_metrics_task.setter
    def _metrics_task(self, value: asyncio.Task | None) -> None:
        """Set/clear the metrics-loop task handle (``shutdown()`` clears it to None)."""
        self._poll_loop._metrics_task = value

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

    def metrics_snapshots_by_instance(self) -> dict[str, dict]:
        """Return the un-folded per-instance resource samples, keyed by instance_id (#1090).

        The cache is already per-instance (#778); this is the reader that hands that truth
        out unchanged, for callers that display ONE bridge's usage — a dashboard row must
        not wear the CPU/RAM of the other bridges sharing its project. A cache entry whose
        instance vanished from the registry between refreshes is dropped, never
        misattributed; :meth:`metrics_snapshots` folds the same entries per project for
        callers that genuinely want a project total.
        """
        return {
            iid: dict(sample)
            for iid, sample in self._metrics_cache.items()
            if iid in self._instances  # forgotten since the last refresh
        }

    def metrics_snapshots(self) -> dict[str, dict]:
        """Return per-project aggregated resource samples (#354).

        Serves the callers that want a whole project's total: the per-project metrics
        route and the Prometheus project-labelled gauges. Per-row display uses
        :meth:`metrics_snapshots_by_instance` instead (#1090).

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
            # Via the shared identity core rather than a hand-rolled epoch compare, so this
            # cannot disagree with the poll loop about the same bridge: an epoch-only
            # comparison here blanked the CPU/RAM chip of every RUNNING card after a clock
            # correction (#1399). It keeps the tight bound the hand-rolled version used —
            # `bridge_proc_start` is our OWN measurement of this pid, so a live match is
            # near-exact, and the loose 2.0s slack left a window in which a pid recycled two
            # seconds later still passed. `is_live_process`, NOT `is_live_bridge`: the
            # cmdline gate is a liveness question this guard has never asked.
            #
            # No separate `proc_create_time is None` pre-check: it absorbs the same psutil
            # exception set and treats a zombie the same way, so it could only ever agree —
            # at the cost of a second thread hop and a second psutil pass per bridge, per
            # metrics tick.
            if not await asyncio.to_thread(
                procutil.is_live_process,
                pid,
                start,
                start_ticks=inst.bridge_start_ticks,
                boot_id=inst.bridge_boot_id,
            ):
                return None  # PID reused onto an unrelated process — skip
        return await asyncio.to_thread(
            metrics.sample_tree,
            pid,
            interval=self._config.metrics.sample_interval_seconds,
            normalize_cpu=self._config.metrics.normalize_cpu,
        )

    async def _refresh_metrics_cache(self) -> None:
        """Re-sample every running bridge into ``_metrics_cache`` (delegates to PollLoop).

        Kept as a thin method on the runner so the direct-call test seam
        (``tests/test_metrics_cache.py``) is unaffected by the #1157 move; the sampler it
        drives (:meth:`_sample_one_bridge`) still lives here and is injected into the loop.
        """
        await self._poll_loop._refresh_metrics_cache()

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
        """Refresh the metrics cache every ``metrics.poll_seconds`` (delegates to PollLoop).

        The crash-resilience + ``CancelledError``-propagation loop lives in :class:`PollLoop`;
        this delegator keeps the direct-call test seam reaching it unchanged (#1157).
        """
        await self._poll_loop._metrics_refresh_forever()

    def get_instance(self, instance_id: str) -> RemoteControlInstance | None:
        """Return the instance with this instance_id, or None if unknown."""
        return self._instances.get(instance_id)

    def get_instance_for_project(self, project_name: str) -> RemoteControlInstance | None:
        """Resolve a bare project name to one instance: the LAST-registered match.

        Existence probe for the adopt path (``_reattach_from_persisted``): "is this
        project already managed, in any status?" A caller that only needs liveness uses
        :meth:`has_running_instance` (#778), and one that needs a specific bridge passes
        its ``instance_id``. Last-registered wins so the single row it returns is the most
        recently registered rather than a long-dead one.

        ⚠️ This is **not** a name-to-bridge resolver, and no longer the CLI/MCP fallback.
        Until #1150 :meth:`resolve_bridge_id` fell through to it, so ``clauster stop
        <project>`` on a project with a live bridge and a newer stopped row could land on
        the stopped row. #1150 closed that divergence by making the name **refuse**
        (returning candidates) when it matches several instances, so ``resolve_bridge_id``
        no longer calls this — the last-registered pick survives only as the adopt-path
        existence check above. Do not reintroduce it as a resolver, and do not reintroduce
        a project-keyed client fold to "match the dashboard" (that WAS #1143); the resolve
        path is the narrow one now.

        Does not raise; ``None`` when no instance matches.
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
        something running here?" (``_bridge_running`` in ``routes/projects.py``) must not miss it.
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

        A unique **prefix** of a known ``instance_id`` also resolves (#1099), but only
        after both exact forms have been tried — see :meth:`_resolve_bridge_ref` for why
        an exact project name has to outrank a prefix. Three surfaces already advertised
        prefixes — ``clauster stop/logs/open --help``, both MCP tool descriptions, and the
        CLI reference — while the CLI prints only the first 8 characters of an id, so the
        tool handed you an identifier and then rejected it. Now it accepts what it prints.

        Returns ``None`` when the identity matches neither a known instance_id nor a
        managed project — the caller raises the same 404 it would have raised before —
        **and also when a prefix is ambiguous.** Failing closed there is the point:
        stopping a live session the operator did not mean to touch is unrecoverable, so
        an ambiguous prefix must never pick one. Callers that want to say *which* ids it
        could have meant read :meth:`bridge_id_candidates`.

        A bare project name resolves only when it names exactly one instance. With two
        or more instances sharing the name it refuses — returning ``None`` here and the
        candidate ids via :meth:`bridge_id_candidates`, exactly as an ambiguous id prefix
        does (#1150) — rather than falling through to the LAST-REGISTERED match, which
        since #1143 need not be the row the dashboard shows. Narrowing the name resolution
        this way, never restoring a project-keyed client fold, is the fix.

        This fallback is for the surfaces where a human types a project name rather
        than an id: the CLI, the MCP tools, and the HTTP routes that still accept
        either. Per-session operations on a multi-session project must send the
        instance_id — the dashboard already does, refusing to act when it has none.
        """
        resolved, _, _ = self._resolve_bridge_ref(identity)
        return resolved

    def bridge_id_candidates(self, identity: str) -> list[str]:
        """Return the instance_ids an AMBIGUOUS ``identity`` could mean, else empty (#1099).

        Empty for every unambiguous case — resolved, or matching nothing at all — so a
        non-empty list means exactly "refused because it was ambiguous", and a caller can
        branch on that alone without re-deriving the resolution.
        """
        _, candidates, _ = self._resolve_bridge_ref(identity)
        return candidates

    def bridge_id_ambiguity(self, identity: str) -> tuple[list[str], str | None]:
        """Return ``(candidates, kind)`` for an AMBIGUOUS ``identity`` (#1099, #1150).

        ``kind`` is ``"prefix"`` (an id prefix matched several bridges) or ``"project"`` (a
        bare project name matched several instances); it is ``None`` whenever ``candidates``
        is empty. Callers that word a retry hint read ``kind`` from here rather than
        re-deriving it from the candidate strings, which misclassifies a hex-ish project
        name that happens to prefix its own instance id.
        """
        _, candidates, kind = self._resolve_bridge_ref(identity)
        return candidates, kind

    def _resolve_bridge_ref(self, identity: str) -> tuple[str | None, list[str], str | None]:
        """Resolve ``identity`` to ``(instance_id_or_None, candidates, ambiguity_kind)``.

        Single source of truth for :meth:`resolve_bridge_id`,
        :meth:`bridge_id_candidates`, and :meth:`bridge_id_ambiguity`, so the callers can
        never disagree about whether a given identity was ambiguous or *why*.

        ``ambiguity_kind`` is ``"prefix"`` when an id prefix matched several bridges
        (#1099), ``"project"`` when a bare project name matched several instances (#1150),
        and ``None`` otherwise. It is carried out from here rather than re-derived from the
        candidate strings by each caller: an instance id can itself be hex-ish and prefix a
        sibling's id, so ``any(c.startswith(identity))`` misclassifies a project match as a
        prefix one and prints the wrong retry hint.

        Order is exact id, then exact project name, then unique id prefix. **Both exact
        forms beat a prefix**, because a prefix is an abbreviation and an exact match is
        not. An exact id wins outright: a full id is never treated as a prefix of some
        longer one, so adding a bridge can't make an id the operator already had stop
        working.

        The project name must also outrank a prefix, and the reason is reachability, not
        taste. Instance ids are UUIDs, so a project name collides only if it is itself
        hex-ish (``cafe``, ``deadbeef``, ``face``) and happens to prefix a live id — rare,
        but the loser of that race is unrecoverable. Ranking the prefix first would make
        the project reading **unreachable**: a project name is a fixed string the operator
        cannot lengthen, so there would be no way left to name that project, and
        ``stop``/``forget`` would silently act on an unrelated bridge. Ranking the exact
        name first costs the prefix reading nothing — the operator just types one more
        character to mean the id.

        The rule that follows from that: **an identity naming a managed project is never
        reinterpreted as a prefix.** If the project is idle, the answer is "that project has
        no instance" — not some other project's bridge that happens to start with the same
        characters. An earlier revision fell through to prefix matching when the project had
        no instance, which reopened the same wrong-bridge hole one step further along: an
        idle ``cafe`` would have resolved ``stop cafe`` onto an unrelated ``cafe0000-…``
        bridge. Only an identity that names no project at all reaches prefix matching, and
        the prefix reading is still available there by typing one more character.
        """
        if identity in self._instances:
            return identity, [], None
        project_matches = sorted(
            iid for iid, inst in self._instances.items() if inst.project == identity
        )
        if len(project_matches) == 1:
            return project_matches[0], [], None
        if len(project_matches) > 1:
            # Multiple instances share this project name (#1150). Refuse rather than silently
            # picking one — the wrong choice is unrecoverable. Callers see (None, candidates)
            # exactly as they do for an ambiguous id prefix (#1099); the "project" kind tells
            # them the operator must retry with an id, not a longer string.
            return None, project_matches, "project"
        if identity in self._discovered():
            # Names a real project that simply has no bridge. `_discovered()` is cached
            # (short TTL + mtime-invalidated) and already runs on every poll_once, so this
            # costs a dict lookup on the common path.
            return None, [], None
        if identity:
            # `if identity` guards the empty string, which prefixes EVERYTHING: without
            # it, "" would read as ambiguous-across-all rather than simply unknown.
            matches = sorted(iid for iid in self._instances if iid.startswith(identity))
            if len(matches) == 1:
                return matches[0], [], None
            if matches:
                return None, matches, "prefix"
        return None, [], None

    @staticmethod
    def _can_own_sessions(inst: RemoteControlInstance) -> bool:
        """Whether this instance could plausibly own a live working session (#1020, #820).

        A row with no resolvable pid that is NOT still starting — an ERROR row whose spawn
        failed, or a ``_stopped_from_persisted`` phantom — owns no process and so can own no
        session. It must be excluded from the reconcile candidate lists: absent from the
        ownership map, it would otherwise be the ungated candidate that
        :func:`inspector._select_owner` falls back to, silently absorbing an external
        hand-run ``claude`` at that cwd (the #820 case) and re-labelling it as a dead
        bridge's session. Its project can be `live` because a *sibling* bridge is alive
        (#778), so the project-level liveness test above does not filter it out.

        A STARTING row is kept even with no pid — that is the #713 window, where the bridge
        genuinely exists and its auto-created session must still attribute.
        """
        return (
            inst.bridge_pid is not None
            or inst.keeper_pid is not None
            or inst.status is InstanceStatus.STARTING
        )

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

        Keyed by ``instance_id`` — the ``parent_instance`` stamped at reconcile. A project
        may run several bridges (#778), so a project key would fold them into one bucket
        and a standard bridge's row would list the independent interactive sessions as if
        it owned them (#1020 A3). Callers must look up by ``instance_id``, not project.
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
        session landing in that dir (a bridge child, an external terminal session), plus
        sessions running in the project's git worktrees.

        Those worktrees MUST stay in lockstep with :func:`usage.transcript_paths_for`,
        which lists the same set (#1020): a worktree session listed but not counted live
        here would render as a dormant conversation and be surfaced by the launch popover's
        Conversation picker (``!live && turn_count > 0``). That picker always sends
        ``--fork-session`` alongside ``--resume <uuid>``, so it only ever BRANCHES — a
        fresh session id, picked conversation never clobbered — and surfacing a live
        session there is not destructive. (The two flags are independent;
        ``hosted``/``supervisor`` send ``--resume`` bare to continue in place.) What this
        function owes the picker is an ACCURATE flag; what the picker should filter on is
        an open product question tracked separately (``scratch`` FE-5). Hosted (claustrum)
        sessions are folded in separately by the route, since they run no ``agents --json``
        session.
        """
        target = pointers.sanitize_cwd(project_path)
        # The SAME string rule usage._transcript_dirs_for scans with, so the listing and the
        # live set cannot disagree. Matching on a different rule (containment in
        # `.claude/worktrees`) left a gap: a cwd like `<project>/.claude/worktrees-foo/x`
        # sanitizes into this prefix and IS listed, but failed the containment test, so a
        # RUNNING session there read as dormant and the Conversation (fork) picker's
        # `!live && turn_count > 0` filter surfaced it.
        worktree_prefix = pointers.sanitize_cwd(Path(project_path) / pointers.WORKTREE_SUBDIR)
        try:
            project_root = Path(project_path).resolve()
        except OSError:  # pragma: no cover - resolve() on a pathological path
            project_root = Path(project_path)

        def _belongs(cwd: Path) -> bool:
            """Decide whether ``cwd`` is this project's, pairing the name rule with containment."""
            sanitized = pointers.sanitize_cwd(cwd)
            if sanitized == target:
                return True
            # The name rule alone is ambiguous (sanitizing is lossy), so pair it with real
            # containment under the project — the liveness equivalent of the scan's
            # sibling-project exclusion, which is what keeps a neighbouring project out.
            # A stray `claude` run elsewhere under the project fails the prefix test, so it
            # is still not claimed. Resolved because is_relative_to is purely lexical:
            # unresolved, `…/worktrees/../../etc` would match and a symlinked cwd would not.
            if not sanitized.startswith(f"{worktree_prefix}-"):
                return False
            try:
                return cwd.resolve().is_relative_to(project_root)
            except OSError:  # pragma: no cover - unreadable/looping symlink
                return False

        return {s.local_uuid for s in self._sessions if _belongs(Path(s.cwd))}

    # ----- persistence (state.json, D14) ----------------------------------

    def _persisted_liveness(self, inst: RemoteControlInstance) -> dict:
        """Return the ``bridge_pid`` pair (``bridge_proc_start``/ticks/boot-id) set to persist.

        A live instance publishes its own pair, and so does an operator-stopped one:
        :meth:`stop` records the intent and sets STOPPED **without** clearing ``bridge_pid``
        (see :meth:`_resync_pids_from_row`, whose generation compare depends on that). The
        cards that carry no pids are the ones REBUILT from a row — :meth:`_stopped_from_row`
        and :meth:`_stopped_from_persisted` zero them so a card can never be re-read as
        alive. This branch therefore engages for exactly those rebuilt cards, which is
        precisely the ratchet it exists to break: the ROW must keep the pair its bridge
        last ran under (#1115).

        That pair is the row's liveness *identity*: :meth:`_reattach_rows_with_pids` uses
        "row has a pid" to tell a post-#1088 instance-keyed row from a legacy pid-less one.
        Writing the card's ``None`` back made every dead row look legacy on the NEXT cold
        start, so it fell through to the project-keyed pointer walk — which rebuilds at
        most ONE card per project. That was a one-way ratchet: each restart hid every
        stopped session but the earliest of each project, while its row sat in the DB.

        Preserved ONLY as a complete pair, which is what makes it safe: a recycled pid is
        rejected because :func:`procutil.is_live_bridge` compares the process create-time
        against the stored one. A pid with NO ``proc_start`` is a different animal — that
        function has no start-time to compare and falls back to "alive + bridge cmdline",
        so a reused pid running any bridge WOULD read as this one coming back to life.
        Such a row is therefore still cleared — correctness beats completeness for the one
        shape that cannot be made reuse-proof. It no longer *folds* for it: the pid-less
        pass at the end of :meth:`rediscover` still cards it under its own id.

        ``keeper_pid`` is deliberately NOT carried through this helper: the reattach
        re-derives it from the sidecar, correlated to the bridge's own pair
        (:meth:`_recover_keeper_pid`), and :meth:`stop` force-kills that tree — so a stale
        keeper pid is the one value here that could reap a stranger's processes. It stays
        on ``_persist_subset``'s own ``inst.keeper_pid`` passthrough (with its paired
        ``keeper_proc_start`` and ``keeper_start_ticks``, #1178 / #1402), which means a
        stop()-ed card's row keeps all three while a REBUILT card's row drops all three.
        """
        if inst.bridge_pid is not None:
            return {
                "bridge_pid": inst.bridge_pid,
                "bridge_proc_start": inst.bridge_proc_start,
                "bridge_start_ticks": inst.bridge_start_ticks,
                "bridge_boot_id": inst.bridge_boot_id,
            }
        # Normalized through the row coercers, not passed through raw: this value is
        # round-tripping from the store, where a hand-edited row can hold junk.
        prior = self._persisted.get(inst.instance_id) or {}
        pid = _row_int(prior.get("bridge_pid"))
        proc_start = _row_float(prior.get("bridge_proc_start"))
        if pid is None or proc_start is None:
            return {
                "bridge_pid": None,
                "bridge_proc_start": None,
                "bridge_start_ticks": None,
                "bridge_boot_id": None,
            }
        # Ticks ride along with the pair rather than gating it (#1399): they are Linux-only
        # and absent from every pre-#1399 row, so requiring them here would clear exactly the
        # rows this helper exists to preserve. A row that keeps the pair but not the ticks
        # compares on the epoch alone — the pre-#1399 behaviour — which is why the prune no
        # longer rests on that comparison.
        return {
            "bridge_pid": pid,
            "bridge_proc_start": proc_start,
            "bridge_start_ticks": _row_int(prior.get("bridge_start_ticks")),
            "bridge_boot_id": _row_str(prior.get("bridge_boot_id")),
        }

    def _persist_subset(self) -> dict[str, dict]:
        """Build the record to persist, keyed by instance id (delegates to :class:`RunnerState`).

        Kept on the runner for the tests that read the overlay via ``runner._persist_subset()``.
        Patching THIS façade method does NOT intercept a persist and would pass vacuously:
        ``RunnerState._persist`` calls its OWN ``_persist_subset`` (the collaborator owns the
        registry + mirror it reads), so a test that must drive the subset patches
        ``runner._registry._persist_subset``. ``RunnerState`` calls the injected
        ``_persisted_liveness`` for each row's liveness pair.
        """
        return self._registry._persist_subset()

    async def _refresh_persisted(self) -> bool:
        """Replace the persist merge base with the current store (delegates to RunnerState).

        The public refresh (takes ``_persist_lock`` then reloads). The poll-loop adoption
        path and the resume/rediscover paths call it here; a ``False`` return (a DB read
        error kept the old base) makes those callers skip their tick, unchanged by the move.
        """
        return await self._registry._refresh_persisted()

    async def _persist(self, *, drop: str | None = None) -> None:
        """Write the persisted subset off-loop when it changed (delegates to :class:`RunnerState`).

        The whole refresh→merge→save stays serialized under ``_persist_lock`` + the
        store-wide flock inside ``RunnerState`` — the persist-serialization invariant is
        preserved by the move, not by this delegator. ``drop`` is :meth:`forget`'s deletion
        path. The spawn/stop/resume/forget callers invoke it through here; the poll loop
        (now in :class:`PollLoop`, #1157) calls ``self._registry._persist()`` on the same
        one ``RunnerState``, so the serialization is shared either way.
        """
        await self._registry._persist(drop=drop)

    # ----- discovery helpers ---------------------------------------------

    def _discovered(self) -> dict[str, Project]:
        """Return the discovered projects under ``projects_root``, keyed by name."""
        # Cached (short TTL + mtime-invalidated): this runs on every poll_once and
        # many lookup paths. A trust write invalidates the cache explicitly
        # (trust_project), so the post-write re-read still reflects the new state.
        return {
            p.name: p
            for p in discover_projects_cached(self._config.projects_root, self._claude_json)
        }

    def _resolve_project(self, name: str) -> Project:
        """Resolve ``name`` to a discovered project, rejecting invalid or unknown names."""
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

    async def trust_all_projects(self) -> list[Project]:
        """Trust every currently-untrusted discovered project; return the refreshed list.

        Claude Code 2.1.232+ stopped covering nested git repos with a parent grant
        (#1224), so a projects_root that once trusted everything now leaves each repo
        untrusted and needing its own key. This grants an own
        ``hasTrustDialogAccepted`` key to each discovered project that is not already
        trusted — the operator's "trust all discovered projects" action — then re-reads
        so the returned list reflects the new state. Idempotent: an already-trusted
        project is skipped. Any ``OSError`` propagates (the route maps it to 500) rather
        than silently trusting a partial set.
        """
        untrusted = [
            p for p in self._discovered().values() if p.trust_state is not TrustState.TRUSTED
        ]
        for proj in untrusted:
            await asyncio.to_thread(trust_directory, proj.path, self._claude_json)
        if untrusted:
            invalidate_discovery_cache()
        return list(self._discovered().values())

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

        Public façade member (#1157): the spawn/resume path lives in
        :class:`~clauster.spawn_coordinator.SpawnCoordinator`; this delegator preserves the
        exact signature every caller (``routes/*``, ``engine.py``, ``mcp_server.py``) reaches.
        """
        return await self._spawner.spawn(
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
        """Spawn a new bridge for ``name`` (delegates to :class:`SpawnCoordinator`, #1157).

        Public façade member: ``routes/*``, ``engine.py``, and ``mcp_server.py`` call
        ``runner.spawn_detailed`` directly, and this delegator preserves that exact
        signature. The full spawn contract — the fail-closed gate order, the two bridge
        modes, ``custom_name``/``sandbox``/``resume_session_id``/``trust`` handling, the
        per-project + cross-process lock, and the :class:`SpawnOutcome` semantics — lives on
        :meth:`clauster.spawn_coordinator.SpawnCoordinator.spawn_detailed`. The coordinator
        takes the SAME shared spawn lock + bridge flock from the one :class:`RunnerState`.
        """
        return await self._spawner.spawn_detailed(
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

    # The four lifecycle locks are owned by :class:`RunnerState` (#1157). These thin
    # delegators keep the exact call-site API (a sync lock getter + three async context
    # managers) so every acquisition site — spawn (`_spawn_locked`), stop, forget, adopt,
    # resume — keeps taking them in the SAME acyclic order: in-proc `_spawn_lock_for` →
    # per-project `_bridge_flock` → `_persist_lock` → store-wide `_store_flock`. The move
    # relocates the state, not a single acquisition.

    def _spawn_lock_for(self, name: str) -> asyncio.Lock:
        """Return the per-project spawn lock (delegates to :class:`RunnerState`).

        Synchronous (no ``await``) so the get-or-create itself can't race on the loop.
        """
        return self._registry._spawn_lock_for(name)

    @contextlib.asynccontextmanager
    async def _bridge_flock(self, name: str) -> AsyncIterator[None]:
        """Hold the per-project bridge-lifecycle flock (delegates to :class:`RunnerState`).

        The cross-process layer under the in-proc :meth:`_spawn_lock_for`; always taken
        inproc-first, cross-process-second. See :meth:`RunnerState._bridge_flock`.
        """
        async with self._registry._bridge_flock(name):
            yield

    @contextlib.asynccontextmanager
    async def _store_flock(self) -> AsyncIterator[None]:
        """Hold the store-wide flock for a read-merge-replace save (delegates to RunnerState).

        Always acquired AFTER any per-project flock and never before one, so the two
        levels can't deadlock across processes. See :meth:`RunnerState._store_flock`.
        """
        async with self._registry._store_flock():
            yield

    @contextlib.asynccontextmanager
    async def _flock(self, target: Path) -> AsyncIterator[None]:
        """Hold :func:`atomicio.cross_process_lock` on ``target`` (delegates to RunnerState).

        Exposed for the cross-process flock tests; the live callers are
        :meth:`RunnerState._bridge_flock` / :meth:`RunnerState._store_flock`.
        """
        async with self._registry._flock(target):
            yield

    # ----- _spawn_locked's pre-spawn gates (delegate to :class:`SpawnCoordinator`, #1157) ---
    # The gate bodies moved with `_spawn_locked` to `spawn_coordinator`; the coordinator runs
    # them in the SAME fail-closed order: stale-resume → option validation → fork-target
    # ownership → per-mode idempotency → trust gate → claude-side settings → poisoned-pointer
    # clear → bridge cap → deferred --trust write → launch. These delegators keep the exact
    # signatures (and async-ness) the direct-call/patch test seams reach on the runner.
    #
    # ⚠️ `_gate_stale_resume` and `_enforce_bridge_cap` stay SYNCHRONOUS (`def`, not
    # `async def`) on the coordinator AND here — each reads loop-owned mutable state and
    # raises on what it read, with no await between the read and the decision, so a
    # concurrent spawn cannot interleave. Do not make either delegator async.

    def _gate_stale_resume(
        self,
        *,
        resume: bool,
        resume_target: RemoteControlInstance | None,
        refreshed: bool,
    ) -> None:
        """Refuse a resume another process forgot (delegates to :class:`SpawnCoordinator`).

        SYNCHRONOUS by design (see the banner above); the delegator stays ``def`` too.
        """
        self._spawner._gate_stale_resume(
            resume=resume, resume_target=resume_target, refreshed=refreshed
        )

    async def _validate_resume_session_id(
        self,
        proj: Project,
        name: str,
        resume_session_id: str,
        *,
        resume: bool,
        effective_resume_mode: ResumeMode,
    ) -> None:
        """Validate a fork-a-past-conversation target (delegates to :class:`SpawnCoordinator`).

        Strict UUID-shape + pty-mode + this-project-ownership gate, all preserved on the
        coordinator; raises :class:`InvalidSpawnOption` (→ 422) on every rejection.
        """
        await self._spawner._validate_resume_session_id(
            proj,
            name,
            resume_session_id,
            resume=resume,
            effective_resume_mode=effective_resume_mode,
        )

    async def _apply_mode_spawn_policy(
        self,
        proj: Project,
        name: str,
        effective_resume_mode: ResumeMode,
        *,
        spawn_mode: SpawnMode | None,
        resume: bool,
        resume_target: RemoteControlInstance | None,
        spawn_warnings: list[str],
    ) -> SpawnOutcome | None:
        """Apply the per-mode idempotency policy (delegates to :class:`SpawnCoordinator`, #777).

        A non-``None`` return means an already-live bridge satisfies this spawn; ``None``
        means carry on to the launch. The two bridge modes stay deliberately separate on the
        coordinator, which reaches the cross-process standard reattach through the one
        :class:`Rediscovery`.
        """
        return await self._spawner._apply_mode_spawn_policy(
            proj,
            name,
            effective_resume_mode,
            spawn_mode=spawn_mode,
            resume=resume,
            resume_target=resume_target,
            spawn_warnings=spawn_warnings,
        )

    async def _ensure_claude_side_settings(self) -> None:
        """Pre-write the claude-side settings a bridge depends on (delegates to coordinator).

        Both writes are best-effort and each is attempted once per runner via the two latches
        the coordinator owns (re-exposed here as proxy properties).
        """
        await self._spawner._ensure_claude_side_settings()

    def _enforce_bridge_cap(self, max_bridges: int | None) -> None:
        """Fail closed at the concurrent-bridge cap (delegates to :class:`SpawnCoordinator`).

        SYNCHRONOUS by design (see the gate banner above); the delegator stays ``def`` too —
        the cap read of ``_instances`` and its raise must not be split by an await.
        """
        self._spawner._enforce_bridge_cap(max_bridges)

    # ----- end _spawn_locked's pre-spawn gates ---------------------------------------

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
        """Spawn (or hand back) a bridge for ``name`` (delegates to :class:`SpawnCoordinator`).

        The body of :meth:`spawn_detailed`, split out so the locking lives in the caller;
        moved to the coordinator with the whole fail-closed gate sequence (#1157). Kept as a
        thin delegator so the direct-call test seams reach it unchanged.
        """
        return await self._spawner._spawn_locked(
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

    async def resume(self, instance_id: str) -> RemoteControlInstance:
        """Re-spawn a stopped/crashed bridge; the instance only (delegates to coordinator).

        Public façade member: the thin wrapper over :meth:`resume_detailed`, mirroring
        :meth:`spawn` / :meth:`spawn_detailed`. The revive body lives on
        :class:`~clauster.spawn_coordinator.SpawnCoordinator`.
        """
        return await self._spawner.resume(instance_id)

    async def resume_detailed(self, instance_id: str) -> SpawnOutcome:
        """Re-spawn a stopped/crashed bridge (delegates to :class:`SpawnCoordinator`, #1157).

        Public façade member: returns the full :class:`SpawnOutcome` so a declined resume
        (the standard one-per-project cap, or the pty already-live path) is never reported as
        a silent success. ``routes/*``, ``engine.py``, and ``mcp_server.py`` call
        ``runner.resume_detailed`` directly; this delegator preserves that signature.
        """
        return await self._spawner.resume_detailed(instance_id)

    def _validate_spawn_options(
        self,
        proj: Project,
        spawn_mode: str,
        permission_mode: str,
        resume_mode: str | None = None,
        sandbox: str | None = None,
    ) -> None:
        """Reject an unrecognized spawn option, or one this ``proj`` forbids.

        Screens ``spawn_mode`` / ``permission_mode`` / ``resume_mode`` / ``sandbox``
        against their allowed sets (:class:`InvalidSpawnOption`), then applies the two
        project-scoped policy gates that are the reason ``proj`` is passed at all: a
        ``worktree`` spawn on a non-git project (:class:`InvalidSpawnOption`), and a
        permission mode the project's config forbids — the bypassPermissions 403 gate
        (:class:`PermissionModeNotAllowed`).
        """
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
        """Return a log path unique to this spawn (delegates to :class:`BridgeLaunch`)."""
        return self._launch._unique_log_path(name)

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
        candidate, so there is no TOCTOU window against a concurrent retention prune. Returns
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

    def archived_log_path(self, instance: RemoteControlInstance) -> Path | None:
        """Re-derive a DEAD instance's on-disk bridge log from the log dir, or ``None`` (#1117).

        A stopped or crashed bridge's record carries no log path once it is restored from
        persistence — the ``instances`` table has no column for one (``_INSTANCE_FIELDS`` in
        :mod:`clauster.db.stores`) — so a fresh ``clauster logs`` process refused to read the
        log of the very bridge an operator most wants to read: the one that just died, whose
        file is still sitting in the log dir.

        Re-derived exactly as a rediscovered *standard* survivor's live tail already is
        (:meth:`_latest_debug_log_for`): the newest ``<project>-<ms>-<seq>.log`` Clauster
        wrote for THIS project, anchored to the project's exact filename shape so a sibling
        project's log can never be handed over. It is that project's newest log rather than
        provably *this* instance's — a project holding several dead rows resolves them all to
        the same file — because nothing on disk binds a log to an instance id. Binding one
        would need a persistence schema addition, which the issue leaves open as a design
        question; a shared newest-log answer beats the operator getting nothing at all.

        Returns the verbatim parse-source when it exists and the redacted at-rest mirror
        otherwise, matching what a live tail resolves to (:meth:`_raw_log_path_for`) — the
        reader redacts either one on the way out, so this changes no redaction semantics.

        Live instances always return ``None``: their tail is bound at spawn or reattach, and
        re-deriving one by project would risk pointing a pty session at a *different*
        session's log — the same hazard :meth:`_reattach_pty_from_sidecar` refuses to take.
        """
        if instance.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING):
            return None
        log_path = self._latest_debug_log_for(instance.project)
        if log_path is None:
            return None
        raw_path = self._raw_log_path_for(log_path)
        # Identical to log_path when `logs.redact_session_url` is off (the single verbatim
        # file); when it is on, prefer the raw parse-source but fall back to the public
        # mirror rather than reporting an unreadable path the operator can't act on.
        return raw_path if raw_path.exists() else log_path

    def _keeper_sidecars_for(self, name: str) -> list[Path]:
        """Keeper sidecars belonging to ``name`` (delegates to :class:`Rediscovery`)."""
        return self._rediscovery._keeper_sidecars_for(name)

    def _prune_logs(self, protected: set[str]) -> None:
        """Apply the bridge-log retention policy (delegates to :class:`BridgePrune`).

        ``protected`` is the set of live instances' log-set keys, which the spawn path
        snapshots from ``_instances`` on the event loop before this runs off-thread — the
        registry read stays on the runner and is passed down, never held by the collaborator.
        """
        self._prune._prune_logs(protected)

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
        """Build the `claude remote-control` argv (delegates to :class:`BridgeLaunch`).

        To stub argv in a test, patch ``BridgeLaunch._build_cmd`` (the launch path,
        via ``_popen``, calls the collaborator's copy) — patching this façade method
        on ``SessionRunner`` does not intercept a spawn and would pass vacuously.
        """
        return self._launch._build_cmd(log_path, name, spawn_mode, permission_mode, sandbox)

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
        """Build the config-driven env overlay (delegates to :class:`BridgeLaunch`).

        Patch ``BridgeLaunch._bridge_env_overlay`` to intercept it — ``_popen`` calls
        the collaborator's copy, so patching this façade method passes vacuously.
        """
        return self._launch._bridge_env_overlay(extra)

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
        """Launch the detached standard bridge subprocess (delegates to :class:`BridgeLaunch`)."""
        return self._launch._popen(
            cwd, log_path, name, spawn_mode, permission_mode, debug_path, sandbox
        )

    # ----- pty / Interactive Session mode (true conversation resume) ------

    def _is_pty_mode(
        self,
        prior: RemoteControlInstance | None = None,
        *,
        requested: str | None = None,
    ) -> bool:
        """Whether the bridge launches under the PTY keeper (Interactive Session).

        A bridge's mode is fixed at first launch. Precedence: an explicit *requested*
        mode (the per-launch picker) always wins, on a fresh start AND on a resume; else
        *prior*'s recorded ``resume_mode``; else the global ``claude.launch_mode`` seeds a
        brand-new bridge. ``stop()`` and ``resume()`` can never disagree about the same
        bridge because :meth:`resume_detailed` passes the prior instance's own mode as
        *requested* — this function does not enforce that on its own. Without honoring
        *prior* at all, editing the config under a
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
        """Build the flag-form pty bridge argv (delegates to :class:`BridgeLaunch`)."""
        return self._launch._build_pty_bridge_argv(
            log_path,
            name,
            permission_mode,
            resume=resume,
            resume_session_id=resume_session_id,
            worktree_name=worktree_name,
        )

    @staticmethod
    def _pty_worktree_name(instance: RemoteControlInstance) -> str | None:
        """Resolve the per-session worktree name for a worktree-mode pty spawn.

        Normally derived from the instance_id — which survives stop→resume (a resume
        revives the same identity) — so the revived session lands back in ITS worktree.
        ``None`` for non-worktree spawns (the session runs in the project dir).

        An explicit ``worktree_name`` wins when the instance carries one (#1241). That is
        the keeper-only reattach: a live keeper adopted from its sidecar with no row whose
        identity it can be given gets a FRESH instance_id, and the derivation then names a
        worktree that does not exist — a resume would create a second one and orphan the
        original (which still holds any uncommitted work and its branch), while the
        stop-time unlock (#1089) would target the empty name and leave the real worktree
        locked. The sidecar records the name the bridge was actually launched with, so the
        recovered value is the truth and the derivation is only the fallback.
        """
        if instance.spawn_mode != "worktree":
            return None
        return instance.worktree_name or f"clauster-{instance.instance_id[:8]}"

    def _unlock_pty_worktree(self, instance: RemoteControlInstance) -> None:
        """Release the git lock on a stopped pty session's worktree (#1089).

        Claude Code creates a ``spawn_mode="worktree"`` interactive session's worktree via
        ``--worktree <name>`` and LOCKS it (lock reason ``claude session …``); it does not
        release the lock on the SIGINT-driven stop. ``git worktree remove`` then refuses it
        (``cannot remove a locked working tree``, naming a now-dead pid), so the operator has
        to discover ``git worktree unlock`` before any cleanup. Clauster knows the session
        ended, so it releases the lock here — the lock only guards against a *live* session,
        and once stopped it protects nothing. The worktree and its branch are left in place:
        they may hold uncommitted work, and a resume reuses them.

        Best-effort: a non-worktree session, an unknown project, a missing/renamed or
        already-unlocked worktree, or a missing ``git`` all no-op. Any failure is logged,
        never raised, so it can never fail the stop.
        """
        name = self._pty_worktree_name(instance)
        if name is None:
            return
        # Fail-safe: this runs inside stop() AFTER the process is already down, so it must
        # NEVER raise — `_resolve_project` can raise `UnknownProject` OR an `OSError`/
        # `RuntimeError` from project discovery / path resolution, and git can fail to spawn;
        # any of those escaping would abort a COMPLETED stop before its handle cleanup,
        # lifecycle emit, and API/MCP/CLI response (#1089 Greptile P1). One broad guard so no
        # exception class can leak out of this best-effort cleanup.
        try:
            proj = self._resolve_project(instance.project)
            worktree = proj.path / pointers.WORKTREE_SUBDIR / name
            res = subprocess.run(
                ["git", "-C", str(proj.path), "worktree", "unlock", str(worktree)],
                capture_output=True,
                text=True,
                timeout=10,
                env=procutil.child_env(),
                check=False,
            )
            if res.returncode != 0:
                # Non-zero is expected + harmless when the worktree was already unlocked or is
                # gone ("not locked" / "is not a working tree"); logged for the genuine-error case.
                _log.debug(
                    "worktree unlock for %s non-zero (%s): %s",
                    instance.instance_id,
                    res.returncode,
                    res.stderr.strip(),
                )
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup must never fail the stop
            _log.debug(
                "worktree unlock for %s failed (best-effort): %s", instance.instance_id, exc
            )

    @staticmethod
    def _keeper_launch_cmd(
        sidecar: Path,
        cwd: Path,
        bridge_argv: list[str],
        screen_sidecar: Path | None = None,
        *,
        state_dir: Path,
    ) -> list[str]:
        """Wrap the bridge argv in a PTY-keeper launcher (delegates to :class:`BridgeLaunch`).

        Patch ``BridgeLaunch._keeper_launch_cmd`` to intercept it — ``_popen_keeper``
        calls the collaborator's copy, so patching this façade method passes vacuously.
        """
        return bridge_launch.BridgeLaunch._keeper_launch_cmd(
            sidecar, cwd, bridge_argv, screen_sidecar, state_dir=state_dir
        )

    def _popen_keeper(
        self,
        cwd: Path,
        sidecar: Path,
        bridge_argv: list[str],
        screen_sidecar: Path | None = None,
        *,
        state_dir: Path,
    ) -> subprocess.Popen:
        """Launch the PTY keeper detached (delegates to :class:`BridgeLaunch`)."""
        return self._launch._popen_keeper(
            cwd, sidecar, bridge_argv, screen_sidecar, state_dir=state_dir
        )

    @staticmethod
    def _read_sidecar(sidecar: Path) -> dict | None:
        """Read the keeper's discovery JSON, or None if absent / mid-write / invalid.

        The ``isinstance`` gate matters: valid JSON that is not an object (``[]``, ``"x"``)
        would otherwise return a non-dict whose ``.get`` raises ``AttributeError`` in every
        caller — and one of those callers now runs inside ``rediscover``, where it would
        take down lifespan startup rather than skipping one unreadable sidecar. Mirrors
        ``pty_keeper._read_sidecar``, which already guards this.
        """
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
            # UnicodeDecodeError (a ValueError) for a non-UTF-8 sidecar must still
            # honor the invalid -> None contract, not break readiness polling.
            return None

    def _recover_keeper_pid(
        self,
        name: str,
        bridge_pid: int | None,
        bridge_proc_start: float | None,
        *,
        bridge_start_ticks: int | None,
    ) -> int | None:
        """Find a rediscovered pty bridge's keeper pid (delegates to :class:`Rediscovery`)."""
        return self._rediscovery._recover_keeper_pid(
            name, bridge_pid, bridge_proc_start, bridge_start_ticks=bridge_start_ticks
        )

    def _recover_keeper_identity(
        self,
        name: str,
        bridge_pid: int | None,
        bridge_proc_start: float | None,
        *,
        bridge_start_ticks: int | None,
    ) -> tuple[int | None, float | None, int | None]:
        """Return the keeper ``(pid, proc_start, ticks)`` trio (delegates to ``Rediscovery``)."""
        return self._rediscovery._recover_keeper_identity(
            name, bridge_pid, bridge_proc_start, bridge_start_ticks=bridge_start_ticks
        )

    def _await_ready_pty(self, sidecar: Path, proc: subprocess.Popen) -> dict:
        """Wait for the keeper's connect URL / exit / timeout (delegates to coordinator).

        Kept as a thin method on the runner so the direct-call test seams reach it;
        the pty readiness body lives on :class:`SpawnCoordinator` (#1157).
        """
        return self._spawner._await_ready_pty(sidecar, proc)

    def _apply_pty_info(
        self, instance: RemoteControlInstance, info: dict, proc: subprocess.Popen
    ) -> None:
        """Fold the keeper sidecar into ``instance`` (delegates to :class:`SpawnCoordinator`).

        The pty analogue of :meth:`_apply_markers`; kept separate from it by design (the two
        bridge modes share no readiness helper). Emits ``ready`` on the RUNNING transition.
        """
        self._spawner._apply_pty_info(instance, info, proc)

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
        """Spawn path for `resume_mode == "pty"` (delegates to :class:`SpawnCoordinator`, #1157).

        Launches the keeper and discovers via its sidecar; deliberately NOT unified with the
        standard :meth:`_spawn_locked` launch (different argv, different readiness). Kept as a
        thin delegator so the direct-call test seams reach it.
        """
        return await self._spawner._spawn_pty(
            instance,
            proj,
            name,
            log_path,
            permission_mode,
            resume,
            resume_session_id=resume_session_id,
        )

    def _cleanup_keeper(
        self, pid: int, *, keeper_proc_start: float | None, keeper_start_ticks: int | None
    ) -> None:
        """Wind the keeper down: reap it, then force its tree down if it lingers.

        The keeper self-exits once its bridge is gone, so for a keeper this process
        spawned this is usually just a reap. A REATTACHED keeper (pid recovered from a
        sidecar/row another process wrote, #1088) is not our child — ``reap_if_exited``
        is a no-op on it, and the real path is the grace loop plus the start-time
        re-verify before ``force_kill_tree``.

        ``keeper_proc_start`` and ``keeper_start_ticks`` are the instance row's RECORDED
        keeper start pair (#1303). They are the only thing that rejects a ``keeper_pid`` a
        row wrote which was already recycled onto a stranger BEFORE this ran; the live
        snapshot below matches such a stranger against itself. Keyword-required with no
        default so a call site that forgets them is a type error, not a silent regression.
        """
        # Identity, snapshotted BEFORE the grace: `is_keeper_process` alone answers "is this
        # pid *a* keeper", never "is it *THIS* keeper", and on a host that spawns keepers
        # continuously those differ.
        #
        # Both halves in ONE `proc_start_pair` read (#1399) — the pair API exists for this
        # and derives the epoch from the ticks rather than sampling twice. Only one half is
        # ever consulted below, so a straddled pair could not authenticate anything *here*;
        # see `proc_start_pair`'s own docstring for the sites where it could.
        expected_start, expected_ticks = procutil.proc_start_pair(pid)
        for _ in range(8):  # ~2s grace for the keeper to follow its bridge out
            procutil.reap_if_exited(pid)
            # `proc_is_gone`, not `proc_create_time(pid) is None` (#1402). The latter asks the
            # clock a liveness question: psutil's `create_time` ends in `+ boot_time()`, which
            # raises on a procfs with no `btime` (gVisor, WSL1 — see
            # `procutil.jiffies_to_epoch`), so on that host a keeper that is plainly running
            # read as gone. This loop returned before the compare below and force-killed
            # nothing, silently, while the boot-relative ticks the compare wants read fine.
            # `proc_is_gone` still counts a zombie as gone, which is what keeps this from
            # reporting an exited keeper as "no longer that keeper" further down.
            if procutil.proc_is_gone(pid):
                return  # pragma: skip-on-win
            time.sleep(0.25)
        # Re-verify before force-killing a TREE. This pid used to be reachable only as this
        # process's own child, so the check was redundant; since #1088 a keeper pid can
        # arrive from a row ANOTHER process wrote at an unknown time, and by the time the
        # grace expires the original keeper may be gone and its pid recycled. Killing the
        # tree of whatever now holds it would take down an unrelated process — including,
        # if the recycled holder is itself a keeper, that keeper's live bridge.
        #
        # This live snapshot is taken off the pid moments earlier, so it detects a recycle
        # INSIDE the grace loop and nothing before it — a `keeper_pid` that a row another
        # process wrote (#1088) had already lost to a stranger keeper matches itself here and
        # passes. The RECORDED keeper pair closes that (#1303): `keeper_proc_start` and
        # `keeper_start_ticks` come from the instance row and are ANDed below as
        # `procutil.is_live_keeper` — the same predicate `forget`'s keeper gate uses, which
        # compares the persisted pair against the live pid, so a stranger already on the pid
        # carries different persisted ticks and is rejected. It reuses `is_live_process`'s
        # exact-tick / coarse-epoch discriminator; the keeper's row carries no boot id (only
        # the sidecar the `keepers` CLI reads does). When the row recorded ticks, that coarse
        # epoch is its "same boot?" fallback, exactly as there; a row that recorded a start but
        # no ticks (pre-#1402) has `is_live_keeper` compare on the exact epoch bound instead. A
        # row with no recorded start (pre-#1178) has nothing to compare and degrades to this
        # live-snapshot gate — never more permissive than before.
        #
        # Compared EXACTLY, and deliberately not with the tolerance its siblings use — on
        # the boot-relative tick count (`proc_start_ticks`, field 22 of `/proc/<pid>/stat`)
        # wherever it is readable. `proc_create_time` would not survive an exact compare
        # across this gap: it is psutil's `starttime/CLK_TCK + boot_time()`, and
        # `boot_time()` re-reads `/proc/stat` btime on every call (verified uncached in
        # psutil 7.2.2). btime tracks the live realtime-vs-uptime offset, so an NTP step
        # between the two samples — ~2s apart, across the grace loop — shifts the epoch by
        # about a second under a keeper that never moved. Ticks are measured from the boot
        # instant and do not move at all, while a pid recycled during the grace differs by a
        # whole `CLK_TCK` of them, so exact is both drift-proof and precise here. Ticks
        # cannot survive a REBOOT (they restart at zero), which is why `is_live_process`
        # pairs them with a coarse epoch bound; a reboot between two samples 2s apart in one
        # call is not reachable, so this site needs no such conjunct.
        #
        # Ticks unavailable at the snapshot falls back to the exact epoch compare this
        # always used. On macOS and Windows that fallback is SOUND, not merely tolerated —
        # they record an absolute timestamp at exec and never re-derive it, so their epochs
        # do not drift. An unreadable `/proc` is the Linux member of the same branch and is
        # not sound, only fail-safe: psutil derives `create_time` from that very file, so in
        # practice both halves fail together and the grace loop has already returned;
        # were it reached, drift there can only over-spare. An emulated procfs with no
        # `btime` (gVisor, WSL1 — see `procutil.jiffies_to_epoch`) used to invert that: ticks
        # read fine while `proc_create_time` was None, so the grace loop's own probe fired and
        # this compare was never reached at all — a lingering keeper was never wound down and
        # nothing was logged. That loop now probes with `procutil.proc_is_gone` (#1402), which
        # needs no clock, so the tick branch below is what decides on that host.
        #
        # Every remaining gap resolves toward sparing: a tick count readable at the snapshot
        # and unreadable now compares unequal, and a snapshot with NO readable start at all
        # is rejected outright rather than matching a later unreadable one.
        #
        # Sparing is the direction we want, but it is NOT free, and the cost is worth stating
        # exactly. A keeper spared here is a keeper NO automated path recovers: `stop()` has
        # already left the instance carded (STOPPED, still persisted), and
        # `pty_keeper.find_orphan_keepers` — whose only caller is the `clauster keepers` CLI —
        # filters out every keeper whose project is carded, so plain `keepers --kill` refuses it
        # and `forget` refuses it too, as still-live. The recovery path is the explicit
        # `keepers --kill <pid> --force` (#1420), which targets the pid past that filter, or
        # killing it by hand. We
        # take that over the alternative anyway: the alternative is a `force_kill_tree` aimed
        # at a stranger, which takes down a process we do not own and, if the stranger is
        # itself a keeper, its live bridge with it. A leak is recoverable by hand; a wrong
        # kill is not recoverable at all.
        #
        # Widening either compare is what would be unsafe: `_EXACT_PROC_START_TOLERANCE`
        # (0.05s) is far too tight to absorb an NTP step anyway, and
        # `_KEEPER_START_TOLERANCE` (2.0s) is wide enough to admit a pid recycled during
        # this very grace loop. That sibling bound guards `pty_keeper.stop_keeper`'s own
        # force-kill after the same ~2s grace, where it is now the FALLBACK rather than the
        # decider: that guard takes the same exact tick compare wherever both sides have it
        # (#1402), so the epoch is consulted only on a host whose epoch does not drift.
        #
        # Order is unchanged, and chosen for freshness and cost rather than for the result:
        # both operands are pure predicates, so `and` would answer the same either way. The
        # cheap cmdline gate reads first, which leaves the start time as the last observation
        # before the kill and skips the `/proc` read entirely when the pid is not a keeper.
        # Note the zombie case rests on that gate alone now: `is_keeper_process` fails closed
        # on a zombie, whereas `proc_start_ticks` matches one happily (a zombie keeps a
        # readable `/proc/<pid>/stat`, where `proc_create_time` answered None instead).
        still_this_keeper = procutil.is_keeper_process(pid) and (
            procutil.proc_start_ticks(pid) == expected_ticks
            if expected_ticks is not None
            else expected_start is not None and procutil.proc_create_time(pid) == expected_start
        )
        # AND the RECORDED keeper pair (#1303). The live-snapshot compare above cannot see a
        # pid recycled BEFORE this method ran, so it is checked against the persisted
        # `(keeper_proc_start, keeper_start_ticks)` too: a stranger already holding the pid
        # carries different persisted ticks and is refused. Only consulted when a start was
        # recorded — an older row without one keeps the live-snapshot answer, unchanged.
        if still_this_keeper and keeper_proc_start is not None:
            still_this_keeper = procutil.is_live_keeper(
                pid, keeper_proc_start, start_ticks=keeper_start_ticks
            )
        if not still_this_keeper:
            _log.warning("keeper pid %s is no longer that keeper — not force-killing", pid)
            return
        procutil.force_kill_tree(pid)
        procutil.reap_if_exited(pid)

    def _await_ready(self, log_path: Path, proc: subprocess.Popen) -> bridge_log.BridgeMarkers:
        """Block until the standard bridge is ready/errors/times out (delegates to coordinator).

        Kept as a thin method on the runner so the direct-call test seams reach it; the
        standard-mode readiness body lives on :class:`SpawnCoordinator` (#1157), separate from
        the pty :meth:`_await_ready_pty` by design.
        """
        return self._spawner._await_ready(log_path, proc)

    @staticmethod
    def _read_markers(log_path: Path) -> bridge_log.BridgeMarkers:
        """Parse the bridge markers out of ``log_path``, returning empties if it can't be read."""
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
        """Fold parsed markers into ``instance`` + derive its status (delegates to coordinator).

        The standard-mode status appliers live on :class:`SpawnCoordinator` (#1157), kept
        separate from the pty :meth:`_apply_pty_info` by design. Emits ``ready`` only on the
        RUNNING transition. Kept as a thin delegator so the direct-call/patch test seams reach
        it.
        """
        self._spawner._apply_markers(instance, markers, proc)

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
                # On Windows `kill()` IS `terminate()` (both TerminateProcess) and neither
                # touches descendants, so killing the pid we hold leaves the real bridge
                # running whenever `claude` resolves to a `.cmd`/npm shim — the npm case is
                # the NORMAL Windows install, so "never leave an idle orphan bridge behind"
                # was exactly what this did there. Reap the tree first; the plain `kill()`
                # still runs below and stays the only path on POSIX, where the pid we hold
                # IS the bridge.
                #
                # Guarded on its own, NOT by the `except (ProcessLookupError, OSError)`
                # below: psutil's error family (`NoSuchProcess`/`AccessDenied`/
                # `ZombieProcess`) descends from `Exception`, not `OSError`, so that tuple
                # cannot catch it. An escape here is the worst of the three reap sites — it
                # would skip `kill()`, the `wait()`, AND `clear_pointer()`, leaving the
                # poisoned pointer in place (the exact loop this method exists to break)
                # and propagating out before `_persist()` writes the ERROR status.
                #
                # `wait_timeout` because the NEXT steps are gated on death: `kill()` is
                # asynchronous and `proc.wait()` below only confirms the pid WE hold (the
                # `.cmd` shim), while the pointer records the real bridge — a descendant
                # `clear_pointer`'s liveness guard would otherwise still see alive, refuse,
                # and leave the poisoned pointer for the next launch. Affordable here
                # (already off the loop in a thread), unlike the claustrum call site.
                if procutil.is_windows():
                    try:
                        await asyncio.to_thread(
                            procutil.force_kill_tree, proc.pid, wait_timeout=_TREE_REAP_WAIT
                        )
                    except Exception as exc:  # noqa: BLE001 — must not skip kill/clear_pointer
                        _log.debug("tree kill of poisoned bridge %s failed: %s", proc.pid, exc)
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
        """Return the discovered path for ``name``, or None when it is not a known project."""
        proj = self._discovered().get(name)
        return proj.path if proj is not None else None

    # ----- startup watch --------------------------------------------------

    def _start_startup_watch(self, instance_id: str) -> None:
        """Launch (or replace) the background watch for a STARTING bridge (delegates, #1157).

        The watch task is added to the shared ``registry._startup_watches`` and removed by
        its own identity-guarded done-callback, so ``shutdown()`` (which drains that one dict)
        strands nothing. Kept as a thin delegator so the direct-call test seams reach it.
        """
        self._spawner._start_startup_watch(instance_id)

    async def _watch_startup(self, instance_id: str) -> None:
        """Resolve a STARTING bridge off the request path (delegates to :class:`SpawnCoordinator`).

        Re-reads the bridge's own readiness source (bridge log for standard, keeper sidecar
        for pty — separate legs by design) until it registers, dies, or the
        ``startup_grace_seconds`` budget expires. Kept as a thin delegator so the
        direct-call test seams reach it.
        """
        await self._spawner._watch_startup(instance_id)

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
            # A pty bridge needs its keeper wound down even if the bridge pid is already
            # gone; capture it up front. A reattached keeper is NOT our child —
            # `_cleanup_keeper` re-verifies identity before it kills anything.
            keeper_pid = instance.keeper_pid
            # Read the recorded keeper start pair together with the pid, before the awaits
            # below, so the identity trio travels as one snapshot (#1303 review). Under the
            # spawn lock these row fields cannot change, so a later read would be equivalent —
            # capturing them here keeps the pid and its start pair a single read.
            keeper_proc_start = instance.keeper_proc_start
            keeper_start_ticks = instance.keeper_start_ticks
            if pid is None:
                if keeper_pid is not None:
                    await asyncio.to_thread(
                        self._cleanup_keeper,
                        keeper_pid,
                        keeper_proc_start=keeper_proc_start,
                        keeper_start_ticks=keeper_start_ticks,
                    )
                instance.status = InstanceStatus.STOPPED
                await asyncio.to_thread(self._unlock_pty_worktree, instance)  # #1089
                self._procs.pop(instance_id, None)  # release dead Popen handle; resume re-adds it
                self._emit_lifecycle("stop", instance)
                return instance

            # Re-validate identity immediately before signalling (TOCTOU / PID reuse).
            if await asyncio.to_thread(
                procutil.is_live_bridge,
                pid,
                instance.bridge_proc_start,
                start_ticks=instance.bridge_start_ticks,
                boot_id=instance.bridge_boot_id,
            ):
                # The flag-form (pty) bridge's TUI treats the first SIGINT as "press
                # again to exit"; a second confirms. The subcommand bridge stops on one.
                twice = instance.resume_mode == "pty"
                await asyncio.to_thread(self._signal_stop, pid, twice=twice)
                await self._await_exit(
                    project_name,
                    pid,
                    instance.bridge_proc_start,
                    start_ticks=instance.bridge_start_ticks,
                    boot_id=instance.bridge_boot_id,
                )
            if keeper_pid is not None:  # pragma: skip-on-win
                await asyncio.to_thread(
                    self._cleanup_keeper,
                    keeper_pid,
                    keeper_proc_start=keeper_proc_start,
                    keeper_start_ticks=keeper_start_ticks,
                )
            instance.status = InstanceStatus.STOPPED
            await asyncio.to_thread(self._unlock_pty_worktree, instance)  # #1089
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

        Fail closed on a record that is still live — STARTING/RUNNING, or a lagging status
        whose bridge/keeper process is still alive — refused with
        :class:`InstanceStillLive`; it must be Stopped first, and forget never kills a
        process. The gate covers a record present only in ``_persisted`` (the supported
        not-yet-materialized path) as well as one in the in-memory registry, reading that
        row's own ``bridge_pid``/``keeper_pid``. That branch is not hypothetical:
        ``rediscover`` deliberately leaves rows UNCARDED precisely when their bridge/keeper
        is live-but-untracked, which is the likeliest way to reach a persisted-only live
        row. Raises :class:`UnknownProject` when there's no such record at all.

        Both liveness checks are gated on the ``(pid, start-time)`` identity against PID
        reuse — ``is_live_bridge`` for the bridge, ``is_live_keeper`` for the keeper (#1178)
        — so a pid the OS recycled onto an unrelated process, *or onto a different live
        process of the same shape*, does not refuse the forget. That matters most here: a
        persisted row can predate a reboot, and since forget never kills, a false "still
        live" would strand the record with no operator path out short of hand-editing the
        state database.

        Each start-time is a PAIR — the wall-clock epoch and the boot-relative tick count
        (``bridge_start_ticks``, #1399; ``keeper_start_ticks``, #1402) — and both halves are
        passed here. The failure that forces it runs the other way from stranding: psutil's
        create-time is re-derived from a ``/proc/stat`` btime that NTP moves, so on a
        drifting host the epoch compare answers "dead" for a keeper that never restarted,
        this gate opens, and the row of a live keeper and its pty bridge is deleted. Nothing
        automated recovers that — ``forget`` did not kill them, and ``clauster keepers``
        cannot reap a keeper whose record just went away. The ticks do not move.

        ⚠️ The check is only as strong as the stored start values. Where both are absent —
        a row persisted with a null ``bridge_proc_start`` (``adopt`` writes one whenever the
        pointer has no comparable start time), or a ``keeper_pid`` written by a pre-#1178
        build with no ``keeper_proc_start`` and no ticks — ``_expected_epoch`` returns None
        and ``is_live_process`` degrades to cmdline+alive, exactly as it behaved before. That
        degrade is deliberate and is the safe direction for an upgrade: an old row keeps
        working instead of reporting its live keeper as dead. Such a row still reads as live
        if a *different* process of the right shape holds that exact pid; it re-acquires the
        full defense the next time the instance is spawned or reattached.

        ⚠️ One row shape never reaches that re-acquisition: a pid-less row :meth:`rediscover`
        leaves UNCARDED because its project already has an unresolved live pty bridge. It is
        never spawned, reattached or adopted, so nothing re-measures its keeper trio and a
        pre-#1402 one keeps comparing on the drifting epoch here indefinitely. Accepted, not
        overlooked — see migration ``c1f4a70b9e63`` for why the two ways to close it are both
        worse than the residue.
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
                # ⚠️ POLARITY: both predicates answer False/None on a psutil error, and here
                # that ALLOWS the forget — the opposite of `stop_keeper`, where False means
                # "don't kill". They fail safe there and permissive here, so read the answer
                # as "not provably alive", and never "simplify" one call site to match the
                # other's assumptions.
                if instance.bridge_pid is not None and await asyncio.to_thread(
                    procutil.is_live_bridge,
                    instance.bridge_pid,
                    instance.bridge_proc_start,
                    start_ticks=instance.bridge_start_ticks,
                    boot_id=instance.bridge_boot_id,
                ):
                    raise InstanceStillLive(
                        f"{instance_id!r} still has a live bridge — Stop it first"
                    )
                if instance.keeper_pid is not None and await asyncio.to_thread(
                    procutil.is_live_keeper,
                    instance.keeper_pid,
                    instance.keeper_proc_start,
                    start_ticks=instance.keeper_start_ticks,
                ):
                    raise InstanceStillLive(
                        f"{instance_id!r} still has a live keeper — Stop it first"
                    )
                self._instances.pop(instance_id, None)
                self._procs.pop(instance_id, None)
            else:
                # Same fail-closed gate for a row that exists ONLY in _persisted. There is no
                # status to consult, so the row's own pids are the whole check — and this is
                # the branch most likely to hold a LIVE bridge, because rediscover leaves a
                # row uncarded exactly when its bridge/keeper is alive but untracked. Pruning
                # it unchecked would orphan that process with its record gone.
                row = self._persisted.get(instance_id) or {}
                row_bridge_pid = _row_int(row.get("bridge_pid"))
                if row_bridge_pid is not None and await asyncio.to_thread(
                    procutil.is_live_bridge,
                    row_bridge_pid,
                    _row_float(row.get("bridge_proc_start")),
                    start_ticks=_row_int(row.get("bridge_start_ticks")),
                    boot_id=_row_str(row.get("bridge_boot_id")),
                ):
                    raise InstanceStillLive(
                        f"{instance_id!r} still has a live bridge — Stop it first"
                    )
                row_keeper_pid = _row_int(row.get("keeper_pid"))
                if row_keeper_pid is not None and await asyncio.to_thread(
                    procutil.is_live_keeper,
                    row_keeper_pid,
                    _row_float(row.get("keeper_proc_start")),
                    start_ticks=_row_int(row.get("keeper_start_ticks")),
                ):
                    raise InstanceStillLive(
                        f"{instance_id!r} still has a live keeper — Stop it first"
                    )
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

    async def _await_exit(
        self,
        name: str,
        pid: int,
        proc_start: float | None,
        *,
        start_ticks: int | None,  # keyword-required — see _recover_keeper_pid (#1399)
        boot_id: str | None,  # keyword-required — must match stop()'s own liveness check
    ) -> None:
        """Wait out the shutdown grace, then force the tree down and reap the child.

        ``name`` is not read: the wait is entirely on the (``pid``, ``proc_start``) pair,
        and no per-project lock or flock is taken here despite the call site passing one.

        ``start_ticks`` and ``boot_id`` must be whatever the caller's own liveness check used
        (#1399 / #1401). The two asking different questions is worse than either being wrong:
        :meth:`stop` signals only a bridge it judged LIVE, so a wait that judges the same pid
        dead breaks on the first iteration and skips both the grace AND ``force_kill_tree`` —
        leaving a bridge that ignored the signal running, under a card marked STOPPED that
        ``_reconcile_status`` will never promote back.
        """
        for _ in range(20):  # ~5s grace for a clean shutdown
            alive = await asyncio.to_thread(
                procutil.is_live_bridge, pid, proc_start, start_ticks=start_ticks, boot_id=boot_id
            )
            if not alive:
                break
            await asyncio.sleep(0.25)
        else:
            # Ignored the graceful signal (or a wrapper process is lingering, e.g.
            # a Windows .cmd shim parked at cmd.exe's prompt) -> force the tree down.
            await asyncio.to_thread(procutil.force_kill_tree, pid)
        await asyncio.to_thread(procutil.reap_if_exited, pid)

    # ----- reattach / adopt / rediscover (delegate to :class:`Rediscovery`, #1157) -----
    # The reattach/adopt/rediscover surface lives in :mod:`clauster.rediscovery`; these thin
    # methods keep the façade + the direct-call/patch test seams reachable on ``SessionRunner``.
    # Every instance a reattach materializes is written into the ONE ``RunnerState`` the
    # collaborator holds, on the loop, under the same locks — see :class:`Rediscovery`.
    # ⚠️ These are DELEGATORS: the collaborator calls its OWN copies internally, so patching one
    # of these on ``SessionRunner`` does NOT intercept a reattach/rediscover path and the test
    # passes vacuously — patch ``Rediscovery.<method>`` (or ``runner._rediscovery``) instead.

    async def _adopt_rows_from_store(self) -> None:
        """Adopt live rows another process created (delegates to :class:`Rediscovery`)."""
        await self._rediscovery._adopt_rows_from_store()

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
        """Take over pids another process resumed with (delegates to :class:`Rediscovery`)."""
        await self._rediscovery._resync_pids_from_row(
            iid, inst, saved, pair, ours_generation, discovered
        )

    @staticmethod
    def _judge_row(
        pid: int, proc_start: float | None, ticks: int | None, boot_id: str | None
    ) -> tuple[int | None, bool]:
        """Judge a row's liveness, stamping ticks (delegates to :class:`Rediscovery`)."""
        return rediscovery.Rediscovery._judge_row(pid, proc_start, ticks, boot_id)

    async def _reattach_rows_with_pids(
        self,
        discovered: dict[str, Project],
        *,
        live_only: bool = False,
        liveness: dict[str, bool] | None = None,
    ) -> tuple[set[str], set[str]]:
        """Reattach persisted rows carrying their own pids (delegates to :class:`Rediscovery`)."""
        return await self._rediscovery._reattach_rows_with_pids(
            discovered, live_only=live_only, liveness=liveness
        )

    def _connect_facts_for(
        self,
        proj: Project,
        resume_mode: ResumeMode,
        pid: int,
        proc_start: float | None,
        *,
        start_ticks: int | None,
    ) -> dict:
        """Recover a reattached bridge's connect facts (delegates to :class:`Rediscovery`)."""
        return self._rediscovery._connect_facts_for(
            proj, resume_mode, pid, proc_start, start_ticks=start_ticks
        )

    def _stopped_from_row(self, instance_id: str, saved: dict) -> RemoteControlInstance:
        """Rebuild a STOPPED card from a persisted row (delegates to :class:`Rediscovery`)."""
        return self._rediscovery._stopped_from_row(instance_id, saved)

    def _stopped_from_persisted(self, name: str) -> RemoteControlInstance | None:
        """Rebuild a STOPPED card from a gone bridge's record (delegates to ``Rediscovery``)."""
        return self._rediscovery._stopped_from_persisted(name)

    @staticmethod
    def _recovered_worktree_name(value: object) -> str | None:
        """Coerce a worktree name read off disk (delegates to :class:`Rediscovery`)."""
        return rediscovery.Rediscovery._recovered_worktree_name(value)

    async def rediscover(self, *, persist: bool = True) -> None:
        """Reattach surviving + persisted bridges at startup (delegates to ``Rediscovery``)."""
        await self._rediscovery.rediscover(persist=persist)

    def _reattach_pty_from_sidecar(self, name: str, saved: dict) -> RemoteControlInstance | None:
        """Reattach a live keeper with no row to correlate (delegates to :class:`Rediscovery`)."""
        return self._rediscovery._reattach_pty_from_sidecar(name, saved)

    def _has_unclaimed_live_keeper(self, name: str, held_keepers: set[int]) -> bool:
        """Whether a live unclaimed keeper remains for ``name`` (delegates to ``Rediscovery``)."""
        return self._rediscovery._has_unclaimed_live_keeper(name, held_keepers)

    def _modes_with_an_unclaimed_live_bridge(
        self,
        pending: dict[str, tuple[frozenset[str], Path]],
        held_keepers: set[int],
        held_pids: set[int],
    ) -> set[tuple[str, str]]:
        """Resume-modes with an untracked live bridge (delegates to :class:`Rediscovery`)."""
        return self._rediscovery._modes_with_an_unclaimed_live_bridge(
            pending, held_keepers, held_pids
        )

    async def adopt(self, name: str) -> RemoteControlInstance:
        """Take over a live standard external bridge as managed (delegates to ``Rediscovery``)."""
        return await self._rediscovery.adopt(name)

    async def _reattach_external_standard(self, proj: Project) -> RemoteControlInstance | None:
        """Reattach a live external standard bridge (delegates to :class:`Rediscovery`)."""
        return await self._rediscovery._reattach_external_standard(proj)

    def adoptable_external_projects(self) -> set[str]:
        """Project names whose live EXTERNAL session is a *standard* bridge safe to adopt.

        A standard external bridge writes an Anthropic pointer whose pid is a live
        ``claude remote-control`` subcommand process; a pty (flag-form) external bridge
        is excluded (unsafe to adopt — see :meth:`adopt`), as is one whose pointer has
        gone stale. Computed from the same pointer + cmdline + cwd-attribution checks
        :meth:`adopt` enforces, so the dashboard's Adopt affordance never offers an
        adoption that fails *those* gates. It does NOT mirror adopt's already-managed
        check, and state can change between this read and the click — :meth:`adopt`
        re-verifies under its locks and is the authority. Synchronous (filesystem +
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

    # ----- background poll (source #2 + liveness reconcile) ---------------

    async def poll_once(self, *, side_effects: bool = True) -> None:
        """Reconcile bridge liveness and cross-check `claude agents --json` (delegates, #1157).

        Public façade member (#1157): the app's own poll loop and the headless MCP server
        (``side_effects=False``) both call ``runner.poll_once`` directly, and this delegator
        preserves that exact signature. The reconcile/cross-check/prune body lives in
        :class:`PollLoop`, which writes the one registry and emits through the one record facade.
        """
        await self._poll_loop.poll_once(side_effects=side_effects)

    async def _promote_ready_unwatched(
        self, pid_is_ours: dict[str, bool], *, side_effects: bool
    ) -> None:
        """Promote a stuck-STARTING adopted bridge (delegates to :class:`PollLoop`, #1106/#1157).

        Kept as a thin method on the runner so the direct-call test seam
        (``tests/test_runner_instance_keyed.py``) reaches it unchanged after the move.
        """
        await self._poll_loop._promote_ready_unwatched(pid_is_ours, side_effects=side_effects)

    @staticmethod
    def _reconcile_status(instance: RemoteControlInstance, alive: bool) -> None:
        """Move a vanished bridge to STOPPED or CRASHED, by whether the exit was expected."""
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
        # the startup-watch via _apply_markers, or — for a bridge adopted from another
        # process, which has no watch — by `_promote_ready_unwatched`, #1106). A bridge
        # can stay alive without ever authenticating to the controller — liveness is
        # not usability, and promoting on it reported uncontrollable bridges as RUNNING.

    @staticmethod
    def _notify_message(event: str, instance: RemoteControlInstance) -> tuple[str, str]:
        """Build the (title, body) for a lifecycle notification (delegates to RecordFacade)."""
        return record_facade.RecordFacade._notify_message(event, instance)

    def _notify_event(self, event: str, instance: RemoteControlInstance) -> None:
        """Fire a best-effort lifecycle notification (delegates to :class:`RecordFacade`)."""
        self._record._notify_event(event, instance)

    def _session_ref_key(self) -> bytes:
        """Return the per-deployment ``session_ref`` HMAC key (delegates to RecordFacade)."""
        return self._record._session_ref_key()

    def _emit_lifecycle(self, event: str, instance: RemoteControlInstance) -> None:
        """Record + webhook + notify a lifecycle transition (delegates to RecordFacade).

        The single chokepoint every spawn / ready / stop / crash transition calls. The
        still-on-runner callers (``stop`` / ``forget``) reach it here; the spawn/ready path
        (now in :class:`SpawnCoordinator`, #1157) and the poll path (in :class:`PollLoop`)
        both call ``self._record._emit_lifecycle`` on the same one ``RecordFacade``. A test
        that class-patches this runner method still intercepts the ``stop``/``forget``
        callers, but one exercising the spawn or poll path patches
        ``runner._record._emit_lifecycle`` instead.
        """
        self._record._emit_lifecycle(event, instance)

    def _record_event(self, event: str, instance: RemoteControlInstance) -> None:
        """Append a session-history row off-loop (delegates to :class:`RecordFacade`)."""
        self._record._record_event(event, instance)

    def _emit_webhook(self, event: str, instance: RemoteControlInstance) -> None:
        """Fire a best-effort lifecycle webhook (delegates to :class:`RecordFacade`)."""
        self._record._emit_webhook(event, instance)

    def emit_event(self, event: str, payload: dict) -> None:
        """Fire a non-bridge lifecycle webhook off-loop (delegates to RecordFacade, #432).

        Public façade member: subsystems that don't hold the runner's :class:`WebhookEmitter`
        (the bg-agent supervisor, the hosted manager, the clone manager) route their
        already-redacted, event-shaped ``payload`` through here. No-op unless webhooks are
        active and this event is enabled (default OFF); fire-and-forget and fail-open.
        """
        self._record.emit_event(event, payload)

    def notify_app_event(self, event: str, title: str, body: str) -> None:
        """Fire a non-bridge notification off-loop (delegates to RecordFacade, #541).

        Public façade member: subsystems that don't hold the runner's :class:`Notifier` (the
        hosted manager's parked-prompt callback, the resume route) route a ready-made
        title/body through here. No-op unless the notifier is active and this event's toggle
        is on (default OFF); fire-and-forget and swallows its own errors.
        """
        self._record.notify_app_event(event, title, body)

    # ----- lifecycle ------------------------------------------------------

    async def _prune_stale_pointers(self) -> None:
        """GC long-dead ``bridge-pointer.json`` files (delegates to :class:`BridgePrune`).

        Stays ``async`` and awaits the collaborator's coroutine so the caller in
        :meth:`start_poll_loop` sees the exact same lifecycle. The pointer GC reads no
        registry — staleness is decided from config, discovery, and pointer mtimes only.
        """
        await self._prune._prune_stale_pointers()

    def _prune_one_pointer(self, project_path: Path, cutoff: float) -> None:
        """Clear one non-live, aged bridge-pointer (delegates to :class:`BridgePrune`)."""
        self._prune._prune_one_pointer(project_path, cutoff)

    async def start_poll_loop(self) -> None:
        """Rediscover already-running bridges, then start the background poll loop.

        Public façade member (#1157): the app startup calls ``runner.start_poll_loop``, and
        this delegator preserves that signature. :class:`PollLoop` rediscovers (through the
        injected ``self.rediscover`` — so a test that swaps it still intercepts), GCs stale
        pointers, then creates + OWNS the two loop tasks; ``shutdown()`` cancels + awaits +
        clears the SAME ``_poll_task`` / ``_metrics_task`` handles through the runner's proxies.
        """
        await self._poll_loop.start_poll_loop()

    async def _poll_forever(self) -> None:
        """Run ``poll_once`` on the configured interval (delegates to :class:`PollLoop`).

        The crash-resilience + ``CancelledError``-propagation loop lives in ``PollLoop``;
        this delegator keeps the direct-call test seam reaching it unchanged (#1157).
        """
        await self._poll_loop._poll_forever()

    def _cancel_startup_watch(self, instance_id: str) -> None:
        """Cancel and forget the startup watch for ``instance_id``, if one is running."""
        task = self._startup_watches.pop(instance_id, None)
        if task is not None and not task.done():  # pragma: skip-on-win
            task.cancel()

    async def shutdown(self) -> None:
        """Cancel the poll loop and startup watches, leaving managed bridges running."""
        # Bridges are left running: they are detached and survive a Clauster restart.
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
