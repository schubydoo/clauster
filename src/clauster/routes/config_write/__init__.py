"""The config-write A route package: status, MCP, permissions, hooks (#1156).

The code-executing config-write trust tier, split out of ``clauster.app.create_app``
in two sequential PRs. This package holds config-write A (status, the MCP surface,
permissions, and hooks); config-write B (claude-md, subagents, skills, settings,
plugins, marketplaces) still lives in :mod:`clauster.app` and calls the shared
pipeline in :mod:`._base` through thin wrappers, until it moves here too.

``router`` aggregates every area module's router with no prefix, so each route keeps
the full path it declares. ``clauster.app.create_app`` includes this one router; the
FastAPI 0.141 lazy-include wrapper is walked by ``app._iter_api_routes`` (which also
raises on any prefix), so the nested includes still show up in the golden route-table
snapshot verbatim.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import hooks, mcp, permissions, status

router = APIRouter()
router.include_router(status.router)
router.include_router(mcp.router)
router.include_router(permissions.router)
router.include_router(hooks.router)
