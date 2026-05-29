"""FastAPI application factory (spec §7)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2_fragments.fastapi import Jinja2Blocks

from . import __version__, claude_cli
from .config import ClausterConfig
from .discovery import discover_projects
from .models import Project

_PKG_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PKG_DIR / "templates"
_STATIC_DIR = _PKG_DIR / "static"


def create_app(config: ClausterConfig) -> FastAPI:
    app = FastAPI(
        title="Clauster",
        version=__version__,
        root_path=config.root_path,
    )
    app.state.config = config
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
            "instances_running": 0,  # SessionRunner lands in a later sprint
        }

    @app.get("/api/projects")
    async def api_projects() -> list[Project]:
        return await list_projects()

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
