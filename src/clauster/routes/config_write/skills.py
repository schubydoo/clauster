"""The config-write skills routes (#1156).

List, read one file from, create/replace, and delete a skill directory
(``.claude/skills``), plus read/replace the ``skillOverrides`` visibility map. Skill
DIRECTORY ops are user/project scope only -- Claude Code has no "local" skills
directory -- while ``skillOverrides`` is an ordinary ``settings.json`` key and so gets
all three scopes. SECURITY: a skill's supporting files (scripts/*) are uploaded,
OPAQUE content -- never parsed/resolved/executed here, only shape-checked; any file
besides ``SKILL.md`` requires the caller to echo the extra script-body confirm token,
a second distinct confirm on top of the ordinary type-the-name gate. Every route runs
behind the same fail-closed gate order: capability first (404, invisible surface),
then scope-enum, then confirm, then path containment before any I/O.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from ... import config_audit, config_write, config_write_skills
from ...dependencies import ConfigDep, RunnerOrNoneDep
from . import _base

router = APIRouter()


@router.get("/api/config-write/skills")
async def api_config_write_skills_list(
    config: ConfigDep, runner: RunnerOrNoneDep, scope: str = "project", project: str = ""
) -> dict:
    """List the skills defined at user or project scope."""
    # Skill DIRECTORY ops are User/Project scope ONLY -- Claude Code has no
    # "local" skills directory (config_write_skills' module docstring). Capability
    # gate FIRST, before the scope-enum check (#819 ordering fix): a disabled
    # surface must 404 for ANY request, a bogus scope included.
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user"):
        raise HTTPException(status_code=422, detail="scope must be 'project' or 'user' for skills")
    # NOTE: config_write_skills.list_{user,project}_skills() never raises
    # ConfigWriteError -- a skill whose SKILL.md fails structural validation is
    # still listed with a "frontmatter_error" field (see its docstring), so
    # there is no error-mapping try/except needed here (unlike the file-read
    # and write routes below, which DO propagate typed failures).
    if scope == "user":
        skills = await asyncio.to_thread(
            config_write_skills.list_user_skills, _base.user_claude_json(runner)
        )
        return {"scope": "user", "skills": skills}
    project_dir = _base.resolve_cw_project(config, project)
    skills = await asyncio.to_thread(config_write_skills.list_project_skills, project_dir)
    return {"scope": "project", "project": project, "skills": skills}


@router.get("/api/config-write/skills/file")
async def api_config_write_skills_file_read(
    config: ConfigDep,
    runner: RunnerOrNoneDep,
    scope: str = "project",
    project: str = "",
    name: str = "",
    relative: str = config_write_skills.SKILL_FILENAME,
) -> dict:
    """Return one redacted file from inside a skill directory (``SKILL.md`` by default)."""
    # Redaction closes the #813 INFO-1 gap here, unlike the CLAUDE.md content-tier
    # route -- see config_write_skills' module docstring.
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user"):
        raise HTTPException(status_code=422, detail="scope must be 'project' or 'user' for skills")
    if not name:
        raise HTTPException(status_code=422, detail="'name' is required")
    if scope == "user":
        try:
            content, file_hash, exists = await asyncio.to_thread(
                config_write_skills.read_user_skill_file,
                _base.user_claude_json(runner),
                name,
                relative,
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {
            "scope": "user",
            "name": name,
            "relative": relative,
            "content": content,
            "hash": file_hash,
            "exists": exists,
        }
    project_dir = _base.resolve_cw_project(config, project)
    try:
        content, file_hash, exists = await asyncio.to_thread(
            config_write_skills.read_project_skill_file, project_dir, name, relative
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    return {
        "scope": "project",
        "project": project,
        "name": name,
        "relative": relative,
        "content": content,
        "hash": file_hash,
        "exists": exists,
    }


@router.put("/api/config-write/skills")
async def api_config_write_skills_write(
    config: ConfigDep, runner: RunnerOrNoneDep, body: dict
) -> dict:
    """Create or replace a skill directory; any script body needs a second explicit confirm."""
    # SECURITY: a skill's supporting files (scripts/*) are uploaded, OPAQUE
    # content -- never parsed/resolved/executed here, only shape-checked
    # (config_write_skills.validate_script_body). Any file besides SKILL.md
    # requires the caller to echo config_write_skills.SCRIPT_CONFIRM_TOKEN back
    # in "confirm_scripts" -- a SECOND, distinct confirm on top of the ordinary
    # type-the-name gate, required only when script bodies are actually present.
    #
    # Capability gate FIRST, before the scope-enum check (#819 ordering fix).
    scope = body.get("scope", "project")
    config_write.require_capability(config, scope)
    if scope not in ("project", "user"):
        raise HTTPException(status_code=422, detail="scope must be 'project' or 'user' for skills")
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="body must include a 'name' string")
    project = body.get("project") if scope != "user" else None
    config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]
    files = body.get("files")
    if not isinstance(files, dict):
        raise HTTPException(status_code=422, detail="body must include a 'files' object")
    expected: str | None = body.get("hash")
    if expected is not None and not isinstance(expected, str):
        raise HTTPException(status_code=422, detail="'hash' must be a string when present")
    confirm_scripts = body.get("confirm_scripts")
    if scope == "user":
        try:
            await asyncio.to_thread(
                config_write_skills.write_user_skill,
                _base.user_claude_json(runner),
                name,
                files,
                expected_hash=expected,
                confirm_scripts=confirm_scripts,
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="skills",
            scope="user",
            target=name,
            action="update",
            actor=_base.SESSION_USER,
            keys=sorted(files),
        )
        return {"scope": "user", "name": name, "ok": True}
    project_dir = _base.resolve_cw_project(config, project, require_exists=True)
    try:
        await asyncio.to_thread(
            config_write_skills.write_project_skill,
            project_dir,
            name,
            files,
            expected_hash=expected,
            confirm_scripts=confirm_scripts,
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    await config_audit.arecord(
        config.state_dir,
        surface="skills",
        scope="project",
        target=name,
        action="update",
        actor=_base.SESSION_USER,
        keys=sorted(files),
    )
    return {"scope": "project", "project": project, "name": name, "ok": True}


@router.post("/api/config-write/skills/delete")
async def api_config_write_skills_delete(
    config: ConfigDep, runner: RunnerOrNoneDep, body: dict
) -> dict:
    """Delete a skill directory behind the same type-the-name confirm a write requires."""
    # The confirm is because deletion is irreversible — no undo store. Capability
    # gate FIRST, before the scope-enum check.
    scope = body.get("scope", "project")
    config_write.require_capability(config, scope)
    if scope not in ("project", "user"):
        raise HTTPException(status_code=422, detail="scope must be 'project' or 'user' for skills")
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="body must include a 'name' string")
    project = body.get("project") if scope != "user" else None
    config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]
    if scope == "user":
        try:
            existed = await asyncio.to_thread(
                config_write_skills.delete_user_skill, _base.user_claude_json(runner), name
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="skills",
            scope="user",
            target=name,
            action="delete",
            actor=_base.SESSION_USER,
            extra={"removed": existed},
        )
        return {"scope": "user", "name": name, "existed": existed}
    project_dir = _base.resolve_cw_project(config, project, require_exists=True)
    try:
        existed = await asyncio.to_thread(
            config_write_skills.delete_project_skill, project_dir, name
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    await config_audit.arecord(
        config.state_dir,
        surface="skills",
        scope="project",
        target=name,
        action="delete",
        actor=_base.SESSION_USER,
        extra={"removed": existed},
    )
    return {"scope": "project", "project": project, "name": name, "existed": existed}


@router.get("/api/config-write/skills/overrides")
async def api_config_write_skills_overrides_read(
    config: ConfigDep, runner: RunnerOrNoneDep, scope: str = "project", project: str = ""
) -> dict:
    """Return the ``skillOverrides`` visibility map for a surface."""
    # skillOverrides is an ordinary settings.json key, so -- unlike the directory
    # ops above -- it gets all three scopes (user/project/local), exactly like
    # config_write_hooks' `hooks` key. Capability gate FIRST (#819 ordering fix).
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    if scope == "user":
        try:
            overrides, file_hash = await asyncio.to_thread(
                config_write_skills.read_user_skill_overrides, _base.user_settings_json(runner)
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {"scope": "user", "overrides": overrides, "hash": file_hash}
    if scope == "local":
        project_dir = _base.resolve_cw_project(config, project)
        try:
            overrides, file_hash = await asyncio.to_thread(
                config_write_skills.read_project_local_skill_overrides, project_dir
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {
            "scope": "local",
            "project": project,
            "overrides": overrides,
            "hash": file_hash,
        }
    project_dir = _base.resolve_cw_project(config, project)
    try:
        overrides, file_hash = await asyncio.to_thread(
            config_write_skills.read_project_skill_overrides, project_dir
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    return {"scope": "project", "project": project, "overrides": overrides, "hash": file_hash}


@router.put("/api/config-write/skills/overrides")
async def api_config_write_skills_overrides_write(
    config: ConfigDep, runner: RunnerOrNoneDep, body: dict
) -> dict:
    """Replace the ``skillOverrides`` map — inert visibility state, never executed."""
    # skillOverrides is inert visibility state (on/name-only/user-invocable-only/
    # off) -- never executed, unlike the skill directory writer above. Gate order
    # mirrors the CLAUDE.md/settings routes (capability -> scope-enum 422 ->
    # confirm 400 -> payload shape 422 -> path resolve/contain -> stale-hash guard
    # (inside the writer) -> atomic write) -- the #819 fix, not the older
    # _base.put_config_write ordering (scope-enum before capability).
    scope = body.get("scope", "project")
    config_write.require_capability(config, scope)
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    project = body.get("project") if scope != "user" else None
    config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]
    payload = body.get("overrides")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="body must include an 'overrides' object")
    expected: str | None = body.get("hash")
    if expected is not None and not isinstance(expected, str):
        raise HTTPException(status_code=422, detail="'hash' must be a string when present")
    if scope == "user":
        user_settings = _base.user_settings_json(runner)
        try:
            await asyncio.to_thread(
                config_write_skills.write_user_skill_overrides,
                user_settings,
                payload,
                expected,
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="skill-overrides",
            scope="user",
            target=str(user_settings),
            action="update",
            actor=_base.SESSION_USER,
            keys=sorted(payload),
        )
        return {"scope": "user", "ok": True}
    if scope == "local":
        project_dir = _base.resolve_cw_project(config, project, require_exists=True)
        try:
            await asyncio.to_thread(
                config_write_skills.write_project_local_skill_overrides,
                project_dir,
                payload,
                expected,
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="skill-overrides",
            scope="local",
            target=str(project_dir),
            action="update",
            actor=_base.SESSION_USER,
            keys=sorted(payload),
        )
        return {"scope": "local", "project": project, "ok": True}
    project_dir = _base.resolve_cw_project(config, project, require_exists=True)
    try:
        await asyncio.to_thread(
            config_write_skills.write_project_skill_overrides, project_dir, payload, expected
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    await config_audit.arecord(
        config.state_dir,
        surface="skill-overrides",
        scope="project",
        target=str(project_dir),
        action="update",
        actor=_base.SESSION_USER,
        keys=sorted(payload),
    )
    return {"scope": "project", "project": project, "ok": True}
