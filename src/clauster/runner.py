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
    SPAWN_MODES,
    ClausterConfig,
    PermissionMode,
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
from .state import StateStore
from .trust import is_trusted, trust_directory

_log = logging.getLogger("clauster.runner")


class SpawnError(RuntimeError):
    """Raised when a bridge cannot be spawned (unknown project, untrusted, etc.)."""


class UnknownProject(SpawnError):
    pass


class NotTrusted(SpawnError):
    pass


class InvalidSpawnOption(SpawnError):
    """Bad spawn_mode/permission_mode value, or worktree requested for a non-git project."""


class PermissionModeNotAllowed(SpawnError):
    """bypassPermissions requested for a project whose config ceiling forbids it."""


# How long to wait for a freshly-spawned bridge to reach its poll loop.
_READY_TIMEOUT = 15.0
_READY_POLL_INTERVAL = 0.25


class SessionRunner:
    def __init__(self, config: ClausterConfig, claude_json: Path | None = None) -> None:
        self._config = config
        self._binary = config.claude.binary
        self._claude_json = claude_json or Path("~/.claude.json").expanduser()
        self._log_dir = (config.state_dir / "logs").expanduser()
        self._instances: dict[str, RemoteControlInstance] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._sessions: list[WorkingSession] = []
        self._poll_task: asyncio.Task | None = None
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
        return list(self._instances.values())

    def get_instance(self, instance_id: str) -> RemoteControlInstance | None:
        return self._instances.get(instance_id)

    def running_count(self) -> int:
        return sum(1 for i in self._instances.values() if i.status is InstanceStatus.RUNNING)

    def working_sessions(self) -> list[WorkingSession]:
        return list(self._sessions)

    def external_sessions_by_project(self) -> dict[str, list[WorkingSession]]:
        """EXTERNAL working sessions (not tied to a managed bridge) grouped by
        the discovered project at their cwd, keyed by project name (bug #4).

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
        return {
            name: {
                "label": inst.label,
                "intentional_stop": inst.intentional_stop,
                "spawn_mode": inst.spawn_mode,
                "permission_mode": inst.permission_mode,
            }
            for name, inst in self._instances.items()
        }

    async def _persist(self) -> None:
        """Write the persisted subset off-loop, but only when it actually changed."""
        subset = self._persist_subset()
        if subset == self._last_saved:
            return
        await asyncio.to_thread(self._state.save, subset)
        self._last_saved = subset

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
    ) -> RemoteControlInstance:
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
        self._validate_spawn_options(proj, spawn_mode, permission_mode)

        if not await asyncio.to_thread(is_trusted, proj.path, self._claude_json):
            raise NotTrusted(
                f"directory not trusted: {proj.path}. Use the Trust action before starting."
            )

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
        await self._persist()
        return instance

    def _validate_spawn_options(
        self, proj: Project, spawn_mode: str, permission_mode: str
    ) -> None:
        if spawn_mode not in SPAWN_MODES:
            raise InvalidSpawnOption(
                f"invalid spawn_mode {spawn_mode!r}; expected one of {SPAWN_MODES}"
            )
        if permission_mode not in PERMISSION_MODES:
            raise InvalidSpawnOption(
                f"invalid permission_mode {permission_mode!r}; expected one of {PERMISSION_MODES}"
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
        """The `claude remote-control` argv. Pure (no side effects) so it's unit-testable."""
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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        return subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

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
            text = log_path.read_text()
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
        else:
            # Never reached the poll loop: trust failure, early exit, or timeout.
            instance.status = InstanceStatus.ERROR

    # ----- stop -----------------------------------------------------------

    async def stop(self, name: str) -> RemoteControlInstance:
        instance = self._instances.get(name)
        if instance is None:
            raise UnknownProject(f"no managed instance: {name!r}")
        instance.intentional_stop = True  # mark intent BEFORE signalling (spec §3 feat 4)
        await self._persist()  # persist the intent so a restart doesn't mislabel it CRASHED

        pid = instance.bridge_pid
        if pid is None:
            instance.status = InstanceStatus.STOPPED
            return instance

        # Re-validate identity immediately before signalling (TOCTOU / PID reuse).
        if await asyncio.to_thread(procutil.is_live_bridge, pid, instance.bridge_proc_start):
            await asyncio.to_thread(self._signal_stop, pid)
            await self._await_exit(name, pid, instance.bridge_proc_start)
        instance.status = InstanceStatus.STOPPED
        return instance

    @staticmethod
    def _signal_stop(pid: int) -> None:
        """Ask a bridge to shut down gracefully: SIGINT on POSIX, CTRL_BREAK on
        Windows (deliverable because the bridge is its own process group)."""
        sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
        try:
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

    async def rediscover(self) -> None:
        """Read-only re-detection of bridges already running (e.g. after a restart)."""
        for proj in self._discovered().values():
            if proj.name in self._instances:
                continue
            ptr = await asyncio.to_thread(pointers.pointer_for_project, proj.path)
            if ptr is None or not await asyncio.to_thread(pointers.is_live, ptr):
                continue
            # Overlay the few fields the pointer-walk can't recover; a bridge
            # found alive is by definition NOT intentionally stopped.
            saved = self._persisted.get(proj.name, {})
            defaults = self._config.instance_defaults
            # Coerce persisted modes against the allowed sets so a hand-edited /
            # corrupt state.json can't fail the (now Literal-typed) model and abort
            # startup — fall back to the configured default instead.
            sm = saved.get("spawn_mode")
            pm = saved.get("permission_mode")
            self._instances[proj.name] = RemoteControlInstance(
                project=proj.name,
                label=saved.get("label") or proj.name,
                spawn_mode=sm if sm in SPAWN_MODES else defaults.spawn_mode,
                permission_mode=pm if pm in PERMISSION_MODES else defaults.permission_mode,
                intentional_stop=False,
                status=InstanceStatus.RUNNING,
                bridge_pid=ptr.pid,
                bridge_proc_start=procutil.jiffies_to_epoch(int(ptr.proc_start)),
                environment_id=ptr.environment_id,
                starter_session_id=ptr.session_id,
                url=f"https://claude.ai/code?environment={ptr.environment_id}",
            )
        await self._persist()

    async def poll_once(self) -> None:
        """Liveness reconcile + `claude agents --json` cross-check (off-loop work,
        applied on-loop)."""
        for instance in list(self._instances.values()):
            pid = instance.bridge_pid
            if pid is None:
                continue
            await asyncio.to_thread(procutil.reap_if_exited, pid)
            alive = await asyncio.to_thread(
                procutil.is_live_bridge, pid, instance.bridge_proc_start
            )
            self._reconcile_status(instance, alive)

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
        managed = {
            Path(self._discovered()[i.project].path): i.project
            for i in self._instances.values()
            if i.project in self._discovered()
        }
        self._sessions = inspector.reconcile(sessions, managed)

    @staticmethod
    def _reconcile_status(instance: RemoteControlInstance, alive: bool) -> None:
        status = instance.status
        if status is InstanceStatus.RUNNING and not alive:
            # session mode is single-shot: the bridge exits when its session ends, so a
            # disappearance is expected (STOPPED), not a crash. same-dir/worktree persist,
            # so an unintended exit there IS a crash.
            expected_exit = instance.intentional_stop or instance.spawn_mode == "session"
            instance.status = InstanceStatus.STOPPED if expected_exit else InstanceStatus.CRASHED
        elif status is InstanceStatus.ERROR and alive:
            # A slow-to-start detached bridge that timed out but is actually up.
            instance.status = InstanceStatus.RUNNING

    # ----- lifecycle ------------------------------------------------------

    async def start_poll_loop(self) -> None:
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

    async def shutdown(self) -> None:
        # Cancel the poll task only; leave bridges running (they are detached).
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
