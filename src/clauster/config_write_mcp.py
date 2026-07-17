"""MCP-server config-write surface (#688) over the #347 Foundation seam.

This child adds exactly two things on top of :mod:`clauster.config_write` (the
fail-closed Foundation — gate, type-the-name confirm, validate-never-execute,
stale-hash guard, structural redaction, subtree writer): a **pure structural
validator** for an MCP server entry, and a **router** that runs the Foundation
pipeline in order for a write. It owns no gate, no writer, and no redaction logic
of its own — it *consumes* the Foundation.

The validator is **structural only**. It inspects shape and types and rejects
anything malformed; it **never resolves, spawns, or "test-runs" the ``command``**
(or any value). Running the candidate to "verify" it would make the validator
itself the RCE the whole trust tier exists to prevent — so it does not.

Three surfaces, one entry shape (mirroring Claude Code's own ``.mcp.json``):

* **project** scope ⇒ ``<project>/.mcp.json`` (top-level ``mcpServers`` object),
  guarded by the stale-hash external-edit check + path containment.
* **local** scope ⇒ ``~/.claude.json`` ``projects[<abs-project-path>].mcpServers`` —
  you, this project only, private to the operator's account (mirrors Claude Code's own
  ``claude mcp add --scope local``, and the existing per-project trust flag at
  ``projects[<abs-project-path>].hasTrustDialogAccepted``, see :mod:`clauster.trust`).
  Written through the locked nested-subtree-merge writer
  (:func:`~clauster.config_write.write_nested_subtree`); no separate file, so no
  gitignore concern (unlike the local-scope permissions/hooks files, this never lives
  inside the project directory).
* **user** scope ⇒ ``~/.claude.json`` ``mcpServers`` subtree, written through the
  locked subtree-merge writer (gated additionally on ``allow_user_scope``).

A server entry is either a **stdio** server (``command`` + optional
``args``/``env``) or a **remote** server (``type``/``transport`` of ``sse``/``http``
+ ``url`` + optional ``headers``). Unknown keys or wrong types ⇒ 422, nothing
written. Secret-shaped ``env`` values and token-bearing ``url``/``headers`` flow
through the Foundation's :func:`~clauster.config_write.redact_secrets` /
:func:`~clauster.config_write.merge_redacted` so the browser never reads a stored
secret out and a returned ``"********"`` sentinel keeps the stored value.

**#769 additions** (CLI-driven writes + enable/disable, over this same Foundation):

* :mod:`clauster.config_write_mcp_cli` drives add/remove/edit through the
  ``claude mcp`` CLI (the design-doc-locked "hybrid" write strategy — CLI for
  MCP/plugins, file writer for skills/subagents/hooks/CLAUDE.md/settings). It
  calls back into this module's :func:`write_project_server_entry` /
  :func:`write_user_server_entry` / :func:`write_project_local_server_entry` for
  every case the CLI cannot safely carry: any entry with a non-empty ``env`` or
  ``headers`` value (or a token-bearing ``url``), which would otherwise sit in the
  ``claude mcp add-json`` argv (visible via ``ps``/``/proc``) for the life of the
  call. Redaction detects secrets by *key name* only, so the routing predicate
  (:func:`clauster.config_write_mcp_cli.entry_needs_direct_write`) deliberately errs
  on ANY inline ``env``/``headers`` value rather than trusting key-name detection.
* **Enable/disable** models project ``.mcp.json`` server approval exactly as
  Claude Code itself does (verified against a live ``claude mcp add-json`` /
  ``reset-project-choices`` run, see #769): two per-project lists,
  ``enabledMcpjsonServers`` / ``disabledMcpjsonServers``, nested at
  ``~/.claude.json`` ``projects[<abs-project-path>]`` — the *same* per-project
  block :func:`read_project_local_servers` / :func:`write_project_local_servers`
  already read/write for local-scope MCP servers (and :mod:`clauster.trust`'s
  ``hasTrustDialogAccepted``). :func:`read_project_approvals` /
  :func:`write_project_approvals` are the read/write pair; ``reset-project-choices``
  has no file-level equivalent clauster models directly — it goes through the
  CLI (:func:`clauster.config_write_mcp_cli.cli_reset_project_choices`), the one
  native verb for it.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from . import config_write as cw

logger = logging.getLogger(__name__)

#: The top-level key holding the server map in both ``.mcp.json`` and ``mcpServers``.
MCP_SERVERS_KEY = "mcpServers"

#: Allowed shape for an MCP **server name**. A name flows through to a positional
#: argument of ``claude mcp add-json <name> …`` / ``claude mcp remove <name> …`` (see
#: :mod:`clauster.config_write_mcp_cli`), so a name that *looks like an option* —
#: ``--scope``, ``--client-secret``, ``-e`` — would be consumed by the CLI's argument
#: parser as a flag rather than the positional name (arg-injection / positional shift;
#: verified against a live ``claude`` 2.1.198, which errors or mis-binds such a name).
#: Constrain it to a conservative identifier charset that CANNOT begin with ``-``: an
#: alphanumeric/underscore first char, then alphanumerics/underscore/dot/hyphen. This is
#: validated structurally (validate-never-execute), so a bad name is a 422 with nothing
#: written, and it is enforced for the direct (non-CLI) writers too so the two write
#: paths never diverge on which names they accept.
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")

#: The top-level ``~/.claude.json`` key holding per-project state, keyed by the
#: resolved absolute project path (the same shape :mod:`clauster.trust` uses for
#: ``hasTrustDialogAccepted``).
PROJECTS_KEY = "projects"

#: The two recognized remote transports (stdio servers carry no transport).
_REMOTE_TRANSPORTS = frozenset({"sse", "http"})

#: Allowed keys for a stdio server entry (``command`` required; rest optional).
_STDIO_KEYS = frozenset({"command", "args", "env", "type", "transport"})

#: Allowed keys for a remote server entry (``url`` required; transport required).
_REMOTE_KEYS = frozenset({"url", "headers", "env", "type", "transport"})


def _validate_str_dict(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a ``dict`` of ``str`` keys to ``str`` values."""
    if not isinstance(value, dict):
        raise cw.InvalidCandidateError(f"{label} must be an object of string values")
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise cw.InvalidCandidateError(f"{label} must map string keys to string values")


def _entry_transport(entry: dict[str, Any]) -> str | None:
    """Return the remote transport declared by ``entry`` (``type``/``transport``), or None.

    ``type`` and ``transport`` are accepted as synonyms (Claude Code has used both).
    A non-string, or the two disagreeing, is a structural error. A stdio entry
    declares neither (or the literal ``"stdio"``) ⇒ returns ``None``.
    """
    declared = {entry[k] for k in ("type", "transport") if k in entry}
    if not declared:
        return None
    if not all(isinstance(v, str) for v in declared):
        raise cw.InvalidCandidateError("server 'type'/'transport' must be a string")
    if len(declared) > 1:
        raise cw.InvalidCandidateError("server 'type' and 'transport' disagree")
    (value,) = declared
    if value == "stdio":
        return None
    if value not in _REMOTE_TRANSPORTS:
        raise cw.InvalidCandidateError(f"unknown server transport {value!r} (want stdio/sse/http)")
    return value


def _validate_server_entry(name: Any, entry: Any) -> None:
    """Structurally validate one ``name -> entry`` MCP server, or raise (→ 422).

    Stdio entries require a non-empty string ``command`` (``args`` a list of
    strings, ``env`` a string→string map); remote entries require a non-empty
    string ``url`` (``headers`` a string→string map). Unknown keys are rejected.
    The ``name`` must additionally match :data:`_SERVER_NAME_RE` (no leading ``-``,
    identifier charset) so it can never be mis-parsed as a CLI option when it reaches
    ``claude mcp``'s argv — see that constant. **Nothing here resolves, spawns, or
    runs the command/url** — shape only.
    """
    if not isinstance(name, str) or not name:
        raise cw.InvalidCandidateError("server name must be a non-empty string")
    if not _SERVER_NAME_RE.match(name):
        raise cw.InvalidCandidateError(
            f"server name {name!r} must match {_SERVER_NAME_RE.pattern} "
            "(identifier chars, no leading '-')"
        )
    if not isinstance(entry, dict):
        raise cw.InvalidCandidateError(f"server {name!r} must be an object")

    transport = _entry_transport(entry)
    is_remote = transport is not None
    allowed = _REMOTE_KEYS if is_remote else _STDIO_KEYS
    unknown = set(entry) - allowed
    if unknown:
        raise cw.InvalidCandidateError(f"server {name!r} has unknown keys: {sorted(unknown)}")

    if "env" in entry:
        _validate_str_dict(entry["env"], f"server {name!r} 'env'")

    if is_remote:
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise cw.InvalidCandidateError(
                f"remote server {name!r} requires a non-empty string 'url'"
            )
        if "headers" in entry:
            _validate_str_dict(entry["headers"], f"server {name!r} 'headers'")
        return

    # stdio
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise cw.InvalidCandidateError(
            f"stdio server {name!r} requires a non-empty string 'command'"
        )
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise cw.InvalidCandidateError(f"server {name!r} 'args' must be a list of strings")


def validate_mcp_servers(candidate: Any) -> None:
    """Structural validator for the whole ``mcpServers`` map (the Foundation hook).

    ``candidate`` is the desired ``mcpServers`` object: a ``dict`` mapping each
    server name to its entry. Every entry is validated structurally; a single bad
    entry rejects the whole write (→ 422 via :func:`config_write.validate_candidate`),
    so a partial/garbled map never lands. **Never executes any value.**
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError("mcpServers must be an object")
    for name, entry in candidate.items():
        _validate_server_entry(name, entry)


def read_project_servers(project_dir: Path) -> tuple[dict[str, Any], str]:
    """Return ``(redacted_servers, content_hash)`` for a project's ``.mcp.json``.

    The hash is over the *current file bytes* (empty digest when absent) — the caller
    echoes it back on write so the stale-hash guard can reject a stale write (409).
    Secret-shaped values are masked by the Foundation's structural redaction before
    they ever leave this function, so the browser never reads a stored secret. A
    missing file reads as an empty server map.
    """
    path = project_dir / ".mcp.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = cw.load_settings_json_obj(raw)
    servers = data.get(MCP_SERVERS_KEY)
    servers = servers if isinstance(servers, dict) else {}
    return cw.redact_secrets(servers), cw.hash_bytes(raw)


def read_user_servers(claude_json: Path) -> dict[str, Any]:
    """Return the redacted user-scope ``mcpServers`` map from ``claude_json``.

    User scope has no stale-hash token (the locked subtree-merge writer reads the
    file under its own ``flock`` at write time); read is for display only, with the
    Foundation's structural redaction applied so no stored secret is surfaced.
    """
    try:
        raw = claude_json.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = cw.load_settings_json_obj(raw)
    servers = data.get(MCP_SERVERS_KEY)
    servers = servers if isinstance(servers, dict) else {}
    return cw.redact_secrets(servers)


def write_project_servers(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the project ``.mcp.json`` ``mcpServers`` map, fail-closed.

    Pipeline (each step aborts before the write): validate the candidate structurally
    (→ 422) → stale-hash external-edit guard over the current bytes (→ 409) →
    :func:`config_write.merge_redacted` so a returned ``"********"`` keeps the stored
    secret → atomic replace of the rendered file. The caller must have already run the
    capability gate, the type-the-name confirm, and path containment.

    The whole read-merge-write runs inside the Foundation's
    :func:`~clauster.config_write.write_settings_subtree` transaction — the same
    hardened ``flock`` + one-time ``.bak`` + unique-``mkstemp`` + mode-preserving +
    atomic-``os.replace`` machinery — so two concurrent project writers can't lose an
    update and the file's permission bits are preserved. The stale-hash check runs
    against the bytes read *under the lock* (no TOCTOU window).

    The candidate is **never executed**; only its shape is checked.
    """
    cw.validate_candidate(incoming, validate_mcp_servers)
    path = project_dir / ".mcp.json"
    cw.write_settings_subtree(
        path, MCP_SERVERS_KEY, incoming, expected_hash, merge=cw.merge_redacted
    )


def write_user_servers(claude_json: Path, incoming: dict[str, Any]) -> None:
    """Validate + write the user-scope ``mcpServers`` subtree, fail-closed.

    Validates structurally (→ 422), then runs the Foundation's locked subtree-merge
    writer: it reads the *current* ``mcpServers`` under the ``flock``, merges via
    :func:`config_write.merge_redacted` (so a ``"********"`` keeps the stored secret),
    and atomically replaces only that subtree — every other key in ``~/.claude.json``
    (trust grants, tokens) is preserved. Never executes the candidate.
    """
    cw.validate_candidate(incoming, validate_mcp_servers)

    def _mutate(current: Any) -> dict[str, Any]:
        stored = current if isinstance(current, dict) else {}
        return cw.merge_redacted(incoming, stored)

    cw.write_subtree(claude_json, MCP_SERVERS_KEY, _mutate)


def read_project_local_servers(claude_json: Path, project_dir: Path) -> dict[str, Any]:
    """Return the redacted local-scope ``mcpServers`` map for ``project_dir``.

    Reads ``~/.claude.json`` ``projects[<abs-project-path>].mcpServers`` — private to
    the operator's account for this one project, never a shared file. Read is for
    display only (no stale-hash token, same as :func:`read_user_servers`: the locked
    nested-subtree writer reads the file under its own ``flock`` at write time), with
    the Foundation's structural redaction applied so no stored secret is surfaced. A
    missing project entry (or a missing file) reads as an empty server map.
    """
    servers = cw.read_nested_subtree(claude_json, PROJECTS_KEY, str(project_dir), MCP_SERVERS_KEY)
    servers = servers if isinstance(servers, dict) else {}
    return cw.redact_secrets(servers)


def write_project_local_servers(
    claude_json: Path, project_dir: Path, incoming: dict[str, Any]
) -> None:
    """Validate + write the local-scope ``mcpServers`` map for ``project_dir``, fail-closed.

    Third (local) scope, sibling of the project/user writers above: validates
    structurally (→ 422), then runs the Foundation's locked *nested*-subtree-merge
    writer (:func:`~clauster.config_write.write_nested_subtree`) — it reads the
    *current* ``projects[<abs-project-path>].mcpServers`` under the ``flock``, merges
    via :func:`~clauster.config_write.merge_redacted` (so a ``"********"`` keeps the
    stored secret), and atomically replaces only that one nested leaf — every other
    project's entry, every other subtree of *this* project's entry, and every other
    top-level key in ``~/.claude.json`` (trust grants, tokens) is preserved. Never
    executes the candidate. ``project_dir`` must already be the *resolved* absolute
    path (the caller's path-containment step), so the ``projects`` key it writes under
    matches the one :mod:`clauster.trust` reads/writes for the same project.
    """
    cw.validate_candidate(incoming, validate_mcp_servers)

    def _mutate(current: Any) -> dict[str, Any]:
        stored = current if isinstance(current, dict) else {}
        return cw.merge_redacted(incoming, stored)

    cw.write_nested_subtree(claude_json, PROJECTS_KEY, str(project_dir), MCP_SERVERS_KEY, _mutate)


# ---------------------------------------------------------------------------
# #769: single-entry merge helpers (the secret-safe alternative to the CLI add)
# ---------------------------------------------------------------------------


class ServerExistsError(cw.ConfigWriteError):
    """An ``add`` targeted a server name that already exists (→ 409, never clobber)."""


class ServerNotFoundError(cw.ConfigWriteError):
    """A ``remove``/edit-remove targeted a server name that doesn't exist (→ 404)."""


def write_project_server_entry(
    project_dir: Path, name: str, entry: dict[str, Any], *, op: str
) -> None:
    """Merge one server ``entry`` into the project's stored map, fail-closed.

    The secret-safe twin of :func:`clauster.config_write_mcp_cli.cli_add_server`:
    used when ``entry`` carries a potential inline secret (see
    :func:`clauster.config_write_mcp_cli.entry_needs_direct_write`), which the CLI's
    ``add-json`` argv cannot carry without exposing it via ``ps``/``/proc``. Reads
    the current *redacted* map, folds ``entry`` in under ``name`` (every sibling
    server's masked value round-trips to its real stored secret via
    :func:`~clauster.config_write.merge_redacted`'s keep-stored rule inside
    :func:`write_project_servers`), and writes atomically. ``op="add"`` refuses
    (:class:`ServerExistsError`) to clobber a name that already exists — matching
    ``claude mcp add-json``'s own "already exists" refusal on the CLI path;
    ``op="edit"`` always overwrites (remove+re-add semantics collapsed into one
    merge, since there is no separate value to remove first).
    """
    redacted, file_hash = read_project_servers(project_dir)
    if op == "add" and name in redacted:
        raise ServerExistsError(f"MCP server {name!r} already exists in project scope")
    incoming = {**redacted, name: entry}
    write_project_servers(project_dir, incoming, expected_hash=file_hash)


def write_user_server_entry(
    claude_json: Path, name: str, entry: dict[str, Any], *, op: str
) -> None:
    """User-scope twin of :func:`write_project_server_entry` — see its docstring."""
    redacted = read_user_servers(claude_json)
    if op == "add" and name in redacted:
        raise ServerExistsError(f"MCP server {name!r} already exists in user scope")
    incoming = {**redacted, name: entry}
    write_user_servers(claude_json, incoming)


def write_project_local_server_entry(
    claude_json: Path, project_dir: Path, name: str, entry: dict[str, Any], *, op: str
) -> None:
    """Local-scope twin of :func:`write_project_server_entry` — see its docstring."""
    redacted = read_project_local_servers(claude_json, project_dir)
    if op == "add" and name in redacted:
        raise ServerExistsError(f"MCP server {name!r} already exists in local scope")
    incoming = {**redacted, name: entry}
    write_project_local_servers(claude_json, project_dir, incoming)


# ---------------------------------------------------------------------------
# #769: UNREDACTED single-entry snapshots (edit-rollback ONLY — never client-facing)
# ---------------------------------------------------------------------------
#
# The public readers above all redact before returning, so a stored secret never
# leaves the process on the display path. The edit orchestration
# (:func:`clauster.config_write_mcp_cli.cli_edit_server`) removes-then-re-adds; if the
# re-add fails after the remove succeeded, the previous definition must be restored
# *verbatim* — which needs its real (unredacted) value. These snapshot readers exist
# solely for that same-request, in-memory rollback: their result is written straight
# back to disk by the direct writer on failure and is NEVER serialized into a response
# or a log. Keep them private to the edit path.


def _raw_project_server(project_dir: Path, name: str) -> dict[str, Any] | None:
    """Return the UNREDACTED stored ``.mcp.json`` entry for ``name`` (rollback snapshot)."""
    path = project_dir / ".mcp.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    servers = cw.load_settings_json_obj(raw).get(MCP_SERVERS_KEY)
    value = servers.get(name) if isinstance(servers, dict) else None
    return value if isinstance(value, dict) else None


def _raw_user_server(claude_json: Path, name: str) -> dict[str, Any] | None:
    """Return the UNREDACTED stored user-scope entry for ``name`` (rollback snapshot)."""
    try:
        raw = claude_json.read_bytes()
    except FileNotFoundError:
        return None
    servers = cw.load_settings_json_obj(raw).get(MCP_SERVERS_KEY)
    value = servers.get(name) if isinstance(servers, dict) else None
    return value if isinstance(value, dict) else None


def _raw_project_local_server(
    claude_json: Path, project_dir: Path, name: str
) -> dict[str, Any] | None:
    """Return the UNREDACTED stored local-scope entry for ``name`` (rollback snapshot)."""
    servers = cw.read_nested_subtree(claude_json, PROJECTS_KEY, str(project_dir), MCP_SERVERS_KEY)
    value = servers.get(name) if isinstance(servers, dict) else None
    return value if isinstance(value, dict) else None


def snapshot_server_entry(
    scope: cw.Scope, name: str, *, claude_json: Path, project_dir: Path
) -> dict[str, Any] | None:
    """Return the UNREDACTED stored entry for ``(scope, name)``, or ``None`` if absent.

    The single public entry point for the edit-rollback snapshot — dispatches to the
    per-scope raw readers above. **For same-request, in-memory rollback ONLY:** the
    result is written straight back to disk by the direct writer if a remove+re-add
    edit fails partway, and must NEVER be serialized into a response or a log (that is
    what the redacted public readers are for). ``project_dir`` is ignored for user
    scope.
    """
    if scope == "user":
        return _raw_user_server(claude_json, name)
    if scope == "local":
        return _raw_project_local_server(claude_json, project_dir, name)
    return _raw_project_server(project_dir, name)


# ---------------------------------------------------------------------------
# #769: project `.mcp.json` server approval state (enable/disable)
# ---------------------------------------------------------------------------

#: Per-project list of ``.mcp.json`` server names the operator has approved to load.
ENABLED_KEY = "enabledMcpjsonServers"

#: Per-project list of ``.mcp.json`` server names the operator has rejected.
DISABLED_KEY = "disabledMcpjsonServers"


def _validate_name_list(candidate: Any, label: str) -> None:
    """Reject ``candidate`` unless it is a list of non-empty strings."""
    if not isinstance(candidate, list) or not all(isinstance(v, str) and v for v in candidate):
        raise cw.InvalidCandidateError(f"{label} must be a list of non-empty strings")


def validate_approvals(candidate: Any) -> None:
    """Structural validator for the ``{"enabled": [...], "disabled": [...]}`` shape.

    Project ``.mcp.json`` servers require operator approval before Claude Code will
    load them (an un-approved server shows as "⏸ Pending approval"); approval state
    is exactly this pair of name lists (verified against a live ``claude mcp
    add-json`` + ``reset-project-choices`` run, #769). A name may not appear in both
    lists (a self-contradicting approve+reject) and neither list may contain a
    duplicate — both reject (→ 422) so the stored lists stay clean sets. **Never**
    resolves or spawns anything named in either list — shape only.
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError("approvals must be an object")
    unknown = set(candidate) - {"enabled", "disabled"}
    if unknown:
        raise cw.InvalidCandidateError(f"approvals has unknown keys: {sorted(unknown)}")
    enabled = candidate.get("enabled", [])
    disabled = candidate.get("disabled", [])
    _validate_name_list(enabled, "'enabled'")
    _validate_name_list(disabled, "'disabled'")
    if len(set(enabled)) != len(enabled):
        raise cw.InvalidCandidateError("'enabled' contains a duplicate server name")
    if len(set(disabled)) != len(disabled):
        raise cw.InvalidCandidateError("'disabled' contains a duplicate server name")
    overlap = set(enabled) & set(disabled)
    if overlap:
        raise cw.InvalidCandidateError(f"server(s) both enabled and disabled: {sorted(overlap)}")


def read_project_approvals(claude_json: Path, project_dir: Path) -> dict[str, list[str]]:
    """Return ``{"enabled": [...], "disabled": [...]}`` for ``project_dir``.

    No secret ever lives in an approval list (server *names* only), so unlike the
    server-map readers above this needs no redaction. A missing project entry (or
    missing file) reads as two empty lists — never approved, never rejected.

    **Folds in the settings-file top-level approvals (#850 / #958 P2).** claude honors
    ``enabledMcpjsonServers`` / ``disabledMcpjsonServers`` at the TOP LEVEL of the settings
    files too, and ``claude mcp add-json --scope local|user`` RELOCATES a project approval
    OUT of ``~/.claude.json`` into ``settings.local.json`` (clearing the ``projects[]``
    entry). A panel that read only ``~/.claude.json`` therefore showed such an approved
    server as un-approved (DF-9). Reflect every source claude honors, more-specific wins
    (``~/.claude.json`` < user < project < local), so the panel state matches both what
    claude loads and the :func:`unapproved_mcp_servers` preflight.

    Read-only reflection only: :func:`write_project_approvals` still targets
    ``~/.claude.json``, so an approval that lives *solely* in a settings file can be
    re-approved here but not *unset* from here — clearing it stays a settings-surface /
    ``claude mcp`` action. This closes the read-consistency gap, not the write asymmetry.

    To keep the panel from offering a change that silently won't take, the result also
    carries a ``"locked"`` list: every server whose effective decision is owned by a
    settings file (it appears in some ``settings*.json`` top-level list). A settings
    decision overrides the ``~/.claude.json`` the writer targets, so any panel toggle of
    a locked server would be reverted on reload; the panel renders those rows read-only
    and points the operator at the settings surface / ``claude mcp`` instead (#958 P2 /
    DF-9 write-asymmetry guard).
    """
    enabled = cw.read_nested_subtree(claude_json, PROJECTS_KEY, str(project_dir), ENABLED_KEY)
    disabled = cw.read_nested_subtree(claude_json, PROJECTS_KEY, str(project_dir), DISABLED_KEY)
    # Base layer: the legacy ~/.claude.json projects[] lists (lowest precedence). Elements
    # are passed through verbatim (as the pre-#958 read did); the settings overlay below is
    # already string-filtered by _settings_mcp_lists.
    state: dict[str, str] = {}
    for name in enabled if isinstance(enabled, list) else []:
        state[name] = "enabled"
    for name in disabled if isinstance(disabled, list) else []:
        state[name] = "disabled"
    # Overlay the settings files, lowest → highest precedence (local wins). A later
    # source's decision for a given name overrides an earlier one; the DF-9 relocation
    # target (settings.local.json) is therefore authoritative. Every name a settings file
    # decides is `locked`: the writer can't change it via ~/.claude.json (settings wins).
    locked: set[str] = set()
    for settings_path in (
        claude_json.parent / ".claude" / "settings.json",
        cw.project_settings_path(project_dir),
        cw.project_local_settings_path(project_dir),
    ):
        s_enabled, s_disabled = _settings_mcp_lists(settings_path)
        for name in s_enabled:
            state[name] = "enabled"
            locked.add(name)
        for name in s_disabled:
            state[name] = "disabled"
            locked.add(name)
    return {
        "enabled": [name for name, decision in state.items() if decision == "enabled"],
        "disabled": [name for name, decision in state.items() if decision == "disabled"],
        "locked": sorted(locked),
    }


def write_project_approvals(
    claude_json: Path, project_dir: Path, enabled: list[str], disabled: list[str]
) -> None:
    """Validate + write both approval lists for ``project_dir`` in one transaction.

    Validates structurally (→ 422, nothing written), then sets **both**
    ``enabledMcpjsonServers`` and ``disabledMcpjsonServers`` under
    ``projects[<abs-project-path>]`` inside a single locked
    :func:`~clauster.claude_json.update_claude_json` transaction — not two separate
    :func:`~clauster.config_write.write_nested_subtree` calls — so the pair can
    never be observed (or crash-interrupted) half-written. Every sibling key at
    every level (this project's ``mcpServers``/trust flags, every other project,
    every other top-level key) is preserved verbatim.
    """
    candidate = {"enabled": enabled, "disabled": disabled}
    cw.validate_candidate(candidate, validate_approvals)
    key = str(project_dir)

    def _apply(data: dict) -> None:
        outer = data.get(PROJECTS_KEY)
        if not isinstance(outer, dict):
            outer = {}
            data[PROJECTS_KEY] = outer
        inner = outer.get(key)
        if not isinstance(inner, dict):
            inner = {}
        inner[ENABLED_KEY] = list(enabled)
        inner[DISABLED_KEY] = list(disabled)
        outer[key] = inner

    cw.update_claude_json(claude_json, _apply)


# ---------------------------------------------------------------------------
# #837: unapproved-server pre-flight (read-only; never mutates config)
# ---------------------------------------------------------------------------


def _settings_mcp_lists(settings_path: Path) -> tuple[list[str], list[str]]:
    """Return ``(enabled, disabled)`` top-level MCP name lists from a settings FILE.

    claude honors top-level ``enabledMcpjsonServers`` / ``disabledMcpjsonServers`` in the
    ``.claude/settings.json`` / ``settings.local.json`` / ``~/.claude/settings.json`` files
    (#850), in addition to the ``~/.claude.json`` ``projects[]`` lists that
    :func:`read_project_approvals` reads. Unlike ``~/.claude.json`` these keys sit at the
    file's TOP LEVEL, not under a per-project subtree. Fail-safe: a missing / unreadable /
    malformed file contributes nothing (two empty lists) rather than raising — every reader
    of these (the launch preflight AND the approvals panel) must never block or crash on it.
    """
    try:
        raw = settings_path.read_bytes()
    except OSError:
        return [], []
    try:
        data = cw.load_settings_json_obj(raw)
    except cw.ConfigWriteError:
        # Malformed / non-object settings file — the config-write panel surfaces its own
        # error; here we simply contribute nothing rather than fail the caller.
        return [], []

    def _names(key: str) -> list[str]:
        names = data.get(key)
        return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []

    return _names(ENABLED_KEY), _names(DISABLED_KEY)


def unapproved_mcp_servers(claude_json: Path, project_dir: Path) -> list[str]:
    """Return committed ``.mcp.json`` server names still awaiting operator approval.

    A name is "unapproved" when it is a key of the project's ``.mcp.json``
    ``mcpServers`` object but appears in **neither** :data:`ENABLED_KEY` nor
    :data:`DISABLED_KEY` for this project (see :func:`read_project_approvals`) —
    exactly the set an interactive (pty) ``claude`` launch would block on at its
    "N new MCP servers found — enable?" startup gate, since that prompt cannot be
    answered through the read-only live-terminal view (#837). Reuses the existing
    readers (:func:`read_project_servers` for the committed server names,
    :func:`read_project_approvals` for the two approval lists) rather than
    re-parsing either file directly, so this can never drift from what the
    Server-approvals panel itself reads.

    ``project_dir`` is resolved before the approvals lookup: the config-write
    surface always keys ``projects[<abs-project-path>]`` by the *resolved* absolute
    path (:func:`~clauster.config_write.resolve_project_dir`), but a discovery-scan
    ``Project.path`` is not guaranteed resolved (e.g. a symlinked ``projects_root``)
    — resolving here keeps this lookup aligned with where an approval actually gets
    written, so it can never spuriously warn (or spuriously go quiet) on a path that
    is byte-different but points at the same directory the Server-approvals panel
    already approved.

    Fail-safe by construction, never raises:

    * no ``.mcp.json`` (or an empty ``mcpServers``) ⇒ ``[]`` — nothing to warn about.
    * either file is unreadable or malformed (bad JSON, non-UTF-8, non-object) ⇒
      logged at ``warning`` and treated as "cannot determine" ⇒ ``[]``, so a
      preflight read failure never blocks (or crashes) a launch.

    The returned order matches ``.mcp.json``'s own key order (stable, not sorted),
    so a repeat call over an unchanged file always warns in the same order.
    """
    try:
        servers, _hash = read_project_servers(project_dir)
    except (cw.ConfigWriteError, OSError) as exc:
        logger.warning(
            "could not read %s for MCP-approval preflight: %s", project_dir / ".mcp.json", exc
        )
        return []
    if not servers:
        return []
    try:
        resolved_dir = project_dir.resolve()
        approvals = read_project_approvals(claude_json, resolved_dir)
    except (cw.ConfigWriteError, OSError) as exc:
        logger.warning(
            "could not read MCP approvals in %s for %s: %s", claude_json, project_dir, exc
        )
        return []
    # `approvals` already folds in the settings-file top-level enabled/disabledMcpjsonServers
    # that claude ALSO honors (#850 / #958 P2 — see read_project_approvals), so "decided in
    # ANY honored source ⇒ no enable gate" is exactly this union; nothing further to merge.
    decided = set(approvals["enabled"]) | set(approvals["disabled"])
    return [name for name in servers if name not in decided]
