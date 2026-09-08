"""The config-write CLAUDE.md routes (#1156).

``GET``/``PUT /api/config-write/claude-md`` read and replace CLAUDE.md for a surface.
CLAUDE.md is prompt-injection CONTENT, not executable config (#768 threat model): the
capability + type-the-name confirm gates still apply, but the payload is free-form
text with no structural shape to validate and no redaction on write (nothing here is
ever assembled from a secret sentinel). Every route runs behind the same fail-closed
gate order as the rest of the tier: capability first (404, invisible surface), then
scope-enum, then confirm, then path containment before any I/O.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from ... import claude_md, config_audit, config_write
from ...dependencies import ConfigDep, RunnerOrNoneDep
from . import _base

router = APIRouter()


@router.get("/api/config-write/claude-md")
async def api_config_write_claude_md_read(
    config: ConfigDep, runner: RunnerOrNoneDep, scope: str = "project", project: str = ""
) -> dict:
    """Return CLAUDE.md for a surface as raw text — content is never redacted (#768)."""
    # Gated exactly like the permissions/hooks/MCP routes: 404 when config-write is
    # off, and 404 for user scope when allow_user_scope is off — the surface is
    # invisible, never 403. Content-tier: unlike the skills file route, this one
    # returns free-form file text with no redacted companion field (#768 threat model).
    #
    # Capability gate FIRST, before the scope-enum check, so a disabled surface 404s
    # for ANY request (a bogus scope included), never a differing 422 that would leak
    # that the endpoint exists (#819/#768).
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    if scope == "user":
        try:
            content, file_hash, exists = await asyncio.to_thread(
                claude_md.read_user_claude_md, _base.user_claude_json(runner)
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {"scope": "user", "content": content, "hash": file_hash, "exists": exists}
    if scope == "local":
        project_dir = _base.resolve_cw_project(config, project)
        try:
            content, file_hash, exists = await asyncio.to_thread(
                claude_md.read_project_local_claude_md, project_dir
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {
            "scope": "local",
            "project": project,
            "content": content,
            "hash": file_hash,
            "exists": exists,
        }
    project_dir = _base.resolve_cw_project(config, project)
    try:
        content, file_hash, exists = await asyncio.to_thread(
            claude_md.read_project_claude_md, project_dir
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    return {
        "scope": "project",
        "project": project,
        "content": content,
        "hash": file_hash,
        "exists": exists,
    }


@router.put("/api/config-write/claude-md")
async def api_config_write_claude_md_write(
    config: ConfigDep, runner: RunnerOrNoneDep, body: dict
) -> dict:
    """Replace CLAUDE.md for a surface behind the capability and type-the-name gates."""
    # CLAUDE.md is prompt-injection CONTENT, not executable config (#768 threat
    # model): the Foundation gate + type-the-name confirm still apply, but there
    # is no structural shape to validate beyond "a string under the size cap" and
    # no redaction on write (nothing here is ever assembled from a secret sentinel).
    # The payload is a single `content` string, not a named JSON subtree, so this
    # route can't reuse `_base.put_config_write` (a dict payload) — the
    # gate order is identical though: capability -> confirm -> shape -> path
    # resolve/contain -> stale-hash guard (inside the writer) -> atomic write.
    #
    # Capability gate FIRST, before the scope-enum check, so a disabled surface
    # 404s for ANY request (a bogus scope included) instead of leaking existence
    # via a differing 422 — same invisible-surface invariant as the GET route.
    scope = body.get("scope", "project")
    config_write.require_capability(config, scope)
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    project = body.get("project") if scope != "user" else None
    config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]
    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=422, detail="body must include a 'content' string")
    expected: str | None = body.get("hash")
    if expected is not None and not isinstance(expected, str):
        raise HTTPException(status_code=422, detail="'hash' must be a string when present")
    if scope == "user":
        user_claude_json = _base.user_claude_json(runner)
        try:
            await asyncio.to_thread(
                claude_md.write_user_claude_md, user_claude_json, content, expected
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="claude-md",
            scope="user",
            target=str(user_claude_json.parent / ".claude" / claude_md.FILENAME),
            action="update",
            actor=_base.SESSION_USER,
        )
        return {"scope": "user", "ok": True}
    project_dir = _base.resolve_cw_project(config, project, require_exists=True)
    write_fn = (
        claude_md.write_project_local_claude_md
        if scope == "local"
        else claude_md.write_project_claude_md
    )
    try:
        await asyncio.to_thread(write_fn, project_dir, content, expected)
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    await config_audit.arecord(
        config.state_dir,
        surface="claude-md",
        scope=scope,
        target=str(project_dir),
        action="update",
        actor=_base.SESSION_USER,
    )
    return {"scope": scope, "project": project, "ok": True}
