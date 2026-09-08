"""The config-write permission-rules routes (#1156).

``GET``/``PUT /api/config-write/permissions`` read and replace the permission-rules
block for a surface, behind the same fail-closed gate order as the other
config-write routes: capability first (404, invisible surface), then scope-enum,
then path containment. ``bypassPermissions`` can never be set here -- the validator
rejects it as a ``defaultMode`` (422), keeping it behind the footgun gate.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from ... import config_write, config_write_permissions
from ...dependencies import ConfigDep, RunnerOrNoneDep
from . import _base

router = APIRouter()


@router.get("/api/config-write/permissions")
async def api_config_write_permissions_read(
    config: ConfigDep, runner: RunnerOrNoneDep, scope: str = "project", project: str = ""
) -> dict:
    """Return the permission-rules block for a surface, 404 when the surface is gated off."""
    # Gated exactly like the MCP/status routes: 404 when config-write is off, and 404
    # for user scope when allow_user_scope is off — the surface is invisible, never
    # 403. A corrupt/non-object on-disk
    # settings.json raises InvalidCandidateError from _load_json_obj; map it through the
    # same helper as the PUT route so a hand-edited file is a clean 422, never a 500.
    # Capability gate FIRST, before the scope-enum check, so a disabled surface 404s
    # for ANY request (a bogus scope included), never a differing 422 (#819/#768).
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    if scope == "user":
        try:
            permissions, file_hash = await asyncio.to_thread(
                config_write_permissions.read_user_permissions, _base.user_settings_json(runner)
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {"scope": "user", "permissions": permissions, "hash": file_hash}
    if scope == "local":
        project_dir = _base.resolve_cw_project(config, project)
        try:
            permissions, file_hash = await asyncio.to_thread(
                config_write_permissions.read_project_local_permissions, project_dir
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {
            "scope": "local",
            "project": project,
            "permissions": permissions,
            "hash": file_hash,
        }
    project_dir = _base.resolve_cw_project(config, project)
    try:
        permissions, file_hash = await asyncio.to_thread(
            config_write_permissions.read_project_permissions, project_dir
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    return {
        "scope": "project",
        "project": project,
        "permissions": permissions,
        "hash": file_hash,
    }


@router.put("/api/config-write/permissions")
async def api_config_write_permissions_write(
    config: ConfigDep, runner: RunnerOrNoneDep, body: dict
) -> dict:
    """Replace the permission-rules block for a surface."""
    # bypassPermissions can never be set here: the validator rejects it as a
    # defaultMode (422), keeping it behind the footgun gate.
    return await _base.put_config_write(
        config,
        body,
        "permissions",
        surface="permissions",
        write_user_fn=config_write_permissions.write_user_permissions,
        write_project_fn=config_write_permissions.write_project_permissions,
        write_local_fn=config_write_permissions.write_project_local_permissions,
        get_user_path=lambda: _base.user_settings_json(runner),
    )
