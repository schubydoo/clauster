"""Instance/bridge registry, lifecycle, and hosted-channel routes (#1156).

These routes back the dashboard's instances surface and the hosted (claustrum
stream-json) channel, which share the ``/api/instances/{id}`` URL space and so
move together. They read the live bridge registry (instances, external working
sessions, the compact widget summary), start and stop and resume and forget a
managed bridge or a hosted session, render a session QR code, and drive a hosted
conversation (send a turn, answer a parked permission request).

Handlers read collaborators through the typed accessors in
:mod:`clauster.dependencies` (``RunnerDep``, ``HostedDep``, ``ConfigDep``,
``EngineDep``, ``ClaustrumDaemonDep``) rather than closing over ``create_app``.
Project-name resolution reuses :func:`clauster.routes._common.resolve_project_path`
and :func:`clauster.routes._common.list_projects`, so the traversal defense
(:func:`clauster.discovery.is_valid_project_name`) has a single home.

The hosted spawn path fails closed: it resolves the project first, validates the
permission mode, mirrors the project's ``allow_bypass_permissions`` ceiling
(:func:`clauster.routes._common.enforce_bypass_ceiling`), and refuses an untrusted
workspace with a 409 before any daemon spawn (invariants 1 and 2). That shared helper
defers to the single :meth:`ClausterConfig.bypass_denied` decision and shares its
:meth:`ClausterConfig.bypass_denied_detail` message, so no channel can diverge.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable
from typing import TYPE_CHECKING, TypeVar, cast

import segno
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import __version__, claude_cli
from ..claustrum_client import ClaustrumError
from ..config import PERMISSION_MODES
from ..dependencies import (
    ClaustrumDaemonDep,
    ConfigDep,
    EngineDep,
    HostedDep,
    RunnerDep,
)
from ..engine import ambiguity_hint
from ..hosted import NO_PROJECT_RESUME_DETAIL, HostedSessionError
from ..models import InstanceStatus, RemoteControlInstance, WorkingSession
from ..runner import (
    InstanceStillLive,
    InvalidSpawnOption,
    PermissionModeNotAllowed,
    SpawnError,
    UnknownProject,
)
from ..trust import is_trusted
from ._common import enforce_bypass_ceiling, list_projects, resolve_project_path

if TYPE_CHECKING:
    from pathlib import Path

    from ..claustrum_client import ClaustrumClient
    from ..claustrum_daemon import ClaustrumDaemon
    from ..config import ClausterConfig, PermissionMode
    from ..engine import ClausterEngine
    from ..hosted import HostedManager
    from ..runner import SessionRunner

router = APIRouter()

# Result type for _spawn_or_http: both the create and resume routes await a
# SpawnOutcome (#778, #1145) — same exception mapping.
_SpawnT = TypeVar("_SpawnT")


def _unresolved_bridge(
    runner: SessionRunner, instance_id: str, not_found_detail: str
) -> HTTPException:
    """Build the error for a bridge reference that didn't resolve (#1099, #1150).

    ``409`` when the reference was ambiguous, ``404`` when nothing matched at all. They are
    genuinely different answers: "no such bridge" versus "several, say which" — and the
    operator can act on the second only if told the candidates. Mirrors the ``ambiguous``
    reply ``session_status`` already returns on the MCP side.

    Two shapes reach the 409: an id **prefix** matching several bridges (#1099), and a bare
    **project name** matching several instances (#1150). The first regresses nothing —
    prefixes never resolved before, so that input used to 404. The second is a deliberate
    change: ``DELETE /api/instances/alpha`` with two ``alpha`` rows used to return 200 by
    silently picking the last-registered one, and now refuses rather than act on the row
    the operator did not mean. The caller passes the whole ``not_found_detail`` rather than
    a fragment so each route keeps its existing 404 wording byte-for-byte — the sites were
    never consistent about quoting the id, and normalizing that here would be an unrelated
    visible change riding along.
    """
    candidates, kind = runner.bridge_id_ambiguity(instance_id)
    if candidates:
        hint = ambiguity_hint(kind)
        return HTTPException(
            status_code=409,
            detail=(f"ambiguous {instance_id!r} — matches {', '.join(candidates)}; {hint}"),
        )
    return HTTPException(status_code=404, detail=not_found_detail)


async def _spawn_or_http(coro: Awaitable[_SpawnT]) -> _SpawnT:
    """Await a spawn/resume coroutine, mapping its exceptions to HTTP codes.

    Shared by the create and resume routes so the mapping lives in one place.
    Generic over the coroutine's result; both routes now await a
    :class:`~clauster.runner.SpawnOutcome` (#778, #1145).
    """
    try:
        return await coro
    except UnknownProject as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidSpawnOption as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionModeNotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SpawnError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _spawn_body(
    instance: RemoteControlInstance,
    *,
    created: bool,
    reason: str | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """Serialize a spawn response: the instance plus additive outcome keys (#778).

    The instance's own fields stay at the top level (existing clients read
    ``status``/``session_url`` etc. straight off the body); ``created`` /
    ``reason`` / ``warnings`` are additive so pre-#778 clients ignore them.
    """
    return {
        **instance.model_dump(mode="json"),
        "created": created,
        "reason": reason,
        "warnings": warnings or [],
    }


async def _hosted_prereqs(
    project: str,
    *,
    daemon: ClaustrumDaemon | None,
    runner: SessionRunner,
    config: ClausterConfig,
    engine: ClausterEngine,
) -> tuple[ClaustrumClient, Path, str]:
    """Resolve (daemon client, trusted project path, claude binary) for a hosted op.

    Shared by hosted spawn and resume; raises the same HTTP errors the spawn path
    has always used (503 no daemon / 503 binary missing, 409 untrusted directory).
    """
    client = daemon.client if daemon is not None else None
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="hosted channel unavailable: claustrum daemon not connected",
        )
    path = await resolve_project_path(project, engine)
    if not await asyncio.to_thread(is_trusted, path, runner.claude_json):
        raise HTTPException(
            status_code=409,
            detail=f"directory not trusted: {path}. Use the Trust action first.",
        )
    try:
        binary = await asyncio.to_thread(claude_cli.resolve_binary, config.claude.binary)
    except claude_cli.ClaudeNotFound as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return client, path, binary


async def _spawn_hosted(
    project: str,
    permission_mode: str | None,
    *,
    hosted: HostedManager,
    daemon: ClaustrumDaemon | None,
    runner: SessionRunner,
    config: ClausterConfig,
    engine: ClausterEngine,
) -> RemoteControlInstance:
    """Start a hosted (claustrum stream-json) session for ``project``."""
    # Confirm the project exists first so a missing name 404s instead of leaking a 403
    # from the ceiling, then gate the effective mode before any daemon/trust/spawn work.
    await resolve_project_path(project, engine)
    pm = permission_mode or config.instance_defaults.permission_mode
    # Validate the mode before any daemon/spawn work — parity with the bridge channel
    # (runner rejects an unknown mode pre-argv). Not exploitable (list-argv, no
    # injection), but an unknown mode is a client error: 422, not a 502 from a daemon
    # spawn that fails downstream.
    if pm not in PERMISSION_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"invalid permission_mode {pm!r}; expected one of {PERMISSION_MODES}",
        )
    enforce_bypass_ceiling(config, project, pm)
    client, path, binary = await _hosted_prereqs(
        project, daemon=daemon, runner=runner, config=config, engine=engine
    )
    try:
        return await hosted.spawn(
            client,
            project=project,
            label=f"hosted:{project}",
            cwd=str(path),
            claude_binary=binary,
            permission_mode=cast("PermissionMode", pm),
        )
    except HostedSessionError as exc:
        # BEFORE `ClaustrumError`, which it subclasses. A `HostedSessionError` from the
        # engine is a precondition the caller got wrong, not a daemon fault — and
        # "hosted spawn failed" at 502 reads as the daemon being down, sending the
        # operator to the wrong place. `_resume_hosted` already orders the two this way.
        # Not dead code: it is also the mapping for `start`'s "already started". Both
        # of its causes are unreachable from here today — this route passes no
        # `resume_uuid` (#1392) and `_spawn_session` mints a fresh session each call —
        # so the arm is what keeps the mapping right the day either changes.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ClaustrumError as exc:
        raise HTTPException(status_code=502, detail=f"hosted spawn failed: {exc}") from exc


async def _resume_hosted(
    hosted_id: str,
    instance: RemoteControlInstance,
    *,
    hosted: HostedManager,
    daemon: ClaustrumDaemon | None,
    runner: SessionRunner,
    config: ClausterConfig,
    engine: ClausterEngine,
) -> RemoteControlInstance:
    """Resume a lost/ended hosted session by id, respawning with ``--resume <uuid>``.

    ``instance`` is the row the route already fetched. Maps the engine's
    :class:`HostedSessionError` (unknown / still-running / no-uuid / malformed-uuid /
    no-project) to 409 and a daemon spawn failure to 502.
    """
    if not instance.project:
        # BEFORE `_hosted_prereqs`, which resolves the project path: a row whose saved
        # record degraded on `project` carries `project=""`, and an empty name 404s there
        # as "project '' not found" — which reads as "that project is gone" and sends the
        # operator looking in the wrong place. `_resume_locked` refuses it too, but only
        # a caller that is not this route would ever reach that guard (#1381).
        raise HTTPException(status_code=409, detail=NO_PROJECT_RESUME_DETAIL)
    client, path, binary = await _hosted_prereqs(
        instance.project, daemon=daemon, runner=runner, config=config, engine=engine
    )
    try:
        return await hosted.resume(client, hosted_id, cwd=str(path), claude_binary=binary)
    except HostedSessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ClaustrumError as exc:
        raise HTTPException(status_code=502, detail=f"hosted resume failed: {exc}") from exc


# ----- registry read surfaces: instances, hosted, widget, sessions ---------------------------
@router.get("/api/instances")
async def api_instances(runner: RunnerDep) -> list[RemoteControlInstance]:
    """Every managed bridge, one row per instance (registration order).

    A project may contribute several rows (#778): at most one *live* standard
    (server-mode) bridge, plus any number of stopped/resumable standard rows and
    any number of interactive (pty) sessions. The cap is on live bridges, not on
    rows — a fresh spawn mints a new ``instance_id``, so stopped standard rows
    accumulate until they are forgotten.

    Group client-side by ``project`` and key rows by ``instance_id`` — ``project``
    is not unique. Keying a client collection by ``project`` silently drops rows
    and was #1143.
    """
    return runner.list_instances()


@router.get("/api/hosted")
async def api_hosted(hosted: HostedDep) -> list[RemoteControlInstance]:
    """Hosted (claustrum stream-json) sessions, each status-synced to its live session.

    Kept separate from ``/api/instances`` (project-keyed bridges): hosted
    sessions live in their own ``HostedManager`` registry, are keyed by a
    client-chosen id, and there may be several per project. The dashboard's
    hosted panel polls this; empty list when the channel is unused. Auth-gated
    by the guard middleware like every other ``/api/*`` route.
    """
    instances = hosted.list_instances()
    # Debounced (no-op when unchanged): refresh the persisted reattach cursors on
    # the dashboard's poll cadence, so a restart replays from a recent daemon seq.
    await hosted.persist()
    return instances


@router.get("/api/widget")
async def api_widget(runner: RunnerDep, engine: EngineDep) -> dict:
    """Compact dashboard-widget summary (e.g. Homepage/Homarr custom API widgets).

    Returns a small, stable, flat JSON shape sourced entirely from live runner
    state plus the project list — the same data the dashboard already renders, so
    nothing here is computed or invented beyond what's observable.

    Shape::

        {
            "projects_total": int,                # discovered projects under projects_root
            "bridges": {<InstanceStatus>: int},   # every status key present, 0 when none
            "running_total": int,                 # == bridges["running"]
            "version": str,                       # clauster package version
        }

    Read-only and auth-gated by the guard middleware like every other ``/api/*``
    route. NB: a Homepage-style scraper hitting an auth-enabled deploy still needs
    to supply auth / network reachability itself — that's not solved here (same
    follow-up as the metrics endpoints).
    """
    instances = runner.list_instances()
    # Enumerate the enum so every status key is always present (0 when none),
    # giving the widget a stable schema regardless of the current bridge mix.
    by_status = {status.value: 0 for status in InstanceStatus}
    for inst in instances:
        by_status[inst.status.value] += 1
    projects = await list_projects(engine)
    return {
        "projects_total": len(projects),
        "bridges": by_status,
        "running_total": by_status[InstanceStatus.RUNNING.value],
        "version": __version__,
    }


@router.get("/api/sessions")
async def api_sessions(runner: RunnerDep) -> dict[str, list[WorkingSession]]:
    """External (unmanaged) working sessions grouped by project name (bug #4)."""
    return runner.external_sessions_by_project()


@router.get("/api/sessions/tracked")
async def api_sessions_tracked(runner: RunnerDep) -> dict[str, list[WorkingSession]]:
    """Live working sessions owned by each managed bridge, keyed by instance (#570).

    A standard ``claude remote-control`` bridge is multi-session; this exposes
    every live session under it (not just the starter) so the dashboard can list
    them. Driven by the same ``agents --json`` reconcile join as ``/api/sessions``
    — no new poll. pty (single-session) bridges simply map to their one session.
    """
    return runner.tracked_sessions_by_instance()


@router.get("/api/sessions/adoptable")
async def api_sessions_adoptable(runner: RunnerDep) -> list[str]:
    """Project names whose live external session is a standard bridge safe to adopt (#330).

    The dashboard gates its per-project Adopt affordance on this list — a pty
    (flag-form) external bridge is excluded (unsafe to adopt; see runner.adopt).
    Off-loaded to a thread (filesystem + ``psutil``). Auth-gated by the guard
    middleware like every other ``/api/*`` route.
    """
    return sorted(await asyncio.to_thread(runner.adoptable_external_projects))


# ----- bridge lifecycle: spawn, stop, resume, forget -----------------------------------------
@router.post("/api/instances", status_code=201)
async def api_spawn(
    body: dict,
    response: Response,
    runner: RunnerDep,
    hosted: HostedDep,
    daemon: ClaustrumDaemonDep,
    config: ConfigDep,
    engine: EngineDep,
) -> dict:
    """Start a bridge/session; 201 when launched, 200 when an existing one was reused.

    The body is the instance plus outcome keys (#778): ``created`` is False —
    with ``reason`` — when the standard-singleton cap returned the already-live
    standard bridge instead of launching a second one; ``warnings`` carries
    non-blocking advisories (an interactive pty session launched without a
    worktree risks conflicting concurrent edits).

    ``channel`` (default ``"remote-control"``) picks the subsystem: ``"hosted"``
    short-circuits to the claustrum stream-json path (always 201, ``created``
    True), and any other value is a 422. Only the remote-control branch can
    return the 200/``created`` False singleton outcome.
    """
    project = body.get("project")
    if not isinstance(project, str) or not project:
        raise HTTPException(status_code=422, detail="body must include a 'project' string")
    spawn_mode = body.get("spawn_mode")
    permission_mode = body.get("permission_mode")
    resume_mode = body.get("resume_mode")
    # Optional custom bridge/session display name (#780) — --name for a standard
    # bridge in place of the project name. Blank/omitted keeps today's default;
    # runner.spawn_detailed validates it (length/control chars) before any spawn
    # side effect, surfaced here as a 422 via _spawn_or_http's InvalidSpawnOption
    # mapping.
    name = body.get("name")
    # Optional per-launch sandbox toggle (#780) — tri-state "default"/"on"/"off" for a
    # standard bridge. DISABLED for 1.0 (#1037): still accepted + enum-validated by
    # runner.spawn_detailed (422 on a bad value), but inert — the runner emits no
    # --sandbox flag and coerces persisted values to "default" until #1046 re-enables it.
    sandbox = body.get("sandbox")
    # Optional past-conversation fork for a pty launch (#303) — the transcripts
    # API's session uuid, spawned as `--resume <uuid> --fork-session`. Format,
    # pty-only, and revive-exclusivity rules are enforced by runner.spawn_detailed
    # BEFORE any spawn side effect (InvalidSpawnOption → 422 via _spawn_or_http);
    # here we only type-gate like the sibling optional fields.
    resume_session_id = body.get("resume_session_id")
    channel = body.get("channel", "remote-control")
    for field, value in (
        ("spawn_mode", spawn_mode),
        ("permission_mode", permission_mode),
        ("resume_mode", resume_mode),
        ("name", name),
        ("sandbox", sandbox),
        ("resume_session_id", resume_session_id),
        ("channel", channel),
    ):
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"{field} must be a string")
    if channel == "hosted":
        return _spawn_body(
            await _spawn_hosted(
                project,
                permission_mode,
                hosted=hosted,
                daemon=daemon,
                runner=runner,
                config=config,
                engine=engine,
            ),
            created=True,
        )
    if channel != "remote-control":
        raise HTTPException(status_code=422, detail=f"unknown channel: {channel!r}")
    outcome = await _spawn_or_http(
        runner.spawn_detailed(
            project,
            spawn_mode=spawn_mode,
            permission_mode=permission_mode,
            resume_mode=resume_mode,
            custom_name=name,
            sandbox=sandbox,
            resume_session_id=resume_session_id,
        )
    )
    if not outcome.created:
        # Nothing was launched — the existing live instance came back. 200, not
        # 201: no resource was created, and `reason` says why.
        response.status_code = 200
    return _spawn_body(
        outcome.instance,
        created=outcome.created,
        reason=outcome.reason,
        warnings=outcome.warnings,
    )


@router.get("/api/instances/{instance_id}")
async def api_instance(
    instance_id: str, runner: RunnerDep, hosted: HostedDep
) -> RemoteControlInstance:
    """Return one bridge or hosted instance by id, or by the project name old clients send."""
    # Also accepts a raw instance_id / hosted id (#777; the #778 API split moves
    # fully to ids).
    resolved = runner.resolve_bridge_id(instance_id)
    instance = (
        runner.get_instance(resolved) if resolved is not None else None
    ) or hosted.get_instance(instance_id)
    if instance is None:
        raise _unresolved_bridge(runner, instance_id, f"no such instance: {instance_id}")
    return instance


@router.post("/api/instances/{instance_id}/message", status_code=202)
async def api_hosted_message(instance_id: str, body: dict, hosted: HostedDep) -> dict:
    """Send one user turn to a hosted session (the conversation input path)."""
    text = body.get("text")
    if not isinstance(text, str) or not text:
        raise HTTPException(status_code=422, detail="body must include a non-empty 'text' string")
    if hosted.get_instance(instance_id) is None:
        raise HTTPException(status_code=404, detail=f"no such hosted session: {instance_id}")
    try:
        await hosted.send(instance_id, text)
    except HostedSessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/api/instances/{instance_id}/permissions/{request_id}", status_code=202)
async def api_hosted_permission(
    instance_id: str, request_id: str, body: dict, hosted: HostedDep
) -> dict:
    """Answer a parked tool-permission request on a hosted session (CL-5).

    The engine parks every tool-permission ``control_request`` and waits
    (fail-closed) until something answers — this is that explicit human gate.
    ``decision`` is ``"allow"`` or ``"deny"`` (a deny may carry a short
    ``message``), mapped to the SDK ``can_use_tool`` response ``{"behavior": …}``.
    """
    decision = body.get("decision")
    if decision not in ("allow", "deny"):
        raise HTTPException(status_code=422, detail="decision must be 'allow' or 'deny'")
    if hosted.get_instance(instance_id) is None:
        raise HTTPException(status_code=404, detail=f"no such hosted session: {instance_id}")
    if decision == "allow":
        response: dict = {"behavior": "allow"}
    else:
        message = body.get("message")
        response = {
            "behavior": "deny",
            "message": message if isinstance(message, str) and message else "Denied by operator",
        }
    try:
        await hosted.respond(instance_id, request_id, response)
    except HostedSessionError as exc:
        # Already answered, or no such parked request — not in a state to answer.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.delete("/api/instances/{instance_id}")
async def api_stop(
    instance_id: str, runner: RunnerDep, hosted: HostedDep
) -> RemoteControlInstance:
    """Stop the identified session, whether it is a hosted one or a managed bridge."""
    if hosted.get_instance(instance_id) is not None:
        # A live hosted session stops cleanly; one with no live session (an orphan
        # that survived a daemon restart, or an already-dead row) is killed/cleaned
        # up by id — kill_orphan, not stop, since stop requires a live session.
        try:
            if hosted.session(instance_id) is not None:
                return await hosted.stop(instance_id)
            return await hosted.kill_orphan(instance_id)
        except HostedSessionError as exc:
            # The row vanished between the existence check and the awaited call
            # (concurrent stop/reattach) — treat as gone, not a 500.
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    resolved = runner.resolve_bridge_id(instance_id)
    if resolved is None:
        raise _unresolved_bridge(runner, instance_id, f"no managed instance: {instance_id!r}")
    try:
        return await runner.stop(resolved)
    except UnknownProject as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/instances/{instance_id}/resume")
async def api_resume(
    instance_id: str,
    runner: RunnerDep,
    hosted: HostedDep,
    daemon: ClaustrumDaemonDep,
    config: ConfigDep,
    engine: EngineDep,
) -> dict:
    """Re-spawn a stopped/crashed bridge or hosted session into its prior conversation.

    Bridges reuse their stored spawn/permission modes; a hosted session respawns
    a fresh daemon process with ``--resume <claude_session_uuid>`` (CL-7).

    The body is the instance plus the same additive outcome keys the create route
    returns (#778): ``created`` is False — with ``reason`` — when nothing was
    revived, because the standard-singleton cap handed back the already-live bridge
    for that project instead, or because an interactive (pty) target was already
    live. A caller that ignores ``created`` reports a resume that never happened as
    success (#1145). The body's ``instance_id`` is usually a *different* bridge, but
    the pty path hands back the target itself — so comparing ids is not a substitute
    for reading ``created``.
    """
    hosted_instance = hosted.get_instance(instance_id)
    if hosted_instance is not None:
        return _spawn_body(
            await _resume_hosted(
                instance_id,
                hosted_instance,
                hosted=hosted,
                daemon=daemon,
                runner=runner,
                config=config,
                engine=engine,
            ),
            created=True,
        )
    resolved = runner.resolve_bridge_id(instance_id)
    if resolved is None:
        raise _unresolved_bridge(
            runner, instance_id, f"no managed instance to resume: {instance_id!r}"
        )
    try:
        outcome = await _spawn_or_http(runner.resume_detailed(resolved))
        return _spawn_body(
            outcome.instance,
            created=outcome.created,
            reason=outcome.reason,
            warnings=outcome.warnings,
        )
    except HTTPException as exc:
        # Only a genuine spawn failure (SpawnError -> 409) means the bridge tried to
        # come back and could not — that is the case worth a #541 reconnect-failed
        # notification. A 404 (the instance vanished / unknown), 422 (bad spawn option)
        # or 403 (permission-mode) is a precondition error, not a failed reconnect, so
        # it must NOT notify (#652). Fire only for 409, then re-raise unchanged.
        if exc.status_code == 409:
            runner.notify_app_event(
                "reconnect-failed",
                "clauster: reconnect failed",
                f"Resuming the bridge for {instance_id!r} failed.",
            )
        raise


@router.post("/api/instances/{instance_id}/forget")
async def api_forget(instance_id: str, runner: RunnerDep, hosted: HostedDep) -> dict:
    """Drop a stopped/crashed session's record so it leaves the Recent/resumable list.

    Both bridges and hosted sessions persist a record that survives a Stop (so they
    stay Resumable); forget removes it to start fresh. Fail closed: a still-live
    session is refused with 409 (Stop/Kill it first) — forget never terminates a
    process — and an unknown id is 404.
    """
    hosted_instance = hosted.get_instance(instance_id)
    try:
        if hosted_instance is not None:
            # A known hosted id can only fail here as "still live" -> 409 (unknown
            # hosted ids are None above and fall through to the bridge runner).
            await hosted.forget(instance_id)
        else:
            # Refuse an ambiguous prefix BEFORE the verbatim fallback below: `or
            # instance_id` would otherwise hand `forget` the raw prefix, and the
            # operator would get a bare 404 for an id that names several real
            # bridges rather than being told which (#1099).
            if runner.bridge_id_candidates(instance_id):
                raise _unresolved_bridge(runner, instance_id, f"no such instance: {instance_id}")
            # Accept the project name the current client sends as well as a raw
            # instance_id (#777); fall back to the id verbatim so a purely-persisted
            # (not-yet-materialized) record still reaches runner.forget's own lookup.
            await runner.forget(runner.resolve_bridge_id(instance_id) or instance_id)
    except UnknownProject as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InstanceStillLive, HostedSessionError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": instance_id, "forgotten": True}


# ----- session QR codes ----------------------------------------------------------------------
@router.get("/api/instances/{instance_id}/qr")
async def api_instance_qr(instance_id: str, runner: RunnerDep) -> Response:
    """SVG QR for the primary deep link (feature 5) — scan to open on mobile."""
    resolved = runner.resolve_bridge_id(instance_id)
    instance = runner.get_instance(resolved) if resolved is not None else None
    if instance is None:
        raise _unresolved_bridge(runner, instance_id, f"no such instance: {instance_id}")
    target = instance.session_url or instance.url
    if not target:
        raise HTTPException(status_code=409, detail="no session URL available yet")
    buf = io.BytesIO()
    segno.make(target, error="m").save(buf, kind="svg", scale=4, border=2)
    return Response(content=buf.getvalue(), media_type="image/svg+xml")
