"""The config-write hooks routes (#1156).

``GET``/``PUT /api/config-write/hooks`` read and replace the stored hooks block for
a surface. SECURITY: hooks are shell commands claude runs on lifecycle events. The
structural validator NEVER resolves, spawns, or shell-parses a command string; it is
stored as inert data and only runs inside a real claude process. Reading never runs a
command. The off-by-default gate plus the validate-never-execute invariant are what
prevent a browser write from reaching host RCE.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from ... import config_write, config_write_hooks
from ...dependencies import ConfigDep, RunnerOrNoneDep
from . import _base

router = APIRouter()


@router.get("/api/config-write/hooks")
async def api_config_write_hooks_read(
    config: ConfigDep, runner: RunnerOrNoneDep, scope: str = "project", project: str = ""
) -> dict:
    """Return the stored (inert) hooks block for a surface; reading never runs a command."""
    # Gated exactly like the permissions/MCP/status routes: 404 when config-write is
    # off, and 404 for user scope when allow_user_scope is off — the surface is
    # invisible, never 403. A corrupt/non-object on-disk settings.json raises
    # InvalidCandidateError from _load_json_obj; map it through the same helper as the
    # PUT route so a hand-edited file is a clean 422, never a 500.
    # Capability gate FIRST, before the scope-enum check, so a disabled surface 404s
    # for ANY request (a bogus scope included), never a differing 422 (#819/#768).
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    if scope == "user":
        try:
            hooks, file_hash = await asyncio.to_thread(
                config_write_hooks.read_user_hooks, _base.user_settings_json(runner)
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {"scope": "user", "hooks": hooks, "hash": file_hash}
    if scope == "local":
        project_dir = _base.resolve_cw_project(config, project)
        try:
            hooks, file_hash = await asyncio.to_thread(
                config_write_hooks.read_project_local_hooks, project_dir
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {"scope": "local", "project": project, "hooks": hooks, "hash": file_hash}
    project_dir = _base.resolve_cw_project(config, project)
    try:
        hooks, file_hash = await asyncio.to_thread(
            config_write_hooks.read_project_hooks, project_dir
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    return {"scope": "project", "project": project, "hooks": hooks, "hash": file_hash}


@router.put("/api/config-write/hooks")
async def api_config_write_hooks_write(
    config: ConfigDep, runner: RunnerOrNoneDep, body: dict
) -> dict:
    """Replace the hooks block for a surface, storing commands as inert, unexecuted data."""
    # SECURITY: hooks are shell commands claude runs on lifecycle events. The
    # structural validator NEVER resolves, spawns, or shell-parses a command
    # string; it is stored as inert data and only runs inside a real claude
    # process. The off-by-default gate + validate-never-execute invariant are
    # what prevent a browser write from reaching host RCE.
    return await _base.put_config_write(
        config,
        body,
        "hooks",
        surface="hooks",
        write_user_fn=config_write_hooks.write_user_hooks,
        write_project_fn=config_write_hooks.write_project_hooks,
        write_local_fn=config_write_hooks.write_project_local_hooks,
        get_user_path=lambda: _base.user_settings_json(runner),
    )
