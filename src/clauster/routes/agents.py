"""Background-agent routes (`claude --bg`, supervisor.py), split from ``create_app`` (#1156).

These four routes drive the agent-view background sessions: list them read-only,
dispatch a new one, stop one cleanly, and resume an ended one. Every dispatch
validates the project name before spawning (invariant 2) and mirrors the runner's
``bypassPermissions`` / ``inherit`` ceiling via
:func:`clauster.routes._common.enforce_bypass_ceiling`, failing closed so the
background channel cannot sidestep a project's ``allow_bypass_permissions`` gate
(invariant 1). A resume applies the same ceiling to the project that holds the job's
recorded directory.
The stop route redacts any ``claude rm`` detail through :func:`clauster.redact.redact_for_disk`
before it leaves the process on the ``bg-settled`` event (invariant 4).
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import threading
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import claude_cli, supervisor
from ..config import CEILING_GATED_MODES, ClausterConfig
from ..dependencies import ConfigDep, RunnerDep
from ..discovery import is_valid_project_name
from ..models import BackgroundJob
from ..redact import redact_for_disk
from ._common import enforce_bypass_ceiling

logger = logging.getLogger(__name__)

router = APIRouter()

# The time limit for the folder checks of a background resume (TASK-487): reading the job,
# resolving its directory, the ceiling check, the trust write. On a healthy disk they take
# milliseconds, so 20 s means a folder that hangs, for example on a network mount that went
# away. It is shorter than the 30 to 60 s that a reverse proxy usually waits, so the caller
# gets this error and not the proxy's. The dispatch process itself, once it started, keeps
# its own limit (``supervisor._DISPATCH_TIMEOUT``, 60 s), so a resume that starts its process
# just before this limit can still take about 80 s in all.
_RESUME_TIME_LIMIT = 20.0


@router.get("/api/agents")
async def api_agents() -> list[BackgroundJob]:
    """Agent-view background sessions (`claude --bg`), observed read-only.

    Sourced from the supervisor's docs-acknowledged on-disk state
    (``jobs/<id>/state.json`` + ``daemon/roster.json``) — no subprocess, no
    daemon protocol. Empty list when agent view is unused. Auth-gated by the
    guard middleware like every other ``/api/*`` route.
    """
    return await asyncio.to_thread(supervisor.list_background_jobs)


@router.post("/api/agents", status_code=201)
async def api_dispatch_agent(body: dict, config: ConfigDep, runner: RunnerDep) -> dict:
    """Dispatch a `claude --bg` background session in a managed project.

    Validates the project name (same guard as the other project routes),
    pre-trusts the cwd, and fires `claude --bg [--rc <name>]`; returns the new
    job id. The bg-agents panel reflects its live state via `GET /api/agents`.

    Body: ``{project, prompt?, rc_name?, model?, permission_mode?}`` — ``prompt``
    is required (422 otherwise) unless ``rc_name`` registers the session on
    claude.ai (#1033). A ``rc_name`` opens the cloud door (a cloud-visible Remote
    Control session); the job is later stopped via ``DELETE /api/agents/{job_id}``,
    whose double-SIGINT path deregisters that cloud session, or restarted via
    ``POST /api/agents/{job_id}/resume``.
    """
    raw_name = body.get("project")
    if not isinstance(raw_name, str):
        raise HTTPException(status_code=422, detail="project must be a string")
    name = raw_name.strip()
    if not is_valid_project_name(name):
        raise HTTPException(status_code=422, detail="invalid project name")

    def _opt_text(field: str, *, empty_ok: bool = True) -> str | None:
        """Validate an optional text field from the arbitrary JSON body.

        Absent/null → None. A present non-string → 422 (rather than letting a
        bad type reach — and partially execute — the spawn path). A present
        empty string → None when ``empty_ok`` (a missing prompt), else 422:
        an explicit empty ``rc_name``/``model``/``permission_mode`` is a
        caller mistake, not "use the default", so it fails loudly instead of
        silently dispatching a different session than intended.
        """
        value = body.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"{field} must be a string")
        if value == "":
            if empty_ok:
                return None
            raise HTTPException(status_code=422, detail=f"{field} must not be empty")
        return value

    prompt = _opt_text("prompt")
    if prompt is not None and not prompt.replace("\ufeff", "").strip():
        # Whitespace-only == no prompt (#1033): the dashboard trims before its
        # gate, so the API must normalize identically or a direct request
        # dispatches a nominally-promptless session around the 422 below.
        # JS trim() also strips U+FEFF (ECMA WhiteSpace) while Python's
        # strip() does not — drop it from the emptiness test for parity.
        prompt = None
    rc_name = _opt_text("rc_name", empty_ok=False)
    model = _opt_text("model", empty_ok=False)
    permission_mode = _opt_text("permission_mode", empty_ok=False)
    cwd = config.projects_root / name
    if not await asyncio.to_thread(cwd.is_dir):
        raise HTTPException(status_code=404, detail=f"project {name!r} not found")
    # Resolve the effective mode (request override, else the configured default) and gate
    # it AFTER the existence check (so a missing project still 404s, not a 403 leaked by
    # the ceiling) but BEFORE dispatch — mirrors the hosted/bridge channels so the bypass
    # ceiling can't be sidestepped by omitting permission_mode when the default is
    # bypassPermissions. (Also makes bg honor instance_defaults, like the other channels.)
    pm = permission_mode or config.instance_defaults.permission_mode
    enforce_bypass_ceiling(config, name, pm)
    if prompt is None and rc_name is None:
        # #1033: an un-registered background session has no composer and no
        # cloud surface — dispatched without a prompt it parks at "send a
        # prompt to start" forever, with no way to ever receive one (the bg
        # card offers only Stop/Forget). ``rc_name`` opens the claude.ai door,
        # where the session is conversational, so a blank prompt is legitimate
        # there. Gated after the 404/403 checks so existence and the bypass
        # ceiling keep their precedence (same ordering rationale as
        # enforce_bypass_ceiling above).
        raise HTTPException(
            status_code=422,
            detail=(
                "prompt is required for a background session unless rc_name "
                "registers it on claude.ai"
            ),
        )
    try:
        job_id = await asyncio.to_thread(
            supervisor.dispatch_background_job,
            cwd,
            prompt=prompt,
            rc_name=rc_name,
            model=model,
            permission_mode=pm,
            remote_control=False,  # rc_name above is the dispatch's claude.ai door
            binary=config.claude.binary,
            claude_json=runner.claude_json,
            before_spawn=None,
        )
    except claude_cli.ClaudeNotFound as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except supervisor.DispatchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"id": job_id}


@router.delete("/api/agents/{job_id}")
async def api_stop_agent(job_id: str, config: ConfigDep, runner: RunnerDep) -> dict:
    """Stop a `claude --bg` background session and remove its job, cleanly.

    Double-SIGINTs the session process so the CLI runs an orderly shutdown and
    deregisters its cloud bridge session (vs `claude stop`, which SIGKILLs and
    leaves a cloud orphan), then `claude rm`s the job. The job id is validated
    to the 8-hex short-id shape (also the `claude rm` argv-injection guard).

    Returns `{id, settled, removed, detail}`. `settled` is True only for a
    confirmed cloud-deregistering stop; `settled:false` (no live worker found)
    is a 200 whose `detail` flags the unconfirmed stop / possible cloud orphan.
    A session that was signalled but didn't settle in time raises StopError → 409
    (escalate from the CLI — we don't force-kill, which would orphan the cloud
    session). When `claude rm` soft-fails for a confirmed-dead worker, clauster
    drops the orphaned job record itself so the row can still be forgotten (#485);
    any residual `removed:false` is reported in the body.
    """
    if not supervisor.valid_job_id(job_id):
        raise HTTPException(status_code=422, detail="invalid job id")
    try:
        result = await asyncio.to_thread(
            supervisor.stop_background_job, job_id, binary=config.claude.binary
        )
    except claude_cli.ClaudeNotFound as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except supervisor.StopError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Fire the #432 `bg-settled` webhook (fire-and-forget, fail-open, default OFF):
    # a background `claude --bg` job reached a terminal state via the supervisor.
    # The job id is this deployment's own short handle (not a foreign secret, same
    # posture as /api/agents); `detail` can carry a `claude rm` path/stderr tail so
    # it is redacted before egress.
    # Bind detail once: pyright narrows the local to str inside the truthiness
    # guard, which a repeated result.get("detail") would not (Unknown | None).
    detail = result.get("detail")
    runner.emit_event(
        "bg-settled",
        {
            "event_type": "bg-settled",
            "id": result.get("id"),
            "settled": result.get("settled"),
            "removed": result.get("removed"),
            "detail": redact_for_disk(detail) if detail else None,
        },
    )
    return result


@router.post("/api/agents/{job_id}/resume", status_code=201)
async def api_resume_agent(job_id: str, config: ConfigDep, runner: RunnerDep) -> dict:
    """Resume an ended `claude --bg` session in the background, with its transcript.

    The path `{job_id}` is the 8-hex short id (validated like the stop path); the
    `--resume` argument is the job's full session UUID, validated separately
    (`valid_session_id`) — the 8-hex guard would reject a UUID. `{id}` is the job id
    claude printed: the same id when claude continues the session (claude 2.1.332), or a
    new one when it started a copy (the session reads as running, or the flags differ from
    its saved ones), or on an older claude (TASK-588). A job that was
    registered with claude.ai is resumed with `--remote-control`, so it registers again.

    The resume runs in the configured default permission mode, like a dispatch that
    names none, and that mode faces the ceiling of the project holding the job's
    directory (403 when refused, TASK-335).

    The folder checks run under one time limit (:data:`_RESUME_TIME_LIMIT`, TASK-487).
    Past it the request gets a 504 and the job is not resumed, also when the blocked
    check finishes later (:func:`_resume_within_limit`).
    """
    if not supervisor.valid_job_id(job_id):
        raise HTTPException(status_code=422, detail="invalid job id")
    pm = config.instance_defaults.permission_mode
    try:
        new_id = await _resume_within_limit(
            lambda gate: supervisor.resume_background_job(
                job_id,
                permission_mode=pm,
                refuse_cwd=lambda cwd: _resume_ceiling_refusal(config, cwd, pm),
                gate=gate,
                binary=config.claude.binary,
                claude_json=runner.claude_json,
            ),
            job_id=job_id,
        )
    except claude_cli.ClaudeNotFound as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except supervisor.ResumeNotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except supervisor.ResumeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except supervisor.DispatchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"id": new_id}


async def _resume_within_limit(
    work: Callable[[supervisor.ResumeGate], str], *, job_id: str
) -> str:
    """Run ``work`` on its own thread and give it :data:`_RESUME_TIME_LIMIT` to dispatch.

    A thread blocked on a hung mount cannot be stopped, so past the limit the request
    abandons the :class:`~clauster.supervisor.ResumeGate` and returns a 504. The thread can
    stay blocked, but when it wakes up its claim on the gate fails and it dispatches
    nothing. A request that is cancelled before the limit abandons the gate the same way.
    When the thread claimed the gate first, the dispatch process is starting: the request
    then waits for it. ``subprocess.run`` bounds the run of that process with its own
    timeout, but not the start itself: a mount that hangs in the moment between the last
    check and the process start (its working directory) can still hold the request.

    The thread is a daemon thread of its own, not one from the shared ``to_thread`` pool,
    so a hung resume cannot take a pool worker from the other routes for good.
    """
    gate = supervisor.ResumeGate()
    loop = asyncio.get_running_loop()
    outcome: asyncio.Future[str] = loop.create_future()

    def _settle(result: str, error: Exception | None) -> None:
        """Hand the thread's result or error to the waiting request, once."""
        if outcome.done():
            return
        if error is not None:
            outcome.set_exception(error)
        else:
            outcome.set_result(result)

    def _run() -> None:
        """Run ``work`` and pass its outcome back to the event loop."""
        result = ""
        error: Exception | None = None
        try:
            result = work(gate)
        # Any exception, so that a request waiting on a dispatch that started never hangs.
        except BaseException as exc:  # raised again in the request by ``await outcome``
            # SystemExit, KeyboardInterrupt and CancelledError would stop the event loop or
            # read as a cancel of the request there, so they go back as a RuntimeError.
            if isinstance(exc, Exception):
                error = exc
            else:
                error = RuntimeError(f"background resume thread raised {type(exc).__name__}")
                error.__cause__ = exc
        try:
            loop.call_soon_threadsafe(_settle, result, error)
        except RuntimeError:
            # The event loop closed while this thread was blocked: no request waits.
            logger.debug("background resume of %s finished after its event loop closed", job_id)

    threading.Thread(target=_run, name=f"clauster-resume-{job_id}", daemon=True).start()
    limit = asyncio.timeout(_RESUME_TIME_LIMIT)
    try:
        async with limit:
            return await asyncio.shield(outcome)
    except asyncio.CancelledError:
        # The request ended before the limit: a thread that wakes up later must not resume a
        # job that no request reports. When the dispatch already started, it runs on, and its
        # outcome is read so that a failure is not logged as never retrieved.
        if gate.abandon():
            outcome.cancel()
        else:
            outcome.add_done_callback(_log_unreported_failure(job_id))
        raise
    except TimeoutError:
        if not limit.expired():
            raise  # a TimeoutError from the work itself, not the limit
        if not gate.abandon():
            # The dispatch started first: wait for it. A cancel during this wait leaves the
            # dispatch running; its failure is then logged, as in the branch above.
            try:
                return await asyncio.shield(outcome)
            except asyncio.CancelledError:
                outcome.add_done_callback(_log_unreported_failure(job_id))
                raise
        outcome.cancel()  # nothing waits for the thread now: drop what it hands back
        raise HTTPException(
            status_code=504,
            detail=(
                f"job {job_id!r} was not resumed: the checks of the job and its working "
                f"directory took longer than {_RESUME_TIME_LIMIT:g} s. A network mount may be "
                "hung. Try again later."
            ),
        ) from None


def _log_unreported_failure(job_id: str) -> Callable[[asyncio.Future[str]], None]:
    """Return a done callback that logs a resume failure no request is left to report.

    Reading the exception also keeps asyncio from logging it as never retrieved. Only the
    class name is logged: the message of a dispatch error can name a path.
    """

    def _callback(done: asyncio.Future[str]) -> None:
        """Log the class of the failure, if the dispatch failed."""
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            logger.warning(
                "background resume of %s failed after its request ended: %s",
                job_id,
                type(error).__name__,
            )

    return _callback


def _resume_ceiling_refusal(config: ClausterConfig, cwd: Path, permission_mode: str) -> str | None:
    """Return the 403 detail when ``permission_mode`` is refused for a job in ``cwd``.

    An explicit mode forces its own flag, so only :data:`CEILING_GATED_MODES` are
    checked. Those need the ceiling of EVERY project whose real directory holds the
    job's real directory (:func:`_projects_holding`), so overlapping project
    directories (one symlinked inside another) can only make the gate stricter, never
    depend on the order of the ``projects:`` keys. A job in no project has no ceiling
    and is refused (fail closed). When ``projects_root`` cannot be listed, the refusal
    names the listing error instead (TASK-487): the job may well be in a project.
    """
    if permission_mode not in CEILING_GATED_MODES:
        return None
    try:
        projects = _projects_holding(config, cwd)
    except _ProjectsRootUnlistable as exc:
        return (
            f"{permission_mode} is not enabled for this background job: Clauster could not "
            f"list projects_root to find the project that holds its directory ({exc})."
        )
    if not projects:
        return (
            f"{permission_mode} is not enabled for this background job: its directory is in "
            "no project that sets projects.<name>.allow_bypass_permissions: true in "
            "clauster.yml."
        )
    for project in projects:
        if config.bypass_denied(project, permission_mode):
            return config.bypass_denied_detail(project, permission_mode=permission_mode)
    return None


class _ProjectsRootUnlistable(Exception):
    """``projects_root`` could not be listed; the message names the error, not the path."""


def _is_link_entry(entry: os.DirEntry[str]) -> bool:
    """Return whether ``entry`` is a symlink, or a junction or other reparse point on Windows.

    Both come from the directory listing itself, with no call that touches the entry's
    target on Windows, or on POSIX when the file system reports the entry type. On Windows
    a junction is not a symlink to ``is_symlink()``, so the reparse-point attribute decides.
    """
    if entry.is_symlink():
        return True
    if os.name != "nt":
        return False
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _projects_holding(config: ClausterConfig, cwd: Path) -> list[str]:
    """Return every project whose directory holds ``cwd``, sorted; empty if none.

    The candidates are every entry directly under ``projects_root``, configured or not
    (an entry with no config entry has no ceiling), the first path part of the resolved
    ``cwd`` under the resolved ``projects_root``, and each valid ``config.projects`` key
    (which can point outside the root through a symlink). Both sides are compared after
    ``resolve()``, so ``..`` and symlinks cannot move a job into a project it does not run
    in, a symlink alias under ``projects_root`` (one project directory reached under two
    names) brings in the alias's own ceiling, and a directory name is never matched by name
    alone: ``/tmp/<name>`` does not borrow ``<name>``'s ceiling. An unresolvable ``cwd``
    matches nothing (fail closed).

    Only an entry that is a symlink or junction is resolved (TASK-487). A plain directory
    resolves to ``<resolved projects_root>/<name>``, which the first-part check already
    covers, and resolving it would touch it: a hung network mount there would block the
    check. This bounds only the unconfigured plain-directory entries: ``projects_root``,
    ``cwd``, every symlink entry and every configured key are still resolved, so a hung
    mount there can still block this thread. The resume route bounds the request with
    :data:`_RESUME_TIME_LIMIT` instead (TASK-487). A listing failure raises
    :class:`_ProjectsRootUnlistable` so the refusal can name it.
    """
    found: set[str] = set()
    # A NUL byte is never a real directory. POSIX resolve() raises ValueError on it, but
    # Windows' non-strict resolve() returns the path unresolved, so refuse it up front.
    if "\x00" in str(cwd):
        return []
    try:
        resolved = cwd.resolve()
        root = config.projects_root.resolve()
        if root in resolved.parents:
            found.add(resolved.relative_to(root).parts[0])
        try:
            with os.scandir(config.projects_root) as listing:
                entries = list(listing)
        except OSError as exc:
            # The class and the OS reason only: str(exc) also names the path.
            raise _ProjectsRootUnlistable(
                f"{type(exc).__name__}: {exc.strerror or 'no reason given'}"
            ) from exc
        for entry in entries:
            if not _is_link_entry(entry):
                continue
            try:
                base = Path(entry.path).resolve()
            except RuntimeError:
                # A symlink loop (3.11/3.12 raise) leads to no directory, so it cannot
                # hold the job. Any other resolve error refuses the resume (outer except).
                continue
            if resolved == base or base in resolved.parents:
                found.add(entry.name)
        for name in config.projects:
            # A key like ".." would make projects_root's parent a "project" (widening).
            if not is_valid_project_name(name):
                continue
            base = (config.projects_root / name).resolve()
            if resolved == base or base in resolved.parents:
                found.add(name)
    # RuntimeError: a symlink loop on 3.11/3.12. ValueError: a path the OS rejects.
    except (OSError, RuntimeError, ValueError):
        return []
    return sorted(found)
