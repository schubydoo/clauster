"""FastAPI application factory (spec §7)."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import time
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from pathlib import Path

import segno
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2_fragments.fastapi import Jinja2Blocks

from . import (
    __version__,
    auth,
    claude_cli,
    environments,
    logstream,
    metrics,
    ops,
    procutil,
    prometheus,
    supervisor,
    usage,
)
from .claude_md import (
    ClaudeMdConflict,
    ClaudeMdError,
    ClaudeMdNotTrusted,
    ClaudeMdTooLarge,
    read_claude_md,
    write_claude_md,
)
from .claustrum_client import ClaustrumError
from .claustrum_daemon import ClaustrumDaemon
from .clone_jobs import CloneJob, CloneJobManager
from .config import ClausterConfig
from .discovery import discover_projects, is_valid_project_name
from .hosted import HostedManager, HostedSessionError
from .hosted_state import HostedStateStore
from .models import (
    BackgroundJob,
    ClaudeMdDoc,
    InstanceStatus,
    Project,
    RemoteControlInstance,
    WorkingSession,
)
from .provisioning import (
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
from .redact import sanitize_line
from .runner import (
    InvalidSpawnOption,
    PermissionModeNotAllowed,
    SessionRunner,
    SpawnError,
    UnknownProject,
)
from .trust import is_trusted

logger = logging.getLogger(__name__)

_SESSION_COOKIE = "clauster_session"
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SESSION_USER = "admin"  # single-user in v0.2; multi-user is v0.3


class LoginThrottle:
    """In-process per-IP failed-login limiter — cheap brute-force resistance."""

    def __init__(self, max_failures: int = 5, window_seconds: int = 300) -> None:
        self._max = max_failures
        self._window = window_seconds
        self._failures: dict[str, list[float]] = {}

    def allowed(self, ip: str | None) -> bool:
        """Whether ``ip`` is under the failure limit within the rolling window."""
        if not ip:
            return True
        now = time.monotonic()
        recent = [t for t in self._failures.get(ip, []) if now - t < self._window]
        self._failures[ip] = recent
        return len(recent) < self._max

    def record_failure(self, ip: str | None) -> None:
        """Record one failed login attempt from ``ip``."""
        if ip:
            self._failures.setdefault(ip, []).append(time.monotonic())

    def reset(self, ip: str | None) -> None:
        """Clear ``ip``'s recorded failures (called on a successful login)."""
        if ip is not None:
            self._failures.pop(ip, None)


_PKG_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PKG_DIR / "templates"
_STATIC_DIR = _PKG_DIR / "static"


def create_app(config: ClausterConfig, runner: SessionRunner | None = None) -> FastAPI:
    """Build and wire the FastAPI app (routes, middleware, static, bridge poll loop)."""
    runner = runner or SessionRunner(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runner.start_poll_loop()  # rediscover running bridges + begin polling
        if config.claustrum.enabled:
            daemon = ClaustrumDaemon(config)
            app.state.claustrum_daemon = daemon
            try:
                await daemon.ensure()  # connect-or-spawn the hosted-channel daemon
                # CL-6: reattach hosted sessions that kept running on the daemon
                # while we were down. Best-effort — a reattach failure is recorded
                # per-session, never blocks startup. Skip if the daemon came up
                # without a live client (nothing to reattach through).
                if daemon.client is not None:
                    await app.state.hosted.reattach_all(daemon.client)
            except ClaustrumError as exc:
                # Fail-closed: the daemon's health carries the error and hosted
                # spawns are refused, but bridges (and startup) are unaffected.
                logger.warning("claustrum daemon unavailable at startup: %s", exc)
        try:
            yield
        finally:
            await app.state.hosted.aclose()  # stop live hosted sessions
            daemon = getattr(app.state, "claustrum_daemon", None)
            if daemon is not None:
                await daemon.aclose()  # drop our connection; leave the daemon running
            await runner.shutdown()  # cancel poll task; leave bridges running

    app = FastAPI(
        title="Clauster",
        version=__version__,
        root_path=config.root_path,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.runner = runner
    app.state.claustrum_daemon = None  # set by lifespan when claustrum.enabled
    # Hosted-channel sessions (CL-4); always present. The store (CL-6) persists them
    # so a clauster restart can reattach the survivors via lifespan reattach_all.
    app.state.hosted = HostedManager(HostedStateStore(config.state_dir))
    clone_jobs = CloneJobManager()
    app.state.clone_jobs = clone_jobs
    # Drop a finished clone job after this grace so a client that disconnected
    # mid-clone can reconnect and still read the terminal frame.
    _CLONE_JOB_TTL = 60.0
    # Hold strong refs to in-flight clone tasks so they aren't GC'd mid-run.
    _clone_tasks: set[asyncio.Task] = set()
    templates = Jinja2Blocks(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ----- auth context (v0.2 foundation, D12/D13) ------------------------
    _root = config.root_path
    _serializer = auth.make_serializer(auth.load_or_create_secret(config.state_dir))
    _hasher = auth.make_hasher()
    _allowed_origins = auth.build_allowed_origins(config)
    _throttle = LoginThrottle()
    # Session epoch: cookies embed it at issue; logout bumps it so every
    # outstanding cookie (incl. a captured one) is revoked. Persisted, so a
    # restart doesn't silently un-revoke. Cached on app.state — single uvicorn
    # worker, so the in-memory value is authoritative.
    app.state.session_epoch = auth.read_epoch(config.state_dir)

    def _authenticate(scope) -> tuple[str | None, bool]:
        """Return (user, via_proxy) for the request/connection.

        Works for both Request and WebSocket (both expose
        .headers/.cookies/.client/.url).
        """
        rp = config.auth.reverse_proxy
        if rp.enabled and auth.peer_trusted(auth.peer_ip(scope), rp.trusted_ips):
            remote_user = scope.headers.get(rp.user_header)
            sig = scope.headers.get(rp.shared_secret_header)
            method = getattr(scope, "method", "GET")  # WS handshake => GET
            if auth.verify_proxy_hmac(
                rp.shared_secret,
                sig,
                remote_user,
                method,
                scope.url.path,
                rp.hmac_window_seconds,
            ):
                return remote_user, True
        user = auth.read_session(
            _serializer,
            scope.cookies.get(_SESSION_COOKIE),
            config.auth.session_max_age_seconds,
            current_epoch=app.state.session_epoch,
        )
        return (user, False) if user else (None, False)

    def _origin_allowed(request: Request) -> bool:
        # Origin only: Referer is spoofable/suppressible (Referrer-Policy, downgrades)
        # so it's not trusted for CSRF. Modern browsers always send Origin on a
        # state-changing fetch/XHR/form POST; its absence => reject.
        origin = request.headers.get("origin")
        if origin is None:
            return False
        return auth.normalize_origin(origin) in _allowed_origins

    def _cookie_secure(request: Request) -> bool:
        mode = config.auth.cookie_secure
        if mode != "auto":
            return mode == "always"
        if request.url.scheme == "https":
            return True
        rp = config.auth.reverse_proxy
        if rp.enabled and auth.peer_trusted(auth.peer_ip(request), rp.trusted_ips):
            return request.headers.get("x-forwarded-proto", "").lower() == "https"
        return False

    def _throttle_key(request: Request) -> str | None:
        # Behind a trusted reverse proxy every login shares the proxy's socket IP,
        # so a per-IP limiter becomes global (one attacker locks everyone out).
        # Key on the proxy-asserted user instead — the trusted proxy overwrites
        # this header, so a client can't forge it. NB: with proxy auth configured,
        # password login is best kept loopback-only; this just hardens the overlap.
        rp = config.auth.reverse_proxy
        ip = auth.peer_ip(request)
        if rp.enabled and auth.peer_trusted(ip, rp.trusted_ips):
            user = request.headers.get(rp.user_header)
            if user:
                # Namespaced so a proxy user can't collide with a raw IP key. This
                # value only ever keys the rate limiter, never an HTTP response, so
                # semgrep's flask format-string-response rule is a false positive
                # on this non-route helper (bare nosemgrep: the line trips nothing
                # else, and the precise rule id overflows the line-length limit).
                return f"proxy-user:{user}"  # nosemgrep
        return ip

    def _is_public(path: str) -> bool:
        return path == "/healthz" or path == "/login" or path.startswith("/static/")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not config.auth.enabled:
            return await call_next(request)
        user, via_proxy = _authenticate(request)
        # CSRF: an unsafe method needs a trusted Origin — unless it's a proxy
        # request, whose HMAC is already bound to method+path.
        if request.method in _UNSAFE_METHODS and not via_proxy and not _origin_allowed(request):
            return JSONResponse({"detail": "origin check failed"}, status_code=403)
        if _is_public(request.url.path):
            return await call_next(request)
        if user is None:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "authentication required"}, status_code=401)
            return RedirectResponse(f"{_root}/login", status_code=303)
        return await call_next(request)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        if _authenticate(request)[0]:
            return RedirectResponse(f"{_root}/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        throttle_key = _throttle_key(request)
        if not _throttle.allowed(throttle_key):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Too many attempts — wait a few minutes."},
                status_code=429,
            )
        form = await request.form()
        if auth.verify_password(_hasher, config.auth.password_hash, str(form.get("password", ""))):
            _throttle.reset(throttle_key)
            resp = RedirectResponse(f"{_root}/", status_code=303)
            resp.set_cookie(
                _SESSION_COOKIE,
                auth.issue_session(_serializer, _SESSION_USER, app.state.session_epoch),
                max_age=config.auth.session_max_age_seconds,
                httponly=True,
                samesite="lax",
                secure=_cookie_secure(request),
                path=_root or "/",
            )
            return resp
        _throttle.record_failure(throttle_key)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect password."}, status_code=401
        )

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        # Bump the server-side epoch so the cookie we just dropped — and any
        # copy of it elsewhere — is actually revoked, not merely cleared client
        # side. Single-user today, so this is "log out everywhere".
        # Floor the bump against the in-memory epoch so a transient read error
        # or corrupt session.epoch can never lower it (which would un-revoke).
        app.state.session_epoch = await asyncio.to_thread(
            auth.bump_epoch, config.state_dir, app.state.session_epoch
        )
        resp = RedirectResponse(f"{_root}/login", status_code=303)
        resp.delete_cookie(_SESSION_COOKIE, path=_root or "/")
        return resp

    async def list_projects() -> list[Project]:
        projects = await asyncio.to_thread(discover_projects, config.projects_root)
        # Surface the config bypass-ceiling per project so the UI can gate the option
        # (discovery has no config knowledge; the app layer owns this).
        for p in projects:
            p.allow_bypass_permissions = config.allows_bypass(p.name)
        return projects

    @app.get("/healthz")
    async def healthz(request: Request) -> dict:
        # Unauthenticated callers get only liveness when auth is enabled — don't
        # leak claude version / running count on a public reverse-proxy deploy.
        if config.auth.enabled and _authenticate(request)[0] is None:
            return {"status": "ok"}
        try:
            version = await asyncio.to_thread(claude_cli.claude_version, config.claude.binary)
            claude_ok = True
        except Exception:
            version = None
            claude_ok = False
        result: dict[str, object] = {
            "status": "ok",
            "version": __version__,
            "claude_ok": claude_ok,
            "claude_version": version,
            "instances_running": runner.running_count(),
        }
        if config.claustrum.enabled:
            daemon = getattr(app.state, "claustrum_daemon", None)
            result["claustrum"] = (
                await daemon.probe() if daemon is not None else {"enabled": True, "running": False}
            )
        return result

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        # Stays behind the auth guard like every route; scraping a guarded deploy
        # needs auth/network handling (see the PR's follow-up note).
        if not config.observability.prometheus_enabled:
            raise HTTPException(status_code=404, detail="metrics endpoint is disabled")
        projects = await list_projects()
        body = prometheus.render_metrics(
            version=__version__,
            instances=runner.list_instances(),
            project_count=len(projects),
        )
        return Response(content=body, media_type=prometheus.CONTENT_TYPE)

    @app.get("/api/projects")
    async def api_projects() -> list[Project]:
        return await list_projects()

    @app.get("/api/doctor")
    async def api_doctor() -> dict:
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

    @app.get("/api/projects/{name}/preflight")
    async def api_project_preflight(name: str) -> dict:
        """Per-project spawn-readiness checks (the system-wide panel is ``/api/doctor``).

        Reports the two preconditions specific to *this* project's bridge launch —
        workspace trust and whether it's a git repo (worktree mode) — as the same
        ``{name, status, detail}`` shape the doctor panel consumes. Derived from the
        discovered project (so trust/git match the card); read-only and auth-gated.
        """
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail="invalid project name")
        proj = next((p for p in await list_projects() if p.name == name), None)
        if proj is None:
            raise HTTPException(status_code=404, detail=f"project {name!r} not found")
        checks = ops.project_preflight_checks(proj)
        return {
            "project": name,
            "ok": all(c.status != ops.FAIL for c in checks),
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
        }

    @app.get("/api/projects/{name}/card", response_class=HTMLResponse)
    async def api_project_card(request: Request, name: str) -> Response:
        """Render one project's card for reactive insertion (no full-page reload).

        Same Jinja partial the dashboard grid loops over — one source of truth.
        """
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail="invalid project name")
        proj = next((p for p in await list_projects() if p.name == name), None)
        if proj is None:
            raise HTTPException(status_code=404, detail=f"project {name!r} not found")
        # pty_supported gates the resume-mode <option>; the fragment render must
        # pass it too (same partial as the grid loop) or the picker vanishes.
        return templates.TemplateResponse(
            request,
            "_project_card.html",
            {"p": proj, "pty_supported": sys.platform != "win32"},
        )

    @app.get("/api/projects/{name}/usage")
    async def api_project_usage(name: str) -> dict:
        # Read-only cost/token rollup for the dashboard badge. We validate the name
        # for path-component safety but deliberately skip a discovery scan: an
        # unknown-but-safe name simply has no transcripts and rolls up to zero.
        # Transcripts can be huge, so the parse runs off the event loop.
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail="invalid project name")
        rollup = await asyncio.to_thread(
            usage.aggregate_project_usage,
            config.projects_root / name,
            project_name=name,
        )
        tot = rollup.totals
        return {
            "project": name,
            "transcripts": rollup.transcript_count,
            "messages": tot.messages,
            "total_tokens": tot.total_tokens,
            # Per-category split so the badge can show a cache-excluded total
            # (token_total_includes_cache) and a breakdown tooltip.
            "token_breakdown": {
                "input": tot.input,
                "output": tot.output,
                "cache_creation": tot.cache_creation,
                "cache_read": tot.cache_read,
            },
            "cost_usd": round(rollup.cost_usd(), 4),
            "approximate": True,  # hand-maintained price table; counts exact, $ ballpark
            "unpriced_models": rollup.unpriced_models(),
            "by_model": {
                model: {
                    "total_tokens": t.total_tokens,
                    "cost_usd": round(usage.cost_usd(model, t) or 0.0, 4),
                }
                for model, t in sorted(rollup.by_model.items())
            },
        }

    @app.get("/api/projects/{name}/metrics")
    async def api_project_metrics(name: str) -> dict:
        # Live CPU/memory/disk for a project's running bridge (dashboard badge).
        # Meaningful only while a bridge runs (and metrics are enabled); otherwise
        # {running: false}. The two-sample read runs off the event loop. No discovery
        # scan needed: an unknown name simply has no running instance.
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail="invalid project name")
        if not config.metrics.enabled:  # feature off → never sample, even on a direct hit
            return {"running": False}
        inst = runner.get_instance(name)
        if inst is None or inst.status is not InstanceStatus.RUNNING or inst.bridge_pid is None:
            return {"running": False}
        try:
            # Guard PID reuse since the last poll: if the live PID's start time no
            # longer matches the bridge we recorded, the OS recycled it onto an
            # unrelated process — don't attribute its metrics to this bridge.
            start = inst.bridge_proc_start
            if start is not None:
                cur = await asyncio.to_thread(procutil.proc_create_time, inst.bridge_pid)
                if cur is None or abs(cur - start) > 2.0:
                    return {"running": False}
            sample = await asyncio.to_thread(
                metrics.sample_tree,
                inst.bridge_pid,
                interval=config.metrics.sample_interval_seconds,
                normalize_cpu=config.metrics.normalize_cpu,
            )
        except Exception:  # fail closed — a sampling error must never 500 the endpoint
            return {"running": False}
        if sample is None:  # pid vanished between the status check and the sample
            return {"running": False}
        return {"running": True, **sample}

    # --- ghost-environment reaper (spec §11), dashboard surface ----------------
    # Destructive first-party API, so: opt-in config gate, fail-closed live set,
    # and every action re-derives the ghost set server-side (see _gather_ghosts).
    def _gather_ghosts() -> tuple[environments.EnvironmentsClient, list, set, list]:
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
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=f"refusing to reap — could not determine live bridges: {exc}",
            ) from exc
        return client, envs, live, environments.find_ghosts(envs, live)

    @app.get("/api/environments/ghosts")
    async def api_environment_ghosts() -> dict:
        if not config.reaper.ui_enabled:
            raise HTTPException(status_code=404, detail="reaper UI is disabled")

        def _work() -> dict:
            _client, envs, live, ghosts = _gather_ghosts()
            return {
                "enabled": True,
                "total": len(envs),
                "live_dirs": len(live),
                "ghosts": [
                    {"id": g.id, "directory": g.config.directory, "name": g.name} for g in ghosts
                ],
            }

        return await asyncio.to_thread(_work)

    @app.post("/api/environments/reap")
    async def api_environment_reap(body: dict) -> dict:
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
            client, _envs, _live, ghosts = _gather_ghosts()
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

    async def _project_by_name(name: str) -> Project:
        for proj in await list_projects():
            if proj.name == name:
                return proj
        raise HTTPException(status_code=500, detail=f"project {name!r} missing after provisioning")

    @app.post("/api/projects", status_code=201)
    async def api_create_project(body: dict) -> Project:
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
        return await _project_by_name(name)

    async def _run_clone(job: CloneJob, name: str, url: str, shallow: bool) -> None:
        """Clone in a worker thread, streaming progress into the job's queue."""
        loop = asyncio.get_running_loop()

        def _forward(line: str) -> None:
            loop.call_soon_threadsafe(clone_jobs.push_progress, job, line)

        try:
            await asyncio.to_thread(
                clone_project,
                config.projects_root,
                name,
                url,
                cfg=config.clone,
                shallow=shallow,
                progress_cb=_forward,
            )
        except ProvisionError as exc:
            clone_jobs.finish(job, error=str(exc))
        except Exception as exc:  # defensive: never leave a job stuck "running"
            clone_jobs.finish(job, error=f"unexpected error: {exc}")
        else:
            clone_jobs.finish(job)
        loop.call_later(_CLONE_JOB_TTL, clone_jobs.discard, job.id)

    @app.post("/api/projects/clone", status_code=202)
    async def api_clone_project(body: dict) -> dict:
        """Start an async clone; returns a job id to watch via ``/ws/clone-progress``."""
        if not config.clone.enabled:
            raise HTTPException(status_code=403, detail="clone is disabled in config")
        name = body.get("name")
        url = body.get("url")
        if not isinstance(name, str) or not name:
            raise HTTPException(status_code=422, detail="body must include a 'name' string")
        if not isinstance(url, str) or not url:
            raise HTTPException(status_code=422, detail="body must include a 'url' string")
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail=f"invalid project name: {name!r}")
        if (config.projects_root / name).exists():
            raise HTTPException(
                status_code=409, detail=f"a directory named {name!r} already exists"
            )
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
        task = asyncio.create_task(_run_clone(job, name, url, shallow))
        _clone_tasks.add(task)
        task.add_done_callback(_clone_tasks.discard)
        return {"job_id": job.id, "name": name}

    @app.get("/api/instances")
    async def api_instances() -> list[RemoteControlInstance]:
        return runner.list_instances()

    @app.get("/api/hosted")
    async def api_hosted() -> list[RemoteControlInstance]:
        """Hosted (claustrum stream-json) sessions, each status-synced to its live session.

        Kept separate from ``/api/instances`` (project-keyed bridges): hosted
        sessions live in their own ``HostedManager`` registry, are keyed by a
        client-chosen id, and there may be several per project. The dashboard's
        hosted panel polls this; empty list when the channel is unused. Auth-gated
        by the guard middleware like every other ``/api/*`` route.
        """
        instances = app.state.hosted.list_instances()
        # Debounced (no-op when unchanged): refresh the persisted reattach cursors on
        # the dashboard's poll cadence, so a restart replays from a recent daemon seq.
        await app.state.hosted.persist()
        return instances

    @app.get("/api/widget")
    async def api_widget() -> dict:
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
        projects = await list_projects()
        return {
            "projects_total": len(projects),
            "bridges": by_status,
            "running_total": by_status[InstanceStatus.RUNNING.value],
            "version": __version__,
        }

    @app.get("/api/sessions")
    async def api_sessions() -> dict[str, list[WorkingSession]]:
        """External (unmanaged) working sessions grouped by project name (bug #4)."""
        return runner.external_sessions_by_project()

    @app.get("/api/agents")
    async def api_agents() -> list[BackgroundJob]:
        """Agent-view background sessions (`claude --bg`), observed read-only.

        Sourced from the supervisor's docs-acknowledged on-disk state
        (``jobs/<id>/state.json`` + ``daemon/roster.json``) — no subprocess, no
        daemon protocol. Empty list when agent view is unused. Auth-gated by the
        guard middleware like every other ``/api/*`` route.
        """
        return await asyncio.to_thread(supervisor.list_background_jobs)

    @app.post("/api/agents", status_code=201)
    async def api_dispatch_agent(body: dict) -> dict:
        """Dispatch a `claude --bg` background session in a managed project.

        Validates the project name (same guard as the other project routes),
        pre-trusts the cwd, and fires `claude --bg [--rc <name>]`; returns the new
        job id. The bg-agents panel reflects its live state via `GET /api/agents`.

        Body: ``{project, prompt?, rc_name?, model?, permission_mode?}``. A
        ``rc_name`` opens the cloud door (a cloud-visible Remote Control session).
        NOTE: stop/teardown is a later slice — a dispatched `--rc` session must
        currently be stopped from the CLI (and a local stop only orphans the cloud
        registration), so no dispatch control is wired into the UI yet.
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
        rc_name = _opt_text("rc_name", empty_ok=False)
        model = _opt_text("model", empty_ok=False)
        permission_mode = _opt_text("permission_mode", empty_ok=False)
        cwd = config.projects_root / name
        if not await asyncio.to_thread(cwd.is_dir):
            raise HTTPException(status_code=404, detail=f"project {name!r} not found")
        try:
            job_id = await asyncio.to_thread(
                supervisor.dispatch_background_job,
                cwd,
                prompt=prompt,
                rc_name=rc_name,
                model=model,
                permission_mode=permission_mode,
                binary=config.claude.binary,
                claude_json=runner.claude_json,
            )
        except claude_cli.ClaudeNotFound as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except supervisor.DispatchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"id": job_id}

    @app.delete("/api/agents/{job_id}")
    async def api_stop_agent(job_id: str) -> dict:
        """Stop a `claude --bg` background session and remove its job, cleanly.

        Double-SIGINTs the session process so the CLI runs an orderly shutdown and
        deregisters its cloud bridge session (vs `claude stop`, which SIGKILLs and
        leaves a cloud orphan), then `claude rm`s the job. The job id is validated
        to the 8-hex short-id shape (also the `claude rm` argv-injection guard).

        Returns `{id, settled, removed, detail}`. A `settled:false` row that didn't
        exit in time is a 409 (escalate from the CLI — we don't force-kill, which
        would orphan the cloud session); `removed:false` (supervisor idle-exited)
        is reported in the body, not an error — the session is already stopped.
        """
        if not supervisor.valid_job_id(job_id):
            raise HTTPException(status_code=422, detail="invalid job id")
        try:
            return await asyncio.to_thread(
                supervisor.stop_background_job, job_id, binary=config.claude.binary
            )
        except claude_cli.ClaudeNotFound as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except supervisor.StopError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def _spawn_or_http(coro: Awaitable[RemoteControlInstance]) -> RemoteControlInstance:
        """Await a spawn/resume coroutine, mapping its exceptions to HTTP codes.

        Shared by the create and resume routes so the mapping lives in one place.
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

    @app.post("/api/instances", status_code=201)
    async def api_spawn(body: dict) -> RemoteControlInstance:
        project = body.get("project")
        if not isinstance(project, str) or not project:
            raise HTTPException(status_code=422, detail="body must include a 'project' string")
        spawn_mode = body.get("spawn_mode")
        permission_mode = body.get("permission_mode")
        resume_mode = body.get("resume_mode")
        channel = body.get("channel", "remote-control")
        for field, value in (
            ("spawn_mode", spawn_mode),
            ("permission_mode", permission_mode),
            ("resume_mode", resume_mode),
            ("channel", channel),
        ):
            if value is not None and not isinstance(value, str):
                raise HTTPException(status_code=422, detail=f"{field} must be a string")
        if channel == "hosted":
            return await _spawn_hosted(project, permission_mode)
        if channel != "remote-control":
            raise HTTPException(status_code=422, detail=f"unknown channel: {channel!r}")
        return await _spawn_or_http(
            runner.spawn(
                project,
                spawn_mode=spawn_mode,
                permission_mode=permission_mode,
                resume_mode=resume_mode,
            )
        )

    async def _hosted_prereqs(project: str) -> tuple[object, Path, str]:
        """Resolve (daemon client, trusted project path, claude binary) for a hosted op.

        Shared by hosted spawn and resume; raises the same HTTP errors the spawn path
        has always used (503 no daemon / 503 binary missing, 409 untrusted directory).
        """
        daemon = getattr(app.state, "claustrum_daemon", None)
        client = daemon.client if daemon is not None else None
        if client is None:
            raise HTTPException(
                status_code=503,
                detail="hosted channel unavailable: claustrum daemon not connected",
            )
        path = await _resolve_project_path(project)
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

    async def _spawn_hosted(project: str, permission_mode: str | None) -> RemoteControlInstance:
        """Start a hosted (claustrum stream-json) session for ``project``."""
        client, path, binary = await _hosted_prereqs(project)
        pm = permission_mode or config.instance_defaults.permission_mode
        try:
            return await app.state.hosted.spawn(
                client,
                project=project,
                label=f"hosted:{project}",
                cwd=str(path),
                claude_binary=binary,
                permission_mode=pm,
            )
        except ClaustrumError as exc:
            raise HTTPException(status_code=502, detail=f"hosted spawn failed: {exc}") from exc

    async def _resume_hosted(
        hosted_id: str, instance: RemoteControlInstance
    ) -> RemoteControlInstance:
        """Resume a lost/ended hosted session by id, respawning with ``--resume <uuid>``.

        ``instance`` is the row the route already fetched. Maps the engine's
        :class:`HostedSessionError` (unknown / still-running / no-uuid) to 409 and a
        daemon spawn failure to 502.
        """
        client, path, binary = await _hosted_prereqs(instance.project)
        try:
            return await app.state.hosted.resume(
                client, hosted_id, cwd=str(path), claude_binary=binary
            )
        except HostedSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ClaustrumError as exc:
            raise HTTPException(status_code=502, detail=f"hosted resume failed: {exc}") from exc

    @app.get("/api/instances/{instance_id}")
    async def api_instance(instance_id: str) -> RemoteControlInstance:
        instance = runner.get_instance(instance_id) or app.state.hosted.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail=f"no such instance: {instance_id}")
        return instance

    @app.post("/api/instances/{instance_id}/message", status_code=202)
    async def api_hosted_message(instance_id: str, body: dict) -> dict:
        """Send one user turn to a hosted session (the conversation input path)."""
        text = body.get("text")
        if not isinstance(text, str) or not text:
            raise HTTPException(
                status_code=422, detail="body must include a non-empty 'text' string"
            )
        if app.state.hosted.get_instance(instance_id) is None:
            raise HTTPException(status_code=404, detail=f"no such hosted session: {instance_id}")
        try:
            await app.state.hosted.send(instance_id, text)
        except HostedSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/instances/{instance_id}/permissions/{request_id}", status_code=202)
    async def api_hosted_permission(instance_id: str, request_id: str, body: dict) -> dict:
        """Answer a parked tool-permission request on a hosted session (CL-5).

        The engine parks every tool-permission ``control_request`` and waits
        (fail-closed) until something answers — this is that explicit human gate.
        ``decision`` is ``"allow"`` or ``"deny"`` (a deny may carry a short
        ``message``), mapped to the SDK ``can_use_tool`` response ``{"behavior": …}``.
        """
        decision = body.get("decision")
        if decision not in ("allow", "deny"):
            raise HTTPException(status_code=422, detail="decision must be 'allow' or 'deny'")
        if app.state.hosted.get_instance(instance_id) is None:
            raise HTTPException(status_code=404, detail=f"no such hosted session: {instance_id}")
        if decision == "allow":
            response: dict = {"behavior": "allow"}
        else:
            message = body.get("message")
            response = {
                "behavior": "deny",
                "message": message
                if isinstance(message, str) and message
                else "Denied by operator",
            }
        try:
            await app.state.hosted.respond(instance_id, request_id, response)
        except HostedSessionError as exc:
            # Already answered, or no such parked request — not in a state to answer.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.delete("/api/instances/{instance_id}")
    async def api_stop(instance_id: str) -> RemoteControlInstance:
        if app.state.hosted.get_instance(instance_id) is not None:
            return await app.state.hosted.stop(instance_id)
        try:
            return await runner.stop(instance_id)
        except UnknownProject as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/instances/{instance_id}/resume")
    async def api_resume(instance_id: str) -> RemoteControlInstance:
        """Re-spawn a stopped/crashed bridge or hosted session into its prior conversation.

        Bridges reuse their stored spawn/permission modes; a hosted session respawns
        a fresh daemon process with ``--resume <claude_session_uuid>`` (CL-7).
        """
        hosted = app.state.hosted.get_instance(instance_id)
        if hosted is not None:
            return await _resume_hosted(instance_id, hosted)
        return await _spawn_or_http(runner.resume(instance_id))

    @app.post("/api/projects/{name}/trust")
    async def api_trust(name: str) -> Project:
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

    async def _resolve_project_path(name: str) -> Path:
        """Map a project name to its path, refusing unknown/unsafe names (traversal)."""
        if not is_valid_project_name(name):
            raise HTTPException(status_code=404, detail=f"no such project: {name!r}")
        for proj in await list_projects():
            if proj.name == name:
                return proj.path
        raise HTTPException(status_code=404, detail=f"no such project: {name!r}")

    def _bridge_running(name: str) -> bool:
        inst = runner.get_instance(name)
        if inst is not None and inst.status is InstanceStatus.RUNNING:
            return True
        return name in runner.external_sessions_by_project()

    @app.get("/api/projects/{name}/claude-md")
    async def api_claude_md_get(name: str) -> ClaudeMdDoc:
        path = await _resolve_project_path(name)
        try:
            doc = await asyncio.to_thread(read_claude_md, path)
        except ClaudeMdError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        doc.bridge_running = _bridge_running(name)
        return doc

    @app.put("/api/projects/{name}/claude-md")
    async def api_claude_md_put(name: str, body: dict) -> ClaudeMdDoc:
        path = await _resolve_project_path(name)
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
        doc.bridge_running = _bridge_running(name)
        return doc

    @app.get("/api/instances/{instance_id}/qr")
    async def api_instance_qr(instance_id: str) -> Response:
        """SVG QR for the primary deep link (feature 5) — scan to open on mobile."""
        instance = runner.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail=f"no such instance: {instance_id}")
        target = instance.session_url or instance.url
        if not target:
            raise HTTPException(status_code=409, detail="no session URL available yet")
        buf = io.BytesIO()
        segno.make(target, error="m").save(buf, kind="svg", scale=4, border=2)
        return Response(content=buf.getvalue(), media_type="image/svg+xml")

    def _ws_authorized(websocket: WebSocket) -> bool:
        """Strict Origin check + session/proxy auth, BEFORE accepting (D12)."""
        origin = websocket.headers.get("origin")
        if not origin or auth.normalize_origin(origin) not in _allowed_origins:
            return False  # cross-site WS hijack defense; browsers always send Origin
        return _authenticate(websocket)[0] is not None

    @app.websocket("/ws/bridge-log/{instance_id}")
    async def ws_bridge_log(websocket: WebSocket, instance_id: str) -> None:
        """Tail the bridge debug log — ANSI-stripped and ID-redacted (feature 6, D11)."""
        if config.auth.enabled and not _ws_authorized(websocket):
            await websocket.close(
                code=1008
            )  # validate before accept — never open an unauthed socket
            return
        await websocket.accept()
        instance = runner.get_instance(instance_id)
        if instance is None or instance.bridge_debug_log_path is None:
            await websocket.close(code=1008)  # nothing to stream
            return
        # Stream the verbatim raw parse-source (== the debug log unless on-disk
        # redaction split it off), sanitizing each line in-flight as always — so the
        # live stream stays current regardless of the at-rest mirror's refresh cadence.
        path = instance.bridge_raw_log_path or instance.bridge_debug_log_path
        strip = config.logs.strip_ansi_in_stream
        offset = await asyncio.to_thread(logstream.initial_offset, path)
        carry = ""
        try:
            while True:
                offset, text = await asyncio.to_thread(logstream.read_new, path, offset)
                if text:
                    # Buffer whole lines so redaction never misses an id split
                    # across two reads.
                    *lines, carry = (carry + text).split("\n")
                    for line in lines:
                        await websocket.send_text(sanitize_line(line, strip_ansi_seq=strip))
                await asyncio.sleep(0.5)
        except (WebSocketDisconnect, RuntimeError):
            return

    @app.websocket("/ws/hosted/{instance_id}")
    async def ws_hosted(websocket: WebSocket, instance_id: str) -> None:
        """Stream a hosted session's live events, replaying the ring past ``?after=``."""
        if config.auth.enabled and not _ws_authorized(websocket):
            await websocket.close(code=1008)  # validate before accept
            return
        await websocket.accept()
        session = app.state.hosted.session(instance_id)
        if session is None:
            await websocket.close(code=1008)  # unknown / already gone
            return
        try:
            after = int(websocket.query_params.get("after", "0"))
        except (TypeError, ValueError):
            after = 0
        queue = session.subscribe(after_seq=after)
        try:
            while True:
                await websocket.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            session.unsubscribe(queue)

    @app.websocket("/ws/clone-progress/{job_id}")
    async def ws_clone_progress(websocket: WebSocket, job_id: str) -> None:
        """Stream a clone job's ``{phase, percent}`` progress, then a terminal frame."""
        if config.auth.enabled and not _ws_authorized(websocket):
            await websocket.close(code=1008)  # validate before accept
            return
        await websocket.accept()
        job = clone_jobs.get(job_id)
        if job is None:
            await websocket.close(code=1008)  # unknown / already pruned
            return
        # Subscribe before the status check so a terminal that fires between the
        # check and the snapshot send below lands in our queue, not the void.
        queue = job.subscribe()
        try:
            if job.status != "running":
                # Already finished (e.g. a reconnect after completion).
                await websocket.send_json(job.terminal_event())
                return
            await websocket.send_json(job.progress_event())  # current snapshot
            while True:
                event = await queue.get()
                await websocket.send_json(event)
                if event.get("type") == "done":
                    break
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            job.unsubscribe(queue)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        projects = await list_projects()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "projects": projects,
                "version": __version__,
                "projects_root": str(config.projects_root),
                "auth_enabled": config.auth.enabled,
                "reaper_ui_enabled": config.reaper.ui_enabled,
                "default_spawn_mode": config.instance_defaults.spawn_mode,
                "default_permission_mode": config.instance_defaults.permission_mode,
                "default_resume_mode": config.claude.resume_mode,
                # pty (true-resume) is POSIX-only; hide the option on Windows hosts.
                "pty_supported": sys.platform != "win32",
                # Usage badge: mode ("cost"|"tokens"|"off"), the currency code + its
                # resolved symbol, the static USD->display multiplier, and whether
                # cache tokens count toward the displayed token total. mode "off"
                # hides the badge and skips the per-project /usage fetch.
                "usage_mode": config.usage.mode,
                "currency": config.usage.currency,
                "currency_symbol": config.usage.effective_symbol,
                "fx_rate": config.usage.fx_rate,
                "token_total_includes_cache": config.usage.token_total_includes_cache,
                # Live per-bridge metrics: master toggle, disk-part toggle, poll cadence.
                "metrics_enabled": config.metrics.enabled,
                "metrics_show_disk": config.metrics.show_disk,
                "metrics_poll_ms": int(config.metrics.poll_seconds * 1000),
                # Hosted channel (CL-4c): the live-view panel only renders when the
                # claustrum daemon is configured; otherwise there's nothing to host.
                "claustrum_enabled": config.claustrum.enabled,
            },
        )

    return app
