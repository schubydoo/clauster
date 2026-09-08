"""Shared helpers used across the ``routes/*`` modules (#1523).

These helpers were duplicated across sibling route modules after the #1156
``create_app`` split (the bypass-ceiling gate) or lived in a domain module and were
imported cross-module by private name (project discovery and path resolution). They
move here so each has a single home and a public name, and every route module imports
from :mod:`clauster.routes._common`.

The security properties are unchanged by the move: the bypass decision is the single
:meth:`clauster.config.ClausterConfig.bypass_denied` (its
:meth:`~clauster.config.ClausterConfig.bypass_denied_detail` message shared), and the
traversal defense is the shared :func:`clauster.discovery.is_valid_project_name`, so no
caller can diverge on either check.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import HTTPException

from ..discovery import is_valid_project_name
from ..models import Project

if TYPE_CHECKING:
    from pathlib import Path

    from ..config import ClausterConfig
    from ..engine import ClausterEngine


def enforce_bypass_ceiling(
    config: ClausterConfig, project: str, permission_mode: str | None
) -> None:
    """Reject ``bypassPermissions`` when a project's config ceiling forbids it.

    The runner enforces this hard ceiling for the bridge channel
    (:class:`~clauster.runner.PermissionModeNotAllowed`, mapped to 403). The
    background-agent channel (``routes/agents.py``) and the hosted channel
    (``routes/instances.py``) spawn outside the runner, so each mirrors the gate through
    this shared helper or a crafted request could run a session in bypass mode that the
    project's ``allow_bypass_permissions`` ceiling forbids. Both defer to the single
    :meth:`ClausterConfig.bypass_denied` decision and share its
    :meth:`ClausterConfig.bypass_denied_detail` message, so neither the decision nor the
    wording can diverge.
    """
    if config.bypass_denied(project, permission_mode):
        raise HTTPException(status_code=403, detail=config.bypass_denied_detail(project))


async def list_projects(engine: ClausterEngine) -> list[Project]:
    """Return the discovered projects through the same facade the CLI uses."""
    # Shared facade (#775): the CLI and the routes go through the same
    # discover-then-stamp-bypass path, so the two can't drift.
    return await asyncio.to_thread(engine.list_projects)


async def resolve_project_path(name: str, engine: ClausterEngine) -> Path:
    """Map a project name to its path, refusing unknown/unsafe names (traversal).

    The shared project-path resolver for the ``routes/*`` modules (the hosted
    spawn/resume path in ``routes/instances.py`` and the ``CLAUDE.md`` routes in
    ``routes/projects.py``). The traversal defense itself is the shared
    :func:`is_valid_project_name`, so no caller can diverge on the security check. (The
    config-write routes resolve their own cwd via
    :func:`clauster.routes.config_write._base.resolve_cw_project` instead.)
    """
    if not is_valid_project_name(name):
        raise HTTPException(status_code=404, detail=f"project {name!r} not found")
    for proj in await list_projects(engine):
        if proj.name == name:
            return proj.path
    raise HTTPException(status_code=404, detail=f"project {name!r} not found")
