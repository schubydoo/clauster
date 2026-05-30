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
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import bridge_log, inspector, pointers, procutil
from .config import ClausterConfig
from .discovery import discover_projects, is_valid_project_name
from .models import (
    Attribution,
    InstanceStatus,
    Project,
    RemoteControlInstance,
    WorkingSession,
)
from .trust import is_trusted, trust_directory


class SpawnError(RuntimeError):
    """Raised when a bridge cannot be spawned (unknown project, untrusted, etc.)."""


class UnknownProject(SpawnError):
    pass


class NotTrusted(SpawnError):
    pass


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

    # ----- read API -------------------------------------------------------

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

    # ----- discovery helpers ---------------------------------------------

    def _discovered(self) -> dict[str, Project]:
        return {p.name: p for p in discover_projects(self._config.projects_root, self._claude_json)}

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

    async def spawn(self, name: str) -> RemoteControlInstance:
        existing = self._instances.get(name)
        if existing is not None and existing.status in (
            InstanceStatus.STARTING,
            InstanceStatus.RUNNING,
        ):
            return existing

        proj = self._resolve_project(name)
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
            started_at=datetime.now(timezone.utc),
            spawn_mode=self._config.instance_defaults.spawn_mode,
        )
        self._instances[name] = instance  # on the loop

        proc = await asyncio.to_thread(self._popen, proj.path, log_path, name)
        self._procs[name] = proc
        instance.bridge_pid = proc.pid
        instance.bridge_proc_start = await asyncio.to_thread(procutil.proc_create_time, proc.pid)

        markers = await asyncio.to_thread(self._await_ready, log_path, proc)
        self._apply_markers(instance, markers, proc)
        return instance

    def _unique_log_path(self, name: str) -> Path:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        # Unique per spawn so the parser never reads a previous run's markers.
        return self._log_dir / f"{name}-{int(time.time() * 1000)}.log"

    def _popen(self, cwd: Path, log_path: Path, name: str) -> subprocess.Popen:
        cmd = [
            self._binary,
            "remote-control",
            "--name",
            name,
            "--debug-file",
            str(log_path),
        ]
        # Detached (own session) so the bridge survives a clauster restart and a
        # SIGINT to clauster never propagates to it.
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

        pid = instance.bridge_pid
        if pid is None:
            instance.status = InstanceStatus.STOPPED
            return instance

        # Re-validate identity immediately before signalling (TOCTOU / PID reuse).
        if await asyncio.to_thread(procutil.is_live_bridge, pid, instance.bridge_proc_start):
            await asyncio.to_thread(os.kill, pid, signal.SIGINT)
            await self._await_exit(name, pid, instance.bridge_proc_start)
        instance.status = InstanceStatus.STOPPED
        return instance

    async def _await_exit(self, name: str, pid: int, proc_start: float | None) -> None:
        for _ in range(20):  # ~5s
            alive = await asyncio.to_thread(procutil.is_live_bridge, pid, proc_start)
            if not alive:
                break
            await asyncio.sleep(0.25)
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
            self._instances[proj.name] = RemoteControlInstance(
                project=proj.name,
                label=proj.name,
                status=InstanceStatus.RUNNING,
                bridge_pid=ptr.pid,
                bridge_proc_start=procutil.jiffies_to_epoch(int(ptr.proc_start)),
                environment_id=ptr.environment_id,
                starter_session_id=ptr.session_id,
                url=f"https://claude.ai/code?environment={ptr.environment_id}",
            )

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
        except Exception:
            return  # cross-check is best-effort; never let it crash the loop
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
            instance.status = (
                InstanceStatus.STOPPED if instance.intentional_stop else InstanceStatus.CRASHED
            )
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
                pass
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
