"""The config-write generic settings routes (#1156).

Read and replace the ``settings.json`` keys no dedicated surface owns (env/model/misc,
#772), plus the per-key effective-value + winning-scope provenance view. SECURITY:
``env`` is where operators keep secrets (#822 lesson) -- the read path masks every env
value unconditionally, and a write that resends the mask sentinel keeps the stored
value (``config_write.merge_redacted``), so this route never assembles a live secret
from a client echo. Every route runs behind the same fail-closed gate order as the
rest of the tier: capability first (404, invisible surface), then scope-enum, then
confirm, then path containment before any I/O.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from ... import config_audit, config_write, config_write_settings
from ...dependencies import ConfigDep, RunnerOrNoneDep
from . import _base

router = APIRouter()


@router.get("/api/config-write/settings")
async def api_config_write_settings_read(
    config: ConfigDep, runner: RunnerOrNoneDep, scope: str = "project", project: str = ""
) -> dict:
    """Return the settings.json keys no dedicated surface owns, with env values masked."""
    # Generic settings.json editor (#772): env/model/misc keys not owned by a
    # dedicated surface (permissions/hooks/plugin+MCP-enable stay on their own
    # routes). Gated exactly like the other config-write reads: 404 when
    # config-write is off, 404 for user scope when allow_user_scope is off.
    #
    # Capability gate FIRST, before the scope-enum check (the #819/#768
    # ordering fix): a disabled surface must 404 for ANY request, a bogus
    # scope included, rather than leak existence via a differing 422.
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    if scope == "user":
        try:
            settings_view, file_hash = await asyncio.to_thread(
                config_write_settings.read_user_settings, _base.user_settings_json(runner)
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {"scope": "user", "settings": settings_view, "hash": file_hash}
    if scope == "local":
        project_dir = _base.resolve_cw_project(config, project)
        try:
            settings_view, file_hash = await asyncio.to_thread(
                config_write_settings.read_project_local_settings, project_dir
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {
            "scope": "local",
            "project": project,
            "settings": settings_view,
            "hash": file_hash,
        }
    project_dir = _base.resolve_cw_project(config, project)
    try:
        settings_view, file_hash = await asyncio.to_thread(
            config_write_settings.read_project_settings, project_dir
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    return {
        "scope": "project",
        "project": project,
        "settings": settings_view,
        "hash": file_hash,
    }


@router.put("/api/config-write/settings")
async def api_config_write_settings_write(
    config: ConfigDep, runner: RunnerOrNoneDep, body: dict
) -> dict:
    """Replace those settings keys, keeping any env value the client resent as the mask."""
    # SECURITY: `env` is where operators keep secrets (#822 lesson) -- the
    # read path masks every env value unconditionally; a write that resends
    # the mask sentinel keeps the stored value (config_write.merge_redacted),
    # so this route never assembles a live secret from a client echo. See
    # config_write_settings' module docstring for the full redaction decision.
    #
    # Gate order mirrors the CLAUDE.md route (capability -> scope-enum 422 ->
    # confirm 400 -> payload shape 422 -> path resolve/contain -> stale-hash
    # guard (inside the writer) -> atomic write) -- the #819/#768 fix, not the
    # older `_base.put_config_write` ordering (scope-enum before capability).
    scope = body.get("scope", "project")
    config_write.require_capability(config, scope)
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    project = body.get("project") if scope != "user" else None
    config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]
    payload = body.get("settings")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="body must include a 'settings' object")
    expected: str | None = body.get("hash")
    if expected is not None and not isinstance(expected, str):
        raise HTTPException(status_code=422, detail="'hash' must be a string when present")
    if scope == "user":
        user_settings = _base.user_settings_json(runner)
        try:
            await asyncio.to_thread(
                config_write_settings.write_user_settings,
                user_settings,
                payload,
                expected,
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="settings",
            scope="user",
            target=str(user_settings),
            action="update",
            actor=_base.SESSION_USER,
            keys=sorted(payload),
        )
        return {"scope": "user", "ok": True}
    project_dir = _base.resolve_cw_project(config, project, require_exists=True)
    write_fn = (
        config_write_settings.write_project_local_settings
        if scope == "local"
        else config_write_settings.write_project_settings
    )
    try:
        await asyncio.to_thread(write_fn, project_dir, payload, expected)
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    await config_audit.arecord(
        config.state_dir,
        surface="settings",
        scope=scope,
        target=str(project_dir),
        action="update",
        actor=_base.SESSION_USER,
        keys=sorted(payload),
    )
    return {"scope": scope, "project": project, "ok": True}


@router.get("/api/config-write/settings/effective")
async def api_config_write_settings_effective(
    config: ConfigDep, runner: RunnerOrNoneDep, project: str = ""
) -> dict:
    """Return each setting's effective value plus the scope layer that supplied it."""
    # Scope-merge provenance (#772, the novel part): per-key effective value
    # + which scope layer supplied it, across every scope clauster manages.
    # Gated on "project" scope -- project/local are inherently per-project,
    # so a project is always required for this view. The user layer is
    # folded into the merge only when allow_user_scope is ALSO on; when it's
    # off, ~/.claude/settings.json is never read for this route either --
    # the user-scope surface stays invisible for every read, this one
    # included, not just the dedicated GET/PUT above.
    config_write.require_capability(config, "project")
    project_dir = _base.resolve_cw_project(config, project)
    try:
        project_misc, _p_hash = await asyncio.to_thread(
            config_write_settings.read_project_settings, project_dir
        )
        local_misc, _l_hash = await asyncio.to_thread(
            config_write_settings.read_project_local_settings, project_dir
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    user_misc: dict[str, Any] | None = None
    if config.config_write.allow_user_scope:
        try:
            user_misc, _u_hash = await asyncio.to_thread(
                config_write_settings.read_user_settings, _base.user_settings_json(runner)
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
    effective = config_write_settings._compute_effective_settings(
        user_misc=user_misc, project_misc=project_misc, local_misc=local_misc
    )
    return {"project": project, "effective": effective}
