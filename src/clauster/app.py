"""FastAPI application factory (spec §7)."""

from __future__ import annotations

import asyncio
import io
import time
from contextlib import asynccontextmanager
from pathlib import Path

import segno
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2_fragments.fastapi import Jinja2Blocks

from . import __version__, auth, claude_cli, logstream
from .config import ClausterConfig
from .discovery import discover_projects
from .models import Project, RemoteControlInstance, WorkingSession
from .redact import sanitize_line
from .runner import SessionRunner, SpawnError, UnknownProject

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
        if not ip:
            return True
        now = time.monotonic()
        recent = [t for t in self._failures.get(ip, []) if now - t < self._window]
        self._failures[ip] = recent
        return len(recent) < self._max

    def record_failure(self, ip: str | None) -> None:
        if ip:
            self._failures.setdefault(ip, []).append(time.monotonic())

    def reset(self, ip: str | None) -> None:
        self._failures.pop(ip, None)

_PKG_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PKG_DIR / "templates"
_STATIC_DIR = _PKG_DIR / "static"


def create_app(config: ClausterConfig, runner: SessionRunner | None = None) -> FastAPI:
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

    def _authenticate(scope) -> tuple[str | None, bool]:
        """Return (user, via_proxy). Works for both Request and WebSocket
        (both expose .headers/.cookies/.client/.url)."""
        rp = config.auth.reverse_proxy
        if rp.enabled and auth.peer_trusted(auth.peer_ip(scope), rp.trusted_ips):
            remote_user = scope.headers.get(rp.user_header)
            sig = scope.headers.get(rp.shared_secret_header)
            method = getattr(scope, "method", "GET")  # WS handshake => GET
            if auth.verify_proxy_hmac(
                rp.shared_secret, sig, remote_user, method, scope.url.path, rp.hmac_window_seconds
            ):
                return remote_user, True
        user = auth.read_session(
            _serializer, scope.cookies.get(_SESSION_COOKIE), config.auth.session_max_age_seconds
        )
        return (user, False) if user else (None, False)

    def _origin_allowed(request: Request) -> bool:
        for header in ("origin", "referer"):
            value = request.headers.get(header)
            if value:
                return auth.normalize_origin(value) in _allowed_origins
        return False  # no Origin/Referer on a state-changing request => reject

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
        ip = auth.peer_ip(request)
        if not _throttle.allowed(ip):
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Too many attempts — wait a few minutes."}, status_code=429,
            )
        form = await request.form()
        if auth.verify_password(_hasher, config.auth.password_hash, str(form.get("password", ""))):
            _throttle.reset(ip)
            resp = RedirectResponse(f"{_root}/", status_code=303)
            resp.set_cookie(
                _SESSION_COOKIE, auth.issue_session(_serializer, _SESSION_USER),
                max_age=config.auth.session_max_age_seconds, httponly=True,
                samesite="lax", secure=_cookie_secure(request), path=_root or "/",
            )
            return resp
        _throttle.record_failure(ip)
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect password."}, status_code=401
        )

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        resp = RedirectResponse(f"{_root}/login", status_code=303)
        resp.delete_cookie(_SESSION_COOKIE, path=_root or "/")
        return resp

    async def list_projects() -> list[Project]:
        return await asyncio.to_thread(discover_projects, config.projects_root)

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

    @app.get("/api/instances")
    async def api_instances() -> list[RemoteControlInstance]:
        return runner.list_instances()

    @app.get("/api/sessions")
    async def api_sessions() -> dict[str, list[WorkingSession]]:
        """External (unmanaged) working sessions grouped by project name (bug #4)."""
        return runner.external_sessions_by_project()

    @app.post("/api/instances", status_code=201)
    async def api_spawn(body: dict) -> RemoteControlInstance:
        project = body.get("project")
        if not isinstance(project, str) or not project:
            raise HTTPException(status_code=422, detail="body must include a 'project' string")
        try:
            return await runner.spawn(project)
        except UnknownProject as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SpawnError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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

    @app.post("/api/projects/{name}/trust")
    async def api_trust(name: str) -> Project:
        try:
            return await runner.trust_project(name)
        except UnknownProject as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
            await websocket.close(code=1008)  # validate before accept — never open an unauthed socket
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
    async def dashboard(request: Request) -> HTMLResponse:
        projects = await list_projects()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "projects": projects,
                "version": __version__,
                "projects_root": str(config.projects_root),
                "auth_enabled": config.auth.enabled,
            },
        )

    return app
