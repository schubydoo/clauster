"""The config-write MCP-server routes (#1156).

The MCP surface of the code-executing config-write tier: read the structurally
redacted server map, replace the whole map, add/edit/remove a single entry (CLI- or
direct-writer driven), and read/replace/reset a project's ``.mcp.json`` approval
lists. Every route runs behind the same fail-closed gate order as the rest of the
tier: capability first (404, invisible surface), then confirm, then path containment
before any I/O.

Every route here injects ``RunnerDep`` (fail-closed): the whole-map write, the
single-entry ``mcp/server`` op, the approvals read/write, and the reset all dereference
``runner.claude_json``. ``create_app`` coerces ``runner = runner or SessionRunner(config)``
and publishes that object, so ``app.state.runner`` is never ``None`` in a wired app; the
accessor's 404 fires only in a harness that nulls it out, and it fires ahead of the
capability check. That is the one spot in the config-write tier where the runner accessor
resolves before ``require_capability`` -- but it returns the same 404 a disabled surface
would, so the invisible-surface invariant still holds. Failing closed here (404) is also
safer than the nullable accessor the permissions/hooks reads use, whose user/local branches
would raise an unhandled 500 on a ``None`` runner.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from ... import config_write, config_write_mcp, config_write_mcp_cli
from ...dependencies import ConfigDep, RunnerDep
from . import _base

router = APIRouter()


@router.get("/api/config-write/mcp")
async def api_config_write_mcp_read(
    config: ConfigDep, runner: RunnerDep, scope: str = "project", project: str = ""
) -> dict:
    """Return the structurally redacted MCP server map for a surface."""
    # Gated exactly like the status route: 404 when config-write is off, and 404 for
    # user scope when allow_user_scope is off — the surface is invisible, never 403.
    # Capability gate FIRST, before the scope-enum check, so a disabled surface 404s for ANY
    # request (a bogus scope included) instead of leaking existence via a differing
    # 422 — the #819/#768 invisible-surface invariant.
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")
    if scope == "user":
        try:
            servers = await asyncio.to_thread(
                config_write_mcp.read_user_servers, runner.claude_json
            )
        except config_write.ConfigWriteError as exc:
            # A corrupt/non-object/non-UTF-8 ~/.claude.json raises InvalidCandidateError
            # from _load_json_obj — same as the project read below; map it to a clean 422
            # rather than letting it escape as an unhandled 500.
            raise _base.map_config_write_error(exc) from exc
        return {"scope": "user", "servers": servers, "hash": None}
    if scope == "local":
        project_dir = _base.resolve_cw_project(config, project)
        try:
            servers = await asyncio.to_thread(
                config_write_mcp.read_project_local_servers, runner.claude_json, project_dir
            )
        except config_write.ConfigWriteError as exc:
            raise _base.map_config_write_error(exc) from exc
        return {"scope": "local", "project": project, "servers": servers, "hash": None}
    project_dir = _base.resolve_cw_project(config, project)
    try:
        servers, file_hash = await asyncio.to_thread(
            config_write_mcp.read_project_servers, project_dir
        )
    except config_write.ConfigWriteError as exc:
        # A corrupt/non-object on-disk .mcp.json raises InvalidCandidateError from
        # _load_json_obj. Map it through the same helper as the PUT route so a
        # hand-edited or partially-written file is reported as a clean 422, never
        # an unhandled 500.
        raise _base.map_config_write_error(exc) from exc
    return {"scope": "project", "project": project, "servers": servers, "hash": file_hash}


@router.put("/api/config-write/mcp")
async def api_config_write_mcp_write(config: ConfigDep, runner: RunnerDep, body: dict) -> dict:
    """Replace the whole MCP server map for a surface."""
    return await _base.put_config_write(
        config,
        body,
        "servers",
        surface="mcp",
        write_user_fn=config_write_mcp.write_user_servers,
        write_project_fn=config_write_mcp.write_project_servers,
        write_local_fn=config_write_mcp.write_project_local_servers,
        get_user_path=lambda: runner.claude_json,
        user_fn_has_hash=False,
        local_fn_has_hash=False,
        get_local_target=lambda: runner.claude_json,
    )


@router.post("/api/config-write/mcp/server")
async def api_config_write_mcp_server(config: ConfigDep, runner: RunnerDep, body: dict) -> dict:
    """Add, edit, or remove a single MCP server entry behind the config-write gate."""
    # CLI-driven add/remove/edit (#769) over the same Foundation gate the PUT
    # (whole-map) route uses. Order mirrors the Foundation docstring exactly:
    # capability (404, FIRST — a disabled surface 404s for ANY request, a bogus
    # scope included, so it never leaks existence via a differing 422; #819/#768)
    # -> scope shape (422) -> confirm (400, FIRST semantic gate, so it fires even
    # against a garbled op/name/entry) -> op/name/entry shape (422) -> path resolve
    # (400/404) -> the CLI/direct-write dispatch itself (409 already-exists, 404
    # not-found, or 400 for any other CLI failure).
    scope = body.get("scope", "project")
    config_write.require_capability(config, scope)  # type: ignore[arg-type]
    if scope not in ("project", "user", "local"):
        raise HTTPException(status_code=422, detail="scope must be 'project', 'user', or 'local'")

    project = body.get("project")
    config_write.require_confirm(
        scope,
        None if scope == "user" else project,
        body.get("confirm"),  # type: ignore[arg-type]
    )

    op = body.get("op")
    if op not in ("add", "remove", "edit"):
        raise HTTPException(status_code=422, detail="op must be 'add', 'remove', or 'edit'")
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=422, detail="body must include a non-empty 'name' string")

    entry = None
    if op in ("add", "edit"):
        entry = body.get("entry")
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=422, detail="body must include an 'entry' object for add/edit"
            )
        try:
            config_write.validate_candidate({name: entry}, config_write_mcp.validate_mcp_servers)
        except config_write.InvalidCandidateError as exc:
            raise _base.map_config_write_error(exc) from exc

    client_secret = body.get("client_secret")
    if client_secret is not None and not isinstance(client_secret, str):
        raise HTTPException(
            status_code=422, detail="'client_secret' must be a string when present"
        )
    # An OAuth client-secret is only deliverable through the CLI (which passes it via
    # MCP_CLIENT_SECRET in the child env). An entry that must bypass the CLI — inline
    # env/headers, or a url carrying a query/userinfo/fragment — takes the direct
    # writer, which has nowhere to put it. Refuse rather than write the entry and
    # silently drop the secret: the operator would believe it was stored and only
    # discover otherwise when the server fails to authenticate.
    if client_secret is not None and entry is not None:
        if config_write_mcp_cli.entry_needs_direct_write(entry):
            raise HTTPException(
                status_code=422,
                detail=(
                    "'client_secret' cannot be stored for this entry: it carries a "
                    "value that must be kept off the CLI's argv (inline env/headers, "
                    "or a url with a query string, userinfo, or fragment), so it is "
                    "written directly to the config file, which has no way to deliver "
                    "the secret. Put the credential in the entry's 'env' or 'headers' "
                    "instead."
                ),
            )

    if scope == "user":
        cli_cwd = runner.claude_json.parent
    else:
        cli_cwd = _base.resolve_cw_project(config, project, require_exists=True)
    binary = config.claude.binary

    def _direct_write(target_entry: dict, target_op: str) -> None:
        """Write one entry with this scope's direct, non-spawning writer."""
        # The #766 direct (non-spawning) writers, one per scope. Used for any entry
        # that must never reach the CLI's argv, and as the edit-rollback restore.
        if scope == "user":
            config_write_mcp.write_user_server_entry(
                runner.claude_json, name, target_entry, op=target_op
            )
        elif scope == "local":
            config_write_mcp.write_project_local_server_entry(
                runner.claude_json, cli_cwd, name, target_entry, op=target_op
            )
        else:
            config_write_mcp.write_project_server_entry(cli_cwd, name, target_entry, op=target_op)

    def _snapshot_prior() -> dict | None:
        """Read the current entry unredacted, in memory only, to enable an edit rollback."""
        # UNREDACTED single-entry read for the edit-rollback (in-memory, same request,
        # never serialized to a response/log — see config_write_mcp.snapshot_server_entry).
        return config_write_mcp.snapshot_server_entry(
            scope,  # type: ignore[arg-type]
            name,
            claude_json=runner.claude_json,
            project_dir=cli_cwd,
        )

    def _work() -> None:
        """Dispatch the add/edit/remove to the CLI or to the direct writer."""
        if op == "remove":
            config_write_mcp_cli.cli_remove_server(binary, cli_cwd, name, scope)  # type: ignore[arg-type]
            return
        # add / edit always carry an `entry` (validated above); narrow it here so the
        # writers see a concrete dict (defensive — the op-gate guarantees it is set).
        if entry is None:  # pragma: no cover - add/edit always populate `entry` above
            raise RuntimeError("internal: add/edit reached _work with no entry")
        # An entry carrying an inline env/headers value (or a secret-shaped url) can
        # never reach the CLI's argv — err toward the direct #766 writer (same file
        # state, no subprocess). See entry_needs_direct_write.
        if config_write_mcp_cli.entry_needs_direct_write(entry):
            _direct_write(entry, op)
            return
        if op == "add":
            config_write_mcp_cli.cli_add_server(
                binary,
                cli_cwd,
                name,
                entry,
                scope,
                client_secret=client_secret,  # type: ignore[arg-type]
            )
        else:
            # Capture the prior definition BEFORE cli_edit_server runs the remove, so
            # a re-add failure can restore it verbatim via the direct writer (a prior
            # secret is thus never re-exposed on argv). op="edit" overwrites in place.
            prior = _snapshot_prior()

            def _restore() -> bool:
                """Put the pre-edit entry back, reporting whether one actually existed."""
                # Return whether a prior actually existed and was restored, so
                # cli_edit_server reports "restored" only when that is true.
                if prior is None:
                    return False
                _direct_write(prior, "edit")
                return True

            config_write_mcp_cli.cli_edit_server(
                binary,
                cli_cwd,
                name,
                entry,
                scope,
                client_secret=client_secret,  # type: ignore[arg-type]
                restore=_restore,
            )

    # Run the mutation (direct OR CLI-driven) and record the base audit line enriched
    # with which files it changed + the redacted `claude mcp` argv it ran (#958 P6).
    try:
        await _base.audit_config_write(
            config,
            work=_work,
            watch=_base.config_write_watch(runner, cli_cwd),
            surface="mcp",
            scope=scope,  # type: ignore[arg-type]
            target=name,
            action=op,
            actor=_base.SESSION_USER,
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    result = {"scope": scope, "name": name, "op": op, "ok": True}
    if scope != "user":
        result["project"] = project
    return result


@router.get("/api/config-write/mcp/approvals")
async def api_config_write_mcp_approvals_read(
    config: ConfigDep, runner: RunnerDep, project: str = ""
) -> dict:
    """Return the project's ``.mcp.json`` server approval lists."""
    # Project `.mcp.json` server approvals (#769) are inherently project-scope
    # only — local/user-scope servers carry no approval step, only a committed
    # .mcp.json server does — so this reads/writes at "project" scope alone,
    # gated exactly like the other config-write surfaces (404 when disabled).
    config_write.require_capability(config, "project")
    project_dir = _base.resolve_cw_project(config, project)
    approvals = await asyncio.to_thread(
        config_write_mcp.read_project_approvals, runner.claude_json, project_dir
    )
    return {"project": project, **approvals}


@router.put("/api/config-write/mcp/approvals")
async def api_config_write_mcp_approvals_write(
    config: ConfigDep, runner: RunnerDep, body: dict
) -> dict:
    """Replace the project's enabled/disabled MCP approval lists."""
    config_write.require_capability(config, "project")
    project = body.get("project")
    config_write.require_confirm("project", project, body.get("confirm"))
    enabled = body.get("enabled")
    disabled = body.get("disabled")
    if not isinstance(enabled, list) or not isinstance(disabled, list):
        raise HTTPException(
            status_code=422, detail="body must include 'enabled' and 'disabled' lists"
        )
    project_dir = _base.resolve_cw_project(config, project, require_exists=True)
    try:
        await _base.audit_config_write(
            config,
            work=lambda: config_write_mcp.write_project_approvals(
                runner.claude_json, project_dir, enabled, disabled
            ),
            watch=_base.config_write_watch(runner, project_dir),
            surface="mcp-approvals",
            scope="project",
            target=str(runner.claude_json),
            action="update",
            actor=_base.SESSION_USER,
            keys=sorted(set(enabled) | set(disabled)),
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    return {"project": project, "ok": True}


@router.post("/api/config-write/mcp/reset-project-choices")
async def api_config_write_mcp_reset_project_choices(
    config: ConfigDep, runner: RunnerDep, body: dict
) -> dict:
    """Clear both of the project's approval lists via the CLI's reset verb."""
    # The one enable/disable-adjacent operation with a real CLI verb (#769) —
    # `claude mcp reset-project-choices` clears both approval lists for the
    # project at `cli_cwd`. Gated + confirmed like the approvals routes above.
    config_write.require_capability(config, "project")
    project = body.get("project")
    config_write.require_confirm("project", project, body.get("confirm"))
    project_dir = _base.resolve_cw_project(config, project, require_exists=True)
    try:
        await _base.audit_config_write(
            config,
            work=lambda: config_write_mcp_cli.cli_reset_project_choices(
                config.claude.binary, project_dir
            ),
            watch=_base.config_write_watch(runner, project_dir),
            surface="mcp-approvals",
            scope="project",
            target=str(runner.claude_json),
            action="reset",
            actor=_base.SESSION_USER,
        )
    except config_write.ConfigWriteError as exc:
        raise _base.map_config_write_error(exc) from exc
    return {"project": project, "ok": True}
