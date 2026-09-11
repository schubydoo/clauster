"""Spawn / resume path for the bridge lifecycle — the fail-closed gate sequence (#1157).

:class:`SpawnCoordinator` is the seventh and final collaborator extracted from
:class:`~clauster.runner.SessionRunner` (issue #1157), and the security-critical one: it
owns the whole spawn/resume path — the ``claude remote-control`` (standard) and
``claude --remote-control`` under a PTY keeper (pty) launches, the pre-spawn gate
sequence that fails closed, the readiness waits, and the off-request startup watch.

Several properties here are load-bearing and preserved BYTE-FOR-BYTE from the runner:

- **Fail-closed gate order (``_spawn_locked``), unchanged.** For every launch the order
  is: (a) ``is_trusted`` check → (b) :meth:`_ensure_claude_side_settings` (the
  remote-control enable) → (c) :meth:`_enforce_bridge_cap` → (d) the deferred
  ``trust_directory`` WRITE, only AFTER the last possible raise → (e) the subprocess exec
  (:meth:`_spawn_pty` for pty, the standard ``_popen`` via
  :class:`~clauster.bridge_launch.BridgeLaunch`). No raise moves after a spawn; no spawn
  happens before the trust check; the trust write stays after the cap gate.
- **Two synchronous gates.** :meth:`_gate_stale_resume` and :meth:`_enforce_bridge_cap`
  are ``def`` (NOT ``async def``) BY DESIGN — each reads loop-owned mutable state
  (``_instances`` / ``_row_backed`` / ``_persisted``) and raises on what it read, with NO
  ``await`` between the read and the decision, so a concurrent spawn cannot interleave.
  Adding an ``await`` (or making either async) silently reopens the concurrent-spawn
  window even though the caller still holds both spawn locks.
- **Two bridge modes stay separate.** The standard path (:meth:`_spawn_locked`'s
  ``_popen`` leg, :meth:`_await_ready`, :meth:`_apply_markers`) and the pty path
  (:meth:`_spawn_pty`, :meth:`_await_ready_pty`, :meth:`_apply_pty_info`) share no helper
  — different argv, different readiness logic, by design. They are NOT unified.
- **Lock order, unchanged.** :meth:`spawn_detailed` and the resume path run under
  ``registry._spawn_lock_for(name)`` then ``registry._bridge_flock(name)``, in that
  order. The coordinator does NOT own the locks: ``stop`` / ``forget`` / ``adopt`` stay on
  :class:`~clauster.runner.SessionRunner` and keep acquiring the SAME shared locks from the
  ONE :class:`~clauster.runner_state.RunnerState`.
- **Startup-watch ownership.** :meth:`_start_startup_watch` adds the watch task to the
  shared ``registry._startup_watches`` dict; :meth:`_watch_startup`'s done-callback removes
  it, identity-guarded so a replaced watch is never popped by its predecessor.
  ``SessionRunner.shutdown()`` still drains that one dict, so no watch is stranded.

All registry reads/writes go through the ONE ``RunnerState`` (``self._registry``); every
lifecycle emission goes through the ONE :class:`~clauster.record_facade.RecordFacade`
(``self._record``); the two bridge subprocess launches go through the ONE
:class:`~clauster.bridge_launch.BridgeLaunch` (``self._launch``). There is no second
registry, and nothing that ran on the event loop is moved off it.

The still-on-runner helpers this path calls but does not own — project resolution, the
mode picker, option validation, the poisoned-pointer clear, the log-retention prune, the
redacted-mirror flush, the poison-heal, the post-spawn enrich, the readiness parsers, the
status reconciler, and the error-detail capture — are injected as callables (the same
pattern the earlier collaborators use). ``SpawnCoordinator`` keeps no ``_runner``
attribute; each injected callable is a deferring lambda that resolves the runner's CURRENT
attribute at call time, so a monkeypatched seam is honored and no bound method is frozen at
construction. The module-level exception classes, :class:`~clauster.runner.SpawnOutcome`,
and ``_normalize_custom_name`` stay on :mod:`clauster.runner` (the façade contract) and are
imported lazily inside the methods that need them, so this module carries NO module-level
``runner`` import and the two modules do not form an import cycle.

The runner delegates the moved methods to the single instance, preserving the exact public
signatures (and async-ness) that callers (``routes/*``, ``engine.py``, ``mcp_server.py``)
and the tests reach on the runner. :meth:`spawn`, :meth:`spawn_detailed`, :meth:`resume`,
and :meth:`resume_detailed` are the public members of that surface.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from . import bridge_log, config, pointers, procutil, redact, usage
from .bridge_launch import BridgeLaunch
from .claude_cli import ClaudeNotFound, resolve_binary
from .config import (
    ClausterConfig,
    PermissionMode,
    ResumeMode,
    SandboxMode,
    SpawnMode,
)
from .discovery import invalidate_discovery_cache
from .field_decode import _row_int, _sidecar_notice
from .models import InstanceStatus, Project, RemoteControlInstance
from .recap import ensure_recap_hook_installed
from .record_facade import RecordFacade
from .rediscovery import Rediscovery
from .runner_state import RunnerState
from .trust import ensure_remote_control_enabled, is_trusted, trust_directory

if TYPE_CHECKING:  # avoid the runner<->spawn_coordinator import cycle (runner imports us)
    from .runner import SpawnOutcome

_log = logging.getLogger("clauster.spawn_coordinator")

# How long to wait for a freshly-spawned bridge to reach its poll loop. Defined here
# because the readiness waits that read them (:meth:`_await_ready`, :meth:`_await_ready_pty`)
# moved here from the runner (#1157); ``clauster.runner`` re-exports them so
# ``from clauster.runner import _READY_TIMEOUT`` and any still-on-runner reader keep working.
_READY_TIMEOUT = 15.0
_READY_POLL_INTERVAL = 0.25
# #867 L3: a *reattach* can reach the poll loop and only THEN have its re-adopted session
# torn down as archived/deleted (#671). After readiness on a reattach (no fresh "Created
# initial session"), watch a brief grace for that poison marker before declaring RUNNING; a
# cold start skips it.
_POISON_GRACE = 4.0
# Cadence at which the post-spawn startup-watch re-reads the bridge log to detect
# a (late) environment registration or a stuck-but-alive bridge.
_STARTUP_WATCH_INTERVAL = 2.0


class SpawnCoordinator:
    """Own the spawn/resume path, the fail-closed gate sequence, and the startup watch."""

    def __init__(
        self,
        *,
        config: ClausterConfig,
        registry: RunnerState,
        record: RecordFacade,
        launch: BridgeLaunch,
        rediscovery: Rediscovery,
        claude_json: Path,
        settings_json: Path,
        resolve_project: Callable[[str], Project],
        discovered: Callable[[], dict[str, Project]],
        is_pty_mode: Callable[..., bool],
        validate_spawn_options: Callable[..., None],
        live_standard_for_project: Callable[[str], RemoteControlInstance | None],
        clear_pointer_if_anchor_poisoned: Callable[[Path], Awaitable[None]],
        log_set_key: Callable[[str], str],
        prune_logs: Callable[[set[str]], None],
        unique_log_path: Callable[[str], Path],
        raw_log_path_for: Callable[[Path], Path],
        flush_redacted_mirror: Callable[[RemoteControlInstance], None],
        heal_poisoned_reattach: Callable[..., Awaitable[None]],
        post_spawn_enrich: Callable[[RemoteControlInstance, Path], Awaitable[None]],
        sidecar_path_for: Callable[[Path], Path],
        screen_sidecar_path_for: Callable[[Path], Path],
        pty_worktree_name: Callable[[RemoteControlInstance], str | None],
        read_markers: Callable[[Path], bridge_log.BridgeMarkers],
        read_sidecar: Callable[[Path], dict | None],
        reconcile_status: Callable[[RemoteControlInstance, bool], None],
        project_path: Callable[[str], Path | None],
        capture_error_detail: Callable[[RemoteControlInstance], None],
    ) -> None:
        """Bind to config + the shared registry/record/launch/rediscovery and injected helpers.

        ``registry`` is the ONE :class:`~clauster.runner_state.RunnerState`, ``record`` the
        ONE :class:`~clauster.record_facade.RecordFacade`, ``launch`` the ONE
        :class:`~clauster.bridge_launch.BridgeLaunch`, and ``rediscovery`` the ONE
        :class:`~clauster.rediscovery.Rediscovery`, all built in ``SessionRunner.__init__``
        and passed by reference so every spawn write, lifecycle emission, subprocess launch,
        and cross-process reattach lands on the same source of truth. The remaining arguments
        are callables the runner still owns (or that other collaborators own, reached through
        the runner's own delegators): they are injected — never captured as back-references —
        so ``SpawnCoordinator`` holds no runner. Each is a deferring lambda that resolves the
        runner's CURRENT attribute at call time (not a bound method captured at construction),
        so a test that swaps one of them after the runner is built still intercepts the call.

        The remote-control / recap-hook latches (:attr:`_rc_setting_ensured` /
        :attr:`_recap_hook_ensured`) live here because :meth:`_ensure_claude_side_settings`,
        their only reader/writer, moved here; ``SessionRunner`` re-exposes them as proxy
        properties so the tests that assert on them still read the one copy.
        """
        self._config = config
        self._registry = registry
        self._record = record
        self._launch = launch
        self._rediscovery = rediscovery
        self._claude_json = claude_json
        self._settings_json = settings_json
        self._resolve_project = resolve_project
        self._discovered = discovered
        self._is_pty_mode = is_pty_mode
        self._validate_spawn_options = validate_spawn_options
        self._live_standard_for_project = live_standard_for_project
        self._clear_pointer_if_anchor_poisoned = clear_pointer_if_anchor_poisoned
        self._log_set_key = log_set_key
        self._prune_logs = prune_logs
        self._unique_log_path = unique_log_path
        self._raw_log_path_for = raw_log_path_for
        self._flush_redacted_mirror = flush_redacted_mirror
        self._heal_poisoned_reattach = heal_poisoned_reattach
        self._post_spawn_enrich = post_spawn_enrich
        self._sidecar_path_for = sidecar_path_for
        self._screen_sidecar_path_for = screen_sidecar_path_for
        self._pty_worktree_name = pty_worktree_name
        self._read_markers = read_markers
        self._read_sidecar = read_sidecar
        self._reconcile_status = reconcile_status
        self._project_path = project_path
        self._capture_error_detail = capture_error_detail
        # Mark remote control as acknowledged once, before the first spawn.
        self._rc_setting_ensured = False
        # Install the resume-recap SessionStart hook once, before the first spawn.
        self._recap_hook_ensured = False

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

        Validates spawn/permission modes, fails closed on an untrusted directory, then
        best-effort pre-enables remote control and the recap hook when configured (each
        gated on its config flag, attempted once per process, and an ``OSError`` only
        warns), launches the process, and watches it until it reaches RUNNING or ERROR.

        ``resume_mode`` ("standard"/"pty") picks the launch mode for *this* bridge,
        overriding the ``claude.launch_mode`` config default (the per-launch picker).
        When the effective mode is ``"pty"`` (POSIX only), the bridge is the
        ``claude --remote-control`` flag form run under a :mod:`clauster.pty_keeper`
        for true conversation resume; ``resume=True`` (set by :meth:`resume`) adds
        ``--continue`` so the restarted session restores its prior context. The mode
        is fixed at first launch and recorded on the instance, so a resume always
        keeps it (see ``_is_pty_mode``).

        ``custom_name`` (#780) is an optional operator-supplied display name for a
        *standard* (server-mode) bridge, passed as ``claude remote-control --name``
        in place of the project name. Blank/``None`` keeps today's default (the
        project name); it is validated by ``_normalize_custom_name`` before any
        spawn side effect. The pty (Interactive Session) launch form has no
        equivalent flag, so it's ignored there (see #780 disposition).

        ``sandbox`` (#780) is the per-launch OS-level filesystem/network isolation
        toggle for a *standard* bridge — tri-state ``"default"``/``"on"``/``"off"``
        (``None`` == ``"default"``). It is validated before any spawn side effect (a bad
        value still 422s), but the toggle is DISABLED for 1.0 (#1037,
        ``config.SANDBOX_TOGGLE_ENABLED``): every requested value is coerced to
        ``"default"``, so NEITHER ``--sandbox`` nor ``--no-sandbox`` currently reaches the
        bridge. #1046 re-enables it, at which point ``"on"`` appends ``--sandbox`` and
        ``"off"`` appends ``--no-sandbox``, while ``"default"`` keeps appending neither
        (claude's own off-by-default / ``sandbox.*`` settings win). Like ``custom_name``
        it is standard-only; the pty form is out of scope for #780.

        Concurrent spawns of the *same* project are serialized by a per-project lock:
        a double-click, retry, or second browser tab must not both pass the
        idempotency check and launch two bridges, because the second would clobber
        the first in ``_instances``/``_procs`` and orphan an untracked, unreapable
        process. Different projects still spawn concurrently. Since #949 the same section
        also holds a per-project *cross-process* lock (``registry._bridge_flock``) held
        through the readiness wait, so a SECOND clauster process (headless CLI/MCP writer
        vs the live web app) serializes here too and its own check-then-launch can't
        interleave with ours; its idempotency check additionally probes the on-disk bridge
        pointer (see :meth:`_spawn_locked`), which our bridge has typically written by the
        time the lock is released.

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
        side effect; invalid values raise :class:`~clauster.runner.InvalidSpawnOption` (→ 422).

        ``trust`` (the headless CLI's ``--trust``, #775) accepts the workspace-trust
        dialog for the project as part of this spawn. It is applied *after* option
        validation and under the per-project spawn lock — so an invalid option (a bad
        ``custom_name``, a forbidden permission mode, a worktree on a non-git project)
        raises without leaving the directory trusted, and the trust write can't race a
        concurrent spawn/stop. Left False, an untrusted directory raises
        :class:`~clauster.runner.NotTrusted` (unchanged). The dashboard trusts via a separate
        explicit action (``trust_project``); this is the headless equivalent, kept atomic.

        Returns a :class:`~clauster.runner.SpawnOutcome`: ``created`` is False when an
        already-live instance was returned instead of launching (with ``reason``), and
        ``warnings`` carries non-blocking advisories (the pty no-worktree collision
        warning) so the API can surface them (#778).
        """
        async with (
            self._registry._spawn_lock_for(name),
            self._registry._bridge_flock(name),
        ):
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

    # ----- _spawn_locked's pre-spawn gates, in the order the spawn runs them ---------
    # Extracted from _spawn_locked (#1155) so the gate ordering is legible in one screen:
    # stale-resume → option validation → fork-target ownership → per-mode idempotency →
    # trust gate → claude-side settings → poisoned-pointer clear → bridge cap →
    # deferred --trust write → launch. Each helper preserves its gate's logic, ordering
    # and messages exactly; _gate_stale_resume is the one whose nested guards were
    # inverted into early returns, and nothing here relaxes or short-circuits a gate.
    #
    # ⚠️ _gate_stale_resume and _enforce_bridge_cap are SYNCHRONOUS BY DESIGN. Each reads
    # loop-owned mutable state (_instances / _row_backed / _persisted) and raises on what
    # it read; with no await between the read and the decision, a concurrent spawn cannot
    # interleave. Making either async — or adding an await inside one — silently reopens
    # that window even though the caller still holds both spawn locks.

    def _gate_stale_resume(
        self,
        *,
        resume: bool,
        resume_target: RemoteControlInstance | None,
        refreshed: bool,
    ) -> None:
        """Refuse a resume whose row another clauster process already forgot (#951 round 4).

        Resuming a DEAD card whose row-backed record is gone from the fresh merge base
        would relaunch — and re-persist — a session another clauster process explicitly
        forgot, silently undoing that delete. Fail closed with the truth instead, and drop
        the card (it was only a view of the deleted row). A LIVE resume target is
        untouched — it falls through to the idempotent already-running return in
        :meth:`_apply_mode_spawn_policy`. When the refresh itself failed, the gate must not
        decide from the known-stale snapshot (#951 round 5): refuse the resume as
        retryable — WITHOUT dropping the card, since we couldn't learn whether its row is
        actually gone. A plain (non-resume) spawn proceeds on a failed refresh: the store
        is non-authoritative and launching bridges must not depend on it; ``_persist``
        re-checks on its own.
        """
        from .runner import SpawnError, UnknownProject

        if not resume or resume_target is None:
            return
        iid = resume_target.instance_id
        dead = resume_target.status not in (InstanceStatus.STARTING, InstanceStatus.RUNNING)
        if not dead or iid not in self._registry._row_backed:
            return
        if not refreshed:
            raise SpawnError(
                f"could not verify session {iid} against the shared state store "
                "(transient read failure) — try the resume again"
            )
        if iid not in self._registry._persisted:
            self._registry._instances.pop(iid, None)
            raise UnknownProject(
                f"session {iid} was forgotten by another clauster process — nothing left to resume"
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
        """Validate a fork-a-past-conversation target BEFORE any spawn side effect (#303).

        Strict by construction: this string ends up on a subprocess argv, so nothing but a
        UUID shape may pass (fail closed; list-argv means no shell, but defense in depth).
        Raises :class:`~clauster.runner.InvalidSpawnOption` (→ 422) on every rejection.
        """
        from .runner import _SESSION_UUID_RE, InvalidSpawnOption

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
        # walks the project's own sanitized-cwd transcript dir PLUS its worktree
        # dirs (#1020) — the same source the picker lists from — so anything it
        # can't resolve is rejected before any spawn side effect.
        #
        # The worktree dirs are found by name prefix, and the same punctuation
        # ambiguity described below applies to them: a sibling project named
        # "<project>--claude-worktrees-x" sanitizes into this project's worktree
        # prefix. _transcript_dirs_for therefore excludes every real sibling
        # project's directory, so the set stays this project's own.
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
        #
        # Everything from here down carries the Windows-exclusion pragma (#1324): it
        # sits BEHIND the `effective_resume_mode != "pty"` raise above, and pty mode is
        # off on the Windows CI run (`_is_pty_mode` → False without pywinpty — see
        # `.coveragerc-win`), so a Windows execution always raises before reaching it.
        # Both raises below are fail-closed fork-ownership gates, and they stay measured
        # for real on Linux/macOS — if the Windows job ever gains pywinpty, drop these
        # pragmas rather than let the exclusions hide a gate that can then run there.
        proj_dir = pointers.sanitize_cwd(proj.path)  # pragma: skip-on-win
        colliding = [  # pragma: skip-on-win
            other.name
            for other in self._discovered().values()
            if other.name != proj.name and pointers.sanitize_cwd(other.path) == proj_dir
        ]
        if colliding:  # pragma: skip-on-win
            raise InvalidSpawnOption(
                f"cannot fork a conversation for {name!r}: its transcript directory "
                f"is shared with project(s) {sorted(colliding)!r} (paths differing only "
                "in punctuation), so conversation ownership is ambiguous"
            )
        resolved_transcript = await asyncio.to_thread(  # pragma: skip-on-win
            usage.resolve_session_transcript, proj.path, resume_session_id
        )
        if resolved_transcript is None:  # pragma: skip-on-win
            raise InvalidSpawnOption(
                f"resume_session_id {resume_session_id!r} is not a conversation "
                f"of project {name!r}"
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
        """Apply the per-mode idempotency policy (#777); return an outcome to hand back.

        A non-``None`` return means an already-live bridge satisfies this spawn and NOTHING
        should be launched — :meth:`_spawn_locked` returns it verbatim. ``None`` means carry
        on to the trust gate and the launch. Non-blocking advisories are appended to
        ``spawn_warnings`` (#778). The two bridge modes stay deliberately separate here.
        """
        from .runner import SpawnOutcome

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
            # take-over :meth:`~clauster.rediscovery.Rediscovery.adopt` performs, with the
            # same live-standard gate (a dead pointer or a pty/flag-form bridge fails it
            # and we launch normally).
            reattached = await self._rediscovery._reattach_external_standard(proj)
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
        return None

    async def _ensure_claude_side_settings(self) -> None:
        """Pre-write the two claude-side settings a bridge start depends on, once per runner.

        Both are BEST-EFFORT by design and neither may fail the spawn — but neither is
        silent either: an :class:`OSError` is logged at WARNING and the ``_ensured`` latch
        still flips, so each write is attempted exactly once per runner (the latches are
        instance state, so a second :class:`~clauster.runner.SessionRunner` in the same
        process retries).
        """
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

    def _enforce_bridge_cap(self, max_bridges: int | None) -> None:
        """Fail closed when the optional concurrent-bridge cap is already reached.

        EVERY live bridge counts, this project's included. The per-mode idempotency
        early-returns in :meth:`_apply_mode_spawn_policy` already sent back any live bridge
        this spawn would duplicate, so nothing reaching here is the instance being spawned —
        but the same project can legitimately hold a live bridge on the OTHER axis (a live
        pty session does not block a standard spawn, and vice versa), and skipping those made
        a project with an interactive session contribute 0 to the cap. Raised BEFORE the
        log-file creation, the process spawn, and the deferred ``--trust`` write — the one
        earlier side effect is the injected ``_clear_pointer_if_anchor_poisoned``, which is
        best-effort pointer hygiene rather than per-spawn state.
        """
        from .runner import CapacityExceeded

        if max_bridges is None:
            return
        live = sum(
            1
            for inst in self._registry._instances.values()
            if inst.status in (InstanceStatus.STARTING, InstanceStatus.RUNNING)
        )
        if live >= max_bridges:
            raise CapacityExceeded(
                f"max_bridges={max_bridges} reached ({live} live); "
                "stop a bridge before starting another"
            )

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
        """Spawn (or hand back) a bridge for ``name`` with the per-project lock held.

        The body of :meth:`spawn_detailed`, split out so the locking lives in the caller.
        """
        from .runner import NotTrusted, SpawnOutcome, _normalize_custom_name

        proj = self._resolve_project(name)
        # Refresh the persist merge-base under the locks (#949): the persisted-record
        # reads below (the reattach probe's saved modes) and this spawn's trailing
        # _persist must see the shared store as it is NOW, not as it was when this
        # runner was constructed — a headless writer's construction-time snapshot can
        # predate rows the web app has since added or forgotten.
        refreshed = await self._registry._refresh_persisted()
        self._gate_stale_resume(resume=resume, resume_target=resume_target, refreshed=refreshed)
        defaults = self._config.instance_defaults
        spawn_mode = spawn_mode or defaults.spawn_mode
        permission_mode = permission_mode or defaults.permission_mode
        # None == "default" (append neither sandbox flag); normalize up front so the value
        # validated below is always one of SANDBOX_MODES. The toggle is DISABLED for 1.0
        # (#1037, config.SANDBOX_TOGGLE_ENABLED): the requested value is still validated (a bad
        # value 422s below) but coerced to "default" so nothing on/off is recorded, resumed, or
        # emitted while `--sandbox` doesn't reach the server-mode worker. Re-enabled via #1046.
        requested_sandbox: SandboxMode = sandbox or "default"
        sandbox_mode: SandboxMode = (
            requested_sandbox if config.SANDBOX_TOGGLE_ENABLED else "default"
        )
        # Resolve resume_mode early so we can apply the per-mode policy checks below
        # before spending side-effect budget (trust writes, log file creation, etc.).
        # For a resume the prior instance is the SPECIFIC one being revived
        # (resume_target) — NOT a mode-agnostic project scan, which could return a
        # coincidentally-live standard bridge and flip a pty resume to standard (#777).
        prior_for_mode = resume_target if resume else None
        effective_resume_mode: ResumeMode = (
            "pty" if self._is_pty_mode(prior_for_mode, requested=resume_mode) else "standard"
        )
        self._validate_spawn_options(
            proj, spawn_mode, permission_mode, resume_mode, requested_sandbox
        )
        if resume_session_id is not None:
            await self._validate_resume_session_id(
                proj,
                name,
                resume_session_id,
                resume=resume,
                effective_resume_mode=effective_resume_mode,
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
        already_live = await self._apply_mode_spawn_policy(
            proj,
            name,
            effective_resume_mode,
            spawn_mode=spawn_mode,
            resume=resume,
            resume_target=resume_target,
            spawn_warnings=spawn_warnings,
        )
        if already_live is not None:
            return already_live
        # --- end per-mode spawn policy ---------------------------------------

        # Workspace-trust gate. Without --trust an untrusted directory fails closed here
        # (fast, before any spawn side effect). With --trust we do NOT trust yet — the
        # trust write is deferred until after the capacity check below, so a rejected
        # start (bad option OR a full bridge cap) never leaves the directory trusted.
        if not trust and not await asyncio.to_thread(is_trusted, proj.path, self._claude_json):
            raise NotTrusted(
                f"directory not trusted: {proj.path}. Use the Trust action before starting."
            )

        await self._ensure_claude_side_settings()

        # #867 L2: before launching, drop a preserved pointer whose anchor was archived or
        # deleted — otherwise the CLI reattaches it and the bridge comes back with no
        # session (#671). Best-effort; a no-op for a cold start, a pty spawn, or when the
        # anchor is healthy/indeterminate.
        await self._clear_pointer_if_anchor_poisoned(proj.path)

        self._enforce_bridge_cap(defaults.max_bridges)

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
            for inst in self._registry._instances.values()
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
            # And carry the EXPLICIT worktree name when the target has one (#1241) — a
            # session rediscovered from its keeper sidecar was carded under a fresh id, so
            # for it the derivation above is exactly what does not hold. Dropping the name
            # here would resume into a new, empty worktree and orphan the one holding the
            # session's uncommitted work.
            instance.worktree_name = resume_target.worktree_name
        # Register under instance_id — the stable UUID minted by RemoteControlInstance
        # (via new_instance_id default_factory) or carried over from the instance
        # being resumed, NOT the project name.
        self._registry._instances[instance.instance_id] = instance  # on the loop

        # One spawn-event chokepoint for both modes: the instance is registered, STARTING,
        # and its resume_mode is now resolved. A "ready" follows iff it reaches RUNNING.
        self._record._emit_lifecycle("spawn", instance)
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
                self._launch._popen,
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
            await self._registry._persist()
            # created=True: a new registry row exists (in ERROR), not a reused one.
            return SpawnOutcome(instance=instance, created=True, warnings=spawn_warnings)
        self._registry._procs[instance.instance_id] = proc
        instance.bridge_pid = proc.pid
        # ONE sample for both halves (#1399). Two `to_thread` hops would put a full
        # suspension point between them, and a bridge that dies in that window — a startup
        # failure, which is exactly what this runs before `_await_ready` to catch — can have
        # its pid recycled, leaving a pair describing two processes. `stop()` signals and
        # force-kills the tree behind that pair. The keeper states the same rule for its
        # sidecar; both now go through the one helper so they cannot drift apart.
        instance.bridge_proc_start, instance.bridge_start_ticks = await asyncio.to_thread(
            procutil.proc_start_pair, proc.pid
        )
        # The boot these ticks belong to (#1401), so `is_live_process` can reject a later
        # boot's recycled pid on identity rather than on the wall-clock epoch NTP moves. A
        # separate hop from the pair is fine: boot_id is a system-wide per-boot value, not a
        # per-pid one, so it needs no atomic sample with this pid's ticks. Off-thread to keep
        # even a tiny `/proc/sys` read off the event loop, like the pair read above.
        instance.bridge_boot_id = await asyncio.to_thread(procutil.proc_boot_id)

        markers = await asyncio.to_thread(self._await_ready, raw_path, proc)
        self._apply_markers(instance, markers, proc)
        await asyncio.to_thread(self._flush_redacted_mirror, instance)
        if markers.poison_reason is not None:
            await self._heal_poisoned_reattach(instance, proc, proj.path, markers.poison_reason)
        else:
            await self._post_spawn_enrich(instance, proj.path)
        await self._registry._persist()
        # A bridge still STARTING after the synchronous readiness wait may yet
        # register (slow start) or may be alive-but-stuck (e.g. it couldn't
        # authenticate to the controller). Watch it off the request path so it is
        # only ever promoted to RUNNING once it actually registers an environment.
        if instance.status is InstanceStatus.STARTING:
            self._start_startup_watch(instance.instance_id)
        return SpawnOutcome(instance=instance, created=True, warnings=spawn_warnings)

    async def resume(self, instance_id: str) -> RemoteControlInstance:
        """Re-spawn a stopped/crashed bridge; the instance only.

        The thin wrapper over :meth:`resume_detailed`, mirroring
        :meth:`spawn` / :meth:`spawn_detailed`. Callers that must tell "revived" from
        "the cap handed back a DIFFERENT, already-live bridge" need the outcome, not the
        instance — see :meth:`resume_detailed`.
        """
        return (await self.resume_detailed(instance_id)).instance

    async def resume_detailed(self, instance_id: str) -> SpawnOutcome:
        """Re-spawn a stopped/crashed bridge, reconnecting to its prior session.

        Returns the full :class:`~clauster.runner.SpawnOutcome` because a resume can
        legitimately decline to revive anything: standard bridges are capped at one live per
        project, and the cap is enforced by RETURNING the live bridge rather than
        raising (#1145). Dropping ``created``/``reason`` here made the API answer a
        declined resume with **200 and an instance that was never revived** — usually a
        different bridge, though the cap and the pty already-live path can both hand back
        the target itself — which the dashboard then reported as success. A silent failure
        in the bridge lifecycle, the one thing this project's first invariant forbids.

        Re-running ``claude remote-control`` in the same cwd reconnects to the
        existing environment + session (the bridge-pointer.json the prior run
        left behind drives it — empirically confirmed). We reuse the stopped
        instance's stored ``spawn_mode``/``permission_mode`` so the resume keeps
        the same permission mode (a *fresh* bare start would drop back to the
        default 'ask'). The session id, which a reconnecting bridge does NOT
        re-log, is recovered from the pointer by :meth:`_spawn_locked`'s enrich step.

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
        from .runner import UnknownProject

        existing = self._registry._instances.get(instance_id)
        if existing is None:
            raise UnknownProject(f"no managed instance to resume: {instance_id!r}")
        # Only forward a label that is a genuine custom name; a bare project-name label
        # → None so the fallback path (not the validator) runs on resume, exactly as it
        # did on first spawn where custom_name was None.
        carried_name = existing.label if existing.label != existing.project else None
        return await self.spawn_detailed(
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
            # Forward the recorded sandbox tri-state so the choice survives a resume,
            # parity with custom_name (#780). Inert while the toggle is disabled (#1037):
            # the recorded value is always "default" until #1046 re-enables it.
            sandbox=existing.sandbox_mode,
        )

    # ----- pty / Interactive Session mode (true conversation resume) ------

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
        bp = _row_int(info.get("bridge_pid"))
        if bp is not None:
            instance.bridge_pid = bp
            ps = info.get("bridge_proc_start")
            if isinstance(ps, (int, float)) and not isinstance(ps, bool):
                instance.bridge_proc_start = float(ps)
            # From the SIDECAR, never re-measured off the live pid (#1399): a fresh
            # `proc_start_ticks` read would agree with whatever holds that pid by
            # construction, so it could authenticate a recycled process the sidecar's own
            # pair rejects. The keeper writes both halves in one breath for this reason.
            instance.bridge_start_ticks = _row_int(info.get("bridge_start_ticks"))
            # The keeper spawned this pty bridge in THIS process, so it is running in the
            # current boot (#1401). The sidecar records no boot id, so stamp the live one beside
            # the ticks — otherwise a pty spawn (which returns before `_spawn_locked`'s own
            # stamp) persists boot-id-less and keeps the coarse-epoch fallback until a later
            # reattach re-stamps it. Read inline rather than off-thread like the other stamp
            # sites: `/proc/sys/kernel/random/boot_id` is a fixed 37-byte kernel-memory
            # pseudo-file, not disk I/O, so it cannot stall the loop — and this method is a sync
            # helper called from two on-loop sites, so a thread hop would have to be plumbed
            # through both.
            instance.bridge_boot_id = procutil.proc_boot_id()
        if sid := info.get("session_id"):
            instance.starter_session_id = sid
        if url := info.get("connect_url"):
            instance.url = url
        # Assigned unconditionally, unlike the fields above: those only ever gain a value
        # (a sidecar re-read during the startup watch must not un-learn a pid), but a note
        # is a statement about the CURRENT sidecar. Clearing it when the sidecar no longer
        # carries one is what lets an advisory go away instead of sticking to the card.
        instance.notice = _sidecar_notice(info)

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
            self._record._emit_lifecycle("ready", instance)  # only on the transition

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
        bridge_argv = self._launch._build_pty_bridge_argv(
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
                self._launch._popen_keeper,
                proj.path,
                sidecar,
                bridge_argv,
                screen_sidecar,
                state_dir=self._config.state_dir,
            )
        except (OSError, ClaudeNotFound) as exc:
            _log.warning("pty spawn of %s failed to launch: %s", name, exc)
            instance.status = InstanceStatus.ERROR
            await self._registry._persist()
            return instance
        self._registry._procs[instance.instance_id] = proc
        instance.keeper_pid = proc.pid
        # Snapshot the keeper's start identity with its pid (#1178) so a later `forget` can
        # tell THIS keeper from another one that inherited the pid. Read immediately after
        # the spawn, while the process is certainly still ours; None (an already-exited
        # keeper, or a psutil error) degrades to the cmdline-only gate rather than pairing
        # the pid with a start time that isn't its own.
        #
        # ONE `proc_start_pair` read for both halves (#1402), not a create-time read plus a
        # ticks read: the boot-relative half is what keeps this identification from moving
        # when NTP corrects the host clock, and sampling the two separately can straddle a
        # pid recycle and produce a pair describing two different processes.
        instance.keeper_proc_start, instance.keeper_start_ticks = await asyncio.to_thread(
            procutil.proc_start_pair, proc.pid
        )
        info = await asyncio.to_thread(self._await_ready_pty, sidecar, proc)
        self._apply_pty_info(instance, info, proc)
        await asyncio.to_thread(self._flush_redacted_mirror, instance)
        if instance.status is InstanceStatus.ERROR:
            # Surface whatever the keeper recorded (openpty/spawn failure); the
            # bridge's own failure reason, if any, is in its --debug-file on disk.
            #
            # Redact + bound it exactly as `_capture_error_detail` does for the other
            # error_detail writer (invariant 4): this field is rendered inline on the
            # dashboard card, and the keeper interpolates arbitrary exception text into
            # it (`pty_keeper`'s conpty read/liveness/wait/abort reasons, #1389) — text
            # that has passed through no redactor on its way here.
            keeper_error = info.get("error")
            instance.error_detail = (
                redact.redact_for_disk(keeper_error)[-2000:]
                if isinstance(keeper_error, str)
                else None
            )
        await self._registry._persist()
        if instance.status is InstanceStatus.STARTING:
            self._start_startup_watch(instance.instance_id)
        return instance

    # ----- standard-mode readiness ----------------------------------------

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

    def _apply_markers(
        self,
        instance: RemoteControlInstance,
        markers: bridge_log.BridgeMarkers,
        proc: subprocess.Popen,
    ) -> None:
        """Fold parsed markers into ``instance`` and derive its status from them plus liveness.

        Emits the ``ready`` lifecycle event only on the transition into RUNNING, never on
        every poll.
        """
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
            self._record._emit_lifecycle("ready", instance)  # only on the transition

    # ----- startup watch --------------------------------------------------

    def _start_startup_watch(self, instance_id: str) -> None:
        """Launch (or replace) the background watch for a STARTING bridge."""
        old = self._registry._startup_watches.pop(instance_id, None)
        if old is not None and not old.done():
            old.cancel()
        task = asyncio.create_task(
            self._watch_startup(instance_id), name=f"startup-watch:{instance_id}"
        )
        self._registry._startup_watches[instance_id] = task

        def _done(t: asyncio.Task, _iid: str = instance_id) -> None:
            """Drop the finished watch from the registry and log an unexpected failure."""
            if self._registry._startup_watches.get(_iid) is t:
                self._registry._startup_watches.pop(_iid, None)
            if not t.cancelled() and (exc := t.exception()) is not None:
                _log.warning("startup-watch for %s failed: %s", _iid, exc)

        task.add_done_callback(_done)

    async def _watch_startup(self, instance_id: str) -> None:
        """Resolve a STARTING bridge off the request path.

        Re-reads the bridge's own readiness source until it registers — the bridge log
        for a standard bridge (:meth:`_apply_markers` promotes it to RUNNING, then the
        injected ``_post_spawn_enrich`` runs), the keeper sidecar for a pty bridge
        (:meth:`_apply_pty_info`; readiness is the connect URL, and no enrich step) — or
        until the ``startup_grace_seconds`` budget expires while it is still alive but
        unregistered, which is a failed start (ERROR), not a running bridge. Both legs
        re-flush the redacted mirror each tick. Process death during startup is delegated
        to the injected ``_reconcile_status`` so the CRASHED/STOPPED outcome matches the
        poll loop exactly.
        """
        grace = self._config.claude.startup_grace_seconds
        deadline = time.monotonic() + grace
        while True:
            await asyncio.sleep(_STARTUP_WATCH_INTERVAL)
            instance = self._registry._instances.get(instance_id)
            proc = self._registry._procs.get(instance_id)
            if instance is None or proc is None or instance.status is not InstanceStatus.STARTING:
                return  # already resolved, stopped, or gone
            if proc.poll() is not None:  # exited during startup
                self._reconcile_status(instance, False)
                await self._registry._persist()
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
                    await self._registry._persist()
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
                    await self._registry._persist()
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
                await self._registry._persist()
                return
