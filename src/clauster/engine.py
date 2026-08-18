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
  (a **read-only** ``rediscover(persist=False)``, with no ``poll_once`` at all — the
  CLI's reads don't need its ``agents --json`` cross-check, so the cheapest way not to
  write shared state or fire lifecycle webhooks is not to call it) before reading, and
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


def ambiguity_hint(kind: str | None) -> str:
    """Map a bridge-ambiguity ``kind`` to the operator's retry advice.

    ``"prefix"`` (#1099) → lengthen the abbreviation; ``"project"`` (#1150) or any other
    value → the operator cannot lengthen a fixed project name, so point them at an
    instance id. Kept beside :meth:`ClausterEngine.bridge_id_ambiguity` so the CLI 2-exit
    path and the HTTP 409 word the hint identically from the same verdict.
    """
    return "use more characters" if kind == "prefix" else "use an instance id directly"


class ClausterEngine(AbstractContextManager["ClausterEngine"]):
    """Shared in-process facade over clauster's reads and bridge start/stop/resume (#775)."""

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
        never writes the shared ``state.json`` — and does not call ``poll_once`` at all,
        so ``clauster status`` on a host running the live service can't clobber its state
        or emit spurious events.

        The CLI reads don't need ``poll_once``'s ``agents --json`` cross-check, so the
        cheapest guarantee here is simply not to call it. A caller that DOES need the
        cross-check (the MCP server) passes ``side_effects=False`` instead — see
        :meth:`~clauster.runner.SessionRunner.poll_once` and #1104.
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
        """Resolve an instance id / unique id prefix / bridge identity, or ``None``."""
        resolved = self._runner.resolve_bridge_id(identity)
        if resolved is None:
            return None
        return self._runner.get_instance(resolved)

    def bridge_id_candidates(self, identity: str) -> list[str]:
        """Return the instance_ids an AMBIGUOUS ``identity`` could mean, else empty (#1099).

        Every resolve on this facade returns ``None`` for an ambiguous prefix as well as
        for an unknown one — failing closed, because acting on the wrong live session is
        unrecoverable. This is how a caller tells the two apart and reports the ids to
        retry with instead of a bare "not found".
        """
        return self._runner.bridge_id_candidates(identity)

    def bridge_id_ambiguity(self, identity: str) -> tuple[list[str], str | None]:
        """Return ``(candidates, kind)`` for an AMBIGUOUS ``identity`` (#1099, #1150).

        ``kind`` (``"prefix"`` / ``"project"`` / ``None``) lets a caller word the retry
        hint from the resolver's own verdict rather than re-deriving it from the candidate
        strings — see :func:`ambiguity_hint`.
        """
        return self._runner.bridge_id_ambiguity(identity)

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

        A headless caller must :meth:`hydrate` first so the registry reflects what is
        already running. The duplicate-launch hazard hydrate used to be load-bearing
        for is closed harder since #949: ``spawn_detailed`` re-checks the project's
        on-disk bridge pointer under a *cross-process* per-project lock (the same
        flock the running web app's spawn holds), so a live standard bridge another
        clauster process started is reattached and returned idempotently instead of
        a second one being launched onto the same environment.

        ``trust`` accepts the workspace-trust dialog for the project (the CLI's
        ``--trust``, the headless equivalent of the dashboard's explicit Trust
        action). It is passed into ``spawn_detailed``, which applies it *after* option
        validation and under the per-project spawn lock — so an invalid option fails
        without leaving the directory trusted, and the trust write is atomic with the
        spawn. Left off, an untrusted directory raises
        :class:`~clauster.runner.NotTrusted` rather than being trusted implicitly.
        """
        return await self._runner.spawn_detailed(
            project,
            spawn_mode=spawn_mode,
            permission_mode=permission_mode,
            resume_mode=resume_mode,
            custom_name=custom_name,
            sandbox=sandbox,
            trust=trust,
        )

    async def stop(self, identity: str) -> RemoteControlInstance | None:
        """Stop the bridge resolved from ``identity``; ``None`` when none matches.

        Resolves an id / unique id prefix / bridge identity the same way the ``DELETE``
        route does (:meth:`~clauster.runner.SessionRunner.resolve_bridge_id`), so a
        headless stop targets exactly the instance the operator named. A headless caller
        must :meth:`hydrate` first so the registry is populated for the resolve.

        ``None`` covers both "nothing matched" and "the prefix was ambiguous" — the
        second never picks a bridge. Call :meth:`bridge_id_candidates` to tell them
        apart and name the ids to retry with.
        """
        resolved = self._runner.resolve_bridge_id(identity)
        if resolved is None:
            return None
        return await self._runner.stop(resolved)

    async def resume(self, identity: str) -> RemoteControlInstance | None:
        """Resume the stopped/crashed bridge resolved from ``identity``; ``None`` if none.

        The headless mirror of ``POST /api/instances/{id}/resume`` for the bridge
        channel: resolves the id / unique id prefix / bridge identity exactly like
        :meth:`stop` — including returning ``None`` rather than guessing when a prefix
        is ambiguous — then re-spawns it into
        its prior conversation via :meth:`~clauster.runner.SessionRunner.resume`, which
        reuses the instance's stored spawn/permission/resume modes. A headless caller
        must :meth:`hydrate` first so the registry is populated for the resolve.
        Bridge-scoped like the rest of the engine — hosted-session resume stays behind
        the app's hosted manager, not this facade.

        Returns the instance only. A caller that must tell a genuine revive from the
        standard-singleton cap handing back a DIFFERENT already-live bridge needs
        :meth:`resume_detailed` — reporting the latter as success is #1148.
        """
        outcome = await self.resume_detailed(identity)
        return outcome.instance if outcome is not None else None

    async def resume_detailed(self, identity: str) -> SpawnOutcome | None:
        """Resume the bridge resolved from ``identity``; the full outcome, or ``None``.

        The detailed mirror of :meth:`resume`, and the headless twin of what
        ``POST /api/instances/{id}/resume`` returns (#1145): a
        :class:`~clauster.runner.SpawnOutcome` whose ``created`` is False — with
        ``reason`` — when nothing was revived, because a standard bridge is capped at
        one live per project and the cap is enforced by RETURNING the already-live
        bridge rather than raising. Dropping that here made the MCP ``resume_session``
        tool answer a declined resume with ``resumed: true`` and a bridge it never
        revived, which an agent then acts on (#1148).

        ``None`` when the id is unknown or an ambiguous prefix — the ambiguous case
        never picks a bridge; use :meth:`bridge_id_candidates` to name the ids to retry
        with, exactly as :meth:`stop`/:meth:`resume` do.
        """
        resolved = self._runner.resolve_bridge_id(identity)
        if resolved is None:
            return None
        return await self._runner.resume_detailed(resolved)

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
        """Read new COMPLETE log lines past ``offset``, redacted; return ``(new_offset, lines)``.

        Redaction is owned here (via :func:`~clauster.redact.sanitize_line`) so no
        caller re-implements it — the same guarantee the WebSocket log stream gives.

        A trailing *unterminated* line is withheld and the offset rewound past it, so it
        is emitted once its newline arrives. ``sanitize_line`` matches whole tokens, so a
        secret flushed across two reads matched neither half and printed verbatim: a
        follower showed ``token=ghp_ABC``, then the remainder on the next poll,
        reassembling the secret on the operator's terminal from a stream documented as
        redacted (#1105).

        This is the same hold-and-rewind :func:`~clauster.logstream.read_new` already
        applies to a trailing incomplete UTF-8 *character*, and the same reason
        :func:`~clauster.logstream.initial_offset` starts on a line boundary — a
        fragment splits a secret from its redaction context. Those covered the first
        line and the character level; this covers the last line.
        """
        new_offset, text = logstream.read_new(path, offset)
        if not text:
            return new_offset, []
        complete, newline, partial = text.rpartition("\n")
        # Rewind by the withheld bytes (not characters — the offset is a byte offset).
        # Computed off `new_offset` rather than the caller's `offset` so a rotation reset
        # inside `read_new` is preserved.
        new_offset -= len(partial.encode("utf-8"))
        if not newline:
            return new_offset, []  # nothing terminated yet; consume nothing
        # `.split("\n")` matches the WebSocket route, whose carry this mirrors: line
        # completeness can only be judged on "\n", so `str.splitlines()`'s extra
        # boundaries (\r, \v, \f,  ) would emit as "complete" a line this offset
        # arithmetic still counts as withheld. The lone \r of a CRLF log is stripped
        # per line so the visible output is unchanged.
        lines = [sanitize_line(line.rstrip("\r")) for line in complete.split("\n")]
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
