"""Project discovery, provisioning, trust, and CLAUDE.md routes (#1156).

These routes back the dashboard's Projects surface: discover projects and their
first-paint preflight/sort metadata, render a single project row fragment, create
or git-clone a project, adopt a live external session, grant workspace trust, and
read or write a project's ``CLAUDE.md``.

Every project name is validated for path-component safety before any disk read or
subprocess (invariant 2); the clone path additionally SSRF-validates the git URL up
front and redacts the ``clone-done`` webhook detail before egress (invariant 4). The
``CLAUDE.md`` write path fails closed on an untrusted workspace and honours the
optional stale-hash guard. The row fragment renders through the shared renderer
published on ``app.state`` so it keeps the ``Cache-Control: no-store`` header (the
fragment reflects live per-project session state); the renderer's CSP nonce is
inherited but unused by ``_project_row.html``, which has no inline ``<script>``.

The transcript/usage/metrics routes under ``/api/projects/{name}/...`` live in
:mod:`clauster.routes.transcripts` and :mod:`clauster.routes.usage`, not here.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.exc import SQLAlchemyError

from .. import ops
from ..claude_md import (
    ClaudeMdConflict,
    ClaudeMdError,
    ClaudeMdNotTrusted,
    ClaudeMdTooLarge,
    read_claude_md,
    write_claude_md,
)
from ..config import BYPASS_DESKTOP_HINT, PERMISSION_LABELS
from ..dependencies import (
    CloneJobsDep,
    CloneTasksDep,
    ConfigDep,
    EngineDep,
    RenderDep,
    RunnerDep,
)
from ..discovery import invalidate_discovery_cache, is_valid_project_name
from ..models import ClaudeMdDoc, Project, RemoteControlInstance
from ..provisioning import (
    BlockedCloneHost,
    GitUnavailable,
    InvalidCloneUrl,
    InvalidProjectName,
    ProvisionError,
    TargetExists,
    clone_project,
    create_project,
    validate_clone_url,
)
from ..redact import redact_for_disk
from ..runner import (
    AdoptionUnavailable,
    InstanceStillLive,
    UnknownProject,
    _conpty_keeper_available,
)

if TYPE_CHECKING:
    from ..clone_jobs import CloneJob, CloneJobManager
    from ..config import ClausterConfig
    from ..engine import ClausterEngine
    from ..runner import SessionRunner

logger = logging.getLogger(__name__)

router = APIRouter()

# Single-user actor for the CLAUDE.md write audit trail (single-user in v0.2; multi-user
# is v0.3). Mirrors ``app._SESSION_USER`` -- the config-write CLAUDE.md route still uses
# the app.py copy; the two merge when that domain also moves to ``routes/`` (#1156).
_SESSION_USER = "admin"

# Drop a finished clone job after this grace so a client that disconnected mid-clone can
# reconnect and still read the terminal frame.
_CLONE_JOB_TTL = 60.0


def _pty_supported() -> bool:
    """Whether Interactive Session (pty) can launch on this host, for the row mode picker.

    POSIX always (`pty.openpty`); on Windows only when the ConPTY keeper's `pywinpty` (the
    `pty` extra) is installed. Mirrors ``app._pty_supported`` and the runner's launch-time
    gate, so the fragment row offers exactly the modes the full-page grid does (#914).
    """
    return sys.platform != "win32" or _conpty_keeper_available()


async def _list_projects(engine: ClausterEngine) -> list[Project]:
    """Return the discovered projects through the same facade the CLI and app.py use."""
    # Shared facade (#775): the CLI and this route go through the same
    # discover-then-stamp-bypass path, so the two can't drift.
    return await asyncio.to_thread(engine.list_projects)


async def _project_by_name(name: str, engine: ClausterEngine) -> Project:
    """Return the discovered project with this name, or 500 if provisioning lost it.

    Reads through the discovery cache -- the caller invalidates it first when it needs
    a just-created project to be visible.
    """
    for proj in await _list_projects(engine):
        if proj.name == name:
            return proj
    raise HTTPException(status_code=500, detail=f"project {name!r} missing after provisioning")


async def _resolve_project_path(name: str, engine: ClausterEngine) -> Path:
    """Map a project name to its path, refusing unknown/unsafe names (traversal).

    Mirrors ``app._resolve_project_path``, which still serves the not-yet-moved
    config-write routes; the two merge when the config-write domain moves (#1156).
    The traversal defense itself is the shared :func:`is_valid_project_name`, so the
    copies cannot diverge on the security check.
    """
    if not is_valid_project_name(name):
        raise HTTPException(status_code=404, detail=f"project {name!r} not found")
    for proj in await _list_projects(engine):
        if proj.name == name:
            return proj.path
    raise HTTPException(status_code=404, detail=f"project {name!r} not found")


def _bridge_running(runner: SessionRunner, name: str) -> bool:
    """Return whether ANY bridge or external session is running for ``name``."""
    # Any-RUNNING scan, not the canonical-instance resolver: with N instances per
    # project (#778) the canonical pick can transiently be a STARTING standard
    # bridge while a pty session is already RUNNING -- this flag must not miss it.
    if runner.has_running_instance(name):
        return True
    return name in runner.external_sessions_by_project()


# ----- projects: discovery, preflight, sort metadata, row fragments --------------------------
@router.get("/api/projects")
async def api_projects(engine: EngineDep) -> list[Project]:
    """Return every discovered project."""
    return await _list_projects(engine)


@router.get("/api/projects/preflight")
async def api_projects_preflight(engine: EngineDep, runner: RunnerDep) -> dict:
    """Batch per-project preflight for first paint — ONE discovery scan, not N.

    First paint needs every project's readiness pill; fetching them one-by-one
    re-ran ``list_projects()`` per project (O(N²) discovery on load). This returns
    the same ``{ok, checks}`` shape keyed by project name from a single scan, so
    the dashboard fires one request instead of N. Declared before the
    ``{name}/preflight`` route so the literal path wins the match. Read-only,
    auth-gated. The per-project route stays for the fragment-inserted-row path.

    Each project's checks (including the #837 MCP-approval check, which reads
    ``.mcp.json`` + ``~/.claude.json``) run in a worker thread — real file I/O,
    so it must not block the event loop.
    """
    result: dict[str, dict] = {}
    for proj in await _list_projects(engine):
        checks = await asyncio.to_thread(ops.project_preflight_checks, proj, runner.claude_json)
        result[proj.name] = {
            "ok": all(c.status != ops.FAIL for c in checks),
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
        }
    return result


@router.get("/api/projects/sortmeta")
async def api_projects_sortmeta(engine: EngineDep, runner: RunnerDep) -> dict:
    """Batch per-project sort keys (last-used + cost) for the Projects sort control.

    Returns ``{name: {last_used: iso|null, cost_usd: float|null}}`` for every
    discovered project, read from the session-history rollup. Powers the
    dashboard's opt-in sort dropdown (name / last-used / cost); the client sorts
    client-side, so this is advisory and read-only. Declared before the
    ``{name}/…`` routes so the literal path wins the match. ``sortmeta_for_all``
    reads every project in one session (two grouped queries, not the old per-project
    N+1) and already degrades to empty on a DB error, so a sort never crashes the
    dashboard; the try/except is just an outer net for an engine/IO fault. Invalid
    project names are dropped before use.
    """
    names = [p.name for p in await _list_projects(engine) if is_valid_project_name(p.name)]

    def _collect() -> dict[str, dict]:
        """Read each project's last-used timestamp and rolled-up cost from history."""
        store = runner.persistence.session_history_store()
        meta = store.sortmeta_for_all(names)
        out: dict[str, dict] = {}
        for name in names:
            last_used, cost_usd = meta.get(name, (None, None))
            out[name] = {
                "last_used": last_used.isoformat() if last_used else None,
                "cost_usd": cost_usd,
            }
        return out

    # Catch only infra (DB engine / IO): a programming bug should surface as a 500
    # (the client falls back to name order on any non-OK response), not be masked as
    # a silently-empty sort.
    try:
        return await asyncio.to_thread(_collect)
    except (OSError, SQLAlchemyError) as exc:
        logger.warning("projects sortmeta read failed, degrading to empty: %s", exc)
        return {}


@router.get("/api/projects/{name}/preflight")
async def api_project_preflight(name: str, engine: EngineDep, runner: RunnerDep) -> dict:
    """Per-project spawn-readiness checks (the system-wide panel is ``/api/doctor``).

    Reports the preconditions specific to *this* project's bridge launch —
    workspace trust, whether it's a git repo (worktree mode), and whether its
    committed ``.mcp.json`` has servers still awaiting approval (#837) — as the
    same ``{name, status, detail}`` shape the doctor panel consumes. Derived from
    the discovered project (so trust/git match the card); read-only and
    auth-gated. Runs off the event loop: the MCP-approval check does real file
    I/O (``.mcp.json`` + ``~/.claude.json``).
    """
    if not is_valid_project_name(name):
        raise HTTPException(status_code=422, detail="invalid project name")
    proj = next((p for p in await _list_projects(engine) if p.name == name), None)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"project {name!r} not found")
    checks = await asyncio.to_thread(ops.project_preflight_checks, proj, runner.claude_json)
    return {
        "project": name,
        "ok": all(c.status != ops.FAIL for c in checks),
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
    }


@router.get("/api/projects/{name}/row", response_class=HTMLResponse)
async def api_project_row(
    request: Request, name: str, engine: EngineDep, config: ConfigDep, render: RenderDep
) -> Response:
    """Render one project's row for reactive insertion (no full-page reload).

    Same Jinja partial the dashboard grid loops over — one source of truth.
    ``idx=0`` so a freshly created project is never hidden by the Projects
    search/cap (it renders within the first page).
    """
    if not is_valid_project_name(name):
        raise HTTPException(status_code=422, detail="invalid project name")
    proj = next((p for p in await _list_projects(engine) if p.name == name), None)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"project {name!r} not found")
    return render(
        request,
        "_project_row.html",
        {
            "p": proj,
            "idx": 0,
            "pty_supported": _pty_supported(),
            # Same canonical label map the dashboard grid loop passes through (#685);
            # the standalone row render must carry it too so the launch <select>'s
            # <option> text and the bypass hint render identically here.
            "permission_labels": PERMISSION_LABELS,
            "bypass_desktop_hint": BYPASS_DESKTOP_HINT,
            # Gate the #837 "Resolve in Server approvals" link on config-write being
            # enabled (its target panel + /api/config-write/* routes 404 when off).
            # The full-page render passes this via _dashboard_context(); the fragment
            # route must pass it too, else an undefined Jinja var reads falsy and the
            # link would WRONGLY vanish on dynamically-inserted rows when it IS on.
            "config_write_enabled": config.config_write.enabled,
        },
    )


# ----- project provisioning: create and clone ------------------------------------------------
@router.post("/api/projects", status_code=201)
async def api_create_project(body: dict, config: ConfigDep, engine: EngineDep) -> Project:
    """Create a new project directory under ``projects_root``, optionally git-initialized."""
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="body must include a 'name' string")
    git_init = bool(body.get("git_init", False))
    try:
        await asyncio.to_thread(create_project, config.projects_root, name, git_init=git_init)
    except InvalidProjectName as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TargetExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GitUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ProvisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # The new directory may not bump projects_root's mtime at the cache's
    # resolution; drop the cache so the project is visible to _project_by_name now.
    invalidate_discovery_cache()
    return await _project_by_name(name, engine)


async def _run_clone(
    job: CloneJob,
    name: str,
    url: str,
    shallow: bool,
    *,
    clone_jobs: CloneJobManager,
    config: ClausterConfig,
    runner: SessionRunner,
) -> None:
    """Clone in a worker thread, streaming progress into the job's queue."""
    loop = asyncio.get_running_loop()

    def _forward(line: str) -> None:
        """Hand one git progress line to the job, hopping back onto the event loop."""
        loop.call_soon_threadsafe(clone_jobs.push_progress, job, line)

    def _register_proc(proc: subprocess.Popen[bytes]) -> None:
        """Register the worker's git process so a cancel request can terminate it."""
        # Hop onto the loop: the job registry is mutated event-loop-only (#573).
        loop.call_soon_threadsafe(job.register_terminate, proc.terminate)

    try:
        target = await asyncio.to_thread(
            clone_project,
            config.projects_root,
            name,
            url,
            cfg=config.clone,
            shallow=shallow,
            progress_cb=_forward,
            on_proc=_register_proc,
        )
    except ProvisionError as exc:
        # A cancel terminates git → non-zero exit → CloneFailed here; report it as a
        # clean `cancelled`, not an error (the worker already tore down the temp dir).
        if job.cancel_requested:
            clone_jobs.cancel(job)
        else:
            clone_jobs.finish(job, error=str(exc))
    except Exception as exc:  # defensive: never leave a job stuck "running"
        if job.cancel_requested:
            clone_jobs.cancel(job)
        else:
            clone_jobs.finish(job, error=f"unexpected error: {exc}")
    else:
        if job.cancel_requested:
            # A cancel arrived but `terminate()` was a no-op — git had already
            # finished and the dir landed. Honor the 202 "cancelling" contract: tear
            # down the just-created project and broadcast `cancelled`, not `done`.
            await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
            invalidate_discovery_cache()
            clone_jobs.cancel(job)
        else:
            # The cloned directory may not bump projects_root's mtime at the cache's
            # resolution; drop the cache so the new project's row renders immediately.
            invalidate_discovery_cache()
            clone_jobs.finish(job)
    # Fire the #432 `clone-done` webhook off the runner's emitter (fire-and-forget,
    # fail-open, default OFF). The error_detail is redacted before egress — a clone
    # failure can echo a remote URL/host into its message. The clone url itself is
    # never sent: it can carry credentials.
    runner.emit_event(
        "clone-done",
        {
            "event_type": "clone-done",
            "project": name,
            "status": job.status,
            "error": redact_for_disk(job.error_detail) if job.error_detail else None,
        },
    )
    loop.call_later(_CLONE_JOB_TTL, clone_jobs.discard, job.id)


@router.post("/api/projects/clone", status_code=202)
async def api_clone_project(
    body: dict,
    config: ConfigDep,
    runner: RunnerDep,
    clone_jobs: CloneJobsDep,
    clone_tasks: CloneTasksDep,
) -> dict:
    """Start an async clone; returns a job id to watch via ``/ws/clone-progress``."""
    if not config.clone.enabled:
        raise HTTPException(status_code=403, detail="clone is disabled in config")
    name = body.get("name")
    url = body.get("url")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="body must include a 'name' string")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=422, detail="A Git URL is required to clone.")
    if not is_valid_project_name(name):
        raise HTTPException(status_code=422, detail=f"invalid project name: {name!r}")
    if (config.projects_root / name).exists():
        raise HTTPException(status_code=409, detail=f"a directory named {name!r} already exists")
    shallow = bool(body.get("shallow", False))
    # Validate the URL up front (scheme + SSRF host resolve) so an obviously
    # bad clone fails the request itself; the DNS resolve runs off the loop.
    try:
        await asyncio.to_thread(validate_clone_url, url, config.clone)
    except InvalidCloneUrl as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except BlockedCloneHost as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    job = clone_jobs.create(name)
    # Hold a strong ref to the in-flight clone task so it isn't GC'd mid-run; the set
    # (injected via CloneTasksDep) lives on app.state so it is shared per app, not global.
    task = asyncio.create_task(
        _run_clone(job, name, url, shallow, clone_jobs=clone_jobs, config=config, runner=runner)
    )
    clone_tasks.add(task)
    task.add_done_callback(clone_tasks.discard)
    return {"job_id": job.id, "name": name}


@router.post("/api/projects/clone/{job_id}/cancel", status_code=202)
async def api_clone_cancel(job_id: str, clone_jobs: CloneJobsDep) -> dict:
    """Cancel an in-progress clone: terminate the git worker + clean its partial dir.

    404 for an unknown/expired job, 409 once it's already terminal (done/error/
    cancelled). The worker reports the ``cancelled`` outcome over the progress WS;
    the partial temp dir is torn down on the terminated git's non-zero exit (#573).
    """
    job = clone_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown or expired clone job")
    if not job.request_cancel():
        raise HTTPException(status_code=409, detail=f"clone job is already {job.status}")
    return {"job_id": job.id, "cancelling": True}


@router.get("/api/projects/clone/active")
async def api_clone_active(clone_jobs: CloneJobsDep) -> dict:
    """List in-flight clone jobs so a second tab can reattach to live progress (#659).

    Clone progress streams over the per-job WebSocket only to tabs that started (or
    already reattached to) it. A tab opened mid-clone polls this on load: a running
    job here lets it reattach to ``/ws/clone-progress/{job_id}`` and show the same
    live bar. The clone URL is never returned (it can carry credentials) — only the
    job id, project name, and current ``{phase, percent}``.
    """
    return {"jobs": [job.status_snapshot() for job in clone_jobs.active_jobs()]}


# ----- project adoption and workspace trust --------------------------------------------------
@router.post("/api/projects/{name}/adopt")
async def api_adopt(name: str, runner: RunnerDep) -> RemoteControlInstance:
    """Take over a live standard external session as a managed instance (#330).

    Fail closed: unknown project -> 404; already managed, or no live standard
    bridge to adopt (it ended, or it's a pty bridge) -> 409. A pty external
    session is never adoptable — it stays display-only.
    """
    try:
        return await runner.adopt(name)
    except UnknownProject as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InstanceStillLive, AdoptionUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/projects/{name}/trust")
async def api_trust(name: str, runner: RunnerDep) -> Project:
    """Accept the workspace-trust dialog for a project and return its refreshed state."""
    try:
        return await runner.trust_project(name)
    except UnknownProject as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OSError as exc:
        # ~/.claude.json exists but couldn't be read/written (e.g. permissions).
        # Surface it instead of silently dropping the operator's other settings.
        raise HTTPException(
            status_code=500, detail=f"could not update trust state: {exc}"
        ) from exc


@router.post("/api/projects/trust-all")
async def api_trust_all(runner: RunnerDep) -> list[Project]:
    """Trust every currently-untrusted discovered project; return the refreshed list.

    For reconciling installs after Claude Code 2.1.232 dropped nested-repo trust
    inheritance (#1224): one parent grant no longer covers the git repos under it, so
    this grants each discovered project its own trust key in a single action.
    """
    try:
        return await runner.trust_all_projects()
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"could not update trust state: {exc}"
        ) from exc


# ----- per-project CLAUDE.md -----------------------------------------------------------------
@router.get("/api/projects/{name}/claude-md")
async def api_claude_md_get(name: str, engine: EngineDep, runner: RunnerDep) -> ClaudeMdDoc:
    """Return the project's CLAUDE.md, stamped with whether a bridge is running."""
    path = await _resolve_project_path(name, engine)
    try:
        doc = await asyncio.to_thread(read_claude_md, path)
    except ClaudeMdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    doc.bridge_running = _bridge_running(runner, name)
    return doc


@router.put("/api/projects/{name}/claude-md")
async def api_claude_md_put(
    name: str, body: dict, config: ConfigDep, engine: EngineDep, runner: RunnerDep
) -> ClaudeMdDoc:
    """Write the project's CLAUDE.md, honouring the optional stale-hash guard."""
    path = await _resolve_project_path(name, engine)
    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=422, detail="body must include a 'content' string")
    base_sha = body.get("base_sha256")
    if base_sha is not None and not isinstance(base_sha, str):
        raise HTTPException(status_code=422, detail="base_sha256 must be a string")
    try:
        doc = await asyncio.to_thread(
            write_claude_md,
            path,
            content,
            base_sha256=base_sha,
            state_dir=config.state_dir,
            user=_SESSION_USER,
            claude_json=runner.claude_json,
        )
    except ClaudeMdTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ClaudeMdNotTrusted as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ClaudeMdConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ClaudeMdError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    doc.bridge_running = _bridge_running(runner, name)
    return doc
