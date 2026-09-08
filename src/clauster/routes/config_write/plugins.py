"""The config-write plugins routes (#1156).

List installed plugins, read the plugin enable/disable map, read one plugin's
component inventory, and enable/disable/install/uninstall/update a plugin (#771).
Most of this state is CLI-only (``claude plugin ...``): the install timestamp,
cwd-dependent ``enabled`` field, and component/token inventory have no ``settings.json``
equivalent to read directly. SECURITY: ``install`` pulls new executable code onto the
host, so it carries a SECOND, stronger confirm (the operator retypes the exact plugin
id) on top of the ordinary scope confirm. Every route runs behind the same fail-closed
gate order as the rest of the tier: capability first (404, invisible surface), then
scope-enum, then confirm, then path containment before any I/O.

The read routes inject ``RunnerOrNoneDep`` and touch the runner only in the user
branch (via the fail-closed ``_base.plugin_cli_cwd`` / ``_base.user_settings_json``
helpers), so a project/local read still works with no runner wired. The action route
injects ``RunnerDep`` (fail-closed): it needs a non-None runner for the #958 P6 audit
watch list at every scope, mirroring the always-non-None closure ``runner`` it used
inside ``create_app`` (#1156).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from ... import config_write, config_write_plugins
from ...dependencies import ConfigDep, RunnerDep, RunnerOrNoneDep
from . import _base

router = APIRouter()


@router.get("/api/config-write/plugins")
async def api_config_write_plugins_list(
    config: ConfigDep, runner: RunnerOrNoneDep, scope: str = "project", project: str = ""
) -> dict:
    """List the installed plugins via the CLI — this state has no file to read directly."""
    # Installed plugins (#771) -- CLI-only (`claude plugin list --json`): cache
    # path / install timestamp / cwd-dependent `enabled` state have no
    # settings.json equivalent to read directly. Capability gate FIRST (#819).
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    cwd = _base.plugin_cli_cwd(config, runner, scope, project)
    try:
        plugins = await asyncio.to_thread(
            config_write_plugins.cli_list_plugins, config.claude.binary, cwd
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    # Omit `project` for user scope (where it is a meaningless "") to match the
    # sibling routes (/plugins/enabled, /marketplaces/declared, the action POSTs).
    result: dict[str, Any] = {"scope": scope, "plugins": plugins}
    if scope != "user":
        result["project"] = project
    return result


@router.get("/api/config-write/plugins/enabled")
async def api_config_write_plugins_enabled(
    config: ConfigDep, runner: RunnerOrNoneDep, scope: str = "project", project: str = ""
) -> dict:
    """Read the plugin enable/disable map straight from the settings file, no CLI spawn."""
    # Mirrors the MCP surface's "file read for display" doctrine; no secret ever
    # lives here.
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    if scope == "user":
        enabled = await asyncio.to_thread(
            config_write_plugins.read_user_enabled_plugins, _base.user_settings_json(runner)
        )
        return {"scope": "user", "enabled": enabled}
    project_dir = _base.resolve_cw_project(config, project)
    read_fn = (
        config_write_plugins.read_project_local_enabled_plugins
        if scope == "local"
        else config_write_plugins.read_project_enabled_plugins
    )
    enabled = await asyncio.to_thread(read_fn, project_dir)
    return {"scope": scope, "project": project, "enabled": enabled}


@router.get("/api/config-write/plugins/{plugin_id}")
async def api_config_write_plugin_details(
    config: ConfigDep,
    runner: RunnerOrNoneDep,
    plugin_id: str,
    scope: str = "project",
    project: str = "",
) -> dict:
    """Return one plugin's component inventory and token-cost projection from the CLI."""
    # `claude plugin details <id>` -- CLI-only (component inventory + token
    # cost projection, not stored in settings.json). Capability gate FIRST.
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    try:
        config_write.validate_candidate(plugin_id, config_write_plugins.validate_plugin_id)
    except config_write.InvalidCandidateError as exc:
        raise _base.map_config_write_error(exc) from exc
    cwd = _base.plugin_cli_cwd(config, runner, scope, project)
    try:
        details = await asyncio.to_thread(
            config_write_plugins.cli_plugin_details, config.claude.binary, cwd, plugin_id
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    return {"scope": scope, "project": project, "plugin": plugin_id, "details": details}


@router.post("/api/config-write/plugins/action")
async def api_config_write_plugins_action(
    config: ConfigDep, runner: RunnerDep, body: dict
) -> dict:
    """Enable, disable, install, uninstall, or update a plugin; install needs a 2nd confirm."""
    # Plugin enable/disable/install/uninstall/update (#771), the highest
    # blast-radius config-write child: `install` pulls new executable code
    # onto the host, so it carries a SECOND, stronger confirm on top of the
    # ordinary scope confirm -- see config_write_plugins.require_install_confirm.
    # Gate order (the #819/#768 fix, extended with the install-specific
    # confirm): capability -> scope-enum 422 -> base scope confirm 400 ->
    # op/plugin-id shape 422 -> [install only] plugin-id confirm 400 ->
    # path resolve/contain (400/404) -> the CLI dispatch itself (404
    # not-found, or 400 for any other CLI failure).
    scope = body.get("scope", "project")
    config_write.require_capability(config, scope)
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    project = body.get("project") if scope != "user" else None
    config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]

    op = body.get("op")
    if op not in ("enable", "disable", "install", "uninstall", "update"):
        raise HTTPException(
            status_code=422,
            detail="op must be 'enable', 'disable', 'install', 'uninstall', or 'update'",
        )
    plugin_id = body.get("plugin")
    if not isinstance(plugin_id, str) or not plugin_id:
        raise HTTPException(
            status_code=422, detail="body must include a non-empty 'plugin' string"
        )
    try:
        config_write.validate_candidate(plugin_id, config_write_plugins.validate_plugin_id)
    except config_write.InvalidCandidateError as exc:
        raise _base.map_config_write_error(exc) from exc

    if op == "install":
        # The STRONG per-install confirm: the operator retypes the exact
        # plugin id being introduced, not just the project/scope name.
        config_write_plugins.require_install_confirm(plugin_id, body.get("confirm_plugin"))

    keep_data = body.get("keep_data", False)
    if not isinstance(keep_data, bool):
        raise HTTPException(status_code=422, detail="'keep_data' must be a boolean")
    prune = body.get("prune", False)
    if not isinstance(prune, bool):
        raise HTTPException(status_code=422, detail="'prune' must be a boolean")

    cwd = _base.plugin_cli_cwd(config, runner, scope, project or "")
    binary = config.claude.binary

    def _work() -> None:
        """Run the requested plugin verb through the CLI."""
        if op == "enable":
            config_write_plugins.cli_enable_plugin(binary, cwd, plugin_id, scope)  # type: ignore[arg-type]
        elif op == "disable":
            config_write_plugins.cli_disable_plugin(binary, cwd, plugin_id, scope)  # type: ignore[arg-type]
        elif op == "install":
            config_write_plugins.cli_install_plugin(binary, cwd, plugin_id, scope)  # type: ignore[arg-type]
        elif op == "uninstall":
            config_write_plugins.cli_uninstall_plugin(
                binary,
                cwd,
                plugin_id,
                scope,  # type: ignore[arg-type]
                keep_data=keep_data,
                prune=prune,
            )
        else:
            config_write_plugins.cli_update_plugin(binary, cwd, plugin_id, scope)  # type: ignore[arg-type]

    # Audit right after the mutation commits, BEFORE the gitignore housekeeping — a
    # failure of that step must not drop the committed change from the trail (#958 P6).
    # Records the changed files + the redacted `claude plugin` argv it ran.
    try:
        await _base.audit_config_write(
            config,
            work=_work,
            watch=_base.config_write_watch(runner, cwd),
            surface="plugins",
            scope=scope,  # type: ignore[arg-type]
            target=plugin_id,
            action=op,  # type: ignore[arg-type]
            actor=_base.SESSION_USER,
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    if scope == "local":
        # The CLI writes settings.local.json directly (never through clauster's
        # own writer), so clauster must gitignore it itself here -- the
        # gitignore-on-create hard requirement (#766) still applies even when
        # the file is CLI-written rather than clauster-written.
        await asyncio.to_thread(
            config_write.ensure_gitignored,
            cwd,
            ".claude/settings.local.json",
            ignore_backup_sibling=True,
        )
    result = {"scope": scope, "plugin": plugin_id, "op": op, "ok": True}
    if scope != "user":
        result["project"] = project
    return result
