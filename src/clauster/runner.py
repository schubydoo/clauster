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
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from . import bridge_log, inspector, pointers, procutil
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
from .discovery import discover_projects, is_valid_project_name
from .models import (
    Attribution,
    InstanceStatus,
    Project,
    RemoteControlInstance,
    WorkingSession,
)
from .recap import ensure_recap_hook_installed
from .state import StateStore
from .trust import ensure_remote_control_enabled, is_trusted, trust_directory

_log = logging.getLogger("clauster.runner")


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

    def __init__(self, config: ClausterConfig, claude_json: Path | None = None) -> None:
        self._config = config
        self._binary = config.claude.binary
        self._claude_json = claude_json or Path("~/.claude.json").expanduser()
        self._log_dir = (config.state_dir / "logs").expanduser()
        self._instances: dict[str, RemoteControlInstance] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._sessions: list[WorkingSession] = []
        self._poll_task: asyncio.Task | None = None
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
        # Lightweight persistence of label / intentional_stop / spawn_mode (D14).
        self._state = StateStore(config.state_dir)
        self._persisted: dict[str, dict] = self._state.load()
        self._last_saved: dict[str, dict] | None = None

    # ----- read API -------------------------------------------------------

    @property
    def claude_json(self) -> Path:
        """The claude.json whose trusted-dirs this runner honors (for trust checks)."""
        return self._claude_json

    def list_instances(self) -> list[RemoteControlInstance]:
        """Return a snapshot list of all managed bridge instances."""
        return list(self._instances.values())

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
        """Write the persisted subset off-loop, but only when it actually changed."""
        subset = self._persist_subset()
        if subset == self._last_saved:
            return
        await asyncio.to_thread(self._state.save, subset)
        self._last_saved = subset
        # Keep the merge base in sync with what's on disk so the next overlay builds
        # on the latest saved state (live modes that changed this round are retained).
        self._persisted = subset

    # ----- discovery helpers ---------------------------------------------

    def _discovered(self) -> dict[str, Project]:
        return {
            p.name: p for p in discover_projects(self._config.projects_root, self._claude_json)
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
        # Re-read so the returned Project reflects the new trust state.
        return self._discovered().get(name, proj)

    # ----- spawn ----------------------------------------------------------

    async def spawn(
        self,
        name: str,
        *,
        spawn_mode: str | None = None,
        permission_mode: str | None = None,
        resume_mode: str | None = None,
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
        spawn_mode: str | None = None,
        permission_mode: str | None = None,
        resume_mode: str | None = None,
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

        log_path = self._unique_log_path(name)
        instance = RemoteControlInstance(
            project=name,
            label=name,
            status=InstanceStatus.STARTING,
            bridge_debug_log_path=log_path,
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
        if instance.resume_mode == "pty":
            return await self._spawn_pty(instance, proj, name, log_path, permission_mode, resume)

        try:
            proc = await asyncio.to_thread(
                self._popen, proj.path, log_path, name, spawn_mode, permission_mode
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

        markers = await asyncio.to_thread(self._await_ready, log_path, proc)
        self._apply_markers(instance, markers, proc)
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
        if permission_mode == "bypassPermissions" and not self._config.allows_bypass(proj.name):
            raise PermissionModeNotAllowed(
                f"bypassPermissions is not enabled for project {proj.name!r}. Set "
                "projects.<name>.allow_bypass_permissions: true in clauster.yml first."
            )

    def _unique_log_path(self, name: str) -> Path:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        # Unique per spawn so the parser never reads a previous run's markers.
        return self._log_dir / f"{name}-{int(time.time() * 1000)}.log"

    def _build_cmd(
        self, log_path: Path, name: str, spawn_mode: str, permission_mode: str
    ) -> list[str]:
        """Build the `claude remote-control` argv. Pure (no side effects) so it's unit-testable."""
        return [
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

    @staticmethod
    def _stderr_path_for(log_path: Path) -> Path:
        """Sibling of the --debug-file that captures the bridge's stdout+stderr.

        The bridge writes startup *failures* (e.g. ``Error: Workspace not
        trusted``, controller-auth errors) to its stderr, NOT the --debug-file.
        Routing both streams here — instead of DEVNULL — lets a failed spawn
        surface a real reason instead of a bare timeout.
        """
        return log_path.with_name(log_path.stem + ".stderr.log")

    def _popen(
        self,
        cwd: Path,
        log_path: Path,
        name: str,
        spawn_mode: str,
        permission_mode: str,
    ) -> subprocess.Popen:
        cmd = self._build_cmd(log_path, name, spawn_mode, permission_mode)
        # Exec the RESOLVED absolute path, not the bare configured name: Windows
        # CreateProcess only auto-appends .exe (never the .cmd/.ps1 shim npm installs
        # for `claude`), so a bare name that the version probe resolves via
        # shutil.which would fail to spawn here. Also pins the binary we validated.
        cmd[0] = resolve_binary(cmd[0])
        # When resume-recap is enabled, flag it in the bridge's env. The detached
        # bridge's child sessions inherit this, and the SessionStart hook (wired
        # into ~/.claude/settings.json) acts only when it is set — so the recap
        # never fires for the user's non-Clauster sessions sharing that config.
        popen_env: dict[str, str] | None = None
        if self._config.claude.resume_recap:
            popen_env = {
                **os.environ,
                "CLAUSTER_RESUME_RECAP": "1",
                "CLAUSTER_RESUME_RECAP_MAX_CHARS": str(self._config.claude.resume_recap_max_chars),
            }
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
        self, log_path: Path, name: str, permission_mode: str, *, resume: bool
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
            return subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=err_fh,
                stderr=subprocess.STDOUT,
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

    async def _spawn_pty(
        self,
        instance: RemoteControlInstance,
        proj: Project,
        name: str,
        log_path: Path,
        permission_mode: str,
        resume: bool,
    ) -> RemoteControlInstance:
        """Spawn path for `resume_mode == "pty"`: launch the keeper, discover via sidecar."""
        sidecar = self._sidecar_path_for(log_path)
        bridge_argv = self._build_pty_bridge_argv(log_path, name, permission_mode, resume=resume)
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
        log_path = instance.bridge_debug_log_path
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
            # Bound it: the UI shows a reason, not a full transcript.
            instance.error_detail = text[-2000:]

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
                if instance.status is not InstanceStatus.STARTING:
                    await self._persist()
                    return
            else:
                markers = await asyncio.to_thread(self._read_markers, log_path)
                self._apply_markers(instance, markers, proc)
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
        return instance

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
            self._instances[proj.name] = RemoteControlInstance(
                project=proj.name,
                label=saved.get("label") or proj.name,
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
        await self._persist()

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
            self._reconcile_status(instance, alive)
            if alive:
                live_projects.add(instance.project)

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

    # ----- lifecycle ------------------------------------------------------

    async def start_poll_loop(self) -> None:
        """Rediscover already-running bridges, then start the background poll loop."""
        await self.rediscover()
        self._poll_task = asyncio.create_task(self._poll_forever())

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
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
