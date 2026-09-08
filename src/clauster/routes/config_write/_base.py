"""Shared pipeline helpers for the config-write routes (#1156).

These functions were closures inside :func:`clauster.app.create_app`. They are the
code-executing config-write trust tier's shared plumbing: path-containment
resolution, typed-error mapping, the audit-fingerprint recorder, and the whole PUT
pipeline. Every config-write route module under this package calls them directly; the
whole domain now lives in this package, so :mod:`clauster.app` no longer holds any
config-write handler or wrapper (#1156).

Every collaborator the closures used to capture -- ``config``, ``runner`` -- is now
an explicit parameter, so a missed call site is a type error rather than a silent
free-variable capture. The gates are unchanged by the move: capability first (a
disabled surface is invisibly 404, never a differing status), then confirm, then
path containment before any I/O.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from ... import (
    config_audit,
    config_write,
    config_write_mcp,
    config_write_plugins,
    config_write_skills,
    config_write_subagents,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ...config import ClausterConfig
    from ...runner import SessionRunner

# Single-user actor for the config-write audit trail (single-user in v0.2; multi-user
# is v0.3). Mirrors ``app._SESSION_USER``, which still exists because ``_authenticate``
# in app.py stamps that copy on the request user; the config-write routes here use this
# copy. The two hold the same value and merge when a shared actor constant lands (#1156).
SESSION_USER = "admin"


def resolve_cw_project(
    config: ClausterConfig, name: object, *, require_exists: bool = False
) -> Path:
    """Resolve a project-scope config-write path, validating containment before any I/O.

    A missing or non-string name is a 422 and an escaping one a 400; with
    ``require_exists`` an absent project directory is a clean 404 rather than an
    unhandled error inside the writer.
    """
    # Validate-before-I/O path containment: an escaping path raises PathEscapeError
    # before any write. The name is also the type-the-name confirm token
    # (server-re-derived below).
    # ``require_exists`` is set on the WRITE path only: a contained-but-absent
    # project dir would make the atomic writer's ``mkstemp(dir=path.parent)``
    # raise ``FileNotFoundError`` (an OSError outside the ConfigWriteError guard)
    # → an unhandled 500. Surface it as a clean 404 instead. The READ path leaves
    # it False so a missing dir still reads as an empty server map (harmless).
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="body must include a 'project' string")
    try:
        project_dir = config_write.resolve_project_dir(config.projects_root, name)
    except config_write.PathEscapeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if require_exists and not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"project directory not found: {name!r}")
    return project_dir


def map_config_write_error(exc: config_write.ConfigWriteError) -> HTTPException:
    """Map a typed config-write failure to its fail-closed HTTP status."""
    # InvalidCandidate ⇒ 422 (bad shape); Stale/ServerExists ⇒ 409; ServerNotFound,
    # AgentNotFound, PluginNotFound, MarketplaceNotFound ⇒ 404; ReadOnlyAgent ⇒ 403.
    # Every other ConfigWriteError — including PathEscapeError, which the routes catch
    # earlier as a 400 before the writer is even reached — falls through to a 400.
    if isinstance(exc, config_write.InvalidCandidateError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, config_write.StaleConfigWriteError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, config_write_mcp.ServerExistsError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, config_write_mcp.ServerNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, config_write_subagents.AgentNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, config_write_subagents.ReadOnlyAgentError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, config_write_skills.ScriptConfirmRequiredError):
        # A skill upload included non-SKILL.md files without echoing the extra
        # script-body confirm token — a distinct 400 gate on top of the ordinary
        # type-the-name confirm (see config_write_skills' module docstring).
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, config_write_plugins.PluginNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, config_write_plugins.MarketplaceNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def config_write_watch(runner: SessionRunner, project_dir: Path) -> list[Path]:
    """Return the config files a `claude mcp`/`claude plugin` write could touch.

    A comprehensive candidate set across scopes for the #958 P6 before/after audit
    fingerprint — an unchanged file simply never appears in the diff, so watching a
    superset is harmless and avoids per-scope path guesswork.
    """
    home = runner.claude_json.parent
    return [
        runner.claude_json,
        home / ".claude" / "settings.json",
        home / ".claude" / "plugins" / "known_marketplaces.json",
        project_dir / ".claude" / "settings.json",
        project_dir / ".claude" / "settings.local.json",
        project_dir / ".mcp.json",
    ]


async def audit_config_write(
    config: ClausterConfig, *, work: Callable[[], None], watch: list[Path], **fields: Any
) -> None:
    """Record a committed config-write's audit line + its file/argv side effects (#958 P6).

    Runs ``work`` off-thread, then records the base audit line enriched with (a) which
    watched files it changed — path + sha256 + size, never contents — and (b) the redacted
    ``claude …`` argv any spawned CLI ran.
    Lets :class:`~config_write.ConfigWriteError` propagate (the caller maps it) and records
    ONLY on success. The argv is captured via :data:`config_write.cli_argv_sink`, which
    propagates into the worker thread; the audit append itself is best-effort and never
    fails the already-committed write.

    Best-effort fingerprint, not a transactional attribution: the snapshots bracket the
    write but are not inside its file lock, and ``watch`` is a cross-scope superset, so
    under (rare, single-operator) concurrent writes the diff can attribute another
    request's change. It's a forensic hint of where a change landed — the base line's
    surface/scope/target/action names the operation exactly.
    """
    before = await asyncio.to_thread(config_audit.file_fingerprints, watch)
    sink: list[list[str]] = []
    token = config_write.cli_argv_sink.set(sink)
    try:
        await asyncio.to_thread(work)
    finally:
        config_write.cli_argv_sink.reset(token)
    after = await asyncio.to_thread(config_audit.file_fingerprints, watch)
    extra: dict[str, Any] = {"files": config_audit.diff_fingerprints(before, after)}
    if sink:
        extra["argv"] = sink
    await config_audit.arecord(config.state_dir, extra=extra, **fields)


async def put_config_write(
    config: ClausterConfig,
    body: dict,
    payload_key: str,
    write_user_fn: Callable[..., None],
    write_project_fn: Callable[[Path, dict, str | None], None],
    write_local_fn: Callable[..., None],
    *,
    surface: str,
    get_user_path: Callable[[], Path],
    user_fn_has_hash: bool = True,
    local_fn_has_hash: bool = True,
    get_local_target: Callable[[], Path] | None = None,
) -> dict:
    """Shared Foundation pipeline for the three PUT /api/config-write/* routes.

    Order: capability (404, FIRST — invisible-surface #819/#768) → scope-enum (422)
    → confirm (400, FIRST semantic gate) →
    payload shape check (422) → path resolve/contain → stale-hash guard (409) →
    atomic write. Any step aborts before the write.

    ``user_fn_has_hash=False`` is only correct for writers that own their own
    hash/locking mechanism (currently: MCP user scope via ``write_user_servers``).
    ``local_fn_has_hash=False`` is the same shape for the local-scope twin (MCP
    local scope via ``write_project_local_servers``, which nests into
    ``~/.claude.json`` rather than a separate hashable file) — when set,
    ``get_local_target`` supplies the extra positional argument (the
    ``~/.claude.json`` path) the writer needs ahead of ``project_dir``. Every other
    surface should leave both hash flags at the default ``True`` so the stale-hash
    guard is enforced. Any ``"hash"`` key the client sends is intentionally not
    forwarded when the relevant flag is ``False``.
    """
    scope = body.get("scope", "project")
    # Capability gate FIRST, before the scope-enum check, so a disabled surface 404s
    # for ANY request (a bogus scope included), never a differing 422 (#819/#768).
    config_write.require_capability(config, scope)
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    if scope == "user":
        config_write.require_confirm("user", None, body.get("confirm"))
        payload = body.get(payload_key)
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=422, detail=f"body must include a '{payload_key}' object"
            )
        user_path = get_user_path()
        if user_fn_has_hash:
            expected: str | None = body.get("hash")
            if expected is not None and not isinstance(expected, str):
                raise HTTPException(status_code=422, detail="'hash' must be a string when present")
            try:
                await asyncio.to_thread(write_user_fn, user_path, payload, expected)
            except config_write.ConfigWriteError as exc:
                raise map_config_write_error(exc) from exc
        else:
            # writer owns its own hash/locking (e.g. MCP); "hash" from body is
            # intentionally not forwarded — see user_fn_has_hash docstring above.
            try:
                await asyncio.to_thread(write_user_fn, user_path, payload)
            except config_write.ConfigWriteError as exc:
                raise map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface=surface,
            scope="user",
            target=str(user_path),
            action="update",
            actor=SESSION_USER,
            keys=sorted(payload),
        )
        return {"scope": "user", "ok": True}
    if scope == "local":
        project = body.get("project")
        config_write.require_confirm("local", project, body.get("confirm"))
        payload = body.get(payload_key)
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=422, detail=f"body must include a '{payload_key}' object"
            )
        project_dir = resolve_cw_project(config, project, require_exists=True)
        if local_fn_has_hash:
            expected = body.get("hash")
            if expected is not None and not isinstance(expected, str):
                raise HTTPException(status_code=422, detail="'hash' must be a string when present")
            try:
                await asyncio.to_thread(write_local_fn, project_dir, payload, expected)
            except config_write.ConfigWriteError as exc:
                raise map_config_write_error(exc) from exc
        else:
            # writer owns its own hash/locking (MCP local scope, nested into
            # ~/.claude.json); "hash" from body is intentionally not forwarded.
            if get_local_target is None:  # pragma: no cover - wiring bug, not user-reachable
                raise HTTPException(status_code=500, detail="local scope writer misconfigured")
            local_target = get_local_target()
            try:
                await asyncio.to_thread(write_local_fn, local_target, project_dir, payload)
            except config_write.ConfigWriteError as exc:
                raise map_config_write_error(exc) from exc
        # The written file is the project dir's settings file (hash-guarded surfaces) or
        # the ~/.claude.json the MCP local writer nests into; `surface` disambiguates.
        await config_audit.arecord(
            config.state_dir,
            surface=surface,
            scope="local",
            target=str(project_dir if local_fn_has_hash else local_target),
            action="update",
            actor=SESSION_USER,
            keys=sorted(payload),
        )
        return {"scope": "local", "project": project, "ok": True}
    project = body.get("project")
    config_write.require_confirm("project", project, body.get("confirm"))
    payload = body.get(payload_key)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail=f"body must include a '{payload_key}' object")
    project_dir = resolve_cw_project(config, project, require_exists=True)
    expected = body.get("hash")
    if expected is not None and not isinstance(expected, str):
        raise HTTPException(status_code=422, detail="'hash' must be a string when present")
    try:
        await asyncio.to_thread(write_project_fn, project_dir, payload, expected)
    except config_write.ConfigWriteError as exc:
        raise map_config_write_error(exc) from exc
    await config_audit.arecord(
        config.state_dir,
        surface=surface,
        scope="project",
        target=str(project_dir),
        action="update",
        actor=SESSION_USER,
        keys=sorted(payload),
    )
    return {"scope": "project", "project": project, "ok": True}


def user_settings_json(runner: SessionRunner | None) -> Path:
    """Resolve the user-scope ``settings.json``, failing closed with a 404 without a runner."""
    # User-scope permission rules live in ~/.claude/settings.json (the settings
    # file), NOT ~/.claude.json. Derive it the same way the runner does internally
    # (beside the claude.json whose trusted-dirs we honor) so the two never diverge.
    #
    # The user-scope surface needs a runner to resolve that path. If none is wired
    # (create_app's runner is None — test harnesses / CLI tooling that skip the
    # SessionRunner coercion), fail CLOSED with the same 404-invisible shape
    # require_capability uses for a disabled user scope, rather than letting
    # runner.claude_json raise an AttributeError that escapes as an unhandled 500.
    if runner is None:
        raise HTTPException(status_code=404, detail="config-write user scope is unavailable")
    return runner.claude_json.parent / ".claude" / "settings.json"


def user_claude_json(runner: SessionRunner | None) -> Path:
    """Resolve the user-scope ``~/.claude.json``, failing closed with a 404 without a runner."""
    # The user-scope skills directory (~/.claude/skills/) and the user-scope CLAUDE.md /
    # subagent files all hang off ~/.claude.json. Like user_settings_json this needs a
    # runner to resolve that path; without one wired (a harness/CLI that skipped the
    # SessionRunner coercion) fail CLOSED with the same 404-invisible shape
    # require_capability uses for a disabled user scope, rather than letting a None runner
    # raise an AttributeError that escapes as an unhandled 500. Mirrors the old
    # ``app._user_claude_json_guarded`` closure (#1156).
    if runner is None:
        raise HTTPException(status_code=404, detail="config-write user scope is unavailable")
    return runner.claude_json


def plugin_cli_cwd(
    config: ClausterConfig, runner: SessionRunner | None, scope: str, project: str
) -> Path:
    """Resolve the directory ``claude plugin ...`` should be spawned from for this scope.

    User scope has no project -- the CLI ignores the cwd, so the runner's
    ``~/.claude.json`` parent is a safe, always-present directory (the same choice
    :mod:`config_write_mcp_cli` makes for MCP user-scope calls); fail closed to 404 when no
    runner is wired. Project/local scope MUST exist on disk (``require_exists=True``):
    several verbs' output genuinely depends on this cwd (plugin ``list``'s per-entry
    ``enabled`` field, marketplace declarations visible from it). Mirrors the old
    ``app._plugin_cli_cwd`` closure (#1156).
    """
    if scope == "user":
        return user_claude_json(runner).parent
    return resolve_cw_project(config, project, require_exists=True)
