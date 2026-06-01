"""FastAPI application factory (spec §7)."""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from pathlib import Path

import segno
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2_fragments.fastapi import Jinja2Blocks

from . import __version__, auth, claude_cli, environments, logstream, usage
from .claude_md import (
    ClaudeMdConflict,
    ClaudeMdError,
    ClaudeMdNotTrusted,
    ClaudeMdTooLarge,
    read_claude_md,
    write_claude_md,
)
from .config import ClausterConfig
from .discovery import discover_projects, is_valid_project_name
from .models import (
    ClaudeMdDoc,
    InstanceStatus,
    Project,
    RemoteControlInstance,
    WorkingSession,
)
from .provisioning import (
    BlockedCloneHost,
    CloneFailed,
    GitUnavailable,
    InvalidCloneUrl,
    InvalidProjectName,
    ProvisionError,
    TargetExists,
    clone_project,
    create_project,
)
from .redact import sanitize_line
from .runner import (
    InvalidSpawnOption,
    PermissionModeNotAllowed,
    SessionRunner,
    SpawnError,
    UnknownProject,
)

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
        try:
            yield
        finally:
            await runner.shutdown()  # cancel poll task; leave bridges running

    app = FastAPI(
        title="Clauster",
        version=__version__,
        root_path=config.root_path,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.runner = runner
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
        return {
            "status": "ok",
            "version": __version__,
            "claude_ok": claude_ok,
            "claude_version": version,
            "instances_running": runner.running_count(),
        }

    @app.get("/api/projects")
    async def api_projects() -> list[Project]:
        return await list_projects()

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

    @app.post("/api/projects/clone", status_code=201)
    async def api_clone_project(body: dict) -> Project:
        if not config.clone.enabled:
            raise HTTPException(status_code=403, detail="clone is disabled in config")
        name = body.get("name")
        url = body.get("url")
        if not isinstance(name, str) or not name:
            raise HTTPException(status_code=422, detail="body must include a 'name' string")
        if not isinstance(url, str) or not url:
            raise HTTPException(status_code=422, detail="body must include a 'url' string")
        shallow = bool(body.get("shallow", False))
        try:
            await asyncio.to_thread(
                clone_project,
                config.projects_root,
                name,
                url,
                cfg=config.clone,
                shallow=shallow,
            )
        except InvalidProjectName as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except TargetExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidCloneUrl as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except BlockedCloneHost as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except GitUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except CloneFailed as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ProvisionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await _project_by_name(name)

    @app.get("/api/instances")
    async def api_instances() -> list[RemoteControlInstance]:
        return runner.list_instances()

    @app.get("/api/sessions")
    async def api_sessions() -> dict[str, list[WorkingSession]]:
        """External (unmanaged) working sessions grouped by project name (bug #4)."""
        return runner.external_sessions_by_project()

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
        for field, value in (
            ("spawn_mode", spawn_mode),
            ("permission_mode", permission_mode),
        ):
            if value is not None and not isinstance(value, str):
                raise HTTPException(status_code=422, detail=f"{field} must be a string")
        return await _spawn_or_http(
            runner.spawn(project, spawn_mode=spawn_mode, permission_mode=permission_mode)
        )

    @app.get("/api/instances/{instance_id}")
    async def api_instance(instance_id: str) -> RemoteControlInstance:
        instance = runner.get_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail=f"no such instance: {instance_id}")
        return instance

    @app.delete("/api/instances/{instance_id}")
    async def api_stop(instance_id: str) -> RemoteControlInstance:
        try:
            return await runner.stop(instance_id)
        except UnknownProject as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/instances/{instance_id}/resume")
    async def api_resume(instance_id: str) -> RemoteControlInstance:
        """Re-spawn a stopped/crashed bridge, reconnecting to its prior session.

        Reuses the bridge's stored spawn/permission modes (so resume keeps the same
        permission mode rather than dropping to the default).
        """
        return await _spawn_or_http(runner.resume(instance_id))

    @app.post("/api/projects/{name}/trust")
    async def api_trust(name: str) -> Project:
        try:
            return await runner.trust_project(name)
        except UnknownProject as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
        path = instance.bridge_debug_log_path
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
            },
        )

    return app
