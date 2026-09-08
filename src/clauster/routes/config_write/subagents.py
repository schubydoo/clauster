"""The config-write subagents routes (#1156).

List, read, create/replace, and delete the subagents defined at user or project
scope (``.claude/agents``). Subagents have exactly two scopes (user/project) -- there
is no genuine local-scope directory Claude Code itself reads. SECURITY: a subagent's
frontmatter can carry ``hooks``/``mcpServers``/``tools``, each validated the same
fail-closed, validate-never-execute way as the dedicated surfaces; a name colliding
with a Claude Code built-in, or an existing on-disk file already detected as
plugin-owned, is refused (403) before the candidate content is even validated. Every
route runs behind the same fail-closed gate order as the rest of the tier: capability
first (404, invisible surface), then scope-enum, then confirm, then path containment.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from ... import config_audit, config_write, config_write_subagents
from ...dependencies import ConfigDep, RunnerOrNoneDep
from . import _base

router = APIRouter()


@router.get("/api/config-write/subagents")
async def api_config_write_subagents_list(
    config: ConfigDep, runner: RunnerOrNoneDep, scope: str = "project", project: str = ""
) -> dict:
    """List the subagents defined at user or project scope."""
    # Subagents have exactly two scopes (user/project) — unlike the JSON-subtree
    # surfaces and CLAUDE.md, there is no genuine local-scope directory Claude
    # Code itself reads (see the config_write_subagents module docstring).
    # Capability gate FIRST, before the scope-enum check, so a disabled surface
    # 404s for ANY request (a bogus scope included) — the #819/#768 ordering.
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user"):
        raise HTTPException(status_code=422, detail="scope must be 'project' or 'user'")
    if scope == "user":
        agents = await asyncio.to_thread(
            config_write_subagents.list_user_agents, _base.user_claude_json(runner)
        )
        return {"scope": "user", "agents": agents}
    project_dir = _base.resolve_cw_project(config, project)
    agents = await asyncio.to_thread(config_write_subagents.list_project_agents, project_dir)
    return {"scope": "project", "project": project, "agents": agents}


@router.get("/api/config-write/subagents/{name}")
async def api_config_write_subagent_get(
    config: ConfigDep,
    runner: RunnerOrNoneDep,
    name: str,
    scope: str = "project",
    project: str = "",
) -> dict:
    """Return one subagent's detail doc; a built-in name yields a synthetic read-only doc."""
    # The synthetic built-in doc is 200-shaped (it really exists in Claude Code,
    # just not as a file) — never a 404. A missing real file raises AgentNotFoundError,
    # mapped to 404 below. `content` is raw/unredacted (the write round trip);
    # `frontmatter` is a derived, structurally redacted display field.
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user"):
        raise HTTPException(status_code=422, detail="scope must be 'project' or 'user'")
    if scope == "user":
        try:
            doc = await asyncio.to_thread(
                config_write_subagents.read_user_agent, _base.user_claude_json(runner), name
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {"scope": "user", **doc}
    project_dir = _base.resolve_cw_project(config, project)
    try:
        doc = await asyncio.to_thread(config_write_subagents.read_project_agent, project_dir, name)
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    return {"scope": "project", "project": project, **doc}


@router.put("/api/config-write/subagents/{name}")
async def api_config_write_subagent_put(
    config: ConfigDep, runner: RunnerOrNoneDep, name: str, body: dict
) -> dict:
    """Create or replace one subagent, refusing built-in and plugin-owned names."""
    # SECURITY: a subagent's frontmatter can carry `hooks`/`mcpServers`/`tools` —
    # each validated the same fail-closed, validate-never-execute way as the
    # dedicated surfaces (hooks reuses config_write_hooks.validate_hooks wholesale,
    # including its plugin-marker rejection). A name colliding with a Claude Code
    # built-in, or an existing on-disk file already detected as plugin-owned, is
    # refused (403) before the candidate content is even validated.
    #
    # Gate order (the #819/#768 fix): capability -> scope-enum 422 -> confirm 400
    # -> payload shape 422 -> path-contain/read-only guard (403, inside the
    # writer) -> stale-hash guard (409, inside the writer) -> atomic write.
    scope = body.get("scope", "project")
    config_write.require_capability(config, scope)
    if scope not in ("project", "user"):
        raise HTTPException(status_code=422, detail="scope must be 'project' or 'user'")
    project = body.get("project") if scope != "user" else None
    config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]
    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=422, detail="body must include a 'content' string")
    expected: str | None = body.get("hash")
    if expected is not None and not isinstance(expected, str):
        raise HTTPException(status_code=422, detail="'hash' must be a string when present")
    if scope == "user":
        try:
            await asyncio.to_thread(
                config_write_subagents.write_user_agent,
                _base.user_claude_json(runner),
                name,
                content,
                expected,
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="subagents",
            scope="user",
            target=name,
            action="update",
            actor=_base.SESSION_USER,
        )
        return {"scope": "user", "name": name, "ok": True}
    project_dir = _base.resolve_cw_project(config, project, require_exists=True)
    try:
        await asyncio.to_thread(
            config_write_subagents.write_project_agent, project_dir, name, content, expected
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    await config_audit.arecord(
        config.state_dir,
        surface="subagents",
        scope="project",
        target=name,
        action="update",
        actor=_base.SESSION_USER,
    )
    return {"scope": "project", "project": project, "name": name, "ok": True}


@router.delete("/api/config-write/subagents/{name}")
async def api_config_write_subagent_delete(
    config: ConfigDep,
    runner: RunnerOrNoneDep,
    name: str,
    scope: str = "project",
    project: str = "",
    confirm: str = "",
) -> dict:
    """Delete one subagent; built-in and plugin-owned names are refused, absent ones no-op."""
    # Same fail-closed gate order as the PUT route (capability -> scope-enum ->
    # confirm -> read-only/path guards inside the deleter). A refusal is a 403; a
    # genuinely absent ordinary name is `deleted: false`, never an error.
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user"):
        raise HTTPException(status_code=422, detail="scope must be 'project' or 'user'")
    proj = project if scope != "user" else None
    config_write.require_confirm(scope, proj, confirm)  # type: ignore[arg-type]
    if scope == "user":
        try:
            existed = await asyncio.to_thread(
                config_write_subagents.delete_user_agent, _base.user_claude_json(runner), name
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="subagents",
            scope="user",
            target=name,
            action="delete",
            actor=_base.SESSION_USER,
            extra={"removed": existed},
        )
        return {"scope": "user", "name": name, "deleted": existed}
    project_dir = _base.resolve_cw_project(config, proj, require_exists=True)
    try:
        existed = await asyncio.to_thread(
            config_write_subagents.delete_project_agent, project_dir, name
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    await config_audit.arecord(
        config.state_dir,
        surface="subagents",
        scope="project",
        target=name,
        action="delete",
        actor=_base.SESSION_USER,
        extra={"removed": existed},
    )
    return {"scope": "project", "project": proj, "name": name, "deleted": existed}
