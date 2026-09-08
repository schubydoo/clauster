"""Background-agent routes (`claude --bg`, supervisor.py), split from ``create_app`` (#1156).

These four routes drive the agent-view background sessions: list them read-only,
dispatch a new one, stop one cleanly, and resume an ended one. Every dispatch
validates the project name before spawning (invariant 2) and mirrors the runner's
``bypassPermissions`` ceiling via :func:`_enforce_bypass_ceiling`, failing closed so
the background channel cannot sidestep a project's ``allow_bypass_permissions`` gate
(invariant 1).
The stop route redacts any ``claude rm`` detail through :func:`clauster.redact.redact_for_disk`
before it leaves the process on the ``bg-settled`` event (invariant 4).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException

from .. import claude_cli, supervisor
from ..dependencies import ConfigDep, RunnerDep
from ..discovery import is_valid_project_name
from ..models import BackgroundJob
from ..redact import redact_for_disk

if TYPE_CHECKING:
    from ..config import ClausterConfig

router = APIRouter()


def _enforce_bypass_ceiling(
    config: ClausterConfig, project: str, permission_mode: str | None
) -> None:
    """Reject ``bypassPermissions`` when a project's config ceiling forbids it.

    The runner enforces this hard ceiling for the bridge channel
    (``PermissionModeNotAllowed`` -> 403 on the bridge path). The background-agent
    channel spawns outside the runner, so it mirrors the gate here or a crafted
    request could run a session in bypass mode the project's
    ``allow_bypass_permissions`` ceiling forbids. A twin in ``routes/instances.py``
    guards the hosted channel the same way; both defer to the single
    :meth:`ClausterConfig.bypass_denied` decision and share its
    :meth:`ClausterConfig.bypass_denied_detail` message, so neither the decision nor
    the wording can diverge. The two thin twins fold into a shared ``routes/`` helper
    in #1523.
    """
    if config.bypass_denied(project, permission_mode):
        raise HTTPException(status_code=403, detail=config.bypass_denied_detail(project))


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
    _enforce_bypass_ceiling(config, name, pm)
    if prompt is None and rc_name is None:
        # #1033: an un-registered background session has no composer and no
        # cloud surface — dispatched without a prompt it parks at "send a
        # prompt to start" forever, with no way to ever receive one (the bg
        # card offers only Stop/Forget). ``rc_name`` opens the claude.ai door,
        # where the session is conversational, so a blank prompt is legitimate
        # there. Gated after the 404/403 checks so existence and the bypass
        # ceiling keep their precedence (same ordering rationale as
        # _enforce_bypass_ceiling above).
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
            binary=config.claude.binary,
            claude_json=runner.claude_json,
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
    """Resume an ended `claude --bg` session into a new bg job inheriting its transcript.

    The path `{job_id}` is the 8-hex short id (validated like the stop path); the
    `--resume` argument is the job's full session UUID, validated separately
    (`valid_session_id`) — the 8-hex guard would reject a UUID. Resume mints a NEW
    job id (and session UUID) that inherits the prior transcript, so `{id}` returned
    here differs from the path id; the panel surfaces it as a new row.
    """
    if not supervisor.valid_job_id(job_id):
        raise HTTPException(status_code=422, detail="invalid job id")
    try:
        new_id = await asyncio.to_thread(
            supervisor.resume_background_job,
            job_id,
            binary=config.claude.binary,
            claude_json=runner.claude_json,
        )
    except claude_cli.ClaudeNotFound as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except supervisor.ResumeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except supervisor.DispatchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"id": new_id}
