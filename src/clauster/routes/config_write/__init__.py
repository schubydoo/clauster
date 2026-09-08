"""The config-write route package: the whole code-executing config-write tier (#1156).

The code-executing config-write trust tier, split out of ``clauster.app.create_app``
in two sequential PRs. This package now holds config-write A (status, the MCP surface,
permissions, hooks) and config-write B (claude-md, subagents, skills, settings,
plugins, marketplaces); every handler reaches the shared pipeline in :mod:`._base`
directly. With B moved here, ``clauster.app.create_app`` holds no config-write route
handlers -- #1156 for this domain is complete.

``router`` aggregates every area module's router with no prefix, so each route keeps
the full path it declares. ``clauster.app.create_app`` includes this one router; the
FastAPI 0.141 lazy-include wrapper is walked by ``app._iter_api_routes`` (which also
raises on any prefix), so the nested includes still show up in the golden route-table
snapshot verbatim. Route order is load-bearing inside :mod:`.plugins`: the literal
``/plugins/enabled`` route is declared ahead of the ``/plugins/{plugin_id}`` parameter
route so the literal wins, and ``include_router`` preserves that source order. Do not
reorder those two.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    claude_md,
    hooks,
    marketplaces,
    mcp,
    permissions,
    plugins,
    settings,
    skills,
    status,
    subagents,
)

router = APIRouter()
router.include_router(status.router)
router.include_router(mcp.router)
router.include_router(permissions.router)
router.include_router(hooks.router)
router.include_router(claude_md.router)
router.include_router(subagents.router)
router.include_router(skills.router)
router.include_router(settings.router)
router.include_router(plugins.router)
router.include_router(marketplaces.router)
