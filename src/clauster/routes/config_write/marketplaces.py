"""The config-write marketplaces routes (#1156).

List the merged marketplace pool, read which marketplaces a given scope declares, and
add/remove/update a marketplace (#771). ``add``/``remove`` always state their scope
explicitly (omitting ``--scope`` on ``remove`` would let the CLI reach into every
scope); ``update`` takes no ``--scope`` but is still routed through the same
scope/project/confirm plumbing for a stable cwd. Every route runs behind the same
fail-closed gate order as the rest of the tier: capability first (404, invisible
surface), then scope-enum, then confirm, then path containment before any I/O.

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


@router.get("/api/config-write/marketplaces")
async def api_config_write_marketplaces_list(
    config: ConfigDep, runner: RunnerOrNoneDep, scope: str = "project", project: str = ""
) -> dict:
    """List the merged marketplace pool via the CLI."""
    # `claude plugin marketplace list --json` (#771) -- a single merged pool,
    # confirmed cwd-independent live, but still gated/routed through the
    # ordinary scope plumbing like every other route here.
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    cwd = _base.plugin_cli_cwd(config, runner, scope, project)
    try:
        marketplaces = await asyncio.to_thread(
            config_write_plugins.cli_list_marketplaces, config.claude.binary, cwd
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    # Omit `project` for user scope (a meaningless "") to match the sibling routes.
    result: dict[str, Any] = {"scope": scope, "marketplaces": marketplaces}
    if scope != "user":
        result["project"] = project
    return result


@router.get("/api/config-write/marketplaces/declared")
async def api_config_write_marketplaces_declared(
    config: ConfigDep, runner: RunnerOrNoneDep, scope: str = "project", project: str = ""
) -> dict:
    """Read which marketplaces this scope declares — what the merged list cannot tell you."""
    # Direct (non-spawning) read of the PER-SCOPE `extraKnownMarketplaces`
    # declaration -- needed to know where a remove/add would land.
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    if scope == "user":
        declared = await asyncio.to_thread(
            config_write_plugins.read_user_marketplaces, _base.user_settings_json(runner)
        )
        return {"scope": "user", "marketplaces": declared}
    project_dir = _base.resolve_cw_project(config, project)
    read_fn = (
        config_write_plugins.read_project_local_marketplaces
        if scope == "local"
        else config_write_plugins.read_project_marketplaces
    )
    declared = await asyncio.to_thread(read_fn, project_dir)
    return {"scope": scope, "project": project, "marketplaces": declared}


@router.post("/api/config-write/marketplaces/action")
async def api_config_write_marketplaces_action(
    config: ConfigDep, runner: RunnerDep, body: dict
) -> dict:
    """Add, remove, or update a marketplace behind the scope/confirm plumbing.

    ``add``/``remove`` always state their scope explicitly; ``update`` takes none.
    """
    # Marketplace add/remove/update (#771). `add`/`remove` are scoped
    # (--scope always explicit, never omitted -- omitting it on `remove`
    # would let the CLI reach into every scope, see config_write_plugins'
    # module docstring); `update` takes no --scope but is still routed
    # through the same scope/project/confirm plumbing for a stable cwd.
    scope = body.get("scope", "project")
    config_write.require_capability(config, scope)
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    project = body.get("project") if scope != "user" else None
    config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]

    op = body.get("op")
    if op not in ("add", "remove", "update"):
        raise HTTPException(status_code=422, detail="op must be 'add', 'remove', or 'update'")

    name_raw = body.get("name")
    source_raw = body.get("source")
    name: str | None = None
    source: str | None = None
    if op == "add":
        if not isinstance(source_raw, str) or not source_raw:
            raise HTTPException(
                status_code=422, detail="body must include a non-empty 'source' string"
            )
        source = source_raw
        try:
            config_write.validate_candidate(
                source, config_write_plugins.validate_marketplace_source
            )
        except config_write.InvalidCandidateError as exc:
            raise _base.map_config_write_error(exc) from exc
    elif op == "remove":
        if not isinstance(name_raw, str) or not name_raw:
            raise HTTPException(
                status_code=422, detail="body must include a non-empty 'name' string"
            )
        name = name_raw
        try:
            config_write.validate_candidate(name, config_write_plugins.validate_marketplace_name)
        except config_write.InvalidCandidateError as exc:
            raise _base.map_config_write_error(exc) from exc
    elif name_raw is not None:
        if not isinstance(name_raw, str) or not name_raw:
            raise HTTPException(
                status_code=422, detail="'name' must be a non-empty string when present"
            )
        name = name_raw
        try:
            config_write.validate_candidate(name, config_write_plugins.validate_marketplace_name)
        except config_write.InvalidCandidateError as exc:
            raise _base.map_config_write_error(exc) from exc

    cwd = _base.plugin_cli_cwd(config, runner, scope, project or "")
    binary = config.claude.binary

    def _work() -> None:
        """Run the requested marketplace verb through the CLI."""
        if op == "add":
            config_write_plugins.cli_marketplace_add(binary, cwd, source, scope)  # type: ignore[arg-type]
        elif op == "remove":
            config_write_plugins.cli_marketplace_remove(binary, cwd, name, scope)  # type: ignore[arg-type]
        else:
            config_write_plugins.cli_marketplace_update(binary, cwd, name)

    # Audit right after the mutation commits, BEFORE the gitignore housekeeping — a
    # failure of that step must not drop the committed change from the trail (#958 P6).
    # Records the changed files + the redacted `claude plugin marketplace` argv it ran.
    try:
        await _base.audit_config_write(
            config,
            work=_work,
            watch=_base.config_write_watch(runner, cwd),
            surface="marketplaces",
            scope=scope,  # type: ignore[arg-type]
            target=(name or source or ""),
            action=op,  # type: ignore[arg-type]
            actor=_base.SESSION_USER,
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    if scope == "local" and op in ("add", "remove"):
        # Only add/remove actually touch the scope's settings file (`update`
        # merely refreshes a git checkout, writing no settings key) -- see the
        # plugins/action route's identical comment for why this is needed at
        # all: the CLI writes settings.local.json directly, bypassing
        # clauster's own gitignore-on-create writer path.
        await asyncio.to_thread(
            config_write.ensure_gitignored,
            cwd,
            ".claude/settings.local.json",
            ignore_backup_sibling=True,
        )
    result: dict[str, Any] = {"scope": scope, "op": op, "ok": True}
    if name is not None:
        result["name"] = name
    if source is not None:
        result["source"] = source
    if scope != "user":
        result["project"] = project
    return result
