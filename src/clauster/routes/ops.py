"""Ops, ghost-environment, and app-config editor routes (#1156).

These routes back the dashboard's operational surface: the liveness/readiness
probes (``/healthz``, ``/metrics``, ``/api/doctor``), the in-place restart used to
apply a saved config change, the ghost hosted-environment reaper, and the Tier-A /
Tier-B in-app config editor.

The auth posture is unchanged by the move. ``/healthz`` stays public (matched by the
guard middleware's ``_is_public`` check) and calls the shared ``_authenticate``
closure — published on ``app.state`` and injected here — only to decide whether to add
the authenticated detail fields. ``/metrics`` stays behind the ``guard`` middleware, which
requires an authenticated session; a valid scrape token (``_metrics_token_ok``) is an
*additional* way in for a session-less caller like Prometheus, not the gate itself. This
handler just renders the payload. The reaper routes are
opt-in (``reaper.ui_enabled``), fail closed on any liveness-probe failure, and
re-derive the ghost set server-side so a client can never widen the blast radius.
The Tier-B ``/api/config/advanced`` routes stay behind the same fail-closed order
(config-write capability → step-up elevation) via the shared ``require_elevated``
gate, and ``POST /api/restart`` mutates ``app.state`` directly through its
``Request`` because those two scalars are written, not read.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .. import (
    __version__,
    claude_cli,
    config_audit,
    config_editor,
    config_writer,
    environments,
    ops,
    prometheus,
)
from ..dependencies import (
    AuthenticateDep,
    ClaustrumDaemonDep,
    ConfigDep,
    EngineDep,
    HostedDep,
    LoginStatusCacheDep,
    RequireElevatedDep,
    RunnerDep,
)
from ..models import InstanceStatus

if TYPE_CHECKING:
    from ..config import ClausterConfig
    from ..runner import SessionRunner

logger = logging.getLogger(__name__)

router = APIRouter()

# Single-user actor for the config-write audit trail (single-user in v0.2; multi-user
# is v0.3). Mirrors ``app._SESSION_USER``; the config-write domain still uses the app.py
# copy, and the two merge when that domain also moves to ``routes/`` (#1156).
_SESSION_USER = "admin"


# ----- liveness, the Prometheus endpoint, and system doctor ----------------------------------
@router.get("/healthz")
async def healthz(
    request: Request,
    config: ConfigDep,
    runner: RunnerDep,
    login_status_cache: LoginStatusCacheDep,
    claustrum_daemon: ClaustrumDaemonDep,
    authenticate: AuthenticateDep,
) -> dict:
    """Report liveness — plus claude version and counts once the caller is authenticated."""
    # Unauthenticated callers get only liveness when auth is enabled — don't
    # leak claude version / running count on a public reverse-proxy deploy.
    if config.auth.enabled and (await authenticate(request))[0] is None:
        return {"status": "ok"}
    try:
        version = await asyncio.to_thread(claude_cli.claude_version, config.claude.binary)
        claude_ok = True
    except Exception as exc:
        # Stay non-throwing and fail closed (claude_ok=False surfaces in the response),
        # but leave a diagnostic line — the one probe in this handler that was silent.
        logger.warning("healthz claude probe failed: %s", exc)
        version = None
        claude_ok = False
    # #838: claude_ok only confirms the binary is invokable, not that the account
    # is authenticated — an expired/absent login lets a bridge spawn and then hang
    # at "Starting" with no upfront signal. `claude auth status --json` is the
    # mechanism-agnostic signal (OAuth / apiKeyHelper / API key / env token all
    # reflected in loggedIn). Read is served from a stale-while-revalidate cache:
    # non-blocking (the subprocess never runs on this request path) and the probe
    # runs at most once per TTL, single-flight. A cold start returns a neutral
    # "unknown" (claude_login_ok=True) so the UI never cries wolf before probing.
    # Only the non-PII loggedIn + authMethod are surfaced (never email/org/token).
    login = login_status_cache.read()
    result: dict[str, object] = {
        "status": "ok",
        "version": __version__,
        "claude_ok": claude_ok,
        "claude_version": version,
        "claude_login_ok": login.logged_in,
        "claude_login_method": login.method,
        "claude_login_expires_at": login.expires_at_ms,
        "instances_running": runner.running_count(),
    }
    if config.claustrum.enabled:
        result["claustrum"] = (
            await claustrum_daemon.probe()
            if claustrum_daemon is not None
            else {"enabled": True, "running": False}
        )
    return result


def _cached_bridge_samples(runner: SessionRunner) -> list[tuple[str, float, int]]:
    """(project, cpu, rss) for each PROJECT in the server-side metrics cache (#354).

    Per-bridge samples are folded and summed per project by
    :meth:`~clauster.runner.SessionRunner.metrics_snapshots`, matching the
    project-only gauge labels. Reads the runner's snapshot (refreshed off the request
    path by the metrics task), so the scrape does no per-request sampling — consistent
    with the per-project / batch endpoints. Empty when metrics are disabled (cache
    stays bare).
    """
    return [
        (project, float(s.get("cpu_percent", 0.0)), int(s.get("rss_bytes", 0)))
        for project, s in runner.metrics_snapshots().items()
    ]


@router.get("/metrics")
async def prometheus_metrics(
    config: ConfigDep,
    runner: RunnerDep,
    hosted: HostedDep,
    engine: EngineDep,
    claustrum_daemon: ClaustrumDaemonDep,
) -> Response:
    """Render the Prometheus exposition, or 404 when the endpoint is disabled."""
    # Behind the auth guard unless observability.metrics_token_hash is set (then a
    # valid scrape token grants access without a session — see the guard).
    if not config.observability.prometheus_enabled:
        raise HTTPException(status_code=404, detail="metrics endpoint is disabled")
    projects = await asyncio.to_thread(engine.list_projects)
    hosted_live = sum(
        1
        for inst in hosted.list_instances()
        if inst.status in (InstanceStatus.RUNNING, InstanceStatus.STARTING)
    )
    claustrum_up: bool | None = None
    if config.claustrum.enabled:
        claustrum_up = claustrum_daemon is not None and bool(
            claustrum_daemon.status().get("running")
        )
    body = prometheus.render_metrics(
        version=__version__,
        instances=runner.list_instances(),
        project_count=len(projects),
        bridge_samples=_cached_bridge_samples(runner),
        crash_counts=runner.crash_counts(),
        hosted_sessions=hosted_live,
        claustrum_up=claustrum_up,
    )
    return Response(content=body, media_type=prometheus.CONTENT_TYPE)


@router.get("/api/doctor")
async def api_doctor(config: ConfigDep) -> dict:
    """System-readiness checks for the dashboard preflight panel.

    Surfaces the same diagnostics as the ``clauster doctor`` CLI (claude binary +
    version, login, projects_root, state_dir, git, auth sanity, workspace trust,
    source freshness) as JSON, so the browser can show a ✓/⚠/✗ checklist up front
    instead of letting a precondition fail silently at spawn time. The CLI's
    listen-port probe is omitted here (``check_port=False``): this server holds the
    port, so it would always false-warn "already in use", and the port isn't a
    bridge prerequisite anyway.

    Read-only and auth-gated by the guard middleware. Runs off the event loop:
    doctor does blocking subprocess probes (``claude --version``, ``git``). It
    re-reads the config from ``source_path`` (same as the CLI), so it also reflects
    on-disk edits made since boot; an env-only deploy with no config file surfaces a
    single ``config`` FAIL, which is the honest result.
    """
    src = config.source_path
    # check_port=False: this server holds config.port, so the availability probe
    # would always warn "already in use (Clauster already running?)" — a false
    # positive in the dashboard (the port isn't a bridge prerequisite either).
    checks, ok = await asyncio.to_thread(
        ops.run_doctor, str(src) if src is not None else None, check_port=False
    )
    return {
        "ok": ok,
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
    }


# --- ghost-environment reaper (spec §11), dashboard surface ----------------
# Destructive first-party API, so: opt-in config gate, fail-closed live set,
# and every action re-derives the ghost set server-side (see _gather_ghosts).
def _gather_ghosts(
    config: ClausterConfig,
) -> tuple[environments.EnvironmentsClient, list, set, list]:
    """Sync: creds → list envs → live set (fail-closed) → ghosts.

    Mirrors the CLI's safety rails. Raises HTTPException on any failure so the
    route never proceeds on partial information.
    """
    try:
        creds = environments.load_credentials(now_ms=int(time.time() * 1000))
    except environments.CredentialsError as exc:
        raise HTTPException(status_code=503, detail=f"credentials unavailable: {exc}") from exc
    client = environments.EnvironmentsClient(creds)
    try:
        envs = client.list_environments()
    except environments.EnvironmentsAPIError as exc:
        raise HTTPException(status_code=502, detail=f"environments API error: {exc}") from exc
    # SAFETY: never reap without a trustworthy live set — an incomplete one could
    # see a live bridge as a ghost. Fail closed on ANY liveness-probe failure.
    try:
        live = environments.live_bridge_directories(config.claude.binary, config.projects_root)
    except Exception as exc:  # noqa: BLE001 — fail closed: any liveness-probe failure must not let a reap proceed
        raise HTTPException(
            status_code=503,
            detail=f"refusing to reap — could not determine live bridges: {exc}",
        ) from exc
    # projects_root scopes the classification: this instance may only reap
    # environments inside its own tree (#1100).
    ghosts = environments.find_ghosts(envs, live, projects_root=config.projects_root)
    return client, envs, live, ghosts


@router.get("/api/environments/ghosts")
async def api_environment_ghosts(config: ConfigDep) -> dict:
    """List the ghost hosted environments, or 404 when the reaper UI is disabled."""
    if not config.reaper.ui_enabled:
        raise HTTPException(status_code=404, detail="reaper UI is disabled")

    def _work() -> dict:
        """Gather the current ghost set and shape it for the response."""
        _client, envs, live, ghosts = _gather_ghosts(config)
        return {
            "enabled": True,
            "total": len(envs),
            "live_dirs": len(live),
            "ghosts": [
                {"id": g.id, "directory": g.config.directory, "name": g.name} for g in ghosts
            ],
        }

    return await asyncio.to_thread(_work)


@router.post("/api/environments/reap")
async def api_environment_reap(body: dict, config: ConfigDep) -> dict:
    """Archive or delete the requested ghost environments behind a typed-confirm gate."""
    if not config.reaper.ui_enabled:
        raise HTTPException(status_code=404, detail="reaper UI is disabled")
    action = body.get("action")
    if action not in ("archive", "delete"):
        raise HTTPException(status_code=422, detail="action must be 'archive' or 'delete'")
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=422, detail="ids must be a non-empty list of strings")
    # Typed-confirm gate; the irreversible force-delete demands the stricter token.
    expected = "DELETE" if action == "delete" else "archive"
    if body.get("confirm") != expected:
        raise HTTPException(status_code=400, detail=f"confirmation text must be {expected!r}")

    def _work() -> dict:
        """Re-derive the ghost set server-side and act only on ids still inside it."""
        client, _envs, _live, ghosts = _gather_ghosts(config)
        # Re-derive server-side: only ever act on ids that are CURRENTLY ghosts.
        # Anything else (a now-live bridge, the cloud Default, an unknown or stale
        # id) is left untouched and reported back as skipped — the client cannot
        # widen the blast radius beyond the freshly-computed ghost set.
        requested = set(ids)
        ghost_ids = {g.id for g in ghosts}
        reaped: list[str] = []
        errors: dict[str, str] = {}
        for g in ghosts:
            if g.id not in requested:
                continue
            try:
                if action == "delete":
                    client.delete_environment(g.id, force=True)
                else:
                    client.archive_environment(g.id)
                reaped.append(g.id)
            except environments.EnvironmentsAPIError as exc:
                errors[g.id] = str(exc)
        return {
            "action": action,
            "reaped": reaped,
            "skipped": sorted(requested - ghost_ids),
            "errors": errors,
        }

    return await asyncio.to_thread(_work)


# ----- in-app config editor: Tier-A (/api/config) and Tier-B (/api/config/advanced) ----------
@router.get("/api/config")
async def api_config_get(config: ConfigDep) -> dict:
    """Tier-A editable config values + a content hash, for the in-app editor (FE-3).

    Only allowlisted operational fields are returned — no auth/secret/bind/
    structural value is ever surfaced (structural redaction). Auth-gated like
    every ``/api/*`` route by the guard middleware.
    """
    path = config.source_path
    # Serve the values from the on-disk file (consistent with the content hash
    # below), not the startup config: a prior save writes the file but does not
    # live-reload the runtime, so app.state.config goes stale after any save —
    # reopening the editor would then show pre-save values and a successful save
    # looks reverted. Fall back to the in-memory config if the file can't be
    # re-read (missing / externally corrupted) so the editor still opens.
    fields = None
    content_hash = None
    present = None
    if path is not None:
        fields = config_editor.editable_values_on_disk(path)
        # One read of the on-disk file yields both the external-edit hash and the set of
        # Tier-A keys literally present — the latter lets the editor drop a deprecated row
        # once the user removed its key (e.g. via `config reconcile`). Both are None when
        # the file can't be read (deleted / unreadable after startup): the editor still
        # opens on the in-memory `fields` fallback below, a save is safely rejected for the
        # missing hash, and nothing is hidden (fail-open on display — never hide a field we
        # can't prove is absent).
        content_hash, present = config_editor.disk_state(path)
    if fields is None:
        fields = config_editor.editable_values(config)
    specs = config_editor.field_specs(present, config=config)
    # The front-end builds its rendered rows from `editable`, so a hidden deprecated
    # field is removed by dropping it here — editing `specs` alone would not hide the row.
    editable = [p for p in config_editor.EDITABLE_FIELDS if not specs[p]["hidden"]]
    return {
        "fields": fields,
        "editable": editable,
        "specs": specs,
        "hash": content_hash,
    }


@router.put("/api/config")
async def api_config_put(body: dict, config: ConfigDep) -> dict:
    """Apply allowlisted config edits: re-validate + backup + atomic write.

    Tier-A only — a non-allowlisted key is a 400, never a silent drop. Requires
    the ``hash`` from GET (external-edit guard → 409 on mismatch). Writes to disk
    but does **not** live-reload; the response flags that a restart is needed.
    """
    path = config.source_path
    if path is None:
        raise HTTPException(status_code=409, detail="config has no on-disk source to edit")
    edits = body.get("edits")
    if not isinstance(edits, dict) or not edits:
        raise HTTPException(status_code=422, detail="body must include a non-empty 'edits' map")
    expected = body.get("hash")
    if not isinstance(expected, str) or not expected:
        raise HTTPException(
            status_code=422, detail="body must include the 'hash' from GET /api/config"
        )
    try:
        new_hash = await asyncio.to_thread(
            config_writer.write_edits, path, edits, expected_hash=expected
        )
    except config_editor.DisallowedFieldError as exc:
        raise HTTPException(status_code=400, detail=f"not editable: {exc}") from exc
    except config_editor.StaleConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except config_editor.ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=f"invalid config: {exc}") from exc
    return {"hash": new_hash, "restart_required": True}


def _require_advanced(
    request: Request, config: ClausterConfig, require_elevated: Callable[..., None]
) -> None:
    """Fail-closed gate for the Tier-B "Advanced" config surface (#978).

    404 when config-write is disabled (invisible surface, like the reaper), then
    403 ``reauth_required`` until the caller has re-proved the password via
    ``POST /api/reauth``. Ordered so a disabled surface never advertises itself
    with a 403.
    """
    if not config.config_write.enabled:
        raise HTTPException(status_code=404, detail="config-write is disabled")
    require_elevated(request)


@router.get("/api/config/advanced")
async def api_config_advanced_get(
    request: Request, config: ConfigDep, require_elevated: RequireElevatedDep
) -> dict:
    """Tier-B config values + a content hash, behind config-write + step-up (#978).

    Mirrors ``GET /api/config`` but over :data:`~clauster.config_editor.TIER_B_FIELDS`
    only — the operational-but-sensitive ``clone.*`` / ``webhooks.*`` scalars plus
    their non-secret list fields (allowed schemes/CIDRs, webhook events). No
    Tier-C secret/bind/auth value is ever surfaced (structural redaction, same as
    Tier-A). Values are read from disk so they stay consistent with the hash and
    reflect what the next restart will load.
    """
    _require_advanced(request, config, require_elevated)
    path = config.source_path
    fields = None
    content_hash = None
    present = None
    if path is not None:
        fields = config_editor.editable_values_on_disk(path, fields=config_editor.TIER_B_FIELDS)
        content_hash, present = config_editor.disk_state(path, fields=config_editor.TIER_B_FIELDS)
    if fields is None:
        fields = config_editor.editable_values(config, fields=config_editor.TIER_B_FIELDS)
    specs = config_editor.field_specs(present, fields=config_editor.TIER_B_FIELDS, config=config)
    editable = [p for p in config_editor.TIER_B_FIELDS if not specs[p]["hidden"]]
    return {
        "fields": fields,
        "editable": editable,
        "specs": specs,
        "hash": content_hash,
    }


@router.put("/api/config/advanced")
async def api_config_advanced_put(
    request: Request, body: dict, config: ConfigDep, require_elevated: RequireElevatedDep
) -> dict:
    """Apply Tier-B config edits: capability + step-up gated, backup + atomic write (#978).

    Fail-closed order: capability (404 when off) → step-up (403 ``reauth_required``)
    → re-validate against TIER_B **only** (a Tier-A or Tier-C key is a 400, never a
    silent drop) → external-edit hash guard (409) → backup + atomic write. Does not
    live-reload; the response flags a restart is needed. Records an audit line with
    the touched key NAMES only (never values).
    """
    _require_advanced(request, config, require_elevated)
    path = config.source_path
    if path is None:
        raise HTTPException(status_code=409, detail="config has no on-disk source to edit")
    edits = body.get("edits")
    if not isinstance(edits, dict) or not edits:
        raise HTTPException(status_code=422, detail="body must include a non-empty 'edits' map")
    expected = body.get("hash")
    if not isinstance(expected, str) or not expected:
        raise HTTPException(
            status_code=422,
            detail="body must include the 'hash' from GET /api/config/advanced",
        )
    try:
        new_hash = await asyncio.to_thread(
            config_writer.write_edits,
            path,
            edits,
            expected_hash=expected,
            allowed=frozenset(config_editor.TIER_B_FIELDS),
        )
    except config_editor.DisallowedFieldError as exc:
        raise HTTPException(status_code=400, detail=f"not editable: {exc}") from exc
    except config_editor.StaleConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except config_editor.ConfigValidationError as exc:
        raise HTTPException(status_code=422, detail=f"invalid config: {exc}") from exc
    await config_audit.arecord(
        config.state_dir,
        surface="config-advanced",
        scope="global",
        target=str(path),
        action="edit",
        actor=_SESSION_USER,
        keys=sorted(edits),
    )
    return {"hash": new_hash, "restart_required": True}


@router.post("/api/restart", status_code=202)
async def api_restart(request: Request) -> dict:
    """Restart Clauster in place to apply a saved config change (#483).

    Re-exec mechanism (``os.execv``): uniform across systemd/launchd/terminal/
    Docker, needs no unit change, and reloads config (read at startup). The same-PID
    guarantee is POSIX-only — on Windows execv is emulated as spawn-then-exit, so the
    PID changes and a Shawl-managed service restarts on that exit (#914). The unit is
    never stopped and child processes are untouched either way:
    ``runner.shutdown()`` leaves bridges running and
    ``hosted.aclose()`` detaches (not stops) hosted sessions, so clauster-managed
    sessions (standard + pty bridges + browser/hosted) survive the swap and are
    reattached on startup (#663). Only in-flight HTTP/WS connections drop during the
    re-bind window; the UI polls ``/healthz`` and reloads once the new image binds.

    Fail-closed: auth-gated by the guard middleware like every ``/api/*`` route, and
    a 503 (not a half-restarted process) if no live server is wired to shut down.
    Returns 202 **before** the swap so the client gets a response; the server then
    shuts down gracefully (releasing the socket) and ``_run`` re-execs.
    """
    server = getattr(request.app.state, "uvicorn_server", None)
    if server is None:
        raise HTTPException(
            status_code=503, detail="no live server to restart (not running under uvicorn)"
        )
    # The flag is read in ``_run`` AFTER serve() returns. `should_exit` only takes
    # effect once this handler's response has flushed (uvicorn checks it in its main
    # loop), so the 202 reaches the client before the swap.
    request.app.state.restart_requested = True
    server.should_exit = True
    return {"restarting": True}
