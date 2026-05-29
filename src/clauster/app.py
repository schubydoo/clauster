"""FastAPI application factory (spec §7)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2_fragments.fastapi import Jinja2Blocks

from . import __version__, claude_cli
from .config import ClausterConfig
from .discovery import discover_projects
from .models import Project, RemoteControlInstance
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
