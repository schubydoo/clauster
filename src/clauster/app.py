"""FastAPI application factory (spec §7)."""

from __future__ import annotations

import asyncio
import io
from contextlib import asynccontextmanager
from pathlib import Path

import segno
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2_fragments.fastapi import Jinja2Blocks

from . import __version__, claude_cli, logstream
from .config import ClausterConfig
from .discovery import discover_projects
from .models import Project, RemoteControlInstance, WorkingSession
from .redact import sanitize_line
from .runner import SessionRunner, SpawnError, UnknownProject

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

    async def list_projects() -> list[Project]:
        return await asyncio.to_thread(discover_projects, config.projects_root)

    @app.get("/healthz")
    async def healthz() -> dict:
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

    @app.websocket("/ws/bridge-log/{instance_id}")
    async def ws_bridge_log(websocket: WebSocket, instance_id: str) -> None:
        """Tail the bridge debug log — ANSI-stripped and ID-redacted (feature 6, D11)."""
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
            },
        )

    return app
