"""In-process engine facade shared by the web app and the headless CLI (#775).

:class:`ClausterEngine` is a thin, dependency-light seam over the service objects
that already own clauster's behaviour — :class:`~clauster.runner.SessionRunner`,
:mod:`clauster.discovery`, :mod:`clauster.logstream`, :mod:`clauster.redact`. The
FastAPI routes call it in-process (constructed with the app's own runner, so no
second runner is built), and the CLI drives the same methods with **no web server
running**.

The **read/observe** surface (Slice A) lists projects, live instances, and working
sessions; resolves a bridge's connect URL; and tails its (redacted) log. The
**write** surface (Slice B) adds :meth:`start` and :meth:`stop` — the same
``spawn_detailed`` / ``stop`` the FastAPI routes drive, so headless and browser
launches share one code path and one set of policy checks. ``send`` (a hosted
session's conversation turn) is deliberately out of scope: a hosted session lives
in-memory in the running web-app process, so it needs the running-server proxy
refinement (or the in-process MCP, #527), not a serverless CLI. Two usage shapes:

* **Web app** — ``ClausterEngine(config, runner=app_runner)``. The runner already
  runs its poll loop under the FastAPI lifespan, so the app never calls
  :meth:`hydrate`, and :meth:`dispose` is a no-op (the app owns the runner).
* **Headless CLI** — ``ClausterEngine(config)`` builds its own runner. There is no
  poll loop, so a command that reports instance state calls :meth:`hydrate` once
  (a **read-only** ``rediscover(persist=False)`` — never ``poll_once``, so a read
  command can't write shared state or fire lifecycle webhooks) before reading, and
  :meth:`dispose` at the end. Use it as a context manager so ``dispose`` is guaranteed.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import TracebackType
from typing import TYPE_CHECKING

from . import inspector, logstream
from .discovery import discover_projects_cached
from .redact import sanitize_line
from .runner import SessionRunner

if TYPE_CHECKING:
    from pathlib import Path

    from .config import ClausterConfig, PermissionMode, ResumeMode, SandboxMode, SpawnMode
    from .models import Project, RemoteControlInstance, WorkingSession
    from .runner import SpawnOutcome


class ClausterEngine(AbstractContextManager["ClausterEngine"]):
    """Shared in-process facade over clauster's read/observe operations (#775)."""

    def __init__(self, config: ClausterConfig, *, runner: SessionRunner | None = None) -> None:
        """Wrap an existing ``runner`` (web app) or build one headless (CLI).

        When ``runner`` is supplied the caller owns its lifecycle and
        :meth:`dispose` is a no-op; when omitted a private :class:`SessionRunner`
        is created and disposed by this engine.
        """
        self._config = config
        self._owns_runner = runner is None
        self._runner = runner if runner is not None else SessionRunner(config)

    async def hydrate(self) -> None:
        """Reattach live bridges into the in-memory registry for a headless read.

        The web app keeps the registry current via its poll loop; a fresh CLI
        runner has an empty one, so a command that reports instance state calls this
        first. It uses ``rediscover(persist=False)`` — a **read-only** reattach that
        never writes the shared ``state.json`` and never fires the lifecycle
        webhooks/notifications ``poll_once`` would, so ``clauster status`` on a host
        running the live service can't clobber its state or emit spurious events.
        """
        await self._runner.rediscover(persist=False)

    # -- projects -------------------------------------------------------------

    def list_projects(self) -> list[Project]:
        """List discoverable projects, stamped with the per-project bypass ceiling.

        Filesystem + ``~/.claude.json`` read only (no runner/DB). Mirrors the
        ``/api/projects`` route: discovery has no config knowledge, so the
        bypass-permissions ceiling is stamped here from the config.
        """
        projects = discover_projects_cached(self._config.projects_root, self._runner.claude_json)
        for project in projects:
            project.allow_bypass_permissions = self._config.allows_bypass(project.name)
        return projects

    # -- instances / sessions -------------------------------------------------

    def list_instances(self) -> list[RemoteControlInstance]:
        """Return the known bridge instances and their status (mode preserved per row)."""
        return self._runner.list_instances()

    def working_sessions(self) -> list[WorkingSession]:
        """List the host's live working sessions via ``claude agents --json``.

        A direct, **read-only** probe (no state write, no lifecycle emit) — unlike
        the runner's poll loop, which reconciles + persists + fires webhooks. Slice A
        lists them flat; tracked-vs-external attribution (which needs the full
        reconcile) is a later slice.
        """
        return inspector.list_working_sessions(self._config.claude.binary)

    def resolve_instance(self, identity: str) -> RemoteControlInstance | None:
        """Resolve an instance id / bridge identity to its instance, or ``None``."""
        resolved = self._runner.resolve_bridge_id(identity)
        if resolved is None:
            return None
        return self._runner.get_instance(resolved)

    # -- write: spawn / stop --------------------------------------------------

    async def start(
        self,
        project: str,
        *,
        spawn_mode: SpawnMode | None = None,
        permission_mode: PermissionMode | None = None,
        resume_mode: ResumeMode | None = None,
        custom_name: str | None = None,
        sandbox: SandboxMode | None = None,
        trust: bool = False,
    ) -> SpawnOutcome:
        """Spawn (or hand back an already-live) bridge for ``project``; return the outcome.

        Mirrors the ``POST /api/instances`` route: the same
        :meth:`~clauster.runner.SessionRunner.spawn_detailed`, so the per-mode policy,
        the standard-singleton cap, and option validation are identical whether a
        bridge is started from the browser or headless. ``resume_mode`` picks the
        bridge mode (``"standard"``/``"pty"``) with **no hidden coercion**, exactly
        as the launch-mode picker does.

        A headless caller must :meth:`hydrate` first: ``spawn_detailed``'s idempotency
        check reads the in-memory registry, so without a reattach a fresh CLI runner
        can't see a bridge the live service already started and would launch a second.

        ``trust`` accepts the workspace-trust dialog for the project before spawning
        (the CLI's ``--trust``, the headless equivalent of the dashboard's explicit
        Trust action). Left off, an untrusted directory raises
        :class:`~clauster.runner.NotTrusted` rather than being trusted implicitly.
        The trust write happens *before* ``spawn_detailed`` validates the launch
        options, so a later invalid option (a bad ``--name``) still leaves the
        directory trusted — intentional: ``--trust`` is a deliberate, persistent,
        stand-alone authorization (as it is in the dashboard), not contingent on the
        rest of the spawn succeeding. ``trust_project`` still validates the project
        name first, so trust is never written for an unresolved project.
        """
        if trust:
            await self._runner.trust_project(project)
        return await self._runner.spawn_detailed(
            project,
            spawn_mode=spawn_mode,
            permission_mode=permission_mode,
            resume_mode=resume_mode,
            custom_name=custom_name,
            sandbox=sandbox,
        )

    async def stop(self, identity: str) -> RemoteControlInstance | None:
        """Stop the bridge resolved from ``identity``; ``None`` when none matches.

        Resolves an id / prefix / bridge identity the same way the ``DELETE`` route
        does (:meth:`~clauster.runner.SessionRunner.resolve_bridge_id`), so a headless
        stop targets exactly the instance the operator named. A headless caller must
        :meth:`hydrate` first so the registry is populated for the resolve.
        """
        resolved = self._runner.resolve_bridge_id(identity)
        if resolved is None:
            return None
        return await self._runner.stop(resolved)

    # -- connect url / logs ---------------------------------------------------

    def connect_url(self, identity: str) -> str | None:
        """Return a bridge's deep-link connect URL (session link, else composer link).

        Mirrors the QR route's fallback: the ``claude.ai/code`` session link once a
        starter session exists, otherwise the ``?environment=`` composer link.
        ``None`` when the instance is unknown or has neither yet.
        """
        instance = self.resolve_instance(identity)
        if instance is None:
            return None
        return instance.session_url or instance.url

    def bridge_log_path(self, identity: str) -> Path | None:
        """Return the on-disk bridge log to tail (raw if present, else the redacted mirror)."""
        instance = self.resolve_instance(identity)
        if instance is None:
            return None
        return instance.bridge_raw_log_path or instance.bridge_debug_log_path

    @staticmethod
    def initial_log_offset(path: Path, *, tail_bytes: int = logstream.DEFAULT_TAIL_BYTES) -> int:
        """Return the byte offset to start tailing ``path`` from (last ``tail_bytes``)."""
        return logstream.initial_offset(path, tail_bytes)

    @staticmethod
    def read_log_lines(path: Path, offset: int) -> tuple[int, list[str]]:
        """Read new log lines past ``offset``, redacted; return ``(new_offset, lines)``.

        Redaction is owned here (via :func:`~clauster.redact.sanitize_line`) so no
        caller re-implements it — the same guarantee the WebSocket log stream gives.
        """
        new_offset, text = logstream.read_new(path, offset)
        if not text:
            return new_offset, []
        lines = [sanitize_line(line) for line in text.splitlines()]
        return new_offset, lines

    # -- lifecycle ------------------------------------------------------------

    def dispose(self) -> None:
        """Dispose the private runner's persistence; a no-op when the runner is injected."""
        if self._owns_runner:
            self._runner.persistence.dispose()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Dispose on context-manager exit (guarantees the headless runner is cleaned up)."""
        self.dispose()
